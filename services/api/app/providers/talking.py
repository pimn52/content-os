"""Provider-neutral contracts for authorized creator Talking generation."""
from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import subprocess
import sys
from typing import Callable, Protocol
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
    """One provider-produced local video file, never a provider URL or secret."""

    video_path: Path
    provider_version: str | None = None


@dataclass(frozen=True)
class TalkingReference:
    """The exact authorized source interval chosen before Talking inference."""

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
        if isinstance(self.subtitle_crop_bottom_ratio, bool) or not isinstance(self.subtitle_crop_bottom_ratio, (int, float)) or not math.isfinite(float(self.subtitle_crop_bottom_ratio)) or not 0 <= float(self.subtitle_crop_bottom_ratio) <= 0.4:
            raise TalkingInputError("Talking reference subtitle crop ratio is invalid")


class MuseTalkLocalRunner:
    """Invoke the isolated MuseTalk bridge for one selected local interval.

    The runner is opt-in: callers must supply all local runtime paths.  It
    neither discovers credentials nor downloads weights, and it keeps the
    model environment outside Content OS's normal dependencies.
    """

    def __init__(
        self,
        *,
        bridge_script: str | Path,
        runtime_python: str | Path,
        musetalk_root: str | Path,
        models_root: str | Path,
        ffmpeg_dir: str | Path,
        batch_size: int = 1,
        timeout_seconds: int = 1_800,
    ) -> None:
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
            raise TalkingConfigurationError("MuseTalk batch size must be positive")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or timeout_seconds < 1:
            raise TalkingConfigurationError("MuseTalk timeout must be positive")
        self.bridge_script = Path(bridge_script).resolve()
        self.runtime_python = Path(runtime_python).resolve()
        self.musetalk_root = Path(musetalk_root).resolve()
        self.models_root = Path(models_root).resolve()
        self.ffmpeg_dir = Path(ffmpeg_dir).resolve()
        self.batch_size = batch_size
        self.timeout_seconds = timeout_seconds

    def __call__(self, _: TalkingProfile, narration: AudioAsset, reference: TalkingReference, target: Path) -> Path:
        if not self.bridge_script.is_file() or not self.runtime_python.is_file():
            raise TalkingConfigurationError("MuseTalk local runner is not configured")
        command = [
            sys.executable, str(self.bridge_script),
            "--runtime-python", str(self.runtime_python),
            "--musetalk-root", str(self.musetalk_root),
            "--models-root", str(self.models_root),
            "--ffmpeg-dir", str(self.ffmpeg_dir),
            "--video", str(reference.source_path),
            "--clip-start-ms", str(reference.start_ms),
            "--clip-end-ms", str(reference.end_ms),
            "--subtitle-crop-bottom-ratio", f"{reference.subtitle_crop_bottom_ratio:g}",
            "--audio", narration.source_file,
            "--output", str(target),
            "--batch-size", str(self.batch_size),
            "--timeout-seconds", str(self.timeout_seconds),
        ]
        try:
            completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=self.timeout_seconds + 180, check=False)
        except subprocess.TimeoutExpired as exc:
            raise TalkingTimeout("isolated MuseTalk bridge timed out") from exc
        except OSError as exc:
            raise TalkingConnectionError("isolated MuseTalk bridge could not start") from exc
        if completed.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
            raise TalkingProviderResponseError("isolated MuseTalk bridge did not produce a local video")
        return target


class TalkingHeadProvider(Protocol):
    provider_name: str
    model: str

    def synthesize(self, profile: TalkingProfile, narration: AudioAsset, reference: TalkingReference, output_path: str | Path) -> TalkingSynthesisResult: ...


class MuseTalkProvider:
    """Optional local MuseTalk adapter with no bundled runtime or weights.

    MuseTalk is intentionally reached through an injected local runner.  The
    provider-neutral contract retains only a local result path and never
    imports model packages, discovers credentials, or treats a model license
    as a Core concern.
    """

    provider_name = "musetalk"
    is_local = True

    def __init__(
        self,
        model: str = "1.5",
        *,
        synthesizer: Callable[[TalkingProfile, AudioAsset, TalkingReference, Path], object] | None = None,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise TalkingConfigurationError("MuseTalk model must not be empty")
        self.model = model.strip()
        self._synthesizer = synthesizer

    def synthesize(self, profile: TalkingProfile, narration: AudioAsset, reference: TalkingReference, output_path: str | Path) -> TalkingSynthesisResult:
        if not isinstance(profile, TalkingProfile) or not profile.consent.confirmed:
            raise TalkingInputError("Talking synthesis requires an explicitly consented TalkingProfile")
        if not isinstance(narration, AudioAsset) or not Path(narration.source_file).is_file():
            raise TalkingInputError("Talking synthesis requires a local narration audio asset")
        if not isinstance(reference, TalkingReference):
            raise TalkingInputError("Talking synthesis requires one selected local reference")
        if not isinstance(output_path, (str, Path)):
            raise TalkingInputError("Talking output path is invalid")
        target = Path(output_path)
        if target.suffix.lower() not in {".mp4", ".mov", ".mkv", ".webm"}:
            raise TalkingInputError("Talking output path must include a video extension")
        if self._synthesizer is None:
            raise TalkingConfigurationError(
                "MuseTalk is an optional local evaluation provider; install and configure its isolated runtime before an explicit evaluation job"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            produced = self._synthesizer(profile, narration, reference, target)
        except TalkingError:
            raise
        except TimeoutError as exc:
            raise TalkingTimeout("local MuseTalk inference timed out") from exc
        except OSError as exc:
            raise TalkingConnectionError("local MuseTalk inference could not access its runtime") from exc
        except Exception:
            raise TalkingProviderResponseError("local MuseTalk inference failed") from None
        path = Path(produced) if isinstance(produced, (str, Path)) else target
        if not path.is_file() or path.stat().st_size == 0:
            raise TalkingProviderResponseError("Talking provider did not produce a playable local video file")
        return TalkingSynthesisResult(path)
