"""FFmpeg scene detection that produces continuous source-backed Clip contracts."""
from __future__ import annotations

import math
import re
import subprocess
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path
from typing import Sequence

from app.domain.models import Asset, Clip


class SegmentationError(RuntimeError):
    """Base error for local scene segmentation."""


class FFmpegBinaryMissing(SegmentationError):
    pass


class FFmpegTimeout(SegmentationError):
    pass


class FFmpegProcessError(SegmentationError):
    pass


class FFmpegMalformedOutput(SegmentationError):
    pass


class NonVideoSource(SegmentationError):
    pass


class SourceFileMissing(SegmentationError):
    pass


_PTS_TIME = re.compile(r"\bpts_time\s*[:=]\s*([^\s]+)")
MAX_MIN_CLIP_DURATION_MS = 3_600_000


def parse_scene_timestamps(output: str, asset_duration_ms: int) -> list[int]:
    """Parse FFmpeg metadata output into sorted, clamped millisecond cuts.

    Empty output means no scene boundaries. Any non-empty output without a
    usable ``pts_time`` record is treated as malformed rather than silently
    producing an arbitrary timeline.
    """
    if isinstance(asset_duration_ms, bool) or not isinstance(asset_duration_ms, int) or asset_duration_ms <= 0:
        raise ValueError("asset_duration_ms must be a positive integer")
    if not isinstance(output, str):
        raise FFmpegMalformedOutput("FFmpeg scene metadata must be text")
    if not output.strip():
        return []
    timestamps: list[int] = []
    matched = False
    for match in _PTS_TIME.finditer(output):
        matched = True
        raw = match.group(1).rstrip(",;")
        try:
            seconds = Decimal(raw)
        except InvalidOperation as exc:
            raise FFmpegMalformedOutput("FFmpeg emitted an invalid scene timestamp") from exc
        if not seconds.is_finite():
            raise FFmpegMalformedOutput("FFmpeg emitted a non-finite scene timestamp")
        milliseconds = int((seconds * Decimal(1000)).to_integral_value(rounding=ROUND_CEILING))
        timestamps.append(min(asset_duration_ms, max(0, milliseconds)))
    if not matched:
        raise FFmpegMalformedOutput("FFmpeg scene metadata contains no pts_time values")
    return sorted(set(timestamps))


def continuous_clips(asset: Asset, boundaries_ms: Sequence[int], min_clip_duration_ms: int) -> list[Clip]:
    """Make complete continuous Clip intervals from candidate source boundaries."""
    _validate_min_clip_duration(min_clip_duration_ms)
    duration = asset.duration_ms
    cuts = {0, duration}
    for boundary in boundaries_ms:
        if isinstance(boundary, bool) or not isinstance(boundary, int):
            raise ValueError("scene boundaries must be integer milliseconds")
        cuts.add(min(duration, max(0, boundary)))
    ordered = sorted(cuts)
    # Remove cuts until every mergeable fragment meets the requested floor.
    # If the entire source is shorter than the floor it remains one valid Clip.
    while len(ordered) > 2:
        short_index = next(
            (index for index in range(len(ordered) - 1) if ordered[index + 1] - ordered[index] < min_clip_duration_ms),
            None,
        )
        if short_index is None:
            break
        # Merge a leading short fragment forward; otherwise merge the short
        # interval backwards by deleting its start boundary.
        del ordered[1 if short_index == 0 else short_index]
    return [
        Clip(asset_id=asset.id, start_ms=start, end_ms=end, asset_duration_ms=duration)
        for start, end in zip(ordered, ordered[1:])
    ]


@dataclass(frozen=True)
class FFmpegSceneDetector:
    """Configurable FFmpeg scene detector with no output-file side effects."""

    command: tuple[str, ...] = ("ffmpeg",)
    threshold: float = 0.3
    min_clip_duration_ms: int = 500
    timeout_seconds: float = 30.0

    def __init__(
        self,
        command: str | Sequence[str] | None = None,
        *,
        threshold: float = 0.3,
        min_clip_duration_ms: int = 500,
        timeout_seconds: float = 30.0,
    ) -> None:
        normalized_command = (command,) if isinstance(command, str) else tuple(command or ("ffmpeg",))
        if not normalized_command or any(not isinstance(item, str) or not item for item in normalized_command):
            raise ValueError("FFmpeg command must not be empty")
        object.__setattr__(self, "command", normalized_command)
        object.__setattr__(self, "threshold", _validate_threshold(threshold))
        _validate_min_clip_duration(min_clip_duration_ms)
        object.__setattr__(self, "min_clip_duration_ms", min_clip_duration_ms)
        object.__setattr__(self, "timeout_seconds", _validate_timeout(timeout_seconds))

    def segment(self, asset: Asset) -> list[Clip]:
        source = Path(asset.source_file)
        if not source.is_file():
            raise SourceFileMissing(f"asset source file does not exist: {source}")
        output = self._run(source)
        return continuous_clips(asset, parse_scene_timestamps(output, asset.duration_ms), self.min_clip_duration_ms)

    def _run(self, source: Path) -> str:
        # Escape FFmpeg's filter separator directly in the argv element. No
        # shell expands the command or the source path.
        filter_graph = f"select=gt(scene\\,{self.threshold:g}),metadata=print:file=-"
        argv = [
            *self.command,
            "-hide_banner", "-nostdin", "-v", "error", "-i", str(source),
            "-map", "0:v:0", "-vf", filter_graph, "-an", "-f", "null", "-",
        ]
        try:
            completed = subprocess.run(
                argv, shell=False, capture_output=True, text=True, timeout=self.timeout_seconds, check=False,
            )
        except FileNotFoundError as exc:
            raise FFmpegBinaryMissing(f"FFmpeg binary not found: {self.command[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise FFmpegTimeout(f"FFmpeg scene detection timed out after {self.timeout_seconds:g}s") from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip()[:500]
            if "matches no streams" in detail.lower() or "does not contain any stream" in detail.lower():
                raise NonVideoSource("asset source contains no video stream")
            raise FFmpegProcessError(f"FFmpeg scene detection failed ({completed.returncode}): {detail}")
        return completed.stdout

    def detect(self, asset: Asset) -> list[Clip]:
        """Alias for callers that use detector terminology."""
        return self.segment(asset)


SceneDetector = FFmpegSceneDetector


def _validate_threshold(value: float) -> float:
    if isinstance(value, bool):
        raise ValueError("threshold must be a finite number from 0 through 1")
    try:
        threshold = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("threshold must be a finite number from 0 through 1") from exc
    if not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise ValueError("threshold must be a finite number from 0 through 1")
    return threshold


def _validate_timeout(value: float) -> float:
    if isinstance(value, bool):
        raise ValueError("timeout_seconds must be finite and positive")
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout_seconds must be finite and positive") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout_seconds must be finite and positive")
    return timeout


def _validate_min_clip_duration(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= MAX_MIN_CLIP_DURATION_MS:
        raise ValueError(f"min_clip_duration_ms must be an integer from 1 to {MAX_MIN_CLIP_DURATION_MS}")
