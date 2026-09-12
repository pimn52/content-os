"""Provider-neutral voice synthesis contracts and an optional OmniVoice adapter.

This module deliberately contains no model download or credential discovery.
The OmniVoice adapter only imports its optional runtime when a claimed job is
executed, and its official pretrained weights remain non-commercial benchmark
material under the R1 execution specification.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from app.domain.models import VoiceProfile


class VoiceError(RuntimeError):
    """Base class for credential-free expected voice-provider errors."""


class VoiceConfigurationError(VoiceError):
    pass


class VoiceInputError(VoiceError):
    pass


class VoiceTimeout(VoiceError):
    pass


class VoiceConnectionError(VoiceError):
    pass


class VoiceAuthenticationError(VoiceError):
    pass


class VoiceRateLimitError(VoiceError):
    pass


class VoiceProviderResponseError(VoiceError):
    pass


@dataclass(frozen=True)
class VoiceSynthesisResult:
    """One provider-produced local audio file, never a provider URL or secret."""

    audio_path: Path
    provider_version: str | None = None


class VoiceProvider(Protocol):
    provider_name: str
    model: str

    def synthesize(self, profile: VoiceProfile, text: str, output_path: str | Path, *, language: str | None = None) -> VoiceSynthesisResult: ...


class OmniVoiceProvider:
    """Optional local non-commercial benchmark adapter.

    The concrete callable is injected so upstream API changes do not leak into
    Content OS contracts. The default intentionally explains that no optional
    runtime was installed instead of attempting a download.
    """

    provider_name = "omnivoice"
    noncommercial_benchmark_only = True
    is_local = True

    def __init__(
        self,
        model: str = "official-pretrained",
        *,
        synthesizer: Callable[[VoiceProfile, str, Path, str | None], object] | None = None,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise VoiceConfigurationError("OmniVoice model must not be empty")
        self.model = model.strip()
        self._synthesizer = synthesizer

    def synthesize(self, profile: VoiceProfile, text: str, output_path: str | Path, *, language: str | None = None) -> VoiceSynthesisResult:
        if not isinstance(profile, VoiceProfile) or not profile.consent.confirmed:
            raise VoiceInputError("voice synthesis requires an explicitly consented VoiceProfile")
        if not isinstance(text, str) or not text.strip():
            raise VoiceInputError("voice synthesis text must not be empty")
        target = Path(output_path)
        if not target.suffix:
            raise VoiceInputError("voice output path must include an audio extension")
        if language is not None and (not isinstance(language, str) or not language.strip()):
            raise VoiceInputError("voice synthesis language must be non-empty when supplied")
        if self._synthesizer is None:
            raise VoiceConfigurationError(
                "OmniVoice is an optional non-commercial benchmark; install and configure its runtime before an explicit evaluation job"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            produced = self._synthesizer(profile, text.strip(), target, language.strip() if language else None)
        except VoiceError:
            raise
        except TimeoutError as exc:
            raise VoiceTimeout("local OmniVoice inference timed out") from exc
        except OSError as exc:
            raise VoiceConnectionError("local OmniVoice inference could not access its runtime") from exc
        except Exception:
            raise VoiceProviderResponseError("local OmniVoice inference failed") from None
        path = Path(produced) if isinstance(produced, (str, Path)) else target
        if not path.is_file() or path.stat().st_size == 0:
            raise VoiceProviderResponseError("voice provider did not produce a playable local audio file")
        return VoiceSynthesisResult(path)
