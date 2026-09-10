"""Minimal local FastAPI entrypoint for the Content OS scaffold."""
from __future__ import annotations

import os
import secrets
import sqlite3
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

from app.budget import BudgetLimitError, ProviderCallError, ProviderCallLedger, is_over_budget, snapshot
from app.costs import ProviderCostEstimator, UnknownProviderCostEstimator, estimate_selected_candidates, reduce_candidate_cost
from app.db import AccountConnectionRepository, AnalysisResultRepository, AssetRepository, AssetUsageRepository, AudioAssetRepository, BudgetPolicyRepository, ClipRepository, ContentOpportunityRepository, Database, FeedbackRepository, HistoricalContentRepository, ImageAssetRepository, IPProfileRepository, JobRepository, ProjectDraftRepository, ProjectRepository, ProviderCallRepository, PublicationRepository, ShootTaskRepository, TalkingProfileRepository, VoiceProfileRepository
from app.domain.models import AccountConnection, AnalysisResultBundle, Asset, AssetUsageEvent, AudioAsset, BudgetPolicy, CandidateAsset, Clip, ConsentRecord, ContentFeedback, ContentOpportunity, CostCategory, CostEstimate, CostReductionSuggestion, DraftRoute, HistoricalContent, ImageAsset, IPProfile, Job, JobStatus, JobType, Project, ProjectDraft, ProjectFormat, ProviderCallRecord, PublicationRecord, RationalFps, RenderVideoJobPayload, ScenePlan, ShootTask, SourceKind, TalkingProfile, UsageCost, VideoSpec, VoiceProfile
from app.assembly import NarrationRequiredForNewScript, VideoSpecAssembler, VideoSpecAssemblyError
from app.asset_library import asset_library_page
from app.m1_gate import m1_gate_page
from app.media import AudioAssetNotFound, AudioImportError, AudioImporter, AudioTranscriptPersistence, ClipTranscriptPersistence, FFProbeAdapter, ImageImportError, ImageImporter, MediaImportError, MediaImporter, NoClipsForAsset, ProbeError, SubtitleParseError, parse_subtitle_file
from app.workspace import workspace_page
from app.jobs.targets import AssetJobIdempotencyConflict, AssetJobTargetStore, UnsupportedAssetJobType
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
from app.search import ClipEmbeddingIndexer, ClipTextSearchService, EmbeddingIndexError, IndexError
from app.routing import AssetRouter, RoutingConfigurationError, RoutingInputError
from app.renderer import LocalResourceError, RemotionRenderer, RenderInputError, RenderProcessError, RenderTimeout, UnauthorizedVisualError, render_output_path
from app.runtime import RuntimeCapability, inspect_runtime_capabilities, resolve_local_executable


class JobEnqueueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=1, max_length=500)
    project_id: UUID | None = None


class JobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    type: JobType
    status: JobStatus
    attempt: int
    idempotency_key: str
    project_id: UUID | None
    target_asset_id: UUID | None
    created_at: datetime
    updated_at: datetime
    error_code: str | None
    error_message: str | None
    render_id: UUID | None = None
    download_url: str | None = None
    preview_url: str | None = None


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


class ScenePlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    script: str | None = Field(default=None, min_length=1, max_length=100_000)
    topic: str | None = Field(default=None, min_length=1, max_length=5_000)


class ScenePlanResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: UUID
    scenes: tuple[ScenePlan, ...]


class AssetRouteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenes: list[ScenePlan] = Field(min_length=1, max_length=100)
    max_candidates: int = Field(default=3, ge=1, le=20)
    capture_gap_threshold: float = Field(default=0.45, ge=0, le=1)


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
    narration_required: bool = False


class RenderRequest(BaseModel):
    """One already-assembled spec, or the inputs needed to assemble one locally."""

    model_config = ConfigDict(extra="forbid")
    video_spec: VideoSpec | None = None
    scenes: list[ScenePlan] | None = Field(default=None, min_length=1, max_length=1_000)
    selections: list[CandidateAsset] | None = Field(default=None, min_length=1, max_length=1_000)
    explicit_scene_ids: list[UUID] = Field(default_factory=list, max_length=1_000)
    narration_asset_ids: dict[UUID, UUID] = Field(default_factory=dict, max_length=1_000)

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


class IPProfileUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    creator_name: str = Field(min_length=1, max_length=200)
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


class AssetUsageUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    usage: Literal["unknown", "reference", "production"]
    identity: Literal["creator", "other", "none", "unknown"]


class AssetStageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["not_started", "pending", "running", "completed", "failed", "cancelled"]
    job_id: UUID | None = None
    error_code: str | None = None


class AssetReadinessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_id: UUID
    stages: dict[str, AssetStageResponse]


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
    name: str = Field(min_length=1, max_length=200)
    provider: str = Field(min_length=1, max_length=100)
    provider_profile_id: str = Field(min_length=1, max_length=500)
    reference_clip_ids: list[UUID] = Field(min_length=1, max_length=100)
    consent: ConsentRecord
    language: str | None = Field(default=None, pattern=r"^[a-z]{2,3}(-[A-Z]{2})?$")
    created_at: AwareDatetime


class TalkingProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID = Field(default_factory=uuid4)
    name: str = Field(min_length=1, max_length=200)
    provider: str = Field(min_length=1, max_length=100)
    provider_profile_id: str | None = Field(default=None, max_length=500)
    reference_clip_ids: list[UUID] = Field(min_length=1, max_length=100)
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
    operation: Literal["scene_planning", "asr", "vision", "embedding", "tts", "talking", "render"]
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


