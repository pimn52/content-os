"""V31's single whole-take pace probe preserves PCM/source identity."""
from __future__ import annotations

from pathlib import Path
import struct
import wave

import pytest

from scripts.evaluate_v31_pace_candidates import TEMPO, pcm_shape, transform_whole_take


def test_whole_take_tempo_is_bounded_and_never_overwrites(tmp_path: Path) -> None:
    source = tmp_path / "source.wav"
    output = tmp_path / "slower.wav"
    samples = [0] * 2400 + ([1200, -1200] * 12000) + [0] * 2400
    with wave.open(str(source), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(24000)
        target.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    original = source.read_bytes()
    before, after = transform_whole_take(source, output)
    assert before == 28800
    assert pcm_shape(output)[:2] == (24000, 1)
    assert after == pcm_shape(output)[2]
    assert abs(after - before / TEMPO) < 24000 * 0.06
    assert source.read_bytes() == original
    with pytest.raises(ValueError, match="already exists"):
        transform_whole_take(source, output)
