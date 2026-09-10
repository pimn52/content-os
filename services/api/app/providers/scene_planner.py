"""Provider-neutral ScenePlan generation with a safe OpenAI-compatible adapter.

This module deliberately only creates in-memory domain contracts.  Persisting a
plan, selecting assets, and enqueueing work remain separate application steps.
"""
from __future__ import annotations

import json
import math
import socket
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Literal, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

from app.domain.models import Project, ScenePlan, SourceKind


class ScenePlannerError(RuntimeError):
    """Base class for expected, credential-safe scene-planning errors."""


class ScenePlannerConfigurationError(ScenePlannerError):
    pass


class ScenePlannerInputError(ScenePlannerError):
    pass


class ScenePlannerTimeout(ScenePlannerError):
    pass


class ScenePlannerConnectionError(ScenePlannerError):
    pass


class ScenePlannerAuthenticationError(ScenePlannerError):
    pass


class ScenePlannerRateLimitError(ScenePlannerError):
    pass


class ScenePlannerHTTPError(ScenePlannerError):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"scene planner provider returned HTTP {status_code}")


class ScenePlannerProviderResponseError(ScenePlannerError):
    pass


@dataclass(frozen=True)
class ScenePlanResult:
    """An immutable, ordered plan bound to exactly one Project."""

    project_id: UUID
    scenes: tuple[ScenePlan, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.scenes, tuple) or not self.scenes:
            raise ValueError("scene plan must be a non-empty immutable tuple")
        if any(not isinstance(scene, ScenePlan) or scene.project_id != self.project_id for scene in self.scenes):
            raise ValueError("each scene must belong to the requested project")
        if [scene.order for scene in self.scenes] != list(range(len(self.scenes))):
            raise ValueError("scene orders must be contiguous and start at zero")
        scene_ids = [scene.scene_id for scene in self.scenes]
        if len(scene_ids) != len(set(scene_ids)):
            raise ValueError("scene IDs must be unique")
        for scene in self.scenes:
            _validate_real_asset_priority(scene)


class ScenePlanner(Protocol):
    def plan(
        self,
        project: Project,
        *,
        script: str | None = None,
        topic: str | None = None,
        context: Mapping[str, object] | None = None,
    ) -> ScenePlanResult: ...


@dataclass(frozen=True)
class ScenePlannerHTTPResponse:
    status_code: int
    body: bytes
    headers: Mapping[str, str]


class ScenePlannerHTTPTransport(Protocol):
    def post(self, url: str, headers: Mapping[str, str], body: bytes, timeout_seconds: float) -> ScenePlannerHTTPResponse: ...


class _NoRedirectHandler(HTTPRedirectHandler):
    """A redirect is returned as HTTP status; credentials are never replayed."""

    def redirect_request(self, request: Request, fp: object, code: int, message: str, headers: object, new_url: str) -> None:
        return None


class UrllibScenePlannerTransport:
    """Small stdlib HTTP boundary independent from other provider adapters."""

    def post(self, url: str, headers: Mapping[str, str], body: bytes, timeout_seconds: float) -> ScenePlannerHTTPResponse:
        request = Request(url, data=body, headers=dict(headers), method="POST")
        try:
            opener = build_opener(_NoRedirectHandler())
            with opener.open(request, timeout=timeout_seconds) as response:  # noqa: S310 - endpoint is validated by the adapter.
                return ScenePlannerHTTPResponse(response.status, response.read(), dict(response.headers.items()))
        except HTTPError as exc:
            return ScenePlannerHTTPResponse(exc.code, exc.read(), dict(exc.headers.items()) if exc.headers else {})
        except socket.timeout as exc:
            raise ScenePlannerTimeout("scene planner request timed out") from exc
        except URLError as exc:
            raise ScenePlannerConnectionError("could not connect to the scene planner provider") from exc


