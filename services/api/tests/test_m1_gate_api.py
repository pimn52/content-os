from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient

from app.domain.models import CandidateAsset, CostCategory, ScenePlan, SourceKind, UsageCost, VisualIntent
from app.main import create_app


def test_m1_gate_page_and_project_creation_are_local(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "gate.sqlite")) as client:
        page = client.get("/m1-gate")
        assert page.status_code == 200
        assert "第一真实素材 Gate" in page.text
        # Keep the critical product behavior visible in the dependency-free UI.
        for marker in (
            "confirmed", "hasAllSelections", "replacement_count",
            "top1_acceptance_rate", "top3_coverage_rate", "selected_user_asset_rate",
            "unknown_cost_count", ".primary:disabled", "start=Date.now()",
            "manualStart", "total_elapsed_seconds", "requires_capture", "/projects/${pid()}/render",
            "/projects/${pid()}/render-jobs", "pollRenderJob", "RENDER_ACTIVE_STATUSES",
            "pending", "running", "completed", "failed", "preview_url", "download_url",
            "scenes[i]?.id", "video_spec:lastSpec", "data-clip-id", "loadedmetadata",
            "isConfirmed", "selectProject", "resetWorkflowState",
        ):
            assert marker in page.text
        workspace = client.get("/workspace")
        assert workspace.status_code == 200
        for marker in ("IP 资料（默认档案）", "保存 IP 资料", "新主题 → 草稿", "素材用途确认", "/ip-profile", "/assets/${id}/usage"):
            assert marker in workspace.text
        response = client.post("/projects", json={"title": "真实素材测试", "topic": "主题", "creator_name": "我"})
        assert response.status_code == 201
        project = response.json()
        assert project["title"] == "真实素材测试"
        listed = client.get("/projects")
        assert listed.status_code == 200
        assert listed.json()[0]["id"] == project["id"]

        second = client.post("/projects", json={"title": "第二个主题", "topic": "另一个主题", "creator_name": "不同输入"})
        assert second.status_code == 201
        assert second.json()["ip_profile_id"] == project["ip_profile_id"]

        profile = client.get("/ip-profile")
        assert profile.status_code == 200
        updated = client.put("/ip-profile", json={
            "creator_name": profile.json()["creator_name"], "domains": [], "topics": [],
            "vocabulary": [], "avoided_expressions": [], "common_hooks": [], "boundaries": [],
            "metadata": {}, "audience": "software creators", "knowledge": ["local workflows"],
            "opinions": ["small tools should stay understandable"], "style_notes": ["concrete"],
        })
        assert updated.status_code == 200
        assert updated.json()["knowledge"] == ["local workflows"]
        revisions = client.get("/ip-profile/revisions")
        assert revisions.status_code == 200
        assert [item["version"] for item in revisions.json()] == [1, 2]


def test_project_create_rejects_secret_fields(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "gate-secret.sqlite")) as client:
        response = client.post("/projects", json={"title": "x", "topic": "y", "api_key": "must-not-persist"})
        assert response.status_code == 422


def test_project_draft_round_trips_across_app_restart(tmp_path: Path) -> None:
    path = tmp_path / "draft.sqlite"
    with TestClient(create_app(path)) as client:
        project = client.post("/projects", json={"title": "可恢复项目", "topic": "持久化主题"}).json()
        project_id = UUID(project["id"])
        scene = ScenePlan(
            project_id=project_id, scene_id="hook", order=0, purpose="hook", voice_text="Draft caption",
            duration_target_ms=800, visual_intent=VisualIntent(subject="creator"),
            preferred_sources=[SourceKind.USER_ASSET],
        )
        candidate = CandidateAsset(
            scene_plan_id=scene.id, source_kind=SourceKind.CAPTURE, match_score=0,
            why=["capture gap"], recommended=True, requires_capture=True,
            estimated_cost=UsageCost(category=CostCategory.CAPTURE),
        )
        payload = {
            "script": "Draft script", "topic": "Draft topic", "scenes": [scene.model_dump(mode="json")],
            "routes": [{"scene_plan_id": str(scene.id), "candidates": [candidate.model_dump(mode="json")]}],
            "confirmed": [candidate.model_dump(mode="json")],
        }
        saved = client.put(f"/projects/{project_id}/draft", json=payload)
        assert saved.status_code == 200, saved.text
        assert saved.json()["version"] == 1

    with TestClient(create_app(path)) as client:
        restored = client.get(f"/projects/{project_id}/draft")
        assert restored.status_code == 200
        assert restored.json()["version"] == 1
        assert restored.json()["script"] == "Draft script"
        assert restored.json()["scenes"][0]["id"] == str(scene.id)


def test_project_draft_rejects_foreign_scene_and_candidate(tmp_path: Path) -> None:
    path = tmp_path / "draft-invalid.sqlite"
    with TestClient(create_app(path)) as client:
        project = client.post("/projects", json={"title": "项目", "topic": "主题"}).json()
        project_id = UUID(project["id"])
        foreign_scene = ScenePlan(
            project_id=UUID(int=99), scene_id="foreign", order=0, purpose="hook", voice_text="Foreign",
            duration_target_ms=800, visual_intent=VisualIntent(subject="creator"),
            preferred_sources=[SourceKind.USER_ASSET],
        )
        response = client.put(f"/projects/{project_id}/draft", json={"scenes": [foreign_scene.model_dump(mode="json")]})
        assert response.status_code == 422
