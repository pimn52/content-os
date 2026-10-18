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


@pytest.mark.parametrize("margin_ms,retained_ms", [(60, 120), (100, 200), (140, 280)])
def test_measured_margin_retains_existing_quiet_without_inserting_silence(
    tmp_path: Path, margin_ms: int, retained_ms: int,
) -> None:
    # These are synthetic test values, not preferred punctuation durations.
    pcm = [10_000] * (800 * 24) + [0] * (280 * 24) + [10_000] * (800 * 24)
    raw, final = tmp_path / "raw.wav", tmp_path / "final.wav"
    _wav(raw, pcm)
    raw_sha = hashlib.sha256(raw.read_bytes()).hexdigest()
    result = compact_measured_seam_quiet(raw, final, [940], margin_ms=margin_ms)
    assert result.final_duration_ms == 1600 + retained_ms
    assert result.cuts == (() if retained_ms == 280 else ((0, 800 + margin_ms, 1080 - margin_ms),))
    with wave.open(str(final), "rb") as wav:
        output = wav.readframes(wav.getnframes())
    expected = [10_000] * (800 * 24) + [0] * (retained_ms * 24) + [10_000] * (800 * 24)
    assert output == b"".join(value.to_bytes(2, "little", signed=True) for value in expected)
    assert hashlib.sha256(raw.read_bytes()).hexdigest() == raw_sha


def test_compaction_leaves_take_internal_quiet_untouched_and_enforces_cut_limit(tmp_path: Path) -> None:
    pcm = ([10_000] * (400 * 24) + [0] * (160 * 24) + [10_000] * (400 * 24)
           + [0] * (280 * 24) + [10_000] * (400 * 24))
    raw, final = tmp_path / "raw.wav", tmp_path / "final.wav"
    _wav(raw, pcm)
    original = raw.read_bytes()
    with pytest.raises(VoiceSeamCompactionError, match="bounded quiet cut"):
        compact_measured_seam_quiet(raw, final, [1100], max_cut_ms=150)
    assert not final.exists() and raw.read_bytes() == original

    result = compact_measured_seam_quiet(raw, final, [1100])
    assert result.cuts == ((0, 1020, 1180),)
    with wave.open(str(final), "rb") as wav:
        output = wav.readframes(wav.getnframes())
    source_pcm = b"".join(value.to_bytes(2, "little", signed=True) for value in pcm)
    assert output == source_pcm[:1020 * 24 * 2] + source_pcm[1180 * 24 * 2:]
    assert output[400 * 24 * 2:560 * 24 * 2] == bytes(160 * 24 * 2)
    assert raw.read_bytes() == original
