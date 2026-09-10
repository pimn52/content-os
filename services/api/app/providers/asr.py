"""Provider-neutral ASR contracts and an OpenAI-compatible BYOK adapter."""
from __future__ import annotations

import json
import mimetypes
import socket
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from ipaddress import ip_address
from pathlib import Path
from typing import Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4


_SUPPORTED_AUDIO_EXTENSIONS = frozenset({"flac", "mp3", "mp4", "mpeg", "mpga", "m4a", "ogg", "wav", "webm"})


class ASRError(RuntimeError):
    """Base class for expected transcription errors safe to show to callers."""


class ASRConfigurationError(ASRError):
    pass


class ASRInputError(ASRError):
    pass


class ASRTimeout(ASRError):
    pass


class ASRConnectionError(ASRError):
    pass


class ASRAuthenticationError(ASRError):
    pass


class ASRRateLimitError(ASRError):
    pass


class ASRHTTPError(ASRError):
    """A non-special-cased HTTP response, with a safe numeric status."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"ASR provider returned HTTP {status_code}")


class ASRProviderResponseError(ASRError):
    pass


@dataclass(frozen=True)
class TranscriptionSegment:
    """A source-compatible spoken interval in integer milliseconds."""

    start_ms: int
    end_ms: int
    text: str

    def __post_init__(self) -> None:
        if isinstance(self.start_ms, bool) or not isinstance(self.start_ms, int) or self.start_ms < 0:
            raise ValueError("segment start_ms must be a non-negative integer")
        if isinstance(self.end_ms, bool) or not isinstance(self.end_ms, int) or self.end_ms <= self.start_ms:
            raise ValueError("segment end_ms must be greater than start_ms")
        if not isinstance(self.text, str):
            raise ValueError("segment text must be a string")


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    segments: tuple[TranscriptionSegment, ...]
    language: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise ValueError("transcription text must be a string")
        if not isinstance(self.segments, tuple):
            raise ValueError("transcription segments must be immutable")
        if self.language is not None and not isinstance(self.language, str):
            raise ValueError("transcription language must be a string or None")


class ASRProvider(Protocol):
    def transcribe(
        self,
        audio_path: str | Path,
        *,
        language: str | None = None,
        prompt: str | None = None,
    ) -> TranscriptionResult: ...


@dataclass(frozen=True)
class HTTPResponse:
    status_code: int
    body: bytes
    headers: Mapping[str, str]


class HTTPTransport(Protocol):
    def post(self, url: str, headers: Mapping[str, str], body: bytes, timeout_seconds: float) -> HTTPResponse: ...


class _NoRedirectHandler(HTTPRedirectHandler):
    """Return 3xx responses to the adapter instead of replaying BYOK headers."""

    def redirect_request(
        self,
        request: Request,
        fp: object,
        code: int,
        message: str,
        headers: object,
        new_url: str,
    ) -> None:
        return None


class UrllibTransport:
    """Small stdlib transport so the adapter has no extra runtime dependency."""

    def post(self, url: str, headers: Mapping[str, str], body: bytes, timeout_seconds: float) -> HTTPResponse:
        request = Request(url, data=body, headers=dict(headers), method="POST")
        try:
            opener = build_opener(_NoRedirectHandler())
            with opener.open(request, timeout=timeout_seconds) as response:  # noqa: S310 - caller configures the BYOK endpoint.
                return HTTPResponse(response.status, response.read(), dict(response.headers.items()))
        except HTTPError as exc:
            return HTTPResponse(exc.code, exc.read(), dict(exc.headers.items()) if exc.headers else {})
        except socket.timeout as exc:
            raise ASRTimeout("ASR request timed out") from exc
        except URLError as exc:
            raise ASRConnectionError("could not connect to the ASR provider") from exc


class OpenAICompatibleASRProvider:
    """POST ``/v1/audio/transcriptions`` with runtime-injected BYOK credentials."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.openai.com/v1",
        model: str = "whisper-1",
        timeout_seconds: float = 60.0,
        transport: HTTPTransport | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ASRConfigurationError("ASR API key must be supplied at runtime")
        normalized_base_url = _validate_base_url(base_url)
        if not isinstance(model, str) or not model.strip():
            raise ASRConfigurationError("ASR model must not be empty")
        if isinstance(timeout_seconds, bool):
            raise ASRConfigurationError("ASR timeout must be finite and positive")
        try:
            timeout = float(timeout_seconds)
        except (TypeError, ValueError) as exc:
            raise ASRConfigurationError("ASR timeout must be finite and positive") from exc
        if timeout <= 0 or timeout == float("inf") or timeout != timeout:
            raise ASRConfigurationError("ASR timeout must be finite and positive")
        self._api_key = api_key.strip()
        self._endpoint = f"{normalized_base_url.rstrip('/')}/audio/transcriptions"
        self.model = model.strip()
        self.timeout_seconds = timeout
        self.transport = transport or UrllibTransport()

    def transcribe(
        self,
        audio_path: str | Path,
        *,
        language: str | None = None,
        prompt: str | None = None,
    ) -> TranscriptionResult:
        path = Path(audio_path)
        if not path.is_file():
            raise ASRInputError(f"audio input does not exist: {path}")
        if not path.suffix:
            raise ASRInputError("audio input filename must include an extension")
        if path.suffix[1:].lower() not in _SUPPORTED_AUDIO_EXTENSIONS:
            raise ASRInputError("audio input extension is not supported by the ASR provider")
        if language is not None and (not isinstance(language, str) or not language.strip()):
            raise ASRInputError("language must be a non-empty string when provided")
        if prompt is not None and (not isinstance(prompt, str) or not prompt.strip()):
            raise ASRInputError("prompt must be a non-empty string when provided")
        body, content_type = _multipart_body(path, self.model, language, prompt)
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": content_type,
            "Accept": "application/json",
        }
        try:
            response = self.transport.post(self._endpoint, headers, body, self.timeout_seconds)
        except ASRError:
            raise
        except TimeoutError as exc:
            raise ASRTimeout("ASR request timed out") from exc
        except OSError as exc:
            raise ASRConnectionError("could not connect to the ASR provider") from exc
        except Exception:
            # A caller can inject a transport for another provider-compatible
            # HTTP client. Never surface its potentially credential-bearing
            # diagnostic text through this public adapter boundary.
            raise ASRConnectionError("ASR transport request failed") from None
        _raise_for_status(response.status_code)
        return _parse_verbose_json(response.body)


