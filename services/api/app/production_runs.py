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
    AssetRepository, AudioAssetRepository, BudgetPolicyRepository, ClipRepository, Database, ImageAssetRepository,
    JobRepository, ProjectDraftRepository, ProjectRepository, ProviderMachineCapabilityProfileRepository, ProviderMachineSettingRepository,
    ProviderCallRepository, TalkingProfileRepository, TalkingSliceSeriesContinuityReviewRepository, TalkingSliceSeriesRepository, VoiceProfileRepository,
)
from app.domain.models import Clip, Job, JobStatus, JobType, PlannedTalkingContext, PlannedTalkingRunOrigin, RenderVideoJobPayload, SourceKind, TalkingGenerationJobPayload, TalkingPerformanceBrief, TalkingQaJobPayload, TalkingRunPreviewJobPayload, TalkingSliceSeries, VisualStyleTokens, VoiceGenerationJobPayload, VoiceQaJobPayload
from app.production_preflight import PreflightInputError, ProductionPreflight, build_production_preflight
from app.talking import TalkingAudioSliceSeriesPlan, TalkingSlicePlanningError, plan_source_forward_reference_windows, plan_talking_audio_slice, select_talking_reference
from app.talking.admission import _matches_interval_copy
from app.voice_qa import comparison_tokens, voice_human_review_status
from app.domain.models import PlannedTalkingRunOriginV2
from app.talking.review_policy import TalkingReviewPolicyError, TalkingReviewPolicyService, require_child_judgment
from app.providers.local_generation import local_generation_ready, local_generation_licensed
from app.source_usage import SourceUsageService, SourceUsageConflict, overlap


class ProductionRunError(ValueError):
    pass


class ProductionRunNotReady(ProductionRunError):
    def __init__(self, reasons: tuple[str, ...]):
        self.reasons = reasons
        super().__init__("production preflight is not executable: " + ", ".join(reasons))


class ProductionRunConflict(ProductionRunError):
    pass


def has_approved_voice_quality(db: Database, audio, *, production_run_id=None) -> bool:
    """Use the same exact native or retained review binding as final assembly."""
    generation = None if audio is None else audio.metadata.get("voice_generation")
    if not isinstance(generation, dict) or generation.get("qa_state") != "verified":
        return False
    from app.assembly.video_spec import _require_generated_voice_qa
    try:
        _require_generated_voice_qa(audio, db, **({"production_run_id":production_run_id} if production_run_id is not None else {}))
    except ValueError:
        return False
    return True


def _select_bound_talking_reference(db: Database, data_root: Path, asset, clip, brief):
    fit = select_talking_reference([clip], brief)
    if fit.selected_clip_id == clip.id or not isinstance(asset.metadata.get("source_derivation"), dict):
        return fit
    # Retained full-interval source reviews predate the retrieval classification.
    # Reopen their exact provenance and bytes; never manufacture visibility scores.
    from app.media import FFProbeAdapter, MediaImporter, VerticalSourceDerivationService
    from app.media.vertical_derivation import VerticalDerivationError
    try:
        VerticalSourceDerivationService(
            db, data_root, MediaImporter(db, data_root, FFProbeAdapter()),
        ).verify_existing(asset, clip)
    except (VerticalDerivationError, ValueError, KeyError, TypeError, OSError):
        return fit
    return select_talking_reference([clip], brief, adopted_full_interval_clip_ids=frozenset({clip.id}))


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
        "u_voice_rejected", "approved_master_ready_to_resume", "voice_qa_ready_for_draft",
        "render_pending", "render_running",
        "render_failed", "render_completed_awaiting_review", "cancelled",
    ]
    talking_review_policy_version: Literal[1, 2] = 1
    review_state: Literal["pending", "approved", "rejected"] = "pending"
    review_notes: str | None = None
    automatic: bool = False
    start_job_id: UUID | None = None
    work_id: UUID | None = None
    delivery_intent: Literal["publish", "draft"] = "publish"


class ProductionRunReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    project_id: UUID
    draft_version: int
    script_revision: int
    stage: str
    created_at: AwareDatetime


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
    """Use complete stored speech segments on the same contiguous delivery clock."""
    from app.voice_boundary_alignment import scene_narration_segment_spans
    from app.talking.slices import derive_talking_delivery_interval
    spans = scene_narration_segment_spans(audio, scenes)
    if spans is None:
        return None
    for scene, (first, end) in zip(scenes, spans):
        if scene.id == scene_plan_id:
            try:
                return derive_talking_delivery_interval(audio,
                    start_segment_index=first, end_segment_index=end)
            except TalkingSlicePlanningError:
                return None
    return None


