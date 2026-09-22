"""Provider-neutral voice synthesis contracts and an optional OmniVoice adapter.

This module deliberately contains no model download or credential discovery.
The OmniVoice adapter only imports its optional runtime when a claimed job is
executed, and its official pretrained weights remain non-commercial benchmark
material under the R1 execution specification.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
import re
from typing import Callable, Protocol

from app.domain.models import NarrationPerformancePlan, VoiceProfile
from app.routing.execution import FeatureSupport


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


class NarrationPerformanceCoverage(StrEnum):
    """How completely one adapter can apply a saved delivery plan."""

    FULL = "full"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"


_SAFE_PREFLIGHT_REASON = re.compile(r"[a-z][a-z0-9_]{0,79}")


@dataclass(frozen=True)
class NarrationPerformancePreflight:
    """Non-billable applicability result for an optional Voice extension.

    Reasons deliberately are small local reason codes rather than adapter text.
    They are safe to retain in future diagnostics without exposing provider
    markup, prompts, credentials, or response bodies.
    """

    coverage: NarrationPerformanceCoverage
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        coverage = self.coverage
        if not isinstance(coverage, NarrationPerformanceCoverage):
            try:
                coverage = NarrationPerformanceCoverage(coverage)
            except (TypeError, ValueError) as exc:
                raise ValueError("narration performance preflight coverage is invalid") from exc
            object.__setattr__(self, "coverage", coverage)
        reasons = tuple(self.reasons)
        if any(not isinstance(reason, str) or not _SAFE_PREFLIGHT_REASON.fullmatch(reason) for reason in reasons):
            raise ValueError("narration performance preflight reasons must be safe reason codes")
        if coverage is NarrationPerformanceCoverage.FULL and reasons:
            raise ValueError("a full narration performance preflight must not include limitation reasons")
        if coverage is not NarrationPerformanceCoverage.FULL and not reasons:
            raise ValueError("a non-full narration performance preflight requires a reason code")
        object.__setattr__(self, "reasons", reasons)


@dataclass(frozen=True)
class VoiceSynthesisResult:
    """One provider-produced local audio file, never a provider URL or secret."""

    audio_path: Path
    provider_version: str | None = None
    performance_intent_applied: bool = False


class VoiceProvider(Protocol):
    provider_name: str
    model: str

    def synthesize(self, profile: VoiceProfile, text: str, output_path: str | Path, *, language: str | None = None) -> VoiceSynthesisResult: ...


class PerformanceAwareVoiceProvider(VoiceProvider, Protocol):
    """Optional extension for an adapter that can apply delivery semantics.

    An adapter must expose support explicitly and return a positive application
    receipt. The base VoiceProvider remains compatible with ordinary voice
    generation and must never receive a performance plan by accident.
    """

    performance_intent_support: FeatureSupport

    def synthesize_with_performance(
        self,
        profile: VoiceProfile,
        text: str,
        output_path: str | Path,
        *,
        language: str | None = None,
        performance_plan: NarrationPerformancePlan,
    ) -> VoiceSynthesisResult: ...


class NarrationPerformancePreflightVoiceProvider(PerformanceAwareVoiceProvider, Protocol):
    """Optional adapter extension that checks whole-plan applicability.

    ``PARTIAL`` intentionally does not authorize a subset run: the current
    Voice job contract requests the complete plan, and a future subset mode
    must be an explicit product/API contract of its own.
    """

    def preflight_narration_performance(
        self,
        profile: VoiceProfile,
        text: str,
        *,
        language: str | None = None,
        performance_plan: NarrationPerformancePlan,
    ) -> NarrationPerformancePreflight: ...


class OmniVoiceProvider:
    """Optional local non-commercial benchmark adapter.

    The concrete callable is injected so upstream API changes do not leak into
    Content OS contracts. The default intentionally explains that no optional
    runtime was installed instead of attempting a download.
    """

    provider_name = "omnivoice"
    noncommercial_benchmark_only = True
    is_local = True
    # OmniVoice's current adapter accepts only its provider-owned numeric
    # settings. It has no safe mapping from Content OS semantic delivery
    # cues, so a queued plan must fail explicitly instead of being ignored.
    performance_intent_support = FeatureSupport.UNKNOWN

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
