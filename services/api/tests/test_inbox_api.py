from datetime import datetime, timezone
import hashlib
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import AssetRepository, Database
from app.domain.models import Asset, RationalFps
from app.main import create_app


def test_inbox_scan_enqueues_local_analysis_without_claiming_completion(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    first_path = inbox / "first.mp4"
    second_path = inbox / "second.mp4"
    first_path.write_bytes(b"first")
    second_path.write_bytes(b"second")
    database_path = tmp_path / "inbox.sqlite3"
    existing = Asset(
        source_file=str(first_path),
        content_hash="a" * 64,
        duration_ms=1_000,
        width=720,
        height=1_280,
        fps=RationalFps(numerator=30, denominator=1),
        authorization_reference="local-reference",
        imported_at=datetime.now(timezone.utc),
    )
    with Database(database_path) as db:
        AssetRepository(db).create(existing)

    class FakeImporter:
        def __init__(self, db, data_root):
            self.db = db

        def import_path(self, path, authorization_reference, *, source_kind):
            repository = AssetRepository(self.db)
            for asset in repository.list():
                if Path(asset.source_file).name == path.name:
                    return asset
            asset = Asset(
                source_file=str(path),
                content_hash=hashlib.sha256(path.name.encode()).hexdigest(),
                duration_ms=2_000,
                width=1_280,
                height=720,
                fps=RationalFps(numerator=30, denominator=1),
                authorization_reference=authorization_reference,
                imported_at=datetime.now(timezone.utc),
            )
            repository.create(asset)
            return asset

    with TestClient(create_app(database_path, media_importer_factory=FakeImporter)) as client:
        payload = {
            "source_path": str(inbox),
            "authorization_reference": "local-reference",
            "source_kind": "user_asset",
        }
        first = client.post("/inbox/scan", json=payload)
        assert first.status_code == 200
        assert first.json()["discovered"] == 2
        assert first.json()["imported"] == 1
        assert first.json()["existing"] == 1
        assert [job["type"] for job in first.json()["analysis_jobs"]] == ["analyze_asset"]
        assert first.json()["analysis_jobs"][0]["target_asset_id"] != str(existing.id)
        assert first.json()["errors"] == []

        second = client.post("/inbox/scan", json=payload)
        assert second.status_code == 200
        assert second.json()["discovered"] == 2
        assert second.json()["imported"] == 0
        assert second.json()["existing"] == 2
        assert second.json()["analysis_jobs"] == []
        assert second.json()["assets"]

        third_path = inbox / "third.mp4"
        third_path.write_bytes(b"third")
        disabled = client.post("/inbox/scan", json={**payload, "enqueue_analysis": False})
        assert disabled.status_code == 200
        assert disabled.json()["imported"] == 1
        assert disabled.json()["analysis_jobs"] == []