OpenAICompatibleTranscriptionAdapter = OpenAICompatibleASRProvider


class FasterWhisperASRProvider:
    """Optional local CPU ASR provider backed by faster-whisper.

    The dependency is imported lazily so the default API installation remains
    provider-neutral. Model loading (and any model download performed by the
    library) only happens after an explicit transcription job starts.
    """

    def __init__(
        self,
        model: str = "small",
        *,
        device: str = "cpu",
        compute_type: str = "int8",
        download_root: str | Path | None = None,
        model_factory: Callable[..., object] | None = None,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ASRConfigurationError("local ASR model must not be empty")
        if not isinstance(device, str) or not device.strip():
            raise ASRConfigurationError("local ASR device must not be empty")
        if not isinstance(compute_type, str) or not compute_type.strip():
            raise ASRConfigurationError("local ASR compute type must not be empty")
        if download_root is not None and not isinstance(download_root, (str, Path)):
            raise ASRConfigurationError("local ASR model directory is invalid")
        self.model = model.strip()
        self.device = device.strip()
        self.compute_type = compute_type.strip()
        self.download_root = Path(download_root) if download_root is not None else None
        self._model_factory = model_factory
        self._model: object | None = None

    def transcribe(
        self,
        audio_path: str | Path,
        *,
        language: str | None = None,
        prompt: str | None = None,
    ) -> TranscriptionResult:
        path = _validate_audio_path(audio_path)
        language_value = _validate_optional_text(language, "language")
        prompt_value = _validate_optional_text(prompt, "prompt")
        model = self._loaded_model()
        try:
            raw_segments, info = model.transcribe(
                str(path),
                language=language_value,
                initial_prompt=prompt_value,
                vad_filter=True,
            )
            segments: list[TranscriptionSegment] = []
            for raw in raw_segments:
                text = getattr(raw, "text", None)
                start = getattr(raw, "start", None)
                end = getattr(raw, "end", None)
                if not isinstance(text, str) or not text.strip():
                    continue
                start_ms = _start_ms(start)
                end_ms = _end_ms(end)
                if end_ms <= start_ms:
                    continue
                segments.append(TranscriptionSegment(start_ms, end_ms, text.strip()))
        except ASRError:
            raise
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ASRProviderResponseError("local ASR returned invalid segment timestamps") from exc
        except Exception:
            raise ASRConnectionError("local ASR inference failed") from None

        detected_language = getattr(info, "language", None)
        if not isinstance(detected_language, str) or not detected_language.strip():
            detected_language = None
        return TranscriptionResult(
            " ".join(segment.text for segment in segments),
            tuple(segments),
            detected_language.strip() if detected_language else None,
        )

    def _loaded_model(self) -> object:
        if self._model is not None:
            return self._model
        factory = self._model_factory
        if factory is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise ASRConfigurationError(
                    "local ASR requires the optional faster-whisper dependency"
                ) from exc
            factory = WhisperModel
        kwargs: dict[str, object] = {
            "device": self.device,
            "compute_type": self.compute_type,
        }
        if self.download_root is not None:
            kwargs["download_root"] = str(self.download_root)
        try:
            self._model = factory(self.model, **kwargs)
        except ASRError:
            raise
        except Exception:
            raise ASRConfigurationError("local ASR model could not be loaded") from None
        return self._model


def _validate_audio_path(audio_path: str | Path) -> Path:
    path = Path(audio_path)
    if not path.is_file():
        raise ASRInputError(f"audio input does not exist: {path}")
    if not path.suffix:
        raise ASRInputError("audio input filename must include an extension")
    if path.suffix[1:].lower() not in _SUPPORTED_AUDIO_EXTENSIONS:
        raise ASRInputError("audio input extension is not supported by the ASR provider")
    return path


def _validate_optional_text(value: str | None, name: str) -> str | None:
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ASRInputError(f"{name} must be a non-empty string when provided")
    return value.strip() if value is not None else None


def _multipart_body(path: Path, model: str, language: str | None, prompt: str | None) -> tuple[bytes, str]:
    filename = path.name
    if not _is_safe_multipart_filename(filename):
        raise ASRInputError("audio input filename is invalid")
    mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    boundary = f"----content-os-{uuid4().hex}"
    chunks: list[bytes] = []

    def field(name: str, value: str) -> None:
        chunks.extend((
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            value.encode("utf-8"), b"\r\n",
        ))

    field("model", model)
    field("response_format", "verbose_json")
    field("timestamp_granularities[]", "segment")
    if language is not None:
        field("language", language.strip())
    if prompt is not None:
        field("prompt", prompt.strip())
    chunks.extend((
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
        f"Content-Type: {mime_type}\r\n\r\n".encode(),
        path.read_bytes(), b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _raise_for_status(status_code: object) -> None:
    if isinstance(status_code, bool) or not isinstance(status_code, int) or not 100 <= status_code <= 599:
        raise ASRProviderResponseError("ASR transport returned an invalid HTTP status")
    if 200 <= status_code < 300:
        return
    if status_code in (401, 403):
        raise ASRAuthenticationError("ASR authentication was rejected")
    if status_code == 429:
        raise ASRRateLimitError("ASR provider rate limit was reached")
    raise ASRHTTPError(status_code)


def _parse_verbose_json(body: bytes) -> TranscriptionResult:
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ASRProviderResponseError("ASR provider returned malformed JSON") from exc
    if not isinstance(document, dict) or not isinstance(document.get("text"), str):
        raise ASRProviderResponseError("ASR provider response has no transcription text")
    raw_segments = document.get("segments")
    if not isinstance(raw_segments, list):
        raise ASRProviderResponseError("ASR provider response has no segment timestamps")
    segments: list[TranscriptionSegment] = []
    for raw in raw_segments:
        if not isinstance(raw, dict) or not isinstance(raw.get("text"), str):
            raise ASRProviderResponseError("ASR provider returned an invalid segment")
        try:
            start_ms = _start_ms(raw["start"])
            end_ms = _end_ms(raw["end"])
            segments.append(TranscriptionSegment(start_ms, end_ms, raw["text"]))
        except (KeyError, InvalidOperation, ValueError) as exc:
            raise ASRProviderResponseError("ASR provider returned invalid segment timestamps") from exc
    language = document.get("language")
    if language is not None and not isinstance(language, str):
        raise ASRProviderResponseError("ASR provider returned an invalid language")
    return TranscriptionResult(document["text"], tuple(segments), language)


def _start_ms(value: object) -> int:
    seconds = _seconds(value)
    return int((seconds * Decimal(1000)).to_integral_value(rounding=ROUND_FLOOR))


def _end_ms(value: object) -> int:
    seconds = _seconds(value)
    return int((seconds * Decimal(1000)).to_integral_value(rounding=ROUND_CEILING))


def _seconds(value: object) -> Decimal:
    seconds = Decimal(str(value))
    if not seconds.is_finite() or seconds < 0:
        raise ValueError("segment timestamps must be finite and non-negative")
    return seconds


def _validate_base_url(base_url: object) -> str:
    if not isinstance(base_url, str) or not base_url or base_url != base_url.strip():
        raise ASRConfigurationError("ASR base URL must not be empty")
    if any(character.isspace() or ord(character) < 32 for character in base_url):
        raise ASRConfigurationError("ASR base URL is invalid")
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except ValueError as exc:
        raise ASRConfigurationError("ASR base URL is invalid") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port is not None and not 1 <= port <= 65535
    ):
        raise ASRConfigurationError("ASR base URL is invalid")
    if parsed.scheme.lower() == "http" and not _is_loopback_host(parsed.hostname):
        raise ASRConfigurationError("HTTP ASR endpoints must use a localhost or loopback host")
    return base_url


def _is_loopback_host(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ip_address(hostname).is_loopback
    except ValueError:
        return False


def _is_safe_multipart_filename(filename: str) -> bool:
    return bool(filename) and not any(
        character in {'"', "\\"} or ord(character) < 32 or 127 <= ord(character) <= 159
        for character in filename
    )
