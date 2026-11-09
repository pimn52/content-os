"""Small, local perceptual fingerprints for already extracted video frames."""
from __future__ import annotations

import math
import subprocess
from pathlib import Path
from statistics import median
from typing import Sequence


class FingerprintError(RuntimeError):
    pass


def dhash64(gray: bytes) -> str:
    """Return a 64-bit horizontal difference hash from a 9x8 gray image."""

    if len(gray) != 72:
        raise FingerprintError("fingerprint image must contain exactly 9x8 gray pixels")
    value = 0
    for row in range(8):
        offset = row * 9
        for column in range(8):
            value = (value << 1) | int(gray[offset + column] > gray[offset + column + 1])
    return f"{value:016x}"


def hamming(left: str, right: str) -> int:
    if len(left) != 16 or len(right) != 16:
        raise FingerprintError("fingerprints must be 64-bit hexadecimal strings")
    try:
        return (int(left, 16) ^ int(right, 16)).bit_count()
    except ValueError as exc:
        raise FingerprintError("fingerprints must be 64-bit hexadecimal strings") from exc


def fingerprint_distance(left: dict, right: dict) -> int:
    """Compare unordered sampled frames, tolerant of changed scene boundaries."""

    left_hashes = [
        item.get("hash") for item in left.get("samples", ())
        if isinstance(item, dict) and isinstance(item.get("hash"), str)
    ]
    right_hashes = [
        item.get("hash") for item in right.get("samples", ())
        if isinstance(item, dict) and isinstance(item.get("hash"), str)
    ]
    if not left_hashes or not right_hashes:
        raise FingerprintError("both fingerprints need sampled frame hashes")
    nearest = [min(hamming(value, candidate) for candidate in right_hashes) for value in left_hashes]
    return int(round(median(nearest)))


class FFmpegFrameFingerprinter:
    """Compute up to eight perceptual frame hashes with the local FFmpeg binary."""

    algorithm = "dhash64-sampled-video-keyframes-v1"
    max_samples = 8

    def __init__(self, command: str | Sequence[str], *, timeout_seconds: float = 30.0):
        if isinstance(command, str):
            command = [command]
        if not command or any(not isinstance(item, str) or not item for item in command):
            raise ValueError("ffmpeg command is required")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("fingerprint timeout must be finite and positive")
        self.command = tuple(command)
        self.timeout_seconds = timeout_seconds

    def _dhash(self, path: Path) -> str:
        argv = [
            *self.command, "-nostdin", "-v", "error", "-i", str(path),
            "-vf", "scale=9:8:flags=area,format=gray",
            "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
        ]
        try:
            result = subprocess.run(
                argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=self.timeout_seconds, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise FingerprintError("frame fingerprint timed out") from exc
        except OSError as exc:
            raise FingerprintError("ffmpeg is unavailable for frame fingerprinting") from exc
        if result.returncode != 0:
            raise FingerprintError("frame fingerprint extraction failed")
        return dhash64(result.stdout)

    def fingerprint(self, frames: Sequence[tuple[Path, float]]) -> dict[str, object]:
        candidates = [(Path(path), float(timestamp)) for path, timestamp in frames]
        if not candidates:
            raise FingerprintError("asset analysis produced no keyframes")
        if len(candidates) <= self.max_samples:
            indexes = list(range(len(candidates)))
        else:
            indexes = list(dict.fromkeys(
                round(index * (len(candidates) - 1) / (self.max_samples - 1))
                for index in range(self.max_samples)
            ))
        samples = [
            {"timestamp_ms": round(candidates[index][1]), "hash": self._dhash(candidates[index][0])}
            for index in indexes
        ]
        return {
            "schema_version": "1.0",
            "algorithm": self.algorithm,
            "sample_count": len(samples),
            "source_keyframe_count": len(candidates),
            "samples": samples,
        }
