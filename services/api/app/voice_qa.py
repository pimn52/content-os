"""Provider-neutral, evidence-based quality checks for generated narration."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Sequence

from app.domain.models import AudioAsset, TranscriptSegment
from app.providers.asr import TranscriptionResult


_TOKEN = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]", re.IGNORECASE)


@dataclass(frozen=True)
class VoiceQaReport:
    """A compact, serializable record of automatic narration acceptance."""

    verified: bool
    playable: bool
    copy_coverage: float
    missing_token_count: int
    duplicate_token_count: int
    longest_silence_ms: int
    transcript_segment_count: int
    checks: tuple[str, ...]

    def metadata(self, *, provider: str, model: str) -> dict[str, object]:
        return {
            "qa_state": "verified" if self.verified else "failed",
            "provider": provider,
            "model": model,
            "playable": self.playable,
            "copy_coverage": self.copy_coverage,
            "missing_token_count": self.missing_token_count,
            "duplicate_token_count": self.duplicate_token_count,
            "longest_silence_ms": self.longest_silence_ms,
            "transcript_segment_count": self.transcript_segment_count,
            "checks": list(self.checks),
        }


class VoiceQaError(ValueError):
    pass


def verify_generated_voice(
    audio: AudioAsset,
    target_text: str,
    transcription: TranscriptionResult,
    *,
    max_silence_ms: int = 2_000,
) -> VoiceQaReport:
    """Validate a real ASR/alignment result without inventing semantic output.

    The caller must provide the transcription obtained from the generated file.
    This intentionally treats absent/malformed timing as a failed QA result,
    rather than accepting the provider's requested input text as evidence.
    """

    if not isinstance(audio, AudioAsset):
        raise VoiceQaError("voice QA requires an AudioAsset")
    if not isinstance(target_text, str) or not target_text.strip():
        raise VoiceQaError("voice QA target text must not be empty")
    if not isinstance(transcription, TranscriptionResult):
        raise VoiceQaError("voice QA requires a real TranscriptionResult")
    if isinstance(max_silence_ms, bool) or not isinstance(max_silence_ms, int) or max_silence_ms < 0:
        raise VoiceQaError("max_silence_ms must be a non-negative integer")

    playable = Path(audio.source_file).is_file() and Path(audio.source_file).stat().st_size > 0
    target = _tokens(target_text)
    observed = _tokens(transcription.text)
    target_counts, observed_counts = Counter(target), Counter(observed)
    missing = sum((target_counts - observed_counts).values())
    duplicate = sum((observed_counts - target_counts).values())
    coverage = 0.0 if not target else max(0.0, min(1.0, (len(target) - missing) / len(target)))
    segments = _segments(transcription.segments, audio.duration_ms)
    longest_silence = _longest_silence(segments, audio.duration_ms)
    checks: list[str] = []
    if not playable:
        checks.append("audio_file_missing_or_empty")
    if not segments:
        checks.append("timed_transcript_missing")
    if missing:
        checks.append("copy_missing_tokens")
    if duplicate:
        checks.append("copy_duplicate_tokens")
    if longest_silence > max_silence_ms:
        checks.append("long_silence")
    if not checks:
        checks.append("verified")
    return VoiceQaReport(
        verified=checks == ["verified"],
        playable=playable,
        copy_coverage=coverage,
        missing_token_count=missing,
        duplicate_token_count=duplicate,
        longest_silence_ms=longest_silence,
        transcript_segment_count=len(segments),
        checks=tuple(checks),
    )


def apply_voice_qa(audio: AudioAsset, report: VoiceQaReport, transcription: TranscriptionResult, *, provider: str, model: str) -> AudioAsset:
    """Persist QA evidence beside the generated asset without vendor schema lock-in."""

    if not isinstance(report, VoiceQaReport) or not isinstance(transcription, TranscriptionResult):
        raise VoiceQaError("voice QA evidence is invalid")
    if not isinstance(provider, str) or not provider.strip() or not isinstance(model, str) or not model.strip():
        raise VoiceQaError("voice QA provider identity is invalid")
    generation = audio.metadata.get("voice_generation")
    if not isinstance(generation, dict):
        raise VoiceQaError("voice QA only applies to a generated voice asset")
    metadata = dict(audio.metadata)
    updated_generation = dict(generation)
    updated_generation["qa"] = report.metadata(provider=provider.strip(), model=model.strip())
    updated_generation["qa_state"] = "verified" if report.verified else "failed"
    metadata["voice_generation"] = updated_generation
    return audio.model_copy(update={
        "metadata": metadata,
        "transcript_segments": [
            TranscriptSegment(start_ms=item.start_ms, end_ms=item.end_ms, text=item.text)
            for item in transcription.segments
            if item.end_ms <= audio.duration_ms and item.end_ms > item.start_ms and item.text.strip()
        ],
        "transcript_source": f"{provider.strip()}:{model.strip()}",
    })


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(item.casefold() for item in _TOKEN.findall(value))


def _segments(values: Sequence[object], duration_ms: int) -> tuple[TranscriptSegment, ...]:
    normalized: list[TranscriptSegment] = []
    for item in values:
        start, end, text = getattr(item, "start_ms", None), getattr(item, "end_ms", None), getattr(item, "text", None)
        if not isinstance(start, int) or not isinstance(end, int) or not isinstance(text, str):
            continue
        if start < 0 or end <= start or end > duration_ms or not text.strip():
            continue
        normalized.append(TranscriptSegment(start_ms=start, end_ms=end, text=text.strip()))
    return tuple(sorted(normalized, key=lambda item: (item.start_ms, item.end_ms, item.text)))


def _longest_silence(segments: Sequence[TranscriptSegment], duration_ms: int) -> int:
    if not segments:
        return duration_ms
    gaps = [segments[0].start_ms, duration_ms - segments[-1].end_ms]
    gaps.extend(max(0, later.start_ms - earlier.end_ms) for earlier, later in zip(segments, segments[1:]))
    return max(gaps)
