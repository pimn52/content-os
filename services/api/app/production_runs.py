"""Narrow durable production lanes for typography renders and planned dependencies.

It links existing Voice/QA/Render Jobs without performing provider calls or
claiming that technical QA substitutes for human product review.
"""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.assembly import VideoSpecAssembler, VideoSpecAssemblyError
from app.db import (
    AssetRepository, AudioAssetRepository, ClipRepository, Database, ImageAssetRepository,
    JobRepository, ProjectDraftRepository, ProjectRepository, ProviderMachineCapabilityProfileRepository,
    TalkingProfileRepository, TalkingSliceSeriesContinuityReviewRepository, TalkingSliceSeriesRepository, VoiceProfileRepository,
)
from app.domain.models import Job, JobStatus, JobType, PlannedTalkingContext, PlannedTalkingRunOrigin, RenderVideoJobPayload, SourceKind, TalkingGenerationJobPayload, TalkingPerformanceBrief, TalkingQaJobPayload, TalkingRunPreviewJobPayload, TalkingSliceSeries, VisualStyleTokens, VoiceGenerationJobPayload, VoiceQaJobPayload
from app.production_preflight import PreflightInputError, ProductionPreflight, build_production_preflight
from app.talking import TalkingSlicePlanningError, plan_talking_audio_slice, select_talking_reference
from app.talking.source_admission import TalkingSourceAdmissionRepository
from app.talking.source_suitability import TalkingSourceSuitabilityRepository
from app.voice_qa import comparison_tokens, voice_human_review_status
from app.domain.models import PlannedTalkingRunOriginV2
from app.talking.review_policy import TalkingReviewPolicyError, TalkingReviewPolicyService, require_child_judgment


class ProductionRunError(ValueError):
    pass


class ProductionRunNotReady(ProductionRunError):
    def __init__(self, reasons: tuple[str, ...]):
        self.reasons = reasons
        super().__init__("production preflight is not executable: " + ", ".join(reasons))


class ProductionRunConflict(ProductionRunError):
    pass


def is_planned_talking_collection(db: Database, series) -> bool:
    """Detect planned provenance even if a collection marker was stripped."""
    if series.planned_origin is not None:
        return True
    jobs = JobRepository(db)
    if any((job := jobs.get(job_id)) is not None
           and isinstance(job.payload, TalkingGenerationJobPayload)
           and job.payload.planned_context is not None for job_id in series.child_job_ids):
        return True
    rows = db.connection.execute("SELECT talking_source_bindings FROM production_runs").fetchall()
    child_ids = set(series.child_job_ids)
    for row in rows:
        for raw in json.loads(row["talking_source_bindings"]):
            binding = TalkingSourceBinding.model_validate(raw)
            if binding.talking_series_id == series.id or binding.talking_job_id in child_ids:
                return True
    return False


