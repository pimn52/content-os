"""Optional local LatentSync 1.5 Talking adapter.

The adapter owns the model-specific command line and the small amount of
media staging LatentSync needs.  Core receives only a provider-neutral
``TalkingHeadProvider`` result.  No model package is imported here, no weights
are downloaded, and the official non-commercial benchmark weights must remain
outside a commercial release.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import math
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
from typing import Callable, Sequence

from app.domain.models import AudioAsset, CostCategory, TalkingProfile, UsageCost

from .talking import (
    TalkingConfigurationError,
    TalkingInputError,
    TalkingProviderResponseError,
    TalkingReference,
    TalkingSynthesisResult,
    TalkingTimeout,
)


LATENTSYNC_PROVIDER = "latentsync"
LATENTSYNC_MODEL = "LatentSync-1.5"
LATENTSYNC_PROCESSING_RESOLUTION_PX = 256
LATENTSYNC_STATED_MINIMUM_VRAM_GB = 6.5


@dataclass(frozen=True)
class LatentSyncRuntimeMetadata:
    """Safe, UI-readable capability and cost facts for this local adapter."""

    provider: str
    model: str
    processing_resolution_px: int
    stated_minimum_vram_gb: float
    estimated_cost: UsageCost


CommandRunner = Callable[[Sequence[str], Path, float], subprocess.CompletedProcess[str]]


def _command_parts(command: str | Path | Sequence[str]) -> tuple[str, ...]:
    if isinstance(command, (str, Path)):
        parts = (str(command),)
    else:
        parts = tuple(str(item) for item in command)
    if not parts or not parts[0].strip():
        raise TalkingConfigurationError("LatentSync command must not be empty")
    return parts


def _positive_float(value: float, label: str) -> float:
    if isinstance(value, bool):
        raise TalkingConfigurationError(f"LatentSync {label} must be finite and positive")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise TalkingConfigurationError(f"LatentSync {label} must be finite and positive") from exc
    if not math.isfinite(result) or result <= 0:
        raise TalkingConfigurationError(f"LatentSync {label} must be finite and positive")
    return result


class LatentSyncProvider:
    """Run the pinned LatentSync release through its official CLI.

    ``command_runner`` is injectable for contract tests.  Runtime execution
    uses ``subprocess.run`` with ``shell=False`` and bounded output capture;
    command output is intentionally never copied into application errors.
    """

    provider_name = LATENTSYNC_PROVIDER
    is_local = True
    noncommercial_benchmark_only = True
    processing_resolution_px = LATENTSYNC_PROCESSING_RESOLUTION_PX
    stated_minimum_vram_gb = LATENTSYNC_STATED_MINIMUM_VRAM_GB

    def __init__(
        self,
        python_executable: str | Path,
        repo_root: str | Path,
        checkpoint_path: str | Path,
        *,
        model: str = LATENTSYNC_MODEL,
        runner_path: str | Path | None = None,
        unet_config_path: str | Path | None = None,
        ffmpeg_command: str | Path | Sequence[str] = "ffmpeg",
        inference_steps: int = 20,
        guidance_scale: float = 1.5,
        seed: int = 1247,
        timeout_seconds: float = 900.0,
        command_runner: CommandRunner | None = None,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise TalkingConfigurationError("LatentSync model must not be empty")
        self.model = model.strip()
        self.python_executable = str(python_executable)
        self.repo_root = Path(repo_root).resolve()
        self.checkpoint_path = Path(checkpoint_path).resolve()
        self.runner_path = Path(runner_path or self.repo_root / "scripts" / "inference.py").resolve()
        self.unet_config_path = Path(
            unet_config_path or self.repo_root / "configs" / "unet" / "stage2.yaml"
        ).resolve()
        self.ffmpeg_command = _command_parts(ffmpeg_command)
        if not self._executable_available(self.python_executable):
            raise TalkingConfigurationError("LatentSync Python runtime is unavailable")
        if not self.repo_root.is_dir():
            raise TalkingConfigurationError("LatentSync repository is unavailable")
        for label, path in (
            ("checkpoint", self.checkpoint_path),
            ("runner", self.runner_path),
            ("U-Net config", self.unet_config_path),
        ):
            if not path.is_file():
                raise TalkingConfigurationError(f"LatentSync {label} is unavailable")
        if isinstance(inference_steps, bool) or not isinstance(inference_steps, int) or not 1 <= inference_steps <= 1_000:
            raise TalkingConfigurationError("LatentSync inference_steps must be an integer from 1 to 1000")
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise TalkingConfigurationError("LatentSync seed must be an integer")
        self.inference_steps = inference_steps
        self.guidance_scale = _positive_float(guidance_scale, "guidance_scale")
        self.seed = seed
        self.timeout_seconds = _positive_float(timeout_seconds, "timeout_seconds")
        self._command_runner = command_runner or self._run_command

    @staticmethod
    def _executable_available(executable: str) -> bool:
        return Path(executable).is_file() or shutil.which(executable) is not None

    @staticmethod
    def _run_command(
        argv: Sequence[str], cwd: Path, timeout_seconds: float
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(argv),
            cwd=cwd,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )

    @property
    def runtime_metadata(self) -> LatentSyncRuntimeMetadata:
        return LatentSyncRuntimeMetadata(
            provider=self.provider_name,
            model=self.model,
            processing_resolution_px=self.processing_resolution_px,
            stated_minimum_vram_gb=self.stated_minimum_vram_gb,
            estimated_cost=UsageCost(
                category=CostCategory.TALKING,
                amount=Decimal("0"),
                currency="USD",
                provider=self.provider_name,
                note="local LatentSync inference; no external provider charge",
            ),
        )

    def synthesize(
        self,
        profile: TalkingProfile,
        narration: AudioAsset,
        reference: TalkingReference,
        output_path: str | Path,
    ) -> TalkingSynthesisResult:
        if not isinstance(profile, TalkingProfile) or not profile.consent.confirmed:
            raise TalkingInputError("LatentSync requires an explicitly consented TalkingProfile")
        if not isinstance(narration, AudioAsset):
            raise TalkingInputError("LatentSync requires a typed narration asset")
        narration_path = self._resolve_input_path(narration.source_file)
        if not narration_path.is_file() or narration_path.stat().st_size == 0:
            raise TalkingInputError("LatentSync narration audio is unavailable")
        if not isinstance(reference, TalkingReference):
            raise TalkingInputError("LatentSync requires a typed Talking reference")
        target = Path(output_path).resolve()
        if target.suffix.lower() != ".mp4":
            raise TalkingInputError("LatentSync output path must use the .mp4 extension")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.unlink(missing_ok=True)

        duration_ms = reference.end_ms - reference.start_ms
        with TemporaryDirectory(prefix="latentsync-talking-", dir=self.repo_root) as temporary:
            temporary_root = Path(temporary)
            staged = Path(temporary) / "reference.mp4"
            runtime_audio = temporary_root / "narration-model.wav"
            model_duration_ms = self._model_duration_ms(narration.duration_ms)
            # A Core Clip may be much longer than the new take.  LatentSync
            # aligns video frames to the driving audio, so sending the whole
            # ordinary Clip here can make the local runner process an
            # unrelated tail or fail before inference.  The Clip remains the
            # source of truth; only this provider boundary stages the window.
            self._stage_reference(reference, staged, duration_ms, model_duration_ms)
            self._prepare_model_audio(narration_path, runtime_audio, narration.duration_ms)
            runtime_output = temporary_root / "generated.mp4"
            self._run_inference(staged, runtime_audio, runtime_output)
            normalized_output = temporary_root / "normalized.mp4"
            self._normalize_output(runtime_output, narration_path, normalized_output, narration.duration_ms)
            if not normalized_output.is_file() or normalized_output.stat().st_size == 0:
                raise TalkingProviderResponseError("LatentSync did not produce a local video file")
            shutil.copyfile(normalized_output, target)

        if not target.is_file() or target.stat().st_size == 0:
            raise TalkingProviderResponseError("LatentSync did not produce a local video file")
        return TalkingSynthesisResult(target, provider_version="1.5")

    @staticmethod
    def _model_duration_ms(duration_ms: int) -> int:
        required_frames = (duration_ms * 25 + 999) // 1_000
        return ((required_frames + 15) // 16) * 16 * 1_000 // 25

    def _resolve_input_path(self, value: str | Path) -> Path:
        """Resolve Core's repo-relative media paths before entering the worker.

        Core intentionally stores portable paths such as
        ``content-os-data/assets/...``.  The pinned LatentSync repository is a
        subdirectory of that data root, and its legacy FFmpeg helper executes
        from there; passing the portable path unchanged would resolve it as
        ``content-os-data/latentsync/content-os-data/...``.  Resolve at the
        provider boundary and keep the Core contract portable.
        """

        path = Path(value)
        if path.is_absolute():
            return path.resolve()
        candidates = (
            (Path.cwd() / path).resolve(),
            (self.repo_root.parent.parent / path).resolve(),
        )
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return candidates[0]

    def _stage_reference(
        self, reference: TalkingReference, staged: Path, duration_ms: int, target_duration_ms: int
    ) -> None:
        argv = [
            *self.ffmpeg_command,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{reference.start_ms / 1000:.3f}",
            "-i",
            str(self._resolve_input_path(reference.source_path)),
            "-t",
            f"{min(duration_ms, target_duration_ms) / 1000:.3f}",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            str(staged),
        ]
        self._execute(argv, "reference preparation")
        if not staged.is_file() or staged.stat().st_size == 0:
            raise TalkingProviderResponseError("LatentSync reference preparation produced no video")

    def _prepare_model_audio(self, source: Path, target: Path, duration_ms: int) -> None:
        """Pad audio to LatentSync's 16-frame inference window boundary."""
        model_duration_ms = self._model_duration_ms(duration_ms)
        pad_seconds = max(0.0, (model_duration_ms - duration_ms) / 1_000)
        argv = [
            *self.ffmpeg_command,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-af",
            f"apad=pad_dur={pad_seconds:.3f}",
            "-t",
            f"{model_duration_ms / 1_000:.3f}",
            "-ar",
            "16000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(target),
        ]
        self._execute(argv, "narration preparation")
        if not target.is_file() or target.stat().st_size == 0:
            raise TalkingProviderResponseError("LatentSync narration preparation produced no audio")

    def _normalize_output(self, generated: Path, narration: Path, target: Path, duration_ms: int) -> None:
        """Restore the exact narration duration and use the original audio."""
        if not generated.is_file() or generated.stat().st_size == 0:
            raise TalkingProviderResponseError("LatentSync did not produce a local video file")
        argv = [
            *self.ffmpeg_command,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(generated),
            "-i",
            str(narration),
            "-filter_complex",
            f"[0:v]tpad=stop_mode=clone:stop_duration={duration_ms / 1_000:.3f}[v]",
            "-map",
            "[v]",
            "-map",
            "1:a:0",
            "-t",
            f"{duration_ms / 1_000:.3f}",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(target),
        ]
        self._execute(argv, "output duration normalization")

    def _run_inference(self, video_path: Path, audio_path: Path, target: Path) -> None:
        def runtime_argument(path: Path) -> str:
            try:
                return str(path.relative_to(self.repo_root))
            except ValueError:
                return str(path)

        argv = [
            self.python_executable,
            str(self.runner_path),
            "--unet_config_path",
            str(self.unet_config_path),
            "--inference_ckpt_path",
            str(self.checkpoint_path),
            "--video_path",
            runtime_argument(video_path),
            "--audio_path",
            runtime_argument(audio_path),
            "--video_out_path",
            runtime_argument(target),
            "--inference_steps",
            str(self.inference_steps),
            "--guidance_scale",
            f"{self.guidance_scale:g}",
            "--seed",
            str(self.seed),
        ]
        self._execute(argv, "LatentSync inference")

    def _execute(self, argv: Sequence[str], operation: str) -> None:
        try:
            completed = self._command_runner(argv, self.repo_root, self.timeout_seconds)
        except FileNotFoundError as exc:
            raise TalkingConfigurationError("LatentSync runtime executable is unavailable") from exc
        except subprocess.TimeoutExpired as exc:
            raise TalkingTimeout(f"LatentSync {operation} timed out") from exc
        except TimeoutError as exc:
            raise TalkingTimeout(f"LatentSync {operation} timed out") from exc
        except OSError as exc:
            raise TalkingConfigurationError("LatentSync runtime could not be started") from exc
        if completed.returncode != 0:
            raise TalkingProviderResponseError(f"LatentSync {operation} failed")


__all__ = [
    "LATENTSYNC_MODEL",
    "LATENTSYNC_PROCESSING_RESOLUTION_PX",
    "LATENTSYNC_PROVIDER",
    "LATENTSYNC_STATED_MINIMUM_VRAM_GB",
    "LatentSyncProvider",
    "LatentSyncRuntimeMetadata",
]
