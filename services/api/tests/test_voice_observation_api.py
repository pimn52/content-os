"""Normal AudioAsset contract for persisted, non-mutating Voice observations."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import struct
import wave

from fastapi.testclient import TestClient

from app.db import AudioAssetRepository, Database
from app.domain.models import AudioAsset, TranscriptSegment
from app.main import create_app


def _write_wav(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = [1500] * 12000 + [0] * 4800 + [1500] * 12000
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(24000)
        target.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_performance_observation_api_persists_once_without_changing_audio_or_voice_gate(tmp_path: Path) -> None:
    data_root = tmp_path / "content-os-data"
    db_path = data_root / "content-os.sqlite3"
    audio_path = data_root / "assets" / "narration.wav"
    content_hash = _write_wav(audio_path)
    source_bytes = audio_path.read_bytes()
    copy = "先说清楚。再给结论。"
    asset = AudioAsset(
        source_file="assets/narration.wav", content_hash=content_hash,
        duration_ms=1200, sample_rate=24000, channels=1,
        authorization_reference="test-authorized", imported_at=datetime.now(timezone.utc),
        transcript_segments=[TranscriptSegment(start_ms=0, end_ms=500, text="先说清楚"),
                             TranscriptSegment(start_ms=700, end_ms=1200, text="再给结论")],
        transcript_source="faster-whisper:test",
        metadata={"voice_word_timing": {
                    "version": "1.0", "source_sha256": content_hash,
                    "transcript_source": "faster-whisper:test",
                    "words": [{"start_ms": 0, "end_ms": 500, "text": "先说清楚"},
                              {"start_ms": 700, "end_ms": 1200, "text": "再给结论"}]},
                  "voice_generation": {"provider": "test", "target_text": copy,
                                       "qa_state": "verified", "human_review_state": "pending",
                                       "qa": {"qa_state": "verified", "copy_coverage": 1.0,
                                              "missing_token_count": 0, "duplicate_token_count": 0,
                                              "substitution_token_count": 0}}},
    )
    with Database(db_path) as db:
        AudioAssetRepository(db).create(asset)
    with TestClient(create_app(db_path)) as client:
        url = f"/audio-assets/{asset.id}/performance-observation"
        assert client.get(url).status_code == 404
        response = client.post(url)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["source_sha256"] == content_hash
        assert result["word_boundary_alignment"]["state"] == "aligned"
        assert result["observed_at"]
        assert client.get(url).json() == result
        assert client.post(url).json() == result
        stored = client.get(f"/audio-assets/{asset.id}").json()
        assert stored["metadata"]["voice_performance_observation"] == result
        assert stored["metadata"]["voice_generation"]["qa_state"] == "verified"
        assert stored["metadata"]["voice_generation"]["human_review_state"] == "pending"
    with TestClient(create_app(db_path)) as client:
        assert client.get(url).json() == result
    assert audio_path.read_bytes() == source_bytes


def test_performance_observation_api_rejects_unverified_audio(tmp_path: Path) -> None:
    data_root = tmp_path / "content-os-data"
    db_path = data_root / "content-os.sqlite3"
    audio_path = data_root / "assets" / "pending.wav"
    content_hash = _write_wav(audio_path)
    asset = AudioAsset(
        source_file="assets/pending.wav", content_hash=content_hash,
        duration_ms=1200, sample_rate=24000, channels=1,
        authorization_reference="test-authorized", imported_at=datetime.now(timezone.utc),
        metadata={"voice_generation": {"provider": "test", "target_text": "先说清楚。再给结论。",
                                       "qa_state": "pending", "human_review_state": "pending"}},
    )
    with Database(db_path) as db:
        AudioAssetRepository(db).create(asset)
    with TestClient(create_app(db_path)) as client:
        response = client.post(f"/audio-assets/{asset.id}/performance-observation")
        assert response.status_code == 422
        assert "verified Voice QA" in response.json()["detail"]
