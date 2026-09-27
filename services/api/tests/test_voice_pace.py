"""Normal Voice pace request, local transform, and independent QA handoff."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import struct
import wave

from fastapi.testclient import TestClient

from app.budget import ProviderCallLedger
from app.db import AudioAssetRepository, BudgetPolicyRepository, Database, IPProfileRepository, JobRepository, ProjectRepository
from app.domain.models import AudioAsset, BudgetPolicy, IPProfile, JobStatus, JobType, Project, RationalFps
from app.jobs import JobRunner, JobStore
from app.jobs.handlers import VoiceQaJobHandler
from app.main import create_app
from app.media import AudioImporter, FFProbeAdapter
from app.providers.asr import TranscriptionResult, TranscriptionSegment
from app.runtime import resolve_local_executable
from app.voice_pace import VoicePaceCandidateJobHandler


def fixture(tmp_path: Path):
    data_root = tmp_path / "content-os-data"
    source = data_root / "assets" / "source.wav"
    source.parent.mkdir(parents=True)
    samples = ([3500, -3500] * 12000) + ([2000, -2000] * 4800)
    with wave.open(str(source), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    db_path = data_root / "content-os.sqlite3"
    with Database(db_path) as db:
        ip = IPProfile(creator_name="Creator")
        IPProfileRepository(db).create(ip)
        project = Project(ip_profile_id=ip.id, title="Voice", topic="New copy",
                          fps=RationalFps(numerator=25, denominator=1), created_at=datetime.now(timezone.utc))
        ProjectRepository(db).create(project)
        audio = AudioAsset(
            source_file="assets/source.wav", content_hash=source_hash, duration_ms=1400,
            sample_rate=24000, channels=1, language="zh", authorization_reference="creator-consent",
            imported_at=datetime.now(timezone.utc), metadata={"voice_generation": {
                "provider": "omnivoice", "model": "test", "project_id": str(project.id),
                "target_text": "你好世界", "qa_state": "verified", "qa": {"qa_state": "verified", "copy_coverage": 1.0},
            }},
        )
        AudioAssetRepository(db).create(audio)
        with db.transaction():
            BudgetPolicyRepository(db).save(BudgetPolicy(project_id=project.id, allow_unknown_cost=False, updated_at=datetime.now(timezone.utc)))
    return db_path, data_root, project, audio, source


def test_pace_api_worker_and_qa_preserve_original_and_gate_candidate(tmp_path: Path) -> None:
    db_path, data_root, project, source_audio, source_path = fixture(tmp_path)
    original = source_path.read_bytes()
    url = f"/projects/{project.id}/voice-pace-candidate-jobs"
    request = {"idempotency_key": "pace-one", "narration_audio_id": str(source_audio.id), "profile": "gentle_slower"}
    with TestClient(create_app(db_path)) as client:
        first = client.post(url, json=request)
        assert first.status_code == 201, first.text
        assert first.json()["type"] == "create_voice_pace_candidate"
        assert client.post(url, json=request).json()["id"] == first.json()["id"]
        assert client.post(url, json={**request, "profile": "fast"}).status_code == 422
        assert client.post(url, json={**request, "narration_audio_id": str(project.id)}).status_code == 404

    with Database(db_path) as db:
        handler = VoicePaceCandidateJobHandler(
            AudioAssetRepository(db), JobRepository(db),
            AudioImporter(db, data_root, FFProbeAdapter(resolve_local_executable("ffprobe"))),
            data_root, resolve_local_executable("ffmpeg"),
        )
        result = JobRunner(JobStore(db), {JobType.CREATE_VOICE_PACE_CANDIDATE: handler},
                           worker_id="pace-worker", lease_duration=timedelta(minutes=1), max_attempts=2).run_once()
        assert result is not None and result.status is JobStatus.COMPLETED, result
        candidates = [audio for audio in AudioAssetRepository(db).list() if audio.id != source_audio.id]
        assert len(candidates) == 1
        candidate = candidates[0]
        generation = candidate.metadata["voice_generation"]
        assert generation["source_audio_asset_id"] == str(source_audio.id)
        assert generation["source_sha256"] == source_audio.content_hash
        assert generation["pace_candidate"] == {"profile": "gentle_slower", "tempo_factor": 0.94}
        assert generation["qa_state"] == "pending"
        assert candidate.authorization_reference == source_audio.authorization_reference
        assert candidate.duration_ms > source_audio.duration_ms
        assert source_path.read_bytes() == original
        qa_jobs = [job for job in JobRepository(db).list() if job.type is JobType.VERIFY_VOICE]
        assert len(qa_jobs) == 1 and qa_jobs[0].payload.narration_audio_id == candidate.id

        class ASR:
            def transcribe(self, path: Path, *, language=None, prompt=None):
                assert Path(path).is_file() and language == "zh"
                return TranscriptionResult("你好世界", (TranscriptionSegment(0, candidate.duration_ms, "你好世界"),), "zh")

        qa_handler = VoiceQaJobHandler(
            AudioAssetRepository(db), ASR(), ProviderCallLedger(db),
            provider_name="faster-whisper", provider_model="test-local",
        )
        qa_result = JobRunner(JobStore(db), {JobType.VERIFY_VOICE: qa_handler},
                              worker_id="qa-worker", lease_duration=timedelta(minutes=1), max_attempts=2).run_once()
        assert qa_result is not None and qa_result.status is JobStatus.COMPLETED, qa_result
        verified = AudioAssetRepository(db).get(candidate.id)
        assert verified.metadata["voice_generation"]["qa_state"] == "verified"
        assert verified.metadata["voice_generation"]["human_review_state"] == "pending"


def test_pace_api_rejects_cross_project_and_unverified_audio(tmp_path: Path) -> None:
    db_path, _, project, audio, _ = fixture(tmp_path)
    with Database(db_path) as db:
        other = Project(ip_profile_id=project.ip_profile_id, title="Other", topic="Another",
                        fps=project.fps, created_at=datetime.now(timezone.utc))
        ProjectRepository(db).create(other)
    with TestClient(create_app(db_path)) as client:
        request = {"idempotency_key": "pace-cross", "narration_audio_id": str(audio.id)}
        assert client.post(f"/projects/{other.id}/voice-pace-candidate-jobs", json=request).status_code == 422
        with Database(db_path) as db:
            changed = audio.model_copy(update={"metadata": {"voice_generation": {"project_id": str(project.id), "qa_state": "pending"}}})
            AudioAssetRepository(db).update(changed)
        assert client.post(f"/projects/{project.id}/voice-pace-candidate-jobs", json=request).status_code == 422


def test_pace_worker_fails_closed_if_source_bytes_change_after_enqueue(tmp_path: Path) -> None:
    db_path, data_root, project, audio, source_path = fixture(tmp_path)
    with TestClient(create_app(db_path)) as client:
        queued = client.post(f"/projects/{project.id}/voice-pace-candidate-jobs", json={
            "idempotency_key": "pace-stale", "narration_audio_id": str(audio.id),
        })
        assert queued.status_code == 201
    with source_path.open("ab") as handle:
        handle.write(b"changed")
    with Database(db_path) as db:
        handler = VoicePaceCandidateJobHandler(
            AudioAssetRepository(db), JobRepository(db),
            AudioImporter(db, data_root, FFProbeAdapter(resolve_local_executable("ffprobe"))),
            data_root, resolve_local_executable("ffmpeg"),
        )
        result = JobRunner(JobStore(db), {JobType.CREATE_VOICE_PACE_CANDIDATE: handler},
                           worker_id="pace-worker", lease_duration=timedelta(minutes=1), max_attempts=2).run_once()
        assert result is not None and result.status is JobStatus.FAILED
        assert result.error_code == "voice_pace_source_bytes_invalid"
        assert len(AudioAssetRepository(db).list()) == 1
