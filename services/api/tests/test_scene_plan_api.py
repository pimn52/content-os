from datetime import datetime, timezone
import json
import pytest
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
    monkeypatch.setenv("CONTENT_OS_LLM_REASONING_EFFORT", "low")
    monkeypatch.setenv("CONTENT_OS_LLM_MAX_OUTPUT_TOKENS", "4096")
    monkeypatch.setenv("CONTENT_OS_LLM_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("CONTENT_OS_LLM_CHAT_OUTPUT_MODE", "json_schema")

    planner = _scene_planner_from_env()

    assert planner.protocol == "chat_completions"
    assert planner.model == "kimi-k3"
    assert planner.reasoning_effort == "low"
    assert planner.max_output_tokens == 4096
    assert planner.timeout_seconds == 120
    assert planner.chat_output_mode == "json_schema"


@pytest.mark.parametrize("field,value", [("MAX_OUTPUT_TOKENS", "bad-secret"), ("MAX_OUTPUT_TOKENS", "0"),
    ("TIMEOUT_SECONDS", "bad-secret"), ("TIMEOUT_SECONDS", "nan"), ("REASONING_EFFORT", "bad-secret"), ("CHAT_OUTPUT_MODE", "bad-secret")])
def test_scene_planner_invalid_environment_fails_safely(monkeypatch, field, value):
    from app.providers.scene_planner import ScenePlannerConfigurationError
    monkeypatch.setenv("CONTENT_OS_LLM_API_KEY", "private-key")
    monkeypatch.setenv("CONTENT_OS_LLM_PROTOCOL", "chat_completions")
    monkeypatch.setenv(f"CONTENT_OS_LLM_{field}", value)
    with pytest.raises(ScenePlannerConfigurationError) as raised:
        _scene_planner_from_env()
    assert "bad-secret" not in str(raised.value) and "private-key" not in str(raised.value)


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
        records_before_retry_id = records[0]["id"]

        transport.status = 429
        failed = client.post(f"/projects/{project.id}/scene-plan", json={"topic": "Budgeted runtime retry"})
        assert failed.status_code == 503
        records = client.get(f"/projects/{project.id}/provider-calls").json()
        assert len(records) == 2
        # Stored timestamps can tie; UUID order is not execution order.
        failure = next(record for record in records if record["id"] != records_before_retry_id)
        assert failure["status"] == "failed"
        assert failure["error_code"] == "scene_planner_temporarily_unavailable"


def test_runtime_scene_plan_replays_one_completed_result_and_rejects_changed_retry_input(tmp_path: Path) -> None:
    path = tmp_path / "scene-plan-idempotency.sqlite"
    project = _project(path)
    calls: list[object] = []

    class Transport:
        def post(self, *args):
            calls.append(args)
            return _runtime_plan_response()

    planner = OpenAICompatibleScenePlanner("runtime-key", transport=Transport(), max_output_tokens=4096)
    with TestClient(create_app(path, scene_planner=planner)) as client:
        assert client.put("/budget", json={"allow_unknown_cost": True, "max_calls": 1}).status_code == 200
        headers = {"Idempotency-Key": "same-planning-request"}
        first = client.post(f"/projects/{project.id}/scene-plan", json={"topic": "First topic"}, headers=headers)
        replay = client.post(f"/projects/{project.id}/scene-plan", json={"topic": "First topic"}, headers=headers)
        conflict = client.post(f"/projects/{project.id}/scene-plan", json={"topic": "Changed topic"}, headers=headers)

        assert first.status_code == replay.status_code == 200
        assert replay.json() == first.json()
        assert conflict.status_code == 409
        assert conflict.json()["detail"] == "provider_idempotency_conflict"
        assert len(calls) == 1
        assert json.loads(calls[0][2])["max_output_tokens"] == 4096
        prompt = json.loads(calls[0][2])["input"][0]["content"][0]["text"]
        context = json.loads(prompt.split("Project request:\n", 1)[1])["production_controls"]
        brief = context["production_feasibility"]
        assert brief["authority"] == "planning_hint_not_admission_or_authorization"
        assert brief["live_usage_observation"][0]["calls"] == 0
        assert brief["capabilities"]["talking"]["dispatch_authorized"] is False
        records = client.get(f"/projects/{project.id}/provider-calls").json()
        assert len(records) == 1
        assert records[0]["status"] == "completed"
        assert records[0]["result_payload"]["result"]["project_id"] == str(project.id)
        assert client.put("/budget", json={"allow_unknown_cost": True, "max_calls": 2}).status_code == 200
        changed_policy = client.post(f"/projects/{project.id}/scene-plan", json={"topic": "First topic"}, headers=headers)
        assert changed_policy.status_code == 409
        assert changed_policy.json()["detail"] == "provider_idempotency_conflict"
        assert len(calls) == 1


def test_truncated_scene_plan_preserves_draft_and_records_failed_attempt(tmp_path):
    path = tmp_path / "scene-plan-truncated.sqlite"
    project = _project(path)
    calls = []
    class Transport:
        def post(self, *args):
            calls.append(args)
            response = _runtime_plan_response()
            value = json.loads(response.body)
            value["status"] = "incomplete"
            return ScenePlannerHTTPResponse(200, json.dumps(value).encode(), {})
    planner = OpenAICompatibleScenePlanner("runtime-key", transport=Transport(), max_output_tokens=100)
    with TestClient(create_app(path, scene_planner=planner)) as client:
        before = client.get(f"/projects/{project.id}/draft").json()
        assert client.put("/budget", json={"allow_unknown_cost": True}).status_code == 200
        response = client.post(f"/projects/{project.id}/scene-plan", json={"persist": True})
        assert response.status_code == 502
        after = client.get(f"/projects/{project.id}/draft").json()
        # An unsaved empty draft is synthesized at read time, including now().
        assert {k: v for k, v in after.items() if k != "updated_at"} == {k: v for k, v in before.items() if k != "updated_at"}
        records = client.get(f"/projects/{project.id}/provider-calls").json()
        assert len(records) == len(calls) == 1
        assert records[0]["status"] == "failed"
        assert records[0]["usage_observable"] is False
    from app.db import ProjectDraftRepository
    db = Database(path)
    assert ProjectDraftRepository(db).get(project.id) is None
    db.close()


@pytest.mark.parametrize("valid", [True, False])
def test_chat_schema_mode_uses_normal_persistence_and_failure_gate(tmp_path, valid, caplog):
    path = tmp_path / "chat-schema-api.sqlite"
    project = _project(path)
    requests = []
    class Transport:
        def post(self, url, headers, body, timeout):
            requests.append(json.loads(body))
            content = json.loads(_runtime_plan_response().body)["output"][0]["content"][0]["text"]
            if not valid:
                document = json.loads(content)
                document["scenes"][0]["private-untrusted-field"] = "private-provider-value"
                content = json.dumps(document)
            return ScenePlannerHTTPResponse(200, json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": content}}]}).encode(), {})
    planner = OpenAICompatibleScenePlanner("private-key", protocol="chat_completions", chat_output_mode="json_schema", transport=Transport())
    with TestClient(create_app(path, scene_planner=planner)) as client:
        assert client.put("/budget", json={"allow_unknown_cost": True, "max_calls": 1}).status_code == 200
        response = client.post(f"/projects/{project.id}/scene-plan", json={"persist": True}, headers={"Idempotency-Key": "chat-schema-test"})
        assert response.status_code == (200 if valid else 502)
        draft = client.get(f"/projects/{project.id}/draft").json()
        assert len(draft["scenes"]) == (1 if valid else 0)
        records = client.get(f"/projects/{project.id}/provider-calls").json()
        assert len(records) == len(requests) == 1
        assert records[0]["status"] == ("completed" if valid else "failed")
        assert requests[0]["response_format"]["json_schema"]["strict"] is True
        if not valid:
            assert records[0]["error_code"] == "scene_planner_response_invalid"
            assert "validation_code=scene_shape" in caplog.text
        assert "private-key" not in caplog.text and "private-provider-value" not in caplog.text


def test_domain_rule_failure_is_private_durable_and_preserves_saved_draft(tmp_path, caplog):
    path = tmp_path / "domain-rule.sqlite"
    project = _project(path)
    requests = []
    class Transport:
        def post(self, url, headers, body, timeout):
            requests.append(json.loads(body))
            content = json.loads(_runtime_plan_response().body)["output"][0]["content"][0]["text"]
            document = json.loads(content)
            document["scenes"][0]["preferred_sources"] = ["typography", "user_asset"]
            document["scenes"][0]["scene_id"] = "private-provider-source"
            return ScenePlannerHTTPResponse(200, json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(document)}}]}).encode(), {})
    planner = OpenAICompatibleScenePlanner("private-key", protocol="chat_completions", transport=Transport())
    with TestClient(create_app(path, scene_planner=planner)) as client:
        client.put("/budget", json={"allow_unknown_cost": True}).raise_for_status()
        client.put(f"/projects/{project.id}/draft", json={"script": "Retained copy", "topic": project.topic}).raise_for_status()
        before = client.get(f"/projects/{project.id}/draft").json()
        response = client.post(f"/projects/{project.id}/scene-plan", json={"persist": True})
        assert response.status_code == 502
        assert client.get(f"/projects/{project.id}/draft").json() == before
        records = client.get(f"/projects/{project.id}/provider-calls").json()
        assert len(records) == len(requests) == 1
        assert records[0]["status"] == "failed" and records[0]["error_code"] == "scene_planner_response_invalid"
        assert "domain_rule=real_asset_priority" in caplog.text
        assert "private-key" not in caplog.text and "private-provider-source" not in caplog.text