_SOURCE_KINDS = [source.value for source in SourceKind]
_SCENE_FIELDS = (
    "scene_id", "order", "purpose", "voice_text", "duration_target_ms", "visual_intent",
    "preferred_sources", "fallback_sources", "caption_emphasis",
)
_VISUAL_INTENT_FIELDS = ("subject", "action", "framing", "description")
_SCENE_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": list(_SCENE_FIELDS),
    "properties": {
        "scene_id": {"type": "string", "minLength": 1, "maxLength": 100},
        "order": {"type": "integer", "minimum": 0, "maximum": 10_000},
        "purpose": {"type": "string", "minLength": 1, "maxLength": 100},
        "voice_text": {"type": "string", "minLength": 1, "maxLength": 10_000},
        "duration_target_ms": {"type": "integer", "minimum": 1, "maximum": 3_600_000},
        "visual_intent": {
            "type": "object", "additionalProperties": False, "required": list(_VISUAL_INTENT_FIELDS),
            "properties": {
                "subject": {"type": ["string", "null"], "maxLength": 500},
                "action": {"type": ["string", "null"], "maxLength": 500},
                "framing": {"type": ["string", "null"], "maxLength": 100},
                "description": {"type": ["string", "null"], "maxLength": 2_000},
            },
        },
        "preferred_sources": {"type": "array", "minItems": 1, "maxItems": 10, "items": {"type": "string", "enum": _SOURCE_KINDS}},
        "fallback_sources": {"type": "array", "maxItems": 10, "items": {"type": "string", "enum": _SOURCE_KINDS}},
        "caption_emphasis": {"type": "array", "maxItems": 30, "items": {"type": "string", "minLength": 1, "maxLength": 500}},
    },
}
_PLAN_SCHEMA: dict[str, object] = {
    "type": "object", "additionalProperties": False, "required": ["scenes"],
    "properties": {"scenes": {"type": "array", "minItems": 1, "maxItems": 100, "items": _SCENE_SCHEMA}},
}
_FIXED_PROMPT = (
    "Create an ordered short-video ScenePlan for the supplied Content OS project. Return only the requested JSON. "
    "Use order values starting at zero with no gaps. Each scene needs a clear purpose, spoken voice text, a positive "
    "millisecond duration, and a concrete visual intent. Prefer user_asset and historical_asset first whenever real "
    "creator media can satisfy a scene; list capture, stock, generated media, typography, screenshots, or charts as "
    "fallbacks when appropriate. Do not claim a specific asset exists and do not create a final video."
)


