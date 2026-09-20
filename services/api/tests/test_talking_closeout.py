from datetime import datetime, timezone

import pytest

from app.domain.models import AudioAsset, RationalFps, SourceKind, TranscriptSegment
from app.talking.closeout import TalkingCloseoutPlanningError, plan_talking_closeout, plan_terminal_talking_delivery


NOW = datetime(2026, 9, 19, tzinfo=timezone.utc)


def _audio(*, duration_ms: int = 5_120, segments: list[TranscriptSegment] | None = None, source: str | None = "verified-asr") -> AudioAsset:
    return AudioAsset(
        source_kind=SourceKind.USER_ASSET,
        source_file="narration.wav",
        content_hash="a" * 64,
        duration_ms=duration_ms,
        sample_rate=24_000,
        channels=1,
        authorization_reference="creator-consent",
        imported_at=NOW,
        transcript_segments=segments or [
            TranscriptSegment(start_ms=460, end_ms=3_120, text="Any first clause"),
            TranscriptSegment(start_ms=3_120, end_ms=4_920, text="任意结尾文本"),
        ],
        transcript_source=source,
    )


def test_closeout_uses_final_verified_segment_not_final_word() -> None:
    plan = plan_talking_closeout(_audio(), RationalFps(numerator=25, denominator=1))

    assert plan is not None
    assert (plan.speech_end_ms, plan.narration_end_ms) == (4_920, 5_120)
    assert (plan.start_frame, plan.end_frame, plan.duration_frames) == (123, 128, 5)


def test_closeout_is_language_and_copy_agnostic() -> None:
    audio = _audio(segments=[TranscriptSegment(start_ms=0, end_ms=800, text="The last spoken phrase changes")])

    plan = plan_talking_closeout(audio, RationalFps(numerator=30_000, denominator=1_001))

    assert plan is not None
    assert plan.speech_end_ms == 800
    assert plan.duration_frames == plan.end_frame - plan.start_frame


def test_closeout_is_not_needed_when_speech_reaches_the_render_end() -> None:
    audio = _audio(duration_ms=1_000, segments=[TranscriptSegment(start_ms=0, end_ms=1_000, text="complete")])

    assert plan_talking_closeout(audio, RationalFps(numerator=25, denominator=1)) is None


def test_terminal_delivery_ends_exactly_on_final_verified_speech_frame() -> None:
    plan = plan_terminal_talking_delivery(_audio(), RationalFps(numerator=25, denominator=1))

    assert (plan.speech_end_ms, plan.end_frame) == (4_920, 123)


def test_terminal_delivery_refuses_to_cut_a_partial_final_frame() -> None:
    audio = _audio(duration_ms=1_000, segments=[TranscriptSegment(start_ms=0, end_ms=813, text="last phrase")])

    with pytest.raises(TalkingCloseoutPlanningError, match="aligned"):
        plan_terminal_talking_delivery(audio, RationalFps(numerator=25, denominator=1))


@pytest.mark.parametrize(
    "audio, message",
    [
        (_audio(source=None), "transcript timing"),
        (_audio(segments=[TranscriptSegment(start_ms=0, end_ms=5_121, text="too late")]), "extends beyond"),
    ],
)
def test_closeout_rejects_unverified_or_invalid_timing(audio: AudioAsset, message: str) -> None:
    with pytest.raises(TalkingCloseoutPlanningError, match=message):
        plan_talking_closeout(audio, RationalFps(numerator=25, denominator=1))
