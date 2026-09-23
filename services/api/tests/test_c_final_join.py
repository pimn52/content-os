"""Real PCM-WAV safeguards for the bounded V19E final-join edit."""
from __future__ import annotations

import struct
import wave

import pytest

from scripts.evaluate_c_final_join import remove_low_energy_join


def _pcm(value: int, duration_ms: int) -> bytes:
    count = duration_ms * 24
    return struct.pack(f"<{count}h", *([value] * count))


def test_join_edit_preserves_undeclared_samples_and_exact_duration(tmp_path) -> None:
    source = _pcm(4000, 100) + _pcm(100, 10) + _pcm(500, 80) + _pcm(100, 10) + _pcm(4000, 100)
    path = tmp_path / "source.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24_000)
        handle.writeframes(source)
    with wave.open(str(path), "rb") as handle:
        pcm = handle.readframes(handle.getnframes())
    edited, audit = remove_low_energy_join(pcm, sample_rate=24_000, start_ms=110, end_ms=190, envelope_ms=5)
    assert len(edited) // 2 == 220 * 24
    assert edited[:105 * 48] == pcm[:105 * 48]
    assert edited[115 * 48:] == pcm[195 * 48:]
    assert audit["kind"] == "asr_unrecognized_low_energy_audio_not_silence"
    assert audit["removed_rms_dbfs"] > -45
    output = tmp_path / "derived.wav"
    with wave.open(str(output), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24_000)
        handle.writeframes(edited)
    with wave.open(str(output), "rb") as handle:
        assert handle.getnframes() == 220 * 24


def test_join_edit_rejects_strong_speech_hidden_inside_interval() -> None:
    source = _pcm(4000, 100) + _pcm(100, 10) + _pcm(5000, 80) + _pcm(100, 10) + _pcm(4000, 100)
    with pytest.raises(ValueError, match="contains strong speech"):
        remove_low_energy_join(source, sample_rate=24_000, start_ms=110, end_ms=190, envelope_ms=5)


def test_join_edit_rejects_strong_speech_at_boundary() -> None:
    source = _pcm(4000, 300)
    with pytest.raises(ValueError, match="edge contains strong speech"):
        remove_low_energy_join(source, sample_rate=24_000, start_ms=110, end_ms=190, envelope_ms=5)
