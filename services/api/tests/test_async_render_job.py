from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import Database, ProjectRepository
from app.domain.models import JobStatus, JobType
from app.jobs import JobRunner, JobStore
from app.jobs.handlers import RenderVideoJobHandler
from app.main import create_app
from app.renderer import RenderInputError, RenderProcessError, RenderTimeout

from test_render_api import _seed


class _Renderer:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls = 0

    def render(self, spec: object, output: Path) -> Path:
        self.calls += 1
        if self.failure:
            raise self.failure
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"\x00\x00\x00\x18ftypisom")
        return output


def _runner(db: Database, renderer: _Renderer, root: Path, *, max_attempts: int = 2) -> JobRunner:
    return JobRunner(
        JobStore(db),
        {JobType.RENDER: RenderVideoJobHandler(ProjectRepository(db), renderer, root)},  # type: ignore[arg-type]
        worker_id="render-worker", lease_duration=timedelta(minutes=1), max_attempts=max_attempts,
    )


def _enqueue(client: TestClient, project_id: object, scene: object, candidate: object, key: str = "render-key") -> dict:
    response = client.post(f"/projects/{project_id}/render-jobs", json={
        "idempotency_key": key,
        "scenes": [scene.model_dump(mode="json")],
        "selections": [candidate.model_dump(mode="json")],
    })
    assert response.status_code == 201, response.text
    return response.json()


def test_render_job_enqueue_is_idempotent_worker_completes_and_status_serves_owned_output(tmp_path: Path) -> None:
    database_path = tmp_path / "render-jobs.sqlite"
    project, scene, candidate = _seed(database_path, tmp_path)
    output_root = tmp_path / "owned-renders"
    app = create_app(database_path, render_output_root=output_root)
    with TestClient(app) as client:
        first = _enqueue(client, project.id, scene, candidate)
        repeated = _enqueue(client, project.id, scene, candidate)
        assert repeated["id"] == first["id"]
        assert first["type"] == "render" and first["status"] == "pending"
        assert first["target_asset_id"] is None and "video_spec" not in first
        render_id = first["render_id"]
        assert client.get(first["download_url"]).status_code == 404

        renderer = _Renderer()
        worker_db = Database(database_path)
        try:
            result = _runner(worker_db, renderer, output_root).run_once()
        finally:
            worker_db.close()
        assert result is not None and result.status is JobStatus.COMPLETED
        assert renderer.calls == 1
        status = client.get(f"/jobs/{first['id']}")
        assert status.status_code == 200
        assert status.json()["status"] == "completed"
        assert status.json()["render_id"] == render_id
        video = client.get(status.json()["download_url"])
        assert video.status_code == 200 and video.headers["content-type"].startswith("video/mp4")
        assert (output_root / str(project.id) / f"{render_id}.mp4").is_file()


def test_render_job_failures_are_classified_and_partial_output_is_not_downloadable(tmp_path: Path) -> None:
    database_path = tmp_path / "render-failures.sqlite"
    project, scene, candidate = _seed(database_path, tmp_path)
    root = tmp_path / "owned-renders"
    app = create_app(database_path, render_output_root=root)
    with TestClient(app) as client:
        retry = _enqueue(client, project.id, scene, candidate, "timeout")
        worker_db = Database(database_path)
        try:
            result = _runner(worker_db, _Renderer(RenderTimeout("private detail")), root).run_once()
        finally:
            worker_db.close()
        assert result is not None and result.status is JobStatus.PENDING
        assert result.error_code == "render_timeout" and "private" not in (result.error_message or "")

        invalid = _enqueue(client, project.id, scene, candidate, "invalid")
        worker_db = Database(database_path)
        try:
            result = _runner(worker_db, _Renderer(RenderInputError("private detail")), root).run_once()
        finally:
            worker_db.close()
        assert result is not None and result.status is JobStatus.FAILED
        assert result.error_code == "render_invalid"
        assert client.get(invalid["download_url"]).status_code == 404

        process = _enqueue(client, project.id, scene, candidate, "process")
        worker_db = Database(database_path)
        try:
            result = _runner(worker_db, _Renderer(RenderProcessError(1)), root, max_attempts=1).run_once()
        finally:
            worker_db.close()
        assert result is not None and result.status is JobStatus.FAILED
        assert result.error_code == "render_processing_failed"
        assert client.get(process["download_url"]).status_code == 404


def test_render_job_payload_rejects_secrets_and_foreign_idempotency_key(tmp_path: Path) -> None:
    database_path = tmp_path / "render-payload.sqlite"
    project, scene, candidate = _seed(database_path, tmp_path)
    with TestClient(create_app(database_path)) as client:
        rejected = client.post(f"/projects/{project.id}/render-jobs", json={
            "idempotency_key": "secret", "api_key": "never-persist", "scenes": [scene.model_dump(mode="json")],
            "selections": [candidate.model_dump(mode="json")],
        })
        assert rejected.status_code == 422
        _enqueue(client, project.id, scene, candidate, "same-key")
        conflicting = client.post(f"/projects/{project.id}/render-jobs", json={
            "idempotency_key": "same-key", "scenes": [scene.model_copy(update={"voice_text": "Different render"}).model_dump(mode="json")],
            "selections": [candidate.model_dump(mode="json")],
        })
        assert conflicting.status_code == 409
