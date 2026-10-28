import hashlib
import json
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import AssetRepository, AudioAssetRepository, ClipRepository, Database, IPProfileRepository, ProjectRepository
from app.domain.models import Asset, AudioAsset, Clip, IPProfile, Project, RationalFps, TranscriptSegment
from app.main import create_app


def test_render_output_root_defaults_under_configured_data_root(tmp_path: Path, monkeypatch):
    media_root = tmp_path / "media"
    monkeypatch.setenv("CONTENT_OS_DATA_ROOT", str(media_root))

    app = create_app(data_path=tmp_path / "database.sqlite")

    assert app.state.render_output_root == (media_root / "renders").resolve()


def test_index_clip_scope_is_asset_bound_and_part_of_idempotency(tmp_path):
    from app.db import ClipRepository, JobRepository
    from app.domain.models import Clip
    path = tmp_path / 'scoped-api.sqlite'
    with Database(path) as db:
        asset = _asset(db, tmp_path)
        clips = [ClipRepository(db).create(Clip(asset_id=asset.id, start_ms=i*500,
            end_ms=(i+1)*500, asset_duration_ms=1000)) for i in range(2)]
    with TestClient(create_app(path)) as client:
        url = f'/assets/{asset.id}/jobs/index_clips'
        data = {'idempotency_key':'scoped', 'clip_ids':[str(clips[1].id)]}
        first = client.post(url, json=data)
        assert first.status_code == 201
        assert client.post(url, json=data).json()['id'] == first.json()['id']
        assert client.post(url, json={**data,'clip_ids':[str(clips[0].id)]}).status_code == 409
        assert client.post(url, json={'idempotency_key':'scoped'}).status_code == 409
        for ids in ([], [str(uuid4())], [str(clips[1].id)]*2):
            assert client.post(url,json={**data,'idempotency_key':'invalid','clip_ids':ids}).status_code == 422
        assert client.post(f'/assets/{asset.id}/jobs/analyze_asset',json=data).status_code == 422
        with Database(path, startup_mode='existing') as db:
            stored = JobRepository(db).get(first.json()['id'])
            assert stored.payload.clip_ids == [clips[1].id]
        visual = client.post(url,json={**data,'idempotency_key':'visual-only','index_embeddings':False})
        assert visual.status_code==201
        assert client.post(url,json={**data,'index_embeddings':False}).status_code==409
        with Database(path,startup_mode='existing') as db:
            from app.domain.models import JobStatus
            job=JobRepository(db).get(visual.json()['id'])
            assert not job.payload.index_embeddings
            JobRepository(db).update(job.model_copy(update={'status':JobStatus.COMPLETED}))
        readiness=client.get(f'/assets/{asset.id}/readiness').json()['stages']
        assert readiness['visual']['status']=='completed'
        assert readiness['index']['status']=='pending'  # Earlier requested indexing, not a false completion.
        assert client.post(url,json={'idempotency_key':'unscoped-visual','index_embeddings':False}).status_code==422


def _asset(db: Database, root: Path) -> Asset:
    path = root / "asset.mp4"
    path.write_bytes(b"asset")
    value = Asset(source_file=str(path), content_hash="e" * 64, duration_ms=1000, width=10, height=10, fps=RationalFps(numerator=25, denominator=1), authorization_reference="rights", imported_at=datetime.now(timezone.utc))
    AssetRepository(db).create(value)
    return value


def test_job_api_enqueue_idempotency_status_and_errors(tmp_path: Path):
    db = Database(tmp_path / "api.sqlite")
    asset = _asset(db, tmp_path)
    db.close()
    app = create_app(data_path=tmp_path / "api.sqlite")
    with TestClient(app) as client:
        first = client.post(f"/assets/{asset.id}/jobs/analyze_asset", json={"idempotency_key": "api-key"})
        assert first.status_code == 201
        body = first.json()
        assert body["target_asset_id"] == str(asset.id)
        assert "api_key" not in body and "secret" not in body
        repeated = client.post(f"/assets/{asset.id}/jobs/analyze_asset", json={"idempotency_key": "api-key"})
        assert repeated.status_code == 201 and repeated.json()["id"] == body["id"]
        conflict = client.post(f"/assets/{asset.id}/jobs/transcribe_audio", json={"idempotency_key": "api-key"})
        assert conflict.status_code == 409
        missing_asset = client.post(f"/assets/{uuid4()}/jobs/analyze_asset", json={"idempotency_key": "missing"})
        assert missing_asset.status_code == 404
        missing_project = client.post(f"/assets/{asset.id}/jobs/analyze_asset", json={"idempotency_key": "project-missing", "project_id": str(uuid4())})
        assert missing_project.status_code == 404
        missing_job = client.get(f"/jobs/{uuid4()}")
        assert missing_job.status_code == 404
        status = client.get(f"/jobs/{body['id']}")
        assert status.status_code == 200 and status.json()["id"] == body["id"]
        vision = client.post(f"/assets/{asset.id}/jobs/index_clips", json={"idempotency_key": "vision-key"})
        assert vision.status_code == 201
        assert vision.json()["type"] == "index_clips"
    assert app.state.database_closed is True


