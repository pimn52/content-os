"""Minimal local FastAPI entrypoint for the Content OS scaffold."""
from __future__ import annotations

import os
import secrets
import sqlite3
import hashlib
import json
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Literal
from uuid import UUID, uuid4

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from app.domain.models import AudioAssetTranscriptionJobPayload, ClipIndexJobPayload

from app.budget import BudgetLimitError, ProviderCallError, ProviderCallLedger, is_over_budget, snapshot
from app.planning_feasibility import planning_feasibility, planning_identity_context
from app.planning_input import creator_planning_brief
from app.search.index import _lexical_terms
from app.costs import ProviderCostEstimator, UnknownProviderCostEstimator, estimate_selected_candidates, reduce_candidate_cost
from app.db import AccountConnectionRepository, AnalysisResultRepository, AssetRepository, AssetUsageRepository, AudioAssetRepository, BudgetPolicyRepository, ClipRepository, ContentOpportunityRepository, Database, FeedbackRepository, HistoricalContentRepository, ImageAssetRepository, IPProfileRepository, JobRepository, ProjectDraftRepository, ProjectRepository, ProviderCallRepository, ProviderMachineCapabilityProfileRepository, ProviderMachineSettingRepository, PublicationRepository, ShootTaskRepository, TalkingProfileRepository, TalkingRunRepository, TalkingSliceSeriesContinuityReviewRepository, TalkingSliceSeriesRepository, VoiceProfileRepository
from app.domain.models import AccountConnection, AnalysisResultBundle, Asset, AssetUsageEvent, AudioAsset, BudgetPolicy, CandidateAsset, Clip, ConsentRecord, ContentFeedback, ContentOpportunity, CostCategory, CostEstimate, CostReductionSuggestion, DraftRoute, EditPlan, HistoricalContent, ImageAsset, IPProfile, Job, JobStatus, JobType, NarrationDeliveryPlan, NarrationPace, NarrationPerformanceCue, NarrationPerformancePlan, NarrationPerformancePlanSource, NarrationPerformanceSuggestions, Project, ProjectDraft, ProjectDraftRevision, ProjectFormat, ProviderCallRecord, ProviderMachineCapabilityProfile, ProviderMachineSetting, PublicationRecord, RationalFps, RenderVideoJobPayload, ScenePlan, ShootTask, SourceKind, TalkingGenerationJobPayload, TalkingPerformanceBrief, TalkingProfile, TalkingReferenceAssessment, TalkingReferenceSelection, TalkingRun, TalkingRunChildEvidence, TalkingSliceSeries, TalkingSliceSeriesContinuityReview, TalkingSliceSeriesRecovery, UsageCost, VerifiedVerticalDerivationRequest, VideoSpec, VisualStyleTokens, VoiceBoundaryAlignmentJobPayload, VoiceGenerationJobPayload, VoiceHumanReview, VoicePaceCandidateJobPayload, VoiceProfile, VoiceQaJobPayload, VoiceReferenceWindowSelection, VoiceReviewOutcome
from app.assembly import NarrationRequiredForNewScript, VideoSpecAssembler, VideoSpecAssemblyError
from app.asset_library import asset_library_page
from app.external_media_api import router as external_media_router
from app.web_discovery_api import router as web_discovery_router
from app.video_production_api import router as video_production_router
from app.m1_gate import m1_gate_page
from app.master_narration import MasterNarrationComposer, MasterNarrationCompositionError
from app.media import AudioAssetNotFound, AudioImportError, AudioImporter, AudioTranscriptPersistence, ClipTranscriptPersistence, FFProbeAdapter, ImageImportError, ImageImporter, MediaImportError, MediaImporter, NoClipsForAsset, ProbeError, SourceAudioDerivationError, SourceAudioDerivationService, SubtitleParseError, VerticalDerivationConflict, VerticalDerivationProcessError, VerticalDerivationValidationError, VerticalSourceDerivationService, parse_subtitle_file
from app.media.crop_assessment import CropAssessment, CropAssessmentRepository, CropProposal
from app.workspace import workspace_page
from app.voice_delivery import VoiceDeliveryTextError, validate_voice_delivery_text
from app.voice_performance import authorized_voice_reference_choices, is_confirmed_voice_reference, resolve_authorized_voice_reference_choice, select_voice_reference_windows, voice_reference_window_selection, VoicePerformancePlanningError
from app.jobs.targets import AssetJobIdempotencyConflict, AssetJobTargetStore, AudioAssetJobTargetStore, UnsupportedAssetJobType
from app.providers.embedding import (
    EmbeddingAuthenticationError,
    EmbeddingConfigurationError,
    EmbeddingConnectionError,
    EmbeddingHTTPError,
    EmbeddingInputError,
    EmbeddingProvider,
    EmbeddingProviderResponseError,
    EmbeddingRateLimitError,
    EmbeddingTimeout,
    OpenAICompatibleEmbeddingProvider,
)
from app.providers.scene_planner import (
    validate_audience_boundary,
    validate_presentation_requirements,
    OpenAICompatibleScenePlanner,
    ScenePlanner,
    ScenePlannerAuthenticationError,
    ScenePlannerConfigurationError,
    ScenePlannerConnectionError,
    ScenePlannerHTTPError,
    ScenePlannerInputError,
    ScenePlannerProviderResponseError,
    ScenePlannerRateLimitError,
    ScenePlannerTimeout,
)
from app.provider_execution import (
    ProviderExecutionAccountingFailure,
    ProviderExecutionIdempotencyConflict,
    ProviderExecutionInProgress,
    ProviderExecutionRecordedFailure,
    ProviderExecutionReplayUnavailable,
    ProviderExecutionService,
    runtime_provider_identity,
)
from app.project_drafts import save_project_draft as persist_project_draft
from app.scene_planning import (ScenePlanRequest,ScenePlanResponse,generate_scene_plan,PlanningContextChanged,
    _scene_planning_context,_scene_planner_from_env,_scene_planner_error_code)
from app.production_preflight import PreflightInputError, ProductionPreflight, StaleProductionPreflight, build_production_preflight
from app.production_runs import ProductionRun, ProductionRunConflict, ProductionRunNotReady, ProductionRunReference, ProductionRunService, TalkingSourceContext, is_planned_talking_collection
from app.talking.planned_admission import PlannedTalkingAdmissionError, PlannedTalkingAdmissionService
from app.talking.repair import TalkingRepairPlan, TalkingRepairRecord, TalkingRepairRequest, TalkingRepairService
from app.voice_repair import VoiceRepairPlan, VoiceRepairRequest, VoiceRepairService
from app.presentation_repair import PresentationObservationRequest, PresentationRepairRequest, PresentationRepairService
from app.feedback_learning import FeedbackLearningService, PreferenceCandidateRequest, PreferenceStateRequest, CreatorPreferenceSelectionRequest, apply_preferences
from app.source_use_constraints import SourceUseConstraintService, RetainedRejectionRequest, UseConstraintStateRequest, digest as use_constraint_digest
from app.narration_performance import NarrationPerformancePlanError, build_narration_performance_plan, compile_narration_delivery_plan, suggest_narration_performance, validate_narration_performance_plan
from app.search import ClipEmbeddingIndexer, ClipTextSearchService, EmbeddingIndexError, IndexError
from app.routing import AssetRouter, CapabilityFeature, CapabilityProfile, CapabilityReadiness, CommercialStatus, EvidenceProvenance, EvidenceStatus, ExecutionOverride, ExecutionSafetyContext, FeatureSupport, OperatingBound, ResolutionSource, RoutingConfigurationError, RoutingInputError, find_provider_settings_schema, find_unique_provider_settings_schema, list_provider_settings_schemas, resolve_execution, resolve_feature_support, resolve_verified_operating_limit, validate_schema_values
from app.renderer import LocalResourceError, RemotionRenderer, RenderInputError, RenderProcessError, RenderTimeout, UnauthorizedVisualError, render_output_path
from app.media.local_paths import existing_local_media_file
from app.media.viewport_feasibility import ViewportFeasibility, ViewportFeasibilityRequest, inspect_viewport
from app.runtime import RuntimeCapability, inspect_runtime_capabilities, resolve_local_executable, scene_planner_api_key
from app.talking import TalkingRunAssembler, TalkingRunAssemblyError, TalkingSlicePlanningError, assess_talking_slice_series, plan_source_forward_reference_windows, plan_talking_audio_slice, plan_talking_audio_slice_series, select_talking_reference
from app.execution_admission import UseAdmissionError
from app.media.audio_access import AudioAccess, require_ordinary_media
from app.talking_qa import TalkingHumanReview, TalkingQaError, apply_talking_human_review
from app.domain.models import TalkingReviewDimensions, TalkingReviewFinding, TalkingReviewConcernAnswer
from app.voice_takes import VoiceTakeComposer
from app.voice_observation import VoiceObservationError, observe_voice_performance
from app.voice_boundary_alignment import voice_asset_project_id
from app.voice_qa import (
    VoiceQaError,
    apply_voice_human_review,
    voice_human_review_status,
)


class JobEnqueueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=1, max_length=500)
    project_id: UUID | None = None
    clip_ids: list[UUID] | None = Field(default=None, min_length=1)
    index_embeddings: bool = Field(default=True, strict=True)


class AudioAssetTranscriptionEnqueueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=1, max_length=500)
    project_id: UUID


class JobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    type: JobType
    status: JobStatus
    attempt: int
    idempotency_key: str
    project_id: UUID | None
    target_asset_id: UUID | None
    started_at: AwareDatetime | None = None
    created_at: datetime
    updated_at: datetime
    error_code: str | None
    error_message: str | None
    render_id: UUID | None = None
    download_url: str | None = None
    preview_url: str | None = None


class TalkingSliceSeriesChildReviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    series_index: int
    job_id: UUID
    job_status: JobStatus | None
    output_asset_id: UUID | None
    automated_qa_state: str | None
    human_review_state: str | None
    blockers: list[str]


class TalkingSliceSeriesReviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    readiness: str
    ready_for_human_continuity_review: bool
    children: list[TalkingSliceSeriesChildReviewResponse]
    blockers: list[str]
    continuity_review_state: str
    continuity_review_evidence_reference: str | None
    continuity_review_findings: list[str]
    planned_origin_sha256: str | None = None
    preview_asset_id: UUID | None = None
    preview_sha256: str | None = None
    review_policy_version: Literal[1, 2] = 1
    required_dimensions: list[str] = Field(default_factory=list)
    review_concerns: list[dict[str, Any]] = Field(default_factory=list)


class TalkingSliceSeriesContinuityReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved: bool
    evidence_reference: str = Field(min_length=1, max_length=2_000)
    findings: list[str] = Field(default_factory=list, max_length=100)
    preview_asset_id: UUID | None = None
    preview_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    review_policy_version: Literal[1, 2] = 1
    dimensions: TalkingReviewDimensions | None = None
    scoped_findings: list[TalkingReviewFinding] = Field(default_factory=list, max_length=100)
    concern_answers: list[TalkingReviewConcernAnswer] = Field(default_factory=list, max_length=100)


class TalkingSliceSeriesContinuityReviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    series_id: UUID
    approved: bool
    evidence_reference: str
    findings: list[str]
    child_job_ids: list[UUID]
    planned_origin_sha256: str | None = None
    preview_asset_id: UUID | None = None
    preview_sha256: str | None = None
    preview_qa_sha256: str | None = None
    reviewed_at: datetime
    review_policy_version: Literal[1, 2] = 1
    dimensions: TalkingReviewDimensions | None = None
    scoped_findings: list[TalkingReviewFinding] = Field(default_factory=list)


class TalkingReviewConcernRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=1, max_length=500)
    preview_asset_id: UUID
    preview_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    finding: TalkingReviewFinding
    evidence_reference: str = Field(min_length=1, max_length=2000)


class TalkingReviewConcernAnswersRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preview_asset_id: UUID
    preview_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    answers: list[TalkingReviewConcernAnswer] = Field(min_length=1, max_length=100)


class TalkingRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    project_id: UUID
    series_id: UUID
    master_narration_audio_id: UUID
    master_start_ms: int
    master_end_ms: int
    authorized_reference_clip_id: UUID
    child_evidence: list[TalkingRunChildEvidence]
    continuity_review_id: UUID
    assembled_asset_id: UUID
    assembled_clip_id: UUID
    automated_qa_state: Literal["verified"]
    admission_state: Literal["admitted"]
    planned_origin_sha256: str | None = None
    reviewed_preview_sha256: str | None = None
    created_at: datetime
    review_policy_version: Literal[1, 2] = 1


class ClipSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=10_000)
    project_id: UUID | None = None
    top_k: int = Field(default=10, ge=1, le=100)
    asset_id: UUID | None = None
    orientation: ProjectFormat | None = None
    talking_candidate: bool | None = None


class ClipSearchHitResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    clip: Clip
    score: float = Field(ge=-1, le=1)
    score_basis: Literal["embedding_similarity", "lexical_overlap"]




class ProductionPreflightRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    style_tokens: VisualStyleTokens = Field(default_factory=VisualStyleTokens)
    expected_fingerprint: str | None = Field(default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    repair_allowance: int = Field(default=1, ge=0, le=2)
    crop_proposals: tuple[CropProposal, ...] = ()


class ProductionRunStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=1, max_length=500)
    expected_fingerprint: str | None = Field(default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    style_tokens: VisualStyleTokens = Field(default_factory=VisualStyleTokens)
    repair_allowance: int = Field(default=1, ge=0, le=2)
    talking_review_policy_version: Literal[1, 2] = 1

    @model_validator(mode="after")
    def require_fingerprint(self):
        if self.expected_fingerprint is None:
            raise ValueError("expected_fingerprint_required")
        return self


class ProductionRunVoiceDispatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    voice_profile_id: UUID | None = None
    cross_ip_voice_authorization_reference: str | None = Field(default=None, min_length=1, max_length=500)
    capability_profile_id: UUID


class ProductionRunTalkingSourceBindRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene_plan_id: UUID
    talking_profile_id: UUID
    reference_clip_id: UUID
    capability_profile_id: UUID
    master_start_ms: int = Field(ge=0)
    master_end_ms: int = Field(gt=0)
    brief: TalkingPerformanceBrief = Field(default_factory=TalkingPerformanceBrief)


class ProductionRunTalkingDispatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene_plan_id: UUID


class ProductionRunTalkingBatchDispatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    talking_profile_id: UUID | None = None
    reference_clip_id: UUID | None = None
    capability_profile_id: UUID | None = None
    brief: TalkingPerformanceBrief = Field(default_factory=TalkingPerformanceBrief)

    @model_validator(mode="after")
    def complete_source_selection(self):
        choices = (self.talking_profile_id, self.reference_clip_id, self.capability_profile_id)
        if any(value is not None for value in choices) and not all(value is not None for value in choices):
            raise ValueError("talking_source_selection_incomplete")
        return self


class ProductionRunTalkingQaAdvanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene_plan_id: UUID




class AssetRouteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenes: list[ScenePlan] = Field(min_length=1, max_length=100)
    max_candidates: int = Field(default=3, ge=1, le=20)
    capture_gap_threshold: float = Field(default=0.45, ge=0, le=1)
    master_narration_asset_id: UUID | None = None
    crop_proposals: tuple[CropProposal, ...] = ()


class ShootListInstruction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene_plan_id: UUID
    scene_id: str = Field(min_length=1, max_length=100)
    what_to_shoot: str = Field(min_length=1, max_length=2_000)
    framing: str = Field(min_length=1, max_length=200)
    duration_ms: int = Field(gt=0, le=300_000)
    requires_speaking: bool
    speaking_note: str = Field(min_length=1, max_length=500)
    fallback: str = Field(min_length=1, max_length=1_000)


class ShootTaskCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene_plan_id: UUID
    scene_id: str = Field(min_length=1, max_length=100)
    what_to_shoot: str = Field(min_length=1, max_length=2_000)
    framing: str = Field(min_length=1, max_length=200)
    duration_ms: int = Field(gt=0, le=300_000)
    requires_speaking: bool


class ShootTaskUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["confirmed", "dismissed"]


class AssetRouteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene_plan_id: UUID
    candidates: tuple[CandidateAsset, ...]
    shoot_list: tuple[ShootListInstruction, ...] = ()


class VideoSpecAssemblyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenes: list[ScenePlan] = Field(min_length=1, max_length=1_000)
    selections: list[CandidateAsset] = Field(min_length=1, max_length=1_000)
    explicit_scene_ids: list[UUID] = Field(default_factory=list, max_length=1_000)
    narration_asset_ids: dict[UUID, UUID] = Field(default_factory=dict, max_length=1_000)
    master_narration_asset_id: UUID | None = None
    narration_required: bool = False
    edit_plan: EditPlan | None = None


class RenderRequest(BaseModel):
    """One already-assembled spec, or the inputs needed to assemble one locally."""

    model_config = ConfigDict(extra="forbid")
    video_spec: VideoSpec | None = None
    scenes: list[ScenePlan] | None = Field(default=None, min_length=1, max_length=1_000)
    selections: list[CandidateAsset] | None = Field(default=None, min_length=1, max_length=1_000)
    explicit_scene_ids: list[UUID] = Field(default_factory=list, max_length=1_000)
    narration_asset_ids: dict[UUID, UUID] = Field(default_factory=dict, max_length=1_000)
    master_narration_asset_id: UUID | None = None
    edit_plan: EditPlan | None = None

    @model_validator(mode="after")
    def one_render_input_shape(self) -> "RenderRequest":
        has_spec = self.video_spec is not None
        has_assembly = self.scenes is not None or self.selections is not None
        if has_spec == has_assembly:
            raise ValueError("supply exactly one of video_spec or scenes with selections")
        if has_assembly and (self.scenes is None or self.selections is None):
            raise ValueError("scenes and selections must be supplied together")
        if has_spec and self.explicit_scene_ids:
            raise ValueError("explicit_scene_ids is only valid with scenes and selections")
        if has_spec and self.narration_asset_ids:
            raise ValueError("narration_asset_ids is only valid with scenes and selections")
        if has_spec and self.master_narration_asset_id is not None:
            raise ValueError("master_narration_asset_id is only valid with scenes and selections")
        if has_spec and self.edit_plan is not None:
            raise ValueError("edit_plan is only valid with scenes and selections")
        return self


class RenderResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: UUID
    render_id: UUID
    video_spec: VideoSpec
    download_url: str
    preview_url: str


class RenderJobEnqueueRequest(RenderRequest):
    """Strict render input plus a caller-selected idempotency key."""

    idempotency_key: str = Field(min_length=1, max_length=500)


class ProjectCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=500)
    topic: str = Field(min_length=1, max_length=5_000)
    creator_name: str = Field(default="本地创作者", min_length=1, max_length=200)
    ip_profile_id: UUID | None = None
    presentation_style: Literal["auto", "creator_led"] = "auto"
    delivery_intent: Literal["publish", "draft"] = "publish"


class ProjectPresentationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    presentation_style: Literal["auto", "creator_led"] | None = None
    delivery_intent: Literal["publish", "draft"] | None = None
    format_policy: Literal["auto", "fixed"] | None = None
    format: Literal["vertical", "horizontal"] | None = None


class IPProfileUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    creator_name: str = Field(min_length=1, max_length=200)
    default_voice_profile_id: UUID | None = None
    domains: list[str] = Field(default_factory=list, max_length=30)
    audience: str | None = Field(default=None, max_length=1_000)
    topics: list[str] = Field(default_factory=list, max_length=100)
    knowledge: list[str] = Field(default_factory=list, max_length=200)
    opinions: list[str] = Field(default_factory=list, max_length=200)
    vocabulary: list[str] = Field(default_factory=list, max_length=200)
    style_notes: list[str] = Field(default_factory=list, max_length=100)
    avoided_expressions: list[str] = Field(default_factory=list, max_length=200)
    boundaries: list[str] = Field(default_factory=list, max_length=100)
    common_hooks: list[str] = Field(default_factory=list, max_length=100)
    metadata: dict[str, Any] = Field(default_factory=dict)


class IPProfileRevisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int
    created_at: datetime
    profile: IPProfile


class AssetIdentityUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    identity: Literal["creator", "other", "none", "unknown"]
    reuse_role: Literal["ordinary", "meme", "signature"] | None = None
    ip_profile_id: UUID | None = None
    talking_profile_id: UUID | None = None
    motion_reference: bool | None = None


class AssetStageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["not_started", "pending", "running", "completed", "failed", "cancelled"]
    job_id: UUID | None = None
    error_code: str | None = None


class AssetReadinessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_id: UUID
    stages: dict[str, AssetStageResponse]


class VerticalDerivationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset: Asset
    clip: Clip


class MediaImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_path: str = Field(min_length=1, max_length=4_000)
    authorization_reference: str = Field(min_length=1, max_length=500)
    source_kind: str = Field(default="user_asset", pattern=r"^(user_asset|historical_asset)$")
    recursive: bool = True


MAX_UPLOAD_BYTES = 5 * 1024 * 1024 * 1024
_VIDEO_SUFFIXES = {
    ".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".mts", ".m2ts", ".ts",
}
_AUDIO_UPLOAD_SUFFIXES = {
    ".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".webm",
}


class _LimitedUploadStream:
    """Bound an UploadFile stream while preserving the importer read contract."""

    def __init__(self, stream: Any, limit: int) -> None:
        self.stream = stream
        self.limit = limit
        self.total = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self.stream.read(size)
        self.total += len(chunk)
        if self.total > self.limit:
            raise ValueError("uploaded media exceeds the local size limit")
        return chunk


class InboxScanRequest(MediaImportRequest):
    """A local polling scan; it never uploads or authorizes an external source."""

    enqueue_analysis: bool = True