def test_explanatory_first_graphic_persists_and_routes_without_waiving_voice(tmp_path):
    path = tmp_path / "explanatory-exception.sqlite"
    project = _project(path)
    requests = []
    class Transport:
        def post(self, url, headers, body, timeout):
            requests.append(json.loads(body))
            content = json.loads(_runtime_plan_response().body)["output"][0]["content"][0]["text"]
            document = json.loads(content)
            document["scenes"][0].update(preferred_sources=["typography"],
                fallback_sources=["user_asset", "historical_asset"], visual_requirement="explanatory",
                visual_requirement_reason="A concise concept card preserves the editorial point.", caption_emphasis=["Concept"],
                visual_intent={"subject": "concept", "action": None, "framing": None, "description": "A short semantic concept card."})
            return ScenePlannerHTTPResponse(200, json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(document)}}]}).encode(), {})
    planner = OpenAICompatibleScenePlanner("key", protocol="chat_completions", transport=Transport())
    with TestClient(create_app(path, scene_planner=planner)) as client:
        client.put("/budget", json={"allow_unknown_cost": True}).raise_for_status()
        response = client.post(f"/projects/{project.id}/scene-plan", json={"persist": True})
        assert response.status_code == 200, response.text
        draft = client.get(f"/projects/{project.id}/draft").json()
        assert draft["scenes"][0]["preferred_sources"] == ["typography"]
        assert draft["scenes"][0]["fallback_sources"] == ["user_asset", "historical_asset"]
        preflight = client.post(f"/projects/{project.id}/production-preflight", json={})
        assert preflight.status_code == 200, preflight.text
        result = preflight.json()
        assert result["scenes"][0]["planned_source_kind"] == "typography"
        assert result["preliminary_edit_plan"] is not None
        assert result["status"] == "blocked" and result["dispatch_performed"] is False
        assert "voice_capability_not_verified" in result["stop_reasons"]
        records = client.get(f"/projects/{project.id}/provider-calls").json()
        assert len(records) == len(requests) == 1 and records[0]["status"] == "completed"