def test_job_api_rejects_extra_secret_and_persists_after_restart(tmp_path: Path):
    path = tmp_path / "restart.sqlite"
    db = Database(path)
    asset = _asset(db, tmp_path)
    db.close()
    with TestClient(create_app(data_path=path)) as client:
        response = client.post(f"/assets/{asset.id}/jobs/analyze_asset", json={"idempotency_key": "persist", "api_key": "must-reject"})
        assert response.status_code == 422
    reopened = Database(path)
    reopened.close()
    with TestClient(create_app(data_path=path)) as client:
        created = client.post(f"/assets/{asset.id}/jobs/transcribe_audio", json={"idempotency_key": "persist"})
        assert created.status_code == 201
        job_id = created.json()["id"]
    with TestClient(create_app(data_path=path)) as client:
        assert client.get(f"/jobs/{job_id}").status_code == 200


def test_concurrent_api_enqueue_requests_are_serialized(tmp_path: Path):
    path = tmp_path / "concurrent-api.sqlite"
    db = Database(path)
    asset = _asset(db, tmp_path)
    db.close()
    with TestClient(create_app(data_path=path)) as client:
        def enqueue(index: int) -> int:
            return client.post(f"/assets/{asset.id}/jobs/analyze_asset", json={"idempotency_key": f"concurrent-{index}"}).status_code

        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(enqueue, range(8)))
        assert statuses == [201] * 8
        assert client.get(f"/jobs/{uuid4()}").status_code == 404
    reopened = Database(path)
    try:
        assert reopened.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 8
        assert reopened.connection.execute("SELECT COUNT(*) FROM asset_job_targets").fetchone()[0] == 8
    finally:
        reopened.close()


def test_audio_asset_transcription_api_requires_project_bound_audio_and_is_idempotent(tmp_path: Path):
    from types import SimpleNamespace
    import pytest
    from fastapi import HTTPException
    from app.main import AudioAssetTranscriptionEnqueueRequest

    path = tmp_path / "audio-transcription.sqlite"
    video_path = tmp_path / "source.mp4"
    video_path.write_bytes(b"source video")
    audio_path = tmp_path / "source.wav"
    audio_path.write_bytes(b"source audio")
    now = datetime.now(timezone.utc)
    with Database(path) as db:
        profile = IPProfileRepository(db).create(IPProfile(creator_name="Creator"))
        project = ProjectRepository(db).create(Project(
            ip_profile_id=profile.id, title="P6", topic="bitrate",
            fps=RationalFps(numerator=30, denominator=1), created_at=now,
        ))
        other_project = ProjectRepository(db).create(Project(
            ip_profile_id=profile.id, title="Other", topic="other",
            fps=RationalFps(numerator=30, denominator=1), created_at=now,
        ))
        source = AssetRepository(db).create(Asset(
            source_file=str(video_path), content_hash=hashlib.sha256(video_path.read_bytes()).hexdigest(),
            duration_ms=5_000, width=1280, height=720, fps=RationalFps(numerator=30, denominator=1),
            has_audio=True, authorization_reference="owner-approved", imported_at=now,
        ))
        clip = ClipRepository(db).create(Clip(
            asset_id=source.id, start_ms=0, end_ms=5_000, asset_duration_ms=5_000,
            transcript="one two", transcript_source="source-asr",
            transcript_segments=[TranscriptSegment(start_ms=1_000, end_ms=2_000, text="one"),
                                 TranscriptSegment(start_ms=2_000, end_ms=3_000, text="two")],
        ))
        segments = [TranscriptSegment(start_ms=0, end_ms=1_000, text="one"),
                    TranscriptSegment(start_ms=1_000, end_ms=2_000, text="two")]

        def digest(value):
            return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), default=str).encode("utf-8")).hexdigest()

        transcript_source = "source-clip:source-asr"
        binding = {
            "project_id": str(project.id), "source_asset_id": str(source.id),
            "source_asset_sha256": source.content_hash, "source_clip_id": str(clip.id),
            "source_start_ms": 1_000, "source_end_ms": 3_000,
            "authorization_reference": source.authorization_reference,
            "transcript_source": transcript_source,
            "source_transcript_source": transcript_source,
            "transcript_sha256": digest([item.model_dump(mode="json") for item in segments]),
            "source_transcript_sha256": digest([item.model_dump(mode="json") for item in segments]),
        }
        binding["binding_sha256"] = digest(binding)
        audio = AudioAssetRepository(db).create(AudioAsset(
            source_file=str(audio_path), content_hash=hashlib.sha256(audio_path.read_bytes()).hexdigest(),
            duration_ms=2_000, sample_rate=48_000, channels=2,
            authorization_reference=source.authorization_reference, imported_at=now,
            transcript_source=transcript_source, transcript_segments=segments,
            metadata={"source_audio": {"schema_version": 1, "bindings": [binding]}},
        ))

    app = create_app(path)
    with Database(path, startup_mode="existing") as db:
        app.state.database = db
        app.state.data_root = tmp_path
        route = next(route for route in app.routes
            if getattr(route, "path", None) == "/audio-assets/{audio_id}/jobs/transcribe")

        def invoke(project_id, key):
            request = SimpleNamespace(app=app)
            coroutine = route.endpoint(audio.id, AudioAssetTranscriptionEnqueueRequest(
                project_id=project_id, idempotency_key=key,
            ), request)
            try:
                coroutine.send(None)
            except StopIteration as completed:
                return completed.value
            raise AssertionError("route unexpectedly suspended")

        first = invoke(project.id, "asr-once")
        assert first["type"] == "transcribe_audio_asset"
        assert first["target_asset_id"] is None
        repeated = invoke(project.id, "asr-once")
        assert repeated["id"] == first["id"]
        with pytest.raises(HTTPException) as error:
            invoke(other_project.id, "wrong-project")
        assert error.value.status_code == 409
        assert db.connection.execute("SELECT COUNT(*) FROM audio_asset_job_targets").fetchone()[0] == 1
