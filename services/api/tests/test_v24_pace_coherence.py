"""V24's single existing-audio timing probe has bounded PCM behavior."""
from __future__ import annotations

from pathlib import Path
import struct
import wave

import pytest

from scripts.evaluate_v24_pace_coherence import TEMPO, read_pcm, transform_excerpt


def write_wav(path: Path) -> None:
    # A real periodic signal plus quiet edges exercises the same FFmpeg path
    # used for the candidate without treating the waveform as copy evidence.
    values = [0] * 2400 + ([1200, -1200] * 12000) + [0] * 2400
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(24000)
        target.writeframes(struct.pack(f"<{len(values)}h", *values))


@pytest.mark.parametrize("tempo", (TEMPO[0], TEMPO[2]))
def test_local_tempo_transform_is_bounded_and_never_overwrites(tmp_path: Path, tempo: float) -> None:
    source, output = tmp_path / "source.wav", tmp_path / "changed.wav"
    write_wav(source)
    original_hash = source.read_bytes()
    source_frames, output_frames = transform_excerpt(source, output, tempo)
    rate, pcm = read_pcm(output)
    assert rate == 24000
    assert source_frames == 28800
    assert output_frames == len(pcm) // 2
    assert abs(output_frames - source_frames / tempo) < 24000 * 0.06
    assert source.read_bytes() == original_hash
    with pytest.raises(ValueError, match="already exists"):
        transform_excerpt(source, output, tempo)
