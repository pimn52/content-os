from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import AssetRepository, ClipRepository, Database, IPProfileRepository, ProjectRepository
from app.domain.models import Asset, Clip, IPProfile, Project, ProjectFormat, RationalFps, ScenePlan, SourceKind, VisualIntent
from app.main import create_app
from app.providers.embedding import EmbeddingBatch
from app.search import ClipSearchService


def _seed(path: Path, root: Path):
    db = Database(path)
    profile = IPProfile(creator_name="Creator")
    IPProfileRepository(db).create(profile)
    project = Project(
        ip_profile_id=profile.id, title="Tutorial", topic="Software workflow",
        fps=RationalFps(numerator=30, denominator=1), created_at=datetime.now(timezone.utc),
    )
    ProjectRepository(db).create(project)
    source = root / "creator.mp4"
    source.write_bytes(b"video")
    asset = Asset(
        source_file=str(source), content_hash="9" * 64, duration_ms=5_000, width=1080, height=1920,
        fps=project.fps, authorization_reference="rights", imported_at=datetime.now(timezone.utc),
    )
    AssetRepository(db).create(asset)
    clip = Clip(
        asset_id=asset.id, start_ms=0, end_ms=5_000, asset_duration_ms=5_000,
        transcript="本人坐在电脑前操作软件", visual_description="creator at computer desk",
        action="operating software", objects=["computer"], orientation=ProjectFormat.VERTICAL,
        quality_score=0.9, talking_candidate=True,
    )
    ClipRepository(db).create(clip)
    ClipSearchService(db).index_clip(clip, (1.0, 0.0))
    db.close()
    scene = ScenePlan(
        project_id=project.id, scene_id="scene-1", order=0, purpose="explain",
        voice_text="本人坐在电脑前操作软件", duration_target_ms=3_000,
        visual_intent=VisualIntent(subject="creator", action="operating software", framing="close"),
        preferred_sources=[SourceKind.USER_ASSET], fallback_sources=[SourceKind.CAPTURE],
    )
    return project, clip, scene


def test_asset_route_api_returns_real_continuous_clip(tmp_path: Path) -> None:
    path = tmp_path / "route-api.sqlite"
    project, clip, scene = _seed(path, tmp_path)

    class Provider:
        def embed(self, texts):
            return EmbeddingBatch(((1.0, 0.0),))

    with TestClient(create_app(path, embedding_provider=Provider())) as client:
        response = client.post(f"/projects/{project.id}/asset-routes", json={"scenes": [scene.model_dump(mode="json")]})
        assert response.status_code == 200
        candidate = response.json()[0]["candidates"][0]
        assert candidate["clip_id"] == str(clip.id)
        assert candidate["recommended"] is True
        assert candidate["estimated_cost"]["amount"] == "0"


def test_asset_route_api_rejects_cross_project_scene(tmp_path: Path) -> None:
    path = tmp_path / "route-project.sqlite"
    project, _, scene = _seed(path, tmp_path)
    foreign = scene.model_copy(update={"project_id": scene.id})
    with TestClient(create_app(path, embedding_provider=object())) as client:
        response = client.post(f"/projects/{project.id}/asset-routes", json={"scenes": [foreign.model_dump(mode="json")]})
        assert response.status_code == 422
