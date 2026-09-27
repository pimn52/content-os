"""Accounted historical word evidence does not mutate prior Voice decisions."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
from pathlib import Path
import struct
import wave

from fastapi.testclient import TestClient

from app.budget import ProviderCallLedger
from app.db import (AudioAssetRepository, BudgetPolicyRepository, Database, IPProfileRepository,
                    JobRepository, ProjectRepository, ProviderCallRepository)
from app.domain.models import (AudioAsset, BudgetPolicy, IPProfile, JobStatus, JobType, Project,
                               RationalFps, TranscriptSegment)
from app.jobs import JobRunner, JobStore
from app.jobs.handlers import VoiceBoundaryAlignmentJobHandler
from app.main import create_app
from app.providers.asr import TranscriptionResult, TranscriptionSegment, TranscriptionWord


def _fixture(tmp_path: Path):
    data_root = tmp_path / "content-os-data"
    path = data_root / "assets" / "voice.wav"
    path.parent.mkdir(parents=True)
    samples = [8000] * 6400 + [0] * 3200 + [8000] * 9600 + [0] * 9600
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    db_path = data_root / "content-os.sqlite3"
    with Database(db_path) as db:
        profile = IPProfile(creator_name="Creator")
        IPProfileRepository(db).create(profile)
        project = Project(ip_profile_id=profile.id, title="Voice", topic="New copy",
                          fps=RationalFps(numerator=25, denominator=1), created_at=datetime.now(timezone.utc))
        ProjectRepository(db).create(project)
        audio = AudioAsset(
            source_file="assets/voice.wav", content_hash=source_hash,
            duration_ms=1800, sample_rate=16000, channels=1,
            authorization_reference="test-authorized", imported_at=datetime.now(timezone.utc),
            transcript_source="old-independent-asr", transcript_segments=[TranscriptSegment(start_ms=0, end_ms=1800, text="先说再说")],
            metadata={"voice_generation": {
                "project_id": str(project.id), "target_text": "先说。再说。", "qa_state": "verified",
                "human_review_state": "pending", "qa": {"qa_state": "verified", "copy_coverage": 1.0,
                    "missing_token_count": 0, "duplicate_token_count": 0}},
                "voice_performance_observation": {"observation_version": "1.0", "prior": True}},
        )
        AudioAssetRepository(db).create(audio)
        with db.transaction():
            BudgetPolicyRepository(db).save(BudgetPolicy(project_id=project.id, allow_unknown_cost=False,
                                                         updated_at=datetime.now(timezone.utc)))
    return db_path, path, project, audio


def test_backfill_api_worker_idempotency_accounting_and_original_evidence(tmp_path: Path) -> None:
    db_path, path, project, audio = _fixture(tmp_path)
    original_bytes = path.read_bytes()
    request = {"idempotency_key": "boundary-once", "narration_audio_id": str(audio.id)}
    with TestClient(create_app(db_path)) as client:
        url = f"/projects/{project.id}/voice-boundary-alignment-jobs"
        assert client.get(f"/audio-assets/{audio.id}/boundary-alignment").status_code == 404
        created = client.post(url, json=request)
        assert created.status_code == 201, created.text
        assert created.json()["type"] == "align_voice_boundaries"
        assert client.post(url, json=request).json()["id"] == created.json()["id"]
        assert client.post(url, json={**request, "idempotency_key": "other"}).status_code == 409

    class ASR:
        def transcribe(self, source: Path, *, language=None, prompt=None):
            assert source == path.resolve()
            return TranscriptionResult(
                "先说再说", (TranscriptionSegment(0, 1800, "先说再说"),),
                words=(TranscriptionWord(0, 400, "先说"), TranscriptionWord(600, 1200, "再说")),
            )

    with Database(db_path) as db:
        handler = VoiceBoundaryAlignmentJobHandler(
            AudioAssetRepository(db), JobRepository(db), ASR(), ProviderCallLedger(db),
            data_root=db_path.parent, provider_model="local-test-model",
        )
        runner = JobRunner(JobStore(db), {JobType.ALIGN_VOICE_BOUNDARIES: handler},
                           worker_id="boundary-worker", lease_duration=timedelta(minutes=1), max_attempts=1)
        first = runner.run_once(allowed_types={JobType.ALIGN_VOICE_BOUNDARIES})
        second = runner.run_once(allowed_types={JobType.ALIGN_VOICE_BOUNDARIES})
        assert first is not None and first.status is JobStatus.COMPLETED
        assert second is None
        stored = AudioAssetRepository(db).get(audio.id)
        assert stored is not None
        assert stored.metadata["voice_generation"] == audio.metadata["voice_generation"]
        assert stored.metadata["voice_performance_observation"] == audio.metadata["voice_performance_observation"]
        assert stored.transcript_segments == audio.transcript_segments
        assert stored.metadata["voice_word_timing"]["job_id"] == str(first.id)
        alignment = stored.metadata["voice_boundary_alignment"]
        assert alignment["state"] == "aligned"
        assert next(item for item in alignment["boundaries"] if item["char_index"] == 3)["physical_quiet_ms"] == 200
        calls = ProviderCallRepository(db).list_for_project(project.id)
        assert len(calls) == 1 and calls[0].status == "completed" and calls[0].estimated_cost.amount == Decimal("0")
    assert path.read_bytes() == original_bytes
    with TestClient(create_app(db_path)) as client:
        assert client.post(url, json=request).json()["id"] == created.json()["id"]
        assert client.post(url, json={**request, "idempotency_key": "third"}).status_code == 409
        assert client.get(f"/audio-assets/{audio.id}/boundary-alignment").json()["state"] == "aligned"


def test_backfill_fails_before_provider_on_source_change(tmp_path: Path) -> None:
    db_path, path, project, audio = _fixture(tmp_path)
    with TestClient(create_app(db_path)) as client:
        response = client.post(f"/projects/{project.id}/voice-boundary-alignment-jobs",
                               json={"idempotency_key": "source-changed", "narration_audio_id": str(audio.id)})
        assert response.status_code == 201
    path.write_bytes(path.read_bytes() + b"changed")

    class ForbiddenASR:
        def transcribe(self, *_args, **_kwargs):
            raise AssertionError("ASR must not run when the source hash changed")

    with Database(db_path) as db:
        handler = VoiceBoundaryAlignmentJobHandler(AudioAssetRepository(db), JobRepository(db), ForbiddenASR(),
                                                   ProviderCallLedger(db), data_root=db_path.parent,
                                                   provider_model="local-test-model")
        result = JobRunner(JobStore(db), {JobType.ALIGN_VOICE_BOUNDARIES: handler},
                           worker_id="boundary-worker", lease_duration=timedelta(minutes=1), max_attempts=1).run_once()
        assert result is not None and result.status is JobStatus.FAILED
        assert result.error_code == "voice_boundary_preflight_failed"
        assert ProviderCallRepository(db).list_for_project(project.id) == []
