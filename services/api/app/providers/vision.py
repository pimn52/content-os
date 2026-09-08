"""Provider-neutral visual analysis and a safe OpenAI-compatible adapter."""
from __future__ import annotations

import base64
import json
import math
import socket
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from typing import Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class VisionError(RuntimeError):
    """Expected credential-safe vision provider errors."""


class VisionConfigurationError(VisionError):
    pass


class VisionInputError(VisionError):
    pass


class VisionTimeout(VisionError):
    pass


class VisionConnectionError(VisionError):
    pass


class VisionAuthenticationError(VisionError):
    pass


class VisionRateLimitError(VisionError):
    pass


class VisionHTTPError(VisionError):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"vision provider returned HTTP {status_code}")


class VisionProviderResponseError(VisionError):
    pass


@dataclass(frozen=True)
class ClipVisualAnalysis:
    """Non-identifying metadata compatible with existing ``Clip`` fields."""

    visual_description: str | None
    people: tuple[str, ...]
    objects: tuple[str, ...]
    location: str | None
    action: str | None
    shot_type: str | None
    quality_score: float | None
    face_visibility: float | None
    mouth_visibility: float | None
    talking_candidate: bool

    def __post_init__(self) -> None:
        _optional_text("visual_description", self.visual_description, 5_000)
        _text_items("people", self.people, 30, 500)
        _text_items("objects", self.objects, 100, 500)
        _optional_text("location", self.location, 500)
        _optional_text("action", self.action, 500)
        _optional_text("shot_type", self.shot_type, 100)
        _score("quality_score", self.quality_score)
        _score("face_visibility", self.face_visibility)
        _score("mouth_visibility", self.mouth_visibility)
        if not isinstance(self.talking_candidate, bool):
            raise ValueError("talking_candidate must be a boolean")


class VisionProvider(Protocol):
    def analyze(self, keyframe_path: str | Path) -> ClipVisualAnalysis: ...


@dataclass(frozen=True)
class VisionHTTPResponse:
    status_code: int
    body: bytes
    headers: Mapping[str, str]


class VisionHTTPTransport(Protocol):
    def post(self, url: str, headers: Mapping[str, str], body: bytes, timeout_seconds: float) -> VisionHTTPResponse: ...


class _NoRedirectHandler(HTTPRedirectHandler):
    """Never replay BYOK authorization to a redirect target."""

    def redirect_request(self, request: Request, fp: object, code: int, message: str, headers: object, new_url: str) -> None:
        return None


class UrllibVisionTransport:
    """Small stdlib-only HTTP boundary, independent from ASR."""

    def post(self, url: str, headers: Mapping[str, str], body: bytes, timeout_seconds: float) -> VisionHTTPResponse:
        request = Request(url, data=body, headers=dict(headers), method="POST")
        try:
            opener = build_opener(_NoRedirectHandler())
            with opener.open(request, timeout=timeout_seconds) as response:  # noqa: S310 - base URL is validated.
                return VisionHTTPResponse(response.status, response.read(), dict(response.headers.items()))
        except HTTPError as exc:
            return VisionHTTPResponse(exc.code, exc.read(), dict(exc.headers.items()) if exc.headers else {})
        except socket.timeout as exc:
            raise VisionTimeout("vision request timed out") from exc
        except URLError as exc:
            raise VisionConnectionError("could not connect to the vision provider") from exc


_REQUIRED = (
    "visual_description", "people", "objects", "location", "action", "shot_type",
    "quality_score", "face_visibility", "mouth_visibility", "talking_candidate",
)
_ANALYSIS_SCHEMA: dict[str, object] = {
    "type": "object", "additionalProperties": False, "required": list(_REQUIRED),
    "properties": {
        "visual_description": {"type": ["string", "null"], "maxLength": 5_000},
        "people": {"type": "array", "maxItems": 30, "items": {"type": "string", "minLength": 1, "maxLength": 500}},
        "objects": {"type": "array", "maxItems": 100, "items": {"type": "string", "minLength": 1, "maxLength": 500}},
        "location": {"type": ["string", "null"], "maxLength": 500},
        "action": {"type": ["string", "null"], "maxLength": 500},
        "shot_type": {"type": ["string", "null"], "maxLength": 100},
        "quality_score": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
        "face_visibility": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
        "mouth_visibility": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
        "talking_candidate": {"type": "boolean"},
    },
}
_FIXED_PROMPT = (
    "Analyze this single local video keyframe for media-library retrieval. Return only the requested JSON schema. "
    "Describe visible people only with non-identifying visual roles or appearance. Do not identify, name, recognize, "
    "or guess identity. Do not infer sensitive attributes, including race, ethnicity, nationality, religion, health, "
    "disability, age, gender identity, sexual orientation, or mental state. Only state what is visibly supported."
)


