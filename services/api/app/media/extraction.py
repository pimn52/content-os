"""FFmpeg-backed derived media extraction.

This module deliberately has no database or provider dependency: callers pass
validated Asset/Clip contracts and receive paths to deterministic local files.
"""
from __future__ import annotations

import math
import os
import subprocess
import tempfile
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from app.domain.models import Asset, Clip


class ExtractionError(RuntimeError):
    """Base class for expected extraction failures."""


class ExtractionBinaryMissing(ExtractionError):
    pass


class ExtractionTimeout(ExtractionError):
    pass


class ExtractionProcessError(ExtractionError):
    pass


class ExtractionOutputError(ExtractionError):
    pass


class AudioUnavailableError(ExtractionError):
    pass


class ExtractionValidationError(ExtractionError):
    pass


@dataclass(frozen=True)
class AudioExtraction:
    path: Path


@dataclass(frozen=True)
class KeyframeExtraction:
    path: Path
    timestamp_ms: Decimal


class MediaExtractor:
    """Extract analysis audio and representative keyframes without mutation."""

    def __init__(
        self,
        data_root: str | Path,
        command: str | Sequence[str] | None = None,
        timeout_seconds: float = 60.0,
        sample_rate: int = 16_000,
        channels: int = 1,
        image_width: int | None = None,
        image_height: int | None = None,
        image_quality: int = 2,
    ) -> None:
        if isinstance(timeout_seconds, bool):
            raise ValueError("timeout_seconds must be finite and positive")
        try:
            timeout = float(timeout_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError("timeout_seconds must be finite and positive") from exc
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        if isinstance(sample_rate, bool) or not isinstance(sample_rate, int) or not 1_000 <= sample_rate <= 192_000:
            raise ValueError("sample_rate must be an integer between 1000 and 192000")
        if isinstance(channels, bool) or not isinstance(channels, int) or not 1 <= channels <= 8:
            raise ValueError("channels must be an integer from 1 to 8")
        for name, value in (("image_width", image_width), ("image_height", image_height)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 16_384):
                raise ValueError(f"{name} must be a positive image dimension")
        if (image_width is None) != (image_height is None):
            raise ValueError("image_width and image_height must be provided together")
        if isinstance(image_quality, bool) or not isinstance(image_quality, int) or not 1 <= image_quality <= 31:
            raise ValueError("image_quality must be an integer from 1 to 31")
        self.data_root = Path(data_root)
        self.command = (command,) if isinstance(command, str) else (("ffmpeg",) if command is None else tuple(command))
        if not self.command or any(not item for item in self.command):
            raise ValueError("ffmpeg command must not be empty")
        self.timeout_seconds = timeout
        self.sample_rate = sample_rate
        self.channels = channels
        self.image_width = image_width
        self.image_height = image_height
        self.image_quality = image_quality

    def extract_audio(self, asset: Asset) -> AudioExtraction:
        if not asset.has_audio:
            raise AudioUnavailableError(f"asset {asset.id} has no audio stream")
        source = self._source(asset)
        output = self.data_root / "assets" / "audio" / f"{asset.content_hash}-{self.sample_rate}-{self.channels}.wav"
        output.parent.mkdir(parents=True, exist_ok=True)
        if self._usable(output):
            return AudioExtraction(output)
        temp = self._temp_path(output)
        argv = [*self.command, "-y", "-v", "error", "-i", str(source), "-vn", "-acodec", "pcm_s16le", "-ar", str(self.sample_rate), "-ac", str(self.channels), str(temp)]
        try:
            self._run(argv)
            self._publish(temp, output)
            self._ensure_output(output)
            return AudioExtraction(output)
        finally:
            temp.unlink(missing_ok=True)

    def extract_keyframe(self, asset: Asset, clip: Clip) -> KeyframeExtraction:
        if clip.asset_id != asset.id:
            raise ExtractionValidationError("clip asset_id does not match asset")
        if clip.asset_duration_ms != asset.duration_ms:
            raise ExtractionValidationError("clip asset_duration_ms does not match asset duration")
        self._source(asset)
        timestamp = (Decimal(clip.start_ms) + Decimal(clip.end_ms)) / Decimal(2)
        if not Decimal(clip.start_ms) < timestamp < Decimal(clip.end_ms):
            raise ExtractionValidationError("clip has no representable interior timestamp")
        stamp = f"{timestamp:012.3f}ms"
        output = self.data_root / "assets" / "keyframes" / str(clip.id) / f"{stamp}.jpg"
        output.parent.mkdir(parents=True, exist_ok=True)
        if self._usable(output):
            return KeyframeExtraction(output, timestamp)
        temp = self._temp_path(output)
        seek_seconds = format(timestamp / Decimal(1000), "f")
        argv = [*self.command, "-y", "-v", "error", "-ss", seek_seconds, "-i", str(asset.source_file), "-frames:v", "1", "-q:v", str(self.image_quality)]
        if self.image_width is not None and self.image_height is not None:
            argv.extend(["-vf", f"scale={self.image_width}:{self.image_height}"])
        argv.append(str(temp))
        try:
            self._run(argv)
            self._publish(temp, output)
            self._ensure_output(output)
            return KeyframeExtraction(output, timestamp)
        finally:
            temp.unlink(missing_ok=True)

    # Friendly plural alias for callers that may later request bounded sets.
    def extract_keyframes(self, asset: Asset, clip: Clip) -> list[KeyframeExtraction]:
        return [self.extract_keyframe(asset, clip)]

    @staticmethod
    def _usable(path: Path) -> bool:
        try:
            return path.is_file() and path.stat().st_size > 0
        except OSError:
            return False

    @staticmethod
    def _temp_path(output: Path) -> Path:
        # Preserve the media suffix so FFmpeg can infer the muxer for the
        # private output while it is still hidden from readers.
        fd, name = tempfile.mkstemp(prefix=f".{output.stem}-", suffix=output.suffix, dir=output.parent)
        os.close(fd)
        path = Path(name)
        path.unlink(missing_ok=True)
        return path

    @staticmethod
    def _publish(temp: Path, output: Path) -> None:
        if not temp.is_file() or temp.stat().st_size <= 0:
            raise ExtractionOutputError(f"ffmpeg produced an empty output: {output.name}")
        os.replace(temp, output)

    @staticmethod
    def _ensure_output(output: Path) -> None:
        if not MediaExtractor._usable(output):
            raise ExtractionOutputError(f"ffmpeg output is missing or empty: {output.name}")

    @staticmethod
    def _source(asset: Asset) -> Path:
        source = Path(asset.source_file)
        if not source.is_file():
            raise ExtractionValidationError(f"asset source file is missing: {source}")
        return source

    def _run(self, argv: list[str]) -> None:
        try:
            completed = subprocess.run(argv, shell=False, capture_output=True, text=True, timeout=self.timeout_seconds, check=False)
        except FileNotFoundError as exc:
            raise ExtractionBinaryMissing(f"ffmpeg binary not found: {self.command[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ExtractionTimeout(f"ffmpeg timed out after {self.timeout_seconds:g}s") from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip()[:500]
            raise ExtractionProcessError(f"ffmpeg failed ({completed.returncode}): {detail}")


FFmpegExtractionService = MediaExtractor
