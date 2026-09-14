from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app.domain.models import AudioAsset, TranscriptSegment
from app.voice_takes import VerifiedVoiceTake, VoiceTakeComposer, VoiceTakeCompositionError


def _take(path: Path, *, text: str, start: int = 0, end: int = 500) -> AudioAsset:
    path.write_bytes(b"audio")
    return AudioAsset(
        id=uuid4(), source_file=str(path), content_hash=("a" if "one" in path.name else "b") * 64,
        duration_ms=1_000, sample_rate=24_000, channels=1, authorization_reference="voice-consent",
        imported_at=datetime.now(timezone.utc), transcript_source="independent-asr:test",
        transcript_segments=[TranscriptSegment(start_ms=start, end_ms=end, text=text)],
        metadata={"voice_generation": {"provider": "test", "qa_state": "verified"}},
    )


def test_composer_requires_independently_verified_timed_takes(tmp_path: Path) -> None:
    audio = _take(tmp_path / "one.wav", text="第一句")
    pending = audio.model_copy(update={"metadata": {"voice_generation": {"qa_state": "pending"}}})
    with pytest.raises(VoiceTakeCompositionError, match="not QA-verified"):
        VoiceTakeComposer().compose([VerifiedVoiceTake("hook", pending)], tmp_path / "master.wav")
    unmapped = audio.model_copy(update={"transcript_segments": [], "transcript_source": None})
    with pytest.raises(VoiceTakeCompositionError, match="no real timed transcript"):
        VoiceTakeComposer().compose([VerifiedVoiceTake("hook", unmapped)], tmp_path / "master.wav")


def test_composer_offsets_source_timing_and_uses_pcm_concat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = _take(tmp_path / "one.wav", text="第一句", start=100, end=800)
    second = _take(tmp_path / "two.wav", text="第二句", start=200, end=900)
    received: list[str] = []

    class Result:
        returncode = 0
        stderr = ""

    def fake_run(command: list[str], **_: object) -> Result:
        received.extend(command)
        Path(command[-1]).write_bytes(b"master")
        return Result()

    monkeypatch.setattr("app.voice_takes.subprocess.run", fake_run)
    result = VoiceTakeComposer("ffmpeg-test").compose(
        [VerifiedVoiceTake("hook", first), VerifiedVoiceTake("proof", second)], tmp_path / "master.wav",
    )
    assert "[0:a][1:a]concat=n=2:v=0:a=1[out]" in received
    assert result.expected_duration_ms == 2_000
    assert [(item.start_ms, item.end_ms, item.text) for item in result.transcript_segments] == [
        (100, 800, "第一句"), (1_200, 1_900, "第二句"),
    ]
    assert result.source_asset_ids == (str(first.id), str(second.id))
