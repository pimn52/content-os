"""Real PCM checks for the single A-sentence reuse experiment."""
from __future__ import annotations

from pathlib import Path
import struct
import wave

import pytest

from scripts.evaluate_v23_a_sentence_composition import (
    choose_low_energy_zero_crossing, extract_sample_interval, read_pcm,
)


def write_wav(path: Path, values: list[int]) -> None:
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(24000)
        target.writeframes(struct.pack(f"<{len(values)}h", *values))


def test_a_sentence_excerpt_preserves_exact_source_samples(tmp_path: Path) -> None:
    values = [1000] * 360 + [0] * 480 + [2000] * 360
    source, output = tmp_path / "source.wav", tmp_path / "excerpt.wav"
    write_wav(source, values)
    _, pcm = read_pcm(source)
    cut = choose_low_energy_zero_crossing(pcm, 20)
    assert 360 <= cut < 840
    extract_sample_interval(source, output, cut, 1050)
    _, excerpt = read_pcm(output)
    assert excerpt == pcm[cut * 2:1050 * 2]
    with pytest.raises(ValueError, match="already exists"):
        extract_sample_interval(source, output, cut, 1050)


def test_a_sentence_cut_rejects_voiced_window(tmp_path: Path) -> None:
    source = tmp_path / "voiced.wav"
    write_wav(source, [1600] * 1200)
    _, pcm = read_pcm(source)
    with pytest.raises(ValueError, match="acoustic content"):
        choose_low_energy_zero_crossing(pcm, 20)
