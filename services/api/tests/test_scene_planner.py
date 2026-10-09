from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterator
from uuid import uuid4

import pytest

from app.domain.models import Project, RationalFps
from app.providers.scene_planner import (
    OpenAICompatibleScenePlanner,
    ScenePlanResult,
    ScenePlannerAuthenticationError,
    ScenePlannerConfigurationError,
    ScenePlannerConnectionError,
    ScenePlannerHTTPError,
    ScenePlannerInputError,
    ScenePlannerProviderResponseError,
    ScenePlannerRateLimitError,
    ScenePlannerTimeout,
)


def _project() -> Project:
    return Project(
        ip_profile_id=uuid4(), title="Creator's practical coding advice", topic="When Vibe Coding is the wrong choice",
        fps=RationalFps(numerator=30, denominator=1), created_at=datetime.now(timezone.utc),
    )


def _scene(scene_id: str, order: int, *, preferred: list[str] | None = None, fallback: list[str] | None = None) -> dict[str, object]:
    return {
        "scene_id": scene_id,
        "order": order,
        "purpose": "hook" if order == 0 else "explain",
        "voice_text": "Most people misunderstand this tradeoff." if order == 0 else "Start with the problem you can verify.",
        "duration_target_ms": 3500,
        "visual_intent": {
            "subject": "creator", "action": "working at a laptop", "framing": "close", "description": "A practical desk setup.",
        },
        "preferred_sources": preferred or ["user_asset", "historical_asset"],
        "fallback_sources": fallback or ["typography", "stock"],
        "caption_emphasis": ["tradeoff"],
    }


