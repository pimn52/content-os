from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import Database, IPProfileRepository, ProjectRepository
from app.domain.models import IPProfile, Project, RationalFps, ScenePlan, SourceKind, VisualIntent
from app.main import _scene_planning_context, create_app
from app.providers.scene_planner import ScenePlanResult


def _project(path: Path) -> Project:
    db = Database(path)
    profile = IPProfile(creator_name="Creator")
    IPProfileRepository(db).create(profile)
    project = Project(
        ip_profile_id=profile.id,
        title="Opportunity test",
        topic="Useful topic",
        fps=RationalFps(numerator=30, denominator=1),
        created_at=datetime.now(timezone.utc),
    )
    ProjectRepository(db).create(project)
    db.close()
    return project


def _payload() -> dict[str, object]:
    return {
        "source_type": "manual",
        "source_ref": "user:idea:1",
        "title": "A sourced topic",
        "observed_at": "2026-09-09T08:00:00Z",
        "fit_reason": "Matches the creator's confirmed audience and knowledge.",
        "angle": "Explain the trade-off with one concrete example.",
        "uncertainty": "Needs a current example from the creator.",
        "evidence_refs": ["note:user:idea:1"],
    }


def test_opportunity_input_is_traceable_and_idempotent(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "opportunities.sqlite3")) as client:
        first = client.post("/opportunities", json=_payload())
        assert first.status_code == 201
        opportunity = first.json()
        assert opportunity["dedupe_key"] == "manual:user:idea:1"
        assert opportunity["status"] == "new"

        repeated = client.post("/opportunities", json=_payload())
        assert repeated.status_code == 201
        assert repeated.json()["id"] == opportunity["id"]

        changed = client.post("/opportunities", json={**_payload(), "angle": "A different angle."})
        assert changed.status_code == 409

        used = client.patch(f"/opportunities/{opportunity['id']}/status", json={"status": "used"})
        assert used.status_code == 200
        assert used.json()["status"] == "used"
        assert client.get("/opportunities?status=used").json()[0]["id"] == opportunity["id"]
        assert client.get("/opportunities?status=dismissed").json() == []


def test_scene_planning_context_includes_active_opportunity_evidence(tmp_path: Path) -> None:
    path = tmp_path / "opportunity-context.sqlite3"
    project = _project(path)
    with TestClient(create_app(path)) as client:
        created = client.post("/opportunities", json=_payload())
        assert created.status_code == 201
        opportunity_id = created.json()["id"]
    db = Database(path)
    try:
        context, refs = _scene_planning_context(db, project)
        assert f"opportunity:{opportunity_id}" in refs
        assert "note:user:idea:1" in refs
        assert context["opportunities"][0]["title"] == "A sourced topic"
    finally:
        db.close()


def test_scene_plan_response_carries_opportunity_evidence(tmp_path: Path) -> None:
    path = tmp_path / "opportunity-scene-plan.sqlite3"
    project = _project(path)

    class Planner:
        def plan(self, candidate, *, script=None, topic=None):
            scene = ScenePlan(
                project_id=candidate.id,
                scene_id="hook",
                order=0,
                purpose="hook",
                voice_text="A sourced opening.",
                duration_target_ms=3_000,
                visual_intent=VisualIntent(subject="creator"),
                preferred_sources=[SourceKind.USER_ASSET],
            )
            return ScenePlanResult(candidate.id, (scene,))

    with TestClient(create_app(path, scene_planner=Planner())) as client:
        created = client.post("/opportunities", json=_payload())
        opportunity_id = created.json()["id"]
        response = client.post(f"/projects/{project.id}/scene-plan", json={"topic": "A sourced topic"})
        assert response.status_code == 200
        assert f"opportunity:{opportunity_id}" in response.json()["scenes"][0]["evidence_refs"]