class TalkingPlanDependency(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene_plan_id: UUID
    scene_id: str
    visual_dependency_fingerprint: str = Field(min_length=64, max_length=64)


class TalkingSourceBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene_plan_id: UUID
    talking_profile_id: UUID
    reference_clip_id: UUID
    reference_asset_id: UUID
    reference_asset_hash: str
    reference_start_ms: int
    reference_end_ms: int
    source_authorization_reference: str
    reference_assessment_reference: str
    performance_brief: TalkingPerformanceBrief
    capability_profile_id: UUID
    capability_evidence_reference: str
    license_evidence_reference: str
    master_audio_id: UUID
    master_start_ms: int
    master_end_ms: int
    authorization_reference: str
    suitability: Literal["full_interval_unknown", "review_claimed_suitable", "unusable"] = "full_interval_unknown"
    suitability_assessment_id: UUID | None = None
    talking_job_id: UUID | None = None
    talking_output_asset_id: UUID | None = None
    talking_qa_job_id: UUID | None = None
    talking_series_id: UUID | None = None
    talking_preview_job_id: UUID | None = None
    talking_run_id: UUID | None = None


def _snapshot_sha256(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _talking_binding_sha256(binding: TalkingSourceBinding) -> str:
    return _snapshot_sha256(binding.model_dump(mode="json", exclude={"talking_job_id", "talking_output_asset_id", "talking_qa_job_id", "talking_series_id", "talking_preview_job_id", "talking_run_id"}))


class TalkingQaDependency(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene_plan_id: UUID
    generation_job_id: UUID
    output_asset_id: UUID | None
    qa_job_id: UUID | None
    state: Literal["generation_pending", "generation_running", "generation_failed", "output_awaiting_qa", "qa_pending", "qa_running", "qa_failed", "qa_verified_awaiting_u_talking", "qa_verified_ready_for_preview"]


class TalkingPreviewDependency(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene_plan_id: UUID
    series_id: UUID
    job_id: UUID
    output_asset_id: UUID | None
    state: Literal["pending", "running", "failed", "completed_review_only", "output_missing", "cancelled", "admitted"]
    child_review_state: Literal["pending", "approved", "rejected"]
    continuity_review_state: Literal["not_submitted", "approved", "rejected"]


class ProductionRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    project_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=500)
    preflight_fingerprint: str = Field(min_length=64, max_length=64)
    render_job_id: UUID | None
    voice_job_id: UUID | None = None
    voice_audio_id: UUID | None = None
    voice_qa_job_id: UUID | None = None
    voice_qa_auto_stop_reasons: tuple[str, ...] = ()
    talking_master_audio_id: UUID | None = None
    talking_dependencies: tuple[TalkingPlanDependency, ...] = ()
    talking_source_bindings: tuple[TalkingSourceBinding, ...] = ()
    talking_qa_dependencies: tuple[TalkingQaDependency, ...] = ()
    talking_preview_dependencies: tuple[TalkingPreviewDependency, ...] = ()
    waiting_stop_reasons: tuple[str, ...] = ()
    created_at: AwareDatetime
    status: Literal[
        "awaiting_approved_master", "awaiting_talking_dependencies", "voice_pending", "voice_running", "voice_failed", "voice_completed_awaiting_qa",
        "voice_qa_pending", "voice_qa_running", "voice_qa_failed", "awaiting_u_voice_review",
        "u_voice_rejected", "approved_master_ready_to_resume",
        "render_pending", "render_running",
        "render_failed", "render_completed_awaiting_review", "cancelled",
    ]
    talking_review_policy_version: Literal[1, 2] = 1


class TalkingSourceContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: UUID
    run_id: UUID
    scene_plan_id: UUID
    scene_id: str
    scene_voice_text: str
    preflight_fingerprint: str
    visual_dependency_fingerprint: str
    master_audio_id: UUID
    master_content_hash: str
    status: Literal["ready", "unresolved"]
    reason: str | None
    speech_start_ms: int | None
    speech_end_ms: int | None


def _unique_scene_speech_span(audio, scenes, scene_plan_id: UUID) -> tuple[int, int] | None:
    """Find one unambiguous ordered exact-segment path accepted by source-bind."""
    segments = tuple(audio.transcript_segments)
    if (not audio.transcript_source or not segments or any(
        item.start_ms < 0 or item.end_ms <= item.start_ms or item.end_ms > audio.duration_ms
        or (index and item.start_ms < segments[index - 1].end_ms)
        for index, item in enumerate(segments)
    )):
        return None
    states: dict[tuple[int, tuple[int, int] | None], int] = {(-1, None): 1}
    for scene in scenes:
        target = "".join(comparison_tokens(scene.voice_text))
        if not target:
            return None
        candidates: list[tuple[int, int]] = []
        for start in range(len(segments)):
            spoken = ""
            for end in range(start, len(segments)):
                spoken += "".join(comparison_tokens(segments[end].text))
                if spoken == target:
                    candidates.append((start, end))
                if not target.startswith(spoken) or len(spoken) >= len(target):
                    break
        if not candidates:
            return None
        next_states: dict[tuple[int, tuple[int, int] | None], int] = {}
        for (last_end, selected), count in states.items():
            for start, end in candidates:
                if start <= last_end:
                    continue
                choice = (start, end) if scene.id == scene_plan_id else selected
                key = (end, choice)
                next_states[key] = min(2, next_states.get(key, 0) + count)
        if not next_states or len(next_states) > 10_000:
            return None
        states = next_states
    if sum(states.values()) != 1:
        return None
    selected = next(iter(states))[1]
    if selected is None:
        return None
    first, last = selected
    return segments[first].start_ms, segments[last].end_ms


def _run_from_row(db: Database, row) -> ProductionRun:
    voice_job_id = None if row["voice_job_id"] is None else UUID(row["voice_job_id"])
    voice_audio_id = None if row["voice_audio_id"] is None else UUID(row["voice_audio_id"])
    voice_qa_job_id = None if row["voice_qa_job_id"] is None else UUID(row["voice_qa_job_id"])
    auto_stops = tuple(json.loads(row["voice_qa_auto_stop_reasons"]))
    talking_master_id = None if row["talking_master_audio_id"] is None else UUID(row["talking_master_audio_id"])
    talking_dependencies = tuple(TalkingPlanDependency.model_validate(item) for item in json.loads(row["talking_dependencies"]))
    talking_bindings = tuple(TalkingSourceBinding.model_validate(item) for item in json.loads(row["talking_source_bindings"]))
    waiting_stops = tuple(json.loads(row["waiting_stop_reasons"]))
    talking_fields = dict(talking_review_policy_version=row["talking_review_policy_version"], talking_master_audio_id=talking_master_id, talking_dependencies=talking_dependencies,
                          talking_source_bindings=talking_bindings, waiting_stop_reasons=waiting_stops)
    if row["stage"] == "awaiting_talking":
        if (row["render_job_id"] is not None or talking_master_id is None or not talking_dependencies
            or (voice_job_id is not None and (voice_audio_id != talking_master_id or voice_qa_job_id is None))):
            raise ProductionRunConflict("Talking waiting stage has invalid dependencies")
        qa_dependencies = []
        jobs = JobRepository(db)
        assets = AssetRepository(db)
        for binding in talking_bindings:
            if binding.talking_job_id is None:
                continue
            generated = jobs.get(binding.talking_job_id)
            if generated is None or generated.type is not JobType.GENERATE_TALKING or generated.project_id != UUID(row["project_id"]):
                raise ProductionRunConflict("planned Talking Job is missing or mismatched")
            if generated.status in {JobStatus.FAILED, JobStatus.CANCELLED}:
                state = "generation_failed"
            elif generated.status is JobStatus.PENDING:
                state = "generation_pending"
            elif generated.status is JobStatus.RUNNING:
                state = "generation_running"
            elif binding.talking_qa_job_id is None:
                state = "output_awaiting_qa"
            else:
                qa = jobs.get(binding.talking_qa_job_id)
                if qa is None or qa.type is not JobType.VERIFY_TALKING or qa.project_id != generated.project_id:
                    raise ProductionRunConflict("planned Talking QA Job is missing or mismatched")
                if qa.status is JobStatus.PENDING:
                    state = "qa_pending"
                elif qa.status is JobStatus.RUNNING:
                    state = "qa_running"
                elif qa.status in {JobStatus.FAILED, JobStatus.CANCELLED}:
                    state = "qa_failed"
                else:
                    output = assets.get(binding.talking_output_asset_id) if binding.talking_output_asset_id else None
                    generation = None if output is None else output.metadata.get("talking_generation")
                    state = ("qa_verified_ready_for_preview" if row["talking_review_policy_version"] == 2 else "qa_verified_awaiting_u_talking") if isinstance(generation, dict) and generation.get("qa_state") == "verified" else "qa_failed"
            qa_dependencies.append(TalkingQaDependency(
                scene_plan_id=binding.scene_plan_id, generation_job_id=generated.id,
                output_asset_id=binding.talking_output_asset_id, qa_job_id=binding.talking_qa_job_id,
                state=state,
            ))
        previews = []
        for binding in talking_bindings:
            if binding.talking_series_id is None or binding.talking_preview_job_id is None:
                continue
            preview = jobs.get(binding.talking_preview_job_id)
            if preview is None or preview.project_id != UUID(row["project_id"]) or preview.type is not JobType.PREPARE_TALKING_RUN_PREVIEW:
                raise ProductionRunConflict("planned Talking preview Job is missing or mismatched")
            candidates = [asset for asset in assets.list() if isinstance(asset.metadata.get("talking_run_preview"), dict)
                          and asset.metadata["talking_run_preview"].get("job_id") == str(preview.id)]
            asset_id = candidates[0].id if len(candidates) == 1 else None
            state = {JobStatus.PENDING: "pending", JobStatus.RUNNING: "running",
                     JobStatus.FAILED: "failed", JobStatus.CANCELLED: "cancelled"}.get(preview.status)
            if state is None:
                state = "completed_review_only" if asset_id is not None else "output_missing"
            if binding.talking_run_id is not None:
                state = "admitted"
            output = assets.get(binding.talking_output_asset_id) if binding.talking_output_asset_id else None
            generation = output.metadata.get("talking_generation") if output is not None else None
            child_review = generation.get("human_review_state") if isinstance(generation, dict) else None
            continuity = TalkingSliceSeriesContinuityReviewRepository(db).get_by_series_id(binding.talking_series_id)
            previews.append(TalkingPreviewDependency(
                scene_plan_id=binding.scene_plan_id, series_id=binding.talking_series_id,
                job_id=preview.id, output_asset_id=asset_id, state=state,
                child_review_state=child_review if child_review in {"approved", "rejected"} else "pending",
                continuity_review_state="not_submitted" if continuity is None else "approved" if continuity.approved else "rejected",
            ))
        return ProductionRun(
            id=UUID(row["id"]), project_id=UUID(row["project_id"]),
            idempotency_key=row["idempotency_key"], preflight_fingerprint=row["preflight_fingerprint"],
            render_job_id=None, created_at=datetime.fromisoformat(row["created_at"]),
            voice_job_id=voice_job_id, voice_audio_id=voice_audio_id, voice_qa_job_id=voice_qa_job_id,
            status="awaiting_talking_dependencies", talking_qa_dependencies=tuple(qa_dependencies),
            talking_preview_dependencies=tuple(previews), **talking_fields,
        )
    if row["stage"] == "voice":
        if voice_job_id is None or row["render_job_id"] is not None:
            raise ProductionRunConflict("Voice production stage has an invalid Job reference")
        job = JobRepository(db).get(voice_job_id)
        if job is None or job.type is not JobType.GENERATE_VOICE or job.project_id != UUID(row["project_id"]):
            raise ProductionRunConflict("production run Voice Job is missing or mismatched")
        status = {
            JobStatus.PENDING: "voice_pending", JobStatus.RUNNING: "voice_running",
            JobStatus.FAILED: "voice_failed", JobStatus.COMPLETED: "voice_completed_awaiting_qa",
            JobStatus.CANCELLED: "cancelled",
        }[job.status]
        if voice_qa_job_id is not None:
            qa_job = JobRepository(db).get(voice_qa_job_id)
            if voice_audio_id is None or qa_job is None or qa_job.type is not JobType.VERIFY_VOICE or qa_job.project_id != job.project_id:
                raise ProductionRunConflict("production run Voice QA dependency is missing or mismatched")
            qa_status = {
                JobStatus.PENDING: "voice_qa_pending", JobStatus.RUNNING: "voice_qa_running",
                JobStatus.FAILED: "voice_qa_failed", JobStatus.CANCELLED: "voice_qa_failed",
                JobStatus.COMPLETED: "awaiting_u_voice_review",
            }[qa_job.status]
            if qa_job.status is JobStatus.COMPLETED:
                audio = AudioAssetRepository(db).get(voice_audio_id)
                generation = None if audio is None else audio.metadata.get("voice_generation")
                if not isinstance(generation, dict) or generation.get("qa_state") != "verified":
                    qa_status = "voice_qa_failed"
                else:
                    review = voice_human_review_status(audio)
                    if review == "rejected":
                        qa_status = "u_voice_rejected"
                    elif review == "approved":
                        qa_status = "approved_master_ready_to_resume"
            status = qa_status
        return ProductionRun(
            id=UUID(row["id"]), project_id=UUID(row["project_id"]),
            idempotency_key=row["idempotency_key"], preflight_fingerprint=row["preflight_fingerprint"],
            render_job_id=None, voice_job_id=voice_job_id,
            voice_audio_id=voice_audio_id, voice_qa_job_id=voice_qa_job_id,
            voice_qa_auto_stop_reasons=auto_stops,
            created_at=datetime.fromisoformat(row["created_at"]), status=status, **talking_fields,
        )
    if row["stage"] in {"awaiting_master", "cancelled"}:
        if row["render_job_id"] is not None:
            raise ProductionRunConflict("non-render production stage has a render Job")
        return ProductionRun(
            id=UUID(row["id"]), project_id=UUID(row["project_id"]),
            idempotency_key=row["idempotency_key"], preflight_fingerprint=row["preflight_fingerprint"],
            render_job_id=None, voice_job_id=voice_job_id, voice_audio_id=voice_audio_id,
            voice_qa_job_id=voice_qa_job_id, created_at=datetime.fromisoformat(row["created_at"]),
            voice_qa_auto_stop_reasons=auto_stops,
            status="awaiting_approved_master" if row["stage"] == "awaiting_master" else "cancelled", **talking_fields,
        )
    if row["render_job_id"] is None:
        raise ProductionRunConflict("render-stage production run has no Job")
    job = JobRepository(db).get(UUID(row["render_job_id"]))
    if job is None or job.type is not JobType.RENDER or job.project_id != UUID(row["project_id"]):
        raise ProductionRunConflict("production run render Job is missing or mismatched")
    status = {
        JobStatus.PENDING: "render_pending",
        JobStatus.RUNNING: "render_running",
        JobStatus.FAILED: "render_failed",
        JobStatus.COMPLETED: "render_completed_awaiting_review",
        JobStatus.CANCELLED: "cancelled",
    }[job.status]
    return ProductionRun(
        id=UUID(row["id"]), project_id=UUID(row["project_id"]),
        idempotency_key=row["idempotency_key"], preflight_fingerprint=row["preflight_fingerprint"],
        render_job_id=job.id, voice_job_id=voice_job_id,
        voice_audio_id=voice_audio_id, voice_qa_job_id=voice_qa_job_id,
        voice_qa_auto_stop_reasons=auto_stops,
        created_at=datetime.fromisoformat(row["created_at"]), status=status, **talking_fields,
    )


class ProductionRunService:
    def __init__(self, db: Database, data_root: str | Path):
        self.db = db
        self.data_root = Path(data_root).resolve()

    def get(self, project_id: UUID, run_id: UUID) -> ProductionRun | None:
        row = self.db.connection.execute(
            "SELECT * FROM production_runs WHERE id = ? AND project_id = ?", (str(run_id), str(project_id)),
        ).fetchone()
        return None if row is None else _run_from_row(self.db, row)

    def talking_source_context(
        self, project_id: UUID, run_id: UUID, *, scene_plan_id: UUID,
    ) -> TalkingSourceContext | None:
        row = self.db.connection.execute(
            "SELECT * FROM production_runs WHERE id = ? AND project_id = ?", (str(run_id), str(project_id)),
        ).fetchone()
        if row is None:
            return None
        if row["stage"] != "awaiting_talking":
            raise ProductionRunConflict("production run is not awaiting Talking dependencies")
        run = _run_from_row(self.db, row)
        dependency = next((item for item in run.talking_dependencies if item.scene_plan_id == scene_plan_id), None)
        if dependency is None:
            raise ProductionRunConflict("scene is not a planned Talking dependency")
        project = ProjectRepository(self.db).get(project_id)
        draft = ProjectDraftRepository(self.db).get(project_id)
        if (project is None or draft is None or draft.version != row["draft_version"]
            or draft.script_revision != row["script_revision"]):
            raise ProductionRunConflict("production plan draft changed")
        self._require_bound_voice_master(row, run.talking_master_audio_id)
        self._require_master_bytes(run.talking_master_audio_id)
        master = AudioAssetRepository(self.db).get(run.talking_master_audio_id)
        preflight = build_production_preflight(
            self.db, project, draft,
            style_tokens=VisualStyleTokens.model_validate(json.loads(row["style_tokens"])),
            repair_allowance=row["repair_allowance"],
        )
        selected = next((item for item in preflight.scenes if item.scene_plan_id == scene_plan_id), None)
        if (self._start_stage(preflight) != "awaiting_talking"
            or preflight.reusable_master_audio_id != run.talking_master_audio_id
            or selected is None or selected.visual_dependency_fingerprint != dependency.visual_dependency_fingerprint):
            raise ProductionRunConflict("planned Talking dependency or Master changed")
        ordered = sorted(draft.scenes, key=lambda item: item.order)
        scene = next((item for item in ordered if item.id == scene_plan_id), None)
        if scene is None:
            raise ProductionRunConflict("scene is missing from current draft")
        interval = _unique_scene_speech_span(master, ordered, scene_plan_id)
        return TalkingSourceContext(
            project_id=project_id, run_id=run_id, scene_plan_id=scene_plan_id,
            scene_id=dependency.scene_id, scene_voice_text=scene.voice_text,
            preflight_fingerprint=run.preflight_fingerprint,
            visual_dependency_fingerprint=dependency.visual_dependency_fingerprint,
            master_audio_id=master.id, master_content_hash=master.content_hash,
            status="ready" if interval is not None else "unresolved",
            reason=None if interval is not None else "master_scene_speech_interval_ambiguous_or_unaligned",
            speech_start_ms=None if interval is None else interval[0],
            speech_end_ms=None if interval is None else interval[1],
        )

    def start(
        self, project_id: UUID, *, idempotency_key: str, expected_fingerprint: str,
        style_tokens: VisualStyleTokens, repair_allowance: int = 1, talking_review_policy_version: int = 1,
        purpose: str = "commercial_production", use_admission_ids: tuple[UUID, ...] = (),
    ) -> ProductionRun:
        if purpose not in ("commercial_production", "internal_evaluation"):
            raise ProductionRunNotReady(("execution_purpose_unsupported",))
        if purpose == "commercial_production" and use_admission_ids:
            raise ProductionRunNotReady(("evaluation_scope_not_commercial",))
        if purpose == "internal_evaluation":
            from app.execution_scope import ExecutionScopeService
            from app.execution_admission import UseAdmissionError
            try:
                ExecutionScopeService(self.db, self.data_root).prepare(project_id,
                    purpose=purpose, admission_ids=use_admission_ids)
            except UseAdmissionError as exc:
                raise ProductionRunNotReady((str(exc),)) from None
            raise ProductionRunNotReady(("evaluation_execution_not_integrated",))
        if talking_review_policy_version not in {1, 2}:
            raise ProductionRunConflict("talking_review_policy_unknown")
        with self.db.transaction(immediate=True):
            project = ProjectRepository(self.db).get(project_id)
            if project is None:
                raise ProductionRunError("project not found")
            draft = ProjectDraftRepository(self.db).get(project_id)
            preflight = build_production_preflight(
                self.db, project, draft, style_tokens=style_tokens,
                expected_fingerprint=expected_fingerprint, repair_allowance=repair_allowance,
            )
            stage = self._start_stage(preflight)
            if talking_review_policy_version == 2 and not any(item.production_need == "new_talking" for item in preflight.scenes):
                raise ProductionRunConflict("aggregate_policy_requires_planned_talking")
            request_fingerprint = _snapshot_sha256({"plan": preflight.fingerprint, "talking_review_policy_version": talking_review_policy_version})
            if stage in {"render", "awaiting_talking"}:
                self._require_master_bytes(preflight.reusable_master_audio_id)
            existing = self.db.connection.execute(
                "SELECT * FROM production_runs WHERE idempotency_key = ?", (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if (existing["project_id"] != str(project_id) or existing["preflight_fingerprint"] != preflight.fingerprint
                    or existing["talking_review_policy_version"] != talking_review_policy_version
                    or existing["review_request_fingerprint"] not in {None, request_fingerprint}):
                    raise ProductionRunConflict("idempotency key belongs to a different production plan")
                return _run_from_row(self.db, existing)
            duplicate = self.db.connection.execute(
                "SELECT * FROM production_runs WHERE project_id = ? AND preflight_fingerprint = ?",
                (str(project_id), preflight.fingerprint),
            ).fetchone()
            if duplicate is not None:
                if duplicate["talking_review_policy_version"] != talking_review_policy_version:
                    raise ProductionRunConflict("production_plan_already_bound_to_different_review_policy")
                return _run_from_row(self.db, duplicate)
            assert draft is not None
            run_id, now = uuid4(), datetime.now(timezone.utc)
            job_id = self._enqueue_render(project, draft, preflight, run_id, now) if stage == "render" else None
            talking_dependencies = tuple(TalkingPlanDependency(
                scene_plan_id=item.scene_plan_id, scene_id=item.scene_id,
                visual_dependency_fingerprint=item.visual_dependency_fingerprint,
            ) for item in preflight.scenes if item.production_need == "new_talking")
            waiting_stops = preflight.stop_reasons if talking_dependencies else ()
            self.db.connection.execute(
                """INSERT INTO production_runs(
                    id, project_id, idempotency_key, preflight_fingerprint, draft_version,
                    script_revision, style_tokens, repair_allowance, stage, render_job_id, created_at,
                    talking_master_audio_id, talking_dependencies, waiting_stop_reasons,
                    talking_review_policy_version, review_request_fingerprint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(run_id), str(project_id), idempotency_key, preflight.fingerprint,
                 draft.version, draft.script_revision, style_tokens.model_dump_json(), repair_allowance,
                 stage, None if job_id is None else str(job_id), now.isoformat(),
                 str(preflight.reusable_master_audio_id) if stage == "awaiting_talking" else None,
                 json.dumps([item.model_dump(mode="json") for item in talking_dependencies]), json.dumps(waiting_stops),
                 talking_review_policy_version, request_fingerprint),
            )
            return ProductionRun(
                id=run_id, project_id=project_id, idempotency_key=idempotency_key,
                preflight_fingerprint=preflight.fingerprint, render_job_id=job_id,
                created_at=now, status={"render": "render_pending", "awaiting_master": "awaiting_approved_master",
                                        "awaiting_talking": "awaiting_talking_dependencies"}[stage],
                talking_master_audio_id=preflight.reusable_master_audio_id if stage == "awaiting_talking" else None,
                talking_dependencies=talking_dependencies, waiting_stop_reasons=waiting_stops,
                talking_review_policy_version=talking_review_policy_version,
            )

    def bind_talking_source(
        self, project_id: UUID, run_id: UUID, *, scene_plan_id: UUID, talking_profile_id: UUID,
        reference_clip_id: UUID, capability_profile_id: UUID, master_start_ms: int,
        master_end_ms: int, authorization_reference: str, brief: TalkingPerformanceBrief,
        _new_inference: bool = True,
    ) -> ProductionRun | None:
        """Retain an exact candidate, not full-interval suitability or dispatch admission."""
        if not authorization_reference.strip():
            raise ProductionRunNotReady(("talking_authorization_reference_required",))
        if master_start_ms < 0 or master_end_ms <= master_start_ms:
            raise ProductionRunNotReady(("talking_master_interval_invalid",))
        with (nullcontext() if self.db.connection.in_transaction else self.db.transaction(immediate=True)):
            row = self.db.connection.execute(
                "SELECT * FROM production_runs WHERE id = ? AND project_id = ?", (str(run_id), str(project_id)),
            ).fetchone()
            if row is None:
                return None
            if row["stage"] != "awaiting_talking":
                raise ProductionRunConflict("production run is not awaiting Talking dependencies")
            run = _run_from_row(self.db, row)
            self._require_bound_voice_master(row, run.talking_master_audio_id)
            dependency = next((item for item in run.talking_dependencies if item.scene_plan_id == scene_plan_id), None)
            if dependency is None:
                raise ProductionRunConflict("scene is not a planned Talking dependency")
            project = ProjectRepository(self.db).get(project_id)
            draft = ProjectDraftRepository(self.db).get(project_id)
            if project is None or draft is None or draft.version != row["draft_version"] or draft.script_revision != row["script_revision"]:
                raise ProductionRunConflict("production plan draft changed; replan before Talking source binding")
            preflight = build_production_preflight(
                self.db, project, draft,
                style_tokens=VisualStyleTokens.model_validate(json.loads(row["style_tokens"])),
                repair_allowance=row["repair_allowance"],
            )
            if self._start_stage(preflight) != "awaiting_talking" or preflight.reusable_master_audio_id != run.talking_master_audio_id:
                raise ProductionRunConflict("planned Talking dependency or Master changed")
            scene = next((item for item in preflight.scenes if item.scene_plan_id == scene_plan_id), None)
            if scene is None or scene.visual_dependency_fingerprint != dependency.visual_dependency_fingerprint:
                raise ProductionRunConflict("planned Talking scene changed")
            if _new_inference and any(reason in preflight.stop_reasons for reason in (
                "budget_call_limit", "budget_amount_limit", "unknown_cost_requires_budget_authorization",
            )):
                raise ProductionRunNotReady(("talking_budget_not_authorized",))
            master = AudioAssetRepository(self.db).get(run.talking_master_audio_id)
            if master is None or voice_human_review_status(master) != "approved":
                raise ProductionRunNotReady(("talking_approved_master_required",))
            self._require_master_bytes(master.id)
            target_scene = next((item for item in draft.scenes if item.id == scene_plan_id), None)
            if target_scene is None or master_end_ms > master.duration_ms:
                raise ProductionRunNotReady(("talking_master_interval_invalid",))
            segments = [item for item in master.transcript_segments
                        if item.start_ms >= master_start_ms and item.end_ms <= master_end_ms]
            if (not segments or segments[0].start_ms != master_start_ms or segments[-1].end_ms != master_end_ms
                or any(item.start_ms < previous.end_ms for previous, item in zip(segments, segments[1:]))
                or "".join(comparison_tokens(" ".join(item.text for item in segments)))
                   != "".join(comparison_tokens(target_scene.voice_text))):
                raise ProductionRunNotReady(("talking_master_interval_not_exact_scene_speech",))
            profile = TalkingProfileRepository(self.db).get(talking_profile_id)
            clip = ClipRepository(self.db).get(reference_clip_id)
            if profile is None or not profile.consent.confirmed or reference_clip_id not in profile.reference_clip_ids:
                raise ProductionRunNotReady(("talking_profile_consent_required",))
            if clip is None:
                raise ProductionRunNotReady(("talking_reference_clip_missing",))
            asset = AssetRepository(self.db).get(clip.asset_id)
            if asset is None or asset.source_kind not in {SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET} or not asset.authorization_reference.strip():
                raise ProductionRunNotReady(("talking_reference_rights_required",))
            requested_brief = brief.model_copy(update={
                "minimum_reference_duration_ms": max(brief.minimum_reference_duration_ms, master_end_ms - master_start_ms),
            })
            fit = select_talking_reference([clip], requested_brief)
            if fit.selected_clip_id != clip.id:
                raise ProductionRunNotReady(("talking_reference_fit_not_verified",))
            self._require_source_bytes(asset.source_file, asset.content_hash)
            capability = ProviderMachineCapabilityProfileRepository(self.db).get(capability_profile_id)
            if (capability is None or capability.capability != "talking" or capability.provider != profile.provider
                or capability.readiness != "verified" or capability.quality_status != "verified"
                or not capability.evidence_reference):
                raise ProductionRunNotReady(("exact_talking_capability_not_verified",))
            if capability.commercial_status != "commercial_safe" or not capability.license_evidence_reference:
                raise ProductionRunNotReady(("talking_provider_license_scope_unverified",))
            binding = TalkingSourceBinding(
                scene_plan_id=scene_plan_id, talking_profile_id=talking_profile_id,
                reference_clip_id=clip.id, reference_asset_id=asset.id,
                reference_asset_hash=asset.content_hash, reference_start_ms=clip.start_ms,
                reference_end_ms=clip.end_ms, source_authorization_reference=asset.authorization_reference,
                reference_assessment_reference=clip.talking_reference_assessment.evidence_reference,
                performance_brief=requested_brief,
                capability_profile_id=capability_profile_id, master_audio_id=master.id,
                capability_evidence_reference=capability.evidence_reference,
                license_evidence_reference=capability.license_evidence_reference,
                master_start_ms=master_start_ms, master_end_ms=master_end_ms,
                authorization_reference=authorization_reference,
            )
            previous = next((item for item in run.talking_source_bindings if item.scene_plan_id == scene_plan_id), None)
            if previous is not None:
                if previous.model_copy(update={
                    "suitability": "full_interval_unknown", "suitability_assessment_id": None,
                    "talking_job_id": None, "talking_output_asset_id": None, "talking_qa_job_id": None,
                    "talking_series_id": None, "talking_preview_job_id": None, "talking_run_id": None,
                }) != binding:
                    raise ProductionRunConflict("Talking source binding differs from persisted candidate")
                return run
            bindings = (*run.talking_source_bindings, binding)
            reasons = tuple(dict.fromkeys((*run.waiting_stop_reasons, f"talking_full_interval_suitability_unverified:{dependency.scene_id}")))
            self.db.connection.execute(
                "UPDATE production_runs SET talking_source_bindings = ?, waiting_stop_reasons = ? WHERE id = ? AND stage = 'awaiting_talking'",
                (json.dumps([item.model_dump(mode="json") for item in bindings]), json.dumps(reasons), str(run_id)),
            )
            return self.get(project_id, run_id)

    def _validated_talking_dispatch(
        self, project_id: UUID, run_id: UUID, binding: TalkingSourceBinding,
        admission_id: UUID, *, new_inference: bool = True,
    ) -> tuple[TalkingSourceBinding, object, object, object, object]:
        """Reopen every mutable candidate gate within the caller's transaction."""
        self.bind_talking_source(
            project_id, run_id, scene_plan_id=binding.scene_plan_id,
            talking_profile_id=binding.talking_profile_id, reference_clip_id=binding.reference_clip_id,
            capability_profile_id=binding.capability_profile_id,
            master_start_ms=binding.master_start_ms, master_end_ms=binding.master_end_ms,
            authorization_reference=binding.authorization_reference, brief=binding.performance_brief,
            _new_inference=new_inference,
        )
        run = self.get(project_id, run_id)
        if run is None or run.status != "awaiting_talking_dependencies":
            raise ProductionRunConflict("planned Talking run is no longer waiting")
        current = next((item for item in run.talking_source_bindings if item.scene_plan_id == binding.scene_plan_id), None)
        if current != binding or current.suitability != "review_claimed_suitable" or current.suitability_assessment_id is None:
            raise ProductionRunNotReady(("talking_source_review_not_admitted",))
        admissions = TalkingSourceAdmissionRepository(self.db, self.data_root)
        receipt = admissions.get(admission_id)
        if receipt is None or receipt.assessment_id != current.suitability_assessment_id:
            raise ProductionRunNotReady(("talking_source_admission_missing",))
        try:
            assessment = admissions.require_current(receipt)
        except ValueError as exc:
            raise ProductionRunNotReady(("talking_source_admission_stale_or_revoked",)) from exc
        if (assessment.source_asset_id != current.reference_asset_id
            or assessment.source_clip_id != current.reference_clip_id
            or assessment.source_content_hash != current.reference_asset_hash
            or assessment.start_ms != current.reference_start_ms
            or assessment.end_ms != current.reference_end_ms
            or assessment.presentation != "source_native_portrait"):
            raise ProductionRunNotReady(("talking_source_admission_mismatch",))
        clip = ClipRepository(self.db).get(current.reference_clip_id)
        if (clip is None or clip.talking_reference_assessment is None
            or clip.talking_reference_assessment.burned_in_subtitles is not False):
            raise ProductionRunNotReady(("talking_native_source_subtitles_not_cleared",))
        profile = TalkingProfileRepository(self.db).get(current.talking_profile_id)
        master = AudioAssetRepository(self.db).get(current.master_audio_id)
        capability = ProviderMachineCapabilityProfileRepository(self.db).get(current.capability_profile_id)
        if profile is None or master is None or capability is None:
            raise ProductionRunNotReady(("talking_execution_dependency_missing",))
        if capability.mode != "local":
            raise ProductionRunNotReady(("talking_remote_dispatch_not_implemented",))
        limit = capability.verified_parameters.get("fresh_voice_talking_duration_ms")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ProductionRunNotReady(("talking_provider_duration_bound_unverified",))
        indices = [index for index, segment in enumerate(master.transcript_segments)
                   if segment.start_ms >= current.master_start_ms and segment.end_ms <= current.master_end_ms]
        if not indices or indices != list(range(indices[0], indices[-1] + 1)):
            raise ProductionRunNotReady(("talking_master_interval_not_contiguous",))
        try:
            slice_plan = plan_talking_audio_slice(
                master, start_segment_index=indices[0], end_segment_index=indices[-1] + 1,
                max_duration_ms=limit,
            )
        except TalkingSlicePlanningError as exc:
            raise ProductionRunNotReady(("talking_scene_exceeds_verified_duration_bound",)) from exc
        if slice_plan.start_ms != current.master_start_ms or slice_plan.end_ms != current.master_end_ms:
            raise ProductionRunNotReady(("talking_master_interval_changed",))
        return current, profile, master, capability, slice_plan

    def dispatch_talking(
        self, project_id: UUID, run_id: UUID, *, scene_plan_id: UUID, admission_id: UUID,
    ) -> ProductionRun | None:
        """Atomically enqueue one bounded planned Talking Job, never a ProviderCall."""
        with self.db.transaction(immediate=True):
            run = self.get(project_id, run_id)
            if run is None:
                return None
            if run.status != "awaiting_talking_dependencies":
                raise ProductionRunConflict("production run is not awaiting Talking dependencies")
            binding = next((item for item in run.talking_source_bindings if item.scene_plan_id == scene_plan_id), None)
            if binding is None:
                raise ProductionRunNotReady(("talking_reference_candidate_not_bound",))
            current, profile, master, capability, slice_plan = self._validated_talking_dispatch(
                project_id, run_id, binding, admission_id,
            )
            context = PlannedTalkingContext(
                run_id=run_id, scene_plan_id=scene_plan_id, source_admission_id=admission_id,
                capability_profile_id=capability.id, expected_provider=capability.provider,
                expected_model=capability.model, expected_runtime=capability.runtime,
                expected_machine_id=capability.machine_id,
                binding_sha256=_talking_binding_sha256(current),
                consent_sha256=_snapshot_sha256(profile.consent.model_dump(mode="json")),
            )
            payload = TalkingGenerationJobPayload(
                project_id=project_id, talking_profile_id=profile.id,
                reference_clip_id=current.reference_clip_id, narration_audio_id=master.id,
                authorization_reference=current.authorization_reference, planned_context=context,
                slice_start_segment_index=slice_plan.start_segment_index,
                slice_end_segment_index=slice_plan.end_segment_index,
                slice_max_duration_ms=capability.verified_parameters["fresh_voice_talking_duration_ms"],
                slice_limit_source=f"verified-capability:{capability.id}",
                reference_window_start_ms=current.reference_start_ms,
                reference_window_end_ms=current.reference_end_ms,
            )
            if current.talking_job_id is not None:
                prior = JobRepository(self.db).get(current.talking_job_id)
                if prior is None or prior.payload != payload or prior.project_id != project_id:
                    raise ProductionRunConflict("planned Talking Job differs from the persisted dependency")
                return run
            now = datetime.now(timezone.utc)
            job = Job(
                project_id=project_id, type=JobType.GENERATE_TALKING,
                idempotency_key=f"production-run:{run_id}:talking:{scene_plan_id}",
                created_at=now, updated_at=now, payload=payload,
            )
            persisted = JobRepository(self.db).create(job)
            if persisted.id != job.id or persisted.payload != payload:
                raise ProductionRunConflict("planned Talking Job idempotency conflict")
            updated = current.model_copy(update={"talking_job_id": job.id})
            bindings = tuple(updated if item.scene_plan_id == scene_plan_id else item
                             for item in run.talking_source_bindings)
            all_positive_bound = all(item.suitability != "review_claimed_suitable" or item.talking_job_id is not None
                                     for item in bindings)
            reasons = tuple(reason for reason in run.waiting_stop_reasons
                            if reason != "talking_dispatch_not_implemented" or not all_positive_bound)
            reasons = tuple(dict.fromkeys((*reasons, f"talking_qa_run_not_implemented:{scene_plan_id}")))
            self.db.connection.execute(
                "UPDATE production_runs SET talking_source_bindings = ?, waiting_stop_reasons = ? WHERE id = ? AND stage = 'awaiting_talking'",
                (json.dumps([item.model_dump(mode="json") for item in bindings]), json.dumps(reasons), str(run_id)),
            )
            return self.get(project_id, run_id)

    def require_talking_job_admission(
        self, job: Job, *, provider: str, model: str, runtime: str | None, machine_id: str | None,
        _new_inference: bool = True,
    ) -> None:
        """Fail before ProviderCall when any planned dependency has changed."""
        if not isinstance(job.payload, TalkingGenerationJobPayload) or job.payload.planned_context is None:
            raise ProductionRunNotReady(("planned_talking_context_missing",))
        context = job.payload.planned_context
        if (provider != context.expected_provider or model != context.expected_model
            or runtime != context.expected_runtime or machine_id != context.expected_machine_id):
            raise ProductionRunNotReady(("talking_worker_identity_mismatch",))
        with (nullcontext() if self.db.connection.in_transaction else self.db.transaction(immediate=True)):
            run = self.get(job.project_id, context.run_id)
            if run is None or run.status != "awaiting_talking_dependencies":
                raise ProductionRunNotReady(("planned_talking_run_cancelled_or_missing",))
            binding = next((item for item in run.talking_source_bindings
                            if item.scene_plan_id == context.scene_plan_id), None)
            if binding is None or binding.talking_job_id != job.id:
                raise ProductionRunNotReady(("planned_talking_job_not_bound",))
            current, profile, master, capability, slice_plan = self._validated_talking_dispatch(
                job.project_id, context.run_id, binding, context.source_admission_id,
                new_inference=_new_inference,
            )
            if (context.capability_profile_id != capability.id
                or context.expected_provider != capability.provider
                or context.expected_model != capability.model
                or context.expected_runtime != capability.runtime
                or context.expected_machine_id != capability.machine_id
                or context.binding_sha256 != _talking_binding_sha256(current)
                or context.consent_sha256 != _snapshot_sha256(profile.consent.model_dump(mode="json"))
                or job.payload.talking_profile_id != profile.id
                or job.payload.reference_clip_id != current.reference_clip_id
                or job.payload.narration_audio_id != master.id
                or job.payload.authorization_reference != current.authorization_reference
                or job.payload.slice_start_segment_index != slice_plan.start_segment_index
                or job.payload.slice_end_segment_index != slice_plan.end_segment_index
                or job.payload.slice_max_duration_ms != capability.verified_parameters["fresh_voice_talking_duration_ms"]
                or job.payload.reference_window_start_ms != current.reference_start_ms
                or job.payload.reference_window_end_ms != current.reference_end_ms):
                raise ProductionRunNotReady(("planned_talking_execution_snapshot_changed",))

    def _require_talking_output(self, project_id: UUID, run_id: UUID, binding: TalkingSourceBinding, job: Job):
        """Select one immutable generated output, never an arbitrary AI video."""
        if job.status is not JobStatus.COMPLETED or not isinstance(job.payload, TalkingGenerationJobPayload):
            raise ProductionRunNotReady(("talking_job_not_completed",))
        context = job.payload.planned_context
        if (context is None or context.run_id != run_id or context.scene_plan_id != binding.scene_plan_id
            or job.project_id != project_id or job.payload.narration_audio_id != binding.master_audio_id):
            raise ProductionRunNotReady(("talking_job_provenance_mismatch",))
        matches = []
        for asset in AssetRepository(self.db).list():
            generation = asset.metadata.get("talking_generation")
            if isinstance(generation, dict) and generation.get("job_id") == str(job.id):
                matches.append(asset)
        if len(matches) != 1:
            raise ProductionRunNotReady(("talking_output_missing_or_ambiguous",))
        asset = matches[0]
        generation = asset.metadata["talking_generation"]
        if (asset.source_kind is not SourceKind.AI_VIDEO
            or asset.authorization_reference != binding.authorization_reference
            or generation.get("planned_context") != context.model_dump(mode="json")
            or generation.get("talking_profile_id") != str(binding.talking_profile_id)
            or generation.get("reference_clip_id") != str(binding.reference_clip_id)
            or generation.get("narration_audio_id") != str(binding.master_audio_id)
            or generation.get("provider") != context.expected_provider
            or generation.get("model") != context.expected_model
            or generation.get("master_slice_start_ms") != binding.master_start_ms
            or generation.get("master_slice_end_ms") != binding.master_end_ms
            or generation.get("reference_window_start_ms") != binding.reference_start_ms
            or generation.get("reference_window_end_ms") != binding.reference_end_ms):
            raise ProductionRunNotReady(("talking_output_provenance_mismatch",))
        self._require_talking_output_bytes(asset)
        return asset

    def _require_talking_output_bytes(self, asset) -> Path:
        value = Path(asset.source_file)
        if value.is_absolute():
            source = value.resolve()  # Legacy imported absolute paths.
        else:
            source = (self.data_root.parent / value if value.parts and value.parts[0].casefold() == self.data_root.name.casefold()
                      else self.data_root / value).resolve()
            if not source.is_relative_to(self.data_root):
                raise ProductionRunNotReady(("talking_output_path_escapes_data_root",))
        if not source.is_file():
            raise ProductionRunNotReady(("talking_output_file_missing",))
        with source.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != asset.content_hash:
                raise ProductionRunNotReady(("talking_output_bytes_changed",))
        return source

    def advance_talking_qa(self, project_id: UUID, run_id: UUID, *, scene_plan_id: UUID) -> ProductionRun | None:
        """Bind one completed planned output to a durable local technical QA Job."""
        with self.db.transaction(immediate=True):
            run = self.get(project_id, run_id)
            if run is None:
                return None
            if run.status != "awaiting_talking_dependencies":
                raise ProductionRunConflict("production run is not awaiting Talking dependencies")
            binding = next((item for item in run.talking_source_bindings if item.scene_plan_id == scene_plan_id), None)
            if binding is None or binding.talking_job_id is None:
                raise ProductionRunNotReady(("planned_talking_job_missing",))
            generated = JobRepository(self.db).get(binding.talking_job_id)
            if generated is None:
                raise ProductionRunNotReady(("planned_talking_job_missing",))
            output = self._require_talking_output(project_id, run_id, binding, generated)
            master = AudioAssetRepository(self.db).get(binding.master_audio_id)
            if master is None:
                raise ProductionRunNotReady(("talking_master_missing",))
            self._require_master_bytes(master.id)
            if (binding.talking_qa_job_id is not None or binding.talking_output_asset_id is not None):
                qa = JobRepository(self.db).get(binding.talking_qa_job_id) if binding.talking_qa_job_id else None
                if (qa is None or not isinstance(qa.payload, TalkingQaJobPayload)
                    or qa.payload.output_asset_id != output.id or qa.payload.output_sha256 != output.content_hash
                    or qa.payload.generation_job_id != generated.id
                    or qa.payload.project_id != project_id or qa.payload.run_id != run_id
                    or qa.payload.scene_plan_id != scene_plan_id
                    or qa.payload.master_audio_id != master.id or qa.payload.master_sha256 != master.content_hash
                    or qa.payload.master_start_ms != binding.master_start_ms
                    or qa.payload.master_end_ms != binding.master_end_ms
                    or binding.talking_output_asset_id != output.id):
                    raise ProductionRunConflict("planned Talking QA dependency changed")
                return run
            if output.metadata["talking_generation"].get("qa_state") != "pending":
                raise ProductionRunNotReady(("talking_output_qa_not_pending",))
            payload = TalkingQaJobPayload(
                project_id=project_id, run_id=run_id, scene_plan_id=scene_plan_id,
                generation_job_id=generated.id, output_asset_id=output.id, output_sha256=output.content_hash,
                master_audio_id=master.id, master_sha256=master.content_hash,
                master_start_ms=binding.master_start_ms, master_end_ms=binding.master_end_ms,
            )
            now = datetime.now(timezone.utc)
            from app.talking.repair import operation_key
            qa = Job(
                project_id=project_id, type=JobType.VERIFY_TALKING,
                idempotency_key=operation_key(self.db, run_id, scene_plan_id, generated.id, "talking-qa"),
                created_at=now, updated_at=now, payload=payload,
            )
            persisted = JobRepository(self.db).create(qa)
            if persisted.id != qa.id or persisted.payload != payload:
                raise ProductionRunConflict("planned Talking QA idempotency conflict")
            updated = binding.model_copy(update={"talking_output_asset_id": output.id, "talking_qa_job_id": qa.id})
            bindings = tuple(updated if item.scene_plan_id == scene_plan_id else item for item in run.talking_source_bindings)
            transient_qa_reasons = {
                f"talking_qa_run_not_implemented:{scene_plan_id}",
                "talking_qa_worker_not_configured", "talking_qa_reconciliation_conflict",
                "talking_output_missing_or_ambiguous", "talking_output_provenance_mismatch",
                "talking_output_file_missing", "talking_output_bytes_changed",
                "talking_master_missing", "master_narration_file_missing", "master_narration_bytes_changed",
            }
            reasons = tuple(reason for reason in run.waiting_stop_reasons if reason not in transient_qa_reasons)
            review_reason = ("talking_exact_preview_review_required" if run.talking_review_policy_version == 2 else "talking_u_review_run_not_implemented")
            reasons = tuple(dict.fromkeys((*reasons, f"{review_reason}:{scene_plan_id}")))
            self.db.connection.execute(
                "UPDATE production_runs SET talking_source_bindings = ?, waiting_stop_reasons = ? WHERE id = ? AND stage = 'awaiting_talking'",
                (json.dumps([item.model_dump(mode="json") for item in bindings]), json.dumps(reasons), str(run_id)),
            )
            return self.get(project_id, run_id)

    def require_talking_qa_job(self, job: Job):
        if job.type is not JobType.VERIFY_TALKING or not isinstance(job.payload, TalkingQaJobPayload):
            raise ProductionRunNotReady(("talking_qa_payload_invalid",))
        payload = job.payload
        with (nullcontext() if self.db.connection.in_transaction else self.db.transaction(immediate=True)):
            run = self.get(payload.project_id, payload.run_id)
            if run is None or run.status not in {"awaiting_talking_dependencies", "render_pending", "render_running", "render_failed", "render_completed_awaiting_review"}:
                raise ProductionRunNotReady(("talking_qa_run_cancelled_or_missing",))
            binding = next((item for item in run.talking_source_bindings if item.scene_plan_id == payload.scene_plan_id), None)
            if (binding is None or binding.talking_job_id != payload.generation_job_id
                or binding.talking_qa_job_id != job.id or binding.talking_output_asset_id != payload.output_asset_id
                or binding.master_audio_id != payload.master_audio_id
                or binding.master_start_ms != payload.master_start_ms or binding.master_end_ms != payload.master_end_ms):
                raise ProductionRunNotReady(("talking_qa_dependency_changed",))
            generated = JobRepository(self.db).get(payload.generation_job_id)
            if generated is None:
                raise ProductionRunNotReady(("talking_generation_job_missing",))
            output = self._require_talking_output(payload.project_id, payload.run_id, binding, generated)
            master = AudioAssetRepository(self.db).get(payload.master_audio_id)
            if (output.id != payload.output_asset_id or output.content_hash != payload.output_sha256
                or master is None or master.content_hash != payload.master_sha256):
                raise ProductionRunNotReady(("talking_qa_media_snapshot_changed",))
            self._require_master_bytes(master.id)
            return output, master

    def require_current_planned_run_origin(
        self, project_id: UUID, run_id: UUID, scene_plan_id: UUID,
        *, expected: PlannedTalkingRunOrigin | None = None,
    ) -> PlannedTalkingRunOrigin:
        """Revalidate an already generated child without asking for a new inference budget."""
        with (nullcontext() if self.db.connection.in_transaction else self.db.transaction(immediate=True)):
            run = self.get(project_id, run_id)
            if run is None or run.status not in {"awaiting_talking_dependencies", "render_pending", "render_running", "render_failed", "render_completed_awaiting_review"}:
                raise ProductionRunNotReady(("planned_talking_run_cancelled_or_missing",))
            voice_row = self.db.connection.execute("SELECT * FROM production_runs WHERE id = ?", (str(run_id),)).fetchone()
            self._require_bound_voice_master(voice_row, run.talking_master_audio_id)
            binding = next((item for item in run.talking_source_bindings if item.scene_plan_id == scene_plan_id), None)
            if binding is None or binding.talking_job_id is None or binding.talking_qa_job_id is None or binding.talking_output_asset_id is None:
                raise ProductionRunNotReady(("planned_talking_review_dependencies_missing",))
            jobs = JobRepository(self.db)
            generated = jobs.get(binding.talking_job_id)
            qa_job = jobs.get(binding.talking_qa_job_id)
            if generated is None or generated.status is not JobStatus.COMPLETED or not isinstance(generated.payload, TalkingGenerationJobPayload):
                raise ProductionRunNotReady(("planned_talking_generation_not_completed",))
            context = generated.payload.planned_context
            if context is None or context.run_id != run_id or context.scene_plan_id != scene_plan_id:
                raise ProductionRunNotReady(("planned_talking_context_mismatch",))
            if binding.talking_run_id is None:
                self.require_talking_job_admission(
                    generated, provider=context.expected_provider, model=context.expected_model,
                    runtime=context.expected_runtime, machine_id=context.expected_machine_id,
                    _new_inference=False,
                )
            else:
                # Once admitted, normal planning can route this Run. Re-entering
                # full preflight from its own consumption gate would recurse.
                self._require_consumed_talking_source(project_id, run_id, binding, generated)
            output = self._require_talking_output(project_id, run_id, binding, generated)
            if output.id != binding.talking_output_asset_id or qa_job is None or qa_job.status is not JobStatus.COMPLETED:
                raise ProductionRunNotReady(("planned_talking_qa_not_completed",))
            if qa_job.type is not JobType.VERIFY_TALKING or not isinstance(qa_job.payload, TalkingQaJobPayload):
                raise ProductionRunNotReady(("planned_talking_qa_mismatch",))
            checked_output, master = self.require_talking_qa_job(qa_job)
            if checked_output.id != output.id or voice_human_review_status(master) != "approved":
                raise ProductionRunNotReady(("planned_talking_review_evidence_mismatch",))
            generation = output.metadata.get("talking_generation")
            qa = generation.get("qa") if isinstance(generation, dict) else None
            human = generation.get("human_review") if isinstance(generation, dict) else None
            if (not isinstance(qa, dict) or qa.get("job_id") != str(qa_job.id)
                or qa.get("automated_verified") is not True
                or generation.get("qa_state") != "verified"):
                raise ProductionRunNotReady(("planned_talking_child_review_not_approved",))
            try:
                require_child_judgment(generation, run.talking_review_policy_version)
                if run.talking_review_policy_version == 2:
                    TalkingReviewPolicyService(self.db).require_clear(project_id, output.content_hash, pending=False)
            except TalkingReviewPolicyError as exc:
                raise ProductionRunNotReady((str(exc),)) from exc
            source = AssetRepository(self.db).get(binding.reference_asset_id)
            clip = ClipRepository(self.db).get(binding.reference_clip_id)
            profile = TalkingProfileRepository(self.db).get(binding.talking_profile_id)
            capability = ProviderMachineCapabilityProfileRepository(self.db).get(binding.capability_profile_id)
            if (source is None or clip is None or profile is None or capability is None
                or clip.asset_id != source.id or source.content_hash != binding.reference_asset_hash
                or clip.start_ms != binding.reference_start_ms or clip.end_ms != binding.reference_end_ms
                or not profile.consent.confirmed or not source.authorization_reference.strip()
                or capability.commercial_status != "commercial_safe"
                or capability.license_evidence_reference != binding.license_evidence_reference):
                raise ProductionRunNotReady(("planned_talking_source_or_license_changed",))
            self._require_source_bytes(source.source_file, source.content_hash)
            origin_type = PlannedTalkingRunOriginV2 if run.talking_review_policy_version == 2 else PlannedTalkingRunOrigin
            policy_fields = {"execution_sha256": _snapshot_sha256({
                "capability": capability.model_dump(mode="json", exclude={"updated_at", "last_verified_at"}),
                "payload": generated.payload.model_dump(mode="json"),
            })} if run.talking_review_policy_version == 2 else {}
            origin = origin_type(
                run_id=run_id, scene_plan_id=scene_plan_id,
                binding_sha256=_talking_binding_sha256(binding), plan_fingerprint=run.preflight_fingerprint,
                generation_job_id=generated.id, qa_job_id=qa_job.id,
                qa_report_sha256=_snapshot_sha256(qa),
                output_asset_id=output.id, output_sha256=output.content_hash,
                master_audio_id=master.id, master_sha256=master.content_hash,
                master_review_sha256=_snapshot_sha256(master.metadata["voice_generation"].get("human_review")),
                master_start_ms=binding.master_start_ms, master_end_ms=binding.master_end_ms,
                source_clip_id=clip.id, source_sha256=source.content_hash,
                source_start_ms=clip.start_ms, source_end_ms=clip.end_ms,
                source_admission_id=context.source_admission_id,
                consent_sha256=context.consent_sha256,
                authorization_reference=binding.authorization_reference,
                license_evidence_reference=binding.license_evidence_reference,
                child_review_sha256=None if run.talking_review_policy_version == 2 else _snapshot_sha256(human),
                **policy_fields,
            )
            if expected is not None and origin != expected:
                raise ProductionRunNotReady(("planned_talking_origin_stale",))
            return origin

    def _require_consumed_talking_source(
        self, project_id: UUID, run_id: UUID, binding: TalkingSourceBinding, generated: Job,
    ) -> None:
        """Current-admission checks that never invoke Router or a new-call budget."""
        row = self.db.connection.execute(
            "SELECT draft_version, script_revision FROM production_runs WHERE id = ? AND project_id = ? AND stage IN ('awaiting_talking', 'render')",
            (str(run_id), str(project_id)),
        ).fetchone()
        draft = ProjectDraftRepository(self.db).get(project_id)
        scene = None if draft is None else next((item for item in draft.scenes if item.id == binding.scene_plan_id), None)
        if (row is None or draft is None or draft.version != row["draft_version"]
            or draft.script_revision != row["script_revision"]
            or scene is None):
            raise ProductionRunNotReady(("planned_talking_plan_changed",))
        context = generated.payload.planned_context
        profile = TalkingProfileRepository(self.db).get(binding.talking_profile_id)
        capability = ProviderMachineCapabilityProfileRepository(self.db).get(binding.capability_profile_id)
        if (context is None or context.binding_sha256 != _talking_binding_sha256(binding)
            or context.capability_profile_id != binding.capability_profile_id
            or profile is None or not profile.consent.confirmed
            or context.consent_sha256 != _snapshot_sha256(profile.consent.model_dump(mode="json"))
            or binding.reference_clip_id not in profile.reference_clip_ids
            or capability is None or capability.provider != context.expected_provider
            or capability.model != context.expected_model or capability.runtime != context.expected_runtime
            or capability.machine_id != context.expected_machine_id
            or capability.readiness != "verified" or capability.quality_status != "verified"
            or capability.evidence_reference != binding.capability_evidence_reference
            or capability.commercial_status != "commercial_safe"
            or capability.license_evidence_reference != binding.license_evidence_reference):
            raise ProductionRunNotReady(("planned_talking_execution_authority_changed",))
        receipt = TalkingSourceAdmissionRepository(self.db, self.data_root).get(context.source_admission_id)
        if receipt is None or receipt.assessment_id != binding.suitability_assessment_id:
            raise ProductionRunNotReady(("planned_talking_source_admission_missing",))
        try:
            assessment = TalkingSourceAdmissionRepository(self.db, self.data_root).require_current(receipt)
        except ValueError as exc:
            raise ProductionRunNotReady(("planned_talking_source_admission_stale_or_revoked",)) from exc
        if (assessment.source_asset_id != binding.reference_asset_id
            or assessment.source_clip_id != binding.reference_clip_id
            or assessment.source_content_hash != binding.reference_asset_hash
            or assessment.start_ms != binding.reference_start_ms
            or assessment.end_ms != binding.reference_end_ms
            or assessment.presentation != "source_native_portrait"):
            raise ProductionRunNotReady(("planned_talking_source_admission_mismatch",))
        clip = ClipRepository(self.db).get(binding.reference_clip_id)
        source = AssetRepository(self.db).get(binding.reference_asset_id)
        master = AudioAssetRepository(self.db).get(binding.master_audio_id)
        segments = [] if master is None else [segment for segment in master.transcript_segments
                                               if segment.start_ms >= binding.master_start_ms
                                               and segment.end_ms <= binding.master_end_ms]
        if (clip is None or clip.talking_reference_assessment is None
            or clip.talking_reference_assessment.burned_in_subtitles is not False
            or source is None or source.authorization_reference != binding.source_authorization_reference
            or master is None or voice_human_review_status(master) != "approved"
            or not segments or segments[0].start_ms != binding.master_start_ms
            or segments[-1].end_ms != binding.master_end_ms
            or "".join(comparison_tokens(" ".join(item.text for item in segments)))
               != "".join(comparison_tokens(scene.voice_text))):
            raise ProductionRunNotReady(("planned_talking_source_or_speech_changed",))

    def prepare_talking_run_preview(
        self, project_id: UUID, run_id: UUID, *, scene_plan_id: UUID,
    ) -> ProductionRun | None:
        """Create a one-child evidence collection and local review-preview Job atomically."""
        with self.db.transaction(immediate=True):
            run = self.get(project_id, run_id)
            if run is None:
                return None
            origin = self.require_current_planned_run_origin(project_id, run_id, scene_plan_id)
            binding = next(item for item in run.talking_source_bindings if item.scene_plan_id == scene_plan_id)
            series_repo = TalkingSliceSeriesRepository(self.db)
            jobs = JobRepository(self.db)
            from app.talking.repair import operation_key
            key = operation_key(self.db, run_id, scene_plan_id, origin.generation_job_id, "talking-series")
            existing = series_repo.get_by_idempotency_key(key)
            if existing is None:
                if any(origin.generation_job_id in candidate.child_job_ids for candidate in series_repo.list_for_project(project_id)):
                    raise ProductionRunConflict("planned Talking child already belongs to another series")
                series = TalkingSliceSeries(
                    project_id=project_id, idempotency_key=key,
                    request_fingerprint=_snapshot_sha256(origin.model_dump(mode="json")),
                    narration_audio_id=origin.master_audio_id,
                    child_job_ids=[origin.generation_job_id], planned_origin=origin,
                    created_at=datetime.now(timezone.utc),
                )
                existing = series_repo.create(series)
                if existing.id != series.id:
                    raise ProductionRunConflict("planned Talking series idempotency conflict")
            if existing.project_id != project_id or existing.planned_origin != origin or existing.child_job_ids != [origin.generation_job_id]:
                raise ProductionRunConflict("planned Talking collection has changed")
            payload = TalkingRunPreviewJobPayload(
                project_id=project_id, series_id=existing.id,
                origin_sha256=_snapshot_sha256(origin.model_dump(mode="json")),
            )
            preview_key = operation_key(self.db, run_id, scene_plan_id, origin.generation_job_id, "talking-preview")
            preview = jobs.get_by_idempotency_key(preview_key)
            if preview is None:
                now = datetime.now(timezone.utc)
                candidate = Job(project_id=project_id, type=JobType.PREPARE_TALKING_RUN_PREVIEW,
                                idempotency_key=preview_key, created_at=now, updated_at=now, payload=payload)
                preview = jobs.create(candidate)
                if preview.id != candidate.id:
                    raise ProductionRunConflict("planned Talking preview idempotency conflict")
            if preview.type is not JobType.PREPARE_TALKING_RUN_PREVIEW or preview.payload != payload or preview.status is JobStatus.CANCELLED:
                raise ProductionRunConflict("planned Talking preview Job differs from exact origin")
            if binding.talking_series_id is not None and binding.talking_series_id != existing.id:
                raise ProductionRunConflict("planned Talking collection binding changed")
            if binding.talking_preview_job_id is not None and binding.talking_preview_job_id != preview.id:
                raise ProductionRunConflict("planned Talking preview binding changed")
            if binding.talking_series_id is None or binding.talking_preview_job_id is None:
                updated = binding.model_copy(update={"talking_series_id": existing.id, "talking_preview_job_id": preview.id})
                bindings = tuple(updated if item.scene_plan_id == scene_plan_id else item for item in run.talking_source_bindings)
                self.db.connection.execute(
                    "UPDATE production_runs SET talking_source_bindings = ? WHERE id = ? AND stage = 'awaiting_talking'",
                    (json.dumps([item.model_dump(mode="json") for item in bindings]), str(run_id)),
                )
            self.db.connection.execute(
                "UPDATE talking_repairs SET successor_series_id = ? WHERE replacement_job_id = ? AND (successor_series_id IS NULL OR successor_series_id = ?)",
                (str(existing.id), str(origin.generation_job_id), str(existing.id)),
            )
            return self.get(project_id, run_id)

    def reconcile_completed_talking_job(self, talking_job_id: UUID, *, qa_worker_ready: bool = True) -> ProductionRun | None:
        row = self.db.connection.execute(
            "SELECT id, project_id, talking_source_bindings FROM production_runs WHERE stage = 'awaiting_talking'"
        ).fetchall()
        for candidate in row:
            bindings = tuple(TalkingSourceBinding.model_validate(item) for item in json.loads(candidate["talking_source_bindings"]))
            binding = next((item for item in bindings if item.talking_job_id == talking_job_id and item.talking_qa_job_id is None), None)
            if binding is None:
                continue
            job = JobRepository(self.db).get(talking_job_id)
            if job is None or job.status is not JobStatus.COMPLETED:
                return None
            run_id, project_id = UUID(candidate["id"]), UUID(candidate["project_id"])
            try:
                if not qa_worker_ready:
                    raise ProductionRunNotReady(("talking_qa_worker_not_configured",))
                return self.advance_talking_qa(project_id, run_id, scene_plan_id=binding.scene_plan_id)
            except ProductionRunNotReady as exc:
                reason = exc.reasons[0]
            except (ProductionRunConflict, ValueError):
                reason = "talking_qa_reconciliation_conflict"
            with self.db.transaction(immediate=True):
                current = self.get(project_id, run_id)
                if current is not None and current.status == "awaiting_talking_dependencies":
                    reasons = tuple(dict.fromkeys((*current.waiting_stop_reasons, reason)))
                    self.db.connection.execute(
                        "UPDATE production_runs SET waiting_stop_reasons = ? WHERE id = ? AND stage = 'awaiting_talking'",
                        (json.dumps(reasons), str(run_id)),
                    )
            return self.get(project_id, run_id)
        return None

    def reconcile_completed_talking_jobs_once(self, *, qa_worker_ready: bool = True) -> tuple[ProductionRun, ...]:
        rows = self.db.connection.execute(
            "SELECT talking_source_bindings FROM production_runs WHERE stage = 'awaiting_talking'"
        ).fetchall()
        results = []
        for row in rows:
            for raw in json.loads(row["talking_source_bindings"]):
                binding = TalkingSourceBinding.model_validate(raw)
                if binding.talking_job_id is not None and binding.talking_qa_job_id is None:
                    result = self.reconcile_completed_talking_job(binding.talking_job_id, qa_worker_ready=qa_worker_ready)
                    if result is not None:
                        results.append(result)
        return tuple(results)

    def apply_talking_suitability(
        self, project_id: UUID, run_id: UUID, *, scene_plan_id: UUID, assessment_id: UUID,
    ) -> ProductionRun | None:
        """Consume exact review evidence on a bound candidate, without dispatch."""
        run = self.get(project_id, run_id)
        if run is None:
            return None
        if run.status != "awaiting_talking_dependencies":
            raise ProductionRunConflict("production run is not awaiting Talking dependencies")
        binding = next((item for item in run.talking_source_bindings if item.scene_plan_id == scene_plan_id), None)
        if binding is None:
            raise ProductionRunNotReady(("talking_reference_candidate_not_bound",))
        # Reuse the V74c2 gates before consuming evidence; duplicate requests do not bypass them.
        self.bind_talking_source(
            project_id, run_id, scene_plan_id=scene_plan_id, talking_profile_id=binding.talking_profile_id,
            reference_clip_id=binding.reference_clip_id, capability_profile_id=binding.capability_profile_id,
            master_start_ms=binding.master_start_ms, master_end_ms=binding.master_end_ms,
            authorization_reference=binding.authorization_reference, brief=binding.performance_brief,
        )
        with self.db.transaction(immediate=True):
            row = self.db.connection.execute(
                "SELECT * FROM production_runs WHERE id = ? AND project_id = ?", (str(run_id), str(project_id)),
            ).fetchone()
            if row is None or row["stage"] != "awaiting_talking":
                raise ProductionRunConflict("Talking waiting run changed")
            current = _run_from_row(self.db, row)
            current_binding = next((item for item in current.talking_source_bindings if item.scene_plan_id == scene_plan_id), None)
            if current_binding != binding:
                raise ProductionRunConflict("Talking source binding changed")
            repository = TalkingSourceSuitabilityRepository(self.db, self.data_root)
            assessment = repository.get(assessment_id)
            if assessment is None:
                raise ProductionRunNotReady(("talking_suitability_assessment_missing",))
            if (assessment.source_asset_id != binding.reference_asset_id
                or assessment.source_clip_id != binding.reference_clip_id
                or assessment.source_content_hash != binding.reference_asset_hash
                or assessment.start_ms != binding.reference_start_ms
                or assessment.end_ms != binding.reference_end_ms):
                raise ProductionRunConflict("Talking suitability evidence does not match the bound source")
            repository.require_current_source(assessment)
            if current_binding.suitability_assessment_id is not None:
                if current_binding.suitability_assessment_id != assessment.id:
                    raise ProductionRunConflict("Talking suitability assessment differs from persisted decision")
                return current
            decision = assessment.decision
            updated_binding = current_binding.model_copy(update={
                "suitability": decision if decision != "unknown" else "full_interval_unknown",
                "suitability_assessment_id": assessment.id,
            })
            bindings = tuple(updated_binding if item.scene_plan_id == scene_plan_id else item
                             for item in current.talking_source_bindings)
            prior = tuple(reason for reason in current.waiting_stop_reasons
                          if reason != f"talking_full_interval_suitability_unverified:{next(item.scene_id for item in current.talking_dependencies if item.scene_plan_id == scene_plan_id)}")
            if decision == "review_claimed_suitable":
                next_reason = "talking_dispatch_not_implemented"
            elif decision == "unusable":
                next_reason = f"talking_reference_unusable:{next(item.scene_id for item in current.talking_dependencies if item.scene_plan_id == scene_plan_id)}"
            else:
                next_reason = f"talking_full_interval_suitability_unverified:{next(item.scene_id for item in current.talking_dependencies if item.scene_plan_id == scene_plan_id)}"
            reasons = tuple(dict.fromkeys((*prior, next_reason)))
            self.db.connection.execute(
                "UPDATE production_runs SET talking_source_bindings = ?, waiting_stop_reasons = ? WHERE id = ? AND stage = 'awaiting_talking'",
                (json.dumps([item.model_dump(mode="json") for item in bindings]), json.dumps(reasons), str(run_id)),
            )
            return self.get(project_id, run_id)

    def cancel(self, project_id: UUID, run_id: UUID) -> ProductionRun | None:
        with self.db.transaction(immediate=True):
            row = self.db.connection.execute(
                "SELECT * FROM production_runs WHERE id = ? AND project_id = ?", (str(run_id), str(project_id)),
            ).fetchone()
            if row is None:
                return None
            if row["stage"] == "awaiting_talking":
                run = _run_from_row(self.db, row)
                jobs = JobRepository(self.db)
                for binding in run.talking_source_bindings:
                    if binding.talking_preview_job_id is not None:
                        preview = jobs.get(binding.talking_preview_job_id)
                        if preview is None:
                            raise ProductionRunConflict("planned Talking preview Job is missing")
                        if preview.status is JobStatus.RUNNING:
                            raise ProductionRunConflict("running Talking preview Job cannot be cancelled")
                        if preview.status is JobStatus.PENDING:
                            jobs.update(preview.model_copy(update={
                                "status": JobStatus.CANCELLED, "updated_at": datetime.now(timezone.utc),
                            }))
                    if binding.talking_qa_job_id is not None:
                        qa = jobs.get(binding.talking_qa_job_id)
                        if qa is None:
                            raise ProductionRunConflict("planned Talking QA Job is missing")
                        if qa.status is JobStatus.RUNNING:
                            raise ProductionRunConflict("running Talking QA Job cannot be cancelled")
                        if qa.status is JobStatus.PENDING:
                            jobs.update(qa.model_copy(update={
                                "status": JobStatus.CANCELLED, "updated_at": datetime.now(timezone.utc),
                            }))
                    if binding.talking_job_id is None:
                        continue
                    job = jobs.get(binding.talking_job_id)
                    if job is None:
                        raise ProductionRunConflict("planned Talking Job is missing")
                    if job.status is JobStatus.RUNNING:
                        raise ProductionRunConflict("running Talking Job cannot be cancelled")
                    if job.status is JobStatus.PENDING:
                        jobs.update(job.model_copy(update={
                            "status": JobStatus.CANCELLED, "updated_at": datetime.now(timezone.utc),
                        }))
                self.db.connection.execute("UPDATE production_runs SET stage = 'cancelled' WHERE id = ?", (str(run_id),))
                return self.get(project_id, run_id)
            if row["stage"] == "awaiting_master":
                self.db.connection.execute("UPDATE production_runs SET stage = 'cancelled' WHERE id = ?", (str(run_id),))
                return self.get(project_id, run_id)
            if row["stage"] == "voice":
                voice_job = JobRepository(self.db).get(UUID(row["voice_job_id"]))
                if voice_job is None:
                    raise ProductionRunConflict("production run Voice Job is missing")
                if voice_job.status is JobStatus.RUNNING:
                    raise ProductionRunConflict("running Voice Job cannot be cancelled")
                if row["voice_qa_job_id"] is not None:
                    qa_job = JobRepository(self.db).get(UUID(row["voice_qa_job_id"]))
                    if qa_job is None:
                        raise ProductionRunConflict("production run Voice QA Job is missing")
                    if qa_job.status is JobStatus.RUNNING:
                        raise ProductionRunConflict("running Voice QA Job cannot be cancelled")
                    if qa_job.status is JobStatus.PENDING:
                        JobRepository(self.db).update(qa_job.model_copy(update={
                            "status": JobStatus.CANCELLED, "updated_at": datetime.now(timezone.utc),
                        }))
                if voice_job.status is JobStatus.PENDING:
                    JobRepository(self.db).update(voice_job.model_copy(update={
                        "status": JobStatus.CANCELLED, "updated_at": datetime.now(timezone.utc),
                    }))
                self.db.connection.execute("UPDATE production_runs SET stage = 'cancelled' WHERE id = ?", (str(run_id),))
                return self.get(project_id, run_id)
            run = _run_from_row(self.db, row)
            if run.status == "cancelled":
                return run
            if run.render_job_id is None:
                raise ProductionRunConflict("production run has no render Job")
            job_repo = JobRepository(self.db)
            job = job_repo.get(run.render_job_id)
            if job is None:
                raise ProductionRunConflict("production run render Job is missing")
            if job.status is JobStatus.CANCELLED:
                return run
            if job.status is not JobStatus.PENDING:
                raise ProductionRunConflict("only a pending production render can be cancelled")
            job_repo.update(job.model_copy(update={"status": JobStatus.CANCELLED, "updated_at": datetime.now(timezone.utc)}))
            return self.get(project_id, run_id)

    def resume(self, project_id: UUID, run_id: UUID) -> ProductionRun | None:
        """After U-Voice admission, recheck the saved draft and dispatch once."""
        with self.db.transaction(immediate=True):
            row = self.db.connection.execute(
                "SELECT * FROM production_runs WHERE id = ? AND project_id = ?", (str(run_id), str(project_id)),
            ).fetchone()
            if row is None:
                return None
            if row["stage"] == "cancelled":
                raise ProductionRunConflict("cancelled production run cannot resume")
            if row["stage"] == "awaiting_talking" or (row["stage"] == "render" and json.loads(row["talking_dependencies"])):
                return self._resume_planned_talking(row, project_id, run_id)
            if row["stage"] == "render":
                return _run_from_row(self.db, row)
            if row["stage"] == "voice":
                voice_job = JobRepository(self.db).get(UUID(row["voice_job_id"]))
                if voice_job is None or voice_job.status is not JobStatus.COMPLETED:
                    raise ProductionRunNotReady(("voice_job_not_completed",))
                if row["voice_qa_job_id"] is not None:
                    qa_job = JobRepository(self.db).get(UUID(row["voice_qa_job_id"]))
                    if qa_job is None or qa_job.status is not JobStatus.COMPLETED:
                        raise ProductionRunNotReady(("voice_qa_not_completed",))
            project = ProjectRepository(self.db).get(project_id)
            draft = ProjectDraftRepository(self.db).get(project_id)
            if project is None or draft is None:
                raise ProductionRunConflict("project draft is unavailable")
            if draft.version != row["draft_version"] or draft.script_revision != row["script_revision"]:
                raise ProductionRunConflict("production plan draft changed; replan before resume")
            style = VisualStyleTokens.model_validate(json.loads(row["style_tokens"]))
            preflight = build_production_preflight(
                self.db, project, draft, style_tokens=style, repair_allowance=row["repair_allowance"],
            )
            if json.loads(row["talking_dependencies"]):
                return self._advance_master_to_talking(row, preflight)
            self._require_ready_lane(preflight)
            self._require_master_bytes(preflight.reusable_master_audio_id)
            if row["stage"] == "voice":
                audio = AudioAssetRepository(self.db).get(preflight.reusable_master_audio_id)
                generation = None if audio is None else audio.metadata.get("voice_generation")
                if not isinstance(generation, dict) or generation.get("job_id") != row["voice_job_id"]:
                    raise ProductionRunNotReady(("approved_master_not_from_run_voice_job",))
                if row["voice_audio_id"] is not None and str(audio.id) != row["voice_audio_id"]:
                    raise ProductionRunNotReady(("approved_master_not_bound_voice_output",))
            duplicate = self.db.connection.execute(
                "SELECT id FROM production_runs WHERE project_id = ? AND preflight_fingerprint = ? AND id != ?",
                (str(project_id), preflight.fingerprint, str(run_id)),
            ).fetchone()
            if duplicate is not None:
                raise ProductionRunConflict("fresh production plan already belongs to another run")
            job_id = self._enqueue_render(project, draft, preflight, run_id, datetime.now(timezone.utc))
            self.db.connection.execute(
                """UPDATE production_runs SET stage = 'render', preflight_fingerprint = ?, render_job_id = ?
                   WHERE id = ? AND stage IN ('awaiting_master', 'voice')""",
                (preflight.fingerprint, str(job_id), str(run_id)),
            )
            return self.get(project_id, run_id)

    def _require_bound_voice_master(self, row, master_id: UUID | None) -> None:
        """New Voice-backed Talking never substitutes another approved take."""
        if row["voice_job_id"] is None:
            return  # Existing reusable-Master lane.
        job = JobRepository(self.db).get(UUID(row["voice_job_id"]))
        qa = JobRepository(self.db).get(UUID(row["voice_qa_job_id"])) if row["voice_qa_job_id"] else None
        audio = AudioAssetRepository(self.db).get(master_id) if master_id else None
        generation = None if audio is None else audio.metadata.get("voice_generation")
        profile = VoiceProfileRepository(self.db).get(job.payload.voice_profile_id) if job is not None and isinstance(job.payload, VoiceGenerationJobPayload) else None
        capability = ProviderMachineCapabilityProfileRepository(self.db).get(job.payload.execution_capability_profile_id) if job is not None and isinstance(job.payload, VoiceGenerationJobPayload) and job.payload.execution_capability_profile_id else None
        if (job is None or job.type is not JobType.GENERATE_VOICE or job.status is not JobStatus.COMPLETED
            or not isinstance(job.payload, VoiceGenerationJobPayload)
            or job.project_id != UUID(row["project_id"])
            or row["voice_audio_id"] is None or str(master_id) != row["voice_audio_id"]
            or qa is None or qa.type is not JobType.VERIFY_VOICE or qa.status is not JobStatus.COMPLETED
            or qa.project_id != job.project_id or not isinstance(qa.payload, VoiceQaJobPayload)
            or qa.payload.narration_audio_id != master_id or qa.payload.target_text != job.payload.text
            or not isinstance(generation, dict) or generation.get("job_id") != str(job.id)
            or generation.get("project_id") != row["project_id"]
            or generation.get("target_text") != job.payload.text
            or generation.get("provider") != job.payload.expected_provider or generation.get("model") != job.payload.expected_model
            or audio.authorization_reference != job.payload.authorization_reference
            or profile is None or not profile.consent.confirmed
            or capability is None or capability.capability != "voice"
            or capability.provider != job.payload.expected_provider or capability.model != job.payload.expected_model
            or capability.readiness != "verified" or capability.quality_status != "verified"
            or not capability.evidence_reference or capability.commercial_status != "commercial_safe"
            or not capability.license_evidence_reference
            or generation.get("qa_state") != "verified" or voice_human_review_status(audio) != "approved"):
            raise ProductionRunNotReady(("bound_voice_master_not_approved_or_qa_complete",))
        self._require_master_bytes(master_id)

    def _advance_master_to_talking(self, row, preflight: ProductionPreflight) -> ProductionRun:
        """No inference: atomically resolve the same plan's missing Talking lane."""
        if self._start_stage(preflight) != "awaiting_talking" or preflight.reusable_master_audio_id is None:
            raise ProductionRunNotReady(("planned_talking_master_or_dependencies_not_ready",))
        self._require_bound_voice_master(row, preflight.reusable_master_audio_id)
        self._require_master_bytes(preflight.reusable_master_audio_id)
        prior = tuple(TalkingPlanDependency.model_validate(item) for item in json.loads(row["talking_dependencies"]))
        dependencies = tuple(TalkingPlanDependency(
            scene_plan_id=item.scene_plan_id, scene_id=item.scene_id,
            visual_dependency_fingerprint=item.visual_dependency_fingerprint,
        ) for item in preflight.scenes if item.production_need == "new_talking")
        if {(item.scene_plan_id, item.scene_id) for item in prior} != {(item.scene_plan_id, item.scene_id) for item in dependencies}:
            raise ProductionRunConflict("planned Talking requirements changed; replan")
        from app.assembly.video_spec import _master_narration_intervals, NarrationTimelineError
        draft = ProjectDraftRepository(self.db).get(UUID(row["project_id"]))
        master = AudioAssetRepository(self.db).get(preflight.reusable_master_audio_id)
        try:
            _master_narration_intervals(master, sorted(draft.scenes, key=lambda item: item.order))
        except NarrationTimelineError as exc:
            raise ProductionRunNotReady(("master_scene_timing_unresolved",)) from exc
        self.db.connection.execute(
            """UPDATE production_runs SET stage = 'awaiting_talking', talking_master_audio_id = ?,
               talking_dependencies = ?, waiting_stop_reasons = ? WHERE id = ? AND stage IN ('voice', 'awaiting_master')""",
            (str(master.id), json.dumps([item.model_dump(mode="json") for item in dependencies]),
             json.dumps(preflight.stop_reasons), row["id"]),
        )
        return self.get(UUID(row["project_id"]), UUID(row["id"]))

    def _resume_planned_talking(self, row, project_id: UUID, run_id: UUID) -> ProductionRun:
        """Bound admitted visuals only; reuse the origin fingerprint across render."""
        from app.db import TalkingRunRepository
        from app.talking.admission import talking_visual_blocker

        run = _run_from_row(self.db, row)
        self._require_bound_voice_master(row, run.talking_master_audio_id)
        project = ProjectRepository(self.db).get(project_id)
        draft = ProjectDraftRepository(self.db).get(project_id)
        if project is None or draft is None or draft.version != row["draft_version"] or draft.script_revision != row["script_revision"]:
            raise ProductionRunConflict("production plan draft changed; replan before resume")
        required = {item.scene_plan_id for item in run.talking_dependencies}
        bindings = {item.scene_plan_id: item for item in run.talking_source_bindings}
        if set(bindings) != required or any(item.talking_run_id is None for item in bindings.values()):
            raise ProductionRunNotReady(("talking_dependencies_not_admitted",))
        clips: dict[UUID, UUID] = {}
        for scene_id, binding in bindings.items():
            admitted = TalkingRunRepository(self.db).get(binding.talking_run_id)
            scene = next((item for item in draft.scenes if item.id == scene_id), None)
            asset = None if admitted is None else AssetRepository(self.db).get(admitted.assembled_asset_id)
            clip = None if admitted is None else ClipRepository(self.db).get(admitted.assembled_clip_id)
            if (admitted is None or admitted.series_id != binding.talking_series_id
                or admitted.master_narration_audio_id != run.talking_master_audio_id
                or admitted.master_start_ms != binding.master_start_ms or admitted.master_end_ms != binding.master_end_ms
                or scene is None or asset is None or clip is None or asset.width >= asset.height
                or talking_visual_blocker(asset, clip, AssetRepository(self.db), project_id=project_id,
                                          copy=scene.voice_text, master_audio_id=run.talking_master_audio_id) is not None):
                raise ProductionRunNotReady(("planned_talking_render_dependency_invalid",))
            if clip.end_ms - clip.start_ms < binding.master_end_ms - binding.master_start_ms:
                raise ProductionRunNotReady(("planned_talking_visual_timing_insufficient",))
            clips[scene_id] = clip.id
        preflight = build_production_preflight(
            self.db, project, draft, style_tokens=VisualStyleTokens.model_validate(json.loads(row["style_tokens"])),
            repair_allowance=row["repair_allowance"], bound_talking_clips=clips,
        )
        self._require_ready_lane(preflight, allow_planned=True)
        if preflight.reusable_master_audio_id != run.talking_master_audio_id:
            raise ProductionRunNotReady(("planned_talking_master_changed",))
        self._require_master_bytes(run.talking_master_audio_id)
        spec = self._resolved_render_spec(project, draft, preflight)
        # The generic assembler may offer a typography tail for short footage.
        # Planned Talking must cover its speech with the admitted visual instead.
        for scene_id, clip_id in clips.items():
            binding = bindings[scene_id]
            fragments = [item for item in spec.scenes if item.narration_start_ms is not None
                         and binding.master_start_ms <= item.narration_start_ms < binding.master_end_ms]
            if len(fragments) != 1 or fragments[0].visual.clip_id != clip_id:
                raise ProductionRunNotReady(("planned_talking_visual_timing_insufficient",))
        if row["stage"] == "render":
            job = JobRepository(self.db).get(run.render_job_id)
            if job is None or not isinstance(job.payload, RenderVideoJobPayload) or job.payload.video_spec != spec:
                raise ProductionRunNotReady(("planned_render_snapshot_changed",))
            return run
        job_id = self._enqueue_render(project, draft, preflight, run_id, datetime.now(timezone.utc))
        self.db.connection.execute(
            "UPDATE production_runs SET stage = 'render', render_job_id = ?, waiting_stop_reasons = '[]' WHERE id = ? AND stage = 'awaiting_talking'",
            (str(job_id), str(run_id)),
        )
        return self.get(project_id, run_id)

    def advance_voice_qa(self, project_id: UUID, run_id: UUID) -> ProductionRun | None:
        """Find the one generated take and schedule its existing independent QA."""
        with self.db.transaction(immediate=True):
            row = self.db.connection.execute(
                "SELECT * FROM production_runs WHERE id = ? AND project_id = ?", (str(run_id), str(project_id)),
            ).fetchone()
            if row is None:
                return None
            if row["stage"] != "voice":
                raise ProductionRunConflict("production run has no active Voice dependency")
            if row["voice_qa_job_id"] is not None:
                return _run_from_row(self.db, row)
            voice_job = JobRepository(self.db).get(UUID(row["voice_job_id"]))
            if voice_job is None or voice_job.status is not JobStatus.COMPLETED or not isinstance(voice_job.payload, VoiceGenerationJobPayload):
                raise ProductionRunNotReady(("voice_job_not_completed",))
            project = ProjectRepository(self.db).get(project_id)
            draft = ProjectDraftRepository(self.db).get(project_id)
            if project is None or draft is None or draft.version != row["draft_version"] or draft.script_revision != row["script_revision"]:
                raise ProductionRunConflict("production plan draft changed; replan before Voice QA")
            preflight = build_production_preflight(
                self.db, project, draft,
                style_tokens=VisualStyleTokens.model_validate(json.loads(row["style_tokens"])),
                repair_allowance=row["repair_allowance"],
            )
            if self._start_stage(preflight) != "awaiting_master":
                raise ProductionRunNotReady(("approved_master_already_available",))
            matches = []
            for audio in AudioAssetRepository(self.db).list():
                generation = audio.metadata.get("voice_generation")
                if isinstance(generation, dict) and generation.get("job_id") == row["voice_job_id"]:
                    matches.append(audio)
            if len(matches) != 1:
                raise ProductionRunNotReady(("voice_output_missing_or_ambiguous",))
            audio = matches[0]
            generation = audio.metadata["voice_generation"]
            if (generation.get("project_id") != str(project_id)
                or generation.get("target_text") != voice_job.payload.text
                or generation.get("provider") != voice_job.payload.expected_provider
                or generation.get("model") != voice_job.payload.expected_model
                or audio.authorization_reference != voice_job.payload.authorization_reference
                or generation.get("qa_state") != "pending"):
                raise ProductionRunNotReady(("voice_output_provenance_mismatch",))
            self._require_master_bytes(audio.id)
            now, qa_job_id = datetime.now(timezone.utc), uuid4()
            qa_payload = VoiceQaJobPayload(
                project_id=project_id, narration_audio_id=audio.id, target_text=voice_job.payload.text,
            )
            repair = self.db.connection.execute("SELECT id FROM voice_repairs WHERE replacement_job_id = ?", (str(voice_job.id),)).fetchone()
            qa_key = f"production-run:{run_id}:voice-qa" if repair is None else f"production-run:{run_id}:voice-qa:repair:{repair['id']}"
            qa_job = Job(
                id=qa_job_id, project_id=project_id, type=JobType.VERIFY_VOICE,
                idempotency_key=qa_key, created_at=now, updated_at=now,
                payload=qa_payload,
            )
            persisted = JobRepository(self.db).create(qa_job)
            if persisted.id != qa_job_id or persisted.payload != qa_payload:
                raise ProductionRunConflict("Voice QA idempotency key belongs to another payload")
            if repair is not None:
                self.db.connection.execute("UPDATE voice_repairs SET qa_job_id = ? WHERE id = ? AND qa_job_id IS NULL", (str(qa_job_id), repair["id"]))
            self.db.connection.execute(
                """UPDATE production_runs SET voice_audio_id = ?, voice_qa_job_id = ?, voice_qa_auto_stop_reasons = '[]'
                   WHERE id = ? AND stage = 'voice'""",
                (str(audio.id), str(qa_job_id), str(run_id)),
            )
            return self.get(project_id, run_id)

    def reconcile_completed_voice_job(self, voice_job_id: UUID, *, qa_worker_ready: bool = True) -> ProductionRun | None:
        """One idempotent post-completion attempt; blocked work waits for explicit retry."""
        row = self.db.connection.execute(
            "SELECT id, project_id FROM production_runs WHERE voice_job_id = ? AND stage = 'voice' AND voice_qa_job_id IS NULL",
            (str(voice_job_id),),
        ).fetchone()
        if row is None:
            return None
        job = JobRepository(self.db).get(voice_job_id)
        if job is None or job.status is not JobStatus.COMPLETED:
            return None
        project_id, run_id = UUID(row["project_id"]), UUID(row["id"])
        if not qa_worker_ready:
            reasons = ("voice_qa_worker_not_configured",)
        else:
            try:
                return self.advance_voice_qa(project_id, run_id)
            except ProductionRunNotReady as exc:
                reasons = exc.reasons
            except (ProductionRunConflict, PreflightInputError, ValueError):
                reasons = ("voice_qa_reconciliation_conflict",)
        with self.db.transaction(immediate=True):
            self.db.connection.execute(
                """UPDATE production_runs SET voice_qa_auto_stop_reasons = ?
                   WHERE id = ? AND stage = 'voice' AND voice_qa_job_id IS NULL""",
                (json.dumps(reasons), str(run_id)),
            )
            return self.get(project_id, run_id)

    def reconcile_completed_voice_jobs_once(self, *, qa_worker_ready: bool = True) -> tuple[ProductionRun, ...]:
        """Recover only completed dependencies at Worker startup, not a poll loop."""
        rows = self.db.connection.execute(
            "SELECT voice_job_id FROM production_runs WHERE stage = 'voice' AND voice_job_id IS NOT NULL AND voice_qa_job_id IS NULL"
        ).fetchall()
        reconciled = []
        for row in rows:
            result = self.reconcile_completed_voice_job(UUID(row["voice_job_id"]), qa_worker_ready=qa_worker_ready)
            if result is not None:
                reconciled.append(result)
        return tuple(reconciled)

    def dispatch_voice(
        self, project_id: UUID, run_id: UUID, *, voice_profile_id: UUID,
        capability_profile_id: UUID, authorization_reference: str,
    ) -> ProductionRun | None:
        """Link one authorized existing Voice Job; never invoke a provider here."""
        if not authorization_reference.strip():
            raise ProductionRunNotReady(("voice_authorization_reference_required",))
        with self.db.transaction(immediate=True):
            row = self.db.connection.execute(
                "SELECT * FROM production_runs WHERE id = ? AND project_id = ?", (str(run_id), str(project_id)),
            ).fetchone()
            if row is None:
                return None
            if row["stage"] == "voice":
                job = JobRepository(self.db).get(UUID(row["voice_job_id"]))
                if job is None or not isinstance(job.payload, VoiceGenerationJobPayload):
                    raise ProductionRunConflict("production run Voice Job is missing")
                selected = ProviderMachineCapabilityProfileRepository(self.db).get(capability_profile_id)
                if (job.payload.voice_profile_id != voice_profile_id
                    or job.payload.authorization_reference != authorization_reference
                    or job.payload.execution_capability_profile_id != capability_profile_id
                    or selected is None or selected.provider != job.payload.expected_provider
                    or selected.model != job.payload.expected_model):
                    raise ProductionRunConflict("Voice dispatch inputs differ from the persisted dependency")
                return _run_from_row(self.db, row)
            if row["stage"] != "awaiting_master":
                raise ProductionRunConflict("production run is not awaiting Voice dispatch")
            project = ProjectRepository(self.db).get(project_id)
            draft = ProjectDraftRepository(self.db).get(project_id)
            if project is None or draft is None or draft.version != row["draft_version"] or draft.script_revision != row["script_revision"]:
                raise ProductionRunConflict("production plan draft changed; replan before Voice dispatch")
            preflight = build_production_preflight(
                self.db, project, draft,
                style_tokens=VisualStyleTokens.model_validate(json.loads(row["style_tokens"])),
                repair_allowance=row["repair_allowance"],
            )
            if self._start_stage(preflight) != "awaiting_master":
                raise ProductionRunNotReady(("approved_master_already_available",))
            if any(reason in preflight.stop_reasons for reason in (
                "budget_call_limit", "budget_amount_limit", "unknown_cost_requires_budget_authorization",
            )):
                raise ProductionRunNotReady(("voice_budget_not_authorized",))
            if draft.narration_performance_plan is not None:
                raise ProductionRunNotReady(("voice_performance_plan_dispatch_not_implemented",))
            profile = VoiceProfileRepository(self.db).get(voice_profile_id)
            capability = ProviderMachineCapabilityProfileRepository(self.db).get(capability_profile_id)
            if profile is None or not profile.consent.confirmed:
                raise ProductionRunNotReady(("voice_profile_consent_required",))
            if any(ClipRepository(self.db).get(clip_id) is None for clip_id in profile.reference_clip_ids):
                raise ProductionRunNotReady(("voice_reference_clip_missing",))
            if (capability is None or capability.capability != "voice" or capability.provider != profile.provider
                or capability.readiness != "verified" or capability.quality_status != "verified"
                or not capability.evidence_reference):
                raise ProductionRunNotReady(("exact_voice_capability_not_verified",))
            if capability.commercial_status != "commercial_safe" or not capability.license_evidence_reference:
                raise ProductionRunNotReady(("voice_provider_license_scope_unverified",))
            job_id, now = uuid4(), datetime.now(timezone.utc)
            payload = VoiceGenerationJobPayload(
                project_id=project_id, voice_profile_id=voice_profile_id,
                text=draft.script or "\n\n".join(scene.voice_text for scene in sorted(draft.scenes, key=lambda scene: scene.order)),
                authorization_reference=authorization_reference,
                expected_provider=capability.provider, expected_model=capability.model,
                execution_capability_profile_id=capability.id,
            )
            job = Job(
                id=job_id, project_id=project_id, type=JobType.GENERATE_VOICE,
                idempotency_key=f"production-run:{run_id}:voice", created_at=now, updated_at=now, payload=payload,
            )
            persisted = JobRepository(self.db).create(job)
            if persisted.id != job_id or persisted.payload != payload:
                raise ProductionRunConflict("Voice dispatch idempotency key belongs to another payload")
            from app.voice_repair import execution_snapshot
            self.db.connection.execute("INSERT INTO voice_dispatch_snapshots(job_id,payload) VALUES (?,?)", (str(job_id), json.dumps(execution_snapshot(self.db, payload))))
            self.db.connection.execute(
                "UPDATE production_runs SET stage = 'voice', voice_job_id = ? WHERE id = ? AND stage = 'awaiting_master'",
                (str(job_id), str(run_id)),
            )
            return self.get(project_id, run_id)

    @staticmethod
    def _start_stage(preflight: ProductionPreflight) -> Literal["render", "awaiting_master", "awaiting_talking"]:
        if preflight.status == "ready_for_authorized_dispatch":
            ProductionRunService._require_ready_lane(preflight)
            return "render"
        voice_only_stops = {"provider_license_scope_unverified", "voice_capability_not_verified"}
        if (
            preflight.reusable_master_audio_id is None
            and preflight.preliminary_edit_plan is not None
            and preflight.stop_reasons
            and set(preflight.stop_reasons).issubset(voice_only_stops)
            and all(item.planned_source_kind is SourceKind.TYPOGRAPHY and item.suitability == "known_deterministic" for item in preflight.scenes)
            and {action.kind for action in preflight.actions if action.required} == {"voice", "render"}
        ):
            return "awaiting_master"
        talking = tuple(item for item in preflight.scenes if item.production_need == "new_talking")
        allowed_stops = {
            "provider_license_scope_unverified", "talking_capability_not_verified",
            "unknown_cost_requires_budget_authorization", "budget_call_limit", "budget_amount_limit",
            *(f"talking_source_not_admitted:{item.scene_id}" for item in talking),
        }
        if (
            talking
            and all(item.planned_source_kind is SourceKind.AI_VIDEO and item.selected_candidate is None
                    and item.suitability == "unknown" for item in talking)
            and all(item.production_need == "new_talking" or
                    (item.production_need == "none" and item.planned_source_kind is SourceKind.TYPOGRAPHY
                     and item.suitability == "known_deterministic") for item in preflight.scenes)
            and set(preflight.stop_reasons).issubset(allowed_stops | {"voice_capability_not_verified"})
            and all(f"talking_source_not_admitted:{item.scene_id}" in preflight.stop_reasons for item in talking)
            and {action.kind for action in preflight.actions if action.required} == (
                {"talking", "render"} if preflight.reusable_master_audio_id is not None else {"voice", "talking", "render"}
            )
        ):
            return "awaiting_talking" if preflight.reusable_master_audio_id is not None else "awaiting_master"
        raise ProductionRunNotReady(preflight.stop_reasons or ("production_dependencies_not_implemented",))

    def _enqueue_render(self, project, draft, preflight: ProductionPreflight, run_id: UUID, now: datetime) -> UUID:
        spec = self._resolved_render_spec(project, draft, preflight)
        job_id = uuid4()
        job = Job(
            id=job_id, project_id=project.id, type=JobType.RENDER,
            idempotency_key=f"production-run:{run_id}:render", created_at=now, updated_at=now,
            payload=RenderVideoJobPayload(project_id=project.id, render_id=uuid4(), video_spec=spec),
        )
        persisted = JobRepository(self.db).create(job)
        if persisted.id != job_id or persisted.payload != job.payload:
            raise ProductionRunConflict("render idempotency key belongs to another payload")
        from app.source_use_constraints import SourceUseConstraintService
        SourceUseConstraintService(self.db, self.data_root).snapshot_render(project.id, job_id)
        return job_id

    def _resolved_render_spec(self, project, draft, preflight: ProductionPreflight):
        assert preflight.preliminary_edit_plan is not None
        selections = {item.scene_plan_id: item.selected_candidate for item in preflight.scenes}
        if any(value is None for value in selections.values()):
            raise ProductionRunNotReady(("visual_selection_incomplete",))
        try:
            spec = VideoSpecAssembler(
                AssetRepository(self.db), ClipRepository(self.db), ImageAssetRepository(self.db),
                AudioAssetRepository(self.db),
            ).assemble(
                project, draft.scenes, selections,
                master_narration_asset_id=preflight.reusable_master_audio_id,
                narration_required=True, edit_plan=preflight.preliminary_edit_plan,
            )
        except VideoSpecAssemblyError as exc:
            raise ProductionRunNotReady((f"video_spec_assembly_failed:{exc}",)) from exc
        return spec

    @staticmethod
    def _require_ready_lane(preflight: ProductionPreflight, *, allow_planned: bool = False) -> None:
        if preflight.status != "ready_for_authorized_dispatch":
            raise ProductionRunNotReady(preflight.stop_reasons or ("preflight_blocked",))
        if preflight.reusable_master_audio_id is None:
            raise ProductionRunNotReady(("approved_master_narration_required",))
        if preflight.preliminary_edit_plan is None:
            raise ProductionRunNotReady(("preliminary_edit_plan_required",))
        if any(not (item.planned_source_kind is SourceKind.TYPOGRAPHY and item.suitability == "known_deterministic")
               and not (allow_planned and item.planned_source_kind is SourceKind.AI_VIDEO and item.suitability == "reviewed_native_planned_run")
               for item in preflight.scenes):
            raise ProductionRunNotReady(("only_suitable_typography_lane_is_implemented",))
        if any(action.required and action.kind != "render" for action in preflight.actions):
            raise ProductionRunNotReady(("production_dependencies_not_implemented",))

    def _require_master_bytes(self, audio_id: UUID | None) -> None:
        if audio_id is None:
            raise ProductionRunNotReady(("approved_master_narration_required",))
        audio = AudioAssetRepository(self.db).get(audio_id)
        if audio is None:
            raise ProductionRunNotReady(("approved_master_narration_missing",))
        value = Path(audio.source_file)
        if value.is_absolute():
            source = value.resolve()  # Legacy imported absolute paths.
        else:
            parts = value.parts
            source = (self.data_root.parent / value if parts and parts[0].casefold() == self.data_root.name.casefold()
                      else self.data_root / value).resolve()
            if not source.is_relative_to(self.data_root):
                raise ProductionRunNotReady(("master_narration_path_escapes_data_root",))
        if not source.is_file():
            raise ProductionRunNotReady(("master_narration_file_missing",))
        with source.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != audio.content_hash:
            raise ProductionRunNotReady(("master_narration_bytes_changed",))

    def _require_source_bytes(self, source_file: str, expected_hash: str) -> None:
        value = Path(source_file)
        if value.is_absolute():
            source = value.resolve()  # Legacy imported absolute paths.
        else:
            parts = value.parts
            source = (self.data_root.parent / value if parts and parts[0].casefold() == self.data_root.name.casefold()
                      else self.data_root / value).resolve()
            if not source.is_relative_to(self.data_root):
                raise ProductionRunNotReady(("talking_reference_path_escapes_data_root",))
        if not source.is_file():
            raise ProductionRunNotReady(("talking_reference_file_missing",))
        with source.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != expected_hash:
            raise ProductionRunNotReady(("talking_reference_bytes_changed",))
