"""Provider-neutral, evidence-based quality checks for generated narration."""
from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import math
from pathlib import Path
import re
from typing import Sequence

from opencc import OpenCC

from app.domain.models import AudioAsset, TranscriptSegment
from app.providers.asr import TranscriptionResult


_TOKEN = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]", re.IGNORECASE)
_TRADITIONAL_TO_SIMPLIFIED = OpenCC("t2s")
# OpenCC intentionally retains some context-dependent CJK variants.  In
# speech-copy comparison, the Mandarin progressive glyph variants 著/着 are
# indistinguishable; normalize them only in the ephemeral comparison stream.
_SPOKEN_GLYPH_VARIANTS = str.maketrans({"著": "着"})


@dataclass(frozen=True)
class VoiceQaReport:
    """A compact, serializable record of automatic narration acceptance."""

    verified: bool
    playable: bool
    copy_coverage: float
    missing_token_count: int
    duplicate_token_count: int
    substitution_token_count: int
    leading_silence_ms: int
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
            "substitution_token_count": self.substitution_token_count,
            "leading_silence_ms": self.leading_silence_ms,
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
    max_leading_silence_ms: int = 500,
    max_substitution_ratio: float = 0.05,
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
    if isinstance(max_leading_silence_ms, bool) or not isinstance(max_leading_silence_ms, int) or max_leading_silence_ms < 0:
        raise VoiceQaError("max_leading_silence_ms must be a non-negative integer")
    if isinstance(max_substitution_ratio, bool) or not isinstance(max_substitution_ratio, (int, float)) or not math.isfinite(float(max_substitution_ratio)) or not 0 <= float(max_substitution_ratio) <= 1:
        raise VoiceQaError("max_substitution_ratio must be between 0 and 1")

    playable = Path(audio.source_file).is_file() and Path(audio.source_file).stat().st_size > 0
    target = comparison_tokens(target_text)
    observed = comparison_tokens(transcription.text)
    missing, duplicate, substitutions = _alignment_counts(target, observed)
    coverage = 0.0 if not target else max(0.0, min(1.0, (len(target) - missing) / len(target)))
    segments = _segments(transcription.segments, audio.duration_ms)
    leading_silence = segments[0].start_ms if segments else audio.duration_ms
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
    if substitutions and substitutions / len(target) > float(max_substitution_ratio):
        checks.append("copy_substitution_tokens")
    elif substitutions:
        # ASR can substitute a homophone or split/merge a product name even
        # when the generated speech contains the full requested copy. Keep
        # this uncertainty explicit and bounded; never turn arbitrary ASR
        # disagreement into a pass.
        checks.append("copy_substitutions_within_tolerance")
    if leading_silence > max_leading_silence_ms:
        checks.append("leading_silence_or_unrecognized_audio")
    if longest_silence > max_silence_ms:
        checks.append("long_silence")
    blocking = {
        "audio_file_missing_or_empty",
        "timed_transcript_missing",
        "copy_missing_tokens",
        "copy_duplicate_tokens",
        "copy_substitution_tokens",
        "leading_silence_or_unrecognized_audio",
        "long_silence",
    }
    if not any(check in blocking for check in checks):
        checks.append("verified")
    return VoiceQaReport(
        verified=not any(check in blocking for check in checks),
        playable=playable,
        copy_coverage=coverage,
        missing_token_count=missing,
        duplicate_token_count=duplicate,
        substitution_token_count=substitutions,
        leading_silence_ms=leading_silence,
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


def comparison_tokens(value: str) -> tuple[str, ...]:
    """Return an auditable comparison stream without changing stored text."""

    # Local ASR can legitimately choose traditional glyphs for spoken
    # Mandarin when the requested copy uses simplified Chinese.  Normalize
    # only for the comparison token stream; retain the original transcript in
    # persisted QA evidence so review remains auditable.
    normalized = _TRADITIONAL_TO_SIMPLIFIED.convert(value).translate(_SPOKEN_GLYPH_VARIANTS)
    # Keep product names with CamelCase boundaries comparable when the
    # requested copy contains a space but ASR returns a single word, e.g.
    # ``Content OS`` versus ``ContentOS``.
    normalized = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", normalized)
    return tuple(item.casefold() for item in _TOKEN.findall(normalized))


def _alignment_counts(target: Sequence[str], observed: Sequence[str]) -> tuple[int, int, int]:
    """Classify ordered ASR disagreement as missing, extra, or substitution.

    Counter-only comparison reports every ASR substitution twice (one missing
    token plus one duplicate).  Ordered alignment preserves the important
    distinction: a deletion or insertion is a copy-coverage failure, while a
    bounded same-position recognition substitution is retained as explicit
    uncertainty for review.
    """

    missing = duplicate = substitutions = 0
    matcher = SequenceMatcher(None, target, observed, autojunk=False)
    for tag, target_start, target_end, observed_start, observed_end in matcher.get_opcodes():
        if tag == "delete":
            missing += target_end - target_start
        elif tag == "insert":
            duplicate += observed_end - observed_start
        elif tag == "replace":
            target_count = target_end - target_start
            observed_count = observed_end - observed_start
            substitutions += min(target_count, observed_count)
            missing += max(0, target_count - observed_count)
            duplicate += max(0, observed_count - target_count)
    return missing, duplicate, substitutions


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