class InboxScanError(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_path: str
    detail: str


class InboxScanResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_path: str
    discovered: int = Field(ge=0)
    imported: int = Field(ge=0)
    existing: int = Field(ge=0)
    assets: list[Asset] = Field(default_factory=list)
    analysis_jobs: list[JobResponse] = Field(default_factory=list)
    errors: list[InboxScanError] = Field(default_factory=list)


class ImageImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_path: str = Field(min_length=1, max_length=4_000)
    authorization_reference: str = Field(min_length=1, max_length=500)
    source_kind: str = Field(default="screenshot", pattern=r"^(screenshot|chart)$")
    recursive: bool = True


class AudioImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_path: str = Field(min_length=1, max_length=4_000)
    authorization_reference: str = Field(min_length=1, max_length=500)
    source_kind: str = Field(default="user_asset", pattern=r"^(user_asset|historical_asset)$")
    language: str | None = Field(default=None, max_length=20)
    recursive: bool = True


class SourceAudioDeriveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_id: UUID
    clip_id: UUID
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def ordered_interval(self):
        if self.end_ms <= self.start_ms:
            raise ValueError("source_audio_interval_invalid")
        return self


class TranscriptImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_path: str = Field(min_length=1, max_length=4_000)
    source_reference: str = Field(min_length=1, max_length=500)


class TranscriptImportResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_id: UUID
    source_reference: str
    segment_count: int = Field(ge=1)
    updated_clip_count: int = Field(ge=1)


class AudioTranscriptImportResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    audio_asset_id: UUID
    source_reference: str
    segment_count: int = Field(ge=1)


class DraftUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    script: str | None = Field(default=None, max_length=100_000)
    topic: str | None = Field(default=None, max_length=5_000)
    scenes: list[ScenePlan] = Field(default_factory=list, max_length=1_000)
    routes: list[DraftRoute] = Field(default_factory=list, max_length=1_000)
    confirmed: list[CandidateAsset] = Field(default_factory=list, max_length=1_000)
    video_spec: VideoSpec | None = None


class AssetUsageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    media_id: UUID
    media_kind: Literal["video", "image", "audio"]
    clip_id: UUID | None = None


class AssetUsageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    output_version: str = Field(min_length=1, max_length=200)
    events: list[AssetUsageInput] = Field(min_length=1, max_length=1_000)


class PublicationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    output_version: str = Field(min_length=1, max_length=200)
    platform: str = Field(min_length=1, max_length=100)
    published_at: datetime
    content_url: str | None = Field(default=None, max_length=2_000)
    content_external_id: str | None = Field(default=None, max_length=500)
    metrics: dict[str, Any] = Field(default_factory=dict)
    metric_source: str | None = Field(default=None, max_length=200)
    observation_window_days: int | None = Field(default=None, ge=0, le=10_000)


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    output_version: str = Field(min_length=1, max_length=200)
    accepted: bool
    changed_fields: list[str] = Field(default_factory=list, max_length=100)
    rejection_reason: str | None = Field(default=None, max_length=2_000)
    notes: str | None = Field(default=None, max_length=10_000)


class ContentOpportunityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_type: Literal["manual", "historical_content", "account_signal"]
    source_ref: str = Field(min_length=1, max_length=500)
    title: str = Field(min_length=1, max_length=500)
    observed_at: AwareDatetime
    fit_reason: str = Field(min_length=1, max_length=5_000)
    angle: str = Field(min_length=1, max_length=5_000)
    uncertainty: str | None = Field(default=None, max_length=5_000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=100)


class OpportunityStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["new", "used", "dismissed"]


class AccountConnectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(min_length=1, max_length=100)
    account_external_id: str = Field(min_length=1, max_length=500)
    display_name: str | None = Field(default=None, max_length=200)
    connected_at: AwareDatetime


class HistoricalContentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_connection_id: UUID
    external_id: str = Field(min_length=1, max_length=500)
    title: str = Field(min_length=1, max_length=500)
    published_at: AwareDatetime | None = None
    description: str | None = Field(default=None, max_length=10_000)
    transcript: str | None = Field(default=None, max_length=100_000)
    metrics: dict[str, int | float] = Field(default_factory=dict)


class VoiceProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID = Field(default_factory=uuid4)
    ip_profile_id: UUID | None = None
    name: str = Field(min_length=1, max_length=200)
    provider: str = Field(min_length=1, max_length=100)
    provider_profile_id: str = Field(min_length=1, max_length=500)
    reference_clip_ids: list[UUID] = Field(min_length=1, max_length=100)
    consent: ConsentRecord
    language: str | None = Field(default=None, pattern=r"^[a-z]{2,3}(-[A-Z]{2})?$")
    created_at: AwareDatetime


class ConfirmVoiceReferenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    complete_sentence_confirmed: Literal[True]
    exact_ref_text_confirmed: Literal[True]
    voice_suitability_confirmed: Literal[True]
    voice_suitability_basis: str = Field(min_length=8, max_length=400)
    asset_id: UUID | None = None
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, gt=0)
    reference_window: VoiceReferenceWindowSelection | None = None
    ref_text: str = Field(min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def one_source_selection(self) -> "ConfirmVoiceReferenceRequest":
        legacy = (self.asset_id, self.start_ms, self.end_ms)
        if self.reference_window is None and any(value is None for value in legacy):
            raise ValueError("legacy Voice reference confirmation requires asset_id and interval")
        if self.reference_window is not None and any(value is not None for value in legacy):
            raise ValueError("Voice reference confirmation must use either a selected window or a legacy Asset interval")
        return self


class VoiceGenerationJobRequest(BaseModel):
    """One persisted, explicit voice-clone request with no credentials."""

    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=1, max_length=500)
    voice_profile_id: UUID | None = None
    cross_ip_voice_authorization_reference: str | None = Field(default=None, min_length=1, max_length=500)
    text: str = Field(min_length=1, max_length=100_000)
    delivery_text: str | None = Field(default=None, min_length=1, max_length=100_000)
    language: str | None = Field(default=None, pattern=r"^[a-z]{2,3}(-[A-Z]{2})?$")
    use_draft_performance_plan: bool = False
    reference_window: VoiceReferenceWindowSelection | None = None


class NarrationPerformancePlanRequest(BaseModel):
    """Editable delivery direction, deliberately separate from Voice knobs."""

    model_config = ConfigDict(extra="forbid")
    delivery_goal: str = Field(min_length=1, max_length=1_000)
    overall_pace: NarrationPace = NarrationPace.CONVERSATIONAL
    cues: list[NarrationPerformanceCue] = Field(min_length=1, max_length=1_000)
    source: NarrationPerformancePlanSource = NarrationPerformancePlanSource.USER
    evidence_refs: list[str] = Field(default_factory=list, max_length=100)


class VoiceQaJobRequest(BaseModel):
    """One local real-ASR QA request for a generated narration asset."""

    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=1, max_length=500)
    narration_audio_id: UUID
    target_text: str = Field(min_length=1, max_length=100_000)


class VoicePaceCandidateJobRequest(BaseModel):
    """One explicit, bounded local pace candidate; no provider speed claim."""

    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=1, max_length=500)
    narration_audio_id: UUID
    profile: Literal["gentle_slower"] = "gentle_slower"


class VoiceBoundaryAlignmentJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=1, max_length=500)
    narration_audio_id: UUID


class VoiceHumanReviewRequest(BaseModel):
    """One U-Voice outcome; server time owns the final review timestamp."""

    model_config = ConfigDict(extra="forbid")
    approved: bool
    evidence_reference: str = Field(min_length=1, max_length=500)
    findings: list[str] = Field(min_length=1, max_length=100)
    likeness: VoiceReviewOutcome
    naturalness: VoiceReviewOutcome
    emphasis: VoiceReviewOutcome
    pace: VoiceReviewOutcome
    pauses: VoiceReviewOutcome
    rhythm: VoiceReviewOutcome

    def review(self, *, reviewed_at: datetime) -> VoiceHumanReview:
        return VoiceHumanReview(
            approved=self.approved,
            evidence_reference=self.evidence_reference,
            findings=self.findings,
            likeness=self.likeness,
            naturalness=self.naturalness,
            emphasis=self.emphasis,
            pace=self.pace,
            pauses=self.pauses,
            rhythm=self.rhythm,
            reviewed_at=reviewed_at,
        )


class TalkingHumanReviewRequest(BaseModel):
    """One asset-specific U-Talking judgment after independent technical QA."""

    model_config = ConfigDict(extra="forbid")
    approved: bool
    evidence_reference: str = Field(min_length=1, max_length=500)
    findings: list[str] = Field(min_length=1, max_length=100)

    def review(self) -> TalkingHumanReview:
        return TalkingHumanReview(
            approved=self.approved,
            evidence_reference=self.evidence_reference,
            findings=tuple(self.findings),
        )


class MasterNarrationCompositionRequest(BaseModel):
    """Ordered source takes; their existing Voice QA remains the first gate."""

    model_config = ConfigDict(extra="forbid")
    take_audio_ids: list[UUID] = Field(min_length=1, max_length=100)
    compact_quiet_seams: bool = False


class TalkingReferenceSelectionRequest(BaseModel):
    """Requirements for finding a usable existing Talking reference."""

    model_config = ConfigDict(extra="forbid")
    brief: TalkingPerformanceBrief


class TalkingReferenceAssessmentRequest(BaseModel):
    """Sourced assessment; values are observed, never generated instructions."""

    model_config = ConfigDict(extra="forbid")
    assessment: TalkingReferenceAssessment


class TalkingProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID = Field(default_factory=uuid4)
    ip_profile_id: UUID | None = None
    name: str = Field(min_length=1, max_length=200)
    provider: str = Field(min_length=1, max_length=100)
    provider_profile_id: str | None = Field(default=None, max_length=500)
    reference_clip_ids: list[UUID] = Field(default_factory=list, max_length=100)
    consent: ConsentRecord
    created_at: AwareDatetime


class NextSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str
    suggestion: str
    evidence_refs: list[str]


class BudgetPolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    max_amount: Decimal | None = Field(default=None, ge=Decimal("0"), max_digits=12, decimal_places=4)
    max_calls: int | None = Field(default=None, ge=0, strict=True)
    allow_unknown_cost: bool = False


class ProviderMachineSettingRequest(BaseModel):
    """A local Advanced Settings value set; it contains no credentials."""

    model_config = ConfigDict(extra="forbid")
    values: dict[str, str | int | float | bool] = Field(min_length=1, max_length=100)


class BudgetSnapshotResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    currency: str
    known_amount: Decimal
    unknown_cost_calls: int
    calls: int
    reserved_calls: int


class BudgetResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy: BudgetPolicy | None
    policy_source: Literal["project", "global", "none"]
    snapshot: BudgetSnapshotResponse
    over_budget: bool
    period: Literal["ledger_lifetime"] = "ledger_lifetime"
    global_policy: BudgetPolicy | None = None
    project_policy: BudgetPolicy | None = None
    global_snapshot: BudgetSnapshotResponse | None = None
    project_snapshot: BudgetSnapshotResponse | None = None


class CostReductionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: UUID
    suggestions: list[CostReductionSuggestion]
    proposed_selections: list[CandidateAsset]
    changed_scene_count: int


class ProviderCallReserveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=1, max_length=500)
    operation: Literal["scene_planning", "asr", "vision", "embedding", "tts", "talking", "render", "stock_search", "stock_import", "web_search", "web_fetch"]
    mode: Literal["assisted_test", "runtime"]
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    input_source: str = Field(min_length=1, max_length=500)
    estimated_cost: UsageCost


class ProviderCallCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["completed", "failed", "cancelled"]
    actual_cost: UsageCost | None = None
    input_tokens: int | None = Field(default=None, ge=0, strict=True)
    output_tokens: int | None = Field(default=None, ge=0, strict=True)
    cache_tokens: int | None = Field(default=None, ge=0, strict=True)
    usage_observable: bool | None = None
    price_date: date | None = None
    error_code: str | None = Field(default=None, max_length=100)


class ProviderCallResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    record: ProviderCallRecord
    budget: BudgetResponse


def _budget_response(db: Database, project_id: UUID | None) -> BudgetResponse:
    policies = BudgetPolicyRepository(db)
    calls = ProviderCallRepository(db)
    global_policy = policies.get_by_project(None)
    project_policy = policies.get_by_project(project_id) if project_id is not None else None
    policy = project_policy or global_policy
    source: Literal["project", "global", "none"] = (
        "project" if project_policy is not None else "global" if global_policy is not None else "none"
    )
    fallback = BudgetPolicy(updated_at=datetime.now(timezone.utc))
    global_effective = global_policy or fallback
    global_calls = calls.list_all()
    global_current = snapshot(global_calls, global_effective.currency)
    project_effective = project_policy or global_policy or fallback
    project_calls = calls.list_for_project(project_id) if project_id is not None else None
    project_current = None if project_calls is None else snapshot(project_calls, project_effective.currency)
    primary_current = project_current or global_current
    global_over_budget = global_policy is not None and is_over_budget(global_policy, global_calls)
    project_over_budget = (
        project_policy is not None
        and project_calls is not None
        and is_over_budget(project_policy, project_calls)
    )
    return BudgetResponse(
        policy=policy,
        policy_source=source,
        snapshot=BudgetSnapshotResponse(**primary_current.__dict__),
        over_budget=global_over_budget or project_over_budget,
        global_policy=global_policy,
        project_policy=project_policy,
        global_snapshot=BudgetSnapshotResponse(**global_current.__dict__),
        project_snapshot=None if project_current is None else BudgetSnapshotResponse(**project_current.__dict__),
    )


def _runtime_idempotency_key(request: Request, operation: str) -> str:
    """Use a caller retry key when supplied; otherwise create one attempt key."""
    return request.headers.get("Idempotency-Key") or f"http-{operation}-{uuid4()}"




def _embedding_error_code(error: Exception) -> str:
    if isinstance(error, (EmbeddingConfigurationError, EmbeddingAuthenticationError)):
        return "embedding_not_configured"
    if isinstance(error, (EmbeddingRateLimitError, EmbeddingTimeout, EmbeddingConnectionError)):
        return "embedding_temporarily_unavailable"
    if isinstance(error, EmbeddingHTTPError):
        return "embedding_request_failed"
    if isinstance(error, EmbeddingProviderResponseError):
        return "embedding_response_invalid"
    if isinstance(error, (EmbeddingInputError, EmbeddingIndexError, IndexError, RoutingConfigurationError, RoutingInputError)):
        return "embedding_input_invalid"
    return "embedding_failed"


def _validate_profile_reference_clips(db: Database, clip_ids: list[UUID]) -> None:
    if len(set(clip_ids)) != len(clip_ids):
        raise HTTPException(status_code=422, detail="reference_clip_ids must be unique")
    clips = ClipRepository(db)
    missing = [str(clip_id) for clip_id in clip_ids if clips.get(clip_id) is None]
    if missing:
        raise HTTPException(status_code=422, detail=f"reference clip not found: {missing[0]}")


def _shoot_list_for_scene(scene: ScenePlan, candidates: tuple[CandidateAsset, ...]) -> tuple[ShootListInstruction, ...]:
    if not any(candidate.source_kind == SourceKind.CAPTURE and candidate.requires_capture for candidate in candidates):
        return ()
    intent = scene.visual_intent
    subject = intent.subject or scene.purpose
    action = intent.action or "保持主体动作清晰"
    what_to_shoot = f"拍摄{subject}，{action}。"
    framing = intent.framing or "稳定中近景，主体清晰"
    normalized = " ".join((scene.purpose, scene.voice_text, intent.subject or "", intent.action or "", intent.description or "")).casefold()
    requires_speaking = any(token in normalized for token in ("talk", "speak", "narrat", "voice", "口播", "讲话", "说", "讲", "介绍", "面对镜头"))
    speaking_note = (
        "需要说话：按场景文案录制本人声音；上传/绑定对应授权旁白后再组装。"
        if requires_speaking else
        "不需要说话：只补画面；若要新增文案，请另行上传/绑定对应授权旁白。"
    )
    if scene.visual_requirement == "creator_speaking":
        requires_speaking = False
        what_to_shoot = "拍摄一段本人自然面对镜头的连续画面，保持光线、脸部与嘴部清晰，避免大幅转头或遮挡。"
        speaking_note = "可安静看镜头或随意说话，不用背稿、不要求流利；配置声音克隆与自动口型后，系统按文案生成旁白和人物画面。素材适配与成片质量仍需检查。"
    fallback_sources = set(scene.fallback_sources)
    fallback_names = []
    if SourceKind.SCREENSHOT in fallback_sources or SourceKind.CHART in fallback_sources:
        fallback_names.append("本地截图/图表")
    if SourceKind.TYPOGRAPHY in fallback_sources:
        fallback_names.append("可编辑排版")
    fallback = (
        f"拒绝补拍时优先使用{'、'.join(fallback_names)}；若仍无法表达，保留明确素材缺口。"
        if fallback_names else
        "拒绝补拍时不生成外部素材；若无可用画面，保留明确素材缺口。"
    )
    return (ShootListInstruction(
        scene_plan_id=scene.id,
        scene_id=scene.scene_id,
        what_to_shoot=what_to_shoot,
        framing=framing,
        duration_ms=scene.duration_target_ms,
        requires_speaking=requires_speaking,
        speaking_note=speaking_note,
        fallback=fallback,
    ),)


class RuntimeCapabilityResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    status: Literal["ready", "provider_not_configured", "not_developed", "unavailable"]
    provider: str | None
    model: str | None
    detail: str
    estimated_cost: UsageCost | None = None
    constraints: list[str] = Field(default_factory=list)


class RuntimeReadinessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    checked_at: datetime
    runtime_ready: bool
    capabilities: list[RuntimeCapabilityResponse]


