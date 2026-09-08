"""Provider-neutral embeddings with a safe OpenAI-compatible BYOK adapter."""
from __future__ import annotations

import json
import math
import socket
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class EmbeddingError(RuntimeError):
    """Base class for expected credential-safe embedding errors."""


class EmbeddingConfigurationError(EmbeddingError):
    pass


class EmbeddingInputError(EmbeddingError):
    pass


class EmbeddingTimeout(EmbeddingError):
    pass


class EmbeddingConnectionError(EmbeddingError):
    pass


class EmbeddingAuthenticationError(EmbeddingError):
    pass


class EmbeddingRateLimitError(EmbeddingError):
    pass


class EmbeddingHTTPError(EmbeddingError):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"embedding provider returned HTTP {status_code}")


class EmbeddingProviderResponseError(EmbeddingError):
    pass


@dataclass(frozen=True)
class EmbeddingBatch:
    """Ordered, immutable vectors corresponding exactly to submitted texts."""

    vectors: tuple[tuple[float, ...], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.vectors, tuple) or not self.vectors:
            raise ValueError("embedding vectors must be a non-empty immutable tuple")
        dimension: int | None = None
        for vector in self.vectors:
            if not isinstance(vector, tuple) or not vector:
                raise ValueError("each embedding vector must be a non-empty immutable tuple")
            if dimension is None:
                dimension = len(vector)
            elif len(vector) != dimension:
                raise ValueError("embedding vectors must have one shared dimension")
            if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) for value in vector):
                raise ValueError("embedding values must be finite numeric values")

    @property
    def dimension(self) -> int:
        return len(self.vectors[0])


class EmbeddingProvider(Protocol):
    def embed(self, texts: Sequence[str]) -> EmbeddingBatch: ...


@dataclass(frozen=True)
class EmbeddingHTTPResponse:
    status_code: int
    body: bytes
    headers: Mapping[str, str]


class EmbeddingHTTPTransport(Protocol):
    def post(self, url: str, headers: Mapping[str, str], body: bytes, timeout_seconds: float) -> EmbeddingHTTPResponse: ...


class _NoRedirectHandler(HTTPRedirectHandler):
    """Do not replay a BYOK authorization header at redirect locations."""

    def redirect_request(self, request: Request, fp: object, code: int, message: str, headers: object, new_url: str) -> None:
        return None


class UrllibEmbeddingTransport:
    """A stdlib transport kept separate from media-provider adapters."""

    def post(self, url: str, headers: Mapping[str, str], body: bytes, timeout_seconds: float) -> EmbeddingHTTPResponse:
        request = Request(url, data=body, headers=dict(headers), method="POST")
        try:
            opener = build_opener(_NoRedirectHandler())
            with opener.open(request, timeout=timeout_seconds) as response:  # noqa: S310 - endpoint validation is local.
                return EmbeddingHTTPResponse(response.status, response.read(), dict(response.headers.items()))
        except HTTPError as exc:
            return EmbeddingHTTPResponse(exc.code, exc.read(), dict(exc.headers.items()) if exc.headers else {})
        except socket.timeout as exc:
            raise EmbeddingTimeout("embedding request timed out") from exc
        except URLError as exc:
            raise EmbeddingConnectionError("could not connect to the embedding provider") from exc


