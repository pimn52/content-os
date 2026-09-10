from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import AssetRepository, ClipRepository, Database
from app.domain.models import Asset, Clip, RationalFps
from app.main import create_app


def test_voice_and_talking_profiles_require_consent_and_existing_reference_clips(tmp_path: Path) -> None:
    database_path = tmp_path / "voice.sqlite3"
    with Database(database_path) as db:
        asset = Asset(
            source_file=str(tmp_path / "reference.mp4"),
            content_hash="c" * 64,
            duration_ms=3_000,
            width=720,
            height=1_280,
            fps=RationalFps(numerator=30, denominator=1),
            authorization_reference="creator-consent-2026",
            imported_at="2026-09-09T08:00:00Z",
        )
        AssetRepository(db).create(asset)
        clip = Clip(asset_id=asset.id, start_ms=0, end_ms=2_000, asset_duration_ms=asset.duration_ms, talking_candidate=True, voice_candidate=True)
        ClipRepository(db).create(clip)

    voice_id = str(uuid4())
    voice_payload = {
        "id": voice_id,
        "name": "Creator voice",
        "provider": "deferred-provider",
        "provider_profile_id": "voice-profile-1",
        "reference_clip_ids": [str(clip.id)],
        "consent": {
            "subject_name": "Creator",
            "basis": "self",
            "confirmed": True,
            "confirmed_at": "2026-09-09T08:00:00Z",
        },
        "language": "zh",
        "created_at": "2026-09-09T08:00:00Z",
    }
    with TestClient(create_app(database_path)) as client:
        voice = client.post("/voice-profiles", json=voice_payload)
        assert voice.status_code == 201
        assert voice.json()["consent"]["confirmed"] is True
        assert client.post("/voice-profiles", json=voice_payload).json()["id"] == voice_id
        assert client.post("/voice-profiles", json={**voice_payload, "name": "Changed"}).status_code == 409
        assert client.get("/voice-profiles").json()[0]["id"] == voice_id

        talking_payload = {
            "name": "Creator talking",
            "provider": "deferred-provider",
            "provider_profile_id": None,
            "reference_clip_ids": [str(clip.id)],
            "consent": voice_payload["consent"],
            "created_at": "2026-09-09T08:00:00Z",
        }
        talking = client.post("/talking-profiles", json=talking_payload)
        assert talking.status_code == 201
        assert client.get("/talking-profiles").json()[0]["name"] == "Creator talking"

        assert client.post("/voice-profiles", json={**voice_payload, "reference_clip_ids": [str(uuid4())]}).status_code == 422
        assert client.post("/talking-profiles", json={**talking_payload, "consent": {**voice_payload["consent"], "confirmed": False}}).status_code == 422
