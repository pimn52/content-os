import hashlib
from pathlib import Path
import wave

import pytest

from app.voice_seam import VoiceSeamCompactionError, compact_measured_seam_quiet


def _wav(path: Path, samples: list[int]) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24_000)
        output.writeframes(b"".join(value.to_bytes(2, "little", signed=True) for value in samples))


def test_compaction_removes_only_measured_quiet_inside_known_seam(tmp_path: Path) -> None:
    rate = 24_000
    samples = [10_000] * (rate * 8 // 10) + [0] * (rate * 16 // 100) + [10_000] * (rate * 8 // 10)
    raw, final = tmp_path / "raw.wav", tmp_path / "final.wav"
    _wav(raw, samples)
    result = compact_measured_seam_quiet(raw, final, [880])
    assert result.cuts == ((0, 860, 900),)
    assert result.raw_duration_ms == 1760 and result.final_duration_ms == 1720
    assert result.raw_sha256 == hashlib.sha256(raw.read_bytes()).hexdigest()
    assert result.final_sha256 == hashlib.sha256(final.read_bytes()).hexdigest()
    with wave.open(str(final), "rb") as wav:
        output = wav.readframes(wav.getnframes())
    original = b"".join(value.to_bytes(2, "little", signed=True) for value in samples)
    assert output == original[:860 * 24 * 2] + original[900 * 24 * 2:]


def test_compaction_rejects_nonquiet_or_same_output(tmp_path: Path) -> None:
    raw = tmp_path / "raw.wav"
    _wav(raw, [10_000] * (24_000 * 2))
    with pytest.raises(VoiceSeamCompactionError, match="measured quiet"):
        compact_measured_seam_quiet(raw, tmp_path / "final.wav", [1_000])
    with pytest.raises(VoiceSeamCompactionError, match="must not overwrite"):
        compact_measured_seam_quiet(raw, raw, [1_000])
    non_pcm = tmp_path / "not-a-wav.wav"
    non_pcm.write_bytes(b"not a wave file")
    with pytest.raises(VoiceSeamCompactionError, match="could not read PCM WAV"):
        compact_measured_seam_quiet(non_pcm, tmp_path / "final.wav", [1_000])
