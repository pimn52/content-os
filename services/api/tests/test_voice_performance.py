from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.domain.models import Asset, Clip, NarrationEmphasis, NarrationPace, NarrationPause, NarrationPerformanceCue, NarrationPerformanceCueKind, NarrationPerformancePlanSource, RationalFps
from app.narration_performance import build_narration_performance_plan
from app.voice_performance import pause_duration_ms, plan_voice_generation_spans, plan_voice_performance_units, select_voice_reference_windows


COPY = "先把判断说清楚。但是，结构决定观众能否听懂。最后，让结论落下。"


def test_performance_units_preserve_copy_and_pause_provenance() -> None:
    plan = build_narration_performance_plan(COPY, delivery_goal="Land the contrast.", overall_pace=NarrationPace.CONVERSATIONAL, source=NarrationPerformancePlanSource.USER, evidence_refs=[], cues=[
        NarrationPerformanceCue(kind=NarrationPerformanceCueKind.EMPHASIS, start_char=COPY.index("判断"), end_char=COPY.index("判断") + 2, emphasis=NarrationEmphasis.STRONG),
        NarrationPerformanceCue(kind=NarrationPerformanceCueKind.PAUSE, start_char=COPY.index("。") + 1, end_char=COPY.index("。") + 1, pause=NarrationPause.BRIEF),
        NarrationPerformanceCue(kind=NarrationPerformanceCueKind.PAUSE, start_char=COPY.index("。", COPY.index("结构")) + 1, end_char=COPY.index("。", COPY.index("结构")) + 1, pause=NarrationPause.BEAT),
    ])
    rendered = plan_voice_performance_units(COPY, plan)
    assert "".join(unit.text for unit in rendered.units) == COPY
    assert [unit.pause_after for unit in rendered.units] == [NarrationPause.BRIEF, NarrationPause.BEAT, None]
    assert pause_duration_ms(NarrationPause.BEAT) == 420


def test_generation_spans_merge_adjacent_units_but_keep_semantic_provenance() -> None:
    plan = build_narration_performance_plan(COPY, delivery_goal="Land the contrast.", overall_pace=NarrationPace.CONVERSATIONAL, source=NarrationPerformancePlanSource.USER, evidence_refs=[], cues=[
        NarrationPerformanceCue(kind=NarrationPerformanceCueKind.EMPHASIS, start_char=COPY.index("判断"), end_char=COPY.index("判断") + 2, emphasis=NarrationEmphasis.STRONG),
        NarrationPerformanceCue(kind=NarrationPerformanceCueKind.PAUSE, start_char=COPY.index("。") + 1, end_char=COPY.index("。") + 1, pause=NarrationPause.BRIEF),
        NarrationPerformanceCue(kind=NarrationPerformanceCueKind.PAUSE, start_char=COPY.index("。", COPY.index("结构")) + 1, end_char=COPY.index("。", COPY.index("结构")) + 1, pause=NarrationPause.BEAT),
    ])
    rendered = plan_voice_performance_units(COPY, plan)
    spans = plan_voice_generation_spans(rendered, maximum_adjacent_units=2)
    assert len(spans) == 2
    assert "".join(span.text for span in spans) == COPY
    assert spans[0].units == rendered.units[:2]
    assert spans[0].units[0].pause_after == NarrationPause.BRIEF
    assert spans[0].pause_after == NarrationPause.BEAT


def test_reference_windows_rank_continuous_authorized_transcript(tmp_path: Path) -> None:
    source = tmp_path / "creator.media"
    source.write_bytes(b"media")
    asset = Asset(source_file=str(source), content_hash="a" * 64, duration_ms=20_000, width=320, height=180, fps=RationalFps(numerator=25, denominator=1), authorization_reference="creator-rights", imported_at=datetime.now(timezone.utc))
    clip = Clip(asset_id=asset.id, start_ms=0, end_ms=20_000, asset_duration_ms=20_000, transcript_segments=[
        {"start_ms": 0, "end_ms": 2_000, "text": "第一句"}, {"start_ms": 2_100, "end_ms": 4_500, "text": "第二句"}, {"start_ms": 4_600, "end_ms": 7_000, "text": "第三句"},
    ], speech_quality=0.8)
    windows = select_voice_reference_windows([clip], {asset.id: asset})
    assert windows and windows[0].clip_id == clip.id
    assert 3_000 <= windows[0].end_ms - windows[0].start_ms <= 10_000
    assert "第一句" in windows[0].transcript
