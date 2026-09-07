"""Provider-agnostic, versioned domain contracts for Content OS."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
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
    vocabulary: list[str] = Field(default_factory=list, max_length=200)
    avoided_expressions: list[str] = Field(default_factory=list, max_length=200)
    common_hooks: list[str] = Field(default_factory=list, max_length=100)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


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


class Clip(ContractModel):
    id: UUID = Field(default_factory=uuid4)
    asset_id: UUID
    start_ms: NonNegativeMs
    end_ms: NonNegativeMs
    asset_duration_ms: PositiveFrames
    transcript: str | None = Field(default=None, max_length=100_000)
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
        existing_media = {SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET, SourceKind.SCREENSHOT, SourceKind.CHART}
        has_reference = self.asset_id is not None or self.clip_id is not None
        if self.source_kind == SourceKind.CAPTURE and not self.requires_capture:
            raise ValueError("capture candidates must require_capture")
        if self.source_kind != SourceKind.CAPTURE and self.requires_capture:
            raise ValueError("requires_capture is only valid for capture candidates")
        if self.source_kind in existing_media and not has_reference:
            raise ValueError("existing-media candidates need an asset_id or clip_id")
        return self


class VideoVisual(ContractModel):
    source_kind: SourceKind
    authorization_reference: str = Field(min_length=1, max_length=500, description="Reference to the source rights record; never credentials.")
    asset_id: UUID | None = None
    clip_id: UUID | None = None
    clip_start_ms: NonNegativeMs | None = None
    clip_end_ms: NonNegativeMs | None = None
    source_duration_ms: PositiveFrames | None = None

    @model_validator(mode="after")
    def visual_source_is_complete(self) -> "VideoVisual":
        existing_media = {SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET, SourceKind.SCREENSHOT, SourceKind.CHART}
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
        return self


class VideoScene(ContractModel):
    scene_id: str = Field(min_length=1, max_length=100)
    start_frame: int = Field(ge=0, strict=True)
    duration_frames: PositiveFrames
    visual: VideoVisual
    narration_asset_id: UUID | None = None
    caption: str | None = Field(default=None, max_length=10_000)
    transition: str = Field(default="cut", max_length=100)


class VideoSpec(ContractModel):
    project_id: UUID
    format: ProjectFormat
    width: int = Field(gt=0, le=16_384)
    height: int = Field(gt=0, le=16_384)
    fps: RationalFps
    scenes: list[VideoScene] = Field(min_length=1, max_length=1_000)
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
