from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import pytest

from app.providers.asr import (
    ASRAuthenticationError,
    ASRConfigurationError,
    ASRConnectionError,
    ASRHTTPError,
    ASRInputError,
    ASRProviderResponseError,
    ASRRateLimitError,
    ASRTimeout,
    FasterWhisperASRProvider,
    HTTPResponse,
    OpenAICompatibleASRProvider,
    TranscriptionSegment,
    _multipart_body,
)


@contextmanager
def _fake_asr_server(
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


def test_openai_compatible_adapter_posts_multipart_verbose_segments(tmp_path: Path) -> None:
    audio = tmp_path / "采访 中文.wav"
    audio.write_bytes(b"RIFF fake wav bytes")
    payload = {
        "text": "hello world",
        "language": "en",
        "segments": [
            {"start": "0", "end": "0.7501", "text": "hello"},
            {"start": 0.7501, "end": 1.5, "text": "world"},
        ],
    }
    with _fake_asr_server(200, payload) as (base_url, requests):
        provider = OpenAICompatibleASRProvider("  test-secret-never-log  ", base_url=base_url)
        result = provider.transcribe(audio, language="en", prompt="keep product names")

    assert result.text == "hello world"
    assert [(segment.start_ms, segment.end_ms, segment.text) for segment in result.segments] == [
        (0, 751, "hello"), (750, 1500, "world"),
    ]
    assert result.language == "en"
    assert len(requests) == 1
    path, headers, body = requests[0]
    assert path == "/v1/audio/transcriptions"
    assert headers["Authorization"] == "Bearer test-secret-never-log"
    assert headers["Content-Type"].startswith("multipart/form-data; boundary=")
    assert b'name="model"' in body and b"whisper-1" in body
    assert b'name="response_format"' in body and b"verbose_json" in body
    assert b'name="timestamp_granularities[]"' in body and b"segment" in body
    assert b'name="language"' in body and b'name="prompt"' in body
    assert b'filename="' in body and audio.name.encode() in body
    assert b"Content-Type: audio/" in body and audio.read_bytes() in body


@pytest.mark.parametrize(
    ("status", "error_type"),
    [(401, ASRAuthenticationError), (429, ASRRateLimitError), (500, ASRHTTPError)],
)
def test_provider_http_errors_are_classified_without_key_leakage(tmp_path: Path, status: int, error_type: type[Exception]) -> None:
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")
    key = "super-secret-key"
    with _fake_asr_server(status, {"error": {"message": key}}) as (base_url, _):
        provider = OpenAICompatibleASRProvider(key, base_url=base_url)
        with pytest.raises(error_type) as raised:
            provider.transcribe(audio)
    assert key not in str(raised.value)
    if status == 500:
        assert isinstance(raised.value, ASRHTTPError)
        assert raised.value.status_code == 500


def test_provider_does_not_follow_redirect_with_byok_header(tmp_path: Path) -> None:
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")
    with _fake_asr_server(200, {"text": "should not be requested", "segments": []}) as (target_url, target_requests):
        with _fake_asr_server(
            302,
            {"redirect": "blocked"},
            {"Location": f"{target_url}/audio/transcriptions"},
        ) as (source_url, source_requests):
            with pytest.raises(ASRHTTPError, match="302"):
                OpenAICompatibleASRProvider("redirect-secret", base_url=source_url).transcribe(audio)

    assert len(source_requests) == 1
    assert target_requests == []


def test_provider_rejects_malformed_segments_and_input(tmp_path: Path) -> None:
    extensionless = tmp_path / "audio"
    extensionless.write_bytes(b"audio")
    with pytest.raises(ASRInputError, match="extension"):
        OpenAICompatibleASRProvider("runtime-key").transcribe(extensionless)
    with _fake_asr_server(200, {"text": "x", "segments": [{"start": 1, "end": 1, "text": "bad"}]}) as (base_url, _):
        with pytest.raises(ASRInputError, match="does not exist"):
            OpenAICompatibleASRProvider("runtime-key", base_url=base_url).transcribe(tmp_path / "missing.wav")
        audio = tmp_path / "audio.wav"
        audio.write_bytes(b"audio")
        with pytest.raises(ASRProviderResponseError, match="timestamps"):
            OpenAICompatibleASRProvider("runtime-key", base_url=base_url).transcribe(audio)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://example.com/v1",
        "ftp://localhost/v1",
        "https://example.test/v1?unexpected=true",
        "https://example.test/v1#fragment",
        "https://user:password@example.test/v1",
        "https:///v1",
    ],
)
def test_provider_rejects_unsafe_base_urls(base_url: str) -> None:
    with pytest.raises(ASRConfigurationError, match="URL|localhost"):
        OpenAICompatibleASRProvider("runtime-key", base_url=base_url)


