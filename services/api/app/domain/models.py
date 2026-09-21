"""Provider-agnostic, versioned domain contracts for Content OS."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from math import isfinite
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

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
    VERIFY_VOICE = "verify_voice"
    GENERATE_TALKING = "generate_talking"
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


class ScenePlan(ContractModel):
    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    scene_id: str = Field(min_length=1, max_length=100)
    order: int = Field(ge=0, le=10_000)
    purpose: str = Field(min_length=1, max_length=100)
    voice_text: str = Field(min_length=1, max_length=10_000)
    duration_target_ms: PositiveFrames
    visual_intent: VisualIntent
    preferred_sources: list[SourceKind] = Field(min_length=1, max_length=10)
    fallback_sources: list[SourceKind] = Field(default_factory=list, max_length=10)
    caption_emphasis: list[str] = Field(default_factory=list, max_length=30)
    evidence_refs: list[str] = Field(default_factory=list, max_length=100, description="Traceable IP/material evidence identifiers.")


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

    @model_validator(mode="after")
    def narration_interval_is_complete(self) -> "VideoScene":
        if (self.narration_start_ms is None) != (self.narration_end_ms is None):
            raise ValueError("narration start/end must be supplied together")
        if self.narration_start_ms is not None and self.narration_end_ms <= self.narration_start_ms:
            raise ValueError("narration interval must be valid")
        if self.narration_asset_id is None and self.narration_start_ms is not None:
            raise ValueError("narration interval requires narration_asset_id")
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
        return self


class ProjectDraft(ContractModel):
    project_id: UUID
    version: int = Field(default=0, ge=0, strict=True)
    script_revision: int = Field(default=0, ge=0, strict=True)
    script: str | None = Field(default=None, max_length=100_000)
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


class VoiceGenerationJobPayload(ContractModel):
    """Credential-free input for one authorized, persisted voice request."""

    project_id: UUID
    voice_profile_id: UUID
    text: str = Field(min_length=1, max_length=100_000)
    authorization_reference: str = Field(min_length=1, max_length=500)
    language: str | None = Field(default=None, pattern=r"^[a-z]{2,3}(-[A-Z]{2})?$")


class VoiceQaJobPayload(ContractModel):
    """Credential-free input for one local, real-ASR voice QA request."""

    project_id: UUID
    narration_audio_id: UUID
    target_text: str = Field(min_length=1, max_length=100_000)


class TalkingGenerationJobPayload(ContractModel):
    """Credential-free input for one authorized Talking/lip-sync request."""

    project_id: UUID
    talking_profile_id: UUID
    reference_clip_id: UUID
    narration_audio_id: UUID
    authorization_reference: str = Field(min_length=1, max_length=500)
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
        if self.slice_start_segment_index is not None and self.terminal_face_closeout:
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
        return self


class TalkingSliceSeries(ContractModel):
    """Atomic parent record for an ordered set of short Talking jobs."""

    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=500)
    request_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    narration_audio_id: UUID
    child_job_ids: list[UUID] = Field(min_length=1, max_length=10_000)
    created_at: AwareDatetime


class TalkingSliceSeriesContinuityReview(ContractModel):
    """Immutable human decision over the exact ordered series evidence."""

    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    series_id: UUID
    approved: bool
    evidence_reference: str = Field(min_length=1, max_length=2_000)
    findings: list[str] = Field(default_factory=list, max_length=100)
    child_job_ids: list[UUID] = Field(min_length=1, max_length=10_000)
    reviewed_at: AwareDatetime


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
    payload: RenderVideoJobPayload | VoiceGenerationJobPayload | VoiceQaJobPayload | TalkingGenerationJobPayload | None = None

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
        elif self.type is JobType.GENERATE_TALKING:
            if self.payload is not None and not isinstance(self.payload, TalkingGenerationJobPayload):
                raise ValueError("talking generation job payload must be a TalkingGenerationJobPayload")
            if self.payload is not None and self.project_id != self.payload.project_id:
                raise ValueError("talking generation job project_id must match its payload")
        elif self.payload is not None:
            raise ValueError("only render, voice generation and talking generation jobs may contain a payload")
        return self