def _run_from_row(db: Database, row) -> ProductionRun:
    voice_job_id = None if row["voice_job_id"] is None else UUID(row["voice_job_id"])
    voice_audio_id = None if row["voice_audio_id"] is None else UUID(row["voice_audio_id"])
    voice_qa_job_id = None if row["voice_qa_job_id"] is None else UUID(row["voice_qa_job_id"])
    auto_stops = tuple(json.loads(row["voice_qa_auto_stop_reasons"]))
    talking_master_id = None if row["talking_master_audio_id"] is None else UUID(row["talking_master_audio_id"])
    talking_dependencies = tuple(TalkingPlanDependency.model_validate(item) for item in json.loads(row["talking_dependencies"]))
    talking_bindings = tuple(TalkingSourceBinding.model_validate(item) for item in json.loads(row["talking_source_bindings"]))
    waiting_stops = tuple(json.loads(row["waiting_stop_reasons"]))
    talking_fields = dict(work_id=row["work_id"] or row["id"],delivery_intent=row["delivery_intent"],automatic=bool(row["automatic"]),start_job_id=row["start_job_id"],talking_review_policy_version=row["talking_review_policy_version"], talking_master_audio_id=talking_master_id, talking_dependencies=talking_dependencies,
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
                    elif row["automatic"]:
                        qa_status = "voice_qa_ready_for_draft"
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
    def __init__(self, db: Database, data_root: str | Path, *, talking_provider=None):
        self.db = db
        self.data_root = Path(data_root).resolve()
        self.talking_provider = talking_provider

    def list_references(self, project_id: UUID, draft_version: int | None = None,
                        script_revision: int | None = None) -> list[ProductionRunReference]:
        if (draft_version is None) != (script_revision is None):
            raise ValueError("draft_version and script_revision must be supplied together")
        where = "project_id = ? AND stage IN ('awaiting_master', 'awaiting_talking', 'voice', 'render', 'cancelled')"
        values: list[str | int] = [str(project_id)]
        if draft_version is not None:
            where += " AND draft_version = ? AND script_revision = ?"
            values.extend((draft_version, script_revision))
        rows = self.db.connection.execute(
            "SELECT id, project_id, draft_version, script_revision, stage, created_at "
            f"FROM production_runs WHERE {where} ORDER BY created_at DESC, rowid DESC LIMIT 20",
            values,
        ).fetchall()
        return [ProductionRunReference.model_validate(dict(row)) for row in rows]

    def get(self, project_id: UUID, run_id: UUID) -> ProductionRun | None:
        row = self.db.connection.execute(
            "SELECT * FROM production_runs WHERE id = ? AND project_id = ? AND stage IN ('awaiting_master', 'awaiting_talking', 'voice', 'render', 'cancelled')",
            (str(run_id), str(project_id)),
        ).fetchone()
        if row is None:
            return None
        review = SourceUsageService(self.db).review(row)
        return _run_from_row(self.db, row).model_copy(update={"review_state":review["state"], "review_notes":review["notes"]})

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
            repair_allowance=row["repair_allowance"], source_usage_run_id=UUID(row["id"]),
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
        self, project_id: UUID, *, idempotency_key: str, expected_fingerprint: str | None,
        style_tokens: VisualStyleTokens, repair_allowance: int = 1, talking_review_policy_version: int = 1,
        automatic: bool = False, start_job_id: UUID | None = None,
        candidate_of_run_id: UUID | None = None,
    ) -> ProductionRun:
        if expected_fingerprint is None:
            raise ProductionRunNotReady(("expected_fingerprint_required",))
        if talking_review_policy_version not in {1, 2}:
            raise ProductionRunConflict("talking_review_policy_unknown")
        with self.db.transaction(immediate=True):
            prior = self.db.connection.execute("SELECT * FROM production_runs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if prior is None:
                prior = self.db.connection.execute("SELECT * FROM production_runs WHERE project_id=? AND preflight_fingerprint=?",
                    (str(project_id),expected_fingerprint)).fetchone()
            project = ProjectRepository(self.db).get(project_id)
            if project is None:
                raise ProductionRunError("project not found")
            parent = self.db.connection.execute("SELECT * FROM production_runs WHERE id=? AND project_id=?",
                (str(candidate_of_run_id), str(project_id))).fetchone() if candidate_of_run_id else None
            if candidate_of_run_id and parent is None:
                raise ProductionRunConflict("candidate_parent_run_not_found")
            draft = ProjectDraftRepository(self.db).get(project_id)
            preflight = build_production_preflight(
                self.db, project, draft, style_tokens=style_tokens,
                expected_fingerprint=expected_fingerprint, repair_allowance=repair_allowance,
                source_usage_run_id=UUID(prior["id"]) if prior is not None and prior["project_id"] == str(project_id) else None,
                source_usage_work_id=UUID(parent["work_id"] or parent["id"]) if parent else None,
            )
            stage = self._start_stage(preflight)
            if talking_review_policy_version == 2 and not any(item.production_need == "new_talking" for item in preflight.scenes):
                raise ProductionRunConflict("aggregate_policy_requires_planned_talking")
            request_fingerprint = _snapshot_sha256({"plan": preflight.fingerprint, "talking_review_policy_version": talking_review_policy_version,
                "candidate_of_run_id": None if candidate_of_run_id is None else str(candidate_of_run_id)})
            if stage in {"render", "awaiting_talking"}:
                self._require_master_bytes(preflight.reusable_master_audio_id)
            existing = self.db.connection.execute(
                "SELECT * FROM production_runs WHERE idempotency_key = ? AND stage IN ('awaiting_master', 'awaiting_talking', 'voice', 'render', 'cancelled')",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if (existing["project_id"] != str(project_id) or existing["preflight_fingerprint"] != preflight.fingerprint
                    or existing["talking_review_policy_version"] != talking_review_policy_version
                    or existing["review_request_fingerprint"] not in {None, request_fingerprint}):
                    raise ProductionRunConflict("idempotency key belongs to a different production plan")
                return _run_from_row(self.db, existing)
            duplicate = self.db.connection.execute(
                "SELECT * FROM production_runs WHERE project_id = ? AND preflight_fingerprint = ? AND stage IN ('awaiting_master', 'awaiting_talking', 'voice', 'render', 'cancelled')",
                (str(project_id), preflight.fingerprint),
            ).fetchone()
            if duplicate is not None:
                if duplicate["talking_review_policy_version"] != talking_review_policy_version:
                    raise ProductionRunConflict("production_plan_already_bound_to_different_review_policy")
                return _run_from_row(self.db, duplicate)
            assert draft is not None
            run_id, now = uuid4(), datetime.now(timezone.utc)
            job_id = None
            work_id = (parent["work_id"] or parent["id"]) if parent else str(run_id)
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
            self.db.connection.execute("UPDATE production_runs SET automatic=?,start_job_id=?,work_id=?,delivery_intent=? WHERE id=?",
                (int(automatic),None if start_job_id is None else str(start_job_id),work_id,project.delivery_intent,str(run_id)))
            if stage == "render":
                job_id = self._enqueue_render(project, draft, preflight, run_id, now)
                self.db.connection.execute("UPDATE production_runs SET render_job_id=? WHERE id=?", (str(job_id), str(run_id)))
            try:
                SourceUsageService(self.db).save(project_id, run_id,
                    SourceUsageService(self.db).preflight_uses(preflight, draft.scenes))
            except SourceUsageConflict as exc:
                raise ProductionRunNotReady((exc.reason,)) from exc
            return ProductionRun(
                id=run_id, project_id=project_id, idempotency_key=idempotency_key,
                preflight_fingerprint=preflight.fingerprint, render_job_id=job_id,
                created_at=now, status={"render": "render_pending", "awaiting_master": "awaiting_approved_master",
                                        "awaiting_talking": "awaiting_talking_dependencies"}[stage],
                talking_master_audio_id=preflight.reusable_master_audio_id if stage == "awaiting_talking" else None,
                talking_dependencies=talking_dependencies, waiting_stop_reasons=waiting_stops,
                talking_review_policy_version=talking_review_policy_version,
                  automatic=automatic,start_job_id=start_job_id,work_id=work_id,delivery_intent=project.delivery_intent,
            )

    def bind_talking_source(
        self, project_id: UUID, run_id: UUID, *, scene_plan_id: UUID, talking_profile_id: UUID,
        reference_clip_id: UUID, capability_profile_id: UUID, master_start_ms: int,
        master_end_ms: int, brief: TalkingPerformanceBrief,
        _new_inference: bool = True,
    ) -> ProductionRun | None:
        """Retain an exact candidate, not full-interval suitability or dispatch admission."""
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
                repair_allowance=row["repair_allowance"], source_usage_run_id=UUID(row["id"]),
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
            if not has_approved_voice_quality(self.db, master, production_run_id=run_id):
                raise ProductionRunNotReady(("talking_approved_master_required",))
            self._require_master_bytes(master.id)
            target_scene = next((item for item in draft.scenes if item.id == scene_plan_id), None)
            if target_scene is None or master_end_ms > master.duration_ms:
                raise ProductionRunNotReady(("talking_master_interval_invalid",))
            segments = [item for item in master.transcript_segments
                        if item.start_ms >= master_start_ms and item.end_ms <= master_end_ms]
            expected_interval = _unique_scene_speech_span(master, sorted(draft.scenes, key=lambda item: item.order), scene_plan_id)
            if (not segments or expected_interval != (master_start_ms, master_end_ms)
                or any(item.start_ms < previous.end_ms for previous, item in zip(segments, segments[1:]))
                or not _matches_interval_copy(target_scene.voice_text, master.transcript_segments,
                    master_start_ms, master_end_ms, audio=master)):
                raise ProductionRunNotReady(("talking_master_interval_not_exact_scene_speech",))
            profile = TalkingProfileRepository(self.db).get(talking_profile_id)
            clip = ClipRepository(self.db).get(reference_clip_id)
            if profile is None or not profile.consent.confirmed or reference_clip_id not in profile.reference_clip_ids:
                raise ProductionRunNotReady(("talking_profile_consent_required",))
            if clip is None:
                raise ProductionRunNotReady(("talking_reference_clip_missing",))
            asset = AssetRepository(self.db).get(clip.asset_id)
            if asset is None or asset.source_kind not in {SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET, SourceKind.STOCK}:
                raise ProductionRunNotReady(("talking_reference_source_missing",))
            requested_brief = brief.model_copy(update={
                "minimum_reference_duration_ms": max(brief.minimum_reference_duration_ms, master_end_ms - master_start_ms),
            })
            fit = _select_bound_talking_reference(self.db, self.data_root, asset, clip, requested_brief)
            if fit.selected_clip_id != clip.id:
                raise ProductionRunNotReady(("talking_reference_fit_not_verified",))
            self._require_source_bytes(asset.source_file, asset.content_hash)
            capability = ProviderMachineCapabilityProfileRepository(self.db).get(capability_profile_id)
            if (capability is None or capability.capability != "talking" or capability.provider != profile.provider
                or not local_generation_ready(capability)):
                raise ProductionRunNotReady(("exact_talking_capability_not_verified",))
            if not local_generation_licensed(capability):
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
                authorization_reference=asset.authorization_reference,
            )
            previous = next((item for item in run.talking_source_bindings if item.scene_plan_id == scene_plan_id), None)
            if previous is not None:
                if previous.model_copy(update={
                    "suitability": "full_interval_unknown", "suitability_assessment_id": None,
                    "talking_job_id": None, "talking_output_asset_id": None, "talking_qa_job_id": None,
                    "talking_series_id": None, "talking_preview_job_id": None, "talking_run_id": None,
                }).model_copy(update={"authorization_reference": binding.authorization_reference}) != binding:
                    raise ProductionRunConflict("Talking source binding differs from persisted candidate")
                return run
            bindings = (*run.talking_source_bindings, binding)
            reasons = tuple(reason for reason in run.waiting_stop_reasons if reason not in {
                f"talking_source_not_bound:{dependency.scene_id}",
                f"talking_source_not_admitted:{dependency.scene_id}",
                f"talking_full_interval_suitability_unverified:{dependency.scene_id}",
            })
            self.db.connection.execute(
                "UPDATE production_runs SET talking_source_bindings = ?, waiting_stop_reasons = ? WHERE id = ? AND stage = 'awaiting_talking'",
                (json.dumps([item.model_dump(mode="json") for item in bindings]), json.dumps(reasons), str(run_id)),
            )
            return self.get(project_id, run_id)

    def _talking_duration_limit(self, capability, reference_duration_ms: int | None = None) -> int | None:
        """A configured attempt ceiling does not assert verified quality."""
        limit = capability.verified_parameters.get("fresh_voice_talking_duration_ms")
        if limit is None:
            setting = ProviderMachineSettingRepository(self.db).get_by_scope_key(capability.scope_key)
            limit = None if setting is None else setting.values.get("max_talking_duration_ms")
        if type(limit) is not int or limit <= 0:
            return reference_duration_ms
        return min(limit, reference_duration_ms) if reference_duration_ms is not None else limit

    def _talking_reference_windows(self, run, master, *, extra_binding=None):
        """Allocate source-forward motion, including model alignment context."""
        from app.talking.pool import required_talking_reference_duration_ms

        peers = list(run.talking_source_bindings)
        if extra_binding is not None and not any(item.scene_plan_id == extra_binding.scene_plan_id for item in peers):
            peers.append(extra_binding)
        usage = SourceUsageService(self.db)
        try:
            occupied = usage.occupied(run.project_id, run_id=run.id)
            row = self.db.connection.execute("SELECT visual_source_uses FROM production_runs WHERE id=?", (str(run.id),)).fetchone()
            occupied.extend(json.loads(row[0]) if row is not None else [])
            sources, durations, pinned = {}, {}, {}
            jobs = JobRepository(self.db)
            for item in peers:
                clip = ClipRepository(self.db).get(item.reference_clip_id)
                if clip is None:
                    raise SourceUsageConflict("talking_reference_clip_missing")
                canonical = usage.resolve(clip.asset_id, clip.start_ms, clip.end_ms, item.scene_plan_id)
                if len(canonical) != 1:
                    raise SourceUsageConflict("visual_source_lineage_unresolved")
                source = canonical[0]
                duration = (item.master_end_ms-item.master_start_ms if self.talking_provider is None else
                    required_talking_reference_duration_ms(self.talking_provider,
                        item.master_end_ms-item.master_start_ms, clip.end_ms-clip.start_ms))
                if duration is None:
                    raise SourceUsageConflict("talking_source_coverage_insufficient_replan")
                sources[item.scene_plan_id] = (clip, source)
                durations[item.scene_plan_id] = duration
                prior = jobs.get(item.talking_job_id) if item.talking_job_id else None
                if prior is not None:
                    if not isinstance(prior.payload, TalkingGenerationJobPayload):
                        raise SourceUsageConflict("planned_talking_execution_snapshot_changed")
                    saved_start = prior.payload.reference_window_start_ms
                    saved_end = prior.payload.reference_window_end_ms
                    saved_start = clip.start_ms if saved_start is None else saved_start
                    saved_end = clip.end_ms if saved_end is None else saved_end
                    if not clip.start_ms <= saved_start < saved_start+duration <= saved_end <= clip.end_ms:
                        raise SourceUsageConflict("talking_source_coverage_insufficient_replan")
                    use = {**source, "start_ms": source["start_ms"]+saved_start-clip.start_ms,
                           "end_ms": source["start_ms"]+saved_start-clip.start_ms+duration}
                    hits = [other for other in occupied if overlap(use, other)]
                    if hits:
                        reason = "visual_source_already_published" if any(v.get("state")=="published" for v in hits) else "visual_source_reserved_by_other_video"
                        raise SourceUsageConflict(reason)
                    pinned[item.scene_plan_id] = (use, (saved_start, saved_end))
            cursors, windows = {}, {}
            for item in sorted(peers, key=lambda value:value.master_start_ms):
                clip, source = sources[item.scene_plan_id]
                duration = durations[item.scene_plan_id]
                previous = cursors.get(source["source_hash"])
                start = source["start_ms"] if previous is None else max(source["start_ms"], previous)
                # Saved jobs own their exact windows. Occupancy changes must not
                # silently remap a queued or completed provider call.
                if item.scene_plan_id in pinned:
                    use, saved = pinned[item.scene_plan_id]
                    if use["start_ms"] < start:
                        raise SourceUsageConflict("talking_motion_restart_requires_replan")
                    windows[item.scene_plan_id] = saved
                    cursors[source["source_hash"]] = use["end_ms"]
                    continue
                # A later saved performance cannot be displaced by an earlier
                # unstarted scene, including aliases of the same physical source.
                future = [value[0] for peer_id, value in pinned.items()
                          if peer_id != item.scene_plan_id
                          and value[0]["source_hash"] == source["source_hash"]
                          and next(peer.master_start_ms for peer in peers if peer.scene_plan_id == peer_id) > item.master_start_ms]
                end = min([source["end_ms"], *(value["start_ms"] for value in future)])
                while start+duration <= end:
                    use = {**source,"start_ms":start,"end_ms":start+duration}
                    hits = [other for other in occupied if overlap(use,other)]
                    if not hits:
                        break
                    start = max(other["end_ms"] for other in hits)
                if start+duration > end:
                    raise SourceUsageConflict("talking_source_coverage_insufficient_replan")
                window_start = clip.start_ms + start-source["start_ms"]
                windows[item.scene_plan_id] = (window_start,
                    window_start+duration if self.talking_provider is not None else clip.end_ms)
                cursors[source["source_hash"]] = start+duration
            return windows
        except SourceUsageConflict as exc:
            raise ProductionRunNotReady((exc.reason,)) from exc

    def _talking_reference_window(self, run, binding, master) -> tuple[int, int]:
        """Keep dispatched windows immutable; fresh windows include alignment."""
        return self._talking_reference_windows(run, master, extra_binding=binding)[binding.scene_plan_id]

    def _require_talking_source_continuity(self, run) -> None:
        ends = {}
        for binding in sorted(run.talking_source_bindings, key=lambda item: item.master_start_ms):
            job = JobRepository(self.db).get(binding.talking_job_id) if binding.talking_job_id else None
            if job is None or not isinstance(job.payload, TalkingGenerationJobPayload):
                continue
            start = job.payload.reference_window_start_ms
            clip = ClipRepository(self.db).get(binding.reference_clip_id)
            try:
                from app.talking.pool import required_talking_reference_duration_ms
                needed = (binding.master_end_ms-binding.master_start_ms if self.talking_provider is None else
                    required_talking_reference_duration_ms(self.talking_provider,
                        binding.master_end_ms-binding.master_start_ms, clip.end_ms-clip.start_ms))
                if needed is None:
                    raise SourceUsageConflict("talking_source_coverage_insufficient_replan")
                source = SourceUsageService(self.db).resolve(clip.asset_id, start,
                    start+needed, binding.scene_plan_id)[0] if start is not None else None
            except SourceUsageConflict as exc:
                raise ProductionRunNotReady((exc.reason,)) from exc
            key = None if source is None else source["source_hash"]
            if source is None or source["start_ms"] < ends.get(key, 0):
                raise ProductionRunNotReady(("talking_motion_restart_requires_replan",))
            ends[key] = source["end_ms"]

    def _validated_talking_dispatch(
        self, project_id: UUID, run_id: UUID, binding: TalkingSourceBinding,
        *, new_inference: bool = True,
    ) -> tuple[TalkingSourceBinding, object, object, object, object]:
        """Reopen every mutable candidate gate within the caller's transaction."""
        self.bind_talking_source(
            project_id, run_id, scene_plan_id=binding.scene_plan_id,
            talking_profile_id=binding.talking_profile_id, reference_clip_id=binding.reference_clip_id,
            capability_profile_id=binding.capability_profile_id,
            master_start_ms=binding.master_start_ms, master_end_ms=binding.master_end_ms,
            brief=binding.performance_brief,
            _new_inference=new_inference,
        )
        run = self.get(project_id, run_id)
        if run is None or run.status != "awaiting_talking_dependencies":
            raise ProductionRunConflict("planned Talking run is no longer waiting")
        current = next((item for item in run.talking_source_bindings if item.scene_plan_id == binding.scene_plan_id), None)
        if current != binding:
            raise ProductionRunConflict("Talking source binding changed")
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
        source_start, source_end = self._talking_reference_window(run, current, master)
        limit = self._talking_duration_limit(capability, source_end - source_start)
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
                delivery_interval_ms=(current.master_start_ms, current.master_end_ms),
            )
        except TalkingSlicePlanningError as exc:
            raise ProductionRunNotReady(("talking_scene_exceeds_verified_duration_bound",)) from exc
        if slice_plan.start_ms != current.master_start_ms or slice_plan.end_ms != current.master_end_ms:
            raise ProductionRunNotReady(("talking_master_interval_changed",))
        self._require_source_usage(project_id, run_id)
        return current, profile, master, capability, slice_plan

    def _require_source_usage(self, project_id, run_id):
        try:
            SourceUsageService(self.db).require_run(project_id, run_id)
        except SourceUsageConflict as exc:
            raise ProductionRunNotReady((exc.reason,)) from exc

    def dispatch_talking(
        self, project_id: UUID, run_id: UUID, *, scene_plan_id: UUID,
    ) -> ProductionRun | None:
        """Atomically enqueue one bounded planned Talking Job, never a ProviderCall."""
        with (nullcontext() if self.db.connection.in_transaction else self.db.transaction(immediate=True)):
            run = self.get(project_id, run_id)
            if run is None:
                return None
            if run.status != "awaiting_talking_dependencies":
                raise ProductionRunConflict("production run is not awaiting Talking dependencies")
            binding = next((item for item in run.talking_source_bindings if item.scene_plan_id == scene_plan_id), None)
            if binding is None:
                raise ProductionRunNotReady(("talking_reference_candidate_not_bound",))
            current, profile, master, capability, slice_plan = self._validated_talking_dispatch(
                project_id, run_id, binding,
            )
            source_start, source_end = self._talking_reference_window(run, current, master)
            context = PlannedTalkingContext(
                run_id=run_id, scene_plan_id=scene_plan_id,
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
                slice_max_duration_ms=self._talking_duration_limit(capability, source_end - source_start),
                slice_limit_source=f"{'verified-capability' if capability.verified_parameters.get('fresh_voice_talking_duration_ms') else 'reference-duration-attempt-limit'}:{capability.id}",
                reference_window_start_ms=source_start,
                reference_window_end_ms=source_end,
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
            reasons = tuple(reason for reason in run.waiting_stop_reasons
                            if reason != "talking_dispatch_not_implemented")
            reasons = tuple(dict.fromkeys((*reasons, f"talking_qa_run_not_implemented:{scene_plan_id}")))
            self.db.connection.execute(
                "UPDATE production_runs SET talking_source_bindings = ?, waiting_stop_reasons = ? WHERE id = ? AND stage = 'awaiting_talking'",
                (json.dumps([item.model_dump(mode="json") for item in bindings]), json.dumps(reasons), str(run_id)),
            )
            dispatched = self.get(project_id, run_id)
            self._require_talking_source_continuity(dispatched)
            return dispatched

    def dispatch_all_talking(
        self, project_id: UUID, run_id: UUID, *, talking_profile_id: UUID | None = None,
        reference_clip_id: UUID | None = None, capability_profile_id: UUID | None = None,
        brief: TalkingPerformanceBrief | None = None,
    ) -> ProductionRun | None:
        """按真实旁白从统一动作池分配；既有窗口和合格结果保持绑定。"""
        supplied = (talking_profile_id, reference_clip_id, capability_profile_id)
        if any(value is not None for value in supplied) and not all(value is not None for value in supplied):
            raise ProductionRunNotReady(("talking_source_selection_incomplete",))
        with (nullcontext() if self.db.connection.in_transaction else self.db.transaction(immediate=True)):
            run = self.get(project_id, run_id)
            if run is None:
                return None
            if run.status != "awaiting_talking_dependencies":
                raise ProductionRunConflict("production run is not awaiting Talking dependencies")
            bindings = {item.scene_plan_id: item for item in run.talking_source_bindings}
            started = {item.scene_plan_id for item in run.talking_qa_dependencies}
            pending = [item for item in run.talking_dependencies if item.scene_plan_id not in started
                       and (item.scene_plan_id not in bindings or bindings[item.scene_plan_id].talking_job_id is None)]
            if not pending:
                return run
            selections={}
            if talking_profile_id is None:
                choices = {(item.talking_profile_id, item.reference_clip_id, item.capability_profile_id)
                           for item in run.talking_source_bindings}
                if len(choices)==1 and not run.automatic:
                    talking_profile_id, reference_clip_id, capability_profile_id = next(iter(choices))
                else:
                    from app.talking.pool import build_talking_pool,allocate_talking_windows,TalkingPoolCapacityError
                    contexts=[self.talking_source_context(project_id,run_id,scene_plan_id=item.scene_plan_id)
                        for item in pending]
                    if any(value is None or value.status!="ready" for value in contexts):
                        raise ProductionRunNotReady(("talking_scene_interval_unresolved",))
                    usage=SourceUsageService(self.db)
                    occupied=usage.occupied(project_id,run_id=run_id)
                    row=self.db.connection.execute('SELECT visual_source_uses FROM production_runs WHERE id=?',(str(run_id),)).fetchone()
                    occupied.extend(json.loads(row[0]) if row else [])
                    for previous in run.talking_source_bindings:
                        prior=JobRepository(self.db).get(previous.talking_job_id) if previous.talking_job_id else None
                        if prior is None:continue
                        clip=ClipRepository(self.db).get(previous.reference_clip_id)
                        start=prior.payload.reference_window_start_ms
                        end=prior.payload.reference_window_end_ms
                        occupied.extend(usage.resolve(clip.asset_id,clip.start_ms if start is None else start,
                            clip.end_ms if end is None else end,previous.scene_plan_id))
                    pool=build_talking_pool(self.db,project_id,talking_provider=self.talking_provider,
                        brief=brief,data_root=self.data_root,run_id=run_id,occupied=occupied)
                    try:
                        allocated=allocate_talking_windows(pool,[value.speech_end_ms-value.speech_start_ms
                            for value in contexts],talking_provider=self.talking_provider)
                    except TalkingPoolCapacityError as exc:
                        raise ProductionRunNotReady((exc.reason,)) from exc
                    selections={dependency.scene_plan_id:(allocation.entry.profile.id,
                        allocation.entry.clip.id,allocation.entry.capability.id)
                        for dependency,allocation in zip(pending,allocated)}
            choice = (talking_profile_id, reference_clip_id, capability_profile_id)
            self._require_talking_batch_budget(project_id, len(pending))
            for dependency in pending:
                selected=selections.get(dependency.scene_plan_id,choice)
                bound = bindings.get(dependency.scene_plan_id)
                if bound is not None and (bound.talking_profile_id, bound.reference_clip_id,
                                           bound.capability_profile_id) != selected:
                    raise ProductionRunConflict("selected Talking source differs from an unstarted scene binding")
                context = self.talking_source_context(project_id, run_id, scene_plan_id=dependency.scene_plan_id)
                if context is None or context.status != "ready":
                    raise ProductionRunNotReady((f"talking_scene_interval_unresolved:{dependency.scene_id}",))
                if bound is not None and (bound.master_start_ms, bound.master_end_ms) != (
                    context.speech_start_ms, context.speech_end_ms,
                ):
                    raise ProductionRunConflict("bound Talking narration interval changed")
                self.bind_talking_source(
                    project_id, run_id, scene_plan_id=dependency.scene_plan_id,
                    talking_profile_id=selected[0], reference_clip_id=selected[1],
                    capability_profile_id=selected[2],
                    master_start_ms=context.speech_start_ms, master_end_ms=context.speech_end_ms,
                    brief=bound.performance_brief if bound is not None else brief or TalkingPerformanceBrief(),
                )
            # Bind and validate the whole source allocation before enqueueing any Job.
            bound_run = self.get(project_id, run_id)
            master = AudioAssetRepository(self.db).get(bound_run.talking_master_audio_id)
            for dependency in pending:
                item = next(value for value in bound_run.talking_source_bindings if value.scene_plan_id == dependency.scene_plan_id)
                self._talking_reference_window(bound_run, item, master)
            for dependency in pending:
                self.dispatch_talking(project_id, run_id, scene_plan_id=dependency.scene_plan_id)
            return self.get(project_id, run_id)

    def _require_talking_batch_budget(self, project_id: UUID, calls: int) -> None:
        """Account for queued one-call work before adding more; execution still reserves."""
        from app.budget import snapshot
        operations = {
            JobType.GENERATE_TALKING: "talking", JobType.GENERATE_VOICE: "tts",
            JobType.VERIFY_VOICE: "asr",
        }
        records = ProviderCallRepository(self.db).list_all()
        accounted = {record.idempotency_key for record in records
                     if record.status != "cancelled" or record.actual_cost is not None}
        # Only active product executions schedule these calls. Historical evidence
        # and orphaned pending Jobs do not reserve a current product budget.
        scheduled: set[str] = set()
        for row in self.db.connection.execute(
            "SELECT voice_job_id, voice_qa_job_id, talking_source_bindings FROM production_runs WHERE stage IN ('voice', 'awaiting_talking')",
        ).fetchall():
            scheduled.update(value for value in (row["voice_job_id"], row["voice_qa_job_id"]) if value is not None)
            scheduled.update(str(item["talking_job_id"]) for item in json.loads(row["talking_source_bindings"])
                             if item.get("talking_job_id") is not None)
        queued = []
        jobs = JobRepository(self.db)
        for job_id in sorted(scheduled):
            job = jobs.get(UUID(job_id))
            if job is None or job.status not in {JobStatus.PENDING, JobStatus.RUNNING} or job.type not in operations:
                continue
            attempt = job.attempt + (job.status is JobStatus.PENDING)
            prefix = f"job:{job.id}:{operations[job.type]}:{attempt}:"
            if not any(key.startswith(prefix) for key in accounted):
                queued.append(job)
        policies = BudgetPolicyRepository(self.db)
        for scope in (None, project_id):
            policy = policies.get_by_project(scope)
            if policy is None:
                continue
            spent = snapshot(records if scope is None else [item for item in records if item.project_id == scope], policy.currency)
            unaccounted = sum(scope is None or job.project_id == scope for job in queued)
            if policy.max_calls is not None and spent.calls + unaccounted + calls > policy.max_calls:
                raise ProductionRunNotReady(("talking_batch_budget_call_limit",))
            # This lane only dispatches the existing local capability (zero external charge).
            if policy.max_amount is not None and spent.known_amount > policy.max_amount:
                raise ProductionRunNotReady(("talking_batch_budget_amount_limit",))

    def require_talking_job_admission(
        self, job: Job, *, provider: str, model: str, runtime: str | None, machine_id: str | None,
        _new_inference: bool = True,
    ):
        """Validate the planned identity and return its exact delivery slice."""
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
                job.project_id, context.run_id, binding,
                new_inference=_new_inference,
            )
            source_start, source_end = self._talking_reference_window(run, current, master)
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
                or job.payload.slice_max_duration_ms != self._talking_duration_limit(
                    capability, source_end - source_start)
                or job.payload.reference_window_start_ms != source_start
                or job.payload.reference_window_end_ms != source_end):
                raise ProductionRunNotReady(("planned_talking_execution_snapshot_changed",))
            return slice_plan

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
            or generation.get("reference_window_start_ms") != job.payload.reference_window_start_ms
            or generation.get("reference_window_end_ms") != job.payload.reference_window_end_ms):
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

    def require_draft_talking_visual(
        self, project_id: UUID, run_id: UUID, asset, clip, *, copy: str,
        master_audio_id: UUID | None = None, selected_duration_ms: int | None = None,
        master_interval: tuple[int, int] | None = None,
    ) -> None:
        """本次 Run 的技术合格输出可进入待审成片；不授予跨 Run 复用资格。"""
        row = self.db.connection.execute(
            "SELECT * FROM production_runs WHERE id = ? AND project_id = ? AND stage IN ('awaiting_talking', 'render')",
            (str(run_id), str(project_id)),
        ).fetchone()
        if row is None:
            raise ProductionRunNotReady(("draft_talking_run_missing",))
        run = _run_from_row(self.db, row)
        binding = next((item for item in run.talking_source_bindings if item.talking_output_asset_id == asset.id), None)
        self._require_talking_source_continuity(run)
        if (binding is None or binding.talking_job_id is None or binding.talking_qa_job_id is None
            or (master_audio_id is not None and master_audio_id != binding.master_audio_id)
            or binding.master_audio_id != run.talking_master_audio_id
            or clip.asset_id != asset.id or clip.start_ms != 0 or clip.end_ms != asset.duration_ms
            or clip.asset_duration_ms != asset.duration_ms):
            raise ProductionRunNotReady(("draft_talking_binding_mismatch",))
        jobs = JobRepository(self.db)
        generated, qa_job = jobs.get(binding.talking_job_id), jobs.get(binding.talking_qa_job_id)
        if (generated is None or generated.status is not JobStatus.COMPLETED
            or qa_job is None or qa_job.status is not JobStatus.COMPLETED):
            raise ProductionRunNotReady(("draft_talking_qa_not_completed",))
        output, master = self.require_talking_qa_job(qa_job)
        self._require_bound_voice_master(row, master.id)
        self._require_consumed_talking_source(project_id, run_id, binding, generated)
        generation = output.metadata.get("talking_generation", {})
        report = generation.get("qa", {})
        if (output.id != asset.id or output.content_hash != asset.content_hash
            or generation.get("qa_state") != "verified" or report.get("automated_verified") is not True
            or report.get("job_id") != str(qa_job.id)
            or generation.get("human_review_state") == "rejected"
            or abs(output.duration_ms - (binding.master_end_ms - binding.master_start_ms)) > 80):
            raise ProductionRunNotReady(("draft_talking_qa_evidence_mismatch",))
        source = AssetRepository(self.db).get(binding.reference_asset_id)
        source_clip = ClipRepository(self.db).get(binding.reference_clip_id)
        if (source is None or source.content_hash != binding.reference_asset_hash
            or source_clip is None or source_clip.asset_id != source.id
            or source_clip.start_ms != binding.reference_start_ms or source_clip.end_ms != binding.reference_end_ms):
            raise ProductionRunNotReady(("draft_talking_source_changed",))
        self._require_source_bytes(source.source_file, source.content_hash)
        from app.talking.admission import _matches_interval_copy
        interval = (binding.master_start_ms, binding.master_end_ms)
        if master_interval is not None and master_interval != interval:
            raise ProductionRunNotReady(("draft_talking_interval_mismatch",))
        if not _matches_interval_copy(copy, master.transcript_segments, *interval, audio=master):
            raise ProductionRunNotReady(("draft_talking_copy_mismatch",))
        if selected_duration_ms is not None and selected_duration_ms < interval[1] - interval[0]:
            raise ProductionRunNotReady(("draft_talking_speech_would_be_cut",))

    def reconcile_completed_talking_qa_job(self, qa_job_id: UUID) -> ProductionRun | None:
        """自动衔接技术 QA 与待审成片；失败保留原因，不重新生成。"""
        job = JobRepository(self.db).get(qa_job_id)
        if (job is None or job.status is not JobStatus.COMPLETED
            or job.type is not JobType.VERIFY_TALKING or not isinstance(job.payload, TalkingQaJobPayload)):
            return None
        payload = job.payload
        run = self.get(payload.project_id, payload.run_id)
        if run is None or run.status != "awaiting_talking_dependencies":
            return run
        try:
            # 单个 QA 完成不代表整条作品就绪；其余人物任务仍由同一队列推进。
            required = {item.scene_plan_id for item in run.talking_dependencies}
            states = {item.scene_plan_id: item.state for item in run.talking_qa_dependencies}
            if any(states.get(scene_id) == "generation_failed" for scene_id in required):
                raise ProductionRunNotReady(("planned_talking_generation_failed",))
            if any(states.get(scene_id) == "qa_failed" for scene_id in required):
                raise ProductionRunNotReady(("planned_talking_qa_failed",))
            verified = {"qa_verified_ready_for_preview", "qa_verified_awaiting_u_talking"}
            if set(states) != required or any(state not in verified for state in states.values()):
                return run
            return self.resume(payload.project_id, payload.run_id)
        except (ProductionRunNotReady, ProductionRunConflict, PreflightInputError, ValueError) as exc:
            reasons = exc.reasons if isinstance(exc, ProductionRunNotReady) else ("talking_render_plan_changed",)
            self.db.connection.execute(
                "UPDATE production_runs SET waiting_stop_reasons = ? WHERE id = ? AND stage = 'awaiting_talking'",
                (json.dumps(reasons), str(payload.run_id)),
            )
            return self.get(payload.project_id, payload.run_id)

    def reconcile_completed_talking_qa_jobs_once(self) -> tuple[ProductionRun, ...]:
        rows = self.db.connection.execute(
            "SELECT id, project_id, talking_source_bindings FROM production_runs WHERE stage = 'awaiting_talking'",
        ).fetchall()
        results = []
        for row in rows:
            for raw in json.loads(row["talking_source_bindings"]):
                binding = TalkingSourceBinding.model_validate(raw)
                if binding.talking_qa_job_id is not None:
                    result = self.reconcile_completed_talking_qa_job(binding.talking_qa_job_id)
                    if result is not None:
                        results.append(result)
                        break
        return tuple(results)

    def require_current_planned_run_origin(
        self, project_id: UUID, run_id: UUID, scene_plan_id: UUID,
        *, expected: PlannedTalkingRunOrigin | None = None, admitted_run_id: UUID | None = None,
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
            if admitted_run_id is not None:
                from app.db import TalkingRunRepository
                admitted = TalkingRunRepository(self.db).get(admitted_run_id)
                series = None if admitted is None else TalkingSliceSeriesRepository(self.db).get(admitted.series_id)
                if (admitted is None or series is None or series.project_id != project_id
                    or series.planned_origin is None or expected != series.planned_origin
                    or admitted.project_id != project_id or admitted.admission_state != "admitted"
                    or admitted.master_narration_audio_id != binding.master_audio_id
                    or admitted.master_start_ms != binding.master_start_ms
                    or admitted.master_end_ms != binding.master_end_ms
                    or admitted.authorized_reference_clip_id != binding.reference_clip_id
                    or [item.job_id for item in admitted.child_evidence] != [binding.talking_job_id]
                    or [item.output_asset_id for item in admitted.child_evidence] != [binding.talking_output_asset_id]
                    or admitted.planned_origin_sha256 != _snapshot_sha256(expected.model_dump(mode="json"))):
                    raise ProductionRunNotReady(("planned_talking_admitted_binding_changed",))
            jobs = JobRepository(self.db)
            generated = jobs.get(binding.talking_job_id)
            qa_job = jobs.get(binding.talking_qa_job_id)
            if generated is None or generated.status is not JobStatus.COMPLETED or not isinstance(generated.payload, TalkingGenerationJobPayload):
                raise ProductionRunNotReady(("planned_talking_generation_not_completed",))
            context = generated.payload.planned_context
            if context is None or context.run_id != run_id or context.scene_plan_id != scene_plan_id:
                raise ProductionRunNotReady(("planned_talking_context_mismatch",))
            if binding.talking_run_id is None and admitted_run_id is None:
                self.require_talking_job_admission(
                    generated, provider=context.expected_provider, model=context.expected_model,
                    runtime=context.expected_runtime, machine_id=context.expected_machine_id,
                    _new_inference=False,
                )
            else:
                # Once admitted, normal planning can route this Run. Re-entering
                # full preflight from its own consumption gate would recurse.
                self._require_consumed_talking_source(
                    project_id, run_id, binding, generated, require_current_plan=admitted_run_id is None,
                )
            output = self._require_talking_output(project_id, run_id, binding, generated)
            if output.id != binding.talking_output_asset_id or qa_job is None or qa_job.status is not JobStatus.COMPLETED:
                raise ProductionRunNotReady(("planned_talking_qa_not_completed",))
            if qa_job.type is not JobType.VERIFY_TALKING or not isinstance(qa_job.payload, TalkingQaJobPayload):
                raise ProductionRunNotReady(("planned_talking_qa_mismatch",))
            checked_output, master = self.require_talking_qa_job(qa_job)
            if checked_output.id != output.id or not has_approved_voice_quality(self.db, master, production_run_id=run_id):
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
                or not profile.consent.confirmed
                or not local_generation_licensed(capability)
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
                source_start_ms=generated.payload.reference_window_start_ms,
                source_end_ms=generated.payload.reference_window_end_ms,
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
        *, require_current_plan: bool = True,
    ) -> None:
        """Current-admission checks that never invoke Router or a new-call budget."""
        row = self.db.connection.execute(
            "SELECT draft_version, script_revision FROM production_runs WHERE id = ? AND project_id = ? AND stage IN ('awaiting_talking', 'render')",
            (str(run_id), str(project_id)),
        ).fetchone()
        draft = ProjectDraftRepository(self.db).get(project_id) if require_current_plan else None
        scene = None if draft is None else next((item for item in draft.scenes if item.id == binding.scene_plan_id), None)
        if row is None or (require_current_plan and (draft is None or draft.version != row["draft_version"]
            or draft.script_revision != row["script_revision"] or scene is None)):
            raise ProductionRunNotReady(("planned_talking_plan_changed",))
        context = generated.payload.planned_context
        source_start, source_end = generated.payload.reference_window_start_ms, generated.payload.reference_window_end_ms
        if (source_start is None or source_end is None
            or source_start < binding.reference_start_ms or source_end > binding.reference_end_ms
            or source_end - source_start < binding.master_end_ms - binding.master_start_ms):
            raise ProductionRunNotReady(("planned_talking_reference_window_changed",))
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
            or not local_generation_ready(capability)
            or capability.evidence_reference != binding.capability_evidence_reference
            or not local_generation_licensed(capability)
            or capability.license_evidence_reference != binding.license_evidence_reference):
            raise ProductionRunNotReady(("planned_talking_execution_authority_changed",))
        clip = ClipRepository(self.db).get(binding.reference_clip_id)
        source = AssetRepository(self.db).get(binding.reference_asset_id)
        master = AudioAssetRepository(self.db).get(binding.master_audio_id)
        segments = [] if master is None else [segment for segment in master.transcript_segments
                                               if segment.start_ms >= binding.master_start_ms
                                               and segment.end_ms <= binding.master_end_ms]
        from app.talking.slices import derive_talking_delivery_interval
        indices = [] if master is None else [index for index, segment in enumerate(master.transcript_segments)
            if segment in segments]
        interval_valid = False
        if indices:
            try:
                interval_valid = derive_talking_delivery_interval(master,
                    start_segment_index=indices[0], end_segment_index=indices[-1] + 1) == (
                        binding.master_start_ms, binding.master_end_ms)
            except TalkingSlicePlanningError:
                pass
            if not require_current_plan:
                interval_valid = interval_valid or (segments[0].start_ms, segments[-1].end_ms) == (
                    binding.master_start_ms, binding.master_end_ms)
        if (clip is None or clip.talking_reference_assessment is None
            or clip.talking_reference_assessment.burned_in_subtitles is not False
            or source is None
            or not has_approved_voice_quality(self.db, master, production_run_id=run_id)
            or not segments or not interval_valid
            or (require_current_plan and not _matches_interval_copy(scene.voice_text, master.transcript_segments,
                binding.master_start_ms, binding.master_end_ms, audio=master))):
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

    def cancel(self, project_id: UUID, run_id: UUID) -> ProductionRun | None:
        with self.db.transaction(immediate=True):
            row = self.db.connection.execute(
                "SELECT * FROM production_runs WHERE id = ? AND project_id = ? AND stage IN ('awaiting_master', 'awaiting_talking', 'voice', 'render', 'cancelled')",
                (str(run_id), str(project_id)),
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
                "SELECT * FROM production_runs WHERE id = ? AND project_id = ? AND stage IN ('awaiting_master', 'awaiting_talking', 'voice', 'render', 'cancelled')",
                (str(run_id), str(project_id)),
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
                self.db, project, draft, style_tokens=style, repair_allowance=row["repair_allowance"], source_usage_run_id=UUID(row["id"]),
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
            or not local_generation_ready(capability) or not local_generation_licensed(capability)
            or not has_approved_voice_quality(self.db, audio, production_run_id=UUID(row["id"]))):
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
        """同 Run 的技术合格 Talking 直接进入待审成片。"""
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
        if set(bindings) != required:
            raise ProductionRunNotReady(("talking_dependencies_not_qa_complete",))
        self._require_talking_source_continuity(run)
        clips: dict[UUID, UUID] = {}
        for scene_id, binding in bindings.items():
            scene = next((item for item in draft.scenes if item.id == scene_id), None)
            if binding.talking_run_id is not None:
                admitted = TalkingRunRepository(self.db).get(binding.talking_run_id)
                asset = None if admitted is None else AssetRepository(self.db).get(admitted.assembled_asset_id)
                clip = None if admitted is None else ClipRepository(self.db).get(admitted.assembled_clip_id)
                if (admitted is None or admitted.series_id != binding.talking_series_id
                    or admitted.master_narration_audio_id != run.talking_master_audio_id
                    or admitted.master_start_ms != binding.master_start_ms or admitted.master_end_ms != binding.master_end_ms):
                    raise ProductionRunNotReady(("planned_talking_render_dependency_invalid",))
            else:
                asset = AssetRepository(self.db).get(binding.talking_output_asset_id) if binding.talking_output_asset_id else None
                if asset is None:
                    raise ProductionRunNotReady(("talking_output_missing_or_ambiguous",))
                clip = next((item for item in ClipRepository(self.db).list_by_asset(asset.id)
                             if item.start_ms == 0 and item.end_ms == asset.duration_ms), None)
                if clip is None:
                    clip = ClipRepository(self.db).create(Clip(asset_id=asset.id, start_ms=0,
                        end_ms=asset.duration_ms, asset_duration_ms=asset.duration_ms, talking_candidate=True))
            if (scene is None or asset is None or clip is None
                or talking_visual_blocker(asset, clip, AssetRepository(self.db), project_id=project_id,
                    copy=scene.voice_text, master_audio_id=run.talking_master_audio_id,
                    production_run_id=run_id) is not None):
                raise ProductionRunNotReady(("planned_talking_render_dependency_invalid",))
            if clip.end_ms - clip.start_ms < binding.master_end_ms - binding.master_start_ms:
                raise ProductionRunNotReady(("planned_talking_visual_timing_insufficient",))
            clips[scene_id] = clip.id
        preflight = build_production_preflight(
            self.db, project, draft, style_tokens=VisualStyleTokens.model_validate(json.loads(row["style_tokens"])),
            repair_allowance=row["repair_allowance"], bound_talking_clips=clips, production_run_id=run_id,
        )
        self._require_ready_lane(preflight, allow_planned=True)
        if preflight.reusable_master_audio_id != run.talking_master_audio_id:
            raise ProductionRunNotReady(("planned_talking_master_changed",))
        self._require_master_bytes(run.talking_master_audio_id)
        spec = self._resolved_render_spec(project, draft, preflight, production_run_id=run_id)
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
            if job is None or not isinstance(job.payload, RenderVideoJobPayload):
                raise ProductionRunNotReady(("planned_render_snapshot_changed",))
            if job.status is JobStatus.FAILED:
                # An explicit resume creates a fresh local render, preserving the
                # failed attempt. Reassemble the unchanged saved plan after a
                # local assembly fix; all bound media/QA have been revalidated.
                from app.source_usage import SourceUsageService, SourceUsageConflict
                try:
                    SourceUsageService(self.db).require_available(project_id, SourceUsageService(self.db).spec_uses(spec), run_id=run_id)
                except SourceUsageConflict as exc:
                    raise ProductionRunNotReady((exc.reason,)) from exc
                now = datetime.now(timezone.utc)
                replacement = Job(project_id=project_id, type=JobType.RENDER,
                    idempotency_key=f"production-run:{run_id}:render-after:{job.id}",
                    created_at=now, updated_at=now,
                    payload=job.payload.model_copy(update={"render_id": uuid4(), "video_spec": spec}))
                replacement = JobRepository(self.db).create(replacement)
                from app.source_use_constraints import SourceUseConstraintService
                SourceUseConstraintService(self.db, self.data_root).snapshot_render(project_id, replacement.id)
                self.db.connection.execute("UPDATE production_runs SET render_job_id = ? WHERE id = ? AND render_job_id = ?",
                    (str(replacement.id), str(run_id), str(job.id)))
                return self.get(project_id, run_id)
            if job.payload.video_spec != spec:
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
                repair_allowance=row["repair_allowance"], source_usage_run_id=UUID(row["id"]),
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
        self, project_id: UUID, run_id: UUID, *, voice_profile_id: UUID | None,
        capability_profile_id: UUID,
        cross_ip_voice_authorization_reference: str | None = None,
    ) -> ProductionRun | None:
        """Link one authorized existing Voice Job; never invoke a provider here."""
        from app.voice_plan import resolve_project_voice_profile
        profile = resolve_project_voice_profile(self.db,project_id,voice_profile_id,cross_ip_voice_authorization_reference)
        voice_profile_id = profile.id
        authorization_reference = (profile.consent.authorization_reference
            or cross_ip_voice_authorization_reference or f"voice-profile-consent:{profile.id}")
        with self.db.transaction(immediate=True):
            self._require_source_usage(project_id, run_id)
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
                    or job.payload.cross_ip_voice_authorization_reference != cross_ip_voice_authorization_reference
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
                repair_allowance=row["repair_allowance"], source_usage_run_id=UUID(row["id"]),
            )
            if self._start_stage(preflight) != "awaiting_master":
                raise ProductionRunNotReady(("approved_master_already_available",))
            if any(reason in preflight.stop_reasons for reason in (
                "budget_call_limit", "budget_amount_limit", "unknown_cost_requires_budget_authorization",
            )):
                raise ProductionRunNotReady(("voice_budget_not_authorized",))
            if draft.narration_performance_plan is not None:
                raise ProductionRunNotReady(("voice_performance_plan_dispatch_not_implemented",))
            capability = ProviderMachineCapabilityProfileRepository(self.db).get(capability_profile_id)
            if any(ClipRepository(self.db).get(clip_id) is None for clip_id in profile.reference_clip_ids):
                raise ProductionRunNotReady(("voice_reference_clip_missing",))
            if (capability is None or capability.capability != "voice" or capability.provider != profile.provider
                or not local_generation_ready(capability)):
                raise ProductionRunNotReady(("exact_voice_capability_not_verified",))
            if not local_generation_licensed(capability):
                raise ProductionRunNotReady(("voice_provider_license_scope_unverified",))
            job_id, now = uuid4(), datetime.now(timezone.utc)
            payload = VoiceGenerationJobPayload(
                project_id=project_id, voice_profile_id=voice_profile_id,
                cross_ip_voice_authorization_reference=cross_ip_voice_authorization_reference,
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
        if preflight.status == "ready_for_authorized_dispatch" and preflight.reusable_master_audio_id is not None:
            ProductionRunService._require_ready_lane(preflight)
            return "render"
        voice_only_stops = {"provider_license_scope_unverified", "voice_capability_not_verified"}
        if (
            preflight.reusable_master_audio_id is None
            and preflight.preliminary_edit_plan is not None
            and set(preflight.stop_reasons).issubset(voice_only_stops)
            and all(ProductionRunService._ready_existing_visual(item) for item in preflight.scenes)
            and {action.kind for action in preflight.actions if action.required} == {"voice", "render"}
        ):
            return "awaiting_master"
        talking = tuple(item for item in preflight.scenes if item.production_need == "new_talking")
        allowed_stops = {
            "provider_license_scope_unverified", "talking_capability_not_verified",
            "unknown_cost_requires_budget_authorization", "budget_call_limit", "budget_amount_limit",
            *(f"talking_source_not_bound:{item.scene_id}" for item in talking),
        }
        if (
            talking
            and all(item.planned_source_kind is SourceKind.AI_VIDEO and item.selected_candidate is None
                    and item.suitability == "unknown" for item in talking)
            and all(item.production_need == "new_talking" or
                    (item.production_need == "none" and ProductionRunService._ready_existing_visual(item))
                    for item in preflight.scenes)
            and set(preflight.stop_reasons).issubset(allowed_stops | {"voice_capability_not_verified"})
            and all(f"talking_source_not_bound:{item.scene_id}" in preflight.stop_reasons for item in talking)
            and {action.kind for action in preflight.actions if action.required} == (
                {"talking", "render"} if preflight.reusable_master_audio_id is not None else {"voice", "talking", "render"}
            )
        ):
            return "awaiting_talking" if preflight.reusable_master_audio_id is not None else "awaiting_master"
        raise ProductionRunNotReady(preflight.stop_reasons or ("production_dependencies_not_implemented",))

    def _enqueue_render(self, project, draft, preflight: ProductionPreflight, run_id: UUID, now: datetime) -> UUID:
        spec = self._resolved_render_spec(project, draft, preflight, production_run_id=run_id)
        try:
            SourceUsageService(self.db).require_available(project.id, SourceUsageService(self.db).spec_uses(spec), run_id=run_id)
        except SourceUsageConflict as exc:
            raise ProductionRunNotReady((exc.reason,)) from exc
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

    def _resolved_render_spec(self, project, draft, preflight: ProductionPreflight, *, production_run_id: UUID | None = None):
        assert preflight.preliminary_edit_plan is not None
        selections = {item.scene_plan_id: item.selected_candidate for item in preflight.scenes}
        if any(value is None for value in selections.values()):
            raise ProductionRunNotReady(("visual_selection_incomplete",))
        try:
            spec = VideoSpecAssembler(
                AssetRepository(self.db), ClipRepository(self.db), ImageAssetRepository(self.db),
                AudioAssetRepository(self.db),
                production_run_id=production_run_id,
            ).assemble(
                project, draft.scenes, selections,
                master_narration_asset_id=preflight.reusable_master_audio_id,
                narration_required=True, edit_plan=preflight.preliminary_edit_plan,
                terminal_talking_delivery=False,
            )
        except VideoSpecAssemblyError as exc:
            raise ProductionRunNotReady((f"video_spec_assembly_failed:{exc}",)) from exc
        return spec

    @staticmethod
    def _ready_existing_visual(item) -> bool:
        return (item.planned_source_kind is SourceKind.TYPOGRAPHY and item.suitability == "known_deterministic") or (
            item.planned_source_kind in {SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET, SourceKind.STOCK}
            and item.suitability in {"reviewed_source_interval", "analyzed_source_interval"}) or (
            item.planned_source_kind is SourceKind.AI_VIDEO and item.suitability == "reviewed_native_planned_run")

    @staticmethod
    def _require_ready_lane(preflight: ProductionPreflight, *, allow_planned: bool = False) -> None:
        if preflight.status != "ready_for_authorized_dispatch":
            raise ProductionRunNotReady(preflight.stop_reasons or ("preflight_blocked",))
        if preflight.reusable_master_audio_id is None:
            raise ProductionRunNotReady(("approved_master_narration_required",))
        if preflight.preliminary_edit_plan is None:
            raise ProductionRunNotReady(("preliminary_edit_plan_required",))
        if any(not ProductionRunService._ready_existing_visual(item)
               and not (allow_planned and item.planned_source_kind is SourceKind.AI_VIDEO
                        and item.suitability in {"reviewed_native_planned_run", "qa_verified_planned_output"})
               for item in preflight.scenes):
            raise ProductionRunNotReady(("suitable_visual_lane_required",))
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


def production_run_jobs(db: Database, run_id: UUID):
    """Read the current durable references; an unrelated pending Job is never a candidate."""
    row = db.connection.execute(
        "SELECT * FROM production_runs WHERE id = ?", (str(run_id),),
    ).fetchone()
    if row is None:
        raise ValueError("production run does not exist")
    references = [(row["voice_job_id"], JobType.GENERATE_VOICE),
                  (row["voice_qa_job_id"], JobType.VERIFY_VOICE)]
    for binding in json.loads(row["talking_source_bindings"]):
        references.extend(((binding.get("talking_job_id"), JobType.GENERATE_TALKING),
                           (binding.get("talking_qa_job_id"), JobType.VERIFY_TALKING)))
    references.append((row["render_job_id"], JobType.RENDER))
    jobs, seen = [], set()
    repository = JobRepository(db)
    for job_id, expected_type in references:
        if job_id is None or job_id in seen:
            continue
        seen.add(job_id)
        job = repository.get(UUID(job_id))
        if job is None or str(job.project_id) != row["project_id"] or job.type is not expected_type:
            raise ValueError("production run Job reference is missing or mismatched")
        jobs.append(job)
    return row, jobs
