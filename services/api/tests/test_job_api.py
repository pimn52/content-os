from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import AssetRepository, Database
from app.domain.models import Asset, RationalFps
from app.main import create_app


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
