from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Database, IPProfileRepository, ProjectRepository
from app.domain.models import IPProfile, Project, RationalFps, ScenePlan, SourceKind, VisualIntent
from app.main import _scene_planner_from_env, create_app
from app.providers.scene_planner import OpenAICompatibleScenePlanner, ScenePlanResult, ScenePlannerHTTPResponse


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
        assert response.json()["scenes"][0]["evidence_refs"] == [f"ip_profile:{project.ip_profile_id}:v1"]
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


def test_scene_planner_runtime_protocol_is_explicitly_configurable(monkeypatch) -> None:
    monkeypatch.setenv("CONTENT_OS_LLM_API_KEY", "runtime-secret")
    monkeypatch.setenv("CONTENT_OS_LLM_BASE_URL", "https://api.moonshot.cn/v1")
    monkeypatch.setenv("CONTENT_OS_LLM_MODEL", "kimi-k3")
    monkeypatch.setenv("CONTENT_OS_LLM_PROTOCOL", "chat_completions")

    planner = _scene_planner_from_env()

    assert planner.protocol == "chat_completions"
    assert planner.model == "kimi-k3"


def _runtime_plan_response(status: int = 200) -> ScenePlannerHTTPResponse:
    if status != 200:
        return ScenePlannerHTTPResponse(status, b"{}", {})
    scene = {
        "scene_id": "runtime-hook",
        "order": 0,
        "purpose": "hook",
        "voice_text": "A runtime planner result.",
        "duration_target_ms": 3_000,
        "visual_intent": {"subject": "creator", "action": "speaking", "framing": "medium", "description": "creator speaking to camera"},
        "preferred_sources": ["user_asset"],
        "fallback_sources": ["typography"],
        "caption_emphasis": ["runtime"],
    }
    body = {"output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps({"scenes": [scene]})}]}]}
    return ScenePlannerHTTPResponse(200, json.dumps(body).encode(), {})


def test_runtime_scene_plan_is_blocked_before_provider_when_cost_is_unknown(tmp_path: Path) -> None:
    project = _project(tmp_path / "scene-plan-budget-block.sqlite")
    calls = []

    class Transport:
        def post(self, *args):
            calls.append(args)
            return _runtime_plan_response()

    planner = OpenAICompatibleScenePlanner("runtime-key", transport=Transport())
    with TestClient(create_app(tmp_path / "scene-plan-budget-block.sqlite", scene_planner=planner)) as client:
        response = client.post(f"/projects/{project.id}/scene-plan", json={"topic": "Budgeted runtime"})
        assert response.status_code == 409
        assert response.json()["detail"] == "unknown_cost"
        assert calls == []
        assert client.get(f"/projects/{project.id}/provider-calls").json() == []


def test_runtime_scene_plan_reconciles_success_and_provider_failure(tmp_path: Path) -> None:
    path = tmp_path / "scene-plan-budget-reconcile.sqlite"
    project = _project(path)

    class Transport:
        status = 200

        def post(self, *args):
            return _runtime_plan_response(self.status)

    transport = Transport()
    planner = OpenAICompatibleScenePlanner("runtime-key", transport=transport)
    with TestClient(create_app(path, scene_planner=planner)) as client:
        assert client.put("/budget", json={"allow_unknown_cost": True}).status_code == 200
        success = client.post(f"/projects/{project.id}/scene-plan", json={"topic": "Budgeted runtime"})
        assert success.status_code == 200
        records = client.get(f"/projects/{project.id}/provider-calls").json()
        assert len(records) == 1
        assert records[0]["operation"] == "scene_planning"
        assert records[0]["status"] == "completed"
        assert records[0]["actual_cost"] is None
        assert records[0]["usage_observable"] is False

        transport.status = 429
        failed = client.post(f"/projects/{project.id}/scene-plan", json={"topic": "Budgeted runtime retry"})
        assert failed.status_code == 503
        records = client.get(f"/projects/{project.id}/provider-calls").json()
        assert len(records) == 2
        assert records[1]["status"] == "failed"
        assert records[1]["error_code"] == "scene_planner_temporarily_unavailable"
