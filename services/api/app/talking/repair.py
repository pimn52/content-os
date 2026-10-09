"""Evidence-bound, one-call local replacement of a planned Talking scene."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue

from app.db import (
    AssetRepository, AudioAssetRepository, BudgetPolicyRepository, Database, JobRepository,
    ProviderCallRepository, TalkingSliceSeriesContinuityReviewRepository,
    TalkingSliceSeriesRepository,
)
from app.domain.models import Job, JobStatus, JobType, TalkingGenerationJobPayload, TalkingReviewFinding
from app.production_runs import (
    ProductionRunConflict, ProductionRunNotReady, ProductionRunService,
    TalkingSourceBinding, _snapshot_sha256,
)
from app.talking.review_policy import TalkingReviewPolicyService


class TalkingRepairRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene_plan_id: UUID
    expected_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=500)
    confirmed_action: Literal["regenerate_scene"]
    reason: str = Field(min_length=1, max_length=2000, pattern=r"\S")


class TalkingRepairPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: UUID
    run_id: UUID
    scene_plan_id: UUID
    fingerprint: str
    failure_kind: Literal["technical", "invalid_request", "quality", "unassessed"]
    action: Literal["regenerate_scene", "replan", "stop"]
    stop_reasons: tuple[str, ...]
    evidence: list[dict[str, JsonValue]]
    findings: list[TalkingReviewFinding]
    master_audio_id: UUID
    master_start_ms: int
    master_end_ms: int
    remaining_run_repairs: int
    max_provider_calls: Literal[1] = 1
    external_charge_ceiling: Literal["0"] = "0"
    currency: Literal["USD"] = "USD"
    local_compute_cost: None = None
    user_active_minutes: None = None
    review_scope: Literal["fresh_child_qa_and_policy_required_whole_result_review"] = "fresh_child_qa_and_policy_required_whole_result_review"


class TalkingRepairRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    project_id: UUID
    run_id: UUID
    scene_plan_id: UUID
    idempotency_key: str
    reason: str
    plan: TalkingRepairPlan
    old_binding: TalkingSourceBinding
    replacement_job_id: UUID
    predecessor_series_id: UUID | None
    successor_series_id: UUID | None = None
    reused_master_audio_id: UUID
    reused_scene_plan_ids: tuple[UUID, ...]
    budget_snapshot: list[dict[str, JsonValue] | None]
    created_at: AwareDatetime


def operation_key(db: Database, run_id: UUID, scene_id: UUID, job_id: UUID, operation: str) -> str:
    """Keep legacy keys unchanged; new QA/collection/preview identities name the repair."""
    row = db.connection.execute(
        "SELECT id FROM talking_repairs WHERE run_id = ? AND scene_plan_id = ? AND replacement_job_id = ?",
        (str(run_id), str(scene_id), str(job_id)),
    ).fetchone()
    base = f"production-run:{run_id}:{operation}:{scene_id}"
    return base if row is None else f"{base}:repair:{row['id']}"


def require_repair_call_budget(db: Database, job: Job, *, known_local_cost: bool) -> None:
    """Called inside the ledger's write transaction, before reservation/inference."""
    row = db.connection.execute(
        "SELECT payload FROM talking_repairs WHERE replacement_job_id = ?", (str(job.id),),
    ).fetchone()
    if row is None:
        return
    record = TalkingRepairRecord.model_validate_json(row["payload"])
    current = db.connection.execute(
        "SELECT stage, talking_source_bindings FROM production_runs WHERE id = ? AND project_id = ?",
        (str(record.run_id), str(record.project_id)),
    ).fetchone()
    if (current is None or current["stage"] != "awaiting_talking" or not any(
        binding.get("scene_plan_id") == str(record.scene_plan_id) and binding.get("talking_job_id") == str(job.id)
        for binding in json.loads(current["talking_source_bindings"])
    )):
        raise ProductionRunNotReady(("talking_repair_job_no_longer_current",))
    if job.project_id != record.project_id or not known_local_cost:
        raise ProductionRunNotReady(("talking_repair_external_charge_not_authorized",))
    prefix = f"job:{job.id}:talking:"
    if any(call.idempotency_key.startswith(prefix) for call in ProviderCallRepository(db).list_for_project(record.project_id)):
        raise ProductionRunNotReady(("talking_repair_call_limit",))