def _scene_planner_error_code(error: Exception) -> str:
    if isinstance(error, (ScenePlannerConfigurationError, ScenePlannerAuthenticationError)):
        return "scene_planner_not_configured"
    if isinstance(error, (ScenePlannerRateLimitError, ScenePlannerTimeout, ScenePlannerConnectionError)):
        return "scene_planner_temporarily_unavailable"
    if isinstance(error, ScenePlannerHTTPError):
        return "scene_planner_request_failed"
    if isinstance(error, ScenePlannerProviderResponseError):
        return "scene_planner_response_invalid"
    if isinstance(error, ScenePlannerInputError):
        return "scene_planner_input_invalid"
    return "scene_planner_failed"


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
    image_importer_factory: Callable[[Database, Path], ImageImporter] | None = None,
    audio_importer_factory: Callable[[Database, Path], AudioImporter] | None = None,
    render_output_root: str | Path | None = None,
    cost_estimator: ProviderCostEstimator | None = None,
) -> FastAPI:
    """Create an app whose SQLite connection belongs to its lifespan thread."""
    selected_path = data_path or os.environ.get("CONTENT_OS_DB_PATH") or Path("content-os-data") / "content-os.sqlite3"

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        selected = Database(selected_path)
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

    application.state.embedding_provider = embedding_provider
    application.state.scene_planner = scene_planner
    application.state.renderer_factory = renderer_factory
    application.state.media_importer_factory = media_importer_factory
    application.state.image_importer_factory = image_importer_factory
    application.state.audio_importer_factory = audio_importer_factory
    application.state.cost_estimator = cost_estimator or UnknownProviderCostEstimator()
    application.state.data_root = Path(os.environ.get("CONTENT_OS_DATA_ROOT") or Path(selected_path).parent).resolve()
    # This is intentionally an application-owned directory, never a request
    # field. Both API and worker derive the same default beneath the local
    # data root, so a completed asynchronous job can be downloaded here.
    application.state.render_output_root = Path(render_output_root or Path(selected_path).parent / "renders").resolve()

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
        # R1 is single-user/single-active-IP: projects share the first local
        # profile instead of creating a disconnected empty profile each time.
        profiles = IPProfileRepository(db).list()
        profile = profiles[0] if profiles else IPProfile(creator_name=payload.creator_name)
        project = Project(
            ip_profile_id=profile.id, title=payload.title, topic=payload.topic,
            fps=RationalFps(numerator=30, denominator=1), created_at=datetime.now(timezone.utc),
        )
        with db.transaction():
            if not profiles:
                IPProfileRepository(db).create(profile)
            ProjectRepository(db).create(project)
        return project

    @application.get("/ip-profile", response_model=IPProfile, tags=["profile"])
    async def get_default_ip_profile(request: Request) -> IPProfile:
        profile = next(iter(IPProfileRepository(request.app.state.database).list()), None)
        if profile is None:
            raise HTTPException(status_code=404, detail="default IP profile not initialized")
        return profile

    @application.put("/ip-profile", response_model=IPProfile, tags=["profile"])
    async def update_default_ip_profile(payload: IPProfileUpdateRequest, request: Request) -> IPProfile:
        db: Database = request.app.state.database
        profile = next(iter(IPProfileRepository(db).list()), None)
        if profile is None:
            profile = IPProfile(id=uuid4(), **payload.model_dump())
            with db.transaction():
                IPProfileRepository(db).create(profile)
            return profile
        updated = IPProfile(id=profile.id, **payload.model_dump())
        with db.transaction():
            IPProfileRepository(db).update(updated)
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
        previous = ProjectDraftRepository(db).get(project_id)
        draft = ProjectDraft(
            project_id=project_id,
            version=1 if previous is None else previous.version + 1,
            script=payload.script,
            topic=payload.topic,
            scenes=payload.scenes,
            routes=payload.routes,
            confirmed=payload.confirmed,
            video_spec=payload.video_spec,
            updated_at=datetime.now(timezone.utc),
        )
        with db.transaction():
            ProjectDraftRepository(db).save(draft)
        return draft

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
    async def list_voice_profiles(request: Request) -> list[VoiceProfile]:
        return VoiceProfileRepository(request.app.state.database).list()

    @application.post("/voice-profiles", response_model=VoiceProfile, status_code=201, tags=["voice"])
    async def create_voice_profile(payload: VoiceProfileRequest, request: Request) -> VoiceProfile:
        db: Database = request.app.state.database
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

    @application.get("/talking-profiles", response_model=list[TalkingProfile], tags=["voice"])
    async def list_talking_profiles(request: Request) -> list[TalkingProfile]:
        return TalkingProfileRepository(request.app.state.database).list()

    @application.post("/talking-profiles", response_model=TalkingProfile, status_code=201, tags=["voice"])
    async def create_talking_profile(payload: TalkingProfileRequest, request: Request) -> TalkingProfile:
        db: Database = request.app.state.database
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

    @application.get("/assets/{asset_id}/readiness", response_model=AssetReadinessResponse, tags=["assets"])
    async def get_asset_readiness(asset_id: UUID, request: Request) -> AssetReadinessResponse:
        db: Database = request.app.state.database
        if AssetRepository(db).get(asset_id) is None:
            raise HTTPException(status_code=404, detail="asset not found")
        jobs = JobRepository(db).list_for_asset(asset_id)

        def stage(job_type: JobType) -> AssetStageResponse:
            matching = [job for job in jobs if job.type is job_type]
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
                "index": indexed,
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
        source = Path(asset.source_file).expanduser().resolve()
        if not source.is_file():
            raise HTTPException(status_code=404, detail="asset source file not found")
        return FileResponse(source)

    @application.patch("/assets/{asset_id}/usage", response_model=Asset, tags=["assets"])
    async def update_asset_usage(asset_id: UUID, payload: AssetUsageUpdateRequest, request: Request) -> Asset:
        db: Database = request.app.state.database
        asset = AssetRepository(db).get(asset_id)
        if asset is None:
            raise HTTPException(status_code=404, detail="asset not found")
        metadata = dict(asset.metadata)
        metadata["r1_usage"] = payload.usage
        metadata["r1_identity"] = payload.identity
        updated = asset.model_copy(update={"metadata": metadata})
        with db.transaction():
            AssetRepository(db).update(updated)
        return updated

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
        source = Path(image.source_file).expanduser().resolve()
        if not source.is_file():
            raise HTTPException(status_code=404, detail="image source file not found")
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

    @application.get("/audio-assets/{audio_id}/media", tags=["audio"])
    async def audio_asset_media(audio_id: UUID, request: Request) -> FileResponse:
        audio = AudioAssetRepository(request.app.state.database).get(audio_id)
        if audio is None:
            raise HTTPException(status_code=404, detail="audio asset not found")
        source = Path(audio.source_file).expanduser().resolve()
        if not source.is_file():
            raise HTTPException(status_code=404, detail="audio source file not found")
        return FileResponse(source)

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
        now = datetime.now(timezone.utc)
        job = Job(id=uuid4(), project_id=payload.project_id, type=job_type, idempotency_key=payload.idempotency_key, created_at=now, updated_at=now)
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
        persisted = JobRepository(db).create(job)
        if (
            persisted.type is not JobType.RENDER
            or persisted.project_id != project_id
            or persisted.payload is None
            or persisted.payload.video_spec != spec
        ):
            raise HTTPException(status_code=409, detail="idempotency key is already used for a different job")
        return _job_response(db, persisted)

    @application.post("/clips/search", response_model=list[ClipSearchHitResponse])
    async def search_clips(payload: ClipSearchRequest, request: Request) -> list[ClipSearchHitResponse]:
        db: Database = request.app.state.database
        retrieval_mode = os.environ.get("CONTENT_OS_RETRIEVAL_MODE", "embedding").strip().lower()

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
        planning_context, evidence_refs = _scene_planning_context(db, project)

        def plan_response(planner: ScenePlanner, *, include_context: bool) -> ScenePlanResponse:
            result = planner.plan(
                project,
                script=payload.script,
                topic=payload.topic,
                **({"context": planning_context} if include_context else {}),
            )
            scenes = tuple(scene.model_copy(update={"evidence_refs": list(evidence_refs)}) for scene in result.scenes)
            return ScenePlanResponse(project_id=result.project_id, scenes=scenes)

        try:
            planner = request.app.state.scene_planner or _scene_planner_from_env()
            identity = runtime_provider_identity(planner)
            if identity is not None:
                return ProviderExecutionService(db, request.app.state.cost_estimator).execute(
                    project_id=project_id,
                    idempotency_key=_runtime_idempotency_key(request, "scene-planning"),
                    operation="scene_planning",
                    input_source=f"project:{project_id}:scene-plan",
                    provider=identity,
                    category=CostCategory.LLM,
                    input_document={
                        "request": payload.model_dump(mode="json"),
                        "planning_context": planning_context,
                        "evidence_refs": list(evidence_refs),
                    },
                    action=lambda: plan_response(planner, include_context=True),
                    encode_result=lambda value: value.model_dump(mode="json"),
                    decode_result=ScenePlanResponse.model_validate,
                    error_code=_scene_planner_error_code,
                )
            # Deterministic/local test adapters retain the narrow original
            # protocol and do not claim an externally metered identity.
            return plan_response(planner, include_context=False)
        except BudgetLimitError as exc:
            raise HTTPException(status_code=409, detail=exc.code) from exc
        except (ProviderExecutionIdempotencyConflict, ProviderExecutionInProgress, ProviderExecutionRecordedFailure, ProviderExecutionReplayUnavailable) as exc:
            raise HTTPException(status_code=409, detail=exc.code) from exc
        except ProviderExecutionAccountingFailure as exc:
            raise HTTPException(status_code=500, detail=exc.code) from exc
        except (ScenePlannerConfigurationError, ScenePlannerAuthenticationError) as exc:
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

        def route_response(searcher: object) -> list[AssetRouteResponse]:
            router = AssetRouter(
                searcher,
                AssetRepository(db),
                images=ImageAssetRepository(db),
                max_candidates=payload.max_candidates,
                capture_gap_threshold=payload.capture_gap_threshold,
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
            retrieval_mode = os.environ.get("CONTENT_OS_RETRIEVAL_MODE", "embedding").strip().lower()
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
                narration_required=payload.narration_required,
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

        render_id = uuid4()
        output = render_output_path(request.app.state.render_output_root, project_id, render_id)
        try:
            renderer = request.app.state.renderer_factory(db) if request.app.state.renderer_factory else _local_renderer(db)
            renderer.render(spec, output)
        except (RenderInputError, LocalResourceError, UnauthorizedVisualError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RenderTimeout as exc:
            raise HTTPException(status_code=504, detail="local renderer timed out") from exc
        except RenderProcessError as exc:
            raise HTTPException(status_code=502, detail="local renderer failed") from exc
        return RenderResponse(
            project_id=project_id,
            render_id=render_id,
            video_spec=spec,
            download_url=f"/projects/{project_id}/renders/{render_id}",
            preview_url=f"/projects/{project_id}/renders/{render_id}",
        )

    @application.get("/projects/{project_id}/renders/{render_id}", tags=["render"])
    async def get_rendered_video(project_id: UUID, render_id: UUID, request: Request) -> FileResponse:
        if ProjectRepository(request.app.state.database).get(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        output = render_output_path(request.app.state.render_output_root, project_id, render_id)
        if not output.is_file():
            raise HTTPException(status_code=404, detail="rendered video not found")
        return FileResponse(output, media_type="video/mp4", filename=output.name)

    @application.head("/projects/{project_id}/renders/{render_id}", include_in_schema=False)
    async def head_rendered_video(project_id: UUID, render_id: UUID, request: Request) -> Response:
        """Allow the Web workspace to validate a persisted local preview cheaply."""
        if ProjectRepository(request.app.state.database).get(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        output = render_output_path(request.app.state.render_output_root, project_id, render_id)
        if not output.is_file():
            raise HTTPException(status_code=404, detail="rendered video not found")
        return Response(
            status_code=200,
            headers={
                "content-type": "video/mp4",
                "content-length": str(output.stat().st_size),
                "accept-ranges": "bytes",
            },
        )

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
        source = Path(asset.source_file).expanduser().resolve()
        if not source.is_file():
            raise HTTPException(status_code=404, detail="asset source file not found")
        return FileResponse(source)

    return application


def _job_response(db: Database, job: Job) -> dict[str, Any]:
    target = AssetJobTargetStore(db).target_for(job.id)
    values = job.model_dump(mode="json")
    values.pop("schema_version", None)
    values.pop("payload", None)
    render = job.payload
    if render is not None:
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
        )
    except VideoSpecAssemblyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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


def _scene_planning_context(db: Database, project: Project) -> tuple[dict[str, object], tuple[str, ...]]:
    """Build bounded IP/material/opportunity context and traceable refs."""
    profile = IPProfileRepository(db).get(project.ip_profile_id)
    refs: list[str] = []
    context: dict[str, object] = {}
    if profile is not None:
        revisions = IPProfileRepository(db).revisions(profile.id)
        profile_version = revisions[-1][0] if revisions else 1
        refs.append(f"ip_profile:{profile.id}:v{profile_version}")
        context.update({
            "ip_profile_version": profile_version,
            "ip_profile": profile.model_dump(mode="json"),
        })
    materials: list[dict[str, object]] = []
    for asset in AssetRepository(db).list():
        metadata = asset.metadata
        analysis = metadata.get("r1_analysis")
        if isinstance(analysis, dict) and isinstance(analysis.get("bundle_id"), str):
            refs.append(f"analysis_bundle:{analysis['bundle_id']}")
        clips = []
        for clip in ClipRepository(db).list_by_asset(asset.id)[:20]:
            clips.append({
                "clip_id": str(clip.id),
                "start_ms": clip.start_ms,
                "end_ms": clip.end_ms,
                "transcript": clip.transcript,
                "visual_description": clip.visual_description,
                "action": clip.action,
            })
        materials.append({
            "asset_id": str(asset.id),
            "source_kind": asset.source_kind.value,
            "usage": metadata.get("r1_usage", "unknown"),
            "identity": metadata.get("r1_identity", "unknown"),
            "clips": clips,
        })
    opportunities = ContentOpportunityRepository(db).list_for_planning()
    opportunity_context: list[dict[str, object]] = []
    for opportunity in opportunities:
        refs.append(f"opportunity:{opportunity.id}")
        refs.extend(opportunity.evidence_refs)
        opportunity_context.append(opportunity.model_dump(mode="json"))
    unique_refs = tuple(dict.fromkeys(refs))
    context.update({
        "evidence_refs": list(unique_refs),
        "materials": materials,
        "opportunities": opportunity_context,
    })
    return context, unique_refs


def _scene_planner_from_env() -> OpenAICompatibleScenePlanner:
    api_key = os.environ.get("CONTENT_OS_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise ScenePlannerConfigurationError("scene planner API key must be supplied at runtime")
    return OpenAICompatibleScenePlanner(
        api_key,
        base_url=os.environ.get("CONTENT_OS_LLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        model=os.environ.get("CONTENT_OS_LLM_MODEL", "gpt-4.1-mini"),
        protocol=os.environ.get("CONTENT_OS_LLM_PROTOCOL", "responses"),
    )


app = create_app()