class OpenAICompatibleScenePlanner:
    """Runtime-key OpenAI-compatible structured ScenePlan provider.

    ``responses`` remains the default. ``chat_completions`` is an explicit
    compatibility mode for providers such as Moonshot/Kimi that expose the
    OpenAI Chat Completions shape instead of the Responses shape.
    """

    provider_name = "openai-compatible"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4.1-mini",
        protocol: Literal["responses", "chat_completions"] | str = "responses",
        timeout_seconds: float = 60.0,
        transport: ScenePlannerHTTPTransport | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ScenePlannerConfigurationError("scene planner API key must be supplied at runtime")
        normalized_base_url = _validate_base_url(base_url)
        if not isinstance(model, str) or not model.strip():
            raise ScenePlannerConfigurationError("scene planner model must not be empty")
        selected_protocol = _validate_protocol(protocol)
        if isinstance(timeout_seconds, bool):
            raise ScenePlannerConfigurationError("scene planner timeout must be finite and positive")
        try:
            timeout = float(timeout_seconds)
        except (TypeError, ValueError) as exc:
            raise ScenePlannerConfigurationError("scene planner timeout must be finite and positive") from exc
        if not math.isfinite(timeout) or timeout <= 0:
            raise ScenePlannerConfigurationError("scene planner timeout must be finite and positive")
        self._api_key = api_key.strip()
        endpoint_suffix = "responses" if selected_protocol == "responses" else "chat/completions"
        self._endpoint = f"{normalized_base_url.rstrip('/')}/{endpoint_suffix}"
        self.model = model.strip()
        self.protocol = selected_protocol
        self.timeout_seconds = timeout
        self.transport = transport or UrllibScenePlannerTransport()

    def plan(
        self,
        project: Project,
        *,
        script: str | None = None,
        topic: str | None = None,
        context: Mapping[str, object] | None = None,
    ) -> ScenePlanResult:
        if not isinstance(project, Project):
            raise ScenePlannerInputError("scene planning requires a Project contract")
        requested_script = _optional_request_text("script", script, 100_000)
        requested_topic = _optional_request_text("topic", topic, 5_000) or project.topic
        request_context = {
            "project": {
                "title": project.title,
                "topic": project.topic,
                "format": project.format.value,
                "resolution_width": project.resolution_width,
                "resolution_height": project.resolution_height,
                "fps": {"numerator": project.fps.numerator, "denominator": project.fps.denominator},
            },
            "requested_topic": requested_topic,
            "user_script": requested_script,
        }
        if context is not None:
            request_context["creator_context"] = context
        prompt = _FIXED_PROMPT + "\n\nProject request:\n" + json.dumps(request_context, ensure_ascii=False)
        if self.protocol == "responses":
            body = {
                "model": self.model,
                "store": False,
                "input": [{"role": "user", "content": [
                    {"type": "input_text", "text": prompt},
                ]}],
                "text": {"format": {"type": "json_schema", "name": "scene_plan", "strict": True, "schema": _PLAN_SCHEMA}},
            }
        else:
            # Chat-completions providers do not share the Responses
            # ``text.format`` contract. Keep the same strict prompt and
            # validate the returned JSON locally before it reaches the domain.
            body = {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
            }
        try:
            response = self.transport.post(
                self._endpoint,
                {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json", "Accept": "application/json"},
                json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                self.timeout_seconds,
            )
        except ScenePlannerError:
            raise
        except TimeoutError as exc:
            raise ScenePlannerTimeout("scene planner request timed out") from exc
        except OSError as exc:
            raise ScenePlannerConnectionError("could not connect to the scene planner provider") from exc
        except Exception:
            raise ScenePlannerConnectionError("scene planner transport request failed") from None
        _raise_for_status(response.status_code)
        return _parse_response(response.body, project, protocol=self.protocol)


OpenAICompatibleScenePlanProvider = OpenAICompatibleScenePlanner


def _optional_request_text(name: str, value: object, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ScenePlannerInputError(f"scene planner {name} must be a non-empty string within its maximum length")
    return value


def _validate_protocol(value: object) -> Literal["responses", "chat_completions"]:
    if not isinstance(value, str) or value not in {"responses", "chat_completions"}:
        raise ScenePlannerConfigurationError("scene planner protocol must be responses or chat_completions")
    return value  # type: ignore[return-value]


def _raise_for_status(status_code: object) -> None:
    if isinstance(status_code, bool) or not isinstance(status_code, int) or not 100 <= status_code <= 599:
        raise ScenePlannerProviderResponseError("scene planner transport returned an invalid HTTP status")
    if 200 <= status_code < 300:
        return
    if status_code in {401, 403}:
        raise ScenePlannerAuthenticationError("scene planner authentication was rejected")
    if status_code == 429:
        raise ScenePlannerRateLimitError("scene planner provider rate limit was reached")
    raise ScenePlannerHTTPError(status_code)


def _parse_response(
    body: bytes,
    project: Project,
    *,
    protocol: Literal["responses", "chat_completions"] = "responses",
) -> ScenePlanResult:
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScenePlannerProviderResponseError("scene planner provider returned malformed JSON") from exc
    if not isinstance(document, dict):
        raise ScenePlannerProviderResponseError("scene planner provider response has no output message")
    if protocol == "responses":
        if not isinstance(document.get("output"), list):
            raise ScenePlannerProviderResponseError("scene planner provider response has no output message")
        texts = [
            content.get("text")
            for output in document["output"]
            if isinstance(output, dict) and output.get("type") == "message" and isinstance(output.get("content"), list)
            for content in output["content"]
            if isinstance(content, dict) and content.get("type") == "output_text" and isinstance(content.get("text"), str)
        ]
    else:
        choices = document.get("choices")
        message = choices[0].get("message") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            texts = [content]
        elif isinstance(content, list):
            texts = [block.get("text") for block in content if isinstance(block, dict) and isinstance(block.get("text"), str)]
        else:
            texts = []
    if len(texts) != 1:
        raise ScenePlannerProviderResponseError("scene planner provider response has no structured output text")
    try:
        cleaned = texts[0].strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        value = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ScenePlannerProviderResponseError("scene planner provider returned invalid structured output") from exc
    if not isinstance(value, dict) or set(value) != {"scenes"} or not isinstance(value["scenes"], list):
        raise ScenePlannerProviderResponseError("scene planner provider returned an incomplete scene plan")
    try:
        raw_scenes = value["scenes"]
        if any(
            not isinstance(item, dict)
            or set(item) != set(_SCENE_FIELDS)
            or not isinstance(item.get("visual_intent"), dict)
            or set(item["visual_intent"]) != set(_VISUAL_INTENT_FIELDS)
            for item in raw_scenes
        ):
            raise ValueError("a scene does not match the strict output shape")
        scenes = tuple(ScenePlan(project_id=project.id, **item) for item in raw_scenes)
        return ScenePlanResult(project_id=project.id, scenes=scenes)
    except (TypeError, ValueError) as exc:
        raise ScenePlannerProviderResponseError("scene planner provider returned an invalid scene plan") from exc


def _validate_real_asset_priority(scene: ScenePlan) -> None:
    real_assets = {SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET}
    preferred_real_positions = [index for index, source in enumerate(scene.preferred_sources) if source in real_assets]
    preferred_other_positions = [index for index, source in enumerate(scene.preferred_sources) if source not in real_assets]
    if preferred_real_positions and preferred_other_positions and min(preferred_other_positions) < max(preferred_real_positions):
        raise ValueError("user and historical assets must precede other preferred sources")
    if real_assets.intersection(scene.fallback_sources) and not real_assets.intersection(scene.preferred_sources):
        raise ValueError("user and historical assets must not be fallback-only sources")


def _validate_base_url(base_url: object) -> str:
    if not isinstance(base_url, str) or not base_url or base_url != base_url.strip():
        raise ScenePlannerConfigurationError("scene planner base URL must not be empty")
    if any(character.isspace() or ord(character) < 32 for character in base_url):
        raise ScenePlannerConfigurationError("scene planner base URL is invalid")
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except ValueError as exc:
        raise ScenePlannerConfigurationError("scene planner base URL is invalid") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port is not None and not 1 <= port <= 65535
    ):
        raise ScenePlannerConfigurationError("scene planner base URL is invalid")
    if parsed.scheme.lower() == "http" and not _is_loopback_host(parsed.hostname):
        raise ScenePlannerConfigurationError("HTTP scene planner endpoints must use a localhost or loopback host")
    return base_url


def _is_loopback_host(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ip_address(hostname).is_loopback
    except ValueError:
        return False
