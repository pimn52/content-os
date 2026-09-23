"""PCM safeguards for the single V19D acoustic-tail experiment."""
from __future__ import annotations

import struct
import wave

import pytest

from scripts.evaluate_c_acoustic_tail import remove_acoustic_tail


def _pcm(value: int, duration_ms: int) -> bytes:
    count = duration_ms * 24
    return struct.pack(f"<{count}h", *([value] * count))


def test_tail_edit_preserves_every_sample_outside_declared_cut_and_edges(tmp_path) -> None:
    source = _pcm(4000, 100) + _pcm(100, 10) + _pcm(400, 80) + _pcm(100, 10) + _pcm(4000, 100)
    path = tmp_path / "source.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24_000)
        handle.writeframes(source)
    with wave.open(str(path), "rb") as handle:
        pcm = handle.readframes(handle.getnframes())
    edited, audit = remove_acoustic_tail(pcm, sample_rate=24_000, start_ms=110, end_ms=190, envelope_ms=5)
    assert len(edited) // 2 == 220 * 24
    assert edited[:105 * 48] == pcm[:105 * 48]
    assert edited[115 * 48:] == pcm[195 * 48:]
    assert audit["kind"] == "acoustic_tail_experiment_not_silence"
    assert audit["removed_rms_dbfs"] > -45  # Audible material was not mislabeled quiet.
    output = tmp_path / "derived.wav"
    with wave.open(str(output), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24_000)
        handle.writeframes(edited)
    with wave.open(str(output), "rb") as handle:
        assert handle.getnframes() == 220 * 24


def test_tail_edit_rejects_strong_speech_join_edges() -> None:
    source = _pcm(4000, 300)
    with pytest.raises(ValueError, match="strong speech"):
        remove_acoustic_tail(source, sample_rate=24_000, start_ms=110, end_ms=190, envelope_ms=5)