class OpenAICompatibleVisionProvider:
    """BYOK ``POST /v1/responses`` adapter for local JPEG keyframes only."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4.1-mini",
        detail: str = "low",
        timeout_seconds: float = 60.0,
        transport: VisionHTTPTransport | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise VisionConfigurationError("vision API key must be supplied at runtime")
        normalized_base_url = _validate_base_url(base_url)
        if not isinstance(model, str) or not model.strip():
            raise VisionConfigurationError("vision model must not be empty")
        if detail not in {"low", "high", "auto"}:
            raise VisionConfigurationError("vision detail must be low, high, or auto")
        if isinstance(timeout_seconds, bool):
            raise VisionConfigurationError("vision timeout must be finite and positive")
        try:
            timeout = float(timeout_seconds)
        except (TypeError, ValueError) as exc:
            raise VisionConfigurationError("vision timeout must be finite and positive") from exc
        if not math.isfinite(timeout) or timeout <= 0:
            raise VisionConfigurationError("vision timeout must be finite and positive")
        self._api_key = api_key.strip()
        self._endpoint = f"{normalized_base_url.rstrip('/')}/responses"
        self.model = model.strip()
        self.detail = detail
        self.timeout_seconds = timeout
        self.transport = transport or UrllibVisionTransport()

    def analyze(self, keyframe_path: str | Path) -> ClipVisualAnalysis:
        image = _local_jpeg_bytes(keyframe_path)
        image_url = "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii")
        body = {
            "model": self.model,
            "store": False,
            "input": [{"role": "user", "content": [
                {"type": "input_text", "text": _FIXED_PROMPT},
                {"type": "input_image", "image_url": image_url, "detail": self.detail},
            ]}],
            "text": {"format": {"type": "json_schema", "name": "clip_visual_analysis", "strict": True, "schema": _ANALYSIS_SCHEMA}},
        }
        try:
            response = self.transport.post(
                self._endpoint,
                {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json", "Accept": "application/json"},
                json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                self.timeout_seconds,
            )
        except VisionError:
            raise
        except TimeoutError as exc:
            raise VisionTimeout("vision request timed out") from exc
        except OSError as exc:
            raise VisionConnectionError("could not connect to the vision provider") from exc
        except Exception:
            raise VisionConnectionError("vision transport request failed") from None
        _raise_for_status(response.status_code)
        return _parse_response(response.body)


OpenAICompatibleVisualAnalysisAdapter = OpenAICompatibleVisionProvider


def _local_jpeg_bytes(value: str | Path) -> bytes:
    path = Path(value)
    if not path.is_file():
        raise VisionInputError("vision keyframe does not exist")
    if path.suffix.lower() not in {".jpg", ".jpeg"}:
        raise VisionInputError("vision keyframe must be a local JPEG file")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise VisionInputError("vision keyframe cannot be read") from exc
    if len(data) < 4 or not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
        raise VisionInputError("vision keyframe is not a valid JPEG file")
    return data


def _raise_for_status(status_code: object) -> None:
    if isinstance(status_code, bool) or not isinstance(status_code, int) or not 100 <= status_code <= 599:
        raise VisionProviderResponseError("vision transport returned an invalid HTTP status")
    if 200 <= status_code < 300:
        return
    if status_code in {401, 403}:
        raise VisionAuthenticationError("vision authentication was rejected")
    if status_code == 429:
        raise VisionRateLimitError("vision provider rate limit was reached")
    raise VisionHTTPError(status_code)


def _parse_response(body: bytes) -> ClipVisualAnalysis:
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VisionProviderResponseError("vision provider returned malformed JSON") from exc
    if not isinstance(document, dict) or not isinstance(document.get("output"), list):
        raise VisionProviderResponseError("vision provider response has no output message")
    texts = [
        content.get("text")
        for output in document["output"]
        if isinstance(output, dict) and output.get("type") == "message" and isinstance(output.get("content"), list)
        for content in output["content"]
        if isinstance(content, dict) and content.get("type") == "output_text" and isinstance(content.get("text"), str)
    ]
    if len(texts) != 1:
        raise VisionProviderResponseError("vision provider response has no structured output text")
    try:
        value = json.loads(texts[0])
    except json.JSONDecodeError as exc:
        raise VisionProviderResponseError("vision provider returned invalid structured output") from exc
    if not isinstance(value, dict) or set(value) != set(_REQUIRED):
        raise VisionProviderResponseError("vision provider returned an incomplete visual analysis")
    try:
        return ClipVisualAnalysis(
            visual_description=value["visual_description"], people=_tuple_of_strings(value["people"]),
            objects=_tuple_of_strings(value["objects"]), location=value["location"], action=value["action"],
            shot_type=value["shot_type"], quality_score=value["quality_score"],
            face_visibility=value["face_visibility"], mouth_visibility=value["mouth_visibility"],
            talking_candidate=value["talking_candidate"],
        )
    except (TypeError, ValueError) as exc:
        raise VisionProviderResponseError("vision provider returned an invalid visual analysis") from exc


def _tuple_of_strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError("expected an array")
    return tuple(value)


def _optional_text(name: str, value: object, maximum: int) -> None:
    if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > maximum):
        raise ValueError(f"{name} must be a non-empty string within its maximum length or None")


def _text_items(name: str, value: object, maximum_count: int, maximum_length: int) -> None:
    if not isinstance(value, tuple) or len(value) > maximum_count:
        raise ValueError(f"{name} must be an immutable list within its maximum length")
    if any(not isinstance(item, str) or not item.strip() or len(item) > maximum_length for item in value):
        raise ValueError(f"{name} items must be non-empty strings within their maximum length")


def _score(name: str, value: object) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or not 0 <= float(value) <= 1:
        raise ValueError(f"{name} must be a finite score from 0 to 1 or None")


def _validate_base_url(base_url: object) -> str:
    if not isinstance(base_url, str) or not base_url or base_url != base_url.strip():
        raise VisionConfigurationError("vision base URL must not be empty")
    if any(character.isspace() or ord(character) < 32 for character in base_url):
        raise VisionConfigurationError("vision base URL is invalid")
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except ValueError as exc:
        raise VisionConfigurationError("vision base URL is invalid") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port is not None and not 1 <= port <= 65535
    ):
        raise VisionConfigurationError("vision base URL is invalid")
    if parsed.scheme.lower() == "http" and not _is_loopback_host(parsed.hostname):
        raise VisionConfigurationError("HTTP vision endpoints must use a localhost or loopback host")
    return base_url


def _is_loopback_host(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ip_address(hostname).is_loopback
    except ValueError:
        return False
