from datetime import datetime, timezone
import hashlib
from pathlib import Path
import struct
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import pytest

from app.db import AssetRepository, ClipRepository, Database, JobRepository, ProjectRepository
from app.domain.models import (
    Asset, Clip, Job, JobStatus, JobType, ProjectFormat, RationalFps, RenderVideoJobPayload,
    SourceKind, VideoScene, VideoSpec, VideoVisual,
)
from app.media import AudioImporter, AudioProbeMetadata, MediaImporter, ProbeMetadata
from app.main import create_app


def _seed(tmp_path: Path):
    db_path = tmp_path / "library.sqlite"
    source = tmp_path / "素材 with spaces.mp4"
    source.write_bytes(b"video bytes")
    db = Database(db_path)
    asset = Asset(source_file=str(source), content_hash=hashlib.sha256(source.read_bytes()).hexdigest(), duration_ms=2_000, width=640, height=360,
                  fps=RationalFps(numerator=30, denominator=1), authorization_reference="rights",
                  imported_at=datetime.now(timezone.utc))
    clip = Clip(asset_id=asset.id, start_ms=200, end_ms=1_200, asset_duration_ms=2_000,
                transcript="hello world", visual_description="person at a desk", people=["person"],
                objects=["computer"], orientation=ProjectFormat.HORIZONTAL)
    AssetRepository(db).create(asset)
    ClipRepository(db).create(clip)
    db.close()
    return db_path, source, asset, clip


def test_asset_library_read_api_and_preview(tmp_path: Path):
    db_path, source, asset, clip = _seed(tmp_path)
    with TestClient(create_app(data_path=db_path)) as client:
        assets = client.get("/assets")
        assert assets.status_code == 200
        assert assets.json()[0]["id"] == str(asset.id)
        assert client.get(f"/assets/{asset.id}").json()["source_file"] == str(source)
        clips = client.get(f"/assets/{asset.id}/clips")
        assert clips.status_code == 200
        assert clips.json()[0]["id"] == str(clip.id)
        assert client.get(f"/clips/{clip.id}").json()["transcript"] == "hello world"
        media = client.get(f"/assets/{asset.id}/media")
        assert media.status_code == 200
        assert media.content == b"video bytes"
        clip_media = client.get(f"/clips/{clip.id}/media")
        assert clip_media.status_code == 200
        assert clip_media.content == source.read_bytes()
        readiness = client.get(f"/assets/{asset.id}/readiness")
        assert readiness.status_code == 200
        assert readiness.json()["stages"]["imported"]["status"] == "completed"
        assert readiness.json()["stages"]["preprocessed"]["status"] == "not_started"
        assert client.get(f"/assets/{asset.id}/media?start_ms=200&end_ms=1200").status_code == 200
        assert client.get("/asset-library").status_code == 200
        page = client.get("/asset-library").text
        assert "Content OS 素材库" in page
        assert "fetch('/clips/search'" in page


def test_asset_library_not_found_and_interval_validation(tmp_path: Path):
    db_path, _, asset, clip = _seed(tmp_path)
    with TestClient(create_app(data_path=db_path)) as client:
        missing = "00000000-0000-0000-0000-000000000000"
        assert client.get(f"/assets/{missing}").status_code == 404
        assert client.get(f"/assets/{missing}/clips").status_code == 404
        assert client.get(f"/clips/{missing}").status_code == 404
        assert client.get(f"/assets/{asset.id}/media?start_ms=100").status_code == 422
        assert client.get(f"/assets/{asset.id}/media?start_ms=1200&end_ms=200").status_code == 422
        assert client.get(f"/assets/{asset.id}/media?start_ms=0&end_ms=3000").status_code == 422
        source = Path(asset.source_file)
        source.unlink()
        assert client.get(f"/clips/{clip.id}/media").status_code == 404


def test_asset_identity_is_saved_separately_from_source_kind(tmp_path: Path):
    db_path, _, asset, _ = _seed(tmp_path)
    with TestClient(create_app(data_path=db_path)) as client:
        response = client.patch(f"/assets/{asset.id}/identity", json={"identity": "none"})
        assert response.status_code == 200
        body = response.json()
        assert body["source_kind"] == "user_asset"
        assert body["metadata"]["r1_identity"] == "none"


@pytest.mark.parametrize("role", ["meme", "signature"])
def test_reusable_asset_roles_are_saved_through_the_asset_library(tmp_path, role):
    db_path,_,asset,_=_seed(tmp_path)
    with TestClient(create_app(db_path)) as client:
        value=client.patch(f"/assets/{asset.id}/identity",json={"identity":"other","reuse_role":role})
        assert value.status_code==200,value.text
        assert client.get(f"/assets/{asset.id}").json()["metadata"]["reuse_role"]==role
        assert client.patch(f"/assets/{asset.id}/identity",json={"identity":"other","reuse_role":"anything"}).status_code==422


