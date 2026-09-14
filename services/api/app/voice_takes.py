"""Provider-neutral composition of already-QA-verified narration takes.

This module deliberately knows nothing about a voice vendor.  A take is only
eligible when its independently persisted voice QA has passed.  FFmpeg makes
one local PCM master file, while the returned provisional timing preserves
each source ASR interval with a deterministic offset.  The caller must still
run independent ASR QA on the resulting master before persisting it as usable
``MasterNarration`` media.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Sequence

from app.domain.models import AudioAsset, TranscriptSegment


class VoiceTakeCompositionError(RuntimeError):
    pass


@dataclass(frozen=True)
class VerifiedVoiceTake:
    """One scene's real, independently QA-verified generated audio."""

    scene_id: str
    audio: AudioAsset


@dataclass(frozen=True)
class ProvisionalMasterNarration:
    """Local output plus source-derived timing pending master-level QA."""

    output_path: Path
    expected_duration_ms: int
    transcript_segments: tuple[TranscriptSegment, ...]
    source_asset_ids: tuple[str, ...]


class VoiceTakeComposer:
    """Create a deterministic local WAV from a bounded ordered take list."""

    def __init__(self, ffmpeg_command: str | Path = "ffmpeg", *, timeout_seconds: float = 120.0) -> None:
        self._ffmpeg = str(ffmpeg_command)
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._timeout_seconds = timeout_seconds

    def compose(self, takes: Sequence[VerifiedVoiceTake], output_path: str | Path) -> ProvisionalMasterNarration:
        if not takes:
            raise VoiceTakeCompositionError("at least one verified voice take is required")
        if len(takes) > 100:
            raise VoiceTakeCompositionError("at most 100 voice takes may be composed at once")
        output = Path(output_path).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        prepared = tuple(self._validate(take) for take in takes)
        if len({take.scene_id for take in prepared}) != len(prepared):
            raise VoiceTakeCompositionError("voice take scene IDs must be unique")

        command = [self._ffmpeg, "-y"]
        for take in prepared:
            command.extend(("-i", take.audio.source_file))
        inputs = "".join(f"[{index}:a]" for index in range(len(prepared)))
        command.extend((
            "-filter_complex", f"{inputs}concat=n={len(prepared)}:v=0:a=1[out]",
            "-map", "[out]", "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", str(output),
        ))
        try:
            completed = subprocess.run(command, shell=False, capture_output=True, text=True, timeout=self._timeout_seconds, check=False)
        except FileNotFoundError as exc:
            raise VoiceTakeCompositionError(f"ffmpeg is unavailable: {self._ffmpeg}") from exc
        except subprocess.TimeoutExpired as exc:
            raise VoiceTakeCompositionError("voice take composition timed out") from exc
        if completed.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
            detail = completed.stderr.strip()[:500]
            raise VoiceTakeCompositionError(f"ffmpeg could not compose voice takes: {detail}")

        offset = 0
        segments: list[TranscriptSegment] = []
        for take in prepared:
            for segment in take.audio.transcript_segments:
                segments.append(TranscriptSegment(
                    start_ms=offset + segment.start_ms,
                    end_ms=offset + segment.end_ms,
                    text=segment.text,
                ))
            offset += take.audio.duration_ms
        return ProvisionalMasterNarration(
            output_path=output,
            expected_duration_ms=offset,
            transcript_segments=tuple(segments),
            source_asset_ids=tuple(str(take.audio.id) for take in prepared),
        )

    @staticmethod
    def _validate(take: VerifiedVoiceTake) -> VerifiedVoiceTake:
        if not isinstance(take, VerifiedVoiceTake) or not take.scene_id.strip():
            raise VoiceTakeCompositionError("each voice take needs a scene ID")
        audio = take.audio
        generation = audio.metadata.get("voice_generation")
        if not isinstance(generation, dict) or generation.get("qa_state") != "verified":
            raise VoiceTakeCompositionError(f"voice take {take.scene_id!r} is not QA-verified")
        if not audio.transcript_source or not audio.transcript_segments:
            raise VoiceTakeCompositionError(f"voice take {take.scene_id!r} has no real timed transcript")
        if not Path(audio.source_file).is_file() or Path(audio.source_file).stat().st_size == 0:
            raise VoiceTakeCompositionError(f"voice take {take.scene_id!r} media is unavailable")
        return take
