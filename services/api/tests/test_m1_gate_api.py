from pathlib import Path

from fastapi.testclient import TestClient

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
        ):
            assert marker in page.text
        response = client.post("/projects", json={"title": "真实素材测试", "topic": "主题", "creator_name": "我"})
        assert response.status_code == 201
        project = response.json()
        assert project["title"] == "真实素材测试"
        listed = client.get("/projects")
        assert listed.status_code == 200
        assert listed.json()[0]["id"] == project["id"]


def test_project_create_rejects_secret_fields(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "gate-secret.sqlite")) as client:
        response = client.post("/projects", json={"title": "x", "topic": "y", "api_key": "must-not-persist"})
        assert response.status_code == 422