def test_asset_readiness_reflects_retryable_job_state(tmp_path: Path):
    db_path, _, asset, _ = _seed(tmp_path)
    with TestClient(create_app(data_path=db_path)) as client:
        queued = client.post(f"/assets/{asset.id}/jobs/analyze_asset", json={"idempotency_key": "readiness-analyze"})
        assert queued.status_code == 201
        readiness = client.get(f"/assets/{asset.id}/readiness")
        assert readiness.json()["stages"]["preprocessed"]["status"] == "pending"
        assert readiness.json()["stages"]["preprocessed"]["job_id"] == queued.json()["id"]


def test_asset_readiness_exposes_visual_evidence_gap_and_scopes_refresh(tmp_path: Path):
    db_path, _, asset, _ = _seed(tmp_path)
    with Database(db_path) as db:
        missing = Clip(asset_id=asset.id, start_ms=1_200, end_ms=2_000, asset_duration_ms=2_000)
        ClipRepository(db).create(missing)
        AssetRepository(db).update(asset.model_copy(update={"metadata": {
            "ip_profile_id": str(uuid4()), "lifecycle_state": "active",
        }}))
    with TestClient(create_app(data_path=db_path)) as client:
        coverage = client.get(f"/assets/{asset.id}/readiness").json()["visual_coverage"]
        assert coverage == {
            "total_clips": 2,
            "described_clips": 1,
            "missing_clip_ids": [str(missing.id)],
            "creator_owned": True,
        }
        queued = client.post(f"/assets/{asset.id}/jobs/index_clips", json={
            "idempotency_key": "visual-evidence-gap",
            "clip_ids": [str(missing.id)],
            "index_embeddings": False,
        })
        assert queued.status_code == 201, queued.text
        stage = client.get(f"/assets/{asset.id}/readiness").json()["stages"]["visual"]
        assert stage["status"] == "pending"
        assert stage["job_id"] == queued.json()["id"]


def test_asset_readiness_aggregates_intake_visual_jobs(tmp_path: Path):
    from datetime import timedelta
    from app.db import JobRepository
    from app.domain.models import ClipIndexJobPayload, Job, JobStatus, JobType
    from app.jobs.targets import AssetJobTargetStore
    db_path, _, asset, clip = _seed(tmp_path)
    now = datetime.now(timezone.utc)
    group = uuid4()
    with Database(db_path) as db:
        target = AssetJobTargetStore(db)
        jobs = []
        for index, status in enumerate((JobStatus.FAILED, JobStatus.PENDING, JobStatus.COMPLETED, JobStatus.COMPLETED)):
            job = Job(type=JobType.INDEX_CLIPS, status=status,
                idempotency_key=f"intake:vision:{group}:{uuid4()}",
                created_at=now + timedelta(seconds=index), updated_at=now + timedelta(seconds=index),
                error_code="provider_budget_blocked" if status is JobStatus.FAILED else None,
                payload=ClipIndexJobPayload(clip_ids=[clip.id], index_embeddings=False))
            jobs.append(target.enqueue(job, asset.id))
    with TestClient(create_app(data_path=db_path)) as client:
        stage = client.get(f"/assets/{asset.id}/readiness").json()["stages"]["visual"]
        assert stage["status"] == "pending" and stage["job_id"] == str(jobs[1].id)
        with Database(db_path) as db:
            JobRepository(db).update(jobs[1].model_copy(update={"status": JobStatus.COMPLETED}))
        stage = client.get(f"/assets/{asset.id}/readiness").json()["stages"]["visual"]
        assert stage["status"] == "failed" and stage["error_code"] == "provider_budget_blocked"
        retry = jobs[0].model_copy(update={"id": uuid4(), "idempotency_key": jobs[0].idempotency_key + "~new-budget",
            "created_at": now + timedelta(seconds=10), "updated_at": now + timedelta(seconds=10),
            "status": JobStatus.PENDING, "error_code": None})
        with Database(db_path) as db:
            AssetJobTargetStore(db).enqueue(retry, asset.id)
        stage = client.get(f"/assets/{asset.id}/readiness").json()["stages"]["visual"]
        assert stage["status"] == "pending" and stage["job_id"] == str(retry.id)
        with Database(db_path) as db:
            JobRepository(db).update(retry.model_copy(update={"status": JobStatus.COMPLETED}))
        stage = client.get(f"/assets/{asset.id}/readiness").json()["stages"]["visual"]
        assert stage["status"] == "completed"


