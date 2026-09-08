from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterator

import pytest

from app.providers.embedding import (
    EmbeddingAuthenticationError,
    EmbeddingBatch,
    EmbeddingConfigurationError,
    EmbeddingConnectionError,
    EmbeddingHTTPError,
    EmbeddingInputError,
    EmbeddingProviderResponseError,
    EmbeddingRateLimitError,
    EmbeddingTimeout,
    OpenAICompatibleEmbeddingProvider,
)


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


def test_embedding_adapter_posts_batch_and_orders_response_indices() -> None:
    payload = {"data": [{"index": 1, "embedding": [3, 4]}, {"index": 0, "embedding": [1, 2]}]}
    with _fake_server(200, payload) as (base_url, requests):
        result = OpenAICompatibleEmbeddingProvider("  runtime-key  ", base_url=base_url, dimensions=2).embed(["first", "second"])

    assert result == EmbeddingBatch(((1.0, 2.0), (3.0, 4.0)))
    assert result.dimension == 2
    path, headers, body = requests[0]
    request = json.loads(body)
    assert path == "/v1/embeddings"
    assert headers["Authorization"] == "Bearer runtime-key"
    assert request == {"model": "text-embedding-3-small", "input": ["first", "second"], "encoding_format": "float", "dimensions": 2}


@pytest.mark.parametrize(
    ("status", "error_type"),
    [(401, EmbeddingAuthenticationError), (429, EmbeddingRateLimitError), (500, EmbeddingHTTPError)],
)
def test_embedding_http_errors_are_classified_without_key_leakage(status: int, error_type: type[Exception]) -> None:
    key = "embedding-secret"
    with _fake_server(status, {"error": {"message": key}}) as (base_url, _):
        with pytest.raises(error_type) as raised:
            OpenAICompatibleEmbeddingProvider(key, base_url=base_url).embed(["text"])
    assert key not in str(raised.value)
    if status == 500:
        assert isinstance(raised.value, EmbeddingHTTPError)
        assert raised.value.status_code == 500


def test_embedding_adapter_does_not_follow_redirect_with_byok_header() -> None:
    with _fake_server(200, {"data": [{"index": 0, "embedding": [1]}]}) as (target_url, target_requests):
        with _fake_server(302, {"redirect": "blocked"}, {"Location": f"{target_url}/embeddings"}) as (source_url, source_requests):
            with pytest.raises(EmbeddingHTTPError, match="302"):
                OpenAICompatibleEmbeddingProvider("redirect-key", base_url=source_url).embed(["text"])
    assert len(source_requests) == 1
    assert target_requests == []


def test_embedding_input_dimension_and_response_validation() -> None:
    provider = OpenAICompatibleEmbeddingProvider("key")
    with pytest.raises(EmbeddingInputError, match="1 to 2048"):
        provider.embed([])
    with pytest.raises(EmbeddingInputError, match="texts"):
        provider.embed(["   "])
    with pytest.raises(EmbeddingConfigurationError, match="dimensions"):
        OpenAICompatibleEmbeddingProvider("key", dimensions=True)

    class BadVectorTransport:
        def post(self, url: str, headers: dict[str, str], body: bytes, timeout_seconds: float) -> object:
            return type("Response", (), {"status_code": 200, "body": b'{"data":[{"index":0,"embedding":[1,"NaN"]}]}', "headers": {}})()

    with pytest.raises(EmbeddingProviderResponseError, match="invalid vectors"):
        OpenAICompatibleEmbeddingProvider("key", transport=BadVectorTransport()).embed(["text"])  # type: ignore[arg-type]

    class WrongDimensionTransport:
        def post(self, url: str, headers: dict[str, str], body: bytes, timeout_seconds: float) -> object:
            return type("Response", (), {"status_code": 200, "body": b'{"data":[{"index":0,"embedding":[1,2]}]}', "headers": {}})()

    with pytest.raises(EmbeddingProviderResponseError, match="dimension"):
        OpenAICompatibleEmbeddingProvider("key", dimensions=3, transport=WrongDimensionTransport()).embed(["text"])  # type: ignore[arg-type]


def test_embedding_configuration_timeout_and_immutable_contract_are_safe() -> None:
    with pytest.raises(EmbeddingConfigurationError, match="localhost"):
        OpenAICompatibleEmbeddingProvider("key", base_url="http://remote.example/v1")
    with pytest.raises(EmbeddingConfigurationError, match="timeout"):
        OpenAICompatibleEmbeddingProvider("key", timeout_seconds=float("nan"))

    class TimeoutTransport:
        def post(self, url: str, headers: dict[str, str], body: bytes, timeout_seconds: float) -> object:
            raise TimeoutError("private detail")

    key = "timeout-key"
    with pytest.raises(EmbeddingTimeout) as raised:
        OpenAICompatibleEmbeddingProvider(key, transport=TimeoutTransport()).embed(["text"])  # type: ignore[arg-type]
    assert key not in str(raised.value)

    class BrokenTransport:
        def post(self, url: str, headers: dict[str, str], body: bytes, timeout_seconds: float) -> object:
            raise RuntimeError(key)

    with pytest.raises(EmbeddingConnectionError) as raised:
        OpenAICompatibleEmbeddingProvider(key, transport=BrokenTransport()).embed(["text"])  # type: ignore[arg-type]
    assert key not in str(raised.value)

    batch = EmbeddingBatch(((1.0,),))
    with pytest.raises(Exception):
        batch.vectors = ()  # type: ignore[misc]
