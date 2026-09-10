from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import AssetRepository, ClipRepository, Database, IPProfileRepository, ProjectRepository
from app.domain.models import Asset, Clip, IPProfile, Project, ProjectFormat, RationalFps, ScenePlan, SourceKind, VisualIntent
from app.main import create_app
from app.providers.embedding import EmbeddingBatch, EmbeddingHTTPResponse, OpenAICompatibleEmbeddingProvider
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


def test_asset_route_api_returns_structured_optional_shoot_list_for_capture_gap(tmp_path: Path) -> None:
    path = tmp_path / "route-shoot-list.sqlite"
    project, _, scene = _seed(path, tmp_path)

    class Provider:
        def embed(self, texts):
            return EmbeddingBatch(((1.0, 0.0),))

    with TestClient(create_app(path, embedding_provider=Provider())) as client:
        response = client.post(
            f"/projects/{project.id}/asset-routes",
            json={"scenes": [scene.model_dump(mode="json")], "capture_gap_threshold": 1.0},
        )

    assert response.status_code == 200
    shoot_list = response.json()[0]["shoot_list"]
    assert shoot_list == [{
        "scene_plan_id": str(scene.id),
        "scene_id": "scene-1",
        "what_to_shoot": "拍摄creator，operating software。",
        "framing": "close",
        "duration_ms": 3_000,
        "requires_speaking": False,
        "speaking_note": "不需要说话：只补画面；若要新增文案，请另行上传/绑定对应授权旁白。",
        "fallback": "拒绝补拍时不生成外部素材；若无可用画面，保留明确素材缺口。",
    }]


def test_shoot_task_confirmation_is_idempotent_and_can_be_dismissed(tmp_path: Path) -> None:
    path = tmp_path / "shoot-task.sqlite"
    project, _, scene = _seed(path, tmp_path)
    shoot_task = {
        "scene_plan_id": str(scene.id),
        "scene_id": scene.scene_id,
        "what_to_shoot": "拍摄creator，operating software。",
        "framing": "close",
        "duration_ms": 3_000,
        "requires_speaking": False,
    }

    with TestClient(create_app(path)) as client:
        draft = client.put(f"/projects/{project.id}/draft", json={"scenes": [scene.model_dump(mode="json")]})
        first = client.post(f"/projects/{project.id}/shoot-tasks", json=shoot_task)
        repeated = client.post(f"/projects/{project.id}/shoot-tasks", json=shoot_task)
        dismissed = client.patch(f"/shoot-tasks/{first.json()['id']}", json={"status": "dismissed"})
        restored = client.post(f"/projects/{project.id}/shoot-tasks", json=shoot_task)
        listed = client.get(f"/projects/{project.id}/shoot-tasks")

    assert draft.status_code == 200
    assert first.status_code == 201
    assert repeated.status_code == 201 and repeated.json()["id"] == first.json()["id"]
    assert dismissed.status_code == 200 and dismissed.json()["status"] == "dismissed"
    assert restored.status_code == 201 and restored.json()["status"] == "confirmed"
    assert listed.status_code == 200 and listed.json()[0]["status"] == "confirmed"


def test_asset_route_api_explicit_lexical_mode_keeps_basis_traceable(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "route-lexical.sqlite"
    project, clip, scene = _seed(path, tmp_path)
    db = Database(path)
    asset = AssetRepository(db).get(clip.asset_id)
    assert asset is not None
    AssetRepository(db).update(asset.model_copy(update={"metadata": {"r1_usage": "production"}}))
    db.close()
    monkeypatch.setenv("CONTENT_OS_RETRIEVAL_MODE", "lexical")

    with TestClient(create_app(path)) as client:
        response = client.post(f"/projects/{project.id}/asset-routes", json={"scenes": [scene.model_dump(mode="json")]})

    assert response.status_code == 200
    candidate = response.json()[0]["candidates"][0]
    assert candidate["clip_id"] == str(clip.id)
    assert "lexical text overlap" in candidate["why"][0]


def test_asset_route_api_rejects_cross_project_scene(tmp_path: Path) -> None:
    path = tmp_path / "route-project.sqlite"
    project, _, scene = _seed(path, tmp_path)
    foreign = scene.model_copy(update={"project_id": scene.id})
    with TestClient(create_app(path, embedding_provider=object())) as client:
        response = client.post(f"/projects/{project.id}/asset-routes", json={"scenes": [foreign.model_dump(mode="json")]})
        assert response.status_code == 422


def test_runtime_embedding_route_reconciles_provider_call(tmp_path: Path) -> None:
    path = tmp_path / "route-budget.sqlite"
    project, clip, scene = _seed(path, tmp_path)

    class Transport:
        def post(self, *args):
            return EmbeddingHTTPResponse(200, b'{"data":[{"index":0,"embedding":[1.0,0.0]}]}', {})

    provider = OpenAICompatibleEmbeddingProvider("runtime-key", transport=Transport())
    with TestClient(create_app(path, embedding_provider=provider)) as client:
        assert client.put("/budget", json={"allow_unknown_cost": True}).status_code == 200
        response = client.post(f"/projects/{project.id}/asset-routes", json={"scenes": [scene.model_dump(mode="json")]})
        assert response.status_code == 200
        records = client.get(f"/projects/{project.id}/provider-calls").json()
        assert len(records) == 1
        assert records[0]["operation"] == "embedding"
        assert records[0]["status"] == "completed"
        assert records[0]["usage_observable"] is False