def test_global_budget_resumes_uncharged_intake_automatically(tmp_path):
    from app.db import JobRepository
    from app.domain.models import ClipIndexJobPayload, Job, JobStatus, JobType
    from app.jobs.targets import AssetJobTargetStore
    db_path, _, asset, clip = _seed(tmp_path)
    now = datetime.now(timezone.utc)
    group = uuid4()
    job = Job(type=JobType.INDEX_CLIPS, status=JobStatus.FAILED,
        idempotency_key=f"intake:vision:{group}:{clip.id}", created_at=now, updated_at=now,
        error_code="provider_budget_blocked", payload=ClipIndexJobPayload(clip_ids=[clip.id], index_embeddings=False))
    with Database(db_path) as db:
        AssetJobTargetStore(db).enqueue(job, asset.id)
    with TestClient(create_app(data_path=db_path)) as client:
        budget = {"currency": "USD", "max_calls": 5, "allow_unknown_cost": False}
        assert client.put('/budget', json=budget).status_code == 200
        assert client.get(f'/assets/{asset.id}/readiness').json()['stages']['visual']['status'] == 'failed'
        budget['allow_unknown_cost'] = True
        assert client.put('/budget', json=budget).status_code == 200
        assert client.get(f'/assets/{asset.id}/readiness').json()['stages']['visual']['status'] == 'pending'
        assert client.put('/budget', json=budget).status_code == 200
        with Database(db_path) as db:
            jobs = JobRepository(db).list_for_asset(asset.id)
            assert len(jobs) == 2 and jobs[0].status is JobStatus.FAILED
            assert db.connection.execute('SELECT COUNT(*) FROM provider_call_records').fetchone()[0] == 0


def test_analysis_result_bundle_persists_provenance_and_replays_clip_semantics(tmp_path: Path):
    db_path, _, asset, _ = _seed(tmp_path)
    bundle = {
        "input_hash": "a" * 64,
        "mode": "assisted_test",
        "source": "local-codex",
        "model": "codex-local",
        "tool": "codex exec",
        "analyzed_at": "2026-09-09T08:00:00Z",
        "profile_snapshot": {"audience": "software creators"},
        "results": [{
            "asset_id": str(asset.id),
            "start_ms": 200,
            "end_ms": 1_200,
            "transcript": "A sourced analysis result",
            "available_subtitles": ["A sourced analysis result"],
            "visual_description": "creator at a desk",
            "action": "typing",
            "confidence": 0.82,
            "keyframes": [{"timestamp_ms": 500, "reference": "frame-500", "description": "hands on keyboard"}],
        }],
    }
    with TestClient(create_app(data_path=db_path)) as client:
        created = client.post("/analysis-results", json=bundle)
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["mode"] == "assisted_test"
        clip_id = body["results"][0]["clip_id"]
        assert client.get(f"/analysis-results/{body['id']}").json()["input_hash"] == "a" * 64
        assert client.get(f"/clips/{clip_id}").json()["transcript"] == "A sourced analysis result"
        assert client.get(f"/assets/{asset.id}").json()["metadata"]["r1_analysis"]["bundle_id"] == body["id"]
        facts = client.get(f"/assets/{asset.id}").json()["metadata"]["visual_analysis"][clip_id]
        assert facts['source_content_hash'] == asset.content_hash
        assert facts['coverage'] == 'sampled_keyframes'
        assert facts['observations'][0]['timestamp_ms'] == 500
        assert 'covered_start_ms' not in facts
        repeated_payload = {**bundle, "id": str(uuid4()), "results": [{**bundle["results"][0], "clip_id": clip_id}]}
        repeated = client.post("/analysis-results", json=repeated_payload)
        assert repeated.status_code == 201
        assert repeated.json()["id"] == body["id"]
        conflict = client.post("/analysis-results", json={**bundle, "model": "different-model"})
        assert conflict.status_code == 409
        invalid = client.post("/analysis-results", json={**bundle, "input_hash": "b" * 64, "results": [{**bundle["results"][0], "asset_id": str(uuid4())}]})
        assert invalid.status_code == 422


