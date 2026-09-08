from __future__ import annotations

import base64
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import pytest

from app.providers.vision import (
    ClipVisualAnalysis,
    OpenAICompatibleVisionProvider,
    VisionAuthenticationError,
    VisionConfigurationError,
    VisionConnectionError,
    VisionHTTPError,
    VisionInputError,
    VisionProviderResponseError,
    VisionRateLimitError,
    VisionTimeout,
)


def _analysis() -> dict[str, object]:
    return {
        "visual_description": "A person works at a desk beside a laptop.",
        "people": ["person seated at a desk"],
        "objects": ["laptop", "desk"],
        "location": "office", "action": "working at a laptop", "shot_type": "medium shot",
        "quality_score": 0.8, "face_visibility": 0.7, "mouth_visibility": 0.6,
        "talking_candidate": True,
    }


def _response(analysis: dict[str, object] | None = None) -> dict[str, object]:
    return {"output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(analysis or _analysis())}]}]}


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


def _jpeg(tmp_path: Path) -> Path:
    path = tmp_path / "关键帧.JPG"
    path.write_bytes(b"\xff\xd8fake-jpeg-bytes\xff\xd9")
    return path


def test_openai_compatible_vision_request_uses_schema_and_local_jpeg(tmp_path: Path) -> None:
    image = _jpeg(tmp_path)
    with _fake_server(200, _response()) as (base_url, requests):
        result = OpenAICompatibleVisionProvider("  runtime-secret  ", base_url=base_url).analyze(image)

    assert isinstance(result, ClipVisualAnalysis)
    assert result.people == ("person seated at a desk",)
    assert result.objects == ("laptop", "desk")
    assert result.talking_candidate is True
    path, headers, body = requests[0]
    request = json.loads(body)
    assert path == "/v1/responses"
    assert headers["Authorization"] == "Bearer runtime-secret"
    assert request["model"] == "gpt-4.1-mini"
    assert request["store"] is False
    assert request["text"]["format"]["type"] == "json_schema"
    assert request["text"]["format"]["schema"]["additionalProperties"] is False
    content = request["input"][0]["content"]
    assert content[0]["type"] == "input_text"
    assert "Do not identify" in content[0]["text"]
    assert content[1]["type"] == "input_image" and content[1]["detail"] == "low"
    prefix, encoded = content[1]["image_url"].split(",", 1)
    assert prefix == "data:image/jpeg;base64"
    assert base64.b64decode(encoded) == image.read_bytes()


@pytest.mark.parametrize(
    ("status", "error_type"),
    [(401, VisionAuthenticationError), (429, VisionRateLimitError), (500, VisionHTTPError)],
)
def test_vision_errors_are_classified_without_key_leakage(
    tmp_path: Path, status: int, error_type: type[Exception]
) -> None:
    key = "vision-key-must-not-leak"
    with _fake_server(status, {"error": {"message": key}}) as (base_url, _):
        with pytest.raises(error_type) as raised:
            OpenAICompatibleVisionProvider(key, base_url=base_url).analyze(_jpeg(tmp_path))
    assert key not in str(raised.value)
    if status == 500:
        assert isinstance(raised.value, VisionHTTPError)
        assert raised.value.status_code == 500


def test_vision_does_not_follow_redirect_with_byok_header(tmp_path: Path) -> None:
    with _fake_server(200, _response()) as (target_url, target_requests):
        with _fake_server(302, {"redirect": "blocked"}, {"Location": f"{target_url}/responses"}) as (source_url, source_requests):
            with pytest.raises(VisionHTTPError, match="302"):
                OpenAICompatibleVisionProvider("redirect-secret", base_url=source_url).analyze(_jpeg(tmp_path))
    assert len(source_requests) == 1
    assert target_requests == []


def test_vision_rejects_nonlocal_or_invalid_jpeg_and_malformed_response(tmp_path: Path) -> None:
    with pytest.raises(VisionInputError, match="does not exist"):
        OpenAICompatibleVisionProvider("key").analyze("https://example.test/image.jpg")
    png = tmp_path / "image.png"
    png.write_bytes(b"not image")
    with pytest.raises(VisionInputError, match="JPEG"):
        OpenAICompatibleVisionProvider("key").analyze(png)
    broken = tmp_path / "image.jpg"
    broken.write_bytes(b"not a jpeg")
    with pytest.raises(VisionInputError, match="valid JPEG"):
        OpenAICompatibleVisionProvider("key").analyze(broken)
    invalid = _analysis()
    invalid["quality_score"] = 1.1
    with _fake_server(200, _response(invalid)) as (base_url, _):
        with pytest.raises(VisionProviderResponseError, match="invalid visual"):
            OpenAICompatibleVisionProvider("key", base_url=base_url).analyze(_jpeg(tmp_path))


def test_vision_configuration_and_injected_timeout_are_safe(tmp_path: Path) -> None:
    with pytest.raises(VisionConfigurationError, match="localhost"):
        OpenAICompatibleVisionProvider("key", base_url="http://remote.example/v1")
    with pytest.raises(VisionConfigurationError, match="detail"):
        OpenAICompatibleVisionProvider("key", detail="full")

    class TimeoutTransport:
        def post(self, url: str, headers: dict[str, str], body: bytes, timeout_seconds: float) -> object:
            raise TimeoutError("secret transport detail")

    key = "timeout-key"
    with pytest.raises(VisionTimeout) as raised:
        OpenAICompatibleVisionProvider(key, transport=TimeoutTransport()).analyze(_jpeg(tmp_path))  # type: ignore[arg-type]
    assert key not in str(raised.value)

    class BrokenTransport:
        def post(self, url: str, headers: dict[str, str], body: bytes, timeout_seconds: float) -> object:
            raise RuntimeError(key)

    with pytest.raises(VisionConnectionError) as raised:
        OpenAICompatibleVisionProvider(key, transport=BrokenTransport()).analyze(_jpeg(tmp_path))  # type: ignore[arg-type]
    assert key not in str(raised.value)


def test_visual_analysis_contract_is_immutable_and_strict() -> None:
    analysis = ClipVisualAnalysis(
        visual_description=None,
        people=(), objects=(), location=None, action=None, shot_type=None,
        quality_score=0, face_visibility=None, mouth_visibility=None, talking_candidate=False,
    )
    with pytest.raises(Exception):
        analysis.people = ("replacement",)  # type: ignore[misc]
    with pytest.raises(ValueError, match="immutable"):
        ClipVisualAnalysis(
            visual_description=None,
            people=["person"],  # type: ignore[arg-type]
            objects=(), location=None, action=None, shot_type=None,
            quality_score=None, face_visibility=None, mouth_visibility=None, talking_candidate=False,
        )
    with pytest.raises(ValueError, match="score"):
        ClipVisualAnalysis(
            visual_description=None,
            people=(), objects=(), location=None, action=None, shot_type=None,
            quality_score=float("nan"), face_visibility=None, mouth_visibility=None, talking_candidate=False,
        )
