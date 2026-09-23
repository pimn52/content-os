from datetime import datetime, timezone
from pathlib import Path
import subprocess
from uuid import uuid4
import wave

import pytest

from app.domain.models import Asset, AudioAsset, Clip, NarrationEmphasis, NarrationPace, NarrationPause, NarrationPerformanceCue, NarrationPerformanceCueKind, NarrationPerformancePlanSource, RationalFps
from app.narration_performance import NarrationBoundaryKind, NarrationBoundaryMap, build_narration_performance_plan, derive_narration_boundary_map
from app.voice_performance import VoicePerformanceComposer, VoicePerformancePlanningError, pause_duration_ms, plan_voice_generation_spans, plan_voice_performance_units, select_voice_reference_windows


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


def test_boundary_map_preserves_punctuation_hierarchy_and_forward_binding() -> None:
    boundaries = derive_narration_boundary_map(COPY)

    assert boundaries.kind_at(COPY.index("。") + 1) is NarrationBoundaryKind.TERMINAL
    but_end = COPY.index("但是") + len("但是")
    last_end = COPY.index("最后") + len("最后")
    assert boundaries.kind_at(but_end) is NarrationBoundaryKind.FORWARD_BINDING
    assert boundaries.kind_at(but_end + 1) is NarrationBoundaryKind.FORWARD_BINDING
    assert boundaries.kind_at(last_end) is NarrationBoundaryKind.FORWARD_BINDING
    assert boundaries.kind_at(COPY.index("决定")) is NarrationBoundaryKind.NO_BREAK


def _write_pcm(path: Path, *, leading_ms: int, trailing_ms: int) -> None:
    rate = 24_000
    body_frames = rate - (leading_ms + trailing_ms) * rate // 1000
    values = [0] * (leading_ms * rate // 1000) + [10_000] * body_frames + [0] * (trailing_ms * rate // 1000)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(b"".join(value.to_bytes(2, "little", signed=True) for value in values))


def _verified_audio(path: Path) -> AudioAsset:
    return AudioAsset(
        source_file=str(path), content_hash="a" * 64, duration_ms=1_000, sample_rate=24_000, channels=1,
        authorization_reference="test-rights", imported_at=datetime.now(timezone.utc),
        metadata={"voice_generation": {"qa_state": "verified"}},
    )


def test_composer_uses_total_pause_budget_and_rejects_nonterminal_seam(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan = build_narration_performance_plan(COPY, delivery_goal="Land the contrast.", overall_pace=NarrationPace.CONVERSATIONAL, source=NarrationPerformancePlanSource.USER, evidence_refs=[], cues=[
        NarrationPerformanceCue(kind=NarrationPerformanceCueKind.PAUSE, start_char=COPY.index("。") + 1, end_char=COPY.index("。") + 1, pause=NarrationPause.BRIEF),
        NarrationPerformanceCue(kind=NarrationPerformanceCueKind.PAUSE, start_char=COPY.index("。", COPY.index("结构")) + 1, end_char=COPY.index("。", COPY.index("结构")) + 1, pause=NarrationPause.BEAT),
    ])
    spans = plan_voice_generation_spans(plan_voice_performance_units(COPY, plan), maximum_adjacent_units=2)
    first, second = tmp_path / "first.wav", tmp_path / "second.wav"
    _write_pcm(first, leading_ms=0, trailing_ms=190)
    _write_pcm(second, leading_ms=110, trailing_ms=0)
    def fake_run(command, **_kwargs):
        Path(command[-1]).write_bytes(b"wav")
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr("app.voice_performance.subprocess.run", fake_run)
    composition = VoicePerformanceComposer().compose([_verified_audio(first), _verified_audio(second)], spans, tmp_path / "out.wav", boundary_map=derive_narration_boundary_map(COPY))

    assert composition.pause_after_ms == (120, 0)

    with pytest.raises(VoicePerformancePlanningError, match="terminal syntax boundary"):
        VoicePerformanceComposer().compose([_verified_audio(first), _verified_audio(second)], spans, tmp_path / "unsafe.wav", boundary_map=NarrationBoundaryMap("a" * 64, ()))


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
