import hashlib
from pathlib import Path
from uuid import uuid4
import wave

from fastapi.testclient import TestClient

from app.db import AssetRepository, ClipRepository, Database, IPProfileRepository, JobRepository, ProjectRepository
from app.domain.models import Asset, Clip, IPProfile, Project, RationalFps
from app.main import create_app


def test_voice_and_talking_profiles_require_consent_and_existing_reference_clips(tmp_path: Path) -> None:
    database_path = tmp_path / "voice.sqlite3"
    with Database(database_path) as db:
        ip=IPProfile(creator_name="Creator")
        IPProfileRepository(db).create(ip)
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
        "ip_profile_id":str(ip.id),
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


def test_reference_candidates_need_explicit_confirmation_before_stable_default_reuse(tmp_path: Path) -> None:
    database_path = tmp_path / "voice-reference.sqlite3"
    source = tmp_path / "reference.mp4"
    source.write_bytes(b"authorized source bytes")
    with Database(database_path) as db:
        ip = IPProfile(creator_name="Creator")
        IPProfileRepository(db).create(ip)
        project = Project(ip_profile_id=ip.id, title="Voice", topic="New copy", fps=RationalFps(numerator=25, denominator=1), created_at="2026-09-09T08:00:00Z")
        ProjectRepository(db).create(project)
        asset = Asset(source_file=str(source), content_hash=hashlib.sha256(source.read_bytes()).hexdigest(), duration_ms=20_000, width=720, height=1280, fps=RationalFps(numerator=25, denominator=1), has_audio=True, authorization_reference="source-rights", imported_at="2026-09-09T08:00:00Z")
        AssetRepository(db).create(asset)
        analysis_audio = tmp_path / "assets" / "audio" / f"{asset.content_hash}-16000-1.wav"
        analysis_audio.parent.mkdir(parents=True)
        with wave.open(str(analysis_audio), "wb") as output:
            output.setnchannels(1); output.setsampwidth(2); output.setframerate(16_000)
            output.writeframes((8_000).to_bytes(2, "little", signed=True) * 16_000 * 20)
        clip = Clip(asset_id=asset.id, start_ms=0, end_ms=8_000, asset_duration_ms=20_000, voice_candidate=True, transcript_segments=[
            {"start_ms": 0, "end_ms": 2_000, "text": "先说明白"},
            {"start_ms": 2_100, "end_ms": 4_500, "text": "再讲重点"},
            {"start_ms": 4_600, "end_ms": 7_000, "text": "最后落下"},
        ], transcript_source="faster-whisper:small")
        ClipRepository(db).create(clip)
        discovered_clip = Clip(asset_id=asset.id, start_ms=10_000, end_ms=18_000, asset_duration_ms=20_000,
            transcript="资产其他时间的完整句子", transcript_segments=[
                {"start_ms": 10_000, "end_ms": 18_000, "text": "资产其他时间的完整句子"},
            ], transcript_source="faster-whisper:small")
        ClipRepository(db).create(discovered_clip)
    profile = {"ip_profile_id":str(ip.id),"name": "Creator voice", "provider": "omnivoice", "provider_profile_id": "creator-reference", "reference_clip_ids": [str(clip.id)], "consent": {"subject_name": "Creator", "basis": "self", "confirmed": True, "confirmed_at": "2026-09-09T08:00:00Z"}, "created_at": "2026-09-09T08:00:00Z"}
    with TestClient(create_app(database_path)) as client:
        created = client.post("/voice-profiles", json=profile)
        assert created.status_code == 201
        profile_id = created.json()["id"]
        response = client.get(f"/voice-profiles/{profile_id}/reference-windows")
        assert response.status_code == 200 and response.json()
        choices = response.json()
        assert any(item["selection"]["clip_id"] == str(discovered_clip.id) for item in choices)
        assert all(item["selection"]["source_timebase"] == "analysis_wav" for item in choices)
        assert all(item["source_timebase"] == "analysis_wav" for item in choices)
        assert all(item["source_asset_content_hash"] == asset.content_hash for item in choices)
        derived_hash = hashlib.sha256(analysis_audio.read_bytes()).hexdigest()
        assert all(item["reference_audio_content_hash"] == derived_hash for item in choices)
        selected = choices[0]["selection"]
        assert choices[0]["review_required"] is True and "speech_quality" not in choices[0]
        request = {"idempotency_key": "selected-ref", "voice_profile_id": profile_id,
            "text": "任意，新文案", "delivery_text": "任意新文案", "authorization_reference": "voice-consent",
            "reference_window": selected}
        assert client.post(f"/projects/{project.id}/voice-jobs", json=request).status_code == 422
        assert client.post(f"/projects/{project.id}/voice-jobs", json={**request,
            "idempotency_key": "no-confirmed-default", "reference_window": None}).status_code == 422

        ref_text = "先说明白事实，再给出清晰的结论。"
        confirmation = {
            "complete_sentence_confirmed": True, "exact_ref_text_confirmed": True,
            "voice_suitability_confirmed": True,
            "voice_suitability_basis": "本人单独清晰说话，无背景音乐，适合作为长期声线参考",
            "reference_window": selected, "ref_text": ref_text,
        }
        assert client.put(f"/voice-profiles/{profile_id}/reference-clip", json={
            **confirmation, "voice_suitability_confirmed": False,
        }).status_code == 422
        saved = client.put(f"/voice-profiles/{profile_id}/reference-clip", json=confirmation)
        assert saved.status_code == 200, saved.json()
        confirmed_id = saved.json()["reference_clip_ids"][0]
        with Database(database_path) as db:
            confirmed = ClipRepository(db).get(confirmed_id)
            assert confirmed is not None and confirmed.transcript == ref_text
            assert confirmed.transcript_source.startswith("owner-confirmed-reference:")
            assert confirmed.reference_source_timebase == "analysis_wav"
            assert confirmed.reference_source_file == analysis_audio.relative_to(tmp_path).as_posix()
            assert confirmed.reference_source_content_hash == derived_hash
            assert confirmed.reference_source_duration_ms == 20_000

        choices_after = client.get(f"/voice-profiles/{profile_id}/reference-windows").json()
        assert choices_after and all(item["review_required"] is True for item in choices_after)
        default_request = {**request, "idempotency_key": "confirmed-default", "reference_window": None}
        first = client.post(f"/projects/{project.id}/voice-jobs", json=default_request)
        assert first.status_code == 201, first.json()
        with Database(database_path) as db:
            stored = JobRepository(db).get(first.json()["id"]).payload
            assert str(stored.reference_window.clip_id) == confirmed_id
            assert stored.reference_window.start_ms == selected["start_ms"]
            assert stored.reference_window.end_ms == selected["end_ms"]
            assert stored.reference_window.source_timebase == "analysis_wav"
            assert stored.reference_window.source_content_hash == derived_hash
            assert stored.reference_window.authorization_content_hash == asset.content_hash
            assert stored.reference_window.transcript_sha256 == hashlib.sha256(ref_text.encode()).hexdigest()
            assert stored.text == request["text"] and stored.delivery_text == request["delivery_text"]
        repeat = client.post(f"/projects/{project.id}/voice-jobs", json=default_request)
        assert repeat.status_code == 201 and repeat.json()["id"] == first.json()["id"]
