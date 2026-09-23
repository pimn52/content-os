"""Real-WAV and semantic-plan checks for the one V19C boundary pilot."""
from __future__ import annotations

import struct
import wave

import pytest

from app.domain.models import (
    NarrationPause, NarrationPerformanceCue, NarrationPerformanceCueKind,
    NarrationPerformancePlan, NarrationRhythm,
)
from app.narration_performance import narration_copy_fingerprint
from scripts.evaluate_c_punctuation_hierarchy import COPY, insert_boundary, plan_with_period_cue


def _pcm(value: int, duration_ms: int) -> bytes:
    count = duration_ms * 24
    return struct.pack(f"<{count}h", *([value] * count))


def test_period_insertion_preserves_voiced_samples_outside_click_envelope(tmp_path) -> None:
    source = _pcm(4000, 100) + _pcm(100, 20) + _pcm(4000, 100)
    path = tmp_path / "source.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24_000)
        handle.writeframes(source)
    with wave.open(str(path), "rb") as handle:
        pcm = handle.readframes(handle.getnframes())
    edited, audit = insert_boundary(pcm, sample_rate=24_000, at_ms=110, pause_ms=180, envelope_ms=5)
    assert len(edited) // 2 == (220 + 180) * 24
    assert edited[:105 * 48] == pcm[:105 * 48]
    assert edited[(115 + 180) * 48:] == pcm[115 * 48:]
    assert edited[110 * 48:(110 + 180) * 48] == _pcm(0, 180)
    assert audit["envelope_samples_per_side"] == 120
    output = tmp_path / "output.wav"
    with wave.open(str(output), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24_000)
        handle.writeframes(edited)
    with wave.open(str(output), "rb") as handle:
        assert handle.getnframes() == 400 * 24


def test_period_insertion_rejects_strong_speech_edge() -> None:
    source = _pcm(4000, 220)
    with pytest.raises(ValueError, match="strong speech"):
        insert_boundary(source, sample_rate=24_000, at_ms=110, pause_ms=180, envelope_ms=5)


def test_user_pause_cue_is_exactly_anchored_and_retains_other_cues() -> None:
    original = NarrationPerformancePlan(
        copy_fingerprint=narration_copy_fingerprint(COPY),
        delivery_goal="Test exact-copy boundary",
        cues=[NarrationPerformanceCue(
            kind=NarrationPerformanceCueKind.RHYTHM,
            start_char=0, end_char=8, rhythm=NarrationRhythm.SETUP,
        )],
    )
    revised = plan_with_period_cue(original.model_dump(mode="json"))
    assert revised.cues[0] == original.cues[0]
    added = revised.cues[-1]
    assert added.kind is NarrationPerformanceCueKind.PAUSE
    assert added.pause is NarrationPause.BRIEF
    assert added.start_char == added.end_char == COPY.index("文案。") + len("文案。")