@pytest.mark.parametrize("replay", [False, True])
def test_control_leak_preserves_draft_and_cannot_bypass_via_accounted_replay(tmp_path, replay, caplog):
    from test_creator_preference_selection import select
    from test_feedback_learning import change
    from test_planning_input import LEAKED_VOICE
    from app.db import ProviderCallRepository
    path = tmp_path / "control-leak.sqlite"
    project = _project(path)
    calls = []
    class Transport:
        def post(self, *args):
            calls.append(args)
            response = _runtime_plan_response()
            if not replay:
                value = json.loads(response.body)
                document = json.loads(value["output"][0]["content"][0]["text"])
                document["scenes"][0]["voice_text"] = LEAKED_VOICE
                value["output"][0]["content"][0]["text"] = json.dumps(document)
                response = ScenePlannerHTTPResponse(200, json.dumps(value).encode(), {})
            return response
    planner = OpenAICompatibleScenePlanner("private-key", transport=Transport())
    with TestClient(create_app(path, scene_planner=planner)) as client:
        root = f"/projects/{project.id}"
        enabled = change(client, project.id, select(client, project.id))
        assert enabled.status_code == 200
        client.put("/budget", json={"allow_unknown_cost": True}).raise_for_status()
        headers = {"Idempotency-Key": "control-boundary"}
        if replay:
            first = client.post(root + "/scene-plan", json={"persist": True}, headers=headers)
            assert first.status_code == 200
            # Synthetic accounted pre-policy output, not a second provider call.
            with Database(path) as db:
                repo = ProviderCallRepository(db)
                record = repo.list_for_project(project.id)[0]
                payload = json.loads(json.dumps(record.result_payload))
                payload["result"]["scenes"][0]["voice_text"] = LEAKED_VOICE
                payload["result"]["script"] = LEAKED_VOICE
                repo.update(record.model_copy(update={"result_payload": payload}))
        before = client.get(root + "/draft").json()
        response = client.post(root + "/scene-plan", json={"persist": True}, headers=headers)
        assert response.status_code == 502
        after = client.get(root + "/draft").json()
        assert {k:v for k,v in before.items() if k != "updated_at"} == {k:v for k,v in after.items() if k != "updated_at"}
        records = client.get(root + "/provider-calls").json()
        assert len(calls) == len(records) == 1
        assert records[0]["status"] == ("completed" if replay else "failed")
        if not replay:
            assert records[0]["error_code"] == "scene_planner_response_invalid"
            assert "domain_rule=audience_control_leak" in caplog.text
        assert LEAKED_VOICE not in caplog.text and "private-key" not in caplog.text
