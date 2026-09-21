"""Provider-neutral recovery guidance for failed generated-Voice QA.

This module deliberately selects a *next safe action*, not a silent repair.
In particular, trimming a leading silent region cannot restore a word that
the independent ASR did not observe.  The caller persists this guidance beside
the immutable QA result so the normal audio-asset API makes the boundary
visible before another provider call is queued.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class _VoiceQaEvidence(Protocol):
    verified: bool
    checks: tuple[str, ...]


class VoiceRecoveryRoute(StrEnum):
    NONE = "none"
    NORMALIZE_LEADING_SILENCE = "normalize_leading_silence"
    REGENERATE_SENTENCE_TAKES = "regenerate_sentence_takes"
    PROVIDER_DIAGNOSIS = "provider_diagnosis"


_COPY_BLOCKERS = frozenset({
    "copy_missing_tokens",
    "copy_duplicate_tokens",
    "copy_substitution_tokens",
})
_LEADING_SILENCE_BLOCKER = "leading_silence_or_unrecognized_audio"
_DIAGNOSIS_BLOCKERS = frozenset({
    "audio_file_missing_or_empty",
    "timed_transcript_missing",
    "long_silence",
})
_BLOCKING_CHECKS = _COPY_BLOCKERS | _DIAGNOSIS_BLOCKERS | {_LEADING_SILENCE_BLOCKER}


@dataclass(frozen=True)
class VoiceRecoveryRecommendation:
    """One explicit, non-executing next step for a generated narration."""

    route: VoiceRecoveryRoute
    reason_codes: tuple[str, ...]
    requires_new_voice_generation: bool
    requires_derived_audio: bool
    requires_fresh_voice_qa: bool
    source_audio_preserved: bool = True

    def metadata(self) -> dict[str, object]:
        return {
            "route": self.route.value,
            "reason_codes": list(self.reason_codes),
            "requires_new_voice_generation": self.requires_new_voice_generation,
            "requires_derived_audio": self.requires_derived_audio,
            "requires_fresh_voice_qa": self.requires_fresh_voice_qa,
            "source_audio_preserved": self.source_audio_preserved,
        }


def recommend_voice_recovery(report: _VoiceQaEvidence) -> VoiceRecoveryRecommendation:
    """Classify a Voice QA result without changing its media or QA evidence.

    A future normalizer may only act on the dedicated leading-silence route,
    create a separate derived audio asset and run independent QA again.  Any
    observed copy loss takes precedence, because no audio trim can establish
    that the omitted requested copy was spoken.
    """

    reasons = tuple(check for check in report.checks if check in _BLOCKING_CHECKS)
    if report.verified:
        return VoiceRecoveryRecommendation(
            VoiceRecoveryRoute.NONE,
            (),
            requires_new_voice_generation=False,
            requires_derived_audio=False,
            requires_fresh_voice_qa=False,
        )
    if any(check in _COPY_BLOCKERS for check in reasons):
        return VoiceRecoveryRecommendation(
            VoiceRecoveryRoute.REGENERATE_SENTENCE_TAKES,
            reasons,
            requires_new_voice_generation=True,
            requires_derived_audio=False,
            requires_fresh_voice_qa=True,
        )
    if reasons == (_LEADING_SILENCE_BLOCKER,):
        return VoiceRecoveryRecommendation(
            VoiceRecoveryRoute.NORMALIZE_LEADING_SILENCE,
            reasons,
            requires_new_voice_generation=False,
            requires_derived_audio=True,
            requires_fresh_voice_qa=True,
        )
    return VoiceRecoveryRecommendation(
        VoiceRecoveryRoute.PROVIDER_DIAGNOSIS,
        reasons,
        requires_new_voice_generation=False,
        requires_derived_audio=False,
        requires_fresh_voice_qa=False,
    )
