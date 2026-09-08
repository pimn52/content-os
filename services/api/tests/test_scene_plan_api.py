from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Database, IPProfileRepository, ProjectRepository
from app.domain.models import IPProfile, Project, RationalFps, ScenePlan, SourceKind, VisualIntent
from app.main import create_app
from app.providers.scene_planner import ScenePlanResult


def _project(path: Path) -> Project:
    db = Database(path)
    profile = IPProfile(creator_name="Creator")
    IPProfileRepository(db).create(profile)
    project = Project(
        ip_profile_id=profile.id, title="Practical tutorial", topic="When to avoid vibe coding",
        fps=RationalFps(numerator=30, denominator=1), created_at=datetime.now(timezone.utc),
    )
    ProjectRepository(db).create(project)
    db.close()
    return project


def test_scene_plan_api_binds_project_and_returns_structured_scenes(tmp_path: Path) -> None:
    path = tmp_path / "scene-plan.sqlite"
    project = _project(path)
    seen = []

    class Planner:
        def plan(self, candidate, *, script=None, topic=None):
            seen.append((candidate.id, script, topic))
            scene = ScenePlan(
                project_id=candidate.id, scene_id="hook", order=0, purpose="hook",
                voice_text="Vibe coding is not always the answer.", duration_target_ms=3_000,
                visual_intent=VisualIntent(subject="creator", action="using a computer"),
                preferred_sources=[SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET],
                fallback_sources=[SourceKind.TYPOGRAPHY], caption_emphasis=["not always"],
            )
            return ScenePlanResult(candidate.id, (scene,))

    with TestClient(create_app(path, scene_planner=Planner())) as client:
        response = client.post(f"/projects/{project.id}/scene-plan", json={"script": "Opening line", "topic": "Tradeoffs"})
        assert response.status_code == 200
        assert response.json()["project_id"] == str(project.id)
        assert response.json()["scenes"][0]["preferred_sources"][0] == "user_asset"
        assert seen == [(project.id, "Opening line", "Tradeoffs")]


def test_scene_plan_api_missing_project_config_and_secret_field(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "scene-plan-config.sqlite"
    project = _project(path)
    monkeypatch.delenv("CONTENT_OS_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with TestClient(create_app(path)) as client:
        assert client.post(f"/projects/{uuid4()}/scene-plan", json={}).status_code == 404
        assert client.post(f"/projects/{project.id}/scene-plan", json={}).status_code == 503
        assert client.post(f"/projects/{project.id}/scene-plan", json={"api_key": "must-not-persist"}).status_code == 422
