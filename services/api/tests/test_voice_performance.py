from datetime import datetime, timezone
import hashlib
from pathlib import Path
import wave

from app.domain.models import Asset, Clip, ConsentRecord, NarrationEmphasis, NarrationPace, NarrationPause, NarrationPerformanceCue, NarrationPerformanceCueKind, NarrationPerformancePlanSource, RationalFps, VoiceProfile
from app.narration_performance import NarrationBoundaryKind, build_narration_performance_plan, derive_narration_boundary_map
from app.voice_performance import authorized_voice_reference_choices, plan_voice_generation_spans, plan_voice_performance_units, select_voice_reference_windows


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


def test_unpunctuated_complete_phrase_remains_one_generation_unit() -> None:
    copy = "先明确问题再解释原因"
    plan = build_narration_performance_plan(
        copy, delivery_goal="Explain the answer.", overall_pace=NarrationPace.CONVERSATIONAL,
        source=NarrationPerformancePlanSource.USER, evidence_refs=[], cues=[
            NarrationPerformanceCue(kind=NarrationPerformanceCueKind.PACE, start_char=0,
                end_char=len(copy), pace=NarrationPace.CONVERSATIONAL),
        ],
    )
    rendered = plan_voice_performance_units(copy, plan)
    spans = plan_voice_generation_spans(rendered, maximum_adjacent_units=1)

    assert len(rendered.units) == len(spans) == 1
    assert rendered.units[0].text == spans[0].text == copy


def test_boundary_map_preserves_punctuation_hierarchy_and_forward_binding() -> None:
    boundaries = derive_narration_boundary_map(COPY)

    assert boundaries.kind_at(COPY.index("。") + 1) is NarrationBoundaryKind.TERMINAL
    but_end = COPY.index("但是") + len("但是")
    last_end = COPY.index("最后") + len("最后")
    assert boundaries.kind_at(but_end) is NarrationBoundaryKind.FORWARD_BINDING
    assert boundaries.kind_at(but_end + 1) is NarrationBoundaryKind.FORWARD_BINDING
    assert boundaries.kind_at(last_end) is NarrationBoundaryKind.FORWARD_BINDING
    assert boundaries.kind_at(COPY.index("决定")) is NarrationBoundaryKind.NO_BREAK


def test_reference_window_ranking_only_proposes_candidates_without_quality_or_boundary_approval(tmp_path: Path) -> None:
    source = tmp_path / "creator.media"
    source.write_bytes(b"media")
    asset = Asset(source_file=str(source), content_hash="a" * 64, duration_ms=20_000, width=320, height=180, fps=RationalFps(numerator=25, denominator=1), authorization_reference="creator-rights", imported_at=datetime.now(timezone.utc))
    analysis_audio = tmp_path / "assets" / "audio" / f"{asset.content_hash}-16000-1.wav"
    analysis_audio.parent.mkdir(parents=True)
    with wave.open(str(analysis_audio), "wb") as output:
        output.setnchannels(1); output.setsampwidth(2); output.setframerate(16_000)
        output.writeframes((10_000).to_bytes(2, "little", signed=True) * 16_000 * 20)
    clip = Clip(asset_id=asset.id, start_ms=15_000, end_ms=20_000, asset_duration_ms=20_000, transcript_source="faster-whisper:small", transcript_segments=[
        {"start_ms": 0, "end_ms": 2_000, "text": "截断尾音"}, {"start_ms": 2_100, "end_ms": 4_500, "text": "片段内容"}, {"start_ms": 4_600, "end_ms": 7_000, "text": "截断句尾"},
    ], speech_quality=None)
    windows = select_voice_reference_windows([clip], {asset.id: asset}, data_root=tmp_path)
    assert windows and windows[0].clip_id == clip.id
    assert 5_000 <= windows[0].end_ms - windows[0].start_ms <= 10_000
    assert any(window.start_ms < clip.start_ms for window in windows)
    assert windows[0].rank_score is not None
    assert all(window.source_timebase == "analysis_wav" for window in windows)
    assert all(window.source_file == str(analysis_audio) and window.source_content_hash == hashlib.sha256(analysis_audio.read_bytes()).hexdigest() for window in windows)
    profile = VoiceProfile(name="Creator", provider="omnivoice", provider_profile_id="creator",
        reference_clip_ids=[clip.id], consent=ConsentRecord(subject_name="Creator", basis="self",
        confirmed=True, confirmed_at=datetime.now(timezone.utc)), created_at=datetime.now(timezone.utc))
    assert authorized_voice_reference_choices(profile, [clip], {asset.id: asset}, data_root=tmp_path) == ()


def test_reference_window_candidates_do_not_fall_back_to_original_asset_time(tmp_path: Path) -> None:
    source = tmp_path / "creator.media"
    source.write_bytes(b"media")
    asset = Asset(source_file=str(source), content_hash="b" * 64, duration_ms=20_000, width=320, height=180,
        fps=RationalFps(numerator=25, denominator=1), authorization_reference="creator-rights", imported_at=datetime.now(timezone.utc))
    clip = Clip(asset_id=asset.id, start_ms=0, end_ms=20_000, asset_duration_ms=20_000,
        transcript_source="faster-whisper:small", transcript_segments=[
            {"start_ms": 0, "end_ms": 2_000, "text": "未经核对的原片时间"},
            {"start_ms": 2_100, "end_ms": 4_500, "text": "分析坐标候选"},
            {"start_ms": 4_600, "end_ms": 7_000, "text": "没有派生音轨"},
        ])
    assert select_voice_reference_windows([clip], {asset.id: asset}, data_root=tmp_path) == ()
