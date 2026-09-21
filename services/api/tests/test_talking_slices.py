from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.domain.models import AudioAsset, Clip, SourceKind, TranscriptSegment
from app.talking.slices import TalkingSliceExtractionError, TalkingSlicePlanningError, extract_talking_audio_slice, plan_source_forward_reference_windows, plan_talking_audio_slice, plan_talking_audio_slice_series


NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)


def _master(*, segments: list[TranscriptSegment] | None = None, verified: bool = True, source: str | None = "independent-asr:test") -> AudioAsset:
    return AudioAsset(
        source_kind=SourceKind.USER_ASSET,
        source_file="master.wav",
        content_hash="m" * 64,
        duration_ms=6_000,
        sample_rate=24_000,
        channels=1,
        authorization_reference="creator-consent",
        imported_at=NOW,
        transcript_source=source,
        transcript_segments=segments or [
            TranscriptSegment(start_ms=120, end_ms=1_420, text="第一句"),
            TranscriptSegment(start_ms=1_620, end_ms=3_000, text="第二句"),
            TranscriptSegment(start_ms=3_240, end_ms=4_800, text="第三句"),
        ],
        metadata={"voice_generation": {"qa_state": "verified" if verified else "failed"}},
    )


def test_plan_uses_complete_adjacent_verified_speech_intervals() -> None:
    master = _master()
    plan = plan_talking_audio_slice(master, start_segment_index=1, end_segment_index=3, max_duration_ms=3_500)
    assert plan.master_audio_id == master.id
    assert (plan.start_ms, plan.end_ms, plan.duration_ms) == (1_620, 4_800, 3_180)
    assert plan.text == "第二句 第三句"
    assert (plan.start_segment_index, plan.end_segment_index) == (1, 3)


def test_series_partitions_all_sentences_and_exposes_visual_handoffs() -> None:
    master = _master()
    series = plan_talking_audio_slice_series(master, max_duration_ms=3_200)

    assert series.master_audio_id == master.id
    assert [(item.start_segment_index, item.end_segment_index) for item in series.slices] == [(0, 2), (2, 3)]
    assert [(item.start_ms, item.end_ms) for item in series.slices] == [(120, 3_000), (3_240, 4_800)]
    assert series.continuity_boundaries_ms == ((3_000, 3_240),)


def test_series_maps_every_delivery_slice_to_one_forward_source_timeline() -> None:
    series = plan_talking_audio_slice_series(_master(), max_duration_ms=3_200)
    reference = Clip(asset_id=series.master_audio_id, start_ms=10_000, end_ms=20_000, asset_duration_ms=20_000)

    windows = plan_source_forward_reference_windows(reference, series, frame_alignment_context_ms=640)

    assert [(item.master_slice_start_ms, item.master_slice_end_ms) for item in windows] == [(120, 3_000), (3_240, 4_800)]
    assert [(item.start_ms, item.end_ms) for item in windows] == [(10_000, 13_520), (13_120, 15_320)]
    assert windows[1].start_ms - windows[0].start_ms == 3_120


def test_series_rejects_reference_that_cannot_cover_final_alignment_context() -> None:
    series = plan_talking_audio_slice_series(_master(), max_duration_ms=3_200)
    reference = Clip(asset_id=series.master_audio_id, start_ms=10_000, end_ms=15_319, asset_duration_ms=20_000)

    with pytest.raises(TalkingSlicePlanningError, match="too short"):
        plan_source_forward_reference_windows(reference, series, frame_alignment_context_ms=640)


def test_series_requires_resolved_bound_and_never_splits_one_sentence() -> None:
    with pytest.raises(TalkingSlicePlanningError, match="locally verified"):
        plan_talking_audio_slice_series(_master(), max_duration_ms=None)
    with pytest.raises(TalkingSlicePlanningError, match="exceeds"):
        plan_talking_audio_slice_series(_master(), max_duration_ms=1_000)


def test_extraction_rebases_only_planned_master_intervals(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "master.wav"
    source.write_bytes(b"master")
    master = _master().model_copy(update={"source_file": str(source)})
    plan = plan_talking_audio_slice(master, start_segment_index=1, end_segment_index=3, max_duration_ms=3_500)
    received: list[str] = []

    class Result:
        returncode = 0
        stderr = ""

    def fake_run(command: list[str], **_: object) -> Result:
        received.extend(command)
        Path(command[-1]).write_bytes(b"slice")
        return Result()

    monkeypatch.setattr("app.talking.slices.subprocess.run", fake_run)
    sliced = extract_talking_audio_slice(master, plan, tmp_path / "slice.wav", ffmpeg_command="ffmpeg-test")

    assert received[:6] == ["ffmpeg-test", "-y", "-ss", "1.620", "-i", str(source)]
    assert sliced.duration_ms == 3_180
    assert [(item.start_ms, item.end_ms, item.text) for item in sliced.transcript_segments] == [
        (0, 1_380, "第二句"), (1_620, 3_180, "第三句"),
    ]
    assert sliced.metadata["talking_slice"]["master_audio_id"] == str(master.id)


def test_extraction_rejects_a_plan_from_another_master(tmp_path) -> None:
    master = _master()
    plan = plan_talking_audio_slice(master, start_segment_index=0, end_segment_index=1, max_duration_ms=2_000)
    with pytest.raises(TalkingSliceExtractionError, match="does not belong"):
        extract_talking_audio_slice(_master(), plan, tmp_path / "slice.wav")


@pytest.mark.parametrize(
    "master, start, end, maximum, message",
    [
        (_master(verified=False), 0, 1, 2_000, "QA-verified"),
        (_master(source=None), 0, 1, 2_000, "transcript timing"),
        (_master(), -1, 1, 2_000, "out of range"),
        (_master(), 1, 1, 2_000, "out of range"),
        (_master(), 0, 4, 2_000, "out of range"),
        (_master(), 0, 1, 1_000, "exceeds"),
        (_master(segments=[TranscriptSegment(start_ms=500, end_ms=1_500, text="甲"), TranscriptSegment(start_ms=1_400, end_ms=2_000, text="乙")]), 0, 2, 2_000, "not ordered"),
    ],
)
def test_plan_rejects_unverified_invalid_or_over_limit_requests(master: AudioAsset, start: int, end: int, maximum: int, message: str) -> None:
    with pytest.raises(TalkingSlicePlanningError, match=message):
        plan_talking_audio_slice(master, start_segment_index=start, end_segment_index=end, max_duration_ms=maximum)
