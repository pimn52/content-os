"""Provider-neutral contracts for authorized creator Talking generation."""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from pathlib import Path
from typing import Mapping, Protocol
from uuid import UUID

from app.domain.models import AudioAsset, TalkingProfile


class TalkingError(RuntimeError):
    """Base class for expected Talking-provider failures without secrets."""


class TalkingConfigurationError(TalkingError):
    pass


class TalkingInputError(TalkingError):
    pass


class TalkingTimeout(TalkingError):
    pass


class TalkingConnectionError(TalkingError):
    pass


class TalkingAuthenticationError(TalkingError):
    pass


class TalkingRateLimitError(TalkingError):
    pass


class TalkingProviderResponseError(TalkingError):
    pass


@dataclass(frozen=True)
class TalkingSynthesisResult:
    video_path: Path
    provider_version: str | None = None


@dataclass(frozen=True)
class TalkingExecutionOptions:
    """Provider-neutral Talking intent plus adapter-owned resolved values."""

    terminal_face_closeout: bool = False
    terminal_delivery_end_ms: int | None = None
    provider_parameters: Mapping[str, str | int | float | bool] = field(default_factory=dict)
    parameter_sources: Mapping[str, str] = field(default_factory=dict)
    profile_reference: str | None = None

    def __post_init__(self) -> None:
        if self.terminal_delivery_end_ms is not None and (
            isinstance(self.terminal_delivery_end_ms, bool)
            or not isinstance(self.terminal_delivery_end_ms, int)
            or self.terminal_delivery_end_ms <= 0
        ):
            raise TalkingInputError("Talking terminal delivery end must be a positive millisecond timestamp")
        if not self.terminal_face_closeout and self.terminal_delivery_end_ms is not None:
            raise TalkingInputError("Talking terminal delivery end requires terminal face closeout intent")
        object.__setattr__(self, "provider_parameters", dict(self.provider_parameters))
        object.__setattr__(self, "parameter_sources", dict(self.parameter_sources))


@dataclass(frozen=True)
class TalkingReference:
    clip_id: UUID
    source_path: Path
    start_ms: int
    end_ms: int
    subtitle_crop_bottom_ratio: float = 0

    def __post_init__(self) -> None:
        if not self.source_path.is_file():
            raise TalkingInputError("Talking reference source file is unavailable")
        if self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise TalkingInputError("Talking reference interval is invalid")
        ratio = self.subtitle_crop_bottom_ratio
        if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not math.isfinite(float(ratio)) or not 0 <= float(ratio) <= 0.4:
            raise TalkingInputError("Talking reference subtitle crop ratio is invalid")


class TalkingHeadProvider(Protocol):
    provider_name: str
    model: str

    def synthesize(
        self,
        profile: TalkingProfile,
        narration: AudioAsset,
        reference: TalkingReference,
        output_path: str | Path,
        options: TalkingExecutionOptions | None = None,
    ) -> TalkingSynthesisResult: ...