def create_app(
    data_path: str | Path | None = None,
    *,
    embedding_provider: EmbeddingProvider | None = None,
    scene_planner: ScenePlanner | None = None,
    renderer_factory: Callable[[Database], RemotionRenderer] | None = None,
    media_importer_factory: Callable[[Database, Path], MediaImporter] | None = None,
    vertical_derivation_factory: Callable[[Database, Path], VerticalSourceDerivationService] | None = None,
    image_importer_factory: Callable[[Database, Path], ImageImporter] | None = None,
    audio_importer_factory: Callable[[Database, Path], AudioImporter] | None = None,
    render_output_root: str | Path | None = None,
    cost_estimator: ProviderCostEstimator | None = None,
    database_startup_mode: Literal["migrate", "existing"] | None = None,
    external_media_provider: object | None = None,
) -> FastAPI:
    """Create an app whose SQLite connection belongs to its lifespan thread."""
    selected_path = data_path or os.environ.get("CONTENT_OS_DB_PATH") or Path("content-os-data") / "content-os.sqlite3"
    selected_startup_mode = database_startup_mode or os.environ.get("CONTENT_OS_DB_STARTUP_MODE", "migrate")
    if selected_startup_mode not in ("migrate", "existing"):
        raise ValueError("CONTENT_OS_DB_STARTUP_MODE must be 'migrate' or 'existing'")

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        selected = Database(selected_path, startup_mode=selected_startup_mode)
        selected.data_root = application.state.data_root
        selected.render_output_root = application.state.render_output_root
        application.state.database = selected
        application.state.database_closed = False
        try:
            yield
        finally:
            selected.close()
            application.state.database_closed = True

    application = FastAPI(title="Content OS API", version="0.1.0", description="Local-first API scaffold for Content OS.", lifespan=lifespan)
    @application.middleware("http")
    async def optional_private_access(request: Request, call_next: Callable[..., Any]) -> Any:
        """Require a configured bearer token for non-static local API access."""
        expected_token = os.environ.get("CONTENT_OS_ACCESS_TOKEN")
        public_path = request.url.path == "/" or request.url.path == "/health" or request.url.path.startswith("/app/")
        if expected_token and not public_path:
            supplied = request.headers.get("authorization", "")
            expected = f"Bearer {expected_token}"
            if not secrets.compare_digest(supplied, expected):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "private access token required"},
                    headers={"WWW-Authenticate": "Bearer"},
                )
        return await call_next(request)

    application.include_router(external_media_router)
    application.include_router(web_discovery_router)
    application.include_router(video_production_router)
    application.state.external_media_provider = external_media_provider
    application.state.embedding_provider = embedding_provider
    application.state.scene_planner = scene_planner
    application.state.renderer_factory = renderer_factory
    application.state.media_importer_factory = media_importer_factory
    application.state.vertical_derivation_factory = vertical_derivation_factory
    application.state.image_importer_factory = image_importer_factory
    application.state.audio_importer_factory = audio_importer_factory
    application.state.cost_estimator = cost_estimator or UnknownProviderCostEstimator()
    application.state.data_root = Path(os.environ.get("CONTENT_OS_DATA_ROOT") or Path(selected_path).parent).resolve()
    # This is intentionally an application-owned directory, never a request
    # field. Both API and worker derive the same default beneath the local
    # data root, so a completed asynchronous job can be downloaded here.
    application.state.render_output_root = Path(render_output_root or application.state.data_root / "renders").resolve()

    @application.get("/health", tags=["system"])
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "content-os-api"}

    @application.get("/runtime/readiness", response_model=RuntimeReadinessResponse, tags=["system"])
    def runtime_readiness() -> RuntimeReadinessResponse:
        capabilities = [RuntimeCapabilityResponse.model_validate(value.__dict__) for value in inspect_runtime_capabilities()]
        required = {"local_media", "render", "scene_planning", "asr", "vision", "embedding"}
        ready_keys = {value.key for value in capabilities if value.status == "ready"}
        return RuntimeReadinessResponse(
            checked_at=datetime.now(timezone.utc),
            runtime_ready=required.issubset(ready_keys),
            capabilities=capabilities,
        )

    @application.get("/execution-settings/schemas", tags=["execution-settings"])
    def execution_settings_schemas() -> dict[str, object]:
        """Return serializable provider-owned settings schemas without probing models."""

        return {"schemas": [schema.as_dict() for schema in list_provider_settings_schemas()]}

    @application.get("/execution-settings/capabilities", response_model=list[ProviderMachineCapabilityProfile], tags=["execution-settings"])
    async def list_execution_capabilities(request: Request) -> list[ProviderMachineCapabilityProfile]:
        """Enumerate persisted evidence only; do not probe, seed or authorize execution."""
        profiles = ProviderMachineCapabilityProfileRepository(request.app.state.database).list()
        return sorted(profiles, key=lambda profile: (profile.scope_key, str(profile.id)))

    def _ordinary_media(path, kind, subject_id, content_hash=None):
        try:
            require_ordinary_media(application.state.database, application.state.data_root,
                path, kind=kind, subject_id=subject_id, content_hash=content_hash)
        except UseAdmissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (ValueError, OSError):
            raise HTTPException(status_code=403, detail="media_source_invalid") from None

    def _seed_d6g_terminal_closeout_profile(
        capability: str, provider: str, model: str, runtime: str, machine_id: str,
    ) -> ProviderMachineCapabilityProfile | None:
        """Return only the concrete, human-approved D6g local evidence record."""

        if (capability, provider, model, runtime, machine_id) != (
            "talking", "latentsync", "LatentSync-1.5", "local-compatibility", "asus-rtx3060-laptop-6gb",
        ):
            return None
        verified_at = datetime(2026, 9, 19, tzinfo=timezone.utc)
        return ProviderMachineCapabilityProfile(
            capability=capability, mode="local", provider=provider, model=model, runtime=runtime, machine_id=machine_id,
            readiness="verified", verified_parameters={"trailing_silence_lookahead_ms": 600},
            feature_support={CapabilityFeature.TERMINAL_FACE_CLOSEOUT.value: FeatureSupport.VERIFIED.value},
            quality_status="verified", continuity_status="unknown",
            provenance_source="Gate D6g U-Talking accepted local evaluation",
            evidence_reference="content-os-data/latentsync-duration-boundary-20260915/gate-d6g-600ms-lookahead-face-visible-closeout.mp4",
            last_verified_at=verified_at, updated_at=verified_at,
        )

    def _execution_capability_profile(
        schema: object, key: object, scope_key: str, db: Database,
    ) -> tuple[CapabilityProfile, ProviderMachineCapabilityProfile | None]:
        """Load one persisted evidence profile, seeding only exact approved local evidence."""

        # ``schema`` and ``key`` stay local to the endpoint to keep the
        # persisted domain record independent of provider adapter classes.
        repository = ProviderMachineCapabilityProfileRepository(db)
        record = repository.get_by_scope_key(scope_key)
        if record is None:
            record = _seed_d6g_terminal_closeout_profile(
                key.capability.value, key.provider, key.model, key.runtime, key.machine_id,
            )
            if record is not None:
                with db.transaction():
                    repository.save(record)
        if record is None:
            return CapabilityProfile(
                key=key, readiness=CapabilityReadiness.CONFIGURED,
                commercial_status=CommercialStatus(schema.commercial_status),
                provenance=EvidenceProvenance("local Advanced Settings configuration", "runtime-readiness"),
            ), None
        features = {
            CapabilityFeature(feature): FeatureSupport(support)
            for feature, support in record.feature_support.items()
        }
        return CapabilityProfile(
            key=key, readiness=CapabilityReadiness(record.readiness),
            verified_parameters=record.verified_parameters, feature_support=features,
            operating_bounds=(OperatingBound("fresh_voice_talking_duration", "ms", observed_pass_at=4_460, observed_fail_at=5_120, status=EvidenceStatus.VERIFIED),) if (key.capability.value, key.provider, key.model, key.runtime, key.machine_id) == ("talking", "latentsync", "LatentSync-1.5", "local-compatibility", "asus-rtx3060-laptop-6gb") else (),
            quality_status=EvidenceStatus(record.quality_status), continuity_status=EvidenceStatus(record.continuity_status),
            commercial_status=CommercialStatus(schema.commercial_status),
            provenance=EvidenceProvenance(record.provenance_source, record.evidence_reference, record.last_verified_at),
        ), record

    def _execution_settings_view(
        capability: str, provider: str, model: str, runtime: str, machine_id: str,
        db: Database,
    ) -> dict[str, object]:
        schema = find_provider_settings_schema(capability, provider, model, runtime)
        if schema is None:
            raise HTTPException(status_code=404, detail="no admitted Advanced Settings schema for this provider configuration")
        key = schema.profile_key(machine_id)
        scope_key = ":".join((capability, "local", provider, model, runtime, machine_id))
        stored = ProviderMachineSettingRepository(db).get_by_scope_key(scope_key)
        override = None if stored is None else ExecutionOverride(
            key, stored.values, ResolutionSource.USER_OVERRIDE, f"provider-machine-setting:{stored.id}"
        )
        profile, evidence_record = _execution_capability_profile(schema, key, scope_key, db)
        resolution = resolve_execution(
            profile,
            parameter_keys=[parameter.key for parameter in schema.parameters],
            saved_override=override,
            provider_defaults=schema.defaults,
            safety=ExecutionSafetyContext(
                consent_authorized=True,
                budget_authorized=True,
                runtime_integrity_verified=True,
                requires_provenance=True,
            ),
        )
        return {
            "schema": schema.as_dict(),
            "machine_id": machine_id,
            "saved_override": None if stored is None else stored.model_dump(mode="json"),
            "capability_profile": None if evidence_record is None else evidence_record.model_dump(mode="json"),
            "features": [
                {
                    "feature": feature.feature.value,
                    "support": feature.support.value,
                    "reason": feature.reason,
                }
                for declared in schema.features
                for feature in (resolve_feature_support(profile, declared.feature, adapter_declared_support=declared.support),)
            ],
            "resolution": {
                "routing_reason": resolution.routing_reason,
                "parameters": [
                    {"key": item.key, "value": item.value, "source": item.source.value, "reason": item.reason}
                    for item in resolution.parameters
                ],
            },
        }

    @application.get("/execution-settings/{capability}/{provider}/{model}/{runtime}/{machine_id}", tags=["execution-settings"])
    async def get_execution_settings(capability: str, provider: str, model: str, runtime: str, machine_id: str, request: Request) -> dict[str, object]:
        return _execution_settings_view(capability, provider, model, runtime, machine_id, request.app.state.database)

    @application.put("/execution-settings/{capability}/{provider}/{model}/{runtime}/{machine_id}", tags=["execution-settings"])
    async def save_execution_settings(
        capability: str, provider: str, model: str, runtime: str, machine_id: str,
        payload: ProviderMachineSettingRequest, request: Request,
    ) -> dict[str, object]:
        schema = find_provider_settings_schema(capability, provider, model, runtime)
        if schema is None:
            raise HTTPException(status_code=404, detail="no admitted Advanced Settings schema for this provider configuration")
        try:
            values = validate_schema_values(schema, payload.values)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        db: Database = request.app.state.database
        scope_key = ":".join((capability, "local", provider, model, runtime, machine_id))
        existing = ProviderMachineSettingRepository(db).get_by_scope_key(scope_key)
        setting = ProviderMachineSetting(
            id=existing.id if existing else uuid4(), capability=capability, mode="local", provider=provider,
            model=model, runtime=runtime, machine_id=machine_id, values=values, updated_at=datetime.now(timezone.utc),
        )
        with db.transaction():
            ProviderMachineSettingRepository(db).save(setting)
        return _execution_settings_view(capability, provider, model, runtime, machine_id, db)

    @application.delete("/execution-settings/{capability}/{provider}/{model}/{runtime}/{machine_id}", status_code=204, tags=["execution-settings"])
    async def reset_execution_settings(capability: str, provider: str, model: str, runtime: str, machine_id: str, request: Request) -> Response:
        schema = find_provider_settings_schema(capability, provider, model, runtime)
        if schema is None:
            raise HTTPException(status_code=404, detail="no admitted Advanced Settings schema for this provider configuration")
        db: Database = request.app.state.database
        scope_key = ":".join((capability, "local", provider, model, runtime, machine_id))
        existing = ProviderMachineSettingRepository(db).get_by_scope_key(scope_key)
        if existing is not None:
            with db.transaction():
                ProviderMachineSettingRepository(db).delete(existing.id)
        return Response(status_code=204)

    web_dist = Path(__file__).resolve().parents[3] / "apps" / "web" / "dist"

    @application.get("/", include_in_schema=False)
    def web_home() -> RedirectResponse:
        return RedirectResponse("/app/" if (web_dist / "index.html").is_file() else "/workspace")

    if web_dist.is_dir():
        application.mount("/app", StaticFiles(directory=web_dist, html=True), name="web")

    # Read-only local Asset Library. Clip preview reuses the original source;
    # the page seeks the video element to the selected Clip interval.
    @application.get("/asset-library", response_class=HTMLResponse, include_in_schema=False)
    def asset_library() -> HTMLResponse:
        return asset_library_page()

    @application.get("/m1-gate", response_class=HTMLResponse, include_in_schema=False)
    def m1_gate() -> HTMLResponse:
        return m1_gate_page()

    @application.get("/workspace", response_class=HTMLResponse, include_in_schema=False)
    def workspace() -> HTMLResponse:
        return workspace_page()

    @application.get("/projects", response_model=list[Project], tags=["projects"])
    async def list_projects() -> list[Project]:
        return ProjectRepository(application.state.database).list()

    @application.post("/projects", response_model=Project, status_code=201, tags=["projects"])
    async def create_project(payload: ProjectCreateRequest, request: Request) -> Project:
        db: Database = request.app.state.database
        profiles = IPProfileRepository(db).list()
        if payload.ip_profile_id is not None:
            profile = IPProfileRepository(db).get(payload.ip_profile_id)
            if profile is None:
                raise HTTPException(status_code=404, detail="IP profile not found")
        elif "creator_name" in payload.model_fields_set:
            matches = [p for p in profiles if p.creator_name == payload.creator_name]
            if len(matches) > 1:
                raise HTTPException(status_code=422, detail="project_ip_selection_required")
            profile = matches[0] if matches else IPProfile(creator_name=payload.creator_name)
        elif len(profiles) > 1:
            raise HTTPException(status_code=422, detail="project_ip_selection_required")
        else:
            profile = profiles[0] if profiles else IPProfile(creator_name=payload.creator_name)
        project = Project(
            ip_profile_id=profile.id, title=payload.title, topic=payload.topic,
            presentation_style=payload.presentation_style,delivery_intent=payload.delivery_intent,
            fps=RationalFps(numerator=30, denominator=1), created_at=datetime.now(timezone.utc),
        )
        with db.transaction():
            if IPProfileRepository(db).get(profile.id) is None:
                IPProfileRepository(db).create(profile)
            ProjectRepository(db).create(project)
        return project

    @application.patch("/projects/{project_id}", response_model=Project, tags=["projects"])
    async def update_project_presentation(project_id: UUID, payload: ProjectPresentationRequest, request: Request) -> Project:
        db: Database = request.app.state.database
        repo = ProjectRepository(db)
        project = repo.get(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        changes=payload.model_dump(exclude_none=True,exclude_unset=True)
        if payload.format is not None:
            from app.domain.models import ProjectFormat
            changes["format"] = ProjectFormat(payload.format)
            changes.setdefault("format_policy", "fixed")
            short, long = sorted((project.resolution_width, project.resolution_height))
            if short * 16 != long * 9:
                short, long = 1080, 1920
            changes.update(resolution_width=short if payload.format=="vertical" else long,
                resolution_height=long if payload.format=="vertical" else short)
        from app.planning_input import short_video_project
        updated = short_video_project(project.model_copy(update=changes))
        if updated == project:
            return project
        with db.transaction():
            repo.update(updated)
            draft = ProjectDraftRepository(db).get(project.id)
            if draft is not None and updated.presentation_style != project.presentation_style:
                persist_project_draft(db, updated, script=draft.script, topic=draft.topic,
                    scenes=(), routes=(), confirmed=(), video_spec=None, evidence_refs=draft.evidence_refs)
            elif draft is not None and (updated.format,updated.resolution_width,updated.resolution_height)!=(project.format,project.resolution_width,project.resolution_height):
                persist_project_draft(db,updated,script=draft.script,topic=draft.topic,
                    scenes=draft.scenes,routes=(),confirmed=(),video_spec=None,evidence_refs=draft.evidence_refs)
        return updated

    @application.get("/ip-profile", response_model=IPProfile, tags=["profile"])
    async def get_default_ip_profile(request: Request, ip_profile_id: UUID | None = None) -> IPProfile:
        repo = IPProfileRepository(request.app.state.database)
        profile = repo.get(ip_profile_id) if ip_profile_id else next(iter(repo.list()), None)
        if profile is None:
            raise HTTPException(status_code=404, detail="default IP profile not initialized")
        return profile

    @application.get("/ip-profiles", response_model=list[IPProfile], tags=["profile"])
    async def list_ip_profiles(request: Request) -> list[IPProfile]:
        return IPProfileRepository(request.app.state.database).list()

    @application.put("/ip-profile", response_model=IPProfile, tags=["profile"])
    async def update_default_ip_profile(payload: IPProfileUpdateRequest, request: Request, ip_profile_id: UUID | None = None) -> IPProfile:
        db: Database = request.app.state.database
        profile = IPProfileRepository(db).get(ip_profile_id) if ip_profile_id else next(iter(IPProfileRepository(db).list()), None)
        if ip_profile_id is not None and profile is None:
            raise HTTPException(status_code=404, detail="IP profile not found")
        if profile is not None and "default_voice_profile_id" not in payload.model_fields_set:
            payload = payload.model_copy(update={"default_voice_profile_id":profile.default_voice_profile_id})
        if payload.default_voice_profile_id is not None:
            voice = VoiceProfileRepository(db).get(payload.default_voice_profile_id)
            if profile is None or voice is None or not voice.consent.confirmed or voice.ip_profile_id != profile.id:
                raise HTTPException(status_code=422, detail="default_voice_must_belong_to_ip")
        if profile is None:
            profile = IPProfile(id=uuid4(), **payload.model_dump())
            with db.transaction():
                IPProfileRepository(db).create(profile)
            return profile
        updated = IPProfile(id=profile.id, **payload.model_dump())
        if updated == profile:
            return profile
        with db.transaction():
            IPProfileRepository(db).update(updated)
            # A project may retain its editable text, but no derived ScenePlan,
            # route, candidate selection, or render may survive an IP revision.
            # This server-side path also catches stale browser tabs.
            for project in ProjectRepository(db).list():
                if project.ip_profile_id != updated.id:
                    continue
                draft = ProjectDraftRepository(db).get(project.id)
                if draft is None:
                    continue
                _, evidence_refs = _scene_planning_context(db, project, topic=draft.topic)
                persist_project_draft(
                    db,
                    project,
                    script=draft.script,
                    topic=draft.topic,
                    scenes=draft.scenes,
                    routes=draft.routes,
                    confirmed=draft.confirmed,
                    video_spec=draft.video_spec,
                    evidence_refs=evidence_refs,
                )
        return updated

    @application.get("/ip-profile/revisions", response_model=list[IPProfileRevisionResponse], tags=["profile"])
    async def list_ip_profile_revisions(request: Request) -> list[IPProfileRevisionResponse]:
        db: Database = request.app.state.database
        profile = next(iter(IPProfileRepository(db).list()), None)
        if profile is None:
            raise HTTPException(status_code=404, detail="default IP profile not initialized")
        return [IPProfileRevisionResponse(version=version, created_at=created_at, profile=value) for version, created_at, value in IPProfileRepository(db).revisions(profile.id)]

    @application.get("/projects/{project_id}/draft", response_model=ProjectDraft, tags=["projects"])
    async def get_project_draft(project_id: UUID, request: Request) -> ProjectDraft:
        db: Database = request.app.state.database
        if ProjectRepository(db).get(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        draft = ProjectDraftRepository(db).get(project_id)
        return draft or ProjectDraft(project_id=project_id, updated_at=datetime.now(timezone.utc))

    @application.get("/projects/{project_id}/draft/revisions", response_model=list[ProjectDraftRevision], tags=["projects"])
    async def list_project_draft_revisions(project_id: UUID, request: Request) -> list[ProjectDraftRevision]:
        db: Database = request.app.state.database
        if ProjectRepository(db).get(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        return ProjectDraftRepository(db).revisions(project_id)

    @application.put("/projects/{project_id}/draft", response_model=ProjectDraft, tags=["projects"])
    async def save_project_draft(project_id: UUID, payload: DraftUpdateRequest, request: Request) -> ProjectDraft:
        db: Database = request.app.state.database
        if ProjectRepository(db).get(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        if any(scene.project_id != project_id for scene in payload.scenes):
            raise HTTPException(status_code=422, detail="all draft scenes must belong to the requested project")
        scene_ids = {scene.id for scene in payload.scenes}
        if any(route.scene_plan_id not in scene_ids for route in payload.routes):
            raise HTTPException(status_code=422, detail="all draft routes must belong to a draft scene")
        if any(candidate.scene_plan_id not in scene_ids for candidate in payload.confirmed):
            raise HTTPException(status_code=422, detail="all confirmed candidates must belong to a draft scene")
        if len({candidate.scene_plan_id for candidate in payload.confirmed}) != len(payload.confirmed):
            raise HTTPException(status_code=422, detail="draft confirmed selections must be unique per scene")
        if payload.video_spec is not None and payload.video_spec.project_id != project_id:
            raise HTTPException(status_code=422, detail="draft VideoSpec must belong to the requested project")
        project = ProjectRepository(db).get(project_id)
        assert project is not None  # checked above; narrows the local contract.
        _, evidence_refs = _scene_planning_context(db, project, topic=payload.topic)
        with db.transaction():
            draft = persist_project_draft(
                db,
                project,
                script=payload.script,
                topic=payload.topic,
                scenes=payload.scenes,
                routes=payload.routes,
                confirmed=payload.confirmed,
                video_spec=payload.video_spec,
                evidence_refs=evidence_refs,
            )
        return draft

    @application.get(
        "/projects/{project_id}/narration-performance-plan",
        response_model=NarrationPerformancePlan,
        tags=["voice"],
    )
    async def get_narration_performance_plan(project_id: UUID, request: Request) -> NarrationPerformancePlan:
        """Return only the plan bound to the current persisted editable copy."""

        db: Database = request.app.state.database
        if ProjectRepository(db).get(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        draft = ProjectDraftRepository(db).get(project_id)
        if draft is None or draft.narration_performance_plan is None:
            raise HTTPException(status_code=404, detail="current draft has no narration performance plan")
        return draft.narration_performance_plan

    @application.get(
        "/projects/{project_id}/narration-performance-suggestions",
        response_model=NarrationPerformanceSuggestions,
        tags=["voice"],
    )
    async def get_narration_performance_suggestions(
        project_id: UUID,
        request: Request,
    ) -> NarrationPerformanceSuggestions:
        """Suggest editable rhetorical cues for the current copy without saving or executing them."""

        db: Database = request.app.state.database
        if ProjectRepository(db).get(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        draft = ProjectDraftRepository(db).get(project_id)
        if draft is None or not draft.script:
            raise HTTPException(status_code=422, detail="narration performance suggestions require a persisted editable script")
        try:
            return suggest_narration_performance(draft.script)
        except NarrationPerformancePlanError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.get(
        "/projects/{project_id}/narration-performance-plan/delivery-plan",
        response_model=NarrationDeliveryPlan,
        tags=["voice"],
    )
    async def get_narration_delivery_plan(project_id: UUID, request: Request) -> NarrationDeliveryPlan:
        """Compile current delivery direction without queueing a Voice job."""

        db: Database = request.app.state.database
        if ProjectRepository(db).get(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        draft = ProjectDraftRepository(db).get(project_id)
        if draft is None or not draft.script or draft.narration_performance_plan is None:
            raise HTTPException(status_code=404, detail="current draft has no narration performance plan")
        try:
            return compile_narration_delivery_plan(draft.narration_performance_plan, draft.script)
        except NarrationPerformancePlanError as exc:
            raise HTTPException(status_code=409, detail="current narration performance plan cannot compile") from exc

    @application.put(
        "/projects/{project_id}/narration-performance-plan",
        response_model=NarrationPerformancePlan,
        tags=["voice"],
    )
    async def save_narration_performance_plan(
        project_id: UUID,
        payload: NarrationPerformancePlanRequest,
        request: Request,
    ) -> NarrationPerformancePlan:
        """Replace delivery direction for exact current copy; never retarget cues."""

        db: Database = request.app.state.database
        project = ProjectRepository(db).get(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        draft = ProjectDraftRepository(db).get(project_id)
        if draft is None or not draft.script:
            raise HTTPException(status_code=422, detail="narration performance intent requires a persisted editable script")
        try:
            plan = build_narration_performance_plan(
                draft.script,
                delivery_goal=payload.delivery_goal,
                overall_pace=payload.overall_pace,
                cues=payload.cues,
                source=payload.source,
                evidence_refs=payload.evidence_refs,
            )
        except NarrationPerformancePlanError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        with db.transaction():
            updated = persist_project_draft(
                db,
                project,
                script=draft.script,
                topic=draft.topic,
                scenes=draft.scenes,
                routes=draft.routes,
                confirmed=draft.confirmed,
                video_spec=draft.video_spec,
                evidence_refs=draft.evidence_refs,
                narration_performance_plan=plan,
            )
        assert updated.narration_performance_plan is not None
        return updated.narration_performance_plan

    @application.delete(
        "/projects/{project_id}/narration-performance-plan",
        status_code=204,
        tags=["voice"],
    )
    async def clear_narration_performance_plan(project_id: UUID, request: Request) -> Response:
        """Clear the current editable plan while prior draft revisions retain it."""

        db: Database = request.app.state.database
        project = ProjectRepository(db).get(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        draft = ProjectDraftRepository(db).get(project_id)
        if draft is None:
            raise HTTPException(status_code=404, detail="project has no persisted draft")
        if draft.narration_performance_plan is not None:
            with db.transaction():
                persist_project_draft(
                    db,
                    project,
                    script=draft.script,
                    topic=draft.topic,
                    scenes=draft.scenes,
                    routes=draft.routes,
                    confirmed=draft.confirmed,
                    video_spec=draft.video_spec,
                    evidence_refs=draft.evidence_refs,
                    narration_performance_plan=None,
                )
        return Response(status_code=204)

    @application.get("/projects/{project_id}/cost-estimate", response_model=CostEstimate, tags=["runtime"])
    async def get_project_cost_estimate(project_id: UUID, request: Request) -> CostEstimate:
        db: Database = request.app.state.database
        if ProjectRepository(db).get(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        draft = ProjectDraftRepository(db).get(project_id)
        if draft is None:
            return estimate_selected_candidates((), required_scene_count=0)
        try:
            scene_labels = {scene.id: f"{scene.order + 1:02d} {scene.scene_id} {scene.purpose}" for scene in draft.scenes}
            return estimate_selected_candidates(draft.confirmed, required_scene_count=len(draft.scenes), scene_labels=scene_labels)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.post("/projects/{project_id}/production-preflight", response_model=ProductionPreflight, tags=["projects"])
    async def production_preflight(project_id: UUID, payload: ProductionPreflightRequest, request: Request) -> ProductionPreflight:
        """Preview the persisted draft without invoking providers or dispatching work."""
        db: Database = request.app.state.database
        project = ProjectRepository(db).get(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        try:
            return build_production_preflight(
                db, project, ProjectDraftRepository(db).get(project_id),
                style_tokens=payload.style_tokens,
                expected_fingerprint=payload.expected_fingerprint,
                repair_allowance=payload.repair_allowance,
                crop_proposals=payload.crop_proposals,
            )
        except StaleProductionPreflight as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PreflightInputError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (RoutingInputError, IndexError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.post("/assets/{asset_id}/crop-assessments", response_model=CropAssessment, status_code=201, tags=["assets"])
    async def record_crop_assessment(asset_id: UUID, payload: CropAssessment, request: Request) -> CropAssessment:
        if payload.proposal.source_asset_id != asset_id:
            raise HTTPException(status_code=422, detail="crop assessment asset identity mismatch")
        if payload.evidence_class == "runtime":
            raise HTTPException(status_code=422, detail="runtime evidence must originate from an internal detector")
        try:
            return CropAssessmentRepository(request.app.state.database).create(payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.post("/projects/{project_id}/production-runs", response_model=ProductionRun, status_code=201, tags=["projects"])
    async def start_production_run(project_id: UUID, payload: ProductionRunStartRequest, request: Request) -> ProductionRun:
        if ProjectRepository(request.app.state.database).get(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        try:
            return ProductionRunService(request.app.state.database, request.app.state.data_root).start(
                project_id, idempotency_key=payload.idempotency_key,
                expected_fingerprint=payload.expected_fingerprint,
                style_tokens=payload.style_tokens, repair_allowance=payload.repair_allowance,
                talking_review_policy_version=payload.talking_review_policy_version,
            )
        except StaleProductionPreflight as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ProductionRunNotReady as exc:
            raise HTTPException(status_code=409, detail={"code": "production_plan_not_ready", "reasons": exc.reasons}) from exc
        except (ProductionRunConflict, PreflightInputError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @application.get("/projects/{project_id}/production-runs", response_model=list[ProductionRunReference], tags=["projects"])
    async def list_production_runs(project_id: UUID, request: Request,
                                   draft_version: int | None = None,
                                   script_revision: int | None = None) -> list[ProductionRunReference]:
        if ProjectRepository(request.app.state.database).get(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        if (draft_version is None) != (script_revision is None):
            raise HTTPException(status_code=422, detail="draft_version and script_revision must be supplied together")
        return ProductionRunService(request.app.state.database, request.app.state.data_root).list_references(
            project_id, draft_version, script_revision)

    @application.get("/projects/{project_id}/production-runs/{run_id}", response_model=ProductionRun, tags=["projects"])
    async def get_production_run(project_id: UUID, run_id: UUID, request: Request) -> ProductionRun:
        run = ProductionRunService(request.app.state.database, request.app.state.data_root).get(project_id, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="production run not found")
        return run

    @application.post("/projects/{project_id}/production-runs/{run_id}/cancel", response_model=ProductionRun, tags=["projects"])
    async def cancel_production_run(project_id: UUID, run_id: UUID, request: Request) -> ProductionRun:
        try:
            run = ProductionRunService(request.app.state.database, request.app.state.data_root).cancel(project_id, run_id)
        except ProductionRunConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if run is None:
            raise HTTPException(status_code=404, detail="production run not found")
        return run

    @application.post("/projects/{project_id}/production-runs/{run_id}/resume", response_model=ProductionRun, tags=["projects"])
    async def resume_production_run(project_id: UUID, run_id: UUID, request: Request) -> ProductionRun:
        try:
            run = ProductionRunService(request.app.state.database, request.app.state.data_root).resume(project_id, run_id)
        except ProductionRunNotReady as exc:
            raise HTTPException(status_code=409, detail={"code": "production_plan_not_ready", "reasons": exc.reasons}) from exc
        except (ProductionRunConflict, PreflightInputError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if run is None:
            raise HTTPException(status_code=404, detail="production run not found")
        return run

    @application.get("/projects/{project_id}/production-runs/{run_id}/talking-repair-plan", response_model=TalkingRepairPlan, tags=["projects"])
    async def get_talking_repair_plan(project_id: UUID, run_id: UUID, scene_plan_id: UUID, request: Request):
        try:
            return TalkingRepairService(request.app.state.database, request.app.state.data_root).plan(project_id, run_id, scene_plan_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ProductionRunNotReady as exc:
            raise HTTPException(status_code=409, detail={"code": "talking_repair_not_ready", "reasons": exc.reasons}) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @application.get("/projects/{project_id}/production-runs/{run_id}/voice-repair-plan", response_model=VoiceRepairPlan, tags=["projects"])
    async def get_voice_repair_plan(project_id: UUID, run_id: UUID, request: Request):
        try:
            return VoiceRepairService(request.app.state.database, request.app.state.data_root).plan(project_id, run_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ProductionRunNotReady as exc:
            raise HTTPException(status_code=409, detail={"code": "voice_repair_not_ready", "reasons": exc.reasons}) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @application.post("/projects/{project_id}/production-runs/{run_id}/voice-repair", status_code=201, tags=["projects"])
    async def execute_voice_repair(project_id: UUID, run_id: UUID, payload: VoiceRepairRequest, request: Request):
        try:
            return VoiceRepairService(request.app.state.database, request.app.state.data_root).execute(project_id, run_id, payload)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ProductionRunNotReady as exc:
            raise HTTPException(status_code=409, detail={"code": "voice_repair_not_ready", "reasons": exc.reasons}) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @application.get("/projects/{project_id}/production-runs/{run_id}/voice-repairs", tags=["projects"])
    async def list_voice_repairs(project_id: UUID, run_id: UUID, request: Request):
        try:
            return VoiceRepairService(request.app.state.database, request.app.state.data_root).records(project_id, run_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    def presentation_service(request: Request):
        return PresentationRepairService(request.app.state.database, request.app.state.data_root, request.app.state.render_output_root)

    @application.post("/projects/{project_id}/production-runs/{run_id}/presentation-observations", status_code=201, tags=["projects"])
    async def observe_presentation(project_id: UUID, run_id: UUID, payload: PresentationObservationRequest, request: Request):
        try: return presentation_service(request).observe(project_id, run_id, payload)
        except LookupError as exc: raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ProductionRunNotReady as exc: raise HTTPException(status_code=409, detail={"code": "presentation_not_ready", "reasons": exc.reasons}) from exc
        except ValueError as exc: raise HTTPException(status_code=409, detail=str(exc)) from exc

    @application.get("/projects/{project_id}/production-runs/{run_id}/presentation-repair-plan", tags=["projects"])
    async def plan_presentation(project_id: UUID, run_id: UUID, observation_id: UUID, request: Request):
        try: return presentation_service(request).plan(project_id, run_id, observation_id)
        except LookupError as exc: raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ProductionRunNotReady as exc: raise HTTPException(status_code=409, detail={"code": "presentation_not_ready", "reasons": exc.reasons}) from exc
        except ValueError as exc: raise HTTPException(status_code=409, detail=str(exc)) from exc

    @application.post("/projects/{project_id}/production-runs/{run_id}/presentation-repair", status_code=201, tags=["projects"])
    async def apply_presentation(project_id: UUID, run_id: UUID, payload: PresentationRepairRequest, request: Request):
        try: return presentation_service(request).apply(project_id, run_id, payload)
        except LookupError as exc: raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ProductionRunNotReady as exc: raise HTTPException(status_code=409, detail={"code": "presentation_not_ready", "reasons": exc.reasons}) from exc
        except ValueError as exc: raise HTTPException(status_code=409, detail=str(exc)) from exc

    @application.get("/projects/{project_id}/production-runs/{run_id}/presentation-repairs", tags=["projects"])
    async def history_presentation(project_id: UUID, run_id: UUID, request: Request):
        try: return presentation_service(request).history(project_id, run_id)
        except LookupError as exc: raise HTTPException(status_code=404, detail=str(exc)) from exc

    @application.post("/projects/{project_id}/production-runs/{run_id}/talking-repair", response_model=TalkingRepairRecord, status_code=201, tags=["projects"])
    async def execute_talking_repair(project_id: UUID, run_id: UUID, payload: TalkingRepairRequest, request: Request):
        try:
            return TalkingRepairService(request.app.state.database, request.app.state.data_root).execute(project_id, run_id, payload)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ProductionRunNotReady as exc:
            raise HTTPException(status_code=409, detail={"code": "talking_repair_not_ready", "reasons": exc.reasons}) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @application.get("/projects/{project_id}/production-runs/{run_id}/talking-repairs", tags=["projects"])
    async def list_talking_repairs(project_id: UUID, run_id: UUID, request: Request):
        db = request.app.state.database
        if ProductionRunService(db, request.app.state.data_root).get(project_id, run_id) is None:
            raise HTTPException(status_code=404, detail="production run not found")
        calls = ProviderCallRepository(db).list_for_project(project_id)
        return [{**record.model_dump(mode="json"),
                 "replacement_job_status": None if (job := JobRepository(db).get(record.replacement_job_id)) is None else job.status.value,
                 "provider_calls": [call.model_dump(mode="json") for call in calls
                 if call.idempotency_key.startswith(f"job:{record.replacement_job_id}:talking:")],
                 "predecessor_provider_calls": [call.model_dump(mode="json") for call in calls
                 if call.idempotency_key.startswith(f"job:{record.old_binding.talking_job_id}:talking:")]}
                for record in TalkingRepairService(db, request.app.state.data_root).records(project_id, run_id)]

    @application.post("/projects/{project_id}/production-runs/{run_id}/talking-dispatch", response_model=ProductionRun, tags=["projects"])
    async def dispatch_production_talking(
        project_id: UUID, run_id: UUID, payload: ProductionRunTalkingDispatchRequest, request: Request,
    ) -> ProductionRun:
        try:
            run = ProductionRunService(request.app.state.database, request.app.state.data_root).dispatch_talking(
                project_id, run_id, scene_plan_id=payload.scene_plan_id,
            )
        except ProductionRunNotReady as exc:
            raise HTTPException(status_code=409, detail={"code": "planned_talking_not_ready", "reasons": exc.reasons}) from exc
        except (ProductionRunConflict, PreflightInputError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if run is None:
            raise HTTPException(status_code=404, detail="production run not found")
        return run

    @application.post("/projects/{project_id}/production-runs/{run_id}/talking-batch-dispatch", response_model=ProductionRun, tags=["projects"])
    async def dispatch_all_production_talking(
        project_id: UUID, run_id: UUID, payload: ProductionRunTalkingBatchDispatchRequest, request: Request,
    ) -> ProductionRun:
        try:
            run = ProductionRunService(request.app.state.database, request.app.state.data_root).dispatch_all_talking(
                project_id, run_id, talking_profile_id=payload.talking_profile_id,
                reference_clip_id=payload.reference_clip_id, capability_profile_id=payload.capability_profile_id,
                brief=payload.brief,
            )
        except ProductionRunNotReady as exc:
            raise HTTPException(status_code=409, detail={"code": "planned_talking_batch_not_ready", "reasons": exc.reasons}) from exc
        except (ProductionRunConflict, PreflightInputError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if run is None:
            raise HTTPException(status_code=404, detail="production run not found")
        return run

    @application.post("/projects/{project_id}/production-runs/{run_id}/talking-qa-advance", response_model=ProductionRun, tags=["projects"])
    async def advance_production_talking_qa(
        project_id: UUID, run_id: UUID, payload: ProductionRunTalkingQaAdvanceRequest, request: Request,
    ) -> ProductionRun:
        try:
            run = ProductionRunService(request.app.state.database, request.app.state.data_root).advance_talking_qa(
                project_id, run_id, scene_plan_id=payload.scene_plan_id,
            )
        except ProductionRunNotReady as exc:
            raise HTTPException(status_code=409, detail={"code": "talking_qa_not_ready", "reasons": exc.reasons}) from exc
        except (ProductionRunConflict, PreflightInputError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if run is None:
            raise HTTPException(status_code=404, detail="production run not found")
        return run

    @application.post("/projects/{project_id}/production-runs/{run_id}/talking-preview-prepare", response_model=ProductionRun, tags=["projects"])
    async def prepare_production_talking_preview(
        project_id: UUID, run_id: UUID, payload: ProductionRunTalkingQaAdvanceRequest, request: Request,
    ) -> ProductionRun:
        try:
            run = ProductionRunService(request.app.state.database, request.app.state.data_root).prepare_talking_run_preview(
                project_id, run_id, scene_plan_id=payload.scene_plan_id,
            )
        except ProductionRunNotReady as exc:
            raise HTTPException(status_code=409, detail={"code": "talking_preview_not_ready", "reasons": exc.reasons}) from exc
        except (ProductionRunConflict, PreflightInputError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if run is None:
            raise HTTPException(status_code=404, detail="production run not found")
        return run

    @application.post("/projects/{project_id}/production-runs/{run_id}/talking-run-admit", response_model=ProductionRun, tags=["projects"])
    async def admit_production_talking_run(
        project_id: UUID, run_id: UUID, payload: ProductionRunTalkingQaAdvanceRequest, request: Request,
    ) -> ProductionRun:
        db: Database = request.app.state.database
        production = ProductionRunService(db, request.app.state.data_root)
        current = production.get(project_id, run_id)
        if current is None:
            raise HTTPException(status_code=404, detail="production run not found")
        binding = next((item for item in current.talking_source_bindings if item.scene_plan_id == payload.scene_plan_id), None)
        if binding is None or binding.talking_series_id is None:
            raise HTTPException(status_code=409, detail="planned Talking collection is not bound")
        series = TalkingSliceSeriesRepository(db).get(binding.talking_series_id)
        if series is None or series.project_id != project_id or series.planned_origin is None:
            raise HTTPException(status_code=409, detail="planned Talking origin is missing")
        try:
            PlannedTalkingAdmissionService(db, request.app.state.data_root).admit(series)
        except (PlannedTalkingAdmissionError, ProductionRunNotReady, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return production.get(project_id, run_id)

    @application.get("/projects/{project_id}/production-runs/{run_id}/talking-source-context", response_model=TalkingSourceContext, tags=["projects"])
    async def get_production_talking_source_context(
        project_id: UUID, run_id: UUID, scene_plan_id: UUID, request: Request,
    ) -> TalkingSourceContext:
        try:
            context = ProductionRunService(request.app.state.database, request.app.state.data_root).talking_source_context(
                project_id, run_id, scene_plan_id=scene_plan_id,
            )
        except ProductionRunNotReady as exc:
            raise HTTPException(status_code=409, detail={"code": "talking_source_context_not_ready", "reasons": exc.reasons}) from exc
        except (ProductionRunConflict, PreflightInputError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if context is None:
            raise HTTPException(status_code=404, detail="production run not found")
        return context

    @application.post("/projects/{project_id}/production-runs/{run_id}/talking-source-bind", response_model=ProductionRun, tags=["projects"])
    async def bind_production_talking_source(
        project_id: UUID, run_id: UUID, payload: ProductionRunTalkingSourceBindRequest, request: Request,
    ) -> ProductionRun:
        try:
            run = ProductionRunService(request.app.state.database, request.app.state.data_root).bind_talking_source(
                project_id, run_id, scene_plan_id=payload.scene_plan_id,
                talking_profile_id=payload.talking_profile_id, reference_clip_id=payload.reference_clip_id,
                capability_profile_id=payload.capability_profile_id, master_start_ms=payload.master_start_ms,
                master_end_ms=payload.master_end_ms, brief=payload.brief,
            )
        except ProductionRunNotReady as exc:
            raise HTTPException(status_code=409, detail={"code": "talking_source_bind_not_ready", "reasons": exc.reasons}) from exc
        except (ProductionRunConflict, PreflightInputError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if run is None:
            raise HTTPException(status_code=404, detail="production run not found")
        return run

    @application.post("/projects/{project_id}/production-runs/{run_id}/voice-dispatch", response_model=ProductionRun, tags=["projects"])
    async def dispatch_production_voice(
        project_id: UUID, run_id: UUID, payload: ProductionRunVoiceDispatchRequest, request: Request,
    ) -> ProductionRun:
        try:
            run = ProductionRunService(request.app.state.database, request.app.state.data_root).dispatch_voice(
                project_id, run_id, voice_profile_id=payload.voice_profile_id,
                capability_profile_id=payload.capability_profile_id,
                cross_ip_voice_authorization_reference=payload.cross_ip_voice_authorization_reference,
            )
        except ProductionRunNotReady as exc:
            raise HTTPException(status_code=409, detail={"code": "voice_dispatch_not_ready", "reasons": exc.reasons}) from exc
        except (ProductionRunConflict, PreflightInputError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if run is None:
            raise HTTPException(status_code=404, detail="production run not found")
        return run

    @application.post("/projects/{project_id}/production-runs/{run_id}/voice-qa-advance", response_model=ProductionRun, tags=["projects"])
    async def advance_production_voice_qa(project_id: UUID, run_id: UUID, request: Request) -> ProductionRun:
        try:
            run = ProductionRunService(request.app.state.database, request.app.state.data_root).advance_voice_qa(project_id, run_id)
        except ProductionRunNotReady as exc:
            raise HTTPException(status_code=409, detail={"code": "voice_qa_not_ready", "reasons": exc.reasons}) from exc
        except (ProductionRunConflict, PreflightInputError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if run is None:
            raise HTTPException(status_code=404, detail="production run not found")
        return run

    @application.get("/projects/{project_id}/cost-reduction", response_model=CostReductionResponse, tags=["runtime"])
    async def get_project_cost_reduction(project_id: UUID, request: Request) -> CostReductionResponse:
        db: Database = request.app.state.database
        if ProjectRepository(db).get(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        draft = ProjectDraftRepository(db).get(project_id)
        if draft is None:
            return CostReductionResponse(project_id=project_id, suggestions=[], proposed_selections=[], changed_scene_count=0)
        selected = {candidate.scene_plan_id: candidate for candidate in draft.confirmed}
        suggestions: list[CostReductionSuggestion] = []
        proposed: list[CandidateAsset] = []
        for route in draft.routes:
            current = selected.get(route.scene_plan_id) or next(
                (candidate for candidate in route.candidates if candidate.recommended), route.candidates[0]
            )
            try:
                decision = reduce_candidate_cost(route.candidates, current=current)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            suggestions.append(CostReductionSuggestion(
                scene_plan_id=route.scene_plan_id,
                current=decision.current,
                suggested=decision.suggested,
                changed=decision.changed,
                reason=decision.reason,
            ))
            proposed.append(decision.suggested)
        return CostReductionResponse(
            project_id=project_id,
            suggestions=suggestions,
            proposed_selections=proposed,
            changed_scene_count=sum(suggestion.changed for suggestion in suggestions),
        )

    @application.get("/projects/{project_id}/shoot-tasks", response_model=list[ShootTask], tags=["capture"])
    async def list_shoot_tasks(project_id: UUID, request: Request) -> list[ShootTask]:
        db: Database = request.app.state.database
        if ProjectRepository(db).get(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        return ShootTaskRepository(db).list_for_project(project_id)

    @application.post("/projects/{project_id}/shoot-tasks", response_model=ShootTask, status_code=201, tags=["capture"])
    async def confirm_shoot_task(project_id: UUID, payload: ShootTaskCreateRequest, request: Request) -> ShootTask:
        db: Database = request.app.state.database
        if ProjectRepository(db).get(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        draft = ProjectDraftRepository(db).get(project_id)
        scene = next((value for value in (draft.scenes if draft else ()) if value.id == payload.scene_plan_id), None)
        if scene is None or scene.scene_id != payload.scene_id:
            raise HTTPException(status_code=422, detail="shoot task must reference a scene in the current project draft")
        repository = ShootTaskRepository(db)
        existing = repository.get_by_scene(project_id, payload.scene_plan_id)
        now = datetime.now(timezone.utc)
        if existing is not None:
            if existing.status == "fulfilled":
                return existing
            value = existing.model_copy(update={**payload.model_dump(), "status": "confirmed", "updated_at": now})
            with db.transaction():
                repository.update(value)
            return value
        value = ShootTask(project_id=project_id, status="confirmed", created_at=now, updated_at=now, **payload.model_dump())
        with db.transaction():
            repository.create(value)
        return value

    @application.patch("/shoot-tasks/{task_id}", response_model=ShootTask, tags=["capture"])
    async def update_shoot_task(task_id: UUID, payload: ShootTaskUpdateRequest, request: Request) -> ShootTask:
        db: Database = request.app.state.database
        repository = ShootTaskRepository(db)
        task = repository.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="shoot task not found")
        if task.status == "fulfilled":
            raise HTTPException(status_code=409, detail="fulfilled shoot tasks cannot be changed")
        value = task.model_copy(update={"status": payload.status, "updated_at": datetime.now(timezone.utc)})
        with db.transaction():
            repository.update(value)
        return value

    @application.get("/budget", response_model=BudgetResponse, tags=["runtime"])
    async def get_global_budget(request: Request) -> BudgetResponse:
        return _budget_response(request.app.state.database, None)

    @application.put("/budget", response_model=BudgetResponse, tags=["runtime"])
    async def set_global_budget(payload: BudgetPolicyRequest, request: Request) -> BudgetResponse:
        db: Database = request.app.state.database
        policy = BudgetPolicy(project_id=None, updated_at=datetime.now(timezone.utc), **payload.model_dump())
        with db.transaction():
            BudgetPolicyRepository(db).save(policy)
        return _budget_response(db, None)

    @application.get("/projects/{project_id}/budget", response_model=BudgetResponse, tags=["runtime"])
    async def get_project_budget(project_id: UUID, request: Request) -> BudgetResponse:
        db: Database = request.app.state.database
        if ProjectRepository(db).get(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        return _budget_response(db, project_id)

    @application.put("/projects/{project_id}/budget", response_model=BudgetResponse, tags=["runtime"])
    async def set_project_budget(project_id: UUID, payload: BudgetPolicyRequest, request: Request) -> BudgetResponse:
        db: Database = request.app.state.database
        if ProjectRepository(db).get(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        policy = BudgetPolicy(project_id=project_id, updated_at=datetime.now(timezone.utc), **payload.model_dump())
        with db.transaction():
            BudgetPolicyRepository(db).save(policy)
        return _budget_response(db, project_id)

    @application.get("/projects/{project_id}/provider-calls", response_model=list[ProviderCallRecord], tags=["runtime"])
    async def list_provider_calls(project_id: UUID, request: Request) -> list[ProviderCallRecord]:
        db: Database = request.app.state.database
        if ProjectRepository(db).get(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        return ProviderCallRepository(db).list_for_project(project_id)

    @application.post("/projects/{project_id}/provider-calls/reserve", response_model=ProviderCallResponse, status_code=201, tags=["runtime"])
    async def reserve_provider_call(project_id: UUID, payload: ProviderCallReserveRequest, request: Request) -> ProviderCallResponse:
        db: Database = request.app.state.database
        if ProjectRepository(db).get(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        try:
            value = ProviderCallLedger(db).reserve(project_id=project_id, **payload.model_dump(mode="python"))
        except BudgetLimitError as exc:
            raise HTTPException(status_code=409, detail=exc.code) from exc
        except ProviderCallError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return ProviderCallResponse(record=value, budget=_budget_response(db, project_id))

    @application.post("/projects/{project_id}/provider-calls/{call_id}/complete", response_model=ProviderCallResponse, tags=["runtime"])
    async def complete_provider_call(project_id: UUID, call_id: UUID, payload: ProviderCallCompleteRequest, request: Request) -> ProviderCallResponse:
        db: Database = request.app.state.database
        if ProjectRepository(db).get(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        try:
            value = ProviderCallLedger(db).finish(project_id=project_id, call_id=call_id, **payload.model_dump(mode="python"))
        except ProviderCallError as exc:
            status = 404 if "not found" in str(exc) else 409
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        return ProviderCallResponse(record=value, budget=_budget_response(db, project_id))

    @application.post("/projects/{project_id}/usage-events", response_model=list[AssetUsageEvent], status_code=201, tags=["publishing"])
    async def record_asset_usage(project_id: UUID, payload: AssetUsageRequest, request: Request) -> list[AssetUsageEvent]:
        db: Database = request.app.state.database
        if ProjectRepository(db).get(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        assets, clips = AssetRepository(db), ClipRepository(db)
        images, audios = ImageAssetRepository(db), AudioAssetRepository(db)
        now = datetime.now(timezone.utc)
        values: list[AssetUsageEvent] = []
        for item in payload.events:
            clip = None
            if item.media_kind == "video":
                asset = assets.get(item.media_id)
                if asset is None:
                    raise HTTPException(status_code=422, detail="video asset not found")
                if item.clip_id is not None:
                    clip = clips.get(item.clip_id)
                    if clip is None or clip.asset_id != asset.id:
                        raise HTTPException(status_code=422, detail="usage Clip does not belong to the video asset")
            elif item.media_kind == "image":
                if images.get(item.media_id) is None or item.clip_id is not None:
                    raise HTTPException(status_code=422, detail="image usage must reference an image asset without a Clip")
            elif audios.get(item.media_id) is None or item.clip_id is not None:
                raise HTTPException(status_code=422, detail="audio usage must reference an audio asset without a Clip")
            event_key = f"{project_id}:{payload.output_version}:{item.media_kind}:{item.media_id}:{item.clip_id or '-'}"
            values.append(AssetUsageEvent(
                project_id=project_id, output_version=payload.output_version, event_key=event_key,
                media_id=item.media_id, media_kind=item.media_kind, clip_id=item.clip_id, used_at=now,
            ))
        persisted: list[AssetUsageEvent] = []
        with db.transaction():
            usage_repo = AssetUsageRepository(db)
            for value in values:
                existing = usage_repo.get_by_event_key(value.event_key)
                if existing is not None:
                    persisted.append(existing)
                    continue
                usage_repo.create(value)
                if value.media_kind == "video" and value.clip_id is not None:
                    clip = clips.get(value.clip_id)
                    if clip is not None:
                        clips.update(clip.model_copy(update={"used_count": clip.used_count + 1, "last_used_at": now}))
                persisted.append(value)
        return persisted

    @application.get("/projects/{project_id}/usage-events", response_model=list[AssetUsageEvent], tags=["publishing"])
    async def list_asset_usage(project_id: UUID, request: Request) -> list[AssetUsageEvent]:
        db: Database = request.app.state.database
        if ProjectRepository(db).get(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        return AssetUsageRepository(db).list_for_project(project_id)

    @application.post("/projects/{project_id}/publications", response_model=PublicationRecord, status_code=201, tags=["publishing"])
    async def record_publication(project_id: UUID, payload: PublicationRequest, request: Request) -> PublicationRecord:
        db: Database = request.app.state.database
        if ProjectRepository(db).get(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        try:
            value = PublicationRecord(project_id=project_id, **payload.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        repository = PublicationRepository(db)
        existing = repository.get_by_key(project_id, value.output_version, value.platform)
        if existing is not None:
            if existing.model_copy(update={"id": value.id}) != value:
                raise HTTPException(status_code=409, detail="publication record already exists for this output/platform")
            return existing
        try:
            with db.transaction():
                repository.create(value)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="publication record already exists for this output/platform") from exc
        return value

    @application.get("/projects/{project_id}/publications", response_model=list[PublicationRecord], tags=["publishing"])
    async def list_publications(project_id: UUID, request: Request) -> list[PublicationRecord]:
        db: Database = request.app.state.database
        if ProjectRepository(db).get(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        return PublicationRepository(db).list_for_project(project_id)

    @application.post("/projects/{project_id}/feedback", response_model=ContentFeedback, status_code=201, tags=["publishing"])
    async def record_feedback(project_id: UUID, payload: FeedbackRequest, request: Request) -> ContentFeedback:
        db: Database = request.app.state.database
        if ProjectRepository(db).get(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        value = ContentFeedback(project_id=project_id, created_at=datetime.now(timezone.utc), **payload.model_dump())
        repository = FeedbackRepository(db)
        existing = repository.get_by_key(project_id, value.output_version)
        if existing is not None:
            if existing.model_copy(update={"id": value.id, "created_at": value.created_at}) != value:
                raise HTTPException(status_code=409, detail="feedback already exists for this output version")
            return existing
        try:
            with db.transaction():
                repository.create(value)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="feedback already exists for this output version") from exc
        return value

    @application.get("/projects/{project_id}/feedback", response_model=list[ContentFeedback], tags=["publishing"])
    async def list_feedback(project_id: UUID, request: Request) -> list[ContentFeedback]:
        db: Database = request.app.state.database
        if ProjectRepository(db).get(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        return FeedbackRepository(db).list_for_project(project_id)

    def learning_service(request: Request):
        return FeedbackLearningService(request.app.state.database, request.app.state.render_output_root)

    def learning_result(action):
        try:
            return action()
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    def use_constraint_service(request: Request):
        return SourceUseConstraintService(request.app.state.database, request.app.state.data_root)

    def use_constraint_result(action):
        try:
            return learning_result(action)
        except OSError:
            raise HTTPException(status_code=409, detail="use_constraint_evidence_unavailable") from None

    @application.get("/projects/{project_id}/source-use-constraints", tags=["planning"])
    async def source_use_constraints(project_id: UUID, request: Request):
        return use_constraint_result(lambda: use_constraint_service(request).records(project_id))

    @application.get("/projects/{project_id}/retained-render-rejections", tags=["planning"])
    async def retained_render_rejections(project_id: UUID, request: Request):
        return use_constraint_result(lambda: use_constraint_service(request).retained_choices(project_id))

    @application.post("/projects/{project_id}/source-use-constraints", status_code=201, tags=["planning"])
    async def intake_use_constraint(project_id: UUID, payload: RetainedRejectionRequest, request: Request):
        return use_constraint_result(lambda: use_constraint_service(request).intake(project_id, payload))

    @application.put("/projects/{project_id}/source-use-constraints/{constraint_id}", tags=["planning"])
    async def adopt_use_constraint(project_id: UUID, constraint_id: UUID, payload: UseConstraintStateRequest, request: Request):
        return use_constraint_result(lambda: use_constraint_service(request).state(project_id, constraint_id, payload))

    @application.get("/projects/{project_id}/source-use-constraints/{constraint_id}/versions", tags=["planning"])
    async def use_constraint_versions(project_id: UUID, constraint_id: UUID, request: Request):
        return use_constraint_result(lambda: use_constraint_service(request).history(project_id, constraint_id))

    @application.get("/projects/{project_id}/planning-observations", tags=["planning"])
    async def planning_observations(project_id: UUID, request: Request):
        return learning_result(lambda: learning_service(request).observations(project_id))

    @application.get("/projects/{project_id}/planning-preferences", tags=["planning"])
    async def planning_preferences(project_id: UUID, request: Request):
        return learning_result(lambda: learning_service(request).list(project_id))

    @application.get("/projects/{project_id}/planning-preference-selection", tags=["planning"])
    async def creator_preference_scope(project_id: UUID, request: Request):
        return learning_result(lambda: learning_service(request).selection_scope(project_id))

    @application.post("/projects/{project_id}/planning-preference-selection", status_code=201, tags=["planning"])
    async def select_creator_preference(project_id: UUID, payload: CreatorPreferenceSelectionRequest, request: Request):
        return learning_result(lambda: learning_service(request).select(project_id, payload))

    @application.post("/projects/{project_id}/planning-preferences", status_code=201, tags=["planning"])
    async def candidate_preference(project_id: UUID, payload: PreferenceCandidateRequest, request: Request):
        return learning_result(lambda: learning_service(request).candidate(project_id, payload))

    @application.put("/projects/{project_id}/planning-preferences/{preference_id}", tags=["planning"])
    async def adopt_preference(project_id: UUID, preference_id: UUID, payload: PreferenceStateRequest, request: Request):
        return learning_result(lambda: learning_service(request).set_state(project_id, preference_id, payload))

    @application.get("/projects/{project_id}/planning-preferences/{preference_id}/versions", tags=["planning"])
    async def preference_versions(project_id: UUID, preference_id: UUID, request: Request):
        return learning_result(lambda: learning_service(request).history(project_id, preference_id))

    @application.get("/projects/{project_id}/next-suggestions", response_model=list[NextSuggestion], tags=["publishing"])
    async def next_suggestions(project_id: UUID, request: Request) -> list[NextSuggestion]:
        db: Database = request.app.state.database
        if ProjectRepository(db).get(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        feedback = FeedbackRepository(db).list_for_project(project_id)
        if not feedback:
            return []
        latest = feedback[-1]
        evidence = [f"feedback:{latest.id}", f"project:{project_id}:output:{latest.output_version}"]
        suggestions: list[NextSuggestion] = []
        for field in latest.changed_fields:
            suggestions.append(NextSuggestion(
                reason=f"用户反馈要求修改 {field}",
                suggestion=f"下一稿优先重新审阅并修改 {field}，保留已确认的主题与素材边界。",
                evidence_refs=evidence,
            ))
        if latest.rejection_reason:
            suggestions.append(NextSuggestion(
                reason="上一版被拒绝且记录了原因",
                suggestion=f"下一稿先处理已记录的拒绝原因：{latest.rejection_reason}",
                evidence_refs=evidence,
            ))
        if latest.accepted and not suggestions:
            suggestions.append(NextSuggestion(
                reason="上一版已接受",
                suggestion="保留已接受的表达约束，下一稿只改变主题或角度并重新记录依据。",
                evidence_refs=evidence,
            ))
        return suggestions

    @application.get("/opportunities", response_model=list[ContentOpportunity], tags=["planning"])
    async def list_opportunities(
        request: Request,
        status: Literal["new", "used", "dismissed"] | None = Query(default=None),
    ) -> list[ContentOpportunity]:
        return ContentOpportunityRepository(request.app.state.database).list_all(status)

    @application.post("/opportunities", response_model=ContentOpportunity, status_code=201, tags=["planning"])
    async def create_opportunity(payload: ContentOpportunityRequest, request: Request) -> ContentOpportunity:
        db: Database = request.app.state.database
        dedupe_key = f"{payload.source_type}:{payload.source_ref}"
        value = ContentOpportunity(
            dedupe_key=dedupe_key,
            created_at=datetime.now(timezone.utc),
            **payload.model_dump(),
        )
        repository = ContentOpportunityRepository(db)
        existing = repository.get_by_dedupe_key(dedupe_key)
        if existing is not None:
            # Status is a user workflow field. Re-submitting the same source
            # must not reset it, but changed evidence/content is a conflict.
            comparable = existing.model_copy(update={"id": value.id, "created_at": value.created_at, "status": value.status})
            if comparable != value:
                raise HTTPException(status_code=409, detail="opportunity source already exists with different content")
            return existing
        try:
            with db.transaction():
                repository.create(value)
        except sqlite3.IntegrityError as exc:
            concurrent = repository.get_by_dedupe_key(dedupe_key)
            if concurrent is not None:
                comparable = concurrent.model_copy(update={"id": value.id, "created_at": value.created_at, "status": value.status})
                if comparable == value:
                    return concurrent
            raise HTTPException(status_code=409, detail="opportunity source already exists") from exc
        return value

    @application.patch("/opportunities/{opportunity_id}/status", response_model=ContentOpportunity, tags=["planning"])
    async def update_opportunity_status(opportunity_id: UUID, payload: OpportunityStatusRequest, request: Request) -> ContentOpportunity:
        db: Database = request.app.state.database
        repository = ContentOpportunityRepository(db)
        existing = repository.get(opportunity_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="opportunity not found")
        value = existing.model_copy(update={"status": payload.status})
        with db.transaction():
            repository.update(value)
        return value

    @application.get("/account-connections", response_model=list[AccountConnection], tags=["intelligence"])
    async def list_account_connections(request: Request) -> list[AccountConnection]:
        return AccountConnectionRepository(request.app.state.database).list()

    @application.post("/account-connections", response_model=AccountConnection, status_code=201, tags=["intelligence"])
    async def create_account_connection(payload: AccountConnectionRequest, request: Request) -> AccountConnection:
        db: Database = request.app.state.database
        value = AccountConnection(read_only=True, **payload.model_dump())
        repository = AccountConnectionRepository(db)
        existing = repository.get_by_key(value.provider, value.account_external_id)
        if existing is not None:
            if existing.model_copy(update={"id": value.id}) != value:
                raise HTTPException(status_code=409, detail="account connection already exists with different metadata")
            return existing
        try:
            with db.transaction():
                repository.create(value)
        except sqlite3.IntegrityError as exc:
            concurrent = repository.get_by_key(value.provider, value.account_external_id)
            if concurrent is not None and concurrent.model_copy(update={"id": value.id}) == value:
                return concurrent
            raise HTTPException(status_code=409, detail="account connection already exists") from exc
        return value

    @application.get("/historical-content", response_model=list[HistoricalContent], tags=["intelligence"])
    async def list_historical_content(
        request: Request,
        account_connection_id: UUID | None = Query(default=None),
    ) -> list[HistoricalContent]:
        repository = HistoricalContentRepository(request.app.state.database)
        if account_connection_id is not None:
            return repository.list_for_account(account_connection_id)
        return repository.list()

    @application.post("/historical-content", response_model=HistoricalContent, status_code=201, tags=["intelligence"])
    async def create_historical_content(payload: HistoricalContentRequest, request: Request) -> HistoricalContent:
        db: Database = request.app.state.database
        if AccountConnectionRepository(db).get(payload.account_connection_id) is None:
            raise HTTPException(status_code=404, detail="account connection not found")
        value = HistoricalContent(**payload.model_dump())
        repository = HistoricalContentRepository(db)
        existing = repository.get_by_key(value.account_connection_id, value.external_id)
        if existing is not None:
            if existing.model_copy(update={"id": value.id}) != value:
                raise HTTPException(status_code=409, detail="historical content already exists with different metadata")
            return existing
        try:
            with db.transaction():
                repository.create(value)
        except sqlite3.IntegrityError as exc:
            concurrent = repository.get_by_key(value.account_connection_id, value.external_id)
            if concurrent is not None and concurrent.model_copy(update={"id": value.id}) == value:
                return concurrent
            raise HTTPException(status_code=409, detail="historical content already exists") from exc
        return value

    @application.get("/voice-profiles", response_model=list[VoiceProfile], tags=["voice"])
    async def list_voice_profiles(request: Request, project_id: UUID | None = None, default_only: bool = False) -> list[VoiceProfile]:
        db = request.app.state.database
        profiles = VoiceProfileRepository(db).list()
        if project_id is None:
            return profiles
        project = ProjectRepository(db).get(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        ip = IPProfileRepository(db).get(project.ip_profile_id)
        return [p for p in profiles if p.consent.confirmed and p.ip_profile_id == project.ip_profile_id
            and (not default_only or ip is not None and p.id == ip.default_voice_profile_id)]

    @application.get("/voice-profiles/{profile_id}/reference-windows", tags=["voice"])
    async def list_voice_reference_windows(profile_id: UUID, request: Request) -> list[dict[str, Any]]:
        db: Database = request.app.state.database
        profile = VoiceProfileRepository(db).get(profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="voice profile not found")
        if not profile.consent.confirmed:
            raise HTTPException(status_code=422, detail="voice profile requires explicit consent")
        clip_repository, asset_repository = ClipRepository(db), AssetRepository(db)
        bound_clips = [clip for clip_id in profile.reference_clip_ids if (clip := clip_repository.get(clip_id)) is not None]
        bound_asset_ids = {clip.asset_id for clip in bound_clips}
        assets = {}
        for asset_id in bound_asset_ids:
            asset = asset_repository.get(asset_id)
            if (asset is None or asset.source_kind not in {SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET, SourceKind.STOCK}
                    or not asset.has_audio
                    or asset.metadata.get("ip_profile_id") not in (None, str(profile.ip_profile_id))):
                continue
            assets[asset.id] = asset
        # Profile Clips authorize the source Asset; discover across that
        # Asset's full indexed timeline instead of limiting search to the few
        # reference Clips already attached to the VoiceProfile.
        clips = [clip for asset_id in assets for clip in clip_repository.list_by_asset(asset_id)]
        candidates = select_voice_reference_windows(
            [clip for clip in clips if not is_confirmed_voice_reference(clip)],
            assets,
            data_root=request.app.state.data_root,
            minimum_ms=5_000,
        )
        return [
            {
                "selection": voice_reference_window_selection(window).model_dump(mode="json"),
                "transcript": window.transcript,
                "duration_ms": window.end_ms - window.start_ms,
                "source_timebase": window.source_timebase,
                "source_asset_content_hash": assets[window.asset_id].content_hash,
                "reference_audio_content_hash": window.source_content_hash,
                "candidate_rank": rank,
                "review_required": True,
            }
            for rank, window in enumerate(candidates, start=1)
        ]

    @application.post("/voice-profiles", response_model=VoiceProfile, status_code=201, tags=["voice"])
    async def create_voice_profile(payload: VoiceProfileRequest, request: Request) -> VoiceProfile:
        db: Database = request.app.state.database
        if payload.ip_profile_id is None or IPProfileRepository(db).get(payload.ip_profile_id) is None:
            raise HTTPException(status_code=422, detail="voice_profile_ip_binding_required")
        _validate_profile_reference_clips(db, payload.reference_clip_ids)
        value = VoiceProfile(**payload.model_dump())
        repository = VoiceProfileRepository(db)
        existing = repository.get(value.id) or repository.get_by_provider_profile(value.provider, value.provider_profile_id)
        if existing is not None:
            if existing.model_copy(update={"id": value.id}) != value:
                raise HTTPException(status_code=409, detail="voice profile already exists with different metadata")
            return existing
        with db.transaction():
            repository.create(value)
        return value

    @application.put("/voice-profiles/{profile_id}/reference-clip", response_model=VoiceProfile, tags=["voice"])
    async def confirm_voice_reference_clip(
        profile_id: UUID, payload: ConfirmVoiceReferenceRequest, request: Request,
    ) -> VoiceProfile:
        db: Database = request.app.state.database
        profiles = VoiceProfileRepository(db)
        profile = profiles.get(profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="voice profile not found")
        if not profile.consent.confirmed or profile.ip_profile_id is None:
            raise HTTPException(status_code=422, detail="confirmed IP-bound voice consent is required")
        if IPProfileRepository(db).get(profile.ip_profile_id) is None:
            raise HTTPException(status_code=422, detail="voice profile IP binding is unavailable")
        clips = ClipRepository(db)
        existing = [clips.get(clip_id) for clip_id in profile.reference_clip_ids]
        selection = payload.reference_window
        asset_id = selection.asset_id if selection is not None else payload.asset_id
        start_ms = selection.start_ms if selection is not None else payload.start_ms
        end_ms = selection.end_ms if selection is not None else payload.end_ms
        if not any(clip is not None and clip.asset_id == asset_id for clip in existing):
            raise HTTPException(status_code=422, detail="reference asset must already be bound to this voice profile")
        asset = AssetRepository(db).get(asset_id)
        if asset is None:
            raise HTTPException(status_code=404, detail="reference asset not found")
        source_timebase = None
        source_file = None
        source_hash = None
        source_duration_ms = None
        if selection is not None:
            if selection.source_timebase != "analysis_wav":
                raise HTTPException(status_code=422, detail="selected derived Voice reference must use its analysis-WAV timeline")
            if (asset.source_kind not in {SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET, SourceKind.STOCK}
                    or not asset.has_audio
                    or asset.metadata.get("ip_profile_id") not in (None, str(profile.ip_profile_id))):
                raise HTTPException(status_code=422, detail="reference source Asset is not eligible for this IP-bound VoiceProfile")
            candidates = select_voice_reference_windows(
                ClipRepository(db).list_by_asset(asset.id), {asset.id: asset},
                data_root=request.app.state.data_root, minimum_ms=5_000,
            )
            matched = next((window for window in candidates
                if voice_reference_window_selection(window) == selection), None)
            if matched is None or matched.source_duration_ms is None:
                raise HTTPException(status_code=409, detail="selected analysis-WAV reference is no longer current")
            source_path = Path(matched.source_file).resolve()
            root = Path(request.app.state.data_root).resolve()
            try:
                source_file = source_path.relative_to(root).as_posix()
            except ValueError:
                raise HTTPException(status_code=422, detail="derived Voice reference must remain within the configured data root") from None
            source_timebase = "analysis_wav"
            source_hash = selection.source_content_hash
            source_duration_ms = matched.source_duration_ms
        duration_ms = end_ms - start_ms
        effective_duration_ms = source_duration_ms if source_timebase == "analysis_wav" else asset.duration_ms
        if not 5_000 <= duration_ms <= 10_000 or end_ms > effective_duration_ms:
            raise HTTPException(status_code=422, detail="confirmed voice reference must be 5–10 seconds within its source timeline")
        ref_text = payload.ref_text.strip()
        suitability_basis = payload.voice_suitability_basis.strip()
        if not ref_text or len(suitability_basis) < 8:
            raise HTTPException(status_code=422, detail="exact reference text and a concrete voice-suitability basis are required")
        for clip in existing:
            if (clip is not None and is_confirmed_voice_reference(clip)
                    and clip.asset_id == asset.id
                    and clip.start_ms == start_ms and clip.end_ms == end_ms
                    and clip.transcript == ref_text
                    and clip.reference_source_timebase == source_timebase
                    and clip.reference_source_file == source_file
                    and clip.reference_source_content_hash == source_hash):
                return profile
        if len(profile.reference_clip_ids) >= 100:
            raise HTTPException(status_code=409, detail="voice profile reference clip limit reached")
        reference = Clip(
            asset_id=asset.id, start_ms=start_ms, end_ms=end_ms,
            asset_duration_ms=asset.duration_ms, transcript=ref_text,
            reference_source_timebase=source_timebase,
            reference_source_file=source_file,
            reference_source_content_hash=source_hash,
            reference_source_duration_ms=source_duration_ms,
            transcript_segments=[{"start_ms": start_ms, "end_ms": end_ms, "text": ref_text}],
            transcript_source=f"owner-confirmed-reference:{suitability_basis}",
            voice_candidate=True,
        )
        updated = profile.model_copy(update={"reference_clip_ids": [reference.id, *profile.reference_clip_ids]})
        with db.transaction(immediate=True):
            clips.create(reference)
            profiles.update(updated)
        return updated

    @application.get("/talking-profiles", response_model=list[TalkingProfile], tags=["voice"])
    async def list_talking_profiles(request: Request) -> list[TalkingProfile]:
        return TalkingProfileRepository(request.app.state.database).list()

    @application.post("/talking-profiles", response_model=TalkingProfile, status_code=201, tags=["voice"])
    async def create_talking_profile(payload: TalkingProfileRequest, request: Request) -> TalkingProfile:
        db: Database = request.app.state.database
        if payload.ip_profile_id is not None and IPProfileRepository(db).get(payload.ip_profile_id) is None:
            raise HTTPException(status_code=404, detail="IP profile not found")
        _validate_profile_reference_clips(db, payload.reference_clip_ids)
        value = TalkingProfile(**payload.model_dump())
        repository = TalkingProfileRepository(db)
        existing = repository.get(value.id)
        if existing is not None:
            if existing != value:
                raise HTTPException(status_code=409, detail="talking profile already exists with different metadata")
            return existing
        with db.transaction():
            repository.create(value)
        return value

    @application.put("/clips/{clip_id}/talking-reference-assessment", response_model=Clip, tags=["voice"])
    async def assess_talking_reference(clip_id: UUID, payload: TalkingReferenceAssessmentRequest, request: Request) -> Clip:
        """Persist observed performance evidence for a reusable Talking reference."""
        repository = ClipRepository(request.app.state.database)
        clip = repository.get(clip_id)
        if clip is None:
            raise HTTPException(status_code=404, detail="clip not found")
        updated = clip.model_copy(update={"talking_reference_assessment": payload.assessment})
        with request.app.state.database.transaction():
            repository.update(updated)
        return updated

    @application.post("/talking-profiles/{talking_profile_id}/reference-selection", response_model=TalkingReferenceSelection, tags=["voice"])
    async def select_talking_profile_reference(
        talking_profile_id: UUID,
        payload: TalkingReferenceSelectionRequest,
        request: Request,
    ) -> TalkingReferenceSelection:
        """Choose a sourced reference or produce a small, explicit capture gap."""
        db: Database = request.app.state.database
        profile = TalkingProfileRepository(db).get(talking_profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="talking profile not found")
        if not profile.consent.confirmed:
            raise HTTPException(status_code=422, detail="talking profile requires explicit consent")
        if not profile.reference_clip_ids:
            raise HTTPException(status_code=422, detail="talking_profile_motion_reference_unavailable")
        clips = ClipRepository(db)
        references = [clips.get(clip_id) for clip_id in profile.reference_clip_ids]
        if any(clip is None for clip in references):
            raise HTTPException(status_code=422, detail="talking profile reference clip is unavailable")
        return select_talking_reference([clip for clip in references if clip is not None], payload.brief)

    @application.get("/assets", response_model=list[Asset], tags=["assets"])
    async def list_assets() -> list[Asset]:
        return AssetRepository(application.state.database).list()

    @application.get("/assets/{asset_id}", response_model=Asset, tags=["assets"])
    async def get_asset(asset_id: UUID) -> Asset:
        asset = AssetRepository(application.state.database).get(asset_id)
        if asset is None:
            raise HTTPException(status_code=404, detail="asset not found")
        return asset

    @application.get("/assets/{asset_id}/clips", response_model=list[Clip], tags=["assets"])
    async def list_asset_clips(asset_id: UUID) -> list[Clip]:
        db: Database = application.state.database
        if AssetRepository(db).get(asset_id) is None:
            raise HTTPException(status_code=404, detail="asset not found")
        return ClipRepository(db).list_by_asset(asset_id)

    @application.post("/assets/{asset_id}/viewport-feasibility", response_model=ViewportFeasibility, tags=["assets"])
    async def viewport_feasibility(asset_id: UUID, payload: ViewportFeasibilityRequest, request: Request) -> ViewportFeasibility:
        if payload.proposal.source_asset_id != asset_id:
            raise HTTPException(status_code=422, detail="viewport source identity mismatch")
        try:
            return inspect_viewport(request.app.state.database, request.app.state.data_root, payload)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="asset not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.post("/assets/vertical-derivations", response_model=VerticalDerivationResponse, status_code=201, tags=["assets"])
    async def derive_vertical_asset(payload: VerifiedVerticalDerivationRequest, request: Request) -> VerticalDerivationResponse:
        """Create one verified portrait viewport without mutating its parent Asset."""
        db: Database = request.app.state.database
        service = request.app.state.vertical_derivation_factory(
            db, request.app.state.data_root,
        ) if request.app.state.vertical_derivation_factory else VerticalSourceDerivationService(
            db,
            request.app.state.data_root,
            MediaImporter(db, request.app.state.data_root, FFProbeAdapter(resolve_local_executable("ffprobe"))),
            command=resolve_local_executable("ffmpeg"),
        )
        try:
            result = service.derive(payload)
            return VerticalDerivationResponse(asset=result.asset, clip=result.clip)
        except VerticalDerivationValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except VerticalDerivationConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except VerticalDerivationProcessError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @application.get("/assets/{asset_id}/readiness", response_model=AssetReadinessResponse, tags=["assets"])
    async def get_asset_readiness(asset_id: UUID, request: Request) -> AssetReadinessResponse:
        db: Database = request.app.state.database
        if AssetRepository(db).get(asset_id) is None:
            raise HTTPException(status_code=404, detail="asset not found")
        jobs = JobRepository(db).list_for_asset(asset_id)

        def stage(job_type: JobType, *, embeddings: bool = False) -> AssetStageResponse:
            matching = [job for job in jobs if job.type is job_type
                and (not embeddings or job.payload is None or job.payload.index_embeddings)]
            if not matching:
                return AssetStageResponse(status="not_started")
            job = matching[-1]
            return AssetStageResponse(status=job.status.value, job_id=job.id, error_code=job.error_code)

        indexed = stage(JobType.INDEX_CLIPS)
        return AssetReadinessResponse(
            asset_id=asset_id,
            stages={
                "imported": AssetStageResponse(status="completed"),
                "preprocessed": stage(JobType.ANALYZE_ASSET),
                "transcript": stage(JobType.TRANSCRIBE_AUDIO),
                "visual": indexed,
                "index": stage(JobType.INDEX_CLIPS, embeddings=True),
            },
        )

    @application.get("/assets/{asset_id}/media", tags=["assets"])
    async def asset_media(asset_id: UUID, start_ms: int | None = Query(default=None, ge=0), end_ms: int | None = Query(default=None, gt=0)) -> FileResponse:
        db: Database = application.state.database
        asset = AssetRepository(db).get(asset_id)
        if asset is None:
            raise HTTPException(status_code=404, detail="asset not found")
        if (start_ms is None) != (end_ms is None) or (start_ms is not None and end_ms is not None and end_ms <= start_ms):
            raise HTTPException(status_code=422, detail="start_ms and end_ms must be a valid pair")
        if end_ms is not None and end_ms > asset.duration_ms:
            raise HTTPException(status_code=422, detail="media interval exceeds asset duration")
        try:
            source = existing_local_media_file(asset.source_file, application.state.data_root)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="asset source file not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="asset source path is not a supported local path") from exc
        _ordinary_media(source, "asset", asset.id, asset.content_hash)
        return FileResponse(source)

    @application.patch("/assets/{asset_id}/identity", response_model=Asset, tags=["assets"])
    async def update_asset_identity(asset_id: UUID, payload: AssetIdentityUpdateRequest, request: Request) -> Asset:
        db: Database = request.app.state.database
        asset = AssetRepository(db).get(asset_id)
        if asset is None:
            raise HTTPException(status_code=404, detail="asset not found")
        from app.talking.source_suitability import register_motion_source
        try:
            with db.transaction():
                return register_motion_source(db, asset, **payload.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.post("/uploads", response_model=Asset, status_code=201, tags=["assets"])
    async def upload_local_media(
        request: Request,
        file: UploadFile = File(...),
        authorization_reference: str = Form(..., min_length=1, max_length=500),
        source_kind: str = Form("user_asset"),
        shoot_task_id: UUID | None = Form(None),
    ) -> Asset:
        """Import one browser/mobile video without exposing a source path."""
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > MAX_UPLOAD_BYTES + 1_048_576:
                    raise HTTPException(status_code=413, detail="uploaded media exceeds the local size limit")
            except ValueError:
                raise HTTPException(status_code=400, detail="invalid content length") from None
        filename = Path(file.filename or "").name
        if not filename or Path(filename).suffix.lower() not in _VIDEO_SUFFIXES:
            raise HTTPException(status_code=422, detail="file must be a supported video")
        try:
            kind = SourceKind(source_kind)
        except ValueError:
            raise HTTPException(status_code=422, detail="source_kind must be user_asset or historical_asset") from None
        db: Database = request.app.state.database
        shoot_task = None
        if shoot_task_id is not None:
            shoot_task = ShootTaskRepository(db).get(shoot_task_id)
            if shoot_task is None:
                raise HTTPException(status_code=404, detail="shoot task not found")
            if shoot_task.status != "confirmed":
                raise HTTPException(status_code=409, detail="shoot task is not awaiting a capture upload")
        importer = request.app.state.media_importer_factory(
            db, request.app.state.data_root,
        ) if request.app.state.media_importer_factory else MediaImporter(
            db, request.app.state.data_root,
            FFProbeAdapter(resolve_local_executable("ffprobe")),
        )
        try:
            asset = importer.import_stream(
                _LimitedUploadStream(file.file, MAX_UPLOAD_BYTES), filename,
                authorization_reference, source_kind=kind,
            )
            if shoot_task is not None:
                updated_task = shoot_task.model_copy(update={
                    "status": "fulfilled", "asset_id": asset.id, "updated_at": datetime.now(timezone.utc),
                })
                with db.transaction():
                    ShootTaskRepository(db).update(updated_task)
            return asset
        except ValueError as exc:
            raise HTTPException(status_code=413, detail="uploaded media exceeds the local size limit") from exc
        except ProbeError as exc:
            status = 503 if "binary not found" in str(exc) else 422
            raise HTTPException(status_code=status, detail="media probe unavailable" if status == 503 else "media probe failed") from exc
        except MediaImportError as exc:
            raise HTTPException(status_code=409, detail="existing imported media is unavailable") from exc
        finally:
            await file.close()

    @application.post("/audio-uploads", response_model=AudioAsset, status_code=201, tags=["audio"])
    async def upload_local_audio(
        request: Request,
        file: UploadFile = File(...),
        authorization_reference: str = Form(..., min_length=1, max_length=500),
        source_kind: str = Form("user_asset"),
        language: str | None = Form(None, max_length=20),
    ) -> AudioAsset:
        """Import one browser/mobile narration recording without exposing a source path."""
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > MAX_UPLOAD_BYTES + 1_048_576:
                    raise HTTPException(status_code=413, detail="uploaded audio exceeds the local size limit")
            except ValueError:
                raise HTTPException(status_code=400, detail="invalid content length") from None
        filename = Path(file.filename or "").name
        if not filename or Path(filename).suffix.lower() not in _AUDIO_UPLOAD_SUFFIXES:
            raise HTTPException(status_code=422, detail="file must be a supported audio recording")
        try:
            kind = SourceKind(source_kind)
        except ValueError:
            raise HTTPException(status_code=422, detail="source_kind must be user_asset or historical_asset") from None
        db: Database = request.app.state.database
        importer = request.app.state.audio_importer_factory(
            db, request.app.state.data_root,
        ) if request.app.state.audio_importer_factory else AudioImporter(
            db, request.app.state.data_root, resolve_local_executable("ffprobe"),
        )
        try:
            return importer.import_stream(
                _LimitedUploadStream(file.file, MAX_UPLOAD_BYTES), filename,
                authorization_reference, source_kind=kind, language=language,
            )
        except ValueError as exc:
            raise HTTPException(status_code=413, detail="uploaded audio exceeds the local size limit") from exc
        except ProbeError as exc:
            status = 503 if "binary not found" in str(exc) else 422
            raise HTTPException(status_code=status, detail="audio probe unavailable" if status == 503 else "audio probe failed") from exc
        except AudioImportError as exc:
            detail = str(exc)
            raise HTTPException(status_code=409 if "missing media" in detail else 422, detail=detail) from exc
        finally:
            await file.close()

    @application.post("/imports", response_model=list[Asset], status_code=201, tags=["assets"])
    async def import_local_media(payload: MediaImportRequest, request: Request) -> list[Asset]:
        source = Path(payload.source_path).expanduser().resolve()
        if source.is_dir():
            iterator = source.rglob("*") if payload.recursive else source.iterdir()
            paths = sorted(path for path in iterator if path.is_file() and path.suffix.lower() in {
                ".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".mts", ".m2ts", ".ts",
            })
            if not paths:
                raise HTTPException(status_code=422, detail="directory contains no supported video files")
        elif source.is_file():
            paths = [source]
        else:
            raise HTTPException(status_code=404, detail="source path not found")
        db: Database = request.app.state.database
        importer = request.app.state.media_importer_factory(
            db, request.app.state.data_root,
        ) if request.app.state.media_importer_factory else MediaImporter(
            db, request.app.state.data_root,
            FFProbeAdapter(resolve_local_executable("ffprobe")),
        )
        try:
            return [
                importer.import_path(
                    path, payload.authorization_reference, source_kind=SourceKind(payload.source_kind),
                )
                for path in paths
            ]
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="source path not found") from exc
        except ProbeError as exc:
            status = 503 if "binary not found" in str(exc) else 422
            raise HTTPException(status_code=status, detail="media probe unavailable" if status == 503 else "media probe failed") from exc
        except MediaImportError as exc:
            raise HTTPException(status_code=409, detail="existing imported media is unavailable") from exc

    @application.post("/inbox/scan", response_model=InboxScanResponse, status_code=200, tags=["assets"])
    async def scan_local_inbox(payload: InboxScanRequest, request: Request) -> InboxScanResponse:
        """Poll a local directory and import only newly addressed media.

        The importer owns content-hash deduplication and persisted originals;
        this endpoint only supplies a bounded, repeatable directory scan and
        reports failures without claiming downstream analysis completed.
        """
        source = Path(payload.source_path).expanduser().resolve()
        allowed = {
            ".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".mts", ".m2ts", ".ts",
        }
        if source.is_dir():
            iterator = source.rglob("*") if payload.recursive else source.iterdir()
            paths = sorted(path for path in iterator if path.is_file() and path.suffix.lower() in allowed)
        elif source.is_file() and source.suffix.lower() in allowed:
            paths = [source]
        elif source.exists():
            raise HTTPException(status_code=422, detail="inbox source is not a supported video file or directory")
        else:
            raise HTTPException(status_code=404, detail="inbox source path not found")
        if not paths:
            return InboxScanResponse(source_path=str(source), discovered=0, imported=0, existing=0)

        db: Database = request.app.state.database
        importer = request.app.state.media_importer_factory(
            db, request.app.state.data_root,
        ) if request.app.state.media_importer_factory else MediaImporter(
            db, request.app.state.data_root,
            FFProbeAdapter(resolve_local_executable("ffprobe")),
        )
        known_ids = {asset.id for asset in AssetRepository(db).list()}
        returned: dict[UUID, Asset] = {}
        imported_ids: set[UUID] = set()
        imported = 0
        existing = 0
        errors: list[InboxScanError] = []
        for path in paths:
            try:
                asset = importer.import_path(
                    path, payload.authorization_reference, source_kind=SourceKind(payload.source_kind),
                )
            except (FileNotFoundError, ProbeError, MediaImportError, OSError) as exc:
                errors.append(InboxScanError(source_path=str(path), detail=str(exc)))
                continue
            if asset.id in known_ids:
                existing += 1
            else:
                imported += 1
                known_ids.add(asset.id)
                imported_ids.add(asset.id)
            returned[asset.id] = asset
        analysis_jobs: list[JobResponse] = []
        if payload.enqueue_analysis:
            target_store = AssetJobTargetStore(db)
            for asset_id in sorted(imported_ids, key=str):
                now = datetime.now(timezone.utc)
                job = Job(
                    id=uuid4(), project_id=None, type=JobType.ANALYZE_ASSET,
                    idempotency_key=f"inbox:analyze:{asset_id}", created_at=now, updated_at=now,
                )
                persisted = target_store.enqueue(job, asset_id)
                analysis_jobs.append(JobResponse(**_job_response(db, persisted)))
        return InboxScanResponse(
            source_path=str(source), discovered=len(paths), imported=imported,
            existing=existing, assets=list(returned.values()), analysis_jobs=analysis_jobs, errors=errors,
        )

    @application.get("/image-assets", response_model=list[ImageAsset], tags=["assets"])
    async def list_image_assets(request: Request) -> list[ImageAsset]:
        return ImageAssetRepository(request.app.state.database).list()

    @application.get("/image-assets/{image_id}", response_model=ImageAsset, tags=["assets"])
    async def get_image_asset(image_id: UUID, request: Request) -> ImageAsset:
        image = ImageAssetRepository(request.app.state.database).get(image_id)
        if image is None:
            raise HTTPException(status_code=404, detail="image asset not found")
        return image

    @application.get("/image-assets/{image_id}/media", tags=["assets"])
    async def image_asset_media(image_id: UUID, request: Request) -> FileResponse:
        image = ImageAssetRepository(request.app.state.database).get(image_id)
        if image is None:
            raise HTTPException(status_code=404, detail="image asset not found")
        try:
            source = existing_local_media_file(image.source_file, request.app.state.data_root)
        except (FileNotFoundError, ValueError):
            raise HTTPException(status_code=404, detail="image source file not found")
        _ordinary_media(source, "image", image.id, image.content_hash)
        return FileResponse(source)

    @application.post("/image-imports", response_model=list[ImageAsset], status_code=201, tags=["assets"])
    async def import_local_images(payload: ImageImportRequest, request: Request) -> list[ImageAsset]:
        source = Path(payload.source_path).expanduser().resolve()
        allowed = {".png", ".jpg", ".jpeg", ".webp"}
        if source.is_dir():
            iterator = source.rglob("*") if payload.recursive else source.iterdir()
            paths = sorted(path for path in iterator if path.is_file() and path.suffix.lower() in allowed)
            if not paths:
                raise HTTPException(status_code=422, detail="directory contains no supported image files")
        elif source.is_file():
            if source.suffix.lower() not in allowed:
                raise HTTPException(status_code=422, detail="unsupported image extension")
            paths = [source]
        else:
            raise HTTPException(status_code=404, detail="source path not found")
        db: Database = request.app.state.database
        importer = request.app.state.image_importer_factory(db, request.app.state.data_root) if request.app.state.image_importer_factory else ImageImporter(db, request.app.state.data_root)
        try:
            return [
                importer.import_path(path, payload.authorization_reference, source_kind=SourceKind(payload.source_kind))
                for path in paths
            ]
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="source path not found") from exc
        except ImageImportError as exc:
            detail = str(exc)
            raise HTTPException(status_code=409 if "missing media" in detail else 422, detail=detail) from exc

    @application.get("/audio-assets", response_model=list[AudioAsset], tags=["audio"])
    async def list_audio_assets(request: Request) -> list[AudioAsset]:
        return AudioAssetRepository(request.app.state.database).list()

    @application.get("/audio-assets/{audio_id}", response_model=AudioAsset, tags=["audio"])
    async def get_audio_asset(audio_id: UUID, request: Request) -> AudioAsset:
        audio = AudioAssetRepository(request.app.state.database).get(audio_id)
        if audio is None:
            raise HTTPException(status_code=404, detail="audio asset not found")
        return audio

    @application.get("/audio-assets/{audio_id}/performance-observation", tags=["voice"])
    async def get_voice_performance_observation(audio_id: UUID, request: Request) -> dict[str, Any]:
        """Read persisted acoustic evidence without claiming a U-Voice judgment."""
        audio = AudioAssetRepository(request.app.state.database).get(audio_id)
        if audio is None:
            raise HTTPException(status_code=404, detail="audio asset not found")
        observation = audio.metadata.get("voice_performance_observation")
        if not isinstance(observation, dict):
            raise HTTPException(status_code=404, detail="voice performance observation not found")
        return observation

    @application.get("/audio-assets/{audio_id}/boundary-alignment", tags=["voice"])
    async def get_voice_boundary_alignment(audio_id: UUID, request: Request) -> dict[str, Any]:
        """Read backfilled word/PCM evidence without changing old observations."""
        audio = AudioAssetRepository(request.app.state.database).get(audio_id)
        if audio is None:
            raise HTTPException(status_code=404, detail="audio asset not found")
        alignment = audio.metadata.get("voice_boundary_alignment")
        if not isinstance(alignment, dict):
            raise HTTPException(status_code=404, detail="voice boundary alignment not found")
        return alignment

    @application.post("/audio-assets/{audio_id}/performance-observation", tags=["voice"])
    async def create_voice_performance_observation(audio_id: UUID, request: Request) -> dict[str, Any]:
        """Analyze one QA-verified narration once and retain its source-bound evidence."""
        db: Database = request.app.state.database
        audios = AudioAssetRepository(db)
        audio = audios.get(audio_id)
        if audio is None:
            raise HTTPException(status_code=404, detail="audio asset not found")
        generation = audio.metadata.get("voice_generation")
        if not isinstance(generation, dict) or generation.get("qa_state") != "verified":
            raise HTTPException(status_code=422, detail="performance observation requires verified Voice QA")
        copy = generation.get("target_text")
        if not isinstance(copy, str) or not copy.strip():
            raise HTTPException(status_code=422, detail="performance observation requires exact requested copy")
        existing = audio.metadata.get("voice_performance_observation")
        if isinstance(existing, dict):
            return existing
        try:
            observation = observe_voice_performance(audio, copy, data_root=request.app.state.data_root)
        except VoiceObservationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        observation["observed_at"] = datetime.now(timezone.utc).isoformat()
        metadata = dict(audio.metadata)
        metadata["voice_performance_observation"] = observation
        with db.transaction():
            audios.update(audio.model_copy(update={"metadata": metadata}))
        return observation

    @application.get("/audio-assets/{audio_id}/access", tags=["audio"])
    async def audio_asset_access(audio_id: UUID, request: Request, project_id: UUID | None = None, operation: str = "review") -> dict:
        audio = AudioAssetRepository(request.app.state.database).get(audio_id)
        if audio is None:
            raise HTTPException(status_code=404, detail="audio asset not found")
        status, _ = AudioAccess(request.app.state.database, request.app.state.data_root).inspect(
            audio, project_id=project_id, operation=operation)
        return status

    @application.get("/audio-assets/{audio_id}/media", tags=["audio"])
    async def audio_asset_media(audio_id: UUID, request: Request, project_id: UUID | None = None, operation: str = "review") -> FileResponse:
        audio = AudioAssetRepository(request.app.state.database).get(audio_id)
        if audio is None:
            raise HTTPException(status_code=404, detail="audio asset not found")
        status, source = AudioAccess(request.app.state.database, request.app.state.data_root).inspect(
            audio, project_id=project_id, operation=operation)
        if source is None:
            raise HTTPException(status_code=404 if status["reasons"][0] == "audio_source_missing" else 403,
                detail=status["reasons"][0])
        return FileResponse(source, headers={"Cache-Control": "no-store"} if status["restricted"] else None)

    @application.post("/audio-assets/{audio_id}/jobs/transcribe", response_model=JobResponse, status_code=201, tags=["audio"])
    async def enqueue_audio_asset_transcription(
        audio_id: UUID, payload: AudioAssetTranscriptionEnqueueRequest, request: Request,
    ) -> dict[str, Any]:
        db: Database = request.app.state.database
        audio = AudioAssetRepository(db).get(audio_id)
        if audio is None:
            raise HTTPException(status_code=404, detail="audio asset not found")
        if ProjectRepository(db).get(payload.project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        from app.media.source_audio import admitted_source_audio_binding
        if admitted_source_audio_binding(db, audio, payload.project_id, require_transcript=False) is None:
            raise HTTPException(status_code=409, detail="audio source binding is unavailable for this project")
        access, _ = AudioAccess(db, request.app.state.data_root).inspect(
            audio, project_id=payload.project_id, operation="review",
        )
        if access["state"] != "available":
            raise HTTPException(status_code=409, detail=access["reasons"][0])
        now = datetime.now(timezone.utc)
        job = Job(
            id=uuid4(), project_id=payload.project_id, type=JobType.TRANSCRIBE_AUDIO_ASSET,
            idempotency_key=payload.idempotency_key, created_at=now, updated_at=now,
            payload=AudioAssetTranscriptionJobPayload(audio_asset_id=audio.id, source_sha256=audio.content_hash),
        )
        try:
            persisted = AudioAssetJobTargetStore(db).enqueue(job, audio.id)
        except AssetJobIdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=404, detail="audio target not found") from exc
        return _job_response(db, persisted)

    @application.post("/audio-imports", response_model=list[AudioAsset], status_code=201, tags=["audio"])
    async def import_local_audio(payload: AudioImportRequest, request: Request) -> list[AudioAsset]:
        source = Path(payload.source_path).expanduser().resolve()
        allowed = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".webm"}
        if source.is_dir():
            iterator = source.rglob("*") if payload.recursive else source.iterdir()
            paths = sorted(path for path in iterator if path.is_file() and path.suffix.lower() in allowed)
            if not paths:
                raise HTTPException(status_code=422, detail="directory contains no supported audio files")
        elif source.is_file():
            if source.suffix.lower() not in allowed:
                raise HTTPException(status_code=422, detail="unsupported audio extension")
            paths = [source]
        else:
            raise HTTPException(status_code=404, detail="source path not found")
        db: Database = request.app.state.database
        importer = request.app.state.audio_importer_factory(db, request.app.state.data_root) if request.app.state.audio_importer_factory else AudioImporter(db, request.app.state.data_root, resolve_local_executable("ffprobe"))
        try:
            return [
                importer.import_path(path, payload.authorization_reference, source_kind=SourceKind(payload.source_kind), language=payload.language)
                for path in paths
            ]
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="source path not found") from exc
        except AudioImportError as exc:
            detail = str(exc)
            raise HTTPException(status_code=409 if "missing media" in detail else 422, detail=detail) from exc

    @application.post("/projects/{project_id}/source-audio", response_model=AudioAsset, status_code=201, tags=["audio"])
    async def derive_project_source_audio(
        project_id: UUID, payload: SourceAudioDeriveRequest, request: Request,
    ) -> AudioAsset:
        db: Database = request.app.state.database
        service = SourceAudioDerivationService(
            db, request.app.state.data_root,
            ffmpeg_command=resolve_local_executable("ffmpeg"),
            ffprobe_command=resolve_local_executable("ffprobe"),
        )
        try:
            return service.derive(
                project_id, payload.asset_id, payload.clip_id, payload.start_ms, payload.end_ms,
            )
        except SourceAudioDerivationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.post("/assets/{asset_id}/transcript-imports", response_model=TranscriptImportResponse, status_code=201, tags=["assets"])
    async def import_local_transcript(asset_id: UUID, payload: TranscriptImportRequest, request: Request) -> TranscriptImportResponse:
        """Persist user-provided, timestamped SRT/VTT without inventing ASR semantics."""
        db: Database = request.app.state.database
        if AssetRepository(db).get(asset_id) is None:
            raise HTTPException(status_code=404, detail="asset not found")
        source = Path(payload.source_path).expanduser().resolve()
        try:
            segments = parse_subtitle_file(source)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="subtitle source path not found") from exc
        except SubtitleParseError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            updated = ClipTranscriptPersistence(db).apply(
                asset_id, segments, source_reference=payload.source_reference.strip(),
            )
        except NoClipsForAsset as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return TranscriptImportResponse(
            asset_id=asset_id,
            source_reference=payload.source_reference.strip(),
            segment_count=len(segments),
            updated_clip_count=len(updated),
        )

    @application.post("/audio-assets/{audio_id}/transcript-imports", response_model=AudioTranscriptImportResponse, status_code=201, tags=["audio"])
    async def import_local_audio_transcript(audio_id: UUID, payload: TranscriptImportRequest, request: Request) -> AudioTranscriptImportResponse:
        """Attach user-provided timed text to an authorized narration recording."""
        source = Path(payload.source_path).expanduser().resolve()
        try:
            segments = parse_subtitle_file(source)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="subtitle source path not found") from exc
        except SubtitleParseError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            updated = AudioTranscriptPersistence(request.app.state.database).apply(
                audio_id, segments, source_reference=payload.source_reference.strip(),
            )
        except AudioAssetNotFound as exc:
            raise HTTPException(status_code=404, detail="audio asset not found") from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return AudioTranscriptImportResponse(
            audio_asset_id=updated.id,
            source_reference=updated.transcript_source or payload.source_reference.strip(),
            segment_count=len(updated.transcript_segments),
        )

    @application.post("/analysis-results", response_model=AnalysisResultBundle, status_code=201, tags=["analysis"])
    async def import_analysis_results(bundle: AnalysisResultBundle, request: Request) -> AnalysisResultBundle:
        """Persist a sourced analysis package and replay its clip semantics locally.

        This is the assisted-test bridge: it accepts model-produced results with
        provenance, but never invents semantic values when a result is absent.
        Reusing an input hash is idempotent and conflicting payloads are rejected.
        """
        db: Database = request.app.state.database
        repository = AnalysisResultRepository(db)
        existing = repository.get_by_input_hash(bundle.input_hash)
        if existing is not None:
            if existing.model_copy(update={"id": bundle.id}) != bundle:
                raise HTTPException(status_code=409, detail="input_hash already has a different analysis result")
            return existing
        assets = AssetRepository(db)
        clips = ClipRepository(db)
        if bundle.ip_profile_id is not None and IPProfileRepository(db).get(bundle.ip_profile_id) is None:
            raise HTTPException(status_code=422, detail="analysis IP profile not found")
        asset_values: dict[UUID, Asset] = {}
        for result in bundle.results:
            asset = assets.get(result.asset_id)
            if asset is None:
                raise HTTPException(status_code=422, detail=f"analysis asset not found: {result.asset_id}")
            if result.end_ms > asset.duration_ms:
                raise HTTPException(status_code=422, detail="analysis clip interval exceeds asset duration")
            existing_clip = clips.get(result.clip_id)
            if existing_clip is not None and existing_clip.asset_id != result.asset_id:
                raise HTTPException(status_code=422, detail="analysis clip belongs to a different asset")
            asset_values[result.asset_id] = asset
        try:
            with db.transaction():
                repository.create(bundle)
                for result in bundle.results:
                    asset = asset_values[result.asset_id]
                    clips.upsert(Clip(
                        id=result.clip_id,
                        asset_id=result.asset_id,
                        start_ms=result.start_ms,
                        end_ms=result.end_ms,
                        asset_duration_ms=asset.duration_ms,
                        transcript=result.transcript,
                        visual_description=result.visual_description,
                        people=result.people,
                        objects=result.objects,
                        action=result.action,
                        talking_candidate=bool(result.transcript),
                        voice_candidate=bool(result.transcript),
                    ))
                    metadata = dict(asset.metadata)
                    from app.media.vision import imported_visual_facts
                    visual_analysis = dict(metadata.get("visual_analysis", {}))
                    visual_analysis[str(result.clip_id)] = imported_visual_facts(asset, result)
                    metadata["visual_analysis"] = visual_analysis
                    metadata["r1_analysis"] = {
                        "bundle_id": str(bundle.id),
                        "mode": bundle.mode,
                        "source": bundle.source,
                        "model": bundle.model,
                        "tool": bundle.tool,
                        "analyzed_at": bundle.analyzed_at.isoformat(),
                    }
                    asset = asset.model_copy(update={"metadata": metadata})
                    assets.update(asset)
                    asset_values[result.asset_id] = asset
        except sqlite3.IntegrityError as exc:
            existing = repository.get_by_input_hash(bundle.input_hash)
            if existing is not None:
                return existing
            raise HTTPException(status_code=409, detail="analysis result could not be persisted") from exc
        return bundle

    @application.get("/analysis-results/{bundle_id}", response_model=AnalysisResultBundle, tags=["analysis"])
    async def get_analysis_results(bundle_id: UUID, request: Request) -> AnalysisResultBundle:
        bundle = AnalysisResultRepository(request.app.state.database).get(bundle_id)
        if bundle is None:
            raise HTTPException(status_code=404, detail="analysis result not found")
        return bundle

    @application.post("/assets/{asset_id}/jobs/{job_type}", response_model=JobResponse, status_code=201)
    async def enqueue_asset_job(asset_id: UUID, job_type: JobType, payload: JobEnqueueRequest, request: Request) -> dict[str, Any]:
        db: Database = request.app.state.database
        if AssetRepository(db).get(asset_id) is None:
            raise HTTPException(status_code=404, detail="asset not found")
        if payload.project_id is not None and ProjectRepository(db).get(payload.project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        scope = None
        if not payload.index_embeddings and payload.clip_ids is None:
            raise HTTPException(status_code=422, detail="visual-only refresh requires explicit clip_ids")
        if payload.clip_ids is not None:
            known = {clip.id for clip in ClipRepository(db).list_by_asset(asset_id)}
            if (job_type is not JobType.INDEX_CLIPS or len(set(payload.clip_ids)) != len(payload.clip_ids)
                or not set(payload.clip_ids) <= known):
                raise HTTPException(status_code=422, detail="clip_ids must uniquely select existing Clips of this Asset for index_clips")
            scope = ClipIndexJobPayload(clip_ids=payload.clip_ids, index_embeddings=payload.index_embeddings)
        now = datetime.now(timezone.utc)
        job = Job(id=uuid4(), project_id=payload.project_id, type=job_type, idempotency_key=payload.idempotency_key, created_at=now, updated_at=now, payload=scope)
        try:
            persisted = AssetJobTargetStore(db).enqueue(job, asset_id)
        except AssetJobIdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except UnsupportedAssetJobType as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=404, detail="asset target not found") from exc
        return _job_response(db, persisted)

    @application.get("/jobs/{job_id}", response_model=JobResponse)
    async def get_job(job_id: UUID, request: Request) -> dict[str, Any]:
        db: Database = request.app.state.database
        job = JobRepository(db).get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return _job_response(db, job)

    @application.post("/projects/{project_id}/render-jobs", response_model=JobResponse, status_code=201, tags=["render"])
    async def enqueue_render_project(project_id: UUID, payload: RenderJobEnqueueRequest, request: Request) -> dict[str, Any]:
        """Persist a render request; a local worker performs the expensive work."""
        db: Database = request.app.state.database
        project = ProjectRepository(db).get(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        spec = _resolve_render_spec(db, project, project_id, payload)
        now = datetime.now(timezone.utc)
        render_payload = RenderVideoJobPayload(project_id=project_id, render_id=uuid4(), video_spec=spec)
        job = Job(
            id=uuid4(), project_id=project_id, type=JobType.RENDER,
            idempotency_key=payload.idempotency_key, created_at=now, updated_at=now,
            payload=render_payload,
        )
        from app.source_usage import SourceUsageService, SourceUsageConflict
        with db.transaction(immediate=True):
            existing = JobRepository(db).get_by_idempotency_key(payload.idempotency_key)
            if existing is None:
                try:
                    usage = SourceUsageService(db)
                    usage.require_available(project_id, usage.spec_uses(spec))
                except SourceUsageConflict as exc:
                    raise HTTPException(status_code=409, detail={"reasons":[exc.reason]}) from exc
            persisted = existing or JobRepository(db).create(job)
        if (
            persisted.type is not JobType.RENDER
            or persisted.project_id != project_id
            or persisted.payload is None
            or persisted.payload.video_spec != spec
        ):
            raise HTTPException(status_code=409, detail="idempotency key is already used for a different job")
        return _job_response(db, persisted)

    @application.post("/projects/{project_id}/voice-jobs", response_model=JobResponse, status_code=201, tags=["voice"])
    async def enqueue_voice_generation(project_id: UUID, payload: VoiceGenerationJobRequest, request: Request) -> dict[str, Any]:
        """Persist one authorized VoiceProvider request for a local worker."""
        db: Database = request.app.state.database
        if ProjectRepository(db).get(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        from app.voice_plan import resolve_project_voice_profile
        try:
            profile = resolve_project_voice_profile(db,project_id,payload.voice_profile_id,payload.cross_ip_voice_authorization_reference)
        except UseAdmissionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        payload = payload.model_copy(update={"voice_profile_id":profile.id})
        if payload.delivery_text is not None:
            try:
                validate_voice_delivery_text(payload.text, payload.delivery_text)
            except VoiceDeliveryTextError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        if payload.delivery_text is not None and payload.use_draft_performance_plan:
            raise HTTPException(status_code=422, detail="delivery text and draft performance plan cannot yet be applied together")
        if payload.reference_window is not None and payload.use_draft_performance_plan:
            raise HTTPException(status_code=422, detail="selected reference and draft performance plan cannot yet be applied together")
        clips = [clip for clip_id in profile.reference_clip_ids if (clip := ClipRepository(db).get(clip_id)) is not None]
        assets = {asset.id: asset for clip in clips if (asset := AssetRepository(db).get(clip.asset_id)) is not None}
        choices = authorized_voice_reference_choices(profile, clips, assets, data_root=request.app.state.data_root)
        if not choices:
            raise HTTPException(status_code=422, detail="voice profile has no user-confirmed default reference")
        if payload.reference_window is None and not payload.use_draft_performance_plan:
            payload = payload.model_copy(update={"reference_window": choices[0][0]})
        elif payload.reference_window is not None:
            try:
                resolve_authorized_voice_reference_choice(payload.reference_window, choices)
            except VoicePerformancePlanningError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        performance_plan = None
        if payload.use_draft_performance_plan:
            draft = ProjectDraftRepository(db).get(project_id)
            if draft is None or draft.narration_performance_plan is None:
                raise HTTPException(status_code=422, detail="requested Voice performance plan is unavailable from the current draft")
            try:
                performance_plan = validate_narration_performance_plan(draft.narration_performance_plan, payload.text)
            except NarrationPerformancePlanError as exc:
                raise HTTPException(status_code=422, detail="requested Voice text does not match the current narration performance plan") from exc
        now = datetime.now(timezone.utc)
        voice_payload = VoiceGenerationJobPayload(
            project_id=project_id,
            voice_profile_id=payload.voice_profile_id,
            cross_ip_voice_authorization_reference=payload.cross_ip_voice_authorization_reference,
            text=payload.text,
            delivery_text=payload.delivery_text,
            authorization_reference=(profile.consent.authorization_reference
                or payload.cross_ip_voice_authorization_reference or f"voice-profile-consent:{profile.id}"),
            language=payload.language,
            narration_performance_plan=performance_plan,
            reference_window=payload.reference_window,
        )
        job = Job(
            id=uuid4(), project_id=project_id, type=JobType.GENERATE_VOICE,
            idempotency_key=payload.idempotency_key, created_at=now, updated_at=now, payload=voice_payload,
        )
        persisted = JobRepository(db).create(job)
        if (
            persisted.type is not JobType.GENERATE_VOICE
            or persisted.project_id != project_id
            or persisted.payload != voice_payload
        ):
            raise HTTPException(status_code=409, detail="idempotency key is already used for a different job")
        return _job_response(db, persisted)

    @application.post("/projects/{project_id}/voice-pace-candidate-jobs", response_model=JobResponse, status_code=201, tags=["voice"])
    async def enqueue_voice_pace_candidate(project_id: UUID, payload: VoicePaceCandidateJobRequest, request: Request) -> dict[str, Any]:
        """Request one conservative whole-take pace variant of a verified source."""
        db: Database = request.app.state.database
        if ProjectRepository(db).get(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        audios = AudioAssetRepository(db)
        jobs = JobRepository(db)
        source = audios.get(payload.narration_audio_id)
        if source is None:
            raise HTTPException(status_code=404, detail="narration audio not found")
        if voice_asset_project_id(source, jobs) != project_id:
            raise HTTPException(status_code=422, detail="narration does not belong to project")
        generation = source.metadata.get("voice_generation")
        qa = generation.get("qa") if isinstance(generation, dict) else None
        if (
            not isinstance(generation, dict) or generation.get("qa_state") != "verified"
            or not isinstance(qa, dict) or qa.get("qa_state") != "verified"
            or not isinstance(generation.get("target_text"), str) or not generation["target_text"].strip()
            or generation.get("provider") in {"content_os_local_pace", "derived_audio_tempo_evaluation"}
            or "pace_candidate" in generation or not source.authorization_reference
        ):
            raise HTTPException(status_code=422, detail="pace candidate requires an authorized, independently QA-verified original narration")
        job_payload = VoicePaceCandidateJobPayload(
            project_id=project_id, narration_audio_id=source.id,
            source_sha256=source.content_hash, profile=payload.profile,
        )
        now = datetime.now(timezone.utc)
        job = Job(
            id=uuid4(), project_id=project_id, type=JobType.CREATE_VOICE_PACE_CANDIDATE,
            idempotency_key=payload.idempotency_key, created_at=now, updated_at=now,
            payload=job_payload,
        )
        persisted = jobs.create(job)
        if persisted.type is not JobType.CREATE_VOICE_PACE_CANDIDATE or persisted.project_id != project_id or persisted.payload != job_payload:
            raise HTTPException(status_code=409, detail="idempotency key is already used for a different job")
        return _job_response(db, persisted)

    @application.post("/projects/{project_id}/voice-qa-jobs", response_model=JobResponse, status_code=201, tags=["voice"])
    async def enqueue_voice_qa(project_id: UUID, payload: VoiceQaJobRequest, request: Request) -> dict[str, Any]:
        """Persist one local real-ASR QA request for a generated narration."""
        db: Database = request.app.state.database
        if ProjectRepository(db).get(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        narration = AudioAssetRepository(db).get(payload.narration_audio_id)
        generation = None if narration is None else narration.metadata.get("voice_generation")
        if narration is None:
            raise HTTPException(status_code=404, detail="narration audio not found")
        if not isinstance(generation, dict):
            raise HTTPException(status_code=422, detail="voice QA requires a generated narration asset")
        expected_text = generation.get("target_text")
        if expected_text is None and isinstance(generation.get("job_id"), str):
            try:
                source_job = JobRepository(db).get(UUID(generation["job_id"]))
            except (ValueError, TypeError):
                source_job = None
            if source_job is not None and source_job.type is JobType.GENERATE_VOICE and isinstance(source_job.payload, VoiceGenerationJobPayload):
                expected_text = source_job.payload.text
        if not isinstance(expected_text, str) or expected_text.strip() != payload.target_text.strip():
            raise HTTPException(status_code=422, detail="voice QA target text must match the generated request")
        if generation.get("qa_state") == "verified":
            raise HTTPException(status_code=409, detail="narration audio is already Voice QA verified")
        now = datetime.now(timezone.utc)
        qa_payload = VoiceQaJobPayload(
            project_id=project_id,
            narration_audio_id=payload.narration_audio_id,
            target_text=payload.target_text,
        )
        job = Job(
            id=uuid4(), project_id=project_id, type=JobType.VERIFY_VOICE,
            idempotency_key=payload.idempotency_key, created_at=now, updated_at=now, payload=qa_payload,
        )
        persisted = JobRepository(db).create(job)
        if persisted.type is not JobType.VERIFY_VOICE or persisted.project_id != project_id or persisted.payload != qa_payload:
            raise HTTPException(status_code=409, detail="idempotency key is already used for a different job")
        return _job_response(db, persisted)

    @application.post("/projects/{project_id}/voice-boundary-alignment-jobs", response_model=JobResponse, status_code=201, tags=["voice"])
    async def enqueue_voice_boundary_alignment(project_id: UUID, payload: VoiceBoundaryAlignmentJobRequest, request: Request) -> dict[str, Any]:
        """Backfill local word timing for an old verified asset, never rerun QA."""
        db: Database = request.app.state.database
        jobs = JobRepository(db)
        audio = AudioAssetRepository(db).get(payload.narration_audio_id)
        if ProjectRepository(db).get(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        if audio is None:
            raise HTTPException(status_code=404, detail="narration audio not found")
        if voice_asset_project_id(audio, jobs) != project_id:
            raise HTTPException(status_code=422, detail="narration does not belong to project")
        generation = audio.metadata.get("voice_generation")
        qa = generation.get("qa") if isinstance(generation, dict) else None
        if not isinstance(generation, dict) or generation.get("qa_state") != "verified" or not isinstance(qa, dict) or qa.get("qa_state") != "verified":
            raise HTTPException(status_code=422, detail="boundary alignment requires independently verified Voice QA")
        job_payload = VoiceBoundaryAlignmentJobPayload(project_id=project_id, narration_audio_id=audio.id, source_sha256=audio.content_hash)
        existing = jobs.get_by_idempotency_key(payload.idempotency_key)
        if existing is not None:
            if existing.type is not JobType.ALIGN_VOICE_BOUNDARIES or existing.project_id != project_id or existing.payload != job_payload:
                raise HTTPException(status_code=409, detail="idempotency key is already used for a different job")
            return _job_response(db, existing)
        if "voice_word_timing" in audio.metadata or "voice_boundary_alignment" in audio.metadata:
            raise HTTPException(status_code=409, detail="word-boundary evidence already exists")
        if any(
            prior.type is JobType.ALIGN_VOICE_BOUNDARIES
            and isinstance(prior.payload, VoiceBoundaryAlignmentJobPayload)
            and prior.payload.narration_audio_id == audio.id
            and prior.status in {JobStatus.PENDING, JobStatus.RUNNING, JobStatus.COMPLETED}
            for prior in jobs.list()
        ):
            raise HTTPException(status_code=409, detail="word-boundary alignment job already exists for this audio")
        now = datetime.now(timezone.utc)
        job = Job(id=uuid4(), project_id=project_id, type=JobType.ALIGN_VOICE_BOUNDARIES,
                  idempotency_key=payload.idempotency_key, created_at=now, updated_at=now, payload=job_payload)
        persisted = jobs.create(job)
        if persisted.type is not JobType.ALIGN_VOICE_BOUNDARIES or persisted.project_id != project_id or persisted.payload != job_payload:
            raise HTTPException(status_code=409, detail="idempotency key is already used for a different job")
        return _job_response(db, persisted)

    @application.post(
        "/projects/{project_id}/voice-assets/{audio_id}/human-review",
        response_model=AudioAsset,
        tags=["voice"],
    )
    async def record_voice_human_review(
        project_id: UUID, audio_id: UUID, payload: VoiceHumanReviewRequest, request: Request,
    ) -> AudioAsset:
        """Persist one irreversible U-Voice decision for an exact QA-verified asset."""

        db: Database = request.app.state.database
        if ProjectRepository(db).get(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        audios = AudioAssetRepository(db)
        audio = audios.get(audio_id)
        if audio is None:
            raise HTTPException(status_code=404, detail="narration audio not found")
        if not _voice_audio_belongs_to_project(db, audio, project_id):
            raise HTTPException(status_code=422, detail="narration audio does not belong to the requested project")
        try:
            updated = apply_voice_human_review(
                audio,
                payload.review(reviewed_at=datetime.now(timezone.utc)),
            )
        except (ValueError, VoiceQaError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        with db.transaction():
            audios.update(updated)
        return updated

    @application.post(
        "/projects/{project_id}/talking-assets/{asset_id}/human-review",
        response_model=Asset,
        tags=["voice"],
    )
    async def record_talking_human_review(
        project_id: UUID, asset_id: UUID, payload: TalkingHumanReviewRequest, request: Request,
    ) -> Asset:
        """Persist one immutable U-Talking decision for the exact project child video."""

        db: Database = request.app.state.database
        if ProjectRepository(db).get(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        assets = AssetRepository(db)
        asset = assets.get(asset_id)
        if asset is None:
            raise HTTPException(status_code=404, detail="Talking video asset not found")
        generation = asset.metadata.get("talking_generation")
        job_id = generation.get("job_id") if isinstance(generation, dict) else None
        try:
            job = JobRepository(db).get(UUID(job_id)) if isinstance(job_id, str) else None
        except ValueError:
            job = None
        if (
            job is None or job.project_id != project_id or job.type is not JobType.GENERATE_TALKING
            or job.status is not JobStatus.COMPLETED
            or not isinstance(job.payload, TalkingGenerationJobPayload)
            or generation.get("narration_audio_id") != str(job.payload.narration_audio_id)
        ):
            raise HTTPException(status_code=422, detail="Talking video does not belong to a completed project job")
        try:
            updated = apply_talking_human_review(asset, payload.review())
        except TalkingQaError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        with db.transaction():
            assets.update(updated)
        return updated

    @application.post(
        "/projects/{project_id}/master-narration-candidates",
        response_model=AudioAsset,
        status_code=201,
        tags=["voice"],
    )
    async def compose_master_narration(
        project_id: UUID, payload: MasterNarrationCompositionRequest, request: Request,
    ) -> AudioAsset:
        """Compose verified source takes into a new candidate awaiting master-level Voice QA."""

        db: Database = request.app.state.database
        if ProjectRepository(db).get(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        importer = AudioImporter(db, request.app.state.data_root, FFProbeAdapter(resolve_local_executable("ffprobe")))
        service = MasterNarrationComposer(
            audios=AudioAssetRepository(db), jobs=JobRepository(db), importer=importer,
            composer=VoiceTakeComposer(resolve_local_executable("ffmpeg"), data_root=request.app.state.data_root),
            output_root=request.app.state.data_root / "generated" / "master-narration",
        )
        try:
            return service.compose(project_id, payload.take_audio_ids, compact_quiet_seams=payload.compact_quiet_seams)
        except MasterNarrationCompositionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.get(
        "/projects/{project_id}/talking-slice-series/{series_id}",
        response_model=TalkingSliceSeriesReviewResponse,
        tags=["voice"],
    )
    async def get_talking_slice_series_review(
        project_id: UUID, series_id: UUID, request: Request,
    ) -> dict[str, Any]:
        """Read the prerequisites for a human continuity review; do not approve it."""

        db: Database = request.app.state.database
        series = TalkingSliceSeriesRepository(db).get(series_id)
        if series is None or series.project_id != project_id:
            raise HTTPException(status_code=404, detail="Talking slice series not found")
        if is_planned_talking_collection(db, series):
            return PlannedTalkingAdmissionService(db, request.app.state.data_root).review_status(series)
        review = assess_talking_slice_series(
            series,
            [JobRepository(db).get(job_id) for job_id in series.child_job_ids],
            AssetRepository(db).list(),
        )
        continuity = TalkingSliceSeriesContinuityReviewRepository(db).get_by_series_id(series.id)
        return {
            "id": review.series_id,
            "readiness": review.readiness,
            "ready_for_human_continuity_review": review.ready_for_human_continuity_review,
            "children": [
                {
                    "series_index": child.series_index,
                    "job_id": child.job_id,
                    "job_status": child.job_status,
                    "output_asset_id": child.output_asset_id,
                    "automated_qa_state": child.automated_qa_state,
                    "human_review_state": child.human_review_state,
                    "blockers": list(child.blockers),
                }
                for child in review.children
            ],
            "blockers": list(review.blockers),
            "continuity_review_state": (
                "not_submitted" if continuity is None else "approved" if continuity.approved else "rejected"
            ),
            "continuity_review_evidence_reference": None if continuity is None else continuity.evidence_reference,
            "continuity_review_findings": [] if continuity is None else list(continuity.findings),
        }

    @application.get(
        "/projects/{project_id}/talking-slice-series",
        response_model=list[TalkingSliceSeriesReviewResponse],
        tags=["voice"],
    )
    async def list_talking_slice_series_reviews(project_id: UUID, request: Request) -> list[dict[str, Any]]:
        """List project-local series evidence; this endpoint has no execution side effects."""

        db: Database = request.app.state.database
        if ProjectRepository(db).get(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        jobs = JobRepository(db)
        assets = AssetRepository(db).list()
        continuity_reviews = TalkingSliceSeriesContinuityReviewRepository(db)
        values: list[dict[str, Any]] = []
        for series in TalkingSliceSeriesRepository(db).list_for_project(project_id):
            if is_planned_talking_collection(db, series):
                values.append(PlannedTalkingAdmissionService(db, request.app.state.data_root).review_status(series))
                continue
            review = assess_talking_slice_series(series, [jobs.get(job_id) for job_id in series.child_job_ids], assets)
            continuity = continuity_reviews.get_by_series_id(series.id)
            values.append({
                "id": review.series_id,
                "readiness": review.readiness,
                "ready_for_human_continuity_review": review.ready_for_human_continuity_review,
                "children": [
                    {
                        "series_index": child.series_index,
                        "job_id": child.job_id,
                        "job_status": child.job_status,
                        "output_asset_id": child.output_asset_id,
                        "automated_qa_state": child.automated_qa_state,
                        "human_review_state": child.human_review_state,
                        "blockers": list(child.blockers),
                    }
                    for child in review.children
                ],
                "blockers": list(review.blockers),
                "continuity_review_state": (
                    "not_submitted" if continuity is None else "approved" if continuity.approved else "rejected"
                ),
                "continuity_review_evidence_reference": None if continuity is None else continuity.evidence_reference,
                "continuity_review_findings": [] if continuity is None else list(continuity.findings),
            })
        return values

    @application.post(
        "/projects/{project_id}/talking-slice-series/{series_id}/continuity-review",
        response_model=TalkingSliceSeriesContinuityReviewResponse,
        status_code=201,
        tags=["voice"],
    )
    async def submit_talking_slice_series_continuity_review(
        project_id: UUID,
        series_id: UUID,
        payload: TalkingSliceSeriesContinuityReviewRequest,
        request: Request,
    ) -> dict[str, Any]:
        """Persist one immutable human continuity decision after all prerequisites."""

        db: Database = request.app.state.database
        series = TalkingSliceSeriesRepository(db).get(series_id)
        if series is None or series.project_id != project_id:
            raise HTTPException(status_code=404, detail="Talking slice series not found")
        reviews = TalkingSliceSeriesContinuityReviewRepository(db)

        def response_for(review: TalkingSliceSeriesContinuityReview) -> dict[str, Any]:
            return {
                "id": review.id,
                "series_id": review.series_id,
                "approved": review.approved,
                "evidence_reference": review.evidence_reference,
                "findings": list(review.findings),
                "child_job_ids": list(review.child_job_ids),
                "planned_origin_sha256": review.planned_origin_sha256,
                "preview_asset_id": review.preview_asset_id,
                "preview_sha256": review.preview_sha256,
                "preview_qa_sha256": review.preview_qa_sha256,
                "reviewed_at": review.reviewed_at,
                "review_policy_version": review.review_policy_version,
                "dimensions": review.dimensions,
                "scoped_findings": review.scoped_findings,
            }

        if is_planned_talking_collection(db, series):
            if series.planned_origin is None or payload.preview_asset_id is None or payload.preview_sha256 is None:
                raise HTTPException(status_code=409, detail="planned continuity review requires exact preview identity")
            try:
                return response_for(PlannedTalkingAdmissionService(db, request.app.state.data_root).record_review(
                    series, approved=payload.approved, evidence_reference=payload.evidence_reference,
                    findings=payload.findings, preview_asset_id=payload.preview_asset_id,
                    preview_sha256=payload.preview_sha256,
                    review_policy_version=payload.review_policy_version, dimensions=payload.dimensions,
                    scoped_findings=payload.scoped_findings, concern_answers=payload.concern_answers,
                ))
            except (PlannedTalkingAdmissionError, ProductionRunNotReady, ValueError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        if (payload.preview_asset_id is not None or payload.preview_sha256 is not None
            or payload.review_policy_version != 1 or payload.dimensions is not None
            or payload.scoped_findings or payload.concern_answers):
            raise HTTPException(status_code=409, detail="legacy continuity review cannot claim a planned preview")

        existing = reviews.get_by_series_id(series.id)
        if existing is not None:
            if (
                existing.project_id != project_id
                or existing.approved != payload.approved
                or existing.evidence_reference != payload.evidence_reference
                or existing.findings != payload.findings
                or existing.child_job_ids != series.child_job_ids
            ):
                raise HTTPException(status_code=409, detail="Talking slice series continuity review is immutable")
            return response_for(existing)
        readiness = assess_talking_slice_series(
            series,
            [JobRepository(db).get(job_id) for job_id in series.child_job_ids],
            AssetRepository(db).list(),
        )
        if not readiness.ready_for_human_continuity_review:
            raise HTTPException(
                status_code=422,
                detail="Talking slice series is not ready for human continuity review",
            )
        review = TalkingSliceSeriesContinuityReview(
            project_id=project_id,
            series_id=series.id,
            approved=payload.approved,
            evidence_reference=payload.evidence_reference,
            findings=payload.findings,
            child_job_ids=series.child_job_ids,
            reviewed_at=datetime.now(timezone.utc),
        )
        with db.transaction(immediate=True):
            persisted = reviews.create(review)
            if persisted.id != review.id:
                if (
                    persisted.approved != payload.approved
                    or persisted.evidence_reference != payload.evidence_reference
                    or persisted.findings != payload.findings
                    or persisted.child_job_ids != series.child_job_ids
                ):
                    raise HTTPException(status_code=409, detail="Talking slice series continuity review is immutable")
        return response_for(persisted)

    @application.get(
        "/projects/{project_id}/talking-slice-series/{series_id}/continuity-review",
        response_model=TalkingSliceSeriesContinuityReviewResponse,
        tags=["voice"],
    )
    async def get_talking_slice_series_continuity_review(
        project_id: UUID, series_id: UUID, request: Request,
    ) -> dict[str, Any]:
        """Read the immutable human decision without re-evaluating it."""

        db: Database = request.app.state.database
        review = TalkingSliceSeriesContinuityReviewRepository(db).get_by_series_id(series_id)
        if review is None or review.project_id != project_id:
            raise HTTPException(status_code=404, detail="Talking slice series continuity review not found")
        return {
            "id": review.id,
            "series_id": review.series_id,
            "approved": review.approved,
            "evidence_reference": review.evidence_reference,
            "findings": list(review.findings),
            "child_job_ids": list(review.child_job_ids),
            "planned_origin_sha256": review.planned_origin_sha256,
            "preview_asset_id": review.preview_asset_id,
            "preview_sha256": review.preview_sha256,
            "preview_qa_sha256": review.preview_qa_sha256,
            "reviewed_at": review.reviewed_at,
            "review_policy_version": review.review_policy_version,
            "dimensions": review.dimensions,
            "scoped_findings": review.scoped_findings,
        }

    @application.post("/projects/{project_id}/talking-slice-series/{series_id}/review-concerns", tags=["voice"], status_code=201)
    async def register_talking_review_concern(project_id: UUID, series_id: UUID,
                                             payload: TalkingReviewConcernRequest, request: Request):
        db = request.app.state.database
        series = TalkingSliceSeriesRepository(db).get(series_id)
        if series is None or series.project_id != project_id:
            raise HTTPException(status_code=404, detail="Talking collection not found")
        try:
            return PlannedTalkingAdmissionService(db, request.app.state.data_root).register_concern(
                series, preview_asset_id=payload.preview_asset_id, preview_sha256=payload.preview_sha256,
                finding=payload.finding, evidence_reference=payload.evidence_reference,
                idempotency_key=payload.idempotency_key,
            )
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @application.post("/projects/{project_id}/talking-slice-series/{series_id}/review-concern-answers", tags=["voice"], response_model=TalkingSliceSeriesReviewResponse)
    async def answer_talking_review_concerns(project_id: UUID, series_id: UUID,
                                            payload: TalkingReviewConcernAnswersRequest, request: Request):
        db = request.app.state.database
        series = TalkingSliceSeriesRepository(db).get(series_id)
        if series is None or series.project_id != project_id:
            raise HTTPException(status_code=404, detail="Talking collection not found")
        try:
            return PlannedTalkingAdmissionService(db, request.app.state.data_root).answer_concerns(
                series, preview_asset_id=payload.preview_asset_id, preview_sha256=payload.preview_sha256,
                answers=payload.answers,
            )
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @application.post(
        "/projects/{project_id}/talking-slice-series/{series_id}/talking-run",
        response_model=TalkingRunResponse,
        status_code=201,
        tags=["voice"],
    )
    async def admit_talking_run(project_id: UUID, series_id: UUID, request: Request) -> TalkingRun:
        """Assemble a reviewed source-forward series into one normal product Asset/Clip."""

        db: Database = request.app.state.database
        series = TalkingSliceSeriesRepository(db).get(series_id)
        if series is None or series.project_id != project_id:
            raise HTTPException(status_code=404, detail="Talking slice series not found")
        if is_planned_talking_collection(db, series):
            if series.planned_origin is None:
                raise HTTPException(status_code=409, detail="planned Talking origin is missing")
            try:
                return PlannedTalkingAdmissionService(db, request.app.state.data_root).admit(series)
            except (PlannedTalkingAdmissionError, ProductionRunNotReady, ValueError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        existing = TalkingRunRepository(db).get_by_series_id(series.id)
        if existing is not None:
            return existing
        importer = MediaImporter(db, request.app.state.data_root, FFProbeAdapter(resolve_local_executable("ffprobe")))
        assembler = TalkingRunAssembler(
            assets=AssetRepository(db), audios=AudioAssetRepository(db), clips=ClipRepository(db),
            jobs=JobRepository(db), reviews=TalkingSliceSeriesContinuityReviewRepository(db),
            runs=TalkingRunRepository(db), importer=importer,
            output_root=request.app.state.data_root / "generated" / "talking-runs",
            ffmpeg_command=resolve_local_executable("ffmpeg"),
        )
        try:
            return assembler.assemble(series)
        except (TalkingRunAssemblyError, MediaImportError, FileNotFoundError, OSError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.get(
        "/projects/{project_id}/talking-runs",
        response_model=list[TalkingRunResponse],
        tags=["voice"],
    )
    async def list_talking_runs(project_id: UUID, request: Request) -> list[TalkingRun]:
        db: Database = request.app.state.database
        if ProjectRepository(db).get(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        return TalkingRunRepository(db).list_for_project(project_id)

    @application.post("/clips/search", response_model=list[ClipSearchHitResponse])
    async def search_clips(payload: ClipSearchRequest, request: Request) -> list[ClipSearchHitResponse]:
        db: Database = request.app.state.database
        retrieval_mode = _retrieval_mode(request.app.state.embedding_provider)

        def run_search(searcher: object) -> list[ClipSearchHitResponse]:
            hits = searcher.search(  # type: ignore[attr-defined]
                payload.query,
                top_k=payload.top_k,
                asset_id=payload.asset_id,
                orientation=payload.orientation,
                talking_candidate=payload.talking_candidate,
            )
            return [ClipSearchHitResponse(clip=hit.clip, score=hit.score, score_basis=hit.score_basis) for hit in hits]

        def decode_search_result(value: object) -> list[ClipSearchHitResponse]:
            if not isinstance(value, list):
                raise ValueError("saved search result must be a list")
            return [ClipSearchHitResponse.model_validate(item) for item in value]

        try:
            if retrieval_mode == "lexical":
                return run_search(ClipTextSearchService(db))
            elif retrieval_mode == "embedding":
                provider = request.app.state.embedding_provider or _embedding_provider_from_env()
                identity = runtime_provider_identity(provider)
                searcher = ClipEmbeddingIndexer(db, provider)
                if identity is None:
                    return run_search(searcher)
                if payload.project_id is None:
                    raise HTTPException(status_code=422, detail="project_id is required for runtime semantic search budget accounting")
                if ProjectRepository(db).get(payload.project_id) is None:
                    raise HTTPException(status_code=404, detail="project not found")
                return ProviderExecutionService(db, request.app.state.cost_estimator).execute(
                    project_id=payload.project_id,
                    idempotency_key=_runtime_idempotency_key(request, "embedding"),
                    operation="embedding",
                    provider=identity,
                    input_source=f"project:{payload.project_id}:clip-search",
                    category=CostCategory.LLM,
                    input_document=payload.model_dump(mode="json"),
                    action=lambda: run_search(searcher),
                    encode_result=lambda value: [item.model_dump(mode="json") for item in value],
                    decode_result=decode_search_result,
                    error_code=_embedding_error_code,
                )
            else:
                raise IndexError("CONTENT_OS_RETRIEVAL_MODE must be embedding or lexical")
        except BudgetLimitError as exc:
            raise HTTPException(status_code=409, detail=exc.code) from exc
        except (ProviderExecutionIdempotencyConflict, ProviderExecutionInProgress, ProviderExecutionRecordedFailure, ProviderExecutionReplayUnavailable) as exc:
            raise HTTPException(status_code=409, detail=exc.code) from exc
        except ProviderExecutionAccountingFailure as exc:
            raise HTTPException(status_code=500, detail=exc.code) from exc
        except (EmbeddingConfigurationError, EmbeddingAuthenticationError) as exc:
            raise HTTPException(status_code=503, detail="embedding provider is not configured") from exc
        except (EmbeddingRateLimitError, EmbeddingTimeout, EmbeddingConnectionError) as exc:
            raise HTTPException(status_code=503, detail="embedding provider is temporarily unavailable") from exc
        except EmbeddingHTTPError as exc:
            status = 503 if exc.status_code >= 500 or exc.status_code in {408, 409, 425, 429} else 502
            raise HTTPException(status_code=status, detail="embedding provider request failed") from exc
        except EmbeddingProviderResponseError as exc:
            raise HTTPException(status_code=502, detail="embedding provider returned an invalid response") from exc
        except (EmbeddingInputError, EmbeddingIndexError, IndexError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.post("/projects/{project_id}/scene-plan", response_model=ScenePlanResponse)
    async def create_scene_plan(project_id: UUID, payload: ScenePlanRequest, request: Request) -> ScenePlanResponse:
        db: Database = request.app.state.database
        project = ProjectRepository(db).get(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        try:
            return generate_scene_plan(db,project,payload,
                request.app.state.scene_planner or _scene_planner_from_env(),request.app.state.cost_estimator,
                _runtime_idempotency_key(request,"scene-planning"))
        except PlanningContextChanged as exc:
            raise HTTPException(status_code=409,detail=str(exc)) from exc
        except BudgetLimitError as exc:
            raise HTTPException(status_code=409, detail=exc.code) from exc
        except (ProviderExecutionIdempotencyConflict, ProviderExecutionInProgress, ProviderExecutionRecordedFailure, ProviderExecutionReplayUnavailable) as exc:
            raise HTTPException(status_code=409, detail=exc.code) from exc
        except ProviderExecutionAccountingFailure as exc:
            raise HTTPException(status_code=500, detail=exc.code) from exc
        except ScenePlannerAuthenticationError as exc:
            raise HTTPException(status_code=503, detail="scene planner authentication was rejected") from exc
        except ScenePlannerConfigurationError as exc:
            raise HTTPException(status_code=503, detail="scene planner is not configured") from exc
        except (ScenePlannerRateLimitError, ScenePlannerTimeout, ScenePlannerConnectionError) as exc:
            raise HTTPException(status_code=503, detail="scene planner is temporarily unavailable") from exc
        except ScenePlannerHTTPError as exc:
            status = 503 if exc.status_code >= 500 or exc.status_code in {408, 409, 425, 429} else 502
            raise HTTPException(status_code=status, detail="scene planner request failed") from exc
        except ScenePlannerProviderResponseError as exc:
            raise HTTPException(status_code=502, detail="scene planner returned an invalid response") from exc
        except ScenePlannerInputError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.post("/projects/{project_id}/asset-routes", response_model=list[AssetRouteResponse])
    async def route_scene_assets(project_id: UUID, payload: AssetRouteRequest, request: Request) -> list[AssetRouteResponse]:
        db: Database = request.app.state.database
        if ProjectRepository(db).get(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        if any(scene.project_id != project_id for scene in payload.scenes):
            raise HTTPException(status_code=422, detail="all scenes must belong to the requested project")
        if payload.master_narration_asset_id is not None:
            master_audio = AudioAssetRepository(db).get(payload.master_narration_asset_id)
            if master_audio is None:
                raise HTTPException(status_code=422, detail="master_narration_asset_not_found")
            if not master_audio.transcript_source or not master_audio.transcript_segments:
                raise HTTPException(status_code=422, detail="master_narration_timing_required")
        proposal_ids = [proposal.scene_plan_id for proposal in payload.crop_proposals]
        if None in proposal_ids or len(set(proposal_ids)) != len(proposal_ids) or not set(proposal_ids).issubset({scene.id for scene in payload.scenes}):
            raise HTTPException(status_code=422, detail="crop proposals require unique routed ScenePlan identities")

        def route_response(searcher: object) -> list[AssetRouteResponse]:
            router = AssetRouter(
                searcher,
                AssetRepository(db),
                images=ImageAssetRepository(db),
                max_candidates=payload.max_candidates,
                capture_gap_threshold=payload.capture_gap_threshold,
                crop_proposals={proposal.scene_plan_id: proposal for proposal in payload.crop_proposals if proposal.scene_plan_id is not None},
            )
            results = router.route_all(payload.scenes)
            return [
                AssetRouteResponse(
                    scene_plan_id=result.scene_plan_id,
                    candidates=result.candidates,
                    shoot_list=_shoot_list_for_scene(
                        next(scene for scene in payload.scenes if scene.id == result.scene_plan_id),
                        result.candidates,
                    ),
                )
                for result in results
            ]

        def decode_route_result(value: object) -> list[AssetRouteResponse]:
            if not isinstance(value, list):
                raise ValueError("saved route result must be a list")
            return [AssetRouteResponse.model_validate(item) for item in value]

        try:
            retrieval_mode = _retrieval_mode(request.app.state.embedding_provider)
            if retrieval_mode == "lexical":
                return route_response(ClipTextSearchService(db))
            elif retrieval_mode == "embedding":
                provider = request.app.state.embedding_provider or _embedding_provider_from_env()
                searcher = ClipEmbeddingIndexer(db, provider)
                identity = runtime_provider_identity(provider)
                if identity is None:
                    return route_response(searcher)
                return ProviderExecutionService(db, request.app.state.cost_estimator).execute(
                    project_id=project_id,
                    idempotency_key=_runtime_idempotency_key(request, "asset-routes-embedding"),
                    operation="embedding",
                    provider=identity,
                    input_source=f"project:{project_id}:asset-routes",
                    category=CostCategory.LLM,
                    input_document={"request": payload.model_dump(mode="json"), "retrieval_mode": retrieval_mode},
                    action=lambda: route_response(searcher),
                    encode_result=lambda value: [item.model_dump(mode="json") for item in value],
                    decode_result=decode_route_result,
                    error_code=_embedding_error_code,
                )
            else:
                raise IndexError("CONTENT_OS_RETRIEVAL_MODE must be embedding or lexical")
        except BudgetLimitError as exc:
            raise HTTPException(status_code=409, detail=exc.code) from exc
        except (ProviderExecutionIdempotencyConflict, ProviderExecutionInProgress, ProviderExecutionRecordedFailure, ProviderExecutionReplayUnavailable) as exc:
            raise HTTPException(status_code=409, detail=exc.code) from exc
        except ProviderExecutionAccountingFailure as exc:
            raise HTTPException(status_code=500, detail=exc.code) from exc
        except (EmbeddingConfigurationError, EmbeddingAuthenticationError) as exc:
            raise HTTPException(status_code=503, detail="embedding provider is not configured") from exc
        except (EmbeddingRateLimitError, EmbeddingTimeout, EmbeddingConnectionError) as exc:
            raise HTTPException(status_code=503, detail="embedding provider is temporarily unavailable") from exc
        except EmbeddingHTTPError as exc:
            status = 503 if exc.status_code >= 500 or exc.status_code in {408, 409, 425, 429} else 502
            raise HTTPException(status_code=status, detail="embedding provider request failed") from exc
        except EmbeddingProviderResponseError as exc:
            raise HTTPException(status_code=502, detail="embedding provider returned an invalid response") from exc
        except (EmbeddingInputError, EmbeddingIndexError, IndexError, RoutingConfigurationError, RoutingInputError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.post("/projects/{project_id}/video-spec", response_model=VideoSpec)
    async def assemble_video_spec(project_id: UUID, payload: VideoSpecAssemblyRequest, request: Request) -> VideoSpec:
        db: Database = request.app.state.database
        project = ProjectRepository(db).get(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        if any(scene.project_id != project_id for scene in payload.scenes):
            raise HTTPException(status_code=422, detail="all scenes must belong to the requested project")
        selected = {candidate.scene_plan_id: candidate for candidate in payload.selections}
        if len(selected) != len(payload.selections):
            raise HTTPException(status_code=422, detail="selections must contain one candidate per scene")
        try:
            return VideoSpecAssembler(AssetRepository(db), ClipRepository(db), ImageAssetRepository(db), AudioAssetRepository(db)).assemble(
                project,
                payload.scenes,
                selected,
                explicit_scene_ids=payload.explicit_scene_ids,
                narration_asset_ids=payload.narration_asset_ids,
                master_narration_asset_id=payload.master_narration_asset_id,
                narration_required=payload.narration_required,
                edit_plan=payload.edit_plan,
            )
        except VideoSpecAssemblyError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.post("/projects/{project_id}/render", response_model=RenderResponse, tags=["render"])
    async def render_project(project_id: UUID, payload: RenderRequest, request: Request) -> RenderResponse:
        """Render authorized local continuous Clips to an application-owned MP4."""
        db: Database = request.app.state.database
        project = ProjectRepository(db).get(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        spec = _resolve_render_spec(db, project, project_id, payload)

        # Preserve the synchronous response while sharing the durable render
        # lane, source reservations, failure records and usage accounting.
        from datetime import timedelta
        from app.jobs import JobRunner, JobStore, RenderVideoJobHandler
        queued = await enqueue_render_project(project_id, RenderJobEnqueueRequest(
            video_spec=spec, idempotency_key=f"sync-render:{uuid4()}"), request)
        renderer = request.app.state.renderer_factory(db) if request.app.state.renderer_factory else _local_renderer(db)
        result = JobRunner(JobStore(db), {JobType.RENDER:RenderVideoJobHandler(
            ProjectRepository(db),renderer,request.app.state.render_output_root)},
            worker_id=f"sync-render:{queued['id']}", lease_duration=timedelta(hours=2),
            max_attempts=1).run_once(job_id=UUID(str(queued["id"])))
        if result is None or result.status is not JobStatus.COMPLETED:
            code = "render_not_claimed" if result is None else result.error_code or "render_failed"
            status = 504 if code == "render_timeout" else 502 if code == "render_processing_failed" else 422
            raise HTTPException(status_code=status,detail=code)
        render_id = result.payload.render_id
        return RenderResponse(
            project_id=project_id,
            render_id=render_id,
            video_spec=spec,
            download_url=f"/projects/{project_id}/renders/{render_id}",
            preview_url=f"/projects/{project_id}/renders/{render_id}",
        )

    def rendered_media_path(project_id, render_id, request):
        db = request.app.state.database
        if ProjectRepository(db).get(project_id) is None:
            raise HTTPException(status_code=404,detail="project not found")
        output = render_output_path(request.app.state.render_output_root,project_id,render_id)
        if not output.is_file():
            raise HTTPException(status_code=404,detail="rendered video not found")
        _ordinary_media(output,"render",render_id)
        return output

    @application.get("/projects/{project_id}/renders/{render_id}", tags=["render"])
    async def get_rendered_video(project_id: UUID, render_id: UUID, request: Request) -> FileResponse:
        output = rendered_media_path(project_id, render_id, request)
        return FileResponse(output,media_type="video/mp4",filename=output.name,headers={"Cache-Control":"no-store"})

    @application.head("/projects/{project_id}/renders/{render_id}", include_in_schema=False)
    async def head_rendered_video(project_id: UUID, render_id: UUID, request: Request) -> Response:
        output = rendered_media_path(project_id, render_id, request)
        return Response(
            status_code=200,
            headers={
                "content-type": "video/mp4",
                "content-length": str(output.stat().st_size),
                "accept-ranges": "bytes",
                "cache-control": "no-store",
            },
        )

    @application.get("/projects/{project_id}/renders/{render_id}/credits", tags=["render"])
    async def rendered_credits(project_id: UUID, render_id: UUID, request: Request) -> FileResponse:
        output = rendered_media_path(project_id, render_id, request)
        credits = output.with_suffix(".credits.txt")
        if not credits.is_file():
            raise HTTPException(404, "此历史成片没有署名文件")
        return FileResponse(credits, media_type="text/plain; charset=utf-8", filename=credits.name)

    @application.get("/clips/{clip_id}", response_model=Clip, tags=["assets"])
    async def get_clip(clip_id: UUID) -> Clip:
        clip = ClipRepository(application.state.database).get(clip_id)
        if clip is None:
            raise HTTPException(status_code=404, detail="clip not found")
        return clip

    @application.get("/clips/{clip_id}/media", tags=["assets"])
    async def clip_media(clip_id: UUID) -> FileResponse:
        db: Database = application.state.database
        clip = ClipRepository(db).get(clip_id)
        if clip is None:
            raise HTTPException(status_code=404, detail="clip not found")
        asset = AssetRepository(db).get(clip.asset_id)
        if asset is None:
            raise HTTPException(status_code=404, detail="asset not found")
        try:
            source = existing_local_media_file(asset.source_file, application.state.data_root)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="asset source file not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="asset source path is not a supported local path") from exc
        _ordinary_media(source, "asset", asset.id, asset.content_hash)
        return FileResponse(source)

    return application


def _voice_audio_belongs_to_project(
    db: Database,
    audio: AudioAsset,
    project_id: UUID,
    *,
    _seen_audio_ids: frozenset[UUID] = frozenset(),
) -> bool:
    """Resolve generated-Voice provenance without trusting an arbitrary audio ID.

    Ordinary takes bind through their generation job.  Composed masters keep a
    project snapshot and, for older candidates, can still derive membership
    from every persisted source take.  Imported narration deliberately has no
    route through this U-Voice review endpoint.
    """

    if audio.id in _seen_audio_ids:
        return False
    seen = _seen_audio_ids | {audio.id}
    generation = audio.metadata.get("voice_generation")
    if not isinstance(generation, dict):
        return False
    job_id = generation.get("job_id")
    if isinstance(job_id, str):
        try:
            job = JobRepository(db).get(UUID(job_id))
        except ValueError:
            job = None
        return job is not None and job.type is JobType.GENERATE_VOICE and job.project_id == project_id
    declared_project_id = generation.get("project_id")
    if declared_project_id == str(project_id) and generation.get("provider") == "composed_voice_takes":
        return True
    composition = generation.get("composition")
    source_ids = composition.get("source_take_audio_ids") if isinstance(composition, dict) else None
    if not isinstance(source_ids, list) or not source_ids:
        return False
    audios = AudioAssetRepository(db)
    for raw_id in source_ids:
        if not isinstance(raw_id, str):
            return False
        try:
            source = audios.get(UUID(raw_id))
        except ValueError:
            return False
        if source is None or not _voice_audio_belongs_to_project(db, source, project_id, _seen_audio_ids=seen):
            return False
    return True


def _job_response(db: Database, job: Job) -> dict[str, Any]:
    target = AssetJobTargetStore(db).target_for(job.id)
    values = job.model_dump(mode="json")
    values.pop("schema_version", None)
    values.pop("payload", None)
    render = job.payload
    if isinstance(render, RenderVideoJobPayload):
        values.update({
            "render_id": render.render_id,
            "download_url": f"/projects/{render.project_id}/renders/{render.render_id}",
            "preview_url": f"/projects/{render.project_id}/renders/{render.render_id}",
        })
    return {**values, "target_asset_id": None if target is None else target.asset_id}


def _local_renderer(db: Database) -> RemotionRenderer:
    renderer_dir = Path(__file__).resolve().parents[3] / "apps" / "renderer"
    return RemotionRenderer(AssetRepository(db), ClipRepository(db), images=ImageAssetRepository(db), audios=AudioAssetRepository(db), renderer_dir=renderer_dir)


def _resolve_render_spec(db: Database, project: Project, project_id: UUID, payload: RenderRequest) -> VideoSpec:
    if payload.video_spec is not None:
        if payload.video_spec.project_id != project_id:
            raise HTTPException(status_code=422, detail="VideoSpec must belong to the requested project")
        return payload.video_spec
    assert payload.scenes is not None and payload.selections is not None
    if any(scene.project_id != project_id for scene in payload.scenes):
        raise HTTPException(status_code=422, detail="all scenes must belong to the requested project")
    selected = {candidate.scene_plan_id: candidate for candidate in payload.selections}
    if len(selected) != len(payload.selections):
        raise HTTPException(status_code=422, detail="selections must contain one candidate per scene")
    try:
        return VideoSpecAssembler(AssetRepository(db), ClipRepository(db), ImageAssetRepository(db), AudioAssetRepository(db)).assemble(
            project, payload.scenes, selected, explicit_scene_ids=payload.explicit_scene_ids,
            narration_asset_ids=payload.narration_asset_ids,
            master_narration_asset_id=payload.master_narration_asset_id,
            edit_plan=payload.edit_plan,
        )
    except VideoSpecAssemblyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _retrieval_mode(embedding_provider: EmbeddingProvider | None) -> str:
    configured = os.environ.get("CONTENT_OS_RETRIEVAL_MODE")
    if configured is not None:
        return configured.strip().lower()
    return "embedding" if embedding_provider is not None or os.environ.get("CONTENT_OS_EMBEDDING_API_KEY") or os.environ.get("OPENAI_API_KEY") else "lexical"


def _embedding_provider_from_env() -> OpenAICompatibleEmbeddingProvider:
    api_key = os.environ.get("CONTENT_OS_EMBEDDING_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise EmbeddingConfigurationError("embedding API key must be supplied at runtime")
    raw_dimensions = os.environ.get("CONTENT_OS_EMBEDDING_DIMENSIONS")
    try:
        dimensions = None if raw_dimensions is None else int(raw_dimensions)
    except ValueError as exc:
        raise EmbeddingConfigurationError("embedding dimensions must be an integer") from exc
    return OpenAICompatibleEmbeddingProvider(
        api_key,
        base_url=os.environ.get("CONTENT_OS_EMBEDDING_BASE_URL") or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        model=os.environ.get("CONTENT_OS_EMBEDDING_MODEL", "text-embedding-3-small"),
        dimensions=dimensions,
    )






app = create_app()
