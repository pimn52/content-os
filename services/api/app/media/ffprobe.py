"""JSON ffprobe adapter with typed, provider-independent errors."""
from __future__ import annotations

import json
import math
import subprocess
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path
from typing import Any, Sequence

from app.domain.models import RationalFps


class ProbeError(RuntimeError):
    pass


class ProbeBinaryMissing(ProbeError):
    pass


class ProbeTimeout(ProbeError):
    pass


class ProbeMalformed(ProbeError):
    pass


class ProbeNoVideo(ProbeError):
    pass


class ProbeInvalid(ProbeError):
    pass


@dataclass(frozen=True)
class ProbeMetadata:
    duration_ms: int
    width: int
    height: int
    fps: RationalFps
    has_audio: bool
    metadata: dict[str, Any]


def _fps(value: Any) -> RationalFps:
    if not isinstance(value, str) or "/" not in value:
        raise ProbeInvalid("ffprobe returned an invalid frame rate")
    numerator, denominator = value.split("/", 1)
    try:
        n, d = int(numerator), int(denominator)
    except ValueError as exc:
        raise ProbeInvalid("ffprobe returned an invalid frame rate") from exc
    if n <= 0 or d <= 0:
        raise ProbeInvalid("ffprobe returned an invalid frame rate")
    try:
        return RationalFps(numerator=n, denominator=d)
    except ValueError as exc:
        raise ProbeInvalid("ffprobe frame rate is outside the supported range") from exc


def _duration_ms(value: Any) -> int:
    try:
        seconds = Decimal(str(value))
        if not seconds.is_finite() or seconds < 0:
            raise InvalidOperation
        milliseconds = (seconds * Decimal(1000)).to_integral_value(rounding=ROUND_CEILING)
        result = int(milliseconds)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ProbeInvalid("ffprobe returned an invalid duration") from exc
    if result <= 0:
        raise ProbeInvalid("ffprobe duration must be positive")
    return result


def _duration_from_probe(format_info: Any, video: dict[str, Any]) -> int:
    """Use the best valid duration source without losing sub-millisecond precision."""
    candidates: list[Any] = []
    if isinstance(format_info, dict):
        candidates.append(format_info.get("duration"))
    candidates.append(video.get("duration"))
    for candidate in candidates:
        try:
            return _duration_ms(candidate)
        except ProbeInvalid:
            pass
    try:
        ticks = Decimal(str(video.get("duration_ts")))
        time_base = _time_base(video.get("time_base"))
        return _duration_ms(ticks * time_base)
    except (InvalidOperation, ValueError, TypeError, ProbeInvalid):
        raise ProbeInvalid("ffprobe returned no valid duration") from None


def _time_base(value: Any) -> Decimal:
    if not isinstance(value, str) or "/" not in value:
        raise ProbeInvalid("ffprobe returned an invalid time base")
    numerator, denominator = value.split("/", 1)
    try:
        n, d = Decimal(numerator), Decimal(denominator)
    except InvalidOperation as exc:
        raise ProbeInvalid("ffprobe returned an invalid time base") from exc
    if not n.is_finite() or not d.is_finite() or n <= 0 or d <= 0:
        raise ProbeInvalid("ffprobe returned an invalid time base")
    return n / d


def _fps_from_video(video: dict[str, Any]) -> RationalFps:
    for field in ("avg_frame_rate", "r_frame_rate"):
        try:
            return _fps(video.get(field))
        except ProbeInvalid:
            pass
    raise ProbeInvalid("ffprobe returned no valid frame rate")


class FFProbeAdapter:
    def __init__(self, command: str | Sequence[str] | None = None, timeout_seconds: float = 30.0):
        self.command = (command,) if isinstance(command, str) else tuple(command or ("ffprobe",))
        if not self.command or any(not item for item in self.command):
            raise ValueError("ffprobe command must not be empty")
        if isinstance(timeout_seconds, bool):
            raise ValueError("timeout_seconds must be finite and positive")
        try:
            timeout = float(timeout_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError("timeout_seconds must be finite and positive") from exc
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        self.timeout_seconds = timeout

    def probe(self, path: str | Path) -> ProbeMetadata:
        argv = [*self.command, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
        try:
            completed = subprocess.run(argv, shell=False, capture_output=True, text=True, timeout=self.timeout_seconds, check=False)
        except FileNotFoundError as exc:
            raise ProbeBinaryMissing(f"ffprobe binary not found: {self.command[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ProbeTimeout(f"ffprobe timed out after {self.timeout_seconds:g}s") from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip()[:500]
            raise ProbeError(f"ffprobe failed ({completed.returncode}): {detail}")
        try:
            document = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ProbeMalformed("ffprobe returned malformed JSON") from exc
        if not isinstance(document, dict):
            raise ProbeMalformed("ffprobe JSON root must be an object")
        streams = document.get("streams")
        if not isinstance(streams, list):
            raise ProbeMalformed("ffprobe JSON has no streams list")
        video = next((stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "video"), None)
        if video is None:
            raise ProbeNoVideo("media contains no video stream")
        try:
            width, height = int(video["width"]), int(video["height"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProbeInvalid("ffprobe returned invalid video dimensions") from exc
        if width <= 0 or height <= 0:
            raise ProbeInvalid("video dimensions must be positive")
        format_info = document.get("format")
        return ProbeMetadata(
            duration_ms=_duration_from_probe(format_info, video),
            width=width,
            height=height,
            fps=_fps_from_video(video),
            has_audio=any(isinstance(stream, dict) and stream.get("codec_type") == "audio" for stream in streams),
            metadata={"format": format_info if isinstance(format_info, dict) else {}, "streams": streams},
        )
