"""Replaceable provider adapters kept outside the Content OS domain contracts."""

from .asr import (
    ASRAuthenticationError,
    ASRConfigurationError,
    ASRConnectionError,
    ASRError,
    ASRHTTPError,
    ASRInputError,
    ASRProvider,
    ASRProviderResponseError,
    ASRRateLimitError,
    ASRTimeout,
    OpenAICompatibleASRProvider,
    TranscriptionResult,
    TranscriptionSegment,
)

__all__ = [
    "ASRAuthenticationError",
    "ASRConfigurationError",
    "ASRConnectionError",
    "ASRError",
    "ASRHTTPError",
    "ASRInputError",
    "ASRProvider",
    "ASRProviderResponseError",
    "ASRRateLimitError",
    "ASRTimeout",
    "OpenAICompatibleASRProvider",
    "TranscriptionResult",
    "TranscriptionSegment",
]
