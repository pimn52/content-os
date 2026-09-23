"""Real PCM-WAV checks for the bounded V19B derived C edit."""
from __future__ import annotations

import math
import struct
import wave

import pytest

from scripts.evaluate_c_pause_salvage import Cut, edit_pcm


def _tone(milliseconds: int, *, sample_rate: int = 24_000) -> bytes:
    count = milliseconds * sample_rate // 1000
    return struct.pack(
        f"<{count}h", *(int(5000 * math.sin(2 * math.pi * 220 * index / sample_rate)) for index in range(count))
    )


def _silence(milliseconds: int, *, sample_rate: int = 24_000) -> bytes:
    return b"\x00\x00" * (milliseconds * sample_rate // 1000)


def test_pcm_edit_preserves_all_undeclared_samples_and_exact_duration(tmp_path) -> None:
    original = _tone(100) + _silence(100) + _tone(100) + _silence(40) + _tone(60) + _silence(60) + _tone(100)
    source = tmp_path / "source.wav"
    with wave.open(str(source), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24_000)
        handle.writeframes(original)
    with wave.open(str(source), "rb") as handle:
        pcm = handle.readframes(handle.getnframes())
    cuts = (
        Cut(120, 180, "quiet", "synthetic quiet interval"),
        Cut(320, 440, "noncopy_preroll", "synthetic extra vocalization with quiet edges"),
    )
    edited, audit = edit_pcm(pcm, sample_rate=24_000, cuts=cuts)
    expected = pcm[:120 * 48] + pcm[180 * 48:320 * 48] + pcm[440 * 48:]
    assert edited == expected
    assert len(edited) // 2 == (560 - 180) * 24
    assert [(item["start_sample"], item["end_sample"]) for item in audit] == [(2880, 4320), (7680, 10560)]
    output = tmp_path / "derived.wav"
    with wave.open(str(output), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24_000)
        handle.writeframes(edited)
    with wave.open(str(output), "rb") as handle:
        assert handle.getnframes() == len(edited) // 2
        assert handle.readframes(handle.getnframes()) == expected


def test_quiet_cut_rejects_voiced_audio() -> None:
    pcm = _silence(100) + _tone(100) + _silence(100)
    with pytest.raises(ValueError, match="contains voiced audio"):
        edit_pcm(pcm, sample_rate=24_000, cuts=(Cut(120, 180, "quiet", "invalid claim"),))


def test_noncopy_cut_requires_quiet_join_edges() -> None:
    pcm = _silence(100) + _tone(100) + _silence(100)
    with pytest.raises(ValueError, match="start and end in measured quiet"):
        edit_pcm(pcm, sample_rate=24_000, cuts=(Cut(100, 180, "noncopy_preroll", "bad edge"),))
