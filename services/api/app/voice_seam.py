"""Explicit PCM-only compaction of measured quiet at known Voice take seams."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Sequence
import wave

from app.voice_observation import VoiceObservationError, _pcm_quiet


class VoiceSeamCompactionError(ValueError):
    pass


@dataclass(frozen=True)
class MeasuredSeamCompaction:
    output_path: Path
    raw_sha256: str
    final_sha256: str
    raw_duration_ms: int
    final_duration_ms: int
    cuts: tuple[tuple[int, int, int], ...]  # seam index, raw start ms, raw end ms


def compact_measured_seam_quiet(
    source_path: str | Path, output_path: str | Path, boundaries_ms: Sequence[int], *,
    margin_ms: int = 60, max_cut_ms: int = 300,
) -> MeasuredSeamCompaction:
    """Keep spoken frames byte-exact; delete only quiet inside a known seam.

    This measures an acoustic interval around an already-known concat point.
    It does not infer word, phrase or punctuation boundaries from a waveform.
    """
    if margin_ms < 40 or max_cut_ms <= 0:
        raise VoiceSeamCompactionError("Voice seam margins or maximum cut are invalid")
    source, output = Path(source_path), Path(output_path)
    if source.resolve() == output.resolve():
        raise VoiceSeamCompactionError("Voice seam compaction must not overwrite the raw master")
    try:
        with wave.open(str(source), "rb") as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (1, 2, 24_000, "NONE"):
                raise VoiceSeamCompactionError("Voice seam compaction requires 24kHz mono PCM16 WAV")
            pcm = wav.readframes(wav.getnframes())
    except (OSError, EOFError, wave.Error) as exc:
        raise VoiceSeamCompactionError("Voice seam compaction could not read PCM WAV") from exc
    try:
        quiet, duration_ms = _pcm_quiet(pcm, 24_000, 1)
    except VoiceObservationError as exc:
        raise VoiceSeamCompactionError("Voice seam PCM measurement failed") from exc
    if not boundaries_ms or len(set(boundaries_ms)) != len(boundaries_ms):
        raise VoiceSeamCompactionError("Voice seam compaction needs unique ordered take boundaries")
    cuts: list[tuple[int, int, int]] = []
    previous = 0
    for index, boundary in enumerate(boundaries_ms):
        if boundary <= previous or boundary >= duration_ms:
            raise VoiceSeamCompactionError("Voice seam boundary is not an interior ordered point")
        matching = [item for item in quiet if item["start_ms"] <= boundary <= item["end_ms"]]
        if len(matching) != 1:
            raise VoiceSeamCompactionError(f"Voice seam {index} lacks one measured quiet interval")
        interval = matching[0]
        start, end = interval["start_ms"] + margin_ms, interval["end_ms"] - margin_ms
        if end > start:
            if end - start > max_cut_ms or start < previous:
                raise VoiceSeamCompactionError(f"Voice seam {index} exceeds the bounded quiet cut")
            cuts.append((index, start, end))
        previous = boundary
    parts: list[bytes] = []
    cursor_frames = 0
    for _, start_ms, end_ms in cuts:
        start_frames = start_ms * 24
        end_frames = end_ms * 24
        if start_frames < cursor_frames or end_frames > len(pcm) // 2:
            raise VoiceSeamCompactionError("Voice seam cuts overlap or escape the raw audio")
        parts.append(pcm[cursor_frames * 2:start_frames * 2])
        cursor_frames = end_frames
    parts.append(pcm[cursor_frames * 2:])
    compacted = b"".join(parts)
    output.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24_000)
        wav.writeframes(compacted)
    return MeasuredSeamCompaction(
        output_path=output, raw_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        final_sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
        raw_duration_ms=duration_ms, final_duration_ms=round(len(compacted) / 2 / 24_000 * 1000),
        cuts=tuple(cuts),
    )


__all__ = ["MeasuredSeamCompaction", "VoiceSeamCompactionError", "compact_measured_seam_quiet"]
