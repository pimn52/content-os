"""Real-WAV safety tests for the bounded V20A focus/landing candidate."""
from __future__ import annotations

import math
import struct
import wave

import pytest

from app.domain.models import (
    NarrationPerformanceCue, NarrationPerformanceCueKind,
    NarrationPerformancePlan, NarrationRhythm,
)
from app.narration_performance import narration_copy_fingerprint
from app.runtime import resolve_local_executable
from scripts.evaluate_c_focus_landing import (
    COPY, apply_gain_windows, plan_with_landing_cues, stretch_final_syllable,
)


def _tone(duration_ms: int, amplitude: int = 3000) -> bytes:
    count = duration_ms * 24
    return struct.pack(
        f"<{count}h", *(round(amplitude * math.sin(2 * math.pi * 200 * index / 24_000)) for index in range(count))
    )


def test_focus_gain_preserves_samples_outside_declared_window() -> None:
    source = _tone(600)
    edited, audit = apply_gain_windows(
        source, windows=((100, 300, 3.0, "test focus"), (400, 500, -2.0, "test contrast")),
        ramp_ms=20,
    )
    assert len(edited) == len(source)
    assert edited[:100 * 48] == source[:100 * 48]
    assert edited[300 * 48:400 * 48] == source[300 * 48:400 * 48]
    assert edited[500 * 48:] == source[500 * 48:]
    assert audit[0]["peak_after"] > audit[0]["peak_before"]
    assert audit[1]["peak_after"] <= audit[1]["peak_before"]


def test_focus_gain_rejects_clipping() -> None:
    source = struct.pack("<14400h", *([32000] * 14400))
    with pytest.raises(ValueError, match="clip"):
        apply_gain_windows(source, windows=((100, 300, 3.0, "unsafe"),), ramp_ms=20)


def test_final_syllable_tempo_is_local_and_playable(tmp_path) -> None:
    source = _tone(400) + _tone(210, amplitude=1800) + b"\x00\x00" * (100 * 24)
    input_path = tmp_path / "source.wav"
    with wave.open(str(input_path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24_000)
        handle.writeframes(source)
    with wave.open(str(input_path), "rb") as handle:
        pcm = handle.readframes(handle.getnframes())
    edited, audit = stretch_final_syllable(
        pcm, start_ms=400, end_ms=610, tempo=0.65,
        crossfade_ms=10, ffmpeg=resolve_local_executable("ffmpeg"),
    )
    assert audit["output_frame_delta"] > 0
    assert edited[:390 * 48] == source[:390 * 48]
    assert edited[-100 * 48:] == source[-100 * 48:]
    output = tmp_path / "derived.wav"
    with wave.open(str(output), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24_000)
        handle.writeframes(edited)
    with wave.open(str(output), "rb") as handle:
        assert handle.getnframes() == len(edited) // 2


def test_landing_plan_adds_exact_copy_cues_without_losing_existing() -> None:
    source = NarrationPerformancePlan(
        copy_fingerprint=narration_copy_fingerprint(COPY),
        delivery_goal="Test exact copy",
        cues=[NarrationPerformanceCue(
            kind=NarrationPerformanceCueKind.RHYTHM,
            start_char=0, end_char=8, rhythm=NarrationRhythm.SETUP,
        )],
    )
    revised = plan_with_landing_cues(source.model_dump(mode="json"))
    assert revised.cues[0] == source.cues[0]
    assert {(cue.kind.value, cue.start_char, cue.end_char) for cue in revised.cues[1:]} == {
        ("emphasis", COPY.index("结论"), COPY.index("结论") + 2),
        ("pace", COPY.index("落下"), COPY.index("落下") + 2),
    }
