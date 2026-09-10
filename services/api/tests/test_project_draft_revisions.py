from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient

from app.domain.models import CandidateAsset, CostCategory, DraftRoute, ScenePlan, SourceKind, UsageCost, VisualIntent
from app.main import create_app
from app.providers.scene_planner import ScenePlanResult


def _scene(project_id: UUID, text: str = "A fresh creator point.") -> ScenePlan:
    return ScenePlan(
        project_id=project_id,
        scene_id="hook",
        order=0,
        purpose="hook",
        voice_text=text,
        duration_target_ms=1_000,
        visual_intent=VisualIntent(subject="creator", action="speaking"),
        preferred_sources=[SourceKind.USER_ASSET],
        fallback_sources=[SourceKind.TYPOGRAPHY],
    )


def _route(scene: ScenePlan) -> tuple[CandidateAsset, DraftRoute]:
    candidate = CandidateAsset(
        scene_plan_id=scene.id,
        source_kind=SourceKind.TYPOGRAPHY,
        match_score=0.8,
        why=["local fallback"],
        recommended=True,
        estimated_cost=UsageCost(category=CostCategory.TYPOGRAPHY),
    )
    return candidate, DraftRoute(scene_plan_id=scene.id, candidates=[candidate])


def test_script_edit_invalidates_old_scene_routes_and_records_history(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "draft-revisions.sqlite")) as client:
        project = client.post("/projects", json={"title": "Revision", "topic": "Original"}).json()
        project_id = UUID(project["id"])
        scene = _scene(project_id)
        candidate, route = _route(scene)
        initial = client.put(f"/projects/{project_id}/draft", json={
            "script": "Original script",
            "topic": "Original",
            "scenes": [scene.model_dump(mode="json")],
            "routes": [route.model_dump(mode="json")],
            "confirmed": [candidate.model_dump(mode="json")],
        })
        assert initial.status_code == 200, initial.text
        assert initial.json()["script_revision"] == 1

        # A stale tab may replay its prior plan and candidates, but changing
        # the script must make the server discard every derived artifact.
        changed = client.put(f"/projects/{project_id}/draft", json={
            "script": "A different new script",
            "topic": "Original",
            "scenes": [scene.model_dump(mode="json")],
            "routes": [route.model_dump(mode="json")],
            "confirmed": [candidate.model_dump(mode="json")],
        })
        assert changed.status_code == 200, changed.text
        body = changed.json()
        assert body["script"] == "A different new script"
        assert body["scenes"] == body["routes"] == body["confirmed"] == []
        assert body["video_spec"] is None
        assert body["script_revision"] == 2
        assert body["invalidation_reasons"] == ["script_changed"]

        revisions = client.get(f"/projects/{project_id}/draft/revisions")
        assert revisions.status_code == 200
        assert [item["version"] for item in revisions.json()] == [1, 2]
        assert [item["script"] for item in revisions.json()] == ["Original script", "A different new script"]


def test_ip_update_invalidates_existing_project_draft_server_side(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "profile-invalidates.sqlite")) as client:
        project = client.post("/projects", json={"title": "IP revision", "topic": "Topic"}).json()
        project_id = UUID(project["id"])
        scene = _scene(project_id)
        candidate, route = _route(scene)
        saved = client.put(f"/projects/{project_id}/draft", json={
            "script": "Keep this editable copy",
            "topic": "Topic",
            "scenes": [scene.model_dump(mode="json")],
            "routes": [route.model_dump(mode="json")],
            "confirmed": [candidate.model_dump(mode="json")],
        })
        assert saved.status_code == 200

        profile = client.get("/ip-profile").json()
        updated = client.put("/ip-profile", json={
            **{key: profile[key] for key in ("creator_name", "domains", "topics", "knowledge", "opinions", "vocabulary", "style_notes", "avoided_expressions", "boundaries", "common_hooks", "metadata")},
            "audience": "builders who want concrete workflows",
        })
        assert updated.status_code == 200

        draft = client.get(f"/projects/{project_id}/draft").json()
        assert draft["script"] == "Keep this editable copy"
        assert draft["scenes"] == draft["routes"] == draft["confirmed"] == []
        assert draft["invalidation_reasons"] == ["ip_profile_changed"]
        assert draft["script_revision"] == 2
        assert draft["ip_profile_version"] == 2


def test_persisted_scene_plan_becomes_editable_generated_copy(tmp_path: Path) -> None:
    class Planner:
        def plan(self, project, *, script=None, topic=None):
            scene = _scene(project.id, "This is model-produced spoken copy.")
            return ScenePlanResult(project.id, (scene,))

    with TestClient(create_app(tmp_path / "generated-copy.sqlite", scene_planner=Planner())) as client:
        project = client.post("/projects", json={"title": "Generated", "topic": "New topic"}).json()
        response = client.post(f"/projects/{project['id']}/scene-plan", json={"persist": True})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["generated_script"] is True
        assert body["script"] == "This is model-produced spoken copy."
        assert body["draft"] is not None
        assert body["draft"]["script"] == body["script"]
        assert body["draft"]["scenes"][0]["voice_text"] == body["script"]
        assert body["draft"]["evidence_refs"] == [f"ip_profile:{project['ip_profile_id']}:v1"]
