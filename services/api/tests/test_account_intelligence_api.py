from pathlib import Path

from fastapi.testclient import TestClient

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
