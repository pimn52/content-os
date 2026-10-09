"""Provider-agnostic, versioned domain contracts for Content OS."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
import hashlib
import json
from math import isfinite
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Discriminator, Field, JsonValue, Tag, field_validator, model_validator
from pydantic_core import PydanticCustomError
from .execution_runtime import HostRuntimeObservation, HostRuntimeObservationV3Policy2, RuntimeInventoryDescriptor

SCHEMA_VERSION = "0.1"

NonNegativeMs = Annotated[int, Field(ge=0, strict=True, description="Milliseconds from the source start.")]
PositiveFrames = Annotated[int, Field(gt=0, strict=True)]
Score = Annotated[float, Field(ge=0, le=1)]


class ContractModel(BaseModel):
    """Shared JSON-safe contract behavior and an explicit wire version."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION

    @field_validator("metadata", check_fields=False)
    @classmethod
    def metadata_is_finite_json(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        def check(item: JsonValue) -> None:
            if isinstance(item, float) and item != item or isinstance(item, float) and item in (float("inf"), float("-inf")):
                raise ValueError("metadata must not contain non-finite numbers")
            if isinstance(item, dict):
                for nested in item.values():
                    check(nested)
            elif isinstance(item, list):
                for nested in item:
                    check(nested)
        check(value)
        return value


class SourceKind(StrEnum):
    USER_ASSET = "user_asset"
    HISTORICAL_ASSET = "historical_asset"
    CAPTURE = "capture"
    STOCK = "stock"
    AI_IMAGE = "ai_image"
    AI_VIDEO = "ai_video"
    TYPOGRAPHY = "typography"
    SCREENSHOT = "screenshot"
    CHART = "chart"
    TALKING_PROFILE = "talking_profile"


class ProjectFormat(StrEnum):
    VERTICAL = "vertical"
    HORIZONTAL = "horizontal"
    SQUARE = "square"


class CostCategory(StrEnum):
    USER_ASSET = "user_asset"
    HISTORICAL_ASSET = "historical_asset"
    CAPTURE = "capture"
    STOCK = "stock"
    AI_IMAGE = "ai_image"
    AI_VIDEO = "ai_video"
    TYPOGRAPHY = "typography"
    SCREENSHOT = "screenshot"
    CHART = "chart"
    TALKING = "talking"
    VOICE = "voice"
    ASR = "asr"
    LLM = "llm"
    RENDER = "render"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobType(StrEnum):
    IMPORT_ASSET = "import_asset"
    ANALYZE_ASSET = "analyze_asset"
    TRANSCRIBE_AUDIO = "transcribe_audio"
    INDEX_CLIPS = "index_clips"
    BUILD_IP_PROFILE = "build_ip_profile"
    BUILD_VOICE_PROFILE = "build_voice_profile"
    BUILD_TALKING_PROFILE = "build_talking_profile"
    PLAN_CONTENT = "plan_content"
    MATCH_ASSETS = "match_assets"
    GENERATE_VOICE = "generate_voice"
    CREATE_VOICE_PACE_CANDIDATE = "create_voice_pace_candidate"
    VERIFY_VOICE = "verify_voice"
    ALIGN_VOICE_BOUNDARIES = "align_voice_boundaries"
    GENERATE_TALKING = "generate_talking"
    VERIFY_TALKING = "verify_talking"
    PREPARE_TALKING_RUN_PREVIEW = "prepare_talking_run_preview"
    RENDER = "render"
    SYNC_ACCOUNT = "sync_account"


class ConsentBasis(StrEnum):
    SELF = "self"
    EXPLICIT_AUTHORIZATION = "explicit_authorization"


class ConsentRecord(ContractModel):
    subject_name: str = Field(min_length=1, max_length=200)
    basis: ConsentBasis
    confirmed: bool
    confirmed_at: AwareDatetime
    authorization_reference: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def requires_confirmed_consent(self) -> "ConsentRecord":
        if not self.confirmed:
            raise ValueError("consent must be explicitly confirmed")
        if self.basis == ConsentBasis.EXPLICIT_AUTHORIZATION and not self.authorization_reference:
            raise ValueError("authorization_reference is required for explicit authorization")
        return self


class RationalFps(ContractModel):
    numerator: int = Field(gt=0, le=240_000, strict=True)
    denominator: int = Field(gt=0, le=10_000, strict=True)

    @property
    def value(self) -> float:
        return self.numerator / self.denominator


class IPProfile(ContractModel):
    id: UUID = Field(default_factory=uuid4)
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
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class AnalysisKeyframe(ContractModel):
    timestamp_ms: NonNegativeMs
    reference: str = Field(min_length=1, max_length=2_000, description="Stable local analysis reference; not a secret URL.")
    description: str | None = Field(default=None, max_length=5_000)


class AnalysisClipResult(ContractModel):
    asset_id: UUID
    clip_id: UUID = Field(default_factory=uuid4)
    start_ms: NonNegativeMs
    end_ms: PositiveFrames
    transcript: str | None = Field(default=None, max_length=100_000)
    available_subtitles: list[str] = Field(default_factory=list, max_length=100)
    visual_description: str | None = Field(default=None, max_length=5_000)
    people: list[str] = Field(default_factory=list, max_length=30)
    objects: list[str] = Field(default_factory=list, max_length=100)
    action: str | None = Field(default=None, max_length=500)
    confidence: Score | None = None
    keyframes: list[AnalysisKeyframe] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def valid_interval(self) -> "AnalysisClipResult":
        if self.end_ms <= self.start_ms:
            raise ValueError("analysis clip interval must be valid")
        if any(frame.timestamp_ms < self.start_ms or frame.timestamp_ms > self.end_ms for frame in self.keyframes):
            raise ValueError("analysis keyframes must fall within the clip interval")
        return self


class AnalysisResultBundle(ContractModel):
    id: UUID = Field(default_factory=uuid4)
    input_hash: str = Field(min_length=16, max_length=128, pattern=r"^[A-Fa-f0-9]+$")
    mode: Literal["assisted_test", "runtime"]
    source: str = Field(min_length=1, max_length=200, description="Origin of the analysis result, never credentials.")
    model: str = Field(min_length=1, max_length=200)
    tool: str = Field(min_length=1, max_length=200)
    analyzed_at: AwareDatetime
    ip_profile_id: UUID | None = None
    ip_profile_version: int | None = Field(default=None, gt=0)
    profile_snapshot: dict[str, JsonValue] = Field(default_factory=dict)
    results: list[AnalysisClipResult] = Field(min_length=1, max_length=10_000)


class AccountConnection(ContractModel):
    id: UUID = Field(default_factory=uuid4)
    provider: str = Field(min_length=1, max_length=100, description="Provider name only; never credentials.")
    account_external_id: str = Field(min_length=1, max_length=500)
    display_name: str | None = Field(default=None, max_length=200)
    read_only: bool = True
    connected_at: AwareDatetime
    last_synced_at: AwareDatetime | None = None


class HistoricalContent(ContractModel):
    id: UUID = Field(default_factory=uuid4)
    account_connection_id: UUID
    external_id: str = Field(min_length=1, max_length=500)
    title: str = Field(min_length=1, max_length=500)
    published_at: AwareDatetime | None = None
    description: str | None = Field(default=None, max_length=10_000)
    transcript: str | None = Field(default=None, max_length=100_000)
    metrics: dict[str, int | float] = Field(default_factory=dict)

    @field_validator("metrics")
    @classmethod
    def metrics_are_finite(cls, values: dict[str, int | float]) -> dict[str, int | float]:
        for name, value in values.items():
            if isinstance(value, float) and value != value or isinstance(value, float) and value in (float("inf"), float("-inf")):
                raise ValueError(f"metric {name!r} must be finite")
        return values


class ContentOpportunity(ContractModel):
    """A traceable topic opportunity supplied by a user or a read-only source."""

    id: UUID = Field(default_factory=uuid4)
    dedupe_key: str = Field(min_length=1, max_length=700)
    source_type: Literal["manual", "historical_content", "account_signal"]
    source_ref: str = Field(min_length=1, max_length=500)
    title: str = Field(min_length=1, max_length=500)
    observed_at: AwareDatetime
    fit_reason: str = Field(min_length=1, max_length=5_000)
    angle: str = Field(min_length=1, max_length=5_000)
    uncertainty: str | None = Field(default=None, max_length=5_000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=100)
    status: Literal["new", "used", "dismissed"] = "new"
    created_at: AwareDatetime


class Asset(ContractModel):
    id: UUID = Field(default_factory=uuid4)
    source_kind: SourceKind = SourceKind.USER_ASSET
    source_file: str = Field(min_length=1, max_length=2_000, description="Local logical path, never a secret URL.")
    content_hash: str = Field(min_length=16, max_length=128)
    duration_ms: PositiveFrames
    width: int = Field(gt=0, le=16_384)
    height: int = Field(gt=0, le=16_384)
    fps: RationalFps
    has_audio: bool = False
    authorization_reference: str = Field(min_length=1, max_length=500, description="Rights or authorization record reference; never credentials.")
    imported_at: AwareDatetime
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class SourceCropWindow(ContractModel):
    """One fixed pixel-space rectangle in an original video Asset."""

    x: int = Field(ge=0, le=16_383)
    y: int = Field(ge=0, le=16_383)
    width: int = Field(gt=0, le=16_384)
    height: int = Field(gt=0, le=16_384)


class VerifiedVerticalDerivationEvidence(ContractModel):
    """Evidence that one exact source interval is safe for one fixed crop."""

    source_asset_id: UUID
    source_clip_id: UUID
    source_content_hash: str | None = Field(default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    crop_window: SourceCropWindow | None = None
    covered_start_ms: NonNegativeMs
    covered_end_ms: PositiveFrames
    method: Literal["human_full_interval_review"] | None = None
    method_version: str | None = Field(default=None, min_length=1, max_length=100)
    coverage: Literal["full_interval_continuous"] | None = None
    confidence: Literal["reviewed"] | None = None
    face_head_clearance_verified: Literal[True]
    crop_stability_verified: Literal[True]
    source_subtitle_clearance_verified: Literal[True]
    evidence_reference: str = Field(min_length=1, max_length=2_000)

    @model_validator(mode="after")
    def covered_interval_is_positive(self) -> "VerifiedVerticalDerivationEvidence":
        if self.covered_end_ms <= self.covered_start_ms:
            raise ValueError("derivation evidence end_ms must be greater than start_ms")
        return self


class VerifiedVerticalDerivationRequest(ContractModel):
    """A bounded, evidence-backed transform from one horizontal Clip to 9:16 media.

    This deliberately accepts a fixed crop only. It does not claim face
    tracking, subtitle removal, or a general approval for the parent Asset.
    """

    idempotency_key: str = Field(min_length=1, max_length=500)
    source_asset_id: UUID
    source_clip_id: UUID
    start_ms: NonNegativeMs
    end_ms: PositiveFrames
    crop_window: SourceCropWindow
    source_subtitle_state: Literal["known_burned_in", "not_observed"]
    known_subtitle_regions: list[SourceCropWindow] = Field(default_factory=list, max_length=100)
    evidence: VerifiedVerticalDerivationEvidence

    @model_validator(mode="after")
    def request_interval_and_subtitle_claim_are_consistent(self) -> "VerifiedVerticalDerivationRequest":
        if self.end_ms <= self.start_ms:
            raise ValueError("vertical derivation end_ms must be greater than start_ms")
        if self.evidence.source_asset_id != self.source_asset_id or self.evidence.source_clip_id != self.source_clip_id:
            raise ValueError("derivation evidence must name the requested source Asset and Clip")
        if self.evidence.crop_window is not None and self.evidence.crop_window != self.crop_window:
            raise ValueError("derivation evidence must name the exact requested crop")
        if self.evidence.covered_start_ms > self.start_ms or self.evidence.covered_end_ms < self.end_ms:
            raise ValueError("derivation evidence must cover the complete requested interval")
        if self.source_subtitle_state == "known_burned_in" and not self.known_subtitle_regions:
            raise ValueError("known burned-in subtitles require their source regions")
        if self.source_subtitle_state == "not_observed" and self.known_subtitle_regions:
            raise ValueError("subtitle regions require known_burned_in source state")
        return self


class ImageAsset(ContractModel):
    id: UUID = Field(default_factory=uuid4)
    source_kind: Literal[SourceKind.SCREENSHOT, SourceKind.CHART] = SourceKind.SCREENSHOT
    source_file: str = Field(min_length=1, max_length=2_000, description="Local logical path, never a secret URL.")
    content_hash: str = Field(min_length=16, max_length=128)
    width: int = Field(gt=0, le=16_384)
    height: int = Field(gt=0, le=16_384)
    authorization_reference: str = Field(min_length=1, max_length=500, description="Rights or authorization record reference; never credentials.")
    imported_at: AwareDatetime
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class TranscriptSegment(ContractModel):
    """A persisted, source-relative spoken interval returned by ASR."""

    start_ms: NonNegativeMs
    end_ms: PositiveFrames
    text: str = Field(min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def valid_interval(self) -> "TranscriptSegment":
        if self.end_ms <= self.start_ms:
            raise ValueError("transcript segment end_ms must be greater than start_ms")
        return self


class AudioAsset(ContractModel):
    id: UUID = Field(default_factory=uuid4)
    source_kind: Literal[SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET] = SourceKind.USER_ASSET
    source_file: str = Field(min_length=1, max_length=2_000, description="Local logical path, never a secret URL.")
    content_hash: str = Field(min_length=16, max_length=128)
    duration_ms: PositiveFrames
    sample_rate: int = Field(gt=0, le=192_000)
    channels: int = Field(gt=0, le=8)
    language: str | None = Field(default=None, max_length=20)
    authorization_reference: str = Field(min_length=1, max_length=500, description="Rights or authorization record reference; never credentials.")
    imported_at: AwareDatetime
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    transcript_segments: list[TranscriptSegment] = Field(default_factory=list, max_length=10_000)
    transcript_source: str | None = Field(default=None, max_length=500, description="Traceable source reference for the current transcript.")


class VoiceReviewOutcome(StrEnum):
    """One explicit human judgment for a creator-Voice quality dimension."""

    PASS = "pass"
    NEEDS_REVISION = "needs_revision"


class VoiceHumanReview(ContractModel):
    """A durable U-Voice judgment after independent automated Voice QA.

    Copy, timing and file checks cannot establish creator likeness or whether
    the delivery actually carries the intended emphasis and rhythm.  Keep
    those subjective judgments explicit, dimensioned and tied to one exact
    asset rather than treating an ordinary provider success as quality proof.
    """

    approved: bool
    evidence_reference: str = Field(min_length=1, max_length=500)
    findings: list[str] = Field(min_length=1, max_length=100)
    likeness: VoiceReviewOutcome
    naturalness: VoiceReviewOutcome
    emphasis: VoiceReviewOutcome
    pace: VoiceReviewOutcome
    pauses: VoiceReviewOutcome
    rhythm: VoiceReviewOutcome
    reviewed_at: AwareDatetime

    @field_validator("findings")
    @classmethod
    def concrete_findings(cls, values: list[str]) -> list[str]:
        if any(not isinstance(item, str) or not item.strip() for item in values):
            raise ValueError("Voice human review findings must be non-empty")
        return values

    @model_validator(mode="after")
    def approval_matches_dimension_judgments(self) -> "VoiceHumanReview":
        outcomes = (
            self.likeness,
            self.naturalness,
            self.emphasis,
            self.pace,
            self.pauses,
            self.rhythm,
        )
        all_pass = all(outcome is VoiceReviewOutcome.PASS for outcome in outcomes)
        if self.approved != all_pass:
            raise ValueError("Voice human review approval must match all six dimension judgments")
        return self


class Clip(ContractModel):
    id: UUID = Field(default_factory=uuid4)
    asset_id: UUID
    start_ms: NonNegativeMs
    end_ms: NonNegativeMs
    asset_duration_ms: PositiveFrames
    transcript: str | None = Field(default=None, max_length=100_000)
    transcript_segments: list[TranscriptSegment] = Field(default_factory=list, max_length=10_000)
    transcript_source: str | None = Field(default=None, max_length=500, description="Traceable source reference for the current transcript.")
    speaker_ids: list[str] = Field(default_factory=list, max_length=20)
    visual_description: str | None = Field(default=None, max_length=5_000)
    people: list[str] = Field(default_factory=list, max_length=30)
    objects: list[str] = Field(default_factory=list, max_length=100)
    location: str | None = Field(default=None, max_length=500)
    action: str | None = Field(default=None, max_length=500)
    shot_type: str | None = Field(default=None, max_length=100)
    orientation: ProjectFormat | None = None
    motion_level: Score | None = None
    quality_score: Score | None = None
    speech_quality: Score | None = None
    face_visibility: Score | None = None
    mouth_visibility: Score | None = None
    talking_candidate: bool = False
    talking_reference_assessment: "TalkingReferenceAssessment | None" = None
    voice_candidate: bool = False
    embedding_ref: str | None = Field(default=None, max_length=500)
    used_count: int = Field(default=0, ge=0)
    last_used_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def valid_interval(self) -> "Clip":
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms must be greater than start_ms")
        if self.end_ms > self.asset_duration_ms:
            raise ValueError("clip interval exceeds asset duration")
        return self


class VoiceProfile(ContractModel):
    id: UUID = Field(default_factory=uuid4)
    name: str = Field(min_length=1, max_length=200)
    provider: str = Field(min_length=1, max_length=100)
    provider_profile_id: str = Field(min_length=1, max_length=500)
    reference_clip_ids: list[UUID] = Field(min_length=1, max_length=100)
    consent: ConsentRecord
    language: str | None = Field(default=None, pattern=r"^[a-z]{2,3}(-[A-Z]{2})?$")
    created_at: AwareDatetime


class TalkingProfile(ContractModel):
    id: UUID = Field(default_factory=uuid4)
    name: str = Field(min_length=1, max_length=200)
    provider: str = Field(min_length=1, max_length=100)
    provider_profile_id: str | None = Field(default=None, max_length=500)
    reference_clip_ids: list[UUID] = Field(min_length=1, max_length=100)
    consent: ConsentRecord
    created_at: AwareDatetime


class GazeDirection(StrEnum):
    """Visible face direction recorded for a Talking reference, never inferred later."""

    CAMERA = "camera"
    AWAY = "away"
    UNCERTAIN = "uncertain"


class TalkingReferenceAssessment(ContractModel):
    """Sourced evidence about a specific reference Clip's usable performance.

    These fields describe observed source material.  They are deliberately
    distinct from Talking-provider capabilities: an audio-driven lip-sync
    model may preserve an observed gaze, but must not be treated as able to
    create a new one.
    """

    start_gaze: GazeDirection | None = None
    end_gaze: GazeDirection | None = None
    expression_labels: list[str] = Field(default_factory=list, max_length=20)
    burned_in_subtitles: bool | None = None
    evidence_reference: str = Field(min_length=1, max_length=500)


class TalkingPerformanceBrief(ContractModel):
    """Product-level requirements for selecting a creator Talking reference."""

    start_gaze: GazeDirection | None = None
    end_gaze: GazeDirection | None = None
    expression_labels: list[str] = Field(default_factory=list, max_length=20)
    require_no_burned_subtitles: bool = True
    minimum_reference_duration_ms: PositiveFrames = 3_000


class TalkingReferenceFit(ContractModel):
    clip_id: UUID
    eligible: bool
    score: Score = 0
    reasons: list[str] = Field(default_factory=list, max_length=20)


class TalkingReferenceSelection(ContractModel):
    """One transparent reference choice or a capture requirement."""

    fits: list[TalkingReferenceFit] = Field(min_length=1, max_length=100)
    selected_clip_id: UUID | None = None
    requires_capture: bool = False
    capture_instruction: str | None = Field(default=None, max_length=2_000)

    @model_validator(mode="after")
    def selection_is_explicit(self) -> "TalkingReferenceSelection":
        if self.selected_clip_id is None and not self.requires_capture:
            raise ValueError("Talking reference selection requires a selected clip or capture")
        if self.selected_clip_id is not None and self.requires_capture:
            raise ValueError("Talking reference selection cannot select a clip and require capture")
        if self.requires_capture and not self.capture_instruction:
            raise ValueError("Talking reference capture requirement needs an instruction")
        return self


class Project(ContractModel):
    id: UUID = Field(default_factory=uuid4)
    ip_profile_id: UUID
    title: str = Field(min_length=1, max_length=500)
    topic: str = Field(min_length=1, max_length=5_000)
    format: ProjectFormat = ProjectFormat.VERTICAL
    resolution_width: int = Field(default=1080, gt=0, le=16_384)
    resolution_height: int = Field(default=1920, gt=0, le=16_384)
    fps: RationalFps
    created_at: AwareDatetime


class VisualIntent(ContractModel):
    subject: str | None = Field(default=None, max_length=500)
    action: str | None = Field(default=None, max_length=500)
    framing: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=2_000)


class GraphicCardPlan(ContractModel):
    """Explicit executable text card, or a named unsupported visual need."""

    kind: Literal["text_card", "unsupported"]
    treatment: Literal["headline", "key_point", "contrast"] | None = None
    points: list[str] = Field(default_factory=list, max_length=4)
    reason: str | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def bounded_realization(self) -> "GraphicCardPlan":
        if self.kind == "unsupported":
            if self.reason is None or self.treatment is not None or self.points:
                raise ValueError("unsupported graphics require a reason and no executable card")
        else:
            if self.reason is not None or self.treatment is None or not self.points or any(
                not point.strip() or len(point) > 24 or "\n" in point or "\r" in point for point in self.points
            ) or sum(len(point) for point in self.points) > 72:
                raise ValueError("text cards require bounded nonblank points and treatment")
            if self.treatment == "headline" and len(self.points) != 1:
                raise ValueError("headline requires one point")
            if self.treatment == "contrast" and len(self.points) != 2:
                raise ValueError("contrast requires two points")
        return self


class ScenePlan(ContractModel):
    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    scene_id: str = Field(min_length=1, max_length=100)
    order: int = Field(ge=0, le=10_000)
    purpose: str = Field(min_length=1, max_length=100)
    voice_text: str = Field(min_length=1, max_length=10_000)
    duration_target_ms: PositiveFrames
    visual_intent: VisualIntent
    graphic_plan: GraphicCardPlan | None = None
    preferred_sources: list[SourceKind] = Field(min_length=1, max_length=10)
    fallback_sources: list[SourceKind] = Field(default_factory=list, max_length=10)
    caption_emphasis: list[str] = Field(default_factory=list, max_length=30)
    evidence_refs: list[str] = Field(default_factory=list, max_length=100, description="Traceable IP/material evidence identifiers.")
    visual_requirement: Literal["unknown", "creator_speaking", "action_evidence", "explanatory"] = "unknown"
    visual_requirement_reason: str | None = Field(default=None, min_length=1, max_length=1000)

    @field_validator("visual_requirement_reason", mode="before")
    @classmethod
    def visual_requirement_reason_not_blank(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            raise PydanticCustomError("visual_requirement_reason_required", "editorial reason must not be blank")
        return value

    @model_validator(mode="after")
    def visual_requirement_has_reason(self) -> "ScenePlan":
        if self.visual_requirement != "unknown" and not (self.visual_requirement_reason or "").strip():
            raise PydanticCustomError("visual_requirement_reason_required", "explicit visual requirement needs an editorial reason")
        return self


class UsageCost(ContractModel):
    category: CostCategory
    amount: Decimal | None = Field(default=None, ge=Decimal("0"), max_digits=12, decimal_places=4)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    provider: str | None = Field(default=None, max_length=100)
    note: str | None = Field(default=None, max_length=500)

    @field_validator("amount")
    @classmethod
    def amount_is_finite(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and not value.is_finite():
            raise ValueError("amount must be finite")
        return value

    @model_validator(mode="after")
    def currency_matches_price_availability(self) -> "UsageCost":
        if self.amount is None and self.currency is not None:
            raise ValueError("currency requires a known amount")
        if self.amount is not None and self.currency is None:
            raise ValueError("currency is required for a known amount")
        return self


class CostLineItem(ContractModel):
    """One explainable line in a project execution estimate."""

    scope: Literal["scene", "provider", "render", "audio"]
    scene_plan_id: UUID | None = None
    label: str = Field(min_length=1, max_length=200)
    cost: UsageCost

    @model_validator(mode="after")
    def scene_scope_has_scene(self) -> "CostLineItem":
        if self.scope == "scene" and self.scene_plan_id is None:
            raise ValueError("scene cost line items require a scene_plan_id")
        if self.scope != "scene" and self.scene_plan_id is not None:
            raise ValueError("only scene cost line items may reference a scene_plan_id")
        return self


class CostEstimate(ContractModel):
    """Project-facing cost summary; unknown amounts remain explicitly unknown."""

    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    known_amount: Decimal = Field(default=Decimal("0"), ge=Decimal("0"), max_digits=12, decimal_places=4)
    unknown_cost_count: int = Field(default=0, ge=0, strict=True)
    selected_scene_count: int = Field(default=0, ge=0, strict=True)
    required_scene_count: int = Field(default=0, ge=0, strict=True)
    line_items: tuple[CostLineItem, ...] = Field(default_factory=tuple, max_length=1_000)

    @field_validator("known_amount")
    @classmethod
    def known_amount_is_finite(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("known_amount must be finite")
        return value

    @model_validator(mode="after")
    def totals_match_line_items(self) -> "CostEstimate":
        known = Decimal("0")
        unknown = 0
        for item in self.line_items:
            if item.cost.amount is None or item.cost.currency != self.currency:
                unknown += 1
            else:
                known += item.cost.amount
        if known != self.known_amount or unknown != self.unknown_cost_count:
            raise ValueError("cost totals must match line_items")
        if self.selected_scene_count > self.required_scene_count:
            raise ValueError("selected_scene_count cannot exceed required_scene_count")
        return self


class BudgetPolicy(ContractModel):
    """A local spending/call ceiling; it never stores provider credentials."""

    id: UUID = Field(default_factory=uuid4)
    project_id: UUID | None = None
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    max_amount: Decimal | None = Field(default=None, ge=Decimal("0"), max_digits=12, decimal_places=4)
    max_calls: int | None = Field(default=None, ge=0, strict=True)
    allow_unknown_cost: bool = False
    updated_at: AwareDatetime

    @field_validator("max_amount")
    @classmethod
    def max_amount_is_finite(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and not value.is_finite():
            raise ValueError("max_amount must be finite")
        return value


class ProviderMachineSetting(ContractModel):
    """One saved Advanced Settings override for an exact local execution key."""

    id: UUID = Field(default_factory=uuid4)
    capability: str = Field(min_length=1, max_length=100)
    mode: Literal["local", "remote"]
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    runtime: str = Field(min_length=1, max_length=200)
    machine_id: str = Field(min_length=1, max_length=200)
    values: dict[str, JsonValue] = Field(min_length=1, max_length=100)
    updated_at: AwareDatetime

    @field_validator("values")
    @classmethod
    def values_are_scalar(cls, values: dict[str, JsonValue]) -> dict[str, JsonValue]:
        for key, value in values.items():
            if not key.strip() or not isinstance(value, (str, int, float, bool)) or isinstance(value, float) and not isfinite(value):
                raise ValueError("provider-machine setting values must be finite JSON scalars")
        return values

    @property
    def scope_key(self) -> str:
        return ":".join((self.capability, self.mode, self.provider, self.model, self.runtime, self.machine_id))


ExecutionPurpose = Literal["commercial_production", "internal_evaluation"]
ProviderUseOperation = Literal["generation", "derivation", "review", "download"]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ExecutionUseIdentity(ContractModel):
    capability: str = Field(min_length=1, max_length=100)
    mode: Literal["local", "remote"]
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    runtime: str = Field(min_length=1, max_length=200)
    machine_id: str = Field(min_length=1, max_length=200)
    model_artifact_sha256: Sha256


class ProviderUseLicenseReview(ContractModel):
    """Operator-reviewed applicability report; adoption is a separate trusted action."""
    policy_version: Literal[1] = 1
    evidence_class: Literal["operator_verified"]
    project_id: UUID
    purpose: Literal["internal_evaluation"]
    capability_profile_id: UUID
    configuration_sha256: Sha256
    identity: ExecutionUseIdentity
    intended_activity: str = Field(min_length=1, max_length=2000)
    license_source: str = Field(min_length=1, max_length=2000)
    license_version: str = Field(min_length=1, max_length=200)
    license_terms_reference: str = Field(min_length=1, max_length=1000)
    license_terms_sha256: Sha256
    permits_intended_activity: Literal[True]
    permitted_operations: list[ProviderUseOperation] = Field(min_length=1, max_length=4)
    findings: list[str] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def distinct_operations_and_findings(self) -> "ProviderUseLicenseReview":
        if len(set(self.permitted_operations)) != len(self.permitted_operations):
            raise ValueError("permitted operations must be distinct")
        if any(not line.strip() or len(line) > 2000 for line in self.findings):
            raise ValueError("license findings must be nonempty and bounded")
        return self


class ExecutionArtifact(BaseModel):
    """Logical artifact identity; deployment paths do not enter the digest."""
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    role: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=500)
    size_bytes: int = Field(gt=0)
    sha256: Sha256

    @field_validator("role", "name")
    @classmethod
    def logical_name(cls, value: str) -> str:
        if (value != value.strip() or "\\" in value or ":" in value
            or any(part in ("", ".", "..") for part in value.split("/"))):
            raise ValueError("execution_artifact_name_invalid")
        return value


def execution_canonical_sha256(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


class ExecutionMachineObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    installation_id: UUID
    host_sha256: Sha256
    device_sha256: Sha256


class ExecutionSpecification(BaseModel):
    """Selected execution configuration, independent of mutable evidence rows.

    Completeness is owned by the adapter's observation recipe; this contract
    validates structure and identity, never grants permission or quality.
    """
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[1]
    capability: str = Field(min_length=1, max_length=100)
    mode: Literal["local"] = "local"
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    runtime: str = Field(min_length=1, max_length=200)
    machine_id: str = Field(min_length=1, max_length=200)
    observation_recipe: str = Field(min_length=1, max_length=100)
    observation_version: int = Field(gt=0)
    model_artifacts: tuple[ExecutionArtifact, ...] = Field(min_length=1, max_length=512)
    runtime_artifacts: tuple[ExecutionArtifact, ...] = Field(min_length=1, max_length=512)
    machine: ExecutionMachineObservation
    parameters: dict[str, str | int | float | bool] = Field(min_length=1, max_length=100)

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value):
        if type(value) is not int:
            raise ValueError("execution_version_invalid")
        return value

    @field_validator("capability", "provider", "model", "runtime", "machine_id", "observation_recipe")
    @classmethod
    def exact_identity_label(cls, value):
        if value != value.strip() or not value.strip():
            raise ValueError("execution_identity_label_invalid")
        return value

    @field_validator("parameters", mode="before")
    @classmethod
    def exact_scalar_parameters(cls, value):
        if not isinstance(value, dict) or any(
            not isinstance(k, str) or not k.strip() or k != k.strip()
            or type(v) not in (str, int, float, bool)
            or isinstance(v, float) and not isfinite(v) for k, v in value.items()):
            raise ValueError("execution_parameters_invalid")
        return value

    @model_validator(mode="after")
    def complete_manifest_structure(self):
        for entries, required in ((self.model_artifacts, {"weights"}),
                                  (self.runtime_artifacts, {"interpreter", "entrypoint", "dependency"})):
            keys = [(entry.role, entry.name) for entry in entries]
            if len(keys) != len(set(keys)) or not required.issubset(entry.role for entry in entries):
                raise ValueError("execution_manifest_invalid")
        return self

    def canonical(self) -> dict:
        value = self.model_dump(mode="json")
        for field in ("model_artifacts", "runtime_artifacts"):
            value[field] = sorted(value[field], key=lambda item: (item["role"], item["name"]))
        return value

    @property
    def execution_sha256(self) -> str:
        return execution_canonical_sha256(self.canonical())

    @property
    def model_artifact_sha256(self) -> str:
        return execution_canonical_sha256({"manifest_version": 1,
            "artifacts": self.canonical()["model_artifacts"]})

    @property
    def identity(self) -> ExecutionUseIdentity:
        return ExecutionUseIdentity(**{key: getattr(self, key) for key in
            ("capability", "mode", "provider", "model", "runtime", "machine_id")},
            model_artifact_sha256=self.model_artifact_sha256)


class ExecutionSpecificationV2(BaseModel):
    """Detached runtime inventory; schema1's serialized meaning is unchanged."""
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[2]
    capability: str = Field(min_length=1, max_length=100)
    mode: Literal["local"] = "local"
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    runtime: str = Field(min_length=1, max_length=200)
    machine_id: str = Field(min_length=1, max_length=200)
    observation_recipe: str = Field(min_length=1, max_length=100)
    observation_version: int = Field(gt=0)
    model_artifacts: tuple[ExecutionArtifact, ...] = Field(min_length=1, max_length=512)
    runtime_inventory: RuntimeInventoryDescriptor
    host_runtime: HostRuntimeObservation
    machine: ExecutionMachineObservation
    parameters: dict[str, str | int | float | bool] = Field(min_length=1, max_length=100)

    _version = field_validator("schema_version", mode="before")(ExecutionSpecification.exact_schema_version.__func__)
    _labels = field_validator("capability", "provider", "model", "runtime", "machine_id", "observation_recipe")(
        ExecutionSpecification.exact_identity_label.__func__)
    _parameters = field_validator("parameters", mode="before")(ExecutionSpecification.exact_scalar_parameters.__func__)

    @model_validator(mode="after")
    def valid_model_and_device(self):
        keys = [(item.role, item.name) for item in self.model_artifacts]
        if len(keys) != len(set(keys)) or not any(item.role == "weights" for item in self.model_artifacts):
            raise ValueError("execution_manifest_invalid")
        expected = "cpu" if self.host_runtime.device.kind == "cpu" else "cuda:0"
        if self.parameters.get("device") != expected:
            raise ValueError("execution_host_device_mismatch")
        return self

    def canonical(self) -> dict:
        value = self.model_dump(mode="json")
        value["model_artifacts"].sort(key=lambda item: (item["role"], item["name"]))
        value["host_runtime"] = self.host_runtime.canonical()
        return value

    execution_sha256 = ExecutionSpecification.execution_sha256
    model_artifact_sha256 = ExecutionSpecification.model_artifact_sha256
    identity = ExecutionSpecification.identity


class ExecutionSpecificationV3(BaseModel):
    """Explicit schema3/host3-policy2 representation; not dispatch authority.

    Standalone rather than a V2 subclass: old typed-instance validators must
    not accept this version. Neither its digest nor a host observation proves
    complete model preparation, current license authority or dispatch rights.
    """
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[3]
    capability: str = Field(min_length=1, max_length=100)
    mode: Literal["local"] = "local"
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    runtime: str = Field(min_length=1, max_length=200)
    machine_id: str = Field(min_length=1, max_length=200)
    observation_recipe: str = Field(min_length=1, max_length=100)
    observation_version: int = Field(gt=0)
    model_artifacts: tuple[ExecutionArtifact, ...] = Field(min_length=1, max_length=512)
    runtime_inventory: RuntimeInventoryDescriptor
    host_runtime: HostRuntimeObservationV3Policy2
    machine: ExecutionMachineObservation
    parameters: dict[str, str | int | float | bool] = Field(min_length=1, max_length=100)

    _version = field_validator("schema_version", mode="before")(ExecutionSpecification.exact_schema_version.__func__)
    _labels = field_validator("capability", "provider", "model", "runtime", "machine_id", "observation_recipe")(
        ExecutionSpecification.exact_identity_label.__func__)
    _parameters = field_validator("parameters", mode="before")(ExecutionSpecification.exact_scalar_parameters.__func__)
    _manifest = model_validator(mode="after")(ExecutionSpecificationV2.valid_model_and_device)

    canonical = ExecutionSpecificationV2.canonical
    execution_sha256 = ExecutionSpecification.execution_sha256
    model_artifact_sha256 = ExecutionSpecification.model_artifact_sha256
    identity = ExecutionSpecification.identity


def _execution_specification_version(value):
    version = value.get("schema_version") if isinstance(value, dict) else getattr(value, "schema_version", None)
    return version if type(version) is int else None


ExecutionSpecificationRecord = Annotated[
    Annotated[ExecutionSpecification, Tag(1)] | Annotated[ExecutionSpecificationV2, Tag(2)]
    | Annotated[ExecutionSpecificationV3, Tag(3)],
    Discriminator(_execution_specification_version)]


class ProviderUseLicenseReviewV2(ProviderUseLicenseReview):
    policy_version: Literal[2]
    execution_specification: ExecutionSpecificationRecord
    execution_sha256: Sha256

    @field_validator("policy_version", mode="before")
    @classmethod
    def exact_policy_version(cls, value):
        if type(value) is not int:
            raise ValueError("execution_version_invalid")
        return value

    @model_validator(mode="after")
    def execution_matches(self):
        if (self.execution_sha256 != self.execution_specification.execution_sha256
            or self.identity != self.execution_specification.identity):
            raise ValueError("execution_attestation_mismatch")
        return self


class ProviderUseAdmission(ContractModel):
    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=500)
    review_reference: str = Field(min_length=1, max_length=1000)
    review_sha256: Sha256
    review: ProviderUseLicenseReview | ProviderUseLicenseReviewV2
    actor_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,100}$")
    created_at: AwareDatetime
    revoked_at: AwareDatetime | None = None
    revoked_by: str | None = None
    revocation_reason: str | None = None

    @model_validator(mode="after")
    def consistent_scope_and_revocation(self) -> "ProviderUseAdmission":
        if self.project_id != self.review.project_id:
            raise ValueError("admission project differs from report")
        fields = (self.revoked_at, self.revoked_by, self.revocation_reason)
        if any(v is not None for v in fields) and not all(v is not None for v in fields):
            raise ValueError("revocation requires actor, time and reason")
        return self


class ProviderMachineCapabilityProfile(ContractModel):
    """Persisted evidence, distinct from a user-selected Advanced Setting."""

    id: UUID = Field(default_factory=uuid4)
    capability: str = Field(min_length=1, max_length=100)
    mode: Literal["local", "remote"]
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    runtime: str = Field(min_length=1, max_length=200)
    machine_id: str = Field(min_length=1, max_length=200)
    readiness: Literal["implemented", "configured", "available", "verified"]
    verified_parameters: dict[str, JsonValue] = Field(default_factory=dict, max_length=100)
    feature_support: dict[str, Literal["verified", "available", "unsupported", "unknown"]] = Field(default_factory=dict, max_length=100)
    quality_status: Literal["verified", "observed", "unknown"] = "unknown"
    continuity_status: Literal["verified", "observed", "unknown"] = "unknown"
    commercial_status: Literal["unknown", "non_commercial_only", "commercial_safe"] = "unknown"
    license_evidence_reference: str | None = Field(default=None, min_length=1, max_length=1_000)
    provenance_source: str = Field(min_length=1, max_length=500)
    evidence_reference: str | None = Field(default=None, min_length=1, max_length=1_000)
    last_verified_at: AwareDatetime | None = None
    updated_at: AwareDatetime

    @field_validator("verified_parameters")
    @classmethod
    def verified_parameters_are_scalar(cls, values: dict[str, JsonValue]) -> dict[str, JsonValue]:
        for key, value in values.items():
            if not key.strip() or not isinstance(value, (str, int, float, bool)) or isinstance(value, float) and not isfinite(value):
                raise ValueError("capability profile parameters must be finite JSON scalars")
        return values

    @model_validator(mode="after")
    def verified_profile_has_evidence(self) -> "ProviderMachineCapabilityProfile":
        if self.readiness == "verified" and not self.evidence_reference:
            raise ValueError("verified capability profile requires an evidence reference")
        if self.readiness != "verified" and self.verified_parameters:
            raise ValueError("verified parameters require a verified capability profile")
        if self.readiness != "verified" and "verified" in self.feature_support.values():
            raise ValueError("verified feature support requires a verified capability profile")
        if self.commercial_status == "commercial_safe" and not self.license_evidence_reference:
            raise ValueError("commercial-safe capability requires license evidence")
        return self

    @property
    def scope_key(self) -> str:
        return ":".join((self.capability, self.mode, self.provider, self.model, self.runtime, self.machine_id))


class ProviderCallRecord(ContractModel):
    """The auditable boundary around a model/provider operation.

    A reservation is persisted before an external call.  Completion may carry
    observed usage and actual cost, while unknown values remain unknown rather
    than being converted to zero.
    """

    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=500)
    operation: Literal["scene_planning", "asr", "vision", "embedding", "tts", "talking", "render"]
    mode: Literal["assisted_test", "runtime"]
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    input_source: str = Field(min_length=1, max_length=500)
    input_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    status: Literal["reserved", "running", "completed", "failed", "cancelled"] = "reserved"
    estimated_cost: UsageCost
    actual_cost: UsageCost | None = None
    input_tokens: int | None = Field(default=None, ge=0, strict=True)
    output_tokens: int | None = Field(default=None, ge=0, strict=True)
    cache_tokens: int | None = Field(default=None, ge=0, strict=True)
    usage_observable: bool | None = None
    price_date: date | None = None
    error_code: str | None = Field(default=None, max_length=100)
    result_payload: dict[str, JsonValue] | None = None
    created_at: AwareDatetime
    completed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def terminal_timestamps_are_consistent(self) -> "ProviderCallRecord":
        terminal = {"completed", "failed", "cancelled"}
        if self.completed_at is not None and self.status not in terminal:
            raise ValueError("completed_at requires a terminal provider call status")
        if self.status == "completed" and self.actual_cost is None:
            # A provider may not expose a billable amount; the record remains
            # auditable with actual_cost=null instead of silently claiming zero.
            return self
        if self.result_payload is not None and self.status != "completed":
            raise ValueError("result_payload requires a completed provider call")
        return self


class CandidateAsset(ContractModel):
    scene_plan_id: UUID
    source_kind: SourceKind
    asset_id: UUID | None = None
    clip_id: UUID | None = None
    match_score: Score
    why: list[str] = Field(min_length=1, max_length=10)
    reuse_count: int = Field(default=0, ge=0)
    recommended: bool = False
    requires_capture: bool = False
    estimated_cost: UsageCost

    @model_validator(mode="after")
    def candidate_references_match_source(self) -> "CandidateAsset":
        existing_media = {SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET, SourceKind.AI_VIDEO, SourceKind.SCREENSHOT, SourceKind.CHART}
        has_reference = self.asset_id is not None or self.clip_id is not None
        if self.source_kind == SourceKind.CAPTURE and not self.requires_capture:
            raise ValueError("capture candidates must require_capture")
        if self.source_kind != SourceKind.CAPTURE and self.requires_capture:
            raise ValueError("requires_capture is only valid for capture candidates")
        if self.source_kind in existing_media and not has_reference:
            raise ValueError("existing-media candidates need an asset_id or clip_id")
        return self


class CostReductionSuggestion(ContractModel):
    """An explainable, non-destructive lower-cost alternative for one scene."""

    scene_plan_id: UUID
    current: CandidateAsset
    suggested: CandidateAsset
    changed: bool
    reason: str = Field(min_length=1, max_length=500)


class ShootTask(ContractModel):
    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    scene_plan_id: UUID
    scene_id: str = Field(min_length=1, max_length=100)
    what_to_shoot: str = Field(min_length=1, max_length=2_000)
    framing: str = Field(min_length=1, max_length=200)
    duration_ms: PositiveFrames
    requires_speaking: bool
    status: Literal["confirmed", "fulfilled", "dismissed"] = "confirmed"
    asset_id: UUID | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def fulfilled_task_requires_asset(self) -> "ShootTask":
        if self.status == "fulfilled" and self.asset_id is None:
            raise ValueError("fulfilled shoot tasks require an asset_id")
        return self


class DraftRoute(ContractModel):
    scene_plan_id: UUID
    candidates: tuple[CandidateAsset, ...] = Field(min_length=1, max_length=20)


class VerticalReframeMode(StrEnum):
    """An explicit, reviewable treatment of non-vertical source media."""

    CONTAIN = "contain"
    CENTER_CROP = "center_crop"


class VideoVisual(ContractModel):
    source_kind: SourceKind
    authorization_reference: str = Field(min_length=1, max_length=500, description="Reference to the source rights record; never credentials.")
    asset_id: UUID | None = None
    clip_id: UUID | None = None
    clip_start_ms: NonNegativeMs | None = None
    clip_end_ms: NonNegativeMs | None = None
    source_duration_ms: PositiveFrames | None = None
    vertical_reframe_mode: VerticalReframeMode = VerticalReframeMode.CONTAIN
    vertical_reframe_evidence_reference: str | None = Field(default=None, max_length=500)
    source_bottom_crop_ratio: float = Field(default=0, ge=0, le=0.4)

    @model_validator(mode="after")
    def visual_source_is_complete(self) -> "VideoVisual":
        existing_media = {SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET, SourceKind.AI_VIDEO, SourceKind.SCREENSHOT, SourceKind.CHART}
        if self.source_kind in existing_media and self.asset_id is None and self.clip_id is None:
            raise ValueError("existing-media visuals need an asset_id or clip_id")
        values = (self.clip_start_ms, self.clip_end_ms)
        if any(value is not None for value in values):
            if any(value is None for value in values) or self.clip_end_ms <= self.clip_start_ms:  # type: ignore[operator]
                raise ValueError("clip start/end must be a valid complete millisecond interval")
            if self.source_duration_ms is not None and self.clip_end_ms > self.source_duration_ms:
                raise ValueError("clip interval exceeds source duration")
        elif self.source_duration_ms is not None:
            raise ValueError("source_duration_ms requires a clip interval")
        if self.vertical_reframe_mode is VerticalReframeMode.CENTER_CROP and not self.vertical_reframe_evidence_reference:
            raise ValueError("center crop requires a reviewable vertical reframe evidence reference")
        if self.vertical_reframe_mode is VerticalReframeMode.CONTAIN and self.vertical_reframe_evidence_reference is not None:
            raise ValueError("vertical reframe evidence is only valid for an explicit center crop")
        if self.source_bottom_crop_ratio and not self.vertical_reframe_evidence_reference:
            raise ValueError("bottom source crop requires a reviewable vertical reframe evidence reference")
        return self


class VideoCaption(ContractModel):
    """A caption interval relative to the containing video scene."""

    start_ms: NonNegativeMs
    end_ms: PositiveFrames
    text: str = Field(min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def valid_interval(self) -> "VideoCaption":
        if self.end_ms <= self.start_ms:
            raise ValueError("caption end_ms must be greater than start_ms")
        return self


class EditVisualRole(StrEnum):
    """Editorial presentation role, independent of the renderer or provider."""

    TALKING = "talking"
    FOOTAGE = "footage"
    GRAPHIC = "graphic"


class TalkingFramingPolicy(StrEnum):
    """Bounded R1 framing choices for creator Talking footage."""

    FACE_SAFE_CONTAIN = "face_safe_contain"
    VERIFIED_STATIC_CROP = "verified_static_crop"


class PortraitPresentation(StrEnum):
    """Where an authorized source sits inside the vertical output canvas."""

    FULL_CANVAS = "full_canvas"
    PORTRAIT_PANEL = "portrait_panel"


class SubtitleTreatment(StrEnum):
    TIMED_CAPTIONS = "timed_captions"
    NONE = "none"


class GraphicTreatment(StrEnum):
    NONE = "none"
    HEADLINE = "headline"
    KEY_POINT = "key_point"
    CONTRAST = "contrast"


class VisualStyleTokens(ContractModel):
    """Reusable visual tokens consumed by the renderer, never provider settings."""

    name: str = Field(default="content-os-dark", min_length=1, max_length=100)
    background_color: str = Field(default="#172033", pattern=r"^#[0-9a-fA-F]{6}$")
    foreground_color: str = Field(default="#f8fbff", pattern=r"^#[0-9a-fA-F]{6}$")
    accent_color: str = Field(default="#65d3b4", pattern=r"^#[0-9a-fA-F]{6}$")
    font_family: str = Field(default="Inter, ui-sans-serif, system-ui", min_length=1, max_length=500)


class EditPlanFallback(ContractModel):
    """An explicit local graphic fallback if the selected visual cannot be used."""

    source_kind: Literal[SourceKind.TYPOGRAPHY] = SourceKind.TYPOGRAPHY
    graphic_treatment: GraphicTreatment
    text: str = Field(min_length=1, max_length=2_000)

    @model_validator(mode="after")
    def treatment_is_renderable(self) -> "EditPlanFallback":
        if self.graphic_treatment is GraphicTreatment.NONE:
            raise ValueError("graphic fallback requires a semantic graphic treatment")
        return self


class EditPlanScene(ContractModel):
    """One provider-neutral presentation decision for one routed ScenePlan."""

    scene_plan_id: UUID
    scene_id: str = Field(min_length=1, max_length=100)
    visual_role: EditVisualRole
    selected_source_kind: SourceKind
    selected_asset_id: UUID | None = None
    selected_clip_id: UUID | None = None
    framing_policy: TalkingFramingPolicy = TalkingFramingPolicy.FACE_SAFE_CONTAIN
    portrait_presentation: PortraitPresentation = PortraitPresentation.FULL_CANVAS
    crop_safety_evidence_reference: str | None = Field(default=None, max_length=500)
    crop_safety_start_ms: NonNegativeMs | None = None
    crop_safety_end_ms: NonNegativeMs | None = None
    subtitle_treatment: SubtitleTreatment = SubtitleTreatment.TIMED_CAPTIONS
    burned_in_subtitles: Literal["unknown", "present", "absent"] = "unknown"
    graphic_treatment: GraphicTreatment = GraphicTreatment.NONE
    graphic_text: str | None = Field(default=None, min_length=1, max_length=10_000)
    transition_intent: Literal["cut"] = "cut"
    fallback: EditPlanFallback

    @model_validator(mode="after")
    def presentation_is_complete(self) -> "EditPlanScene":
        source_requires_media = self.selected_source_kind in {
            SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET, SourceKind.AI_VIDEO,
        }
        if source_requires_media != (self.selected_asset_id is not None and self.selected_clip_id is not None):
            raise ValueError("continuous selected visuals require both asset and Clip identities")
        if self.selected_source_kind in {SourceKind.SCREENSHOT, SourceKind.CHART} and (
            self.selected_asset_id is None or self.selected_clip_id is not None
        ):
            raise ValueError("static selected visuals require an asset identity without a Clip")
        crop_values = (self.crop_safety_evidence_reference, self.crop_safety_start_ms, self.crop_safety_end_ms)
        if self.framing_policy is TalkingFramingPolicy.VERIFIED_STATIC_CROP:
            if any(value is None for value in crop_values):
                raise ValueError("verified static crop requires evidence and a complete verified interval")
            if self.crop_safety_end_ms is not None and self.crop_safety_start_ms is not None and self.crop_safety_end_ms <= self.crop_safety_start_ms:
                raise ValueError("verified static crop interval must be valid")
        elif any(value is not None for value in crop_values):
            raise ValueError("crop safety evidence is only valid for verified_static_crop")
        if self.portrait_presentation is PortraitPresentation.PORTRAIT_PANEL and (
            self.visual_role is not EditVisualRole.TALKING
            or self.selected_source_kind is not SourceKind.AI_VIDEO
            or self.framing_policy is not TalkingFramingPolicy.FACE_SAFE_CONTAIN
        ):
            raise ValueError("portrait_panel requires a face-safe Talking visual without a crop")
        if self.visual_role is EditVisualRole.GRAPHIC:
            if self.graphic_treatment is GraphicTreatment.NONE or self.graphic_text is None:
                raise ValueError("graphic scenes require semantic treatment and display text")
        elif self.graphic_text is not None and self.graphic_treatment is GraphicTreatment.NONE:
            raise ValueError("graphic text requires a semantic graphic treatment")
        return self


class EditPlan(ContractModel):
    """Visual-direction bridge from ScenePlan/routed assets to VideoSpec."""

    project_id: UUID
    scenes: list[EditPlanScene] = Field(min_length=1, max_length=1_000)
    style_tokens: VisualStyleTokens = Field(default_factory=VisualStyleTokens)

    @model_validator(mode="after")
    def scene_identities_are_unique(self) -> "EditPlan":
        if len({scene.scene_plan_id for scene in self.scenes}) != len(self.scenes):
            raise ValueError("EditPlan scenes must have unique ScenePlan identities")
        if len({scene.scene_id for scene in self.scenes}) != len(self.scenes):
            raise ValueError("EditPlan scenes must have unique scene_id values")
        return self


class VideoScene(ContractModel):
    scene_id: str = Field(min_length=1, max_length=100)
    start_frame: int = Field(ge=0, strict=True)
    duration_frames: PositiveFrames
    visual: VideoVisual
    narration_asset_id: UUID | None = None
    narration_start_ms: NonNegativeMs | None = None
    narration_end_ms: NonNegativeMs | None = None
    caption: str | None = Field(default=None, max_length=10_000)
    captions: list[VideoCaption] = Field(default_factory=list, max_length=1_000)
    transition: str = Field(default="cut", max_length=100)
    visual_role: EditVisualRole = EditVisualRole.FOOTAGE
    framing_policy: TalkingFramingPolicy = TalkingFramingPolicy.FACE_SAFE_CONTAIN
    portrait_presentation: PortraitPresentation = PortraitPresentation.FULL_CANVAS
    subtitle_treatment: SubtitleTreatment = SubtitleTreatment.TIMED_CAPTIONS
    graphic_treatment: GraphicTreatment = GraphicTreatment.NONE
    graphic_text: str | None = Field(default=None, max_length=10_000)
    style_tokens: VisualStyleTokens = Field(default_factory=VisualStyleTokens)

    @model_validator(mode="after")
    def narration_interval_is_complete(self) -> "VideoScene":
        if (self.narration_start_ms is None) != (self.narration_end_ms is None):
            raise ValueError("narration start/end must be supplied together")
        if self.narration_start_ms is not None and self.narration_end_ms <= self.narration_start_ms:
            raise ValueError("narration interval must be valid")
        if self.narration_asset_id is None and self.narration_start_ms is not None:
            raise ValueError("narration interval requires narration_asset_id")
        if self.visual_role is EditVisualRole.GRAPHIC and (
            self.graphic_treatment is GraphicTreatment.NONE or not self.graphic_text
        ):
            raise ValueError("graphic video scenes require semantic treatment and display text")
        return self


class MasterNarration(ContractModel):
    """One authorized, timestamped narration track driving a whole VideoSpec.

    The audio remains a single continuous local asset.  Video scenes reference
    intervals on this track for provenance and caption timing; they do not
    each replay the recording from its beginning.
    """

    audio_asset_id: UUID
    start_ms: NonNegativeMs = 0
    end_ms: PositiveFrames
    transcript_source: str = Field(min_length=1, max_length=500, description="Traceable provider alignment or imported SRT/VTT reference.")
    transcript_segments: list[TranscriptSegment] = Field(min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def complete_timed_track(self) -> "MasterNarration":
        if self.end_ms <= self.start_ms:
            raise ValueError("master narration end_ms must be greater than start_ms")
        previous_end = self.start_ms
        for segment in self.transcript_segments:
            if segment.start_ms < self.start_ms or segment.end_ms > self.end_ms:
                raise ValueError("master narration transcript segment exceeds its audio interval")
            if segment.start_ms < previous_end:
                raise ValueError("master narration transcript segments must be ordered and non-overlapping")
            previous_end = segment.end_ms
        return self


class VideoSpec(ContractModel):
    project_id: UUID
    format: ProjectFormat
    width: int = Field(gt=0, le=16_384)
    height: int = Field(gt=0, le=16_384)
    fps: RationalFps
    scenes: list[VideoScene] = Field(min_length=1, max_length=1_000)
    master_narration: MasterNarration | None = None
    voice_profile_id: UUID | None = None
    estimated_cost: UsageCost | None = None
    edit_plan: EditPlan | None = None

    @model_validator(mode="after")
    def scenes_are_valid_for_timeline(self) -> "VideoSpec":
        if len({scene.scene_id for scene in self.scenes}) != len(self.scenes):
            raise ValueError("video scenes must have unique scene_id values")
        ordered = sorted(self.scenes, key=lambda scene: scene.start_frame)
        for previous, current in zip(ordered, ordered[1:]):
            if current.start_frame < previous.start_frame + previous.duration_frames:
                raise ValueError("video scenes must not overlap")
        for scene in self.scenes:
            visual = scene.visual
            if visual.clip_start_ms is not None and visual.clip_end_ms is not None:
                source_ms = visual.clip_end_ms - visual.clip_start_ms
                required_frame_count = scene.duration_frames - 1
                if source_ms * self.fps.numerator < required_frame_count * self.fps.denominator * 1_000:
                    raise ValueError("source clip is shorter than the scene duration")
        if self.master_narration is not None:
            master = self.master_narration
            if ordered[0].start_frame != 0:
                raise ValueError("master narration timeline must start with the first video scene")
            expected_audio_start = master.start_ms
            expected_video_start = 0
            for scene in ordered:
                if scene.start_frame != expected_video_start:
                    raise ValueError("master narration video scenes must be contiguous")
                if (
                    scene.narration_asset_id != master.audio_asset_id
                    or scene.narration_start_ms is None
                    or scene.narration_end_ms is None
                ):
                    raise ValueError("each master narration video scene must reference its timed master audio interval")
                if scene.narration_start_ms != expected_audio_start:
                    raise ValueError("master narration video scene intervals must be contiguous")
                expected_audio_start = scene.narration_end_ms
                expected_video_start = scene.start_frame + scene.duration_frames
            if expected_audio_start != master.end_ms:
                raise ValueError("master narration video scenes must cover the complete audio interval")
            master_frames = (master.end_ms - master.start_ms) * self.fps.numerator
            frame_denominator = 1_000 * self.fps.denominator
            required_frames = (master_frames + frame_denominator - 1) // frame_denominator
            if expected_video_start < required_frames:
                raise ValueError("master narration video timeline ends before its audio")
        if self.edit_plan is not None and self.edit_plan.project_id != self.project_id:
            raise ValueError("EditPlan must belong to the VideoSpec project")
        return self


class NarrationPace(StrEnum):
    """Editorial pace direction, deliberately not a provider speed value."""

    MEASURED = "measured"
    CONVERSATIONAL = "conversational"
    DRIVEN = "driven"


class NarrationEmphasis(StrEnum):
    """Relative prominence for a spoken range of copy."""

    LIGHT = "light"
    CLEAR = "clear"
    STRONG = "strong"


class NarrationPause(StrEnum):
    """Semantic pause length, never a provider-specific millisecond control."""

    BRIEF = "brief"
    BEAT = "beat"
    LONG = "long"


class NarrationRhythm(StrEnum):
    """The rhetorical job a range plays in the delivery."""

    SETUP = "setup"
    BUILD = "build"
    TURN = "turn"
    LAND = "land"


class NarrationPerformanceCueKind(StrEnum):
    EMPHASIS = "emphasis"
    PACE = "pace"
    PAUSE = "pause"
    RHYTHM = "rhythm"


class NarrationPerformancePlanSource(StrEnum):
    """Who supplied the editable delivery direction."""

    USER = "user"
    ASSISTED = "assisted"
    IMPORTED = "imported"


class NarrationPerformanceCue(ContractModel):
    """One semantic delivery instruction anchored to character positions.

    Character positions are Python/Unicode character offsets in the exact
    current copy. They avoid forcing English-word tokenization onto Chinese
    scripts while keeping a deterministic, auditable anchor for every cue.
    """

    kind: NarrationPerformanceCueKind
    start_char: int = Field(ge=0, le=100_000, strict=True)
    end_char: int = Field(ge=0, le=100_000, strict=True)
    emphasis: NarrationEmphasis | None = None
    pace: NarrationPace | None = None
    pause: NarrationPause | None = None
    rhythm: NarrationRhythm | None = None
    note: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def is_one_semantic_instruction(self) -> "NarrationPerformanceCue":
        expected = {
            NarrationPerformanceCueKind.EMPHASIS: "emphasis",
            NarrationPerformanceCueKind.PACE: "pace",
            NarrationPerformanceCueKind.PAUSE: "pause",
            NarrationPerformanceCueKind.RHYTHM: "rhythm",
        }[self.kind]
        provided = [
            name for name in ("emphasis", "pace", "pause", "rhythm")
            if getattr(self, name) is not None
        ]
        if provided != [expected]:
            raise ValueError("Narration performance cue must supply exactly its matching semantic value")
        if self.kind is NarrationPerformanceCueKind.PAUSE:
            if self.start_char != self.end_char:
                raise ValueError("Narration pause cue must use one zero-width copy boundary")
        elif self.end_char <= self.start_char:
            raise ValueError("Narration performance cue range must be non-empty")
        return self


class NarrationPerformancePlan(ContractModel):
    """Editable, provider-neutral delivery direction for exact narration copy.

    This is narrative intent, not SSML or an execution configuration. A Voice
    adapter may later report that it applied an immutable copy of this plan,
    but the plan alone never claims an acoustic result.
    """

    copy_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    delivery_goal: str = Field(min_length=1, max_length=1_000)
    overall_pace: NarrationPace = NarrationPace.CONVERSATIONAL
    cues: list[NarrationPerformanceCue] = Field(min_length=1, max_length=1_000)
    source: NarrationPerformancePlanSource = NarrationPerformancePlanSource.USER
    evidence_refs: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("evidence_refs")
    @classmethod
    def evidence_refs_are_distinct(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("Narration performance evidence references must not be blank")
        if len(set(values)) != len(values):
            raise ValueError("Narration performance evidence references must be distinct")
        return values

    @model_validator(mode="after")
    def cues_are_unambiguous(self) -> "NarrationPerformancePlan":
        seen: set[tuple[NarrationPerformanceCueKind, int, int]] = set()
        ranges_by_kind: dict[NarrationPerformanceCueKind, list[tuple[int, int]]] = {
            NarrationPerformanceCueKind.EMPHASIS: [],
            NarrationPerformanceCueKind.PACE: [],
            NarrationPerformanceCueKind.RHYTHM: [],
        }
        pause_boundaries: set[int] = set()
        for cue in self.cues:
            identity = (cue.kind, cue.start_char, cue.end_char)
            if identity in seen:
                raise ValueError("Narration performance plan has duplicate cue anchors")
            seen.add(identity)
            ranges = ranges_by_kind.get(cue.kind)
            if ranges is not None:
                if any(cue.start_char < end and start < cue.end_char for start, end in ranges):
                    raise ValueError(f"Narration {cue.kind.value} cues may not overlap")
                ranges.append((cue.start_char, cue.end_char))
            if cue.kind is NarrationPerformanceCueKind.PAUSE:
                if cue.start_char in pause_boundaries:
                    raise ValueError("Narration performance plan has duplicate pause boundaries")
                pause_boundaries.add(cue.start_char)
        return self


class NarrationPerformanceSuggestionPattern(StrEnum):
    """Rhetorical structure detected by the local editorial assistant."""

    OPENING = "opening"
    BUILD = "build"
    TURN = "turn"
    LANDING = "landing"
    PARALLEL_CLAIM = "parallel_claim"
    ENUMERATION = "enumeration"
    CLAIM_BOUNDARY = "claim_boundary"


class NarrationPerformanceSuggestion(ContractModel):
    """One editable suggestion with its structural—not acoustic—reason."""

    cue: NarrationPerformanceCue
    pattern: NarrationPerformanceSuggestionPattern
    rationale: str = Field(min_length=1, max_length=1_000)


class NarrationPerformanceSuggestions(ContractModel):
    """Ephemeral assisted plan plus auditable rationale for each cue.

    This object is intentionally separate from a Draft.  A caller must still
    explicitly save/edit ``suggested_plan`` through the normal plan endpoint.
    """

    copy_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    suggested_plan: NarrationPerformancePlan
    suggestions: list[NarrationPerformanceSuggestion] = Field(min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def suggestion_set_matches_its_assisted_plan(self) -> "NarrationPerformanceSuggestions":
        if self.suggested_plan.copy_fingerprint != self.copy_fingerprint:
            raise ValueError("Narration performance suggestions must match their exact copy")
        if self.suggested_plan.source is not NarrationPerformancePlanSource.ASSISTED:
            raise ValueError("Narration performance suggestions must remain assisted editable input")
        if [item.cue for item in self.suggestions] != self.suggested_plan.cues:
            raise ValueError("Narration performance suggestions must exactly describe the suggested plan cues")
        return self


class NarrationDeliverySegment(ContractModel):
    """One exact-copy interval with inherited editorial delivery direction.

    ``text`` intentionally retains source whitespace. It is a structural
    compiler output, not normalized synthesis text; a future adapter must make
    its own explicit policy for whitespace-only intervals.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    index: int = Field(ge=0, strict=True)
    start_char: int = Field(ge=0, le=100_000, strict=True)
    end_char: int = Field(ge=1, le=100_000, strict=True)
    text: str = Field(min_length=1, max_length=100_000)
    is_spoken: bool
    pace: NarrationPace
    emphasis: NarrationEmphasis | None = None
    rhythm: NarrationRhythm | None = None
    pause_after: NarrationPause | None = None
    cue_notes: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def represents_one_non_empty_copy_interval(self) -> "NarrationDeliverySegment":
        if self.end_char <= self.start_char:
            raise ValueError("Narration delivery segment range must be non-empty")
        if len(self.text) != self.end_char - self.start_char:
            raise ValueError("Narration delivery segment text must preserve its character interval")
        if self.is_spoken != bool(self.text.strip()):
            raise ValueError("Narration delivery segment spoken state must match its exact text")
        return self


class NarrationDeliveryPlan(ContractModel):
    """Deterministic speaker/take structure derived before any Voice call."""

    copy_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    performance_plan_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    opening_pause: NarrationPause | None = None
    segments: list[NarrationDeliverySegment] = Field(min_length=1, max_length=2_001)
    closing_pause: NarrationPause | None = None

    @model_validator(mode="after")
    def segments_are_contiguous(self) -> "NarrationDeliveryPlan":
        if [item.index for item in self.segments] != list(range(len(self.segments))):
            raise ValueError("Narration delivery segments must have contiguous order")
        if self.segments[0].start_char != 0:
            raise ValueError("Narration delivery plan must begin with the first copy character")
        if any(right.start_char != left.end_char for left, right in zip(self.segments, self.segments[1:])):
            raise ValueError("Narration delivery segments must be character-contiguous")
        return self


class ProjectDraft(ContractModel):
    project_id: UUID
    version: int = Field(default=0, ge=0, strict=True)
    script_revision: int = Field(default=0, ge=0, strict=True)
    script: str | None = Field(default=None, max_length=100_000)
    narration_performance_plan: NarrationPerformancePlan | None = None
    topic: str | None = Field(default=None, max_length=5_000)
    scenes: list[ScenePlan] = Field(default_factory=list, max_length=1_000)
    routes: list[DraftRoute] = Field(default_factory=list, max_length=1_000)
    confirmed: list[CandidateAsset] = Field(default_factory=list, max_length=1_000)
    video_spec: VideoSpec | None = None
    ip_profile_version: int | None = Field(default=None, gt=0)
    evidence_refs: list[str] = Field(default_factory=list, max_length=1_000)
    input_fingerprint: str | None = Field(default=None, min_length=16, max_length=128)
    invalidation_reasons: list[Literal["script_changed", "topic_changed", "scene_copy_changed", "ip_profile_changed"]] = Field(default_factory=list, max_length=4)
    updated_at: AwareDatetime


class ProjectDraftRevision(ContractModel):
    """Immutable, inspectable record of one persisted script/draft version."""

    project_id: UUID
    version: int = Field(gt=0, strict=True)
    script_revision: int = Field(ge=0, strict=True)
    script: str | None = Field(default=None, max_length=100_000)
    narration_performance_plan: NarrationPerformancePlan | None = None
    topic: str | None = Field(default=None, max_length=5_000)
    ip_profile_version: int | None = Field(default=None, gt=0)
    evidence_refs: list[str] = Field(default_factory=list, max_length=1_000)
    input_fingerprint: str | None = Field(default=None, min_length=16, max_length=128)
    invalidation_reasons: list[Literal["script_changed", "topic_changed", "scene_copy_changed", "ip_profile_changed"]] = Field(default_factory=list, max_length=4)
    created_at: AwareDatetime


class AssetUsageEvent(ContractModel):
    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    output_version: str = Field(min_length=1, max_length=200)
    event_key: str = Field(min_length=16, max_length=500)
    media_id: UUID
    media_kind: Literal["video", "image", "audio"]
    clip_id: UUID | None = None
    usage_kind: Literal["production"] = "production"
    used_at: AwareDatetime


class PublicationRecord(ContractModel):
    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    output_version: str = Field(min_length=1, max_length=200)
    platform: str = Field(min_length=1, max_length=100)
    published_at: AwareDatetime
    content_url: str | None = Field(default=None, max_length=2_000)
    content_external_id: str | None = Field(default=None, max_length=500)
    metrics: dict[str, JsonValue] = Field(default_factory=dict)
    metric_source: str | None = Field(default=None, max_length=200)
    observation_window_days: int | None = Field(default=None, ge=0, le=10_000)

    @model_validator(mode="after")
    def metrics_need_source(self) -> "PublicationRecord":
        if self.metrics and not self.metric_source:
            raise ValueError("metric_source is required when metrics are provided")
        return self


class ContentFeedback(ContractModel):
    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    output_version: str = Field(min_length=1, max_length=200)
    accepted: bool
    changed_fields: list[str] = Field(default_factory=list, max_length=100)
    rejection_reason: str | None = Field(default=None, max_length=2_000)
    notes: str | None = Field(default=None, max_length=10_000)
    created_at: AwareDatetime


class RenderVideoJobPayload(ContractModel):
    """All persistent input for one server-owned local render."""

    project_id: UUID
    render_id: UUID
    video_spec: VideoSpec

    @model_validator(mode="after")
    def belongs_to_project(self) -> "RenderVideoJobPayload":
        if self.video_spec.project_id != self.project_id:
            raise ValueError("render VideoSpec must belong to payload project_id")
        return self


class VoiceReferenceWindowSelection(ContractModel):
    """Stable authorized source window selected for one Voice generation job."""

    clip_id: UUID
    asset_id: UUID
    source_content_hash: str = Field(min_length=16, max_length=128)
    start_ms: NonNegativeMs
    end_ms: PositiveFrames
    transcript_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def valid_interval(self) -> "VoiceReferenceWindowSelection":
        if self.end_ms <= self.start_ms:
            raise ValueError("Voice reference window end_ms must exceed start_ms")
        return self


class VoiceGenerationJobPayload(ContractModel):
    """Credential-free input for one authorized, persisted voice request."""

    project_id: UUID
    voice_profile_id: UUID
    text: str = Field(min_length=1, max_length=100_000)
    delivery_text: str | None = Field(default=None, min_length=1, max_length=100_000)
    authorization_reference: str = Field(min_length=1, max_length=500)
    language: str | None = Field(default=None, pattern=r"^[a-z]{2,3}(-[A-Z]{2})?$")
    narration_performance_plan: NarrationPerformancePlan | None = None
    reference_window: VoiceReferenceWindowSelection | None = None
    expected_provider: str | None = None
    expected_model: str | None = None
    execution_capability_profile_id: UUID | None = None

    @model_validator(mode="after")
    def complete_execution_pin(self) -> "VoiceGenerationJobPayload":
        pins = (self.expected_provider, self.expected_model, self.execution_capability_profile_id)
        if any(value is not None for value in pins) and not all(value is not None for value in pins):
            raise ValueError("pinned Voice execution requires provider, model and capability profile")
        return self


class VoiceQaJobPayload(ContractModel):
    """Credential-free input for one local, real-ASR voice QA request."""

    project_id: UUID
    narration_audio_id: UUID
    target_text: str = Field(min_length=1, max_length=100_000)


class VoicePaceCandidateJobPayload(ContractModel):
    """One explicit, source-bound local whole-take pace operation."""

    project_id: UUID
    narration_audio_id: UUID
    source_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    profile: Literal["gentle_slower"]


class VoiceBoundaryAlignmentJobPayload(ContractModel):
    """Credential-free request to backfill observation evidence, never QA."""

    project_id: UUID
    narration_audio_id: UUID
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PlannedTalkingContext(ContractModel):
    """Execution-only identity for one admitted ProductionRun Talking scene."""

    run_id: UUID
    scene_plan_id: UUID
    source_admission_id: UUID
    capability_profile_id: UUID
    expected_provider: str = Field(min_length=1, max_length=100)
    expected_model: str = Field(min_length=1, max_length=200)
    expected_runtime: str = Field(min_length=1, max_length=200)
    expected_machine_id: str = Field(min_length=1, max_length=200)
    binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    consent_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class TalkingQaJobPayload(ContractModel):
    """One exact planned output, measured against its Master slice."""

    project_id: UUID
    run_id: UUID
    scene_plan_id: UUID
    generation_job_id: UUID
    output_asset_id: UUID
    output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    master_audio_id: UUID
    master_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    master_start_ms: int = Field(ge=0)
    master_end_ms: int = Field(ge=1)

    @model_validator(mode="after")
    def positive_interval(self) -> "TalkingQaJobPayload":
        if self.master_end_ms <= self.master_start_ms:
            raise ValueError("Talking QA interval must be positive")
        return self


class TalkingGenerationJobPayload(ContractModel):
    """Credential-free input for one authorized Talking/lip-sync request."""

    project_id: UUID
    talking_profile_id: UUID
    reference_clip_id: UUID
    narration_audio_id: UUID
    authorization_reference: str = Field(min_length=1, max_length=500)
    planned_context: PlannedTalkingContext | None = None
    terminal_face_closeout: bool = False
    terminal_delivery_end_ms: int | None = Field(default=None, ge=1)
    execution_parameters: dict[str, JsonValue] = Field(default_factory=dict, max_length=100)
    execution_parameter_sources: dict[str, str] = Field(default_factory=dict, max_length=100)
    execution_profile_reference: str | None = Field(default=None, min_length=1, max_length=500)
    slice_start_segment_index: int | None = Field(default=None, ge=0)
    slice_end_segment_index: int | None = Field(default=None, ge=1)
    slice_max_duration_ms: int | None = Field(default=None, ge=1)
    slice_limit_source: str | None = Field(default=None, min_length=1, max_length=100)
    reference_window_start_ms: int | None = Field(default=None, ge=0)
    reference_window_end_ms: int | None = Field(default=None, ge=1)
    slice_series_id: UUID | None = None
    slice_series_index: int | None = Field(default=None, ge=0)
    slice_series_size: int | None = Field(default=None, ge=1)

    @field_validator("execution_parameters")
    @classmethod
    def execution_parameters_are_scalar(cls, values: dict[str, JsonValue]) -> dict[str, JsonValue]:
        for key, value in values.items():
            if not key.strip() or not isinstance(value, (str, int, float, bool)) or isinstance(value, float) and not isfinite(value):
                raise ValueError("Talking execution parameters must be finite JSON scalars")
        return values

    @model_validator(mode="after")
    def terminal_options_are_explicit(self) -> "TalkingGenerationJobPayload":
        if not self.terminal_face_closeout and (
            self.terminal_delivery_end_ms is not None
            or self.execution_parameters
            or self.execution_parameter_sources
            or self.execution_profile_reference
        ):
            raise ValueError("Talking execution parameters require terminal face closeout intent")
        if self.terminal_face_closeout and self.terminal_delivery_end_ms is None:
            raise ValueError("Terminal face closeout requires a verified speech-end timestamp")
        if set(self.execution_parameter_sources) - set(self.execution_parameters):
            raise ValueError("Talking execution parameter sources must describe resolved parameters")
        if self.terminal_face_closeout and set(self.execution_parameter_sources) != set(self.execution_parameters):
            raise ValueError("Terminal face closeout requires provenance for every resolved parameter")
        slice_values = (self.slice_start_segment_index, self.slice_end_segment_index, self.slice_max_duration_ms, self.slice_limit_source)
        if any(value is not None for value in slice_values) and any(value is None for value in slice_values):
            raise ValueError("Talking slice requires indices, resolved maximum duration and provenance")
        if self.slice_start_segment_index is not None and self.slice_end_segment_index is not None and self.slice_end_segment_index <= self.slice_start_segment_index:
            raise ValueError("Talking slice end index must follow start index")
        if self.slice_start_segment_index is not None and self.terminal_face_closeout and self.slice_series_id is None:
            raise ValueError("Talking slice and terminal face closeout require separate execution planning")
        reference_window = (self.reference_window_start_ms, self.reference_window_end_ms)
        if any(value is not None for value in reference_window) and any(value is None for value in reference_window):
            raise ValueError("Talking reference window requires both start and end timestamps")
        if self.reference_window_start_ms is not None and self.reference_window_end_ms is not None and self.reference_window_end_ms <= self.reference_window_start_ms:
            raise ValueError("Talking reference window end must follow start")
        series_values = (self.slice_series_id, self.slice_series_index, self.slice_series_size)
        if any(value is not None for value in series_values) and any(value is None for value in series_values):
            raise ValueError("Talking slice series requires identifier, index and size")
        if self.slice_series_id is not None:
            if self.slice_start_segment_index is None:
                raise ValueError("Talking slice series requires a resolved Talking slice")
            if self.slice_series_index is not None and self.slice_series_size is not None and self.slice_series_index >= self.slice_series_size:
                raise ValueError("Talking slice series index must be below its size")
            if self.terminal_face_closeout and self.slice_series_index != self.slice_series_size - 1:
                raise ValueError("only the final Talking slice series child may request terminal face closeout")
        if self.planned_context is not None and (
            self.slice_start_segment_index is None or self.reference_window_start_ms is None
            or self.slice_series_id is not None or self.terminal_face_closeout
        ):
            raise ValueError("planned Talking requires one bounded source-forward slice without series/terminal options")
        return self


class TalkingSliceSeriesRecovery(ContractModel):
    """Immutable provenance for one explicitly replaced failed child."""

    series_index: int = Field(ge=0)
    failed_job_id: UUID
    replacement_job_id: UUID
    evidence_reference: str = Field(min_length=1, max_length=2_000)
    recovered_at: AwareDatetime


class PlannedTalkingRunOrigin(ContractModel):
    """Immutable evidence snapshot linking an existing planned child to a collection."""

    version: Literal[1] = 1
    run_id: UUID
    scene_plan_id: UUID
    binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    generation_job_id: UUID
    qa_job_id: UUID
    qa_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_asset_id: UUID
    output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    master_audio_id: UUID
    master_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    master_review_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    master_start_ms: int = Field(ge=0)
    master_end_ms: int = Field(ge=1)
    source_clip_id: UUID
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_start_ms: int = Field(ge=0)
    source_end_ms: int = Field(ge=1)
    source_admission_id: UUID
    consent_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization_reference: str = Field(min_length=1)
    license_evidence_reference: str = Field(min_length=1)
    child_review_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def intervals_are_positive(self) -> "PlannedTalkingRunOrigin":
        if self.master_end_ms <= self.master_start_ms or self.source_end_ms <= self.source_start_ms:
            raise ValueError("planned Talking origin intervals must be positive")
        return self


class PlannedTalkingRunOriginV2(PlannedTalkingRunOrigin):
    """Aggregate policy keeps child technical provenance without inventing approval."""

    version: Literal[2] = 2
    review_policy_version: Literal[2] = 2
    child_review_sha256: None = None
    execution_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class TalkingReviewDimensions(ContractModel):
    visible_sync: Literal["pass", "fail", "unknown"]
    identity: Literal["pass", "fail", "unknown"]
    artifacts: Literal["pass", "fail", "unknown"]
    source_performance: Literal["pass", "fail", "unknown"]
    continuity: Literal["pass", "fail", "unknown"]
    publishability: Literal["pass", "fail", "unknown"]


class TalkingReviewFinding(ContractModel):
    dimension: Literal["visible_sync", "identity", "artifacts", "source_performance", "continuity", "publishability"]
    reason: str = Field(min_length=1, max_length=2000)
    start_ms: NonNegativeMs | None = None
    end_ms: PositiveFrames | None = None

    @model_validator(mode="after")
    def complete_interval(self) -> "TalkingReviewFinding":
        if (self.start_ms is None) != (self.end_ms is None) or (
            self.start_ms is not None and self.end_ms <= self.start_ms
        ):
            raise ValueError("review finding requires a complete positive interval or whole-result scope")
        return self


class TalkingReviewConcern(ContractModel):
    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=500)
    series_id: UUID
    subject_asset_id: UUID
    subject_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    finding: TalkingReviewFinding
    evidence_reference: str = Field(min_length=1, max_length=2000)
    source: Literal["human_review"] = "human_review"
    created_at: AwareDatetime


class TalkingReviewConcernAnswer(ContractModel):
    concern_id: UUID
    approved: bool
    evidence_reference: str = Field(min_length=1, max_length=2000)
    reason: str = Field(min_length=1, max_length=2000)


class TalkingRunPreviewJobPayload(ContractModel):
    project_id: UUID
    series_id: UUID
    origin_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class TalkingSliceSeries(ContractModel):
    """Atomic parent record for an ordered set of short Talking jobs."""

    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=500)
    request_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    narration_audio_id: UUID
    child_job_ids: list[UUID] = Field(min_length=1, max_length=10_000)
    recovery_history: list[TalkingSliceSeriesRecovery] = Field(default_factory=list)
    planned_origin: PlannedTalkingRunOrigin | PlannedTalkingRunOriginV2 | None = None
    created_at: AwareDatetime

    @model_validator(mode="after")
    def planned_collection_is_one_immutable_child(self) -> "TalkingSliceSeries":
        if self.planned_origin is not None and (
            self.child_job_ids != [self.planned_origin.generation_job_id]
            or self.narration_audio_id != self.planned_origin.master_audio_id
            or self.recovery_history
        ):
            raise ValueError("planned Talking collection must contain exactly its original child")
        return self


class TalkingSliceSeriesContinuityReview(ContractModel):
    """Immutable human decision over the exact ordered series evidence."""

    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    series_id: UUID
    approved: bool
    evidence_reference: str = Field(min_length=1, max_length=2_000)
    findings: list[str] = Field(default_factory=list, max_length=100)
    child_job_ids: list[UUID] = Field(min_length=1, max_length=10_000)
    planned_origin_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    preview_asset_id: UUID | None = None
    preview_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    preview_qa_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reviewed_at: AwareDatetime
    review_policy_version: Literal[1, 2] = 1
    dimensions: TalkingReviewDimensions | None = None
    scoped_findings: list[TalkingReviewFinding] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def policy_scope_is_explicit(self) -> "TalkingSliceSeriesContinuityReview":
        if self.review_policy_version == 2:
            if self.dimensions is None or self.preview_asset_id is None:
                raise ValueError("aggregate review requires six dimensions and exact preview")
            if self.approved and any(value != "pass" for value in self.dimensions.model_dump(exclude={"schema_version"}).values()):
                raise ValueError("aggregate approval requires all six dimensions to pass")
            if not self.approved and not self.scoped_findings:
                raise ValueError("aggregate rejection requires scoped findings")
        elif self.dimensions is not None or self.scoped_findings:
            raise ValueError("legacy review cannot claim aggregate scope")
        return self

    @model_validator(mode="after")
    def planned_review_fields_are_complete(self) -> "TalkingSliceSeriesContinuityReview":
        fields = (self.planned_origin_sha256, self.preview_asset_id, self.preview_sha256, self.preview_qa_sha256)
        if any(value is not None for value in fields) and any(value is None for value in fields):
            raise ValueError("planned continuity review must bind the entire exact preview")
        return self


class TalkingRunChildEvidence(ContractModel):
    """One ordered provider child retained as execution evidence only."""

    series_index: int = Field(ge=0)
    job_id: UUID
    output_asset_id: UUID
    master_start_ms: NonNegativeMs
    master_end_ms: PositiveFrames
    reference_window_start_ms: NonNegativeMs
    reference_window_end_ms: PositiveFrames
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    provider_version: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def intervals_are_valid(self) -> "TalkingRunChildEvidence":
        if self.master_end_ms <= self.master_start_ms:
            raise ValueError("TalkingRun child master interval must be valid")
        if self.reference_window_end_ms <= self.reference_window_start_ms:
            raise ValueError("TalkingRun child reference interval must be valid")
        return self


class TalkingRun(ContractModel):
    """Admitted product-level creator Talking result, independent of child topology."""

    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    series_id: UUID
    master_narration_audio_id: UUID
    master_start_ms: NonNegativeMs
    master_end_ms: PositiveFrames
    authorized_reference_clip_id: UUID
    child_evidence: list[TalkingRunChildEvidence] = Field(min_length=1, max_length=10_000)
    continuity_review_id: UUID
    assembled_asset_id: UUID
    assembled_clip_id: UUID
    automated_qa_state: Literal["verified"]
    admission_state: Literal["admitted"]
    planned_origin_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reviewed_preview_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    created_at: AwareDatetime
    review_policy_version: Literal[1, 2] = 1

    @model_validator(mode="after")
    def planned_run_fields_are_complete(self) -> "TalkingRun":
        if (self.planned_origin_sha256 is None) != (self.reviewed_preview_sha256 is None):
            raise ValueError("planned TalkingRun requires origin and reviewed preview hashes together")
        return self

    @model_validator(mode="after")
    def admitted_run_is_contiguous(self) -> "TalkingRun":
        ordered = sorted(self.child_evidence, key=lambda child: child.series_index)
        if [child.series_index for child in ordered] != list(range(len(ordered))):
            raise ValueError("TalkingRun children must have contiguous order")
        if ordered[0].master_start_ms != self.master_start_ms or ordered[-1].master_end_ms != self.master_end_ms:
            raise ValueError("TalkingRun interval must match its ordered child evidence")
        if any(left.master_end_ms > right.master_start_ms for left, right in zip(ordered, ordered[1:])):
            raise ValueError("TalkingRun child master intervals may not overlap")
        return self


class Job(ContractModel):
    id: UUID = Field(default_factory=uuid4)
    project_id: UUID | None = None
    type: JobType
    status: JobStatus = JobStatus.PENDING
    attempt: int = Field(default=0, ge=0, le=100)
    idempotency_key: str = Field(min_length=1, max_length=500)
    created_at: AwareDatetime
    updated_at: AwareDatetime
    error_code: str | None = Field(default=None, max_length=100)
    error_message: str | None = Field(default=None, max_length=2_000)
    payload: RenderVideoJobPayload | VoiceGenerationJobPayload | VoicePaceCandidateJobPayload | VoiceQaJobPayload | VoiceBoundaryAlignmentJobPayload | TalkingGenerationJobPayload | TalkingQaJobPayload | TalkingRunPreviewJobPayload | None = None

    @model_validator(mode="after")
    def validates_typed_payload(self) -> "Job":
        if self.type is JobType.RENDER:
            # Legacy generic Job construction remains valid so target stores
            # can reject RENDER as an unsupported asset job. The render API
            # always persists this typed payload, and the handler rejects a
            # missing payload before side effects.
            if self.payload is not None and self.project_id != self.payload.project_id:
                raise ValueError("render job project_id must match its payload")
            if self.payload is not None and not isinstance(self.payload, RenderVideoJobPayload):
                raise ValueError("render job payload must be a RenderVideoJobPayload")
        elif self.type is JobType.GENERATE_VOICE:
            if self.payload is not None and not isinstance(self.payload, VoiceGenerationJobPayload):
                raise ValueError("voice generation job payload must be a VoiceGenerationJobPayload")
            if self.payload is not None and self.project_id != self.payload.project_id:
                raise ValueError("voice generation job project_id must match its payload")
        elif self.type is JobType.VERIFY_VOICE:
            if self.payload is not None and not isinstance(self.payload, VoiceQaJobPayload):
                raise ValueError("voice QA job payload must be a VoiceQaJobPayload")
            if self.payload is not None and self.project_id != self.payload.project_id:
                raise ValueError("voice QA job project_id must match its payload")
        elif self.type is JobType.CREATE_VOICE_PACE_CANDIDATE:
            if not isinstance(self.payload, VoicePaceCandidateJobPayload):
                raise ValueError("voice pace job payload must be a VoicePaceCandidateJobPayload")
            if self.project_id != self.payload.project_id:
                raise ValueError("voice pace job project_id must match its payload")
        elif self.type is JobType.ALIGN_VOICE_BOUNDARIES:
            if not isinstance(self.payload, VoiceBoundaryAlignmentJobPayload):
                raise ValueError("voice boundary job payload must be a VoiceBoundaryAlignmentJobPayload")
            if self.project_id != self.payload.project_id:
                raise ValueError("voice boundary job project_id must match its payload")
        elif self.type is JobType.GENERATE_TALKING:
            if self.payload is not None and not isinstance(self.payload, TalkingGenerationJobPayload):
                raise ValueError("talking generation job payload must be a TalkingGenerationJobPayload")
            if self.payload is not None and self.project_id != self.payload.project_id:
                raise ValueError("talking generation job project_id must match its payload")
        elif self.type is JobType.VERIFY_TALKING:
            if not isinstance(self.payload, TalkingQaJobPayload):
                raise ValueError("talking QA job payload must be a TalkingQaJobPayload")
            if self.project_id != self.payload.project_id:
                raise ValueError("talking QA job project_id must match its payload")
        elif self.type is JobType.PREPARE_TALKING_RUN_PREVIEW:
            if not isinstance(self.payload, TalkingRunPreviewJobPayload) or self.project_id != self.payload.project_id:
                raise ValueError("Talking preview job requires matching typed project payload")
        elif self.payload is not None:
            raise ValueError("only declared typed jobs may contain a payload")
        return self
