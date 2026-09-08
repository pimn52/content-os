from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import AssetRepository, ClipRepository, Database
from app.domain.models import Asset, Clip, ProjectFormat, RationalFps
from app.main import create_app


def _seed(tmp_path: Path):
    db_path = tmp_path / "library.sqlite"
    source = tmp_path / "素材 with spaces.mp4"
    source.write_bytes(b"video bytes")
    db = Database(db_path)
    asset = Asset(source_file=str(source), content_hash="f" * 64, duration_ms=2_000, width=640, height=360,
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