def _response(scenes: list[dict[str, object]]) -> dict[str, object]:
    return {"output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps({"scenes": scenes})}]}]}


def _chat_response(scenes: list[dict[str, object]]) -> dict[str, object]:
    return {"choices": [{"message": {"content": json.dumps({"scenes": scenes})}}]}


@pytest.mark.parametrize("requirement", ["unknown", "creator_speaking", "action_evidence", "explanatory"])
def test_planner_accepts_explicit_visual_requirement_and_requests_it(requirement):
    from app.providers.scene_planner import _PLAN_SCHEMA
    scene = {**_scene("intent", 0), "visual_requirement": requirement,
             "visual_requirement_reason": None if requirement == "unknown" else "Retain the editorial evidence this scene needs."}
    with _fake_server(200, _chat_response([scene])) as (url, requests):
        result = OpenAICompatibleScenePlanner("key", base_url=url, protocol="chat_completions").plan(_project())
    assert result.scenes[0].visual_requirement == requirement
    schema = _PLAN_SCHEMA["properties"]["scenes"]["items"]
    assert "visual_requirement" in schema["required"]
    assert "Never classify as explanatory merely" in json.loads(requests[0][2])["messages"][0]["content"]


@pytest.mark.parametrize("fields", [
    {"visual_requirement": "explanatory"},
    {"visual_requirement": "explanatory", "visual_requirement_reason": None},
    {"visual_requirement": "action_evidence", "visual_requirement_reason": "   "},
    {"visual_requirement": "invented", "visual_requirement_reason": "Invalid classification"},
])
def test_planner_rejects_partial_or_unreasoned_visual_requirement(fields):
    with _fake_server(200, _chat_response([{**_scene("intent", 0), **fields}])) as (url, _):
        with pytest.raises(ScenePlannerProviderResponseError):
            OpenAICompatibleScenePlanner("key", base_url=url, protocol="chat_completions").plan(_project())


@contextmanager
def _fake_server(
    status: int,
    payload: object,
    response_headers: dict[str, str] | None = None,
) -> Iterator[tuple[str, list[tuple[str, dict[str, str], bytes]]]]:
    requests: list[tuple[str, dict[str, str], bytes]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append((self.path, dict(self.headers.items()), body))
            response = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            for name, value in (response_headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_scene_planner_posts_strict_schema_and_binds_project() -> None:
    project = _project()
    response = _response([_scene("scene_01", 0), _scene("scene_02", 1)])
    with _fake_server(200, response) as (base_url, requests):
        result = OpenAICompatibleScenePlanner("  runtime-secret  ", base_url=base_url).plan(
            project, script="Open with the misconception.", topic="When to avoid Vibe Coding",
            context={"ip_profile_version": 2, "evidence_refs": ["ip_profile:example:v2"], "ip_profile": {"audience": "builders"}},
        )

    assert isinstance(result, ScenePlanResult)
    assert result.project_id == project.id
    assert [scene.scene_id for scene in result.scenes] == ["scene_01", "scene_02"]
    assert [scene.order for scene in result.scenes] == [0, 1]
    assert result.scenes[0].project_id == project.id
    path, headers, body = requests[0]
    request = json.loads(body)
    assert path == "/v1/responses"
    assert headers["Authorization"] == "Bearer runtime-secret"
    assert request["model"] == "gpt-4.1-mini"
    assert request["store"] is False
    schema = request["text"]["format"]
    assert schema["type"] == "json_schema" and schema["strict"] is True
    assert schema["schema"]["additionalProperties"] is False
    assert schema["schema"]["properties"]["scenes"]["items"]["additionalProperties"] is False
    prompt = request["input"][0]["content"][0]["text"]
    assert "Prefer user_asset and historical_asset first" in prompt
    assert "Open with the misconception." in prompt
    assert "When to avoid Vibe Coding" in prompt
    assert request["input"][0]["content"][0]["text"].find('"ip_profile_version": 2') >= 0


def test_scene_planner_supports_explicit_chat_completions_protocol() -> None:
    project = _project()
    with _fake_server(200, _chat_response([_scene("chat_scene", 0)])) as (base_url, requests):
        result = OpenAICompatibleScenePlanner(
            "runtime-secret", base_url=base_url, model="kimi-k3", protocol="chat_completions"
        ).plan(project, topic="H.265 码率与平台转码")

    assert result.project_id == project.id
    assert [scene.scene_id for scene in result.scenes] == ["chat_scene"]
    path, headers, body = requests[0]
    request = json.loads(body)
    assert path == "/v1/chat/completions"
    assert headers["Authorization"] == "Bearer runtime-secret"
    assert request["model"] == "kimi-k3"
    assert list(request) == ["model", "messages"]
    assert request["messages"][0]["role"] == "user"
    assert "H.265 码率与平台转码" in request["messages"][0]["content"]
    prompt = request["messages"][0]["content"]
    from app.providers.scene_planner import _PLAN_SCHEMA
    schema_text = prompt.split("Required output JSON Schema (return only the JSON instance, not the schema):\n", 1)[1].split("\n", 1)[0]
    assert json.loads(schema_text) == _PLAN_SCHEMA
    assert "milliseconds" in prompt
    assert "response_format" not in request


def test_chat_schema_opt_in_uses_the_same_contract_and_ignores_reasoning():
    from app.providers.scene_planner import _PLAN_SCHEMA
    payload = _chat_response([_scene("schema", 0)])
    payload["choices"][0]["message"]["reasoning_content"] = "private reasoning, not JSON"
    with _fake_server(200, payload) as (url, requests):
        result = OpenAICompatibleScenePlanner("key", base_url=url, protocol="chat_completions", chat_output_mode="json_schema").plan(_project())
    assert result.scenes[0].scene_id == "schema"
    assert json.loads(requests[0][2])["response_format"] == {
        "type": "json_schema", "json_schema": {"name": "scene_plan", "strict": True, "schema": _PLAN_SCHEMA}}
    assert len(requests) == 1


@pytest.mark.parametrize("mode,protocol", [("auto", "chat_completions"), ("", "chat_completions"), (None, "chat_completions"), ("json_schema", "responses")])
def test_chat_output_mode_is_explicitly_validated(mode, protocol):
    with pytest.raises(ScenePlannerConfigurationError):
        OpenAICompatibleScenePlanner("key", protocol=protocol, chat_output_mode=mode)


def test_schema_rejection_does_not_trigger_prompt_fallback():
    with _fake_server(400, {"error": "unsupported schema private-key"}) as (url, requests):
        with pytest.raises(ScenePlannerHTTPError):
            OpenAICompatibleScenePlanner("private-key", base_url=url, protocol="chat_completions", chat_output_mode="json_schema").plan(_project())
    assert len(requests) == 1


@pytest.mark.parametrize("failure,code", [
    ("response_json", "response_json"), ("response_envelope", "response_envelope"),
    ("output_text", "output_text"), ("output_json", "output_json"), ("fence", "output_json"),
    ("plan_shape", "plan_shape"), ("scene_shape", "scene_shape"), ("scene_values", "scene_values"),
    ("incomplete_answer", "incomplete_answer"),
])
def test_safe_validation_codes_without_provider_values(failure, code, caplog):
    import traceback
    secret = "private-provider-content"
    payload = _chat_response([_scene("safe", 0)])
    if failure == "response_json": payload = ("not json " + secret).encode()
    elif failure == "response_envelope": payload = [secret]
    elif failure == "output_text": payload = {"choices": [{"message": {"reasoning_content": secret}}]}
    elif failure == "output_json": payload["choices"][0]["message"]["content"] = secret
    elif failure == "fence": payload["choices"][0]["message"]["content"] = "```" + secret
    elif failure == "plan_shape": payload["choices"][0]["message"]["content"] = json.dumps({"wrong": secret})
    elif failure == "scene_shape":
        scene = _scene("safe", 0)
        scene[secret] = secret
        payload = _chat_response([scene])
    elif failure == "scene_values":
        scene = _scene("safe", 0)
        scene["duration_target_ms"] = secret
        payload = _chat_response([scene])
    elif failure == "incomplete_answer": payload["choices"][0]["finish_reason"] = "length"
    with _fake_server(200, payload) as (url, requests):
        with pytest.raises(ScenePlannerProviderResponseError) as raised:
            OpenAICompatibleScenePlanner(secret, base_url=url, protocol="chat_completions").plan(_project())
    assert raised.value.validation_code == code
    assert f"validation_code={code}" in caplog.text
    assert secret not in caplog.text
    assert secret not in "".join(traceback.format_exception(raised.value))
    assert len(requests) == 1


@pytest.mark.parametrize("failure,rule,field", [
    ("duration", "field_value", "duration_target_ms"),
    ("intent", "field_value", "visual_intent"),
    ("reason", "visual_requirement_reason_required", "visual_requirement_reason"),
    ("order", "scene_order", "unknown"),
    ("duplicate", "scene_id_duplicate", "unknown"),
    ("priority", "real_asset_priority", "unknown"),
    ("fallback", "real_asset_fallback_only", "unknown"),
    ("empty", "empty_plan", "unknown"),
])
def test_domain_diagnostics_are_specific_fixed_and_private(failure, rule, field, caplog):
    import traceback
    secret = "private-source-response-content"
    scenes = [_scene(secret, 0)]
    if failure == "duration": scenes[0]["duration_target_ms"] = secret
    elif failure == "intent": scenes[0]["visual_intent"]["action"] = [secret]
    elif failure == "reason": scenes[0].update(visual_requirement="explanatory", visual_requirement_reason="   ")
    elif failure == "order": scenes[0]["order"] = 1
    elif failure == "duplicate": scenes.append({**_scene(secret, 1)})
    elif failure == "priority": scenes[0]["preferred_sources"] = ["typography", "user_asset"]
    elif failure == "fallback": scenes[0].update(preferred_sources=["typography"], fallback_sources=["user_asset"])
    elif failure == "empty": scenes = []
    with _fake_server(200, _chat_response(scenes)) as (url, requests):
        with pytest.raises(ScenePlannerProviderResponseError) as raised:
            OpenAICompatibleScenePlanner(secret, base_url=url, protocol="chat_completions").plan(_project())
    assert raised.value.validation_code == "scene_values"
    assert raised.value.domain_rule == rule and raised.value.domain_field == field
    assert f"domain_rule={rule} domain_field={field}" in caplog.text
    assert secret not in caplog.text + "".join(traceback.format_exception(raised.value))
    assert len(requests) == 1


def test_domain_diagnostic_rejects_untrusted_identifiers():
    error = ScenePlannerProviderResponseError("safe", domain_rule="private-rule", domain_field="private-field")
    assert error.domain_rule == error.domain_field == "unknown"


@pytest.mark.parametrize("protocol", ["responses", "chat_completions"])
@pytest.mark.parametrize("graphic,accepted", [
    ({"kind": "text_card", "treatment": "key_point", "points": ["结论", "条件", "证据", "限制"], "reason": None}, True),
    ({"kind": "unsupported", "treatment": None, "points": [], "reason": "Requires node links"}, True),
    (None, False),
    ({"kind": "text_card", "treatment": "headline", "points": ["结论", "条件"], "reason": None}, False),
])
def test_explicit_graphic_provider_shape_preserves_realization_or_rejects_invalid(protocol, graphic, accepted):
    value = {**_scene("graphic", 0), "visual_requirement": "explanatory",
             "visual_requirement_reason": "A concept explanation", "graphic_plan": graphic}
    response = _response([value]) if protocol == "responses" else _chat_response([value])
    with _fake_server(200, response) as (url, requests):
        planner = OpenAICompatibleScenePlanner("key", base_url=url, protocol=protocol)
        if accepted:
            assert planner.plan(_project()).scenes[0].graphic_plan.model_dump(mode="json", exclude={"schema_version"}) == graphic
        else:
            with pytest.raises(ScenePlannerProviderResponseError) as failure:
                planner.plan(_project())
            assert failure.value.validation_code == "scene_values"
        assert len(requests) == 1


@pytest.mark.parametrize("protocol", ["responses", "chat_completions"])
def test_prompt_states_local_cross_field_source_rules(protocol):
    response = _response([_scene("valid", 0)]) if protocol == "responses" else _chat_response([_scene("valid", 0)])
    with _fake_server(200, response) as (url, requests):
        result = OpenAICompatibleScenePlanner("key", base_url=url, protocol=protocol).plan(_project())
    body = json.loads(requests[0][2])
    text = body["messages"][0]["content"] if protocol == "chat_completions" else body["input"][0]["content"][0]["text"]
    assert "every user_asset/historical_asset in preferred_sources must precede every" in text
    assert "real sources cannot be fallback-only" in text
    assert "creative preference from an irreplaceable visual requirement" in text
    assert "not proof that the creator must visibly speak" in text
    assert "not merely because the topic mentions an action" in text
    assert "Read production_feasibility before choosing sources" in text
    assert "Unknown price is not zero" in text
    assert result.scenes[0].visual_requirement == "unknown"  # unchanged legacy behavior


@pytest.mark.parametrize("protocol", ["responses", "chat_completions"])
@pytest.mark.parametrize("topic,accepted", [("保住采访原意", False), ("Content OS 的预检流程", True)])
def test_retained_audience_control_leak_across_protocols(protocol, topic, accepted, caplog):
    from test_planning_input import context, LEAKED_VOICE
    value = {**_scene("caption-discipline", 0), "voice_text": LEAKED_VOICE}
    response = _response([value]) if protocol == "responses" else _chat_response([value])
    with _fake_server(200, response) as (url, requests):
        planner = OpenAICompatibleScenePlanner("private-key", base_url=url, protocol=protocol)
        if accepted:
            assert planner.plan(_project(), topic=topic, context=context()).scenes[0].voice_text == LEAKED_VOICE
        else:
            with pytest.raises(ScenePlannerProviderResponseError) as failure:
                planner.plan(_project(), topic=topic, context=context())
            assert failure.value.validation_code == "scene_values"
            assert failure.value.domain_rule == "audience_control_leak"
            assert failure.value.domain_field == "voice_text"
            assert "domain_rule=audience_control_leak" in caplog.text
        assert len(requests) == 1
        body = json.loads(requests[0][2])
        prompt = body["messages"][0]["content"] if protocol == "chat_completions" else body["input"][0]["content"][0]["text"]
        serialized = prompt.split("Project request:\n", 1)[1].split("\n\nRequired output JSON Schema", 1)[0]
        request = json.loads(serialized)
        assert request["audience_brief"]["topic"] == topic
        assert "planning_preferences" not in request["background_evidence"]
        assert "instruction" not in request["production_controls"]["planning_preferences"][0]
    assert LEAKED_VOICE not in caplog.text and "private-key" not in caplog.text


@pytest.mark.parametrize("requirement,preferred,accepted", [
    ("explanatory", ["typography"], True),
    ("explanatory", ["typography", "screenshot"], True),
    ("unknown", ["typography"], False),
    ("creator_speaking", ["typography"], False),
    ("action_evidence", ["typography"], False),
    ("explanatory", ["screenshot", "typography"], False),
    ("explanatory", ["typography", "ai_video"], False),
    ("explanatory", ["typography", "talking_profile"], False),
    ("explanatory", ["typography", "user_asset"], False),
])
def test_explanatory_typography_first_exception_is_narrow(requirement, preferred, accepted):
    scene = {**_scene("explain", 0, preferred=preferred, fallback=["user_asset", "historical_asset"]),
        "visual_requirement": requirement, "visual_requirement_reason": "A concept card preserves the explanation without person/action evidence."}
    with _fake_server(200, _chat_response([scene])) as (url, requests):
        planner = OpenAICompatibleScenePlanner("key", base_url=url, protocol="chat_completions")
        if accepted:
            result = planner.plan(_project())
            assert [s.value for s in result.scenes[0].preferred_sources] == preferred
            assert [s.value for s in result.scenes[0].fallback_sources] == ["user_asset", "historical_asset"]
        else:
            with pytest.raises(ScenePlannerProviderResponseError) as raised:
                planner.plan(_project())
            assert raised.value.domain_rule == ("real_asset_priority" if "user_asset" in preferred else "real_asset_fallback_only")
    assert len(requests) == 1


@pytest.mark.parametrize("protocol", ["", "chat", "responses/v1", 1, [], None])
def test_scene_planner_rejects_unknown_protocol(protocol: object) -> None:
    with pytest.raises(ScenePlannerConfigurationError, match="protocol"):
        OpenAICompatibleScenePlanner("key", protocol=protocol)  # type: ignore[arg-type]


@pytest.mark.parametrize("protocol,field", [("responses", "max_output_tokens"), ("chat_completions", "max_completion_tokens")])
def test_explicit_output_limit_and_effort(protocol, field, caplog):
    payload = _response([_scene("bounded", 0)]) if protocol == "responses" else _chat_response([_scene("bounded", 0)])
    with _fake_server(200, payload) as (url, requests):
        with caplog.at_level("INFO", logger="app.providers.scene_planner"):
            OpenAICompatibleScenePlanner("private-key", base_url=url, protocol=protocol,
                max_output_tokens=4096, reasoning_effort="low" if protocol == "chat_completions" else None).plan(_project(), script="private-script")
    body = json.loads(requests[0][2])
    assert body[field] == 4096
    assert body.get("reasoning_effort") == ("low" if protocol == "chat_completions" else None)
    assert len(requests) == 1
    assert "outcome=success stage=validation http=200" in caplog.text
    assert "private-key" not in caplog.text and "private-script" not in caplog.text


@pytest.mark.parametrize("options", [
    {"max_output_tokens": True}, {"max_output_tokens": 0}, {"max_output_tokens": -1},
    {"max_output_tokens": "4096"}, {"max_output_tokens": 1.5},
    {"reasoning_effort": "medium", "protocol": "chat_completions"},
    {"reasoning_effort": "low"}, {"reasoning_effort": ""},
])
def test_invalid_planner_controls_fail_before_dispatch(options):
    with pytest.raises(ScenePlannerConfigurationError):
        OpenAICompatibleScenePlanner("key", **options)


@pytest.mark.parametrize("finish_reason", ["length", "tool_calls", "content_filter"])
def test_incomplete_chat_response_is_rejected_even_if_json_valid(finish_reason, caplog):
    payload = _chat_response([_scene("partial", 0)])
    payload["choices"][0]["finish_reason"] = finish_reason
    with _fake_server(200, payload) as (url, requests):
        with pytest.raises(ScenePlannerProviderResponseError, match="complete answer"):
            OpenAICompatibleScenePlanner("key", base_url=url, protocol="chat_completions").plan(_project())
    assert len(requests) == 1
    assert "outcome=failure stage=validation http=200" in caplog.text


@pytest.mark.parametrize("stage", ["awaiting_headers", "reading_body", "private-key"])
def test_diagnostic_failure_stage_is_allowlisted(stage, caplog):
    class FailureTransport:
        calls = 0
        def post(self, *args):
            self.calls += 1
            error = ScenePlannerTimeout("safe timeout")
            error.stage = stage
            raise error
    transport = FailureTransport()
    with pytest.raises(ScenePlannerTimeout):
        OpenAICompatibleScenePlanner("private-key", transport=transport).plan(_project(), script="private-script")
    expected = stage if stage != "private-key" else "transport"
    assert f"stage={expected} http=None" in caplog.text
    assert transport.calls == 1
    assert "private-key" not in caplog.text and "private-script" not in caplog.text


@pytest.mark.parametrize("stage", ["awaiting_headers", "reading_body"])
def test_default_transport_reports_timeout_stage(monkeypatch, stage):
    from app.providers.scene_planner import UrllibScenePlannerTransport
    class Response:
        status = 200
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): raise TimeoutError("private upstream detail")
    class Opener:
        def open(self, *args, **kwargs):
            if stage == "awaiting_headers": raise TimeoutError("private upstream detail")
            return Response()
    monkeypatch.setattr("app.providers.scene_planner.build_opener", lambda *args: Opener())
    with pytest.raises(ScenePlannerTimeout) as raised:
        UrllibScenePlannerTransport().post("https://example.com/v1", {}, b"{}", 1)
    assert raised.value.stage == stage
    assert "private" not in str(raised.value)


def test_incomplete_responses_output_cannot_be_admitted():
    payload = _response([_scene("partial", 0)])
    payload["status"] = "incomplete"
    with _fake_server(200, payload) as (url, requests):
        with pytest.raises(ScenePlannerProviderResponseError, match="complete answer"):
            OpenAICompatibleScenePlanner("key", base_url=url).plan(_project())
    assert len(requests) == 1


def test_wrapped_socket_timeout_is_not_misclassified(monkeypatch):
    from urllib.error import URLError
    from app.providers.scene_planner import UrllibScenePlannerTransport
    class Opener:
        def open(self, *args, **kwargs):
            raise URLError(TimeoutError("private detail"))
    monkeypatch.setattr("app.providers.scene_planner.build_opener", lambda *args: Opener())
    with pytest.raises(ScenePlannerTimeout) as raised:
        UrllibScenePlannerTransport().post("https://example.com/v1", {}, b"{}", 1)
    assert raised.value.stage == "awaiting_headers"


@pytest.mark.parametrize(
    ("status", "error_type"),
    [(401, ScenePlannerAuthenticationError), (429, ScenePlannerRateLimitError), (500, ScenePlannerHTTPError)],
)
def test_scene_planner_http_errors_are_safe(status: int, error_type: type[Exception]) -> None:
    key = "scene-planner-secret"
    with _fake_server(status, {"error": {"message": key}}) as (base_url, _):
        with pytest.raises(error_type) as raised:
            OpenAICompatibleScenePlanner(key, base_url=base_url).plan(_project())
    assert key not in str(raised.value)
    if status == 500:
        assert isinstance(raised.value, ScenePlannerHTTPError)
        assert raised.value.status_code == 500


def test_scene_planner_does_not_follow_redirect_with_byok() -> None:
    with _fake_server(200, _response([_scene("winner", 0)])) as (target_url, target_requests):
        with _fake_server(302, {"redirect": "blocked"}, {"Location": f"{target_url}/responses"}) as (source_url, source_requests):
            with pytest.raises(ScenePlannerHTTPError, match="302"):
                OpenAICompatibleScenePlanner("redirect-secret", base_url=source_url).plan(_project())
    assert len(source_requests) == 1
    assert target_requests == []


def test_scene_planner_rejects_bad_provider_order_and_real_asset_priority() -> None:
    unordered = _response([_scene("later", 1), _scene("first", 0)])
    with _fake_server(200, unordered) as (base_url, _):
        with pytest.raises(ScenePlannerProviderResponseError, match="invalid scene plan"):
            OpenAICompatibleScenePlanner("key", base_url=base_url).plan(_project())

    extra_field = _scene("extra", 0)
    extra_field["provider_only_id"] = "must-not-pass-through"
    with _fake_server(200, _response([extra_field])) as (base_url, _):
        with pytest.raises(ScenePlannerProviderResponseError, match="invalid scene plan"):
            OpenAICompatibleScenePlanner("key", base_url=base_url).plan(_project())

    real_as_fallback_only = _response([_scene("bad", 0, preferred=["typography"], fallback=["user_asset"])])
    with _fake_server(200, real_as_fallback_only) as (base_url, _):
        with pytest.raises(ScenePlannerProviderResponseError, match="invalid scene plan"):
            OpenAICompatibleScenePlanner("key", base_url=base_url).plan(_project())


def test_scene_planner_input_configuration_and_transport_errors_are_safe() -> None:
    with pytest.raises(ScenePlannerConfigurationError, match="localhost"):
        OpenAICompatibleScenePlanner("key", base_url="http://remote.example/v1")
    with pytest.raises(ScenePlannerConfigurationError, match="timeout"):
        OpenAICompatibleScenePlanner("key", timeout_seconds=float("nan"))
    with pytest.raises(ScenePlannerInputError, match="script"):
        OpenAICompatibleScenePlanner("key").plan(_project(), script=" ")

    class TimeoutTransport:
        def post(self, url: str, headers: dict[str, str], body: bytes, timeout_seconds: float) -> object:
            raise TimeoutError("secret transport detail")

    key = "timeout-secret"
    with pytest.raises(ScenePlannerTimeout) as raised:
        OpenAICompatibleScenePlanner(key, transport=TimeoutTransport()).plan(_project())  # type: ignore[arg-type]
    assert key not in str(raised.value)

    class BrokenTransport:
        def post(self, url: str, headers: dict[str, str], body: bytes, timeout_seconds: float) -> object:
            raise RuntimeError(key)

    with pytest.raises(ScenePlannerConnectionError) as raised:
        OpenAICompatibleScenePlanner(key, transport=BrokenTransport()).plan(_project())  # type: ignore[arg-type]
    assert key not in str(raised.value)


def test_scene_plan_result_is_immutable_and_rejects_invalid_status() -> None:
    project = _project()
    with pytest.raises(ValueError, match="immutable"):
        ScenePlanResult(project_id=project.id, scenes=[_scene("not-a-contract", 0)])  # type: ignore[arg-type]

    class InvalidStatusTransport:
        def post(self, url: str, headers: dict[str, str], body: bytes, timeout_seconds: float) -> object:
            return type("Response", (), {"status_code": True, "body": b"{}", "headers": {}})()

    with pytest.raises(ScenePlannerProviderResponseError, match="invalid HTTP status"):
        OpenAICompatibleScenePlanner("key", transport=InvalidStatusTransport()).plan(project)  # type: ignore[arg-type]
