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
            project, script="Open with the misconception.", topic="When to avoid Vibe Coding"
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
