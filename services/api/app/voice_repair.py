"""Explicit, evidence-bound whole-take recovery; no provider execution here."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from app.budget import BudgetLimitError, enforce_reservation
from app.db import (AssetRepository, AudioAssetRepository, BudgetPolicyRepository,
    ClipRepository, Database, JobRepository, ProjectDraftRepository, ProviderCallRepository,
    ProviderMachineCapabilityProfileRepository, ProviderMachineSettingRepository, VoiceProfileRepository)
from app.domain.models import Job, JobStatus, JobType, UsageCost, CostCategory, VoiceGenerationJobPayload
from app.production_runs import ProductionRunConflict, ProductionRunNotReady, ProductionRunService, _snapshot_sha256
from app.repair_allowance import used_repairs
from app.voice_qa import voice_human_review_status


class VoiceRepairRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=500)
    confirmed_action: Literal["regenerate_take"]
    reason: str = Field(min_length=1, max_length=2000, pattern=r"\S")


class VoiceRepairPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: UUID
    run_id: UUID
    fingerprint: str
    action: Literal["regenerate_take", "replan", "stop"]
    failure_kind: Literal["technical", "copy_qa", "subjective", "unassessed", "invalid_request"]
    stop_reasons: tuple[str, ...]
    evidence: dict[str, JsonValue]
    remaining_run_repairs: int
    max_tts_calls: Literal[1] = 1
    max_qa_asr_calls: Literal[1] = 1
    external_charge_ceiling: Literal["0"] = "0"
    currency: Literal["USD"] = "USD"
    local_compute_cost: None = None
    user_active_minutes: None = None
    execution_granularity: Literal["whole_take"] = "whole_take"
    review_scope: Literal["fresh_full_copy_qa_and_exact_u_voice"] = "fresh_full_copy_qa_and_exact_u_voice"
    worker_readiness: Literal["not_live_observed"] = "not_live_observed"


def execution_snapshot(db: Database, payload: VoiceGenerationJobPayload) -> dict:
    profile = VoiceProfileRepository(db).get(payload.voice_profile_id)
    capability = ProviderMachineCapabilityProfileRepository(db).get(payload.execution_capability_profile_id) if payload.execution_capability_profile_id else None
    references = []
    for clip_id in (() if profile is None else profile.reference_clip_ids):
        clip = ClipRepository(db).get(clip_id)
        asset = None if clip is None else AssetRepository(db).get(clip.asset_id)
        references.append({"clip": None if clip is None else clip.model_dump(mode="json"),
                           "asset": None if asset is None else asset.model_dump(mode="json")})
    settings = sorted((item.model_dump(mode="json") for item in ProviderMachineSettingRepository(db).list()
                       if capability is not None and item.provider == capability.provider), key=lambda item: item["id"])
    return {"job_payload": payload.model_dump(mode="json"), "profile": None if profile is None else profile.model_dump(mode="json"),
            "capability": None if capability is None else capability.model_dump(mode="json"),
            "references": references, "settings_sha256": _snapshot_sha256(settings)}


class VoiceRepairService:
    def __init__(self, db: Database, data_root: str | Path):
        self.db = db
        self.production = ProductionRunService(db, data_root)

    def _row(self, project: UUID, run: UUID):
        row = self.db.connection.execute("SELECT * FROM production_runs WHERE id = ? AND project_id = ?", (str(run), str(project))).fetchone()
        if row is None:
            raise LookupError("production run not found")
        return row

    def _qa_capability(self):
        choices = [item for item in ProviderMachineCapabilityProfileRepository(self.db).list()
                   if item.capability == "asr" and item.mode == "local" and item.readiness == "verified"
                   and item.quality_status == "verified" and item.evidence_reference
                   and item.commercial_status == "commercial_safe" and item.license_evidence_reference]
        # No sort-order recommendation or hidden fallback.
        return choices[0] if len(choices) == 1 else None

    def _budgets(self, project):
        repo = BudgetPolicyRepository(self.db)
        return [None if (p := repo.get_by_project(scope)) is None else p.model_dump(mode="json") for scope in (None, project)]

    def require_execution(self, project: UUID, run: UUID, job: Job, expected: dict):
        row = self._row(project, run)
        if row["stage"] != "voice" or row["voice_job_id"] != str(job.id):
            raise ProductionRunNotReady(("voice_repair_job_no_longer_current",))
        payload = job.payload
        draft = ProjectDraftRepository(self.db).get(project)
        copy = None if draft is None else draft.script or "\n\n".join(s.voice_text for s in sorted(draft.scenes, key=lambda s: s.order))
        if (draft is None or draft.version != row["draft_version"] or draft.script_revision != row["script_revision"]
            or copy != payload.text or draft.narration_performance_plan != payload.narration_performance_plan):
            raise ProductionRunNotReady(("voice_repair_copy_or_intent_changed",))
        current = execution_snapshot(self.db, payload)
        if current != expected:
            raise ProductionRunNotReady(("voice_repair_execution_snapshot_changed",))
        profile, capability = current["profile"], current["capability"]
        if (not profile or not profile["consent"]["confirmed"] or not capability
            or capability["mode"] != "local" or capability["capability"] != "voice"
            or capability["readiness"] != "verified" or capability["quality_status"] != "verified"
            or not capability["evidence_reference"] or capability["commercial_status"] != "commercial_safe"
            or not capability["license_evidence_reference"] or profile["provider"] != payload.expected_provider
            or capability["provider"] != payload.expected_provider or capability["model"] != payload.expected_model):
            raise ProductionRunNotReady(("voice_repair_local_admission_required",))
        for reference in current["references"]:
            if reference["clip"] is None or reference["asset"] is None or not reference["asset"]["authorization_reference"]:
                raise ProductionRunNotReady(("voice_repair_reference_unavailable",))
            self.production._require_source_bytes(reference["asset"]["source_file"], reference["asset"]["content_hash"])

    def plan(self, project: UUID, run: UUID) -> VoiceRepairPlan:
        row = self._row(project, run)
        job = None if row["voice_job_id"] is None else JobRepository(self.db).get(UUID(row["voice_job_id"]))
        reasons, kind, action = [], "unassessed", "stop"
        audio = None if row["voice_audio_id"] is None else AudioAssetRepository(self.db).get(UUID(row["voice_audio_id"]))
        qa = None if row["voice_qa_job_id"] is None else JobRepository(self.db).get(UUID(row["voice_qa_job_id"]))
        snapshot_row = None if job is None else self.db.connection.execute("SELECT payload FROM voice_dispatch_snapshots WHERE job_id = ?", (str(job.id),)).fetchone()
        expected = None if snapshot_row is None else json.loads(snapshot_row["payload"])
        receipt_row = None if job is None else self.db.connection.execute("SELECT payload FROM voice_execution_receipts WHERE job_id = ?", (str(job.id),)).fetchone()
        receipt = None if receipt_row is None else json.loads(receipt_row["payload"])
        if (expected is None or receipt is None or not expected.get("capability")
            or receipt.get("runtime") != expected["capability"]["runtime"]
            or receipt.get("machine_id") != expected["capability"]["machine_id"]
            or receipt.get("ambiguous") or not isinstance(receipt.get("parameters"), dict)):
            reasons.append("voice_repair_original_worker_execution_unavailable")
        remaining = max(0, row["repair_allowance"] - used_repairs(self.db, run))
        if row["stage"] != "voice" or row["render_job_id"] or row["talking_master_audio_id"] or json.loads(row["talking_source_bindings"]):
            reasons.append("voice_repair_requires_unconsumed_voice_stage")
        if remaining == 0:
            reasons.append("run_repair_allowance_exhausted")
        if self.db.connection.execute("SELECT id FROM voice_repairs WHERE run_id = ?", (str(run),)).fetchone():
            reasons.append("voice_take_repair_already_attempted")
        if job is None or not isinstance(job.payload, VoiceGenerationJobPayload) or job.project_id != project:
            reasons.append("voice_repair_generation_mismatch")
        elif job.status == JobStatus.FAILED:
            kind = "technical"
            if job.error_code == "voice_temporarily_unavailable":
                action = "regenerate_take"
            else:
                kind = "invalid_request" if job.error_code == "voice_invalid_request" else "unassessed"
                reasons.append("voice_repair_failure_not_verified_transient")
        elif job.status == JobStatus.COMPLETED and audio is not None and qa is not None:
            generation = audio.metadata.get("voice_generation", {})
            if not isinstance(generation, dict):
                generation = {}
            if (generation.get("job_id") != str(job.id) or generation.get("project_id") != str(project)
                or generation.get("target_text") != job.payload.text or generation.get("provider") != job.payload.expected_provider
                or generation.get("model") != job.payload.expected_model or audio.authorization_reference != job.payload.authorization_reference):
                reasons.append("voice_repair_output_provenance_mismatch")
            if voice_human_review_status(audio) == "approved":
                reasons.append("voice_repair_approved_master_not_replaceable")
            elif generation.get("human_review") is not None or voice_human_review_status(audio) == "rejected":
                kind, action = "subjective", "replan"
                reasons.append("voice_repair_human_judgment_requires_replan")
            elif (qa.status == JobStatus.FAILED and qa.error_code == "voice_qa_failed"
                  and qa.type == JobType.VERIFY_VOICE and qa.project_id == project
                  and qa.payload.narration_audio_id == audio.id and qa.payload.target_text == job.payload.text
                  and generation.get("job_id") == str(job.id) and generation.get("project_id") == str(project)
                  and generation.get("target_text") == job.payload.text and generation.get("qa_state") == "failed"):
                report = generation.get("qa", {})
                if not isinstance(report, dict):
                    report = {}
                checks = set(report.get("checks", [])) if isinstance(report, dict) else set()
                copy_checks = {"copy_missing_tokens", "copy_duplicate_tokens", "copy_substitution_tokens"}
                if (report.get("qa_state") == "failed" and report.get("playable") is True
                    and report.get("transcript_segment_count", 0) == len(audio.transcript_segments) > 0
                    and audio.transcript_source == f"{report.get('provider')}:{report.get('model')}"
                    and all(0 <= s.start_ms < s.end_ms <= audio.duration_ms for s in audio.transcript_segments)
                    and checks & copy_checks and checks <= copy_checks | {"copy_substitutions_within_tolerance", "leading_silence_or_unrecognized_audio", "long_silence"}):
                    kind, action = "copy_qa", "regenerate_take"
                else:
                    reasons.append("voice_repair_qa_requires_diagnosis_not_trimming")
            else:
                reasons.append("voice_repair_complete_independent_qa_required")
            try:
                self.production._require_master_bytes(audio.id)
            except ProductionRunNotReady as exc:
                reasons.extend(exc.reasons)
            if generation.get("composition") is not None or any(key in audio.metadata for key in ("voice_composition", "master_narration", "voice_pace_candidate")):
                reasons.append("voice_repair_requires_single_original_take")
        else:
            reasons.append("voice_repair_terminal_dependency_required")
        if job is not None and expected is not None:
            try:
                self.require_execution(project, run, job, expected)
            except ProductionRunNotReady as exc:
                reasons.extend(exc.reasons)
        else:
            reasons.append("voice_repair_original_snapshot_unavailable")
        qa_cap = self._qa_capability()
        if qa_cap is None:
            reasons.append("voice_repair_local_qa_capability_missing_or_ambiguous")
        if kind == "unassessed" and not reasons:
            reasons.append("voice_repair_persisted_failure_required")
        calls = ProviderCallRepository(self.db)
        for scope in (None, project):
            policy = BudgetPolicyRepository(self.db).get_by_project(scope)
            if policy:
                records = calls.list_all() if scope is None else calls.list_for_project(project)
                try:
                    current = enforce_reservation(policy, records, UsageCost(category=CostCategory.VOICE, amount=Decimal("0"), currency="USD"), ignore_existing_unknown_cost=True)
                    if policy.max_calls is not None and current.calls + 2 > policy.max_calls:
                        reasons.append("voice_repair_tts_and_asr_budget_required")
                except BudgetLimitError as exc:
                    reasons.append(f"voice_repair_budget_{exc.code}")
        if reasons and action == "regenerate_take":
            action = "stop"
        evidence = {"job": None if job is None else job.model_dump(mode="json"),
                    "audio": None if audio is None else audio.model_dump(mode="json"),
                    "qa_job": None if qa is None else qa.model_dump(mode="json"),
                    "execution": expected, "worker_execution": receipt, "qa_capability": None if qa_cap is None else qa_cap.model_dump(mode="json"),
                    "budgets": self._budgets(project), "draft_version": row["draft_version"], "script_revision": row["script_revision"]}
        values = dict(project_id=project, run_id=run, action=action, failure_kind=kind,
                      stop_reasons=tuple(dict.fromkeys(reasons)), evidence=evidence, remaining_run_repairs=remaining)
        draft_plan = VoiceRepairPlan(fingerprint="0" * 64, **values)
        return draft_plan.model_copy(update={"fingerprint": _snapshot_sha256(draft_plan.model_dump(mode="json", exclude={"fingerprint"}))})

    def records(self, project: UUID, run: UUID) -> list[dict]:
        self._row(project, run)
        rows = self.db.connection.execute("SELECT * FROM voice_repairs WHERE project_id = ? AND run_id = ? ORDER BY rowid", (str(project), str(run))).fetchall()
        calls = ProviderCallRepository(self.db).list_for_project(project)
        result = []
        for row in rows:
            record = json.loads(row["payload"])
            current = JobRepository(self.db).get(UUID(row["replacement_job_id"]))
            outputs = [a.model_dump(mode="json") for a in AudioAssetRepository(self.db).list() if isinstance(a.metadata.get("voice_generation"), dict) and a.metadata["voice_generation"].get("job_id") == row["replacement_job_id"]]
            record.update(replacement_job_status=None if current is None else current.status.value,
                          successor_audio=outputs, successor_qa_job_id=row["qa_job_id"],
                          provider_calls=[c.model_dump(mode="json") for c in calls if c.idempotency_key.startswith(f"job:{row['replacement_job_id']}:tts:") or row["qa_job_id"] and c.idempotency_key.startswith(f"job:{row['qa_job_id']}:asr:")],
                          predecessor_provider_calls=[c.model_dump(mode="json") for c in calls if any(c.idempotency_key.startswith(f"job:{j}:") for j in record["old_job_ids"] if j)])
            result.append(record)
        return result

    def execute(self, project: UUID, run: UUID, request: VoiceRepairRequest) -> dict:
        with self.db.transaction(immediate=True):
            digest = _snapshot_sha256(request.model_dump(mode="json"))
            prior = self.db.connection.execute("SELECT * FROM voice_repairs WHERE project_id = ? AND idempotency_key = ?", (str(project), request.idempotency_key)).fetchone()
            if prior:
                if prior["run_id"] != str(run) or prior["request_fingerprint"] != digest:
                    raise ProductionRunConflict("voice_repair_idempotency_conflict")
                return json.loads(prior["payload"])
            plan = self.plan(project, run)
            if plan.fingerprint != request.expected_fingerprint:
                raise ProductionRunConflict("voice_repair_plan_stale")
            if plan.action != "regenerate_take":
                raise ProductionRunNotReady(plan.stop_reasons)
            row = self._row(project, run)
            original = JobRepository(self.db).get(UUID(row["voice_job_id"]))
            now, repair_id = datetime.now(timezone.utc), uuid4()
            job = Job(project_id=project, type=JobType.GENERATE_VOICE, payload=original.payload,
                      idempotency_key=f"voice-repair:{repair_id}", created_at=now, updated_at=now)
            JobRepository(self.db).create(job)
            record = {"id": str(repair_id), "project_id": str(project), "run_id": str(run),
                      "idempotency_key": request.idempotency_key, "reason": request.reason, "plan": plan.model_dump(mode="json"),
                      "old_job_ids": [row["voice_job_id"], row["voice_qa_job_id"]], "old_audio_id": row["voice_audio_id"],
                      "replacement_job_id": str(job.id), "created_at": now.isoformat()}
            self.db.connection.execute("INSERT INTO voice_repairs(id,project_id,run_id,idempotency_key,request_fingerprint,replacement_job_id,payload) VALUES (?,?,?,?,?,?,?)",
                (str(repair_id), str(project), str(run), request.idempotency_key, digest, str(job.id), json.dumps(record)))
            self.db.connection.execute("INSERT INTO voice_dispatch_snapshots(job_id,payload) VALUES (?,?)", (str(job.id), json.dumps(plan.evidence["execution"])))
            self.db.connection.execute("UPDATE production_runs SET voice_job_id = ?, voice_audio_id = NULL, voice_qa_job_id = NULL, voice_qa_auto_stop_reasons = '[]' WHERE id = ? AND stage = 'voice'", (str(job.id), str(run)))
            return record


def require_voice_repair_call(db: Database, job: Job, *, operation: str, known_local_cost: bool, provider: str, model: str, data_root: Path, worker_execution: dict | None):
    row = db.connection.execute("SELECT * FROM voice_repairs WHERE replacement_job_id = ? OR qa_job_id = ?", (str(job.id), str(job.id))).fetchone()
    if row is None:
        return
    record = json.loads(row["payload"])
    service = VoiceRepairService(db, data_root)
    current = service._row(UUID(row["project_id"]), UUID(row["run_id"]))
    original = JobRepository(db).get(UUID(row["replacement_job_id"]))
    service.require_execution(UUID(row["project_id"]), UUID(row["run_id"]), original, record["plan"]["evidence"]["execution"])
    expected = record["plan"]["evidence"]["execution"]["capability"] if operation == "tts" else record["plan"]["evidence"]["qa_capability"]
    if (worker_execution is None or worker_execution.get("runtime") != expected["runtime"]
        or worker_execution.get("machine_id") != expected["machine_id"]
        or operation == "tts" and worker_execution != record["plan"]["evidence"]["worker_execution"]):
        raise ProductionRunNotReady(("voice_repair_worker_execution_changed_or_unknown",))
    if operation not in {"tts", "asr"} or not known_local_cost or job.project_id != UUID(row["project_id"]) or provider != expected["provider"] or model != expected["model"]:
        raise ProductionRunNotReady(("voice_repair_external_or_changed_execution_not_authorized",))
    if operation == "asr":
        cap = ProviderMachineCapabilityProfileRepository(db).get(UUID(expected["id"]))
        if (cap is None or cap.model_dump(mode="json") != expected or current["voice_qa_job_id"] != str(job.id)
            or worker_execution.get("parameters") != expected["verified_parameters"]):
            raise ProductionRunNotReady(("voice_repair_qa_admission_changed",))
        audio = AudioAssetRepository(db).get(job.payload.narration_audio_id)
        if (audio is None or current["voice_audio_id"] != str(audio.id)
            or job.payload.target_text != original.payload.text
            or not isinstance(audio.metadata.get("voice_generation"), dict)
            or audio.metadata["voice_generation"].get("job_id") != str(original.id)):
            raise ProductionRunNotReady(("voice_repair_qa_candidate_changed",))
        service.production._require_master_bytes(audio.id)
    prefix = f"job:{job.id}:{operation}:"
    if any(c.idempotency_key.startswith(prefix) for c in ProviderCallRepository(db).list_for_project(job.project_id)):
        raise ProductionRunNotReady(("voice_repair_call_limit",))