def test_local_import_endpoint_uses_existing_importer_and_deduplicates(tmp_path: Path):
    db_path = tmp_path / "import-api.sqlite"
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"one media")
    other = tmp_path / "other.mov"
    other.write_bytes(b"two media")

    class Probe:
        def probe(self, path: Path) -> ProbeMetadata:
            return ProbeMetadata(
                duration_ms=1_000, width=640, height=360,
                fps=RationalFps(numerator=30, denominator=1), has_audio=True, metadata={"name": path.name},
            )

    def importer_factory(db: Database, root: Path) -> MediaImporter:
        return MediaImporter(db, root, Probe())

    with TestClient(create_app(db_path, media_importer_factory=importer_factory)) as client:
        payload = {"source_path": str(source), "authorization_reference": "creator-rights"}
        first = client.post("/imports", json=payload)
        assert first.status_code == 201, first.text
        first_asset = first.json()[0]
        pending = client.get(f"/assets/{first_asset['id']}/readiness").json()
        assert pending['stages']['preprocessed']['status'] == 'pending'
        assert pending['stages']['visual']['status'] == 'not_started'
        repeated = client.post("/imports", json=payload)
        assert repeated.status_code == 201
        assert repeated.json()[0]["id"] == first_asset["id"]
        assert client.get(f"/assets/{first_asset['id']}/readiness").json()['stages']['preprocessed']['job_id'] == pending['stages']['preprocessed']['job_id']
        directory = client.post("/imports", json={**payload, "source_path": str(tmp_path)})
        assert directory.status_code == 201
        directory_hashes = {item["content_hash"] for item in directory.json()}
        assert first_asset["content_hash"] in directory_hashes and len(directory_hashes) == 2
        assert client.post("/imports", json={**payload, "source_path": str(tmp_path / "missing")}).status_code == 404
        workspace = client.get("/workspace")
        assert "本地素材导入" in workspace.text and "json('/imports'" in workspace.text
        assert "静态图片/截图导入" in workspace.text and "json('/image-imports'" in workspace.text
        assert "配音/录音导入" in workspace.text and "json('/audio-imports'" in workspace.text
        assert "本地 Inbox 增量扫描" in workspace.text and "json('/inbox/scan'" in workspace.text
        assert "有来源选题" in workspace.text and "json('/opportunities'" in workspace.text
        assert "readiness" in workspace.text and "重跑预处理" in workspace.text


def test_local_image_import_endpoint_deduplicates_and_serves_static_asset(tmp_path: Path):
    db_path = tmp_path / "image-api.sqlite"
    source = tmp_path / "screen.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + struct.pack(">II", 100, 50) + b"image-payload")
    with TestClient(create_app(data_path=db_path)) as client:
        payload = {"source_path": str(source), "authorization_reference": "creator-screen"}
        first = client.post("/image-imports", json=payload)
        assert first.status_code == 201, first.text
        image = first.json()[0]
        assert image["width"] == 100 and image["height"] == 50 and image["source_kind"] == "screenshot"
        assert client.get(f"/image-assets/{image['id']}").status_code == 200
        assert client.get(f"/image-assets/{image['id']}/media").content == source.read_bytes()
        repeated = client.post("/image-imports", json=payload)
        assert repeated.status_code == 201 and repeated.json()[0]["id"] == image["id"]
        assert client.get("/image-assets").json()[0]["id"] == image["id"]


def test_local_audio_import_endpoint_deduplicates_and_serves_narration_asset(tmp_path: Path):
    db_path = tmp_path / "audio-api.sqlite"
    source = tmp_path / "voice.wav"
    source.write_bytes(b"RIFF fake narration")

    class Probe:
        def probe_audio(self, path: Path) -> AudioProbeMetadata:
            return AudioProbeMetadata(duration_ms=2_000, sample_rate=48_000, channels=1, language="zh", metadata={"name": path.name})

    def importer_factory(db: Database, root: Path) -> AudioImporter:
        return AudioImporter(db, root, Probe())

    with TestClient(create_app(data_path=db_path, audio_importer_factory=importer_factory)) as client:
        payload = {"source_path": str(source), "authorization_reference": "creator-voice", "language": "zh"}
        first = client.post("/audio-imports", json=payload)
        assert first.status_code == 201, first.text
        audio = first.json()[0]
        assert audio["duration_ms"] == 2_000 and audio["sample_rate"] == 48_000 and audio["language"] == "zh"
        assert client.get(f"/audio-assets/{audio['id']}").status_code == 200
        assert client.get(f"/audio-assets/{audio['id']}/media").content == source.read_bytes()
        repeated = client.post("/audio-imports", json=payload)
        assert repeated.status_code == 201 and repeated.json()[0]["id"] == audio["id"]


