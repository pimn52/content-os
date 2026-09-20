"""Provider-neutral timing policies for the end of a creator Talking take.

Talking inference may need non-speech context after creator speech.  That
context belongs to inference, not automatically to the delivered timeline.
This module keeps the transcript-derived boundary explicit so the execution
planner can either continue an editorial timeline or make a face-visible,
speech-complete terminal delivery.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.domain.models import AudioAsset, RationalFps


class TalkingCloseoutPlanningError(ValueError):
    """The narration cannot safely drive a sentence-final close-out."""


@dataclass(frozen=True)
class TalkingCloseoutPlan:
    """One frame-safe, transcript-derived non-speech visual interval.

    The plan deliberately has no provider/model field and makes no claim about
    how a selected visual should be rendered. The Hybrid Asset Router chooses
    a forward source continuation when one exists, otherwise typography.
    """

    speech_end_ms: int
    narration_end_ms: int
    start_frame: int
    end_frame: int

    @property
    def duration_frames(self) -> int:
        return self.end_frame - self.start_frame


@dataclass(frozen=True)
class TerminalTalkingDeliveryPlan:
    """A lossless frame-aligned terminal cut at verified final speech.

    This is deliberately opt-in: ending a project at speech completion is
    valid only when the narrative itself ends there.  A timestamp which is not
    exactly representable at the project frame rate is rejected rather than
    clipping a partial final phoneme or silently extending the audio.
    """

    speech_end_ms: int
    end_frame: int


def plan_talking_closeout(narration: AudioAsset, fps: RationalFps) -> TalkingCloseoutPlan | None:
    """Return the visual-only tail after any verified narration's final speech.

    ``None`` means the final verified speech interval already reaches the last
    renderable narration frame. Text is intentionally not inspected: the
    policy applies equally to every language and final word.
    """

    _require_verified_timing(narration, fps)

    speech_end_ms = max(segment.end_ms for segment in narration.transcript_segments)
    if speech_end_ms > narration.duration_ms:
        raise TalkingCloseoutPlanningError("narration transcript extends beyond narration audio")
    start_frame = _ceiling_frame(speech_end_ms, fps)
    end_frame = _ceiling_frame(narration.duration_ms, fps)
    if start_frame >= end_frame:
        return None
    return TalkingCloseoutPlan(
        speech_end_ms=speech_end_ms,
        narration_end_ms=narration.duration_ms,
        start_frame=start_frame,
        end_frame=end_frame,
    )


def plan_terminal_talking_delivery(
    narration: AudioAsset,
    fps: RationalFps,
) -> TerminalTalkingDeliveryPlan:
    """Return the exact terminal boundary for a face-visible Talking ending.

    The delivered audio ends on its verified final speech timestamp.  The
    added silent look-ahead used by a model is never represented here.  This
    keeps exported audio and video duration equal without manufacturing a
    no-mouth visual tail.
    """

    _require_verified_timing(narration, fps)
    speech_end_ms = max(segment.end_ms for segment in narration.transcript_segments)
    if speech_end_ms > narration.duration_ms:
        raise TalkingCloseoutPlanningError("narration transcript extends beyond narration audio")
    numerator = speech_end_ms * fps.numerator
    denominator = 1_000 * fps.denominator
    if numerator % denominator:
        raise TalkingCloseoutPlanningError(
            "terminal Talking delivery requires a final speech timestamp aligned to the project frame rate"
        )
    return TerminalTalkingDeliveryPlan(speech_end_ms=speech_end_ms, end_frame=numerator // denominator)


def _require_verified_timing(narration: AudioAsset, fps: RationalFps) -> None:
    """Validate the shared, persisted timing inputs for end-boundary plans."""

    if not isinstance(narration, AudioAsset):
        raise TalkingCloseoutPlanningError("Talking close-out requires a typed narration audio asset")
    if not isinstance(fps, RationalFps):
        raise TalkingCloseoutPlanningError("Talking close-out requires a rational project frame rate")
    if not narration.transcript_source or not narration.transcript_segments:
        raise TalkingCloseoutPlanningError(
            "Talking close-out requires persisted verified narration transcript timing"
        )


def _ceiling_frame(timestamp_ms: int, fps: RationalFps) -> int:
    if isinstance(timestamp_ms, bool) or not isinstance(timestamp_ms, int) or timestamp_ms < 0:
        raise TalkingCloseoutPlanningError("narration timestamps must be non-negative milliseconds")
    numerator = timestamp_ms * fps.numerator
    denominator = 1_000 * fps.denominator
    return (numerator + denominator - 1) // denominator


__all__ = [
    "TalkingCloseoutPlan",
    "TalkingCloseoutPlanningError",
    "TerminalTalkingDeliveryPlan",
    "plan_talking_closeout",
    "plan_terminal_talking_delivery",
]
