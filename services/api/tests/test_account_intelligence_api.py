from datetime import datetime, timezone
from pathlib import Path
from fastapi.testclient import TestClient

from app.db import AssetRepository, Database, IPProfileRepository
from app.domain.models import Asset, IPProfile, RationalFps
from app.main import create_app


def test_read_only_account_and_historical_content_are_local_and_idempotent(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "intelligence.sqlite3")) as client:
        account_payload = {
            "provider": "youtube",
            "account_external_id": "channel-123",
            "display_name": "Creator channel",
            "connected_at": "2026-09-09T08:00:00Z",
        }
        account = client.post("/account-connections", json=account_payload)
        assert account.status_code == 201
        account_body = account.json()
        assert account_body["read_only"] is True

        repeated_account = client.post("/account-connections", json=account_payload)
        assert repeated_account.status_code == 201
        assert repeated_account.json()["id"] == account_body["id"]
        assert client.post("/account-connections", json={**account_payload, "display_name": "Renamed"}).status_code == 409
        assert client.post("/account-connections", json={**account_payload, "access_token": "must-not-persist"}).status_code == 422

        history_payload = {
            "account_connection_id": account_body["id"],
            "external_id": "video-001",
            "title": "A historical video",
            "published_at": "2026-08-01T08:00:00Z",
            "description": "Imported read-only metadata.",
            "transcript": "The historical transcript remains a source, not a new script.",
            "metrics": {"views": 1200},
        }
        history = client.post("/historical-content", json=history_payload)
        assert history.status_code == 201
        history_body = history.json()
        repeated_history = client.post("/historical-content", json=history_payload)
        assert repeated_history.status_code == 201
        assert repeated_history.json()["id"] == history_body["id"]
        assert client.post("/historical-content", json={**history_payload, "title": "Changed"}).status_code == 409
        assert client.get(f"/historical-content?account_connection_id={account_body['id']}").json()[0]["external_id"] == "video-001"
        assert client.post("/historical-content", json={**history_payload, "account_connection_id": "00000000-0000-0000-0000-000000000000"}).status_code == 404

        opportunity = client.post(
            "/opportunities",
            json={
                "source_type": "historical_content",
                "source_ref": f"historical_content:{history_body['id']}",
                "title": "A historical-derived opportunity",
                "observed_at": "2026-08-01T08:00:00Z",
                "fit_reason": "The historical transcript is a direct source for the proposed angle.",
                "angle": "Reframe the old point for the current audience.",
                "evidence_refs": [f"historical_content:{history_body['id']}"],
            },
        )
        assert opportunity.status_code == 201


def test_historical_content_can_link_only_a_same_ip_local_asset(tmp_path: Path) -> None:
    path = tmp_path / "historical-asset-link.sqlite3"
    now = datetime.now(timezone.utc)
    with Database(path) as db:
        profile = IPProfileRepository(db).create(IPProfile(creator_name="Creator"))
        other = IPProfileRepository(db).create(IPProfile(creator_name="Other"))
        asset = AssetRepository(db).create(Asset(
            source_file="creator-source.media", content_hash="a" * 64, duration_ms=60_000,
            width=1280, height=720, fps=RationalFps(numerator=30, denominator=1),
            authorization_reference="creator-intake", imported_at=now,
            metadata={"ip_profile_id": str(profile.id), "lifecycle_state": "active"},
        ))
        other_asset = AssetRepository(db).create(Asset(
            source_file="other-source.media", content_hash="b" * 64, duration_ms=60_000,
            width=1280, height=720, fps=RationalFps(numerator=30, denominator=1),
            authorization_reference="creator-intake", imported_at=now,
            metadata={"ip_profile_id": str(other.id), "lifecycle_state": "active"},
        ))
    with TestClient(create_app(path)) as client:
        account = client.post("/account-connections", json={
            "ip_profile_id": str(profile.id), "provider": "bilibili",
            "account_external_id": "creator-channel", "connected_at": now.isoformat(),
        }).json()
        history = client.post("/historical-content", json={
            "account_connection_id": account["id"], "external_id": "BV-source",
            "title": "Original publication", "published_at": now.isoformat(),
            "availability": "available", "content_url": "https://www.bilibili.com/video/BV-source/",
            "evidence_refs": ["https://example.invalid/archive"],
        })
        assert history.status_code == 201, history.text
        history = history.json()
        linked = client.patch(f"/historical-content/{history['id']}/asset-link", json={
            "asset_id": str(asset.id), "content_url": "https://www.bilibili.com/video/BV-source/",
            "availability": "available", "evidence_refs": ["https://example.invalid/archive"],
        })
        assert linked.status_code == 200, linked.text
        assert linked.json()["asset_id"] == str(asset.id)
        assert client.get(f"/historical-content?asset_id={asset.id}").json()[0]["id"] == history["id"]
        rejected = client.patch(f"/historical-content/{history['id']}/asset-link", json={
            "asset_id": str(other_asset.id), "availability": "available",
        })
        assert rejected.status_code == 422
        assert rejected.json()["detail"] == "historical content asset must belong to the account IP"
