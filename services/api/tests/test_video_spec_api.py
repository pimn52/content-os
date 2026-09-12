from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import AssetRepository, AudioAssetRepository, ClipRepository, Database, IPProfileRepository, ProjectRepository
from app.domain.models import (
    Asset, AudioAsset, CandidateAsset, Clip, CostCategory, IPProfile, Project, RationalFps,
    ScenePlan, SourceKind, TranscriptSegment, UsageCost, VisualIntent,
)
from app.main import create_app


def _seed(path: Path, root: Path):
    db = Database(path)
    profile = IPProfile(creator_name="Creator")
    IPProfileRepository(db).create(profile)
    project = Project(
        ip_profile_id=profile.id, title="Video", topic="Workflow",
        fps=RationalFps(numerator=30, denominator=1), created_at=datetime.now(timezone.utc),
    )
    ProjectRepository(db).create(project)
    source = root / "source.mp4"
    source.write_bytes(b"video")
    asset = Asset(
        source_file=str(source), content_hash="8" * 64, duration_ms=4_000,
        width=1080, height=1920, fps=project.fps, authorization_reference="rights",
        imported_at=datetime.now(timezone.utc),
    )
    AssetRepository(db).create(asset)
    clip = Clip(asset_id=asset.id, start_ms=250, end_ms=3_750, asset_duration_ms=4_000)
    ClipRepository(db).create(clip)
    db.close()
    scene = ScenePlan(
        project_id=project.id, scene_id="hook", order=0, purpose="hook", voice_text="Opening caption",
        duration_target_ms=3_000, visual_intent=VisualIntent(subject="creator"),
        preferred_sources=[SourceKind.USER_ASSET], fallback_sources=[SourceKind.CAPTURE],
    )
    candidate = CandidateAsset(
        scene_plan_id=scene.id, source_kind=SourceKind.USER_ASSET, asset_id=asset.id, clip_id=clip.id,
        match_score=0.9, why=["local clip"], recommended=True,
        estimated_cost=UsageCost(category=CostCategory.USER_ASSET, amount=Decimal("0"), currency="USD"),
    )
    return project, scene, candidate, clip


def test_video_spec_api_assembles_contiguous_local_timeline(tmp_path: Path) -> None:
    path = tmp_path / "spec-api.sqlite"
    project, scene, candidate, clip = _seed(path, tmp_path)
    with TestClient(create_app(path)) as client:
        response = client.post(f"/projects/{project.id}/video-spec", json={
            "scenes": [scene.model_dump(mode="json")],
            "selections": [candidate.model_dump(mode="json")],
        })
        assert response.status_code == 200
        body = response.json()
        assert body["scenes"][0]["start_frame"] == 0
        assert body["scenes"][0]["duration_frames"] == 90
        assert body["scenes"][0]["visual"]["clip_id"] == str(clip.id)
        assert body["scenes"][0]["caption"] == "Opening caption"

        explicit = client.post(f"/projects/{project.id}/video-spec", json={
            "scenes": [scene.model_dump(mode="json")],
            "selections": [candidate.model_dump(mode="json")],
            "explicit_scene_ids": [str(scene.id)],
        })
        assert explicit.status_code == 200
        assert explicit.json()["scenes"][0]["visual"]["clip_id"] == str(clip.id)


def test_video_spec_api_rejects_duplicate_or_capture_selection(tmp_path: Path) -> None:
    path = tmp_path / "spec-invalid.sqlite"
    project, scene, candidate, _ = _seed(path, tmp_path)
    duplicate = [candidate.model_dump(mode="json"), candidate.model_dump(mode="json")]
    with TestClient(create_app(path)) as client:
        assert client.post(f"/projects/{project.id}/video-spec", json={
            "scenes": [scene.model_dump(mode="json")], "selections": duplicate,
        }).status_code == 422
        capture = CandidateAsset(
            scene_plan_id=scene.id, source_kind=SourceKind.CAPTURE, match_score=0,
            why=["capture"], recommended=True, requires_capture=True,
            estimated_cost=UsageCost(category=CostCategory.CAPTURE),
        )
        response = client.post(f"/projects/{project.id}/video-spec", json={
            "scenes": [scene.model_dump(mode="json")], "selections": [capture.model_dump(mode="json")],
        })
        assert response.status_code == 422
        assert "requires capture" in response.json()["detail"]


def test_video_spec_api_attaches_persisted_narration_asset(tmp_path: Path) -> None:
    path = tmp_path / "spec-audio.sqlite"
    project, scene, candidate, _ = _seed(path, tmp_path)
    audio_path = tmp_path / "voice.wav"
    audio_path.write_bytes(b"voice")
    db = Database(path)
    audio = AudioAsset(
        source_kind="user_asset", source_file=str(audio_path), content_hash="f" * 64,
        duration_ms=4_000, sample_rate=48_000, channels=1, authorization_reference="creator-voice", imported_at=datetime.now(timezone.utc),
    )
    AudioAssetRepository(db).create(audio)
    db.close()
    with TestClient(create_app(path)) as client:
        response = client.post(f"/projects/{project.id}/video-spec", json={
            "scenes": [scene.model_dump(mode="json")],
            "selections": [candidate.model_dump(mode="json")],
            "narration_asset_ids": {str(scene.id): str(audio.id)},
        })
    assert response.status_code == 200
    assert response.json()["scenes"][0]["narration_asset_id"] == str(audio.id)


def test_video_spec_api_assembles_a_timed_master_narration(tmp_path: Path) -> None:
    path = tmp_path / "spec-master-audio.sqlite"
    project, scene, candidate, _ = _seed(path, tmp_path)
    audio_path = tmp_path / "full-voice.wav"
    audio_path.write_bytes(b"voice")
    db = Database(path)
    audio = AudioAsset(
        source_kind="user_asset", source_file=str(audio_path), content_hash="b" * 64,
        duration_ms=3_000, sample_rate=48_000, channels=1, authorization_reference="creator-voice", imported_at=datetime.now(timezone.utc),
        transcript_segments=[TranscriptSegment(start_ms=0, end_ms=3_000, text="Opening caption")],
        transcript_source="provided-vtt",
    )
    AudioAssetRepository(db).create(audio)
    db.close()
    with TestClient(create_app(path)) as client:
        response = client.post(f"/projects/{project.id}/video-spec", json={
            "scenes": [scene.model_dump(mode="json")],
            "selections": [candidate.model_dump(mode="json")],
            "master_narration_asset_id": str(audio.id),
            "narration_required": True,
        })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["master_narration"]["audio_asset_id"] == str(audio.id)
    assert body["master_narration"]["transcript_source"] == "provided-vtt"
    assert body["scenes"][0]["narration_start_ms"] == 0
    assert body["scenes"][0]["narration_end_ms"] == 3_000


def test_video_spec_api_rejects_new_script_without_narration_when_requested(tmp_path: Path) -> None:
    path = tmp_path / "spec-narration-required.sqlite"
    project, scene, candidate, _ = _seed(path, tmp_path)
    with TestClient(create_app(path)) as client:
        response = client.post(f"/projects/{project.id}/video-spec", json={
            "scenes": [scene.model_dump(mode="json")],
            "selections": [candidate.model_dump(mode="json")],
            "narration_required": True,
        })
    assert response.status_code == 422
    assert "no narration audio" in response.json()["detail"]