class TalkingRepairService:
    def __init__(self, db: Database, data_root: str | Path):
        self.db = db
        self.production = ProductionRunService(db, data_root)

    def records(self, project_id: UUID, run_id: UUID) -> list[TalkingRepairRecord]:
        rows = self.db.connection.execute(
            "SELECT payload, successor_series_id FROM talking_repairs WHERE project_id = ? AND run_id = ? ORDER BY rowid",
            (str(project_id), str(run_id)),
        ).fetchall()
        return [TalkingRepairRecord.model_validate_json(row["payload"]).model_copy(update={
            "successor_series_id": None if row["successor_series_id"] is None else UUID(row["successor_series_id"]),
        }) for row in rows]

    def _budgets(self, project_id: UUID) -> list[dict[str, JsonValue] | None]:
        repo = BudgetPolicyRepository(self.db)
        return [None if (policy := repo.get_by_project(scope)) is None else policy.model_dump(mode="json")
                for scope in (None, project_id)]

    def plan(self, project_id: UUID, run_id: UUID, scene_id: UUID) -> TalkingRepairPlan:
        run = self.production.get(project_id, run_id)
        if run is None:
            raise LookupError("production run not found")
        binding = next((item for item in run.talking_source_bindings if item.scene_plan_id == scene_id), None)
        if binding is None or binding.talking_job_id is None:
            raise ProductionRunNotReady(("talking_repair_generation_missing",))
        job = JobRepository(self.db).get(binding.talking_job_id)
        if (job is None or job.project_id != project_id or not isinstance(job.payload, TalkingGenerationJobPayload)
            or job.payload.planned_context is None or job.payload.planned_context.run_id != run_id
            or job.payload.planned_context.scene_plan_id != scene_id):
            raise ProductionRunNotReady(("talking_repair_generation_mismatch",))
        reasons: list[str] = []
        if run.status != "awaiting_talking_dependencies" or binding.talking_run_id is not None:
            reasons.append("talking_repair_requires_unadmitted_waiting_scene")
        self.production._require_consumed_talking_source(project_id, run_id, binding, job)
        self.production._require_master_bytes(binding.master_audio_id)
        source = AssetRepository(self.db).get(binding.reference_asset_id)
        if source is None:
            raise ProductionRunNotReady(("talking_repair_source_missing",))
        self.production._require_source_bytes(source.source_file, binding.reference_asset_hash)
        row = self.db.connection.execute("SELECT repair_allowance FROM production_runs WHERE id = ?", (str(run_id),)).fetchone()
        prior = self.records(project_id, run_id)
        from app.repair_allowance import used_repairs
        remaining = max(0, row["repair_allowance"] - used_repairs(self.db, run_id))
        if remaining == 0:
            reasons.append("talking_repair_allowance_exhausted")
        if any(item.scene_plan_id == scene_id for item in prior):
            reasons.append("talking_scene_repair_already_attempted")
        evidence: list[dict[str, JsonValue]] = [{"type": "generation_job", "record": job.model_dump(mode="json")}]
        findings: list[TalkingReviewFinding] = []
        kind, action = "unassessed", "stop"
        if job.status is JobStatus.FAILED:
            kind = "technical"
            if job.error_code == "talking_temporarily_unavailable":
                action = "regenerate_scene"
            elif job.error_code == "talking_invalid_request":
                kind = "invalid_request"
                reasons.append("talking_invalid_request_requires_correction")
            else:
                reasons.append("talking_failure_not_verified_transient")
        elif job.status is JobStatus.COMPLETED and binding.talking_series_id is not None:
            from app.talking.planned_admission import PlannedTalkingAdmissionService
            series = TalkingSliceSeriesRepository(self.db).get(binding.talking_series_id)
            if series is None or series.project_id != project_id:
                raise ProductionRunNotReady(("talking_repair_collection_missing",))
            preview, qa_hash = PlannedTalkingAdmissionService(self.db, self.production.data_root).require_current_candidate(series)
            review = TalkingSliceSeriesContinuityReviewRepository(self.db).get_by_series_id(series.id)
            if review is not None:
                if (review.preview_asset_id != preview.id or review.preview_sha256 != preview.content_hash
                    or review.preview_qa_sha256 != qa_hash
                    or review.planned_origin_sha256 != _snapshot_sha256(series.planned_origin.model_dump(mode="json"))):
                    raise ProductionRunNotReady(("talking_repair_review_evidence_stale",))
                evidence.append({"type": "preview_review", "record": review.model_dump(mode="json")})
                if not review.approved:
                    findings.extend(review.scoped_findings)
                    kind = "quality"
                    if review.dimensions is not None and any(
                        outcome == "fail" and dimension not in {"visible_sync", "artifacts"}
                        for dimension, outcome in review.dimensions.model_dump(exclude={"schema_version"}).items()
                    ):
                        reasons.append("talking_failed_dimension_requires_replan")
            policy = TalkingReviewPolicyService(self.db)
            for concern in policy.concerns(project_id, preview.content_hash):
                answer = policy.answer(concern.id)
                evidence.append({"type": "concern", "record": concern.model_dump(mode="json"),
                                 "answer": None if answer is None else answer.model_dump(mode="json")})
                if answer is not None and not answer.approved:
                    findings.append(concern.finding)
                    kind = "quality"
                elif answer is None:
                    reasons.append("talking_repair_unanswered_concern")
            evidence.append({"type": "preview", "asset_id": str(preview.id), "sha256": preview.content_hash})
            if kind == "quality":
                if (findings and all(item.dimension in {"visible_sync", "artifacts"} for item in findings)
                    and "talking_failed_dimension_requires_replan" not in reasons):
                    action = "regenerate_scene"
                else:
                    action = "replan"
                    reasons.append("talking_quality_requires_source_presentation_or_intent_replan")
        if kind == "unassessed":
            reasons.append("talking_repair_requires_persisted_failure_evidence")
        if action == "regenerate_scene" and not reasons:
            context = job.payload.planned_context
            try:
                self.production.require_talking_job_admission(
                    job, provider=context.expected_provider, model=context.expected_model,
                    runtime=context.expected_runtime, machine_id=context.expected_machine_id,
                )
            except ProductionRunNotReady as exc:
                reasons.extend(exc.reasons)
        if reasons and action == "regenerate_scene":
            action = "stop"
        master = AudioAssetRepository(self.db).get(binding.master_audio_id)
        values = dict(project_id=project_id, run_id=run_id, scene_plan_id=scene_id,
                      failure_kind=kind, action=action, stop_reasons=tuple(reasons), evidence=evidence,
                      findings=findings, master_audio_id=binding.master_audio_id,
                      master_start_ms=binding.master_start_ms, master_end_ms=binding.master_end_ms,
                      remaining_run_repairs=remaining)
        draft = TalkingRepairPlan(fingerprint="0" * 64, **values)
        fingerprint = _snapshot_sha256({"plan": draft.model_dump(mode="json", exclude={"fingerprint"}),
                                       "binding": binding.model_dump(mode="json"),
                                       "master": master.model_dump(mode="json"), "budgets": self._budgets(project_id)})
        return TalkingRepairPlan(fingerprint=fingerprint, **values)

    def execute(self, project_id: UUID, run_id: UUID, request: TalkingRepairRequest) -> TalkingRepairRecord:
        with self.db.transaction(immediate=True):
            fingerprint = _snapshot_sha256(request.model_dump(mode="json"))
            existing = self.db.connection.execute(
                "SELECT request_fingerprint FROM talking_repairs WHERE project_id = ? AND idempotency_key = ?",
                (str(project_id), request.idempotency_key),
            ).fetchone()
            if existing is not None:
                record = next((item for item in self.records(project_id, run_id) if item.idempotency_key == request.idempotency_key), None)
                if record is None or existing["request_fingerprint"] != fingerprint:
                    raise ProductionRunConflict("talking_repair_idempotency_conflict")
                return record
            plan = self.plan(project_id, run_id, request.scene_plan_id)
            if plan.fingerprint != request.expected_fingerprint:
                raise ProductionRunConflict("talking_repair_plan_stale")
            if plan.action != "regenerate_scene":
                raise ProductionRunNotReady(plan.stop_reasons)
            run = self.production.get(project_id, run_id)
            binding = next(item for item in run.talking_source_bindings if item.scene_plan_id == request.scene_plan_id)
            old_job = JobRepository(self.db).get(binding.talking_job_id)
            context = old_job.payload.planned_context
            self.production.require_talking_job_admission(
                old_job, provider=context.expected_provider, model=context.expected_model,
                runtime=context.expected_runtime, machine_id=context.expected_machine_id,
            )
            now, repair_id = datetime.now(timezone.utc), uuid4()
            job = Job(project_id=project_id, type=JobType.GENERATE_TALKING,
                      idempotency_key=f"talking-repair:{repair_id}", payload=old_job.payload, created_at=now, updated_at=now)
            persisted = JobRepository(self.db).create(job)
            if persisted.id != job.id:
                raise ProductionRunConflict("talking_repair_job_conflict")
            record = TalkingRepairRecord(
                id=repair_id, project_id=project_id, run_id=run_id, scene_plan_id=request.scene_plan_id,
                idempotency_key=request.idempotency_key, reason=request.reason, plan=plan, old_binding=binding,
                replacement_job_id=job.id, predecessor_series_id=binding.talking_series_id,
                reused_master_audio_id=binding.master_audio_id,
                reused_scene_plan_ids=tuple(item.scene_plan_id for item in run.talking_source_bindings if item.scene_plan_id != binding.scene_plan_id),
                budget_snapshot=self._budgets(project_id), created_at=now,
            )
            self.db.connection.execute(
                "INSERT INTO talking_repairs(id, project_id, run_id, scene_plan_id, idempotency_key, request_fingerprint, replacement_job_id, predecessor_series_id, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(record.id), str(project_id), str(run_id), str(request.scene_plan_id), request.idempotency_key,
                 fingerprint, str(job.id), None if binding.talking_series_id is None else str(binding.talking_series_id), record.model_dump_json()),
            )
            replacement = binding.model_copy(update={"talking_job_id": job.id, "talking_output_asset_id": None,
                "talking_qa_job_id": None, "talking_series_id": None, "talking_preview_job_id": None, "talking_run_id": None})
            bindings = [replacement if item.scene_plan_id == binding.scene_plan_id else item for item in run.talking_source_bindings]
            self.db.connection.execute(
                "UPDATE production_runs SET talking_source_bindings = ? WHERE id = ? AND stage = 'awaiting_talking'",
                (json.dumps([item.model_dump(mode="json") for item in bindings]), str(run_id)),
            )
            return record