def test_asset_usage_events_are_export_only_and_idempotent(tmp_path: Path):
    db_path, _, asset, clip = _seed(tmp_path)
    with TestClient(create_app(data_path=db_path)) as client:
        project = client.post("/projects", json={"title": "Usage project", "topic": "Usage topic"}).json()
        payload = {
            "output_version": "draft-v1",
            "events": [{"media_id": str(asset.id), "media_kind": "video", "clip_id": str(clip.id)}],
        }
        first = client.post(f"/projects/{project['id']}/usage-events", json=payload)
        assert first.status_code == 201, first.text
        assert first.json()[0]["usage_kind"] == "production"
        assert client.get(f"/clips/{clip.id}").json()["used_count"] == 1
        repeated = client.post(f"/projects/{project['id']}/usage-events", json=payload)
        assert repeated.status_code == 201 and repeated.json()[0]["id"] == first.json()[0]["id"]
        assert client.get(f"/clips/{clip.id}").json()["used_count"] == 1
        assert len(client.get(f"/projects/{project['id']}/usage-events").json()) == 1


def test_publication_and_feedback_records_are_traceable_and_idempotent(tmp_path: Path):
    db_path, _, asset, clip = _seed(tmp_path)
    with TestClient(create_app(data_path=db_path)) as client:
        project = client.post("/projects", json={"title": "Publishing project", "topic": "Feedback topic"}).json()
        project_id = UUID(project["id"])
        now = datetime.now(timezone.utc)
        render_id = uuid4()
        with Database(db_path) as db:
            project_model = ProjectRepository(db).get(project_id)
            assert project_model is not None
            spec = VideoSpec(
                project_id=project_model.id, format="vertical", width=1080, height=1920, fps=project_model.fps,
                scenes=[VideoScene(
                    scene_id="scene-1", start_frame=0, duration_frames=30,
                    visual=VideoVisual(
                        source_kind=SourceKind.USER_ASSET, authorization_reference="rights",
                        asset_id=asset.id, clip_id=clip.id, clip_start_ms=200, clip_end_ms=1_200,
                        source_duration_ms=asset.duration_ms,
                    ),
                )],
            )
            JobRepository(db).create(Job(
                project_id=project_model.id, type=JobType.RENDER, status=JobStatus.COMPLETED,
                idempotency_key="publication-render", created_at=now, updated_at=now,
                payload=RenderVideoJobPayload(project_id=project_model.id, render_id=render_id, video_spec=spec),
            ))
            output = tmp_path / "renders" / str(project_model.id) / f"{render_id}.mp4"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"rendered output")
        output_version = f"render:{render_id}"
        outputs = client.get(f"/projects/{project_id}/publishable-outputs")
        assert outputs.status_code == 200
        assert outputs.json()[0]["output_version"] == output_version
        assert outputs.json()[0]["available"] is True
        publication = {
            "output_version": output_version, "platform": "manual-youtube", "published_at": "2026-09-09T09:00:00Z",
            "title": "Publishing package", "description": "Package description", "cover_frame_ms": 0,
            "content_url": "https://example.invalid/video", "metrics": {"views": 12}, "metric_source": "manual-export",
            "observation_window_days": 1,
        }
        assert client.post(f"/projects/{project_id}/publications", json={
            **publication, "output_version": "draft-v1",
        }).status_code == 409
        created = client.post(f"/projects/{project_id}/publications", json=publication)
        assert created.status_code == 201, created.text
        repeated = client.post(f"/projects/{project_id}/publications", json=publication)
        assert repeated.status_code == 201 and repeated.json()["id"] == created.json()["id"]
        assert len(client.get(f"/projects/{project_id}/publications").json()) == 1
        assert client.post(f"/projects/{project_id}/publications", json={**publication, "metrics": {"views": 99}}).status_code == 409
        assert client.post(f"/projects/{project_id}/publications", json={**publication, "metrics": {"views": 12}, "metric_source": None}).status_code == 422
        assert client.post(f"/projects/{project_id}/feedback", json={
            "output_version": "draft-v1", "accepted": False,
        }).status_code == 409
        feedback = {"output_version": output_version, "accepted": False, "changed_fields": ["hook"], "rejection_reason": "hook too broad"}
        feedback_response = client.post(f"/projects/{project_id}/feedback", json=feedback)
        assert feedback_response.status_code == 201, feedback_response.text
        feedback_repeat = client.post(f"/projects/{project_id}/feedback", json=feedback)
        assert feedback_repeat.status_code == 201 and feedback_repeat.json()["id"] == feedback_response.json()["id"]
        assert len(client.get(f"/projects/{project_id}/feedback").json()) == 1
        suggestions = client.get(f"/projects/{project_id}/next-suggestions")
        assert suggestions.status_code == 200
        evidence = {ref for item in suggestions.json() for ref in item["evidence_refs"]}
        assert any(ref.startswith("feedback:") for ref in evidence)
        assert any(ref.startswith("publication:") for ref in evidence)