def test_provider_rejects_unsupported_extension_before_transport(tmp_path: Path) -> None:
    unsupported = tmp_path / "audio.txt"
    unsupported.write_bytes(b"not audio")

    class NoRequestTransport:
        def post(self, url: str, headers: dict[str, str], body: bytes, timeout_seconds: float) -> HTTPResponse:
            raise AssertionError("unsupported input must not make a request")

    provider = OpenAICompatibleASRProvider("runtime-key", transport=NoRequestTransport())
    with pytest.raises(ASRInputError, match="not supported"):
        provider.transcribe(unsupported)


@pytest.mark.parametrize("filename", ['bad"name.wav', "bad\\name.wav", "bad\x01name.wav"])
def test_multipart_rejects_unsafe_filenames(filename: str) -> None:
    class Upload:
        name = filename

        @staticmethod
        def read_bytes() -> bytes:
            return b"audio"

    with pytest.raises(ASRInputError, match="filename"):
        _multipart_body(Upload(), "whisper-1", None, None)  # type: ignore[arg-type]


@pytest.mark.parametrize("status_code", [True, "200", 99, 600])
def test_invalid_transport_status_is_safe_provider_response_error(tmp_path: Path, status_code: object) -> None:
    audio = tmp_path / "audio.WAV"
    audio.write_bytes(b"audio")

    class InvalidStatusTransport:
        def post(self, url: str, headers: dict[str, str], body: bytes, timeout_seconds: float) -> HTTPResponse:
            return HTTPResponse(status_code, b'{"text": "must not parse", "segments": []}', {})  # type: ignore[arg-type]

    with pytest.raises(ASRProviderResponseError, match="invalid HTTP status"):
        OpenAICompatibleASRProvider("runtime-key", transport=InvalidStatusTransport()).transcribe(audio)


def test_configuration_and_injected_timeout_do_not_expose_key(tmp_path: Path) -> None:
    with pytest.raises(ASRConfigurationError, match="API key"):
        OpenAICompatibleASRProvider("")
    with pytest.raises(ASRConfigurationError, match="timeout"):
        OpenAICompatibleASRProvider("runtime-key", timeout_seconds=float("nan"))
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")

    class TimeoutTransport:
        def post(self, url: str, headers: dict[str, str], body: bytes, timeout_seconds: float) -> HTTPResponse:
            raise TimeoutError("network timeout")

    key = "do-not-leak-this"
    with pytest.raises(ASRTimeout) as raised:
        OpenAICompatibleASRProvider(key, transport=TimeoutTransport()).transcribe(audio)
    assert key not in str(raised.value)

    class BrokenTransport:
        def post(self, url: str, headers: dict[str, str], body: bytes, timeout_seconds: float) -> HTTPResponse:
            raise RuntimeError(key)

    with pytest.raises(ASRConnectionError) as raised:
        OpenAICompatibleASRProvider(key, transport=BrokenTransport()).transcribe(audio)
    assert key not in str(raised.value)


def test_immutable_provider_neutral_segment_contract() -> None:
    segment = TranscriptionSegment(0, 1, "text")
    with pytest.raises(Exception):
        segment.start_ms = 1  # type: ignore[misc]
    with pytest.raises(ValueError):
        TranscriptionSegment(1, 1, "text")


def test_local_faster_whisper_adapter_maps_timestamped_segments_without_network(tmp_path: Path) -> None:
    audio = tmp_path / "local.wav"
    audio.write_bytes(b"audio")
    calls: list[tuple[str, dict[str, object]]] = []

    class Segment:
        def __init__(self, start: float, end: float, text: str) -> None:
            self.start = start
            self.end = end
            self.text = text

    class Info:
        language = "zh"

    class Model:
        def transcribe(self, path: str, **kwargs: object):
            calls.append((path, kwargs))
            return iter([Segment(0.001, 0.851, "  第一段 "), Segment(0.900, 1.600, "第二段")]), Info()

    def factory(model: str, **kwargs: object) -> Model:
        assert model == "tiny"
        assert kwargs == {"device": "cpu", "compute_type": "int8"}
        return Model()

    result = FasterWhisperASRProvider("tiny", model_factory=factory).transcribe(
        audio, language="zh", prompt="保留专有名词"
    )

    assert result.text == "第一段 第二段"
    assert result.language == "zh"
    assert [(item.start_ms, item.end_ms, item.text) for item in result.segments] == [
        (1, 851, "第一段"),
        (900, 1600, "第二段"),
    ]
    assert calls == [(str(audio), {"language": "zh", "initial_prompt": "保留专有名词", "vad_filter": True})]


def test_local_faster_whisper_adapter_hides_model_loader_diagnostics(tmp_path: Path) -> None:
    audio = tmp_path / "local.wav"
    audio.write_bytes(b"audio")
    provider = FasterWhisperASRProvider("tiny", model_factory=lambda *_args, **_kwargs: (_ for _ in ()).throw(ImportError()))
    with pytest.raises(ASRConfigurationError, match="could not be loaded"):
        provider.transcribe(audio)