class OpenAICompatibleEmbeddingProvider:
    """Runtime-key ``POST /v1/embeddings`` adapter with ordered batch results."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.openai.com/v1",
        model: str = "text-embedding-3-small",
        dimensions: int | None = None,
        timeout_seconds: float = 60.0,
        transport: EmbeddingHTTPTransport | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise EmbeddingConfigurationError("embedding API key must be supplied at runtime")
        normalized_base_url = _validate_base_url(base_url)
        if not isinstance(model, str) or not model.strip():
            raise EmbeddingConfigurationError("embedding model must not be empty")
        if dimensions is not None and (isinstance(dimensions, bool) or not isinstance(dimensions, int) or not 1 <= dimensions <= 100_000):
            raise EmbeddingConfigurationError("embedding dimensions must be an integer from 1 to 100000 or None")
        if isinstance(timeout_seconds, bool):
            raise EmbeddingConfigurationError("embedding timeout must be finite and positive")
        try:
            timeout = float(timeout_seconds)
        except (TypeError, ValueError) as exc:
            raise EmbeddingConfigurationError("embedding timeout must be finite and positive") from exc
        if not math.isfinite(timeout) or timeout <= 0:
            raise EmbeddingConfigurationError("embedding timeout must be finite and positive")
        self._api_key = api_key.strip()
        self._endpoint = f"{normalized_base_url.rstrip('/')}/embeddings"
        self.model = model.strip()
        self.dimensions = dimensions
        self.timeout_seconds = timeout
        self.transport = transport or UrllibEmbeddingTransport()

    def embed(self, texts: Sequence[str]) -> EmbeddingBatch:
        values = _validate_texts(texts)
        payload: dict[str, object] = {"model": self.model, "input": list(values), "encoding_format": "float"}
        if self.dimensions is not None:
            payload["dimensions"] = self.dimensions
        try:
            response = self.transport.post(
                self._endpoint,
                {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json", "Accept": "application/json"},
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                self.timeout_seconds,
            )
        except EmbeddingError:
            raise
        except TimeoutError as exc:
            raise EmbeddingTimeout("embedding request timed out") from exc
        except OSError as exc:
            raise EmbeddingConnectionError("could not connect to the embedding provider") from exc
        except Exception:
            raise EmbeddingConnectionError("embedding transport request failed") from None
        _raise_for_status(response.status_code)
        return _parse_response(response.body, len(values), self.dimensions)


OpenAICompatibleEmbeddingsAdapter = OpenAICompatibleEmbeddingProvider


def _validate_texts(texts: Sequence[str]) -> tuple[str, ...]:
    if isinstance(texts, str) or not isinstance(texts, Sequence) or not 1 <= len(texts) <= 2_048:
        raise EmbeddingInputError("embedding input must contain from 1 to 2048 texts")
    if any(not isinstance(text, str) or not text.strip() or len(text) > 100_000 for text in texts):
        raise EmbeddingInputError("embedding texts must be non-empty strings of at most 100000 characters")
    return tuple(texts)


def _raise_for_status(status_code: object) -> None:
    if isinstance(status_code, bool) or not isinstance(status_code, int) or not 100 <= status_code <= 599:
        raise EmbeddingProviderResponseError("embedding transport returned an invalid HTTP status")
    if 200 <= status_code < 300:
        return
    if status_code in {401, 403}:
        raise EmbeddingAuthenticationError("embedding authentication was rejected")
    if status_code == 429:
        raise EmbeddingRateLimitError("embedding provider rate limit was reached")
    raise EmbeddingHTTPError(status_code)


def _parse_response(body: bytes, count: int, expected_dimension: int | None) -> EmbeddingBatch:
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EmbeddingProviderResponseError("embedding provider returned malformed JSON") from exc
    data = document.get("data") if isinstance(document, dict) else None
    if not isinstance(data, list) or len(data) != count:
        raise EmbeddingProviderResponseError("embedding provider response count does not match input")
    indexed: list[tuple[int, tuple[float, ...]]] = []
    try:
        for item in data:
            if not isinstance(item, dict) or isinstance(item.get("index"), bool) or not isinstance(item.get("index"), int):
                raise ValueError("invalid embedding index")
            raw_vector = item.get("embedding")
            if not isinstance(raw_vector, list) or not raw_vector:
                raise ValueError("invalid embedding vector")
            vector = tuple(float(value) for value in raw_vector)
            if any(isinstance(value, bool) for value in raw_vector) or not all(math.isfinite(value) for value in vector):
                raise ValueError("invalid embedding values")
            indexed.append((item["index"], vector))
    except (TypeError, ValueError) as exc:
        raise EmbeddingProviderResponseError("embedding provider returned invalid vectors") from exc
    if {index for index, _ in indexed} != set(range(count)):
        raise EmbeddingProviderResponseError("embedding provider response indices do not match input")
    vectors = tuple(vector for _, vector in sorted(indexed))
    try:
        result = EmbeddingBatch(vectors)
    except ValueError as exc:
        raise EmbeddingProviderResponseError("embedding provider returned invalid vector dimensions") from exc
    if expected_dimension is not None and result.dimension != expected_dimension:
        raise EmbeddingProviderResponseError("embedding provider returned an unexpected vector dimension")
    return result


def _validate_base_url(base_url: object) -> str:
    if not isinstance(base_url, str) or not base_url or base_url != base_url.strip():
        raise EmbeddingConfigurationError("embedding base URL must not be empty")
    if any(character.isspace() or ord(character) < 32 for character in base_url):
        raise EmbeddingConfigurationError("embedding base URL is invalid")
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except ValueError as exc:
        raise EmbeddingConfigurationError("embedding base URL is invalid") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port is not None and not 1 <= port <= 65535
    ):
        raise EmbeddingConfigurationError("embedding base URL is invalid")
    if parsed.scheme.lower() == "http" and not _is_loopback_host(parsed.hostname):
        raise EmbeddingConfigurationError("HTTP embedding endpoints must use a localhost or loopback host")
    return base_url


def _is_loopback_host(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ip_address(hostname).is_loopback
    except ValueError:
        return False
