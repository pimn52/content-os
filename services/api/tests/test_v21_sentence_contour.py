"""Small real-PCM checks for the bounded V21 evaluation bridge."""
from __future__ import annotations

from pathlib import Path
import struct
import wave

import pytest

from scripts.evaluate_v21_sentence_contour import PARTS, COPY, assert_quiet_cut, extract_part


def write_wav(path: Path, samples: list[int]) -> None:
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(24000)
        target.writeframes(struct.pack(f"<{len(samples)}h", *samples))


def test_v21_exact_copy_and_real_pcm_excerpt(tmp_path: Path) -> None:
    assert "".join(PARTS) == COPY
    samples = [1200] * 240 + [0] * 480 + [2200] * 240
    source, excerpt = tmp_path / "source.wav", tmp_path / "excerpt.wav"
    write_wav(source, samples)
    assert_quiet_cut(source, 20)
    extract_part(source, excerpt, 10, 30)
    with wave.open(str(excerpt), "rb") as rendered:
        assert rendered.getnframes() == 480
        assert rendered.readframes(480) == struct.pack("<480h", *samples[240:720])
    with pytest.raises(ValueError, match="already exists"):
        extract_part(source, excerpt, 10, 30)


def test_v21_rejects_voiced_seam(tmp_path: Path) -> None:
    source = tmp_path / "voiced.wav"
    write_wav(source, [1500] * 1200)
    with pytest.raises(ValueError, match="acoustic content"):
        assert_quiet_cut(source, 20)
