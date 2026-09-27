import hashlib
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import AssetRepository, ClipRepository, Database, IPProfileRepository, JobRepository, ProjectRepository
from app.domain.models import Asset, Clip, IPProfile, Project, RationalFps
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


def test_voice_reference_choice_round_trips_into_normal_job_and_rejects_stale_choice(tmp_path: Path) -> None:
    database_path = tmp_path / "voice-reference.sqlite3"
    source = tmp_path / "reference.mp4"
    source.write_bytes(b"authorized source bytes")
    with Database(database_path) as db:
        ip = IPProfile(creator_name="Creator")
        IPProfileRepository(db).create(ip)
        project = Project(ip_profile_id=ip.id, title="Voice", topic="New copy", fps=RationalFps(numerator=25, denominator=1), created_at="2026-09-09T08:00:00Z")
        ProjectRepository(db).create(project)
        asset = Asset(source_file=str(source), content_hash=hashlib.sha256(source.read_bytes()).hexdigest(), duration_ms=12_000, width=720, height=1280, fps=RationalFps(numerator=25, denominator=1), authorization_reference="source-rights", imported_at="2026-09-09T08:00:00Z")
        AssetRepository(db).create(asset)
        clip = Clip(asset_id=asset.id, start_ms=0, end_ms=12_000, asset_duration_ms=12_000, voice_candidate=True, transcript_segments=[
            {"start_ms": 0, "end_ms": 2_000, "text": "先说明白"},
            {"start_ms": 2_100, "end_ms": 4_500, "text": "再讲重点"},
            {"start_ms": 4_600, "end_ms": 7_000, "text": "最后落下"},
        ])
        ClipRepository(db).create(clip)
    profile = {"name": "Creator voice", "provider": "omnivoice", "provider_profile_id": "creator-reference", "reference_clip_ids": [str(clip.id)], "consent": {"subject_name": "Creator", "basis": "self", "confirmed": True, "confirmed_at": "2026-09-09T08:00:00Z"}, "created_at": "2026-09-09T08:00:00Z"}
    with TestClient(create_app(database_path)) as client:
        created = client.post("/voice-profiles", json=profile)
        assert created.status_code == 201
        profile_id = created.json()["id"]
        choices = client.get(f"/voice-profiles/{profile_id}/reference-windows")
        assert choices.status_code == 200 and choices.json()
        selected = choices.json()[0]["selection"]
        request = {"idempotency_key": "selected-ref", "voice_profile_id": profile_id, "text": "任意，新文案", "delivery_text": "任意新文案", "authorization_reference": "voice-consent", "reference_window": selected}
        first = client.post(f"/projects/{project.id}/voice-jobs", json=request)
        assert first.status_code == 201
        with Database(database_path) as db:
            stored = JobRepository(db).get(first.json()["id"]).payload
            assert stored.reference_window.model_dump(mode="json") == selected
            assert stored.text == request["text"] and stored.delivery_text == request["delivery_text"]
        repeat = client.post(f"/projects/{project.id}/voice-jobs", json=request)
        assert repeat.status_code == 201 and repeat.json()["id"] == first.json()["id"]
        assert client.post(f"/projects/{project.id}/voice-jobs", json={**request, "reference_window": {**selected, "source_content_hash": "0" * 64}}).status_code == 422
        assert client.post(f"/projects/{project.id}/voice-jobs", json={**request, "reference_window": None}).status_code == 409
        assert client.post(f"/projects/{project.id}/voice-jobs", json={**request, "delivery_text": "任意别的文案"}).status_code == 422
        assert client.post(f"/projects/{project.id}/voice-jobs", json={**request, "use_draft_performance_plan": True}).status_code == 422
