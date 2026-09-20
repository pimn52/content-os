"""Provider-neutral capability evidence and deterministic execution resolution.

Narrative and asset routing decide *what* a scene needs.  This module decides
which already-admitted provider/runtime configuration may execute that need,
using evidence scoped to one concrete machine.  It deliberately persists
nothing and makes no provider calls; database/UI integration belongs to later
work packages.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from math import isfinite
from typing import Mapping, TypeAlias

from app.domain.models import UsageCost


ParameterValue: TypeAlias = str | int | float | bool


class CapabilityType(StrEnum):
    VOICE = "voice"
    TALKING = "talking"
    ASR = "asr"
    VISION = "vision"
    RENDER = "render"


class ExecutionMode(StrEnum):
    LOCAL = "local"
    REMOTE = "remote"


class CapabilityReadiness(StrEnum):
    IMPLEMENTED = "implemented"
    CONFIGURED = "configured"
    AVAILABLE = "available"
    VERIFIED = "verified"


class EvidenceStatus(StrEnum):
    VERIFIED = "verified"
    OBSERVED = "observed"
    UNKNOWN = "unknown"


class CommercialStatus(StrEnum):
    COMMERCIAL_SAFE = "commercial_safe"
    NON_COMMERCIAL_ONLY = "non_commercial_only"
    UNKNOWN = "unknown"


class CapabilityFeature(StrEnum):
    """Provider-neutral optional behavior a capability may expose."""

    TERMINAL_FACE_CLOSEOUT = "terminal_face_closeout"


class FeatureSupport(StrEnum):
    """Evidence-aware support state; availability is not local verification."""

    VERIFIED = "verified"
    AVAILABLE = "available"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class FeatureImplementationOwner(StrEnum):
    """Who supplies a safe implementation for a provider-neutral feature.

    ``CONTENT_OS_ADAPTER`` is deliberately distinct from an upstream vendor
    claim. It lets the product correct an observed output failure even where
    the provider has never named the failure or exposed a corresponding knob.
    """

    CONTENT_OS_ADAPTER = "content_os_adapter"
    PROVIDER_NATIVE = "provider_native"


class ResolutionSource(StrEnum):
    JOB_OVERRIDE = "job_override"
    USER_OVERRIDE = "user_override"
    LOCAL_VERIFIED = "local_verified"
    PROVIDER_DEFAULT = "provider_default"
    UNKNOWN = "unknown"


class ExecutionRoutingError(ValueError):
    """A malformed profile/override cannot be used for deterministic routing."""


class ExecutionSafetyError(ExecutionRoutingError):
    """A route or override attempted to cross a non-bypassable safety gate."""


def _required(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExecutionRoutingError(f"{field_name} must be a non-empty string")
    return value.strip()


def _parameter_values(values: Mapping[str, ParameterValue], field_name: str) -> dict[str, ParameterValue]:
    if not isinstance(values, Mapping):
        raise ExecutionRoutingError(f"{field_name} must be a mapping")
    normalized: dict[str, ParameterValue] = {}
    for key, value in values.items():
        key = _required(key, f"{field_name} key")
        if isinstance(value, float) and not isfinite(value):
            raise ExecutionRoutingError(f"{field_name}.{key} must be finite")
        if isinstance(value, bool) or isinstance(value, (str, int, float)):
            normalized[key] = value
        else:
            raise ExecutionRoutingError(f"{field_name}.{key} must be a JSON scalar")
    return normalized


@dataclass(frozen=True)
class ExecutionProfileKey:
    """Identity of one provider/model/runtime configuration on one machine."""

    capability: CapabilityType
    mode: ExecutionMode
    provider: str
    model: str
    runtime: str
    machine_id: str

    def __post_init__(self) -> None:
        for field_name in ("provider", "model", "runtime", "machine_id"):
            object.__setattr__(self, field_name, _required(getattr(self, field_name), field_name))


@dataclass(frozen=True)
class OperatingBound:
    """One observed operating boundary; ``None`` deliberately means unknown."""

    metric: str
    unit: str
    observed_pass_at: float | None = None
    observed_fail_at: float | None = None
    status: EvidenceStatus = EvidenceStatus.UNKNOWN

    def __post_init__(self) -> None:
        object.__setattr__(self, "metric", _required(self.metric, "metric"))
        object.__setattr__(self, "unit", _required(self.unit, "unit"))
        for name in ("observed_pass_at", "observed_fail_at"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(float(value))):
                raise ExecutionRoutingError(f"{name} must be a finite number or None")
        if self.status is EvidenceStatus.VERIFIED and self.observed_pass_at is None and self.observed_fail_at is None:
            raise ExecutionRoutingError("verified operating evidence needs an observed bound")


@dataclass(frozen=True)
class EvidenceProvenance:
    source: str
    evidence_reference: str | None = None
    last_verified_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _required(self.source, "provenance source"))
        if self.evidence_reference is not None:
            object.__setattr__(self, "evidence_reference", _required(self.evidence_reference, "evidence_reference"))


@dataclass(frozen=True)
class CapabilityProfile:
    """Evidence and known configuration values for exactly one execution key."""

    key: ExecutionProfileKey
    readiness: CapabilityReadiness
    verified_parameters: Mapping[str, ParameterValue] = field(default_factory=dict)
    feature_support: Mapping[CapabilityFeature, FeatureSupport] = field(default_factory=dict)
    operating_bounds: tuple[OperatingBound, ...] = ()
    quality_status: EvidenceStatus = EvidenceStatus.UNKNOWN
    continuity_status: EvidenceStatus = EvidenceStatus.UNKNOWN
    latency_ms: int | None = None
    resource_notes: Mapping[str, ParameterValue] = field(default_factory=dict)
    estimated_cost: UsageCost | None = None
    commercial_status: CommercialStatus = CommercialStatus.UNKNOWN
    provenance: EvidenceProvenance | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.key, ExecutionProfileKey):
            raise ExecutionRoutingError("key must be an ExecutionProfileKey")
        object.__setattr__(self, "verified_parameters", _parameter_values(self.verified_parameters, "verified_parameters"))
        normalized_features: dict[CapabilityFeature, FeatureSupport] = {}
        for feature, support in self.feature_support.items():
            if not isinstance(feature, CapabilityFeature) or not isinstance(support, FeatureSupport):
                raise ExecutionRoutingError("feature_support must map CapabilityFeature to FeatureSupport")
            normalized_features[feature] = support
        object.__setattr__(self, "feature_support", normalized_features)
        object.__setattr__(self, "resource_notes", _parameter_values(self.resource_notes, "resource_notes"))
        if any(not isinstance(bound, OperatingBound) for bound in self.operating_bounds):
            raise ExecutionRoutingError("operating_bounds must contain OperatingBound values")
        if self.latency_ms is not None and (isinstance(self.latency_ms, bool) or not isinstance(self.latency_ms, int) or self.latency_ms < 0):
            raise ExecutionRoutingError("latency_ms must be a non-negative integer or None")
        if self.estimated_cost is not None and not isinstance(self.estimated_cost, UsageCost):
            raise ExecutionRoutingError("estimated_cost must be a UsageCost or None")
        if self.readiness is not CapabilityReadiness.VERIFIED and self.verified_parameters:
            raise ExecutionRoutingError("verified_parameters require a verified capability profile")
        if self.readiness is not CapabilityReadiness.VERIFIED and FeatureSupport.VERIFIED in normalized_features.values():
            raise ExecutionRoutingError("verified feature support requires a verified capability profile")


@dataclass(frozen=True)
class ExecutionOverride:
    """A reversible set of values scoped to one exact execution profile."""

    profile_key: ExecutionProfileKey
    values: Mapping[str, ParameterValue]
    scope: ResolutionSource
    reference: str

    def __post_init__(self) -> None:
        if self.scope not in {ResolutionSource.JOB_OVERRIDE, ResolutionSource.USER_OVERRIDE}:
            raise ExecutionRoutingError("override scope must be job_override or user_override")
        if not isinstance(self.profile_key, ExecutionProfileKey):
            raise ExecutionRoutingError("override profile_key must be an ExecutionProfileKey")
        object.__setattr__(self, "values", _parameter_values(self.values, "override values"))
        if not self.values:
            raise ExecutionRoutingError("override values must not be empty")
        object.__setattr__(self, "reference", _required(self.reference, "override reference"))

    def without(self, parameter: str) -> "ExecutionOverride | None":
        """Return a reset copy, or ``None`` when the final override is removed."""

        parameter = _required(parameter, "parameter")
        values = {key: value for key, value in self.values.items() if key != parameter}
        if not values:
            return None
        return ExecutionOverride(self.profile_key, values, self.scope, self.reference)


@dataclass(frozen=True)
class ExecutionSafetyContext:
    """Non-bypassable gates owned by existing consent/budget/runtime boundaries."""

    consent_authorized: bool
    budget_authorized: bool
    runtime_integrity_verified: bool
    requires_provenance: bool = True
    commercial_use: bool = False


@dataclass(frozen=True)
class ResolvedParameter:
    key: str
    value: ParameterValue | None
    source: ResolutionSource
    reason: str


@dataclass(frozen=True)
class ExecutionResolution:
    profile_key: ExecutionProfileKey
    parameters: tuple[ResolvedParameter, ...]
    routing_reason: str

    def parameter(self, key: str) -> ResolvedParameter:
        for parameter in self.parameters:
            if parameter.key == key:
                return parameter
        raise KeyError(key)


@dataclass(frozen=True)
class ResolvedFeature:
    feature: CapabilityFeature
    support: FeatureSupport
    reason: str


def resolve_feature_support(
    profile: CapabilityProfile,
    feature: CapabilityFeature,
    *,
    adapter_declared_support: FeatureSupport = FeatureSupport.UNKNOWN,
) -> ResolvedFeature:
    """Resolve adapter implementation vs local evidence without overclaiming."""

    if not isinstance(profile, CapabilityProfile):
        raise ExecutionRoutingError("profile must be a CapabilityProfile")
    if not isinstance(feature, CapabilityFeature) or not isinstance(adapter_declared_support, FeatureSupport):
        raise ExecutionRoutingError("feature support resolution requires known feature/support values")
    local = profile.feature_support.get(feature)
    if local is not None:
        return ResolvedFeature(feature, local, "provider+machine capability profile evidence")
    if adapter_declared_support is FeatureSupport.UNSUPPORTED:
        return ResolvedFeature(feature, FeatureSupport.UNSUPPORTED, "selected adapter has no safe implementation for this product protection")
    if adapter_declared_support is FeatureSupport.AVAILABLE:
        return ResolvedFeature(feature, FeatureSupport.AVAILABLE, "Content OS adapter provides the protection; local quality verification is unknown")
    return ResolvedFeature(feature, FeatureSupport.UNKNOWN, "no Content OS adapter implementation or local capability evidence")


def resolve_execution(
    profile: CapabilityProfile,
    *,
    parameter_keys: tuple[str, ...] | list[str],
    per_job_override: ExecutionOverride | None = None,
    saved_override: ExecutionOverride | None = None,
    provider_defaults: Mapping[str, ParameterValue] | None = None,
    safety: ExecutionSafetyContext,
) -> ExecutionResolution:
    """Resolve values in fixed precedence while retaining value provenance."""

    if not isinstance(profile, CapabilityProfile):
        raise ExecutionRoutingError("profile must be a CapabilityProfile")
    if not isinstance(safety, ExecutionSafetyContext):
        raise ExecutionRoutingError("safety must be an ExecutionSafetyContext")
    keys = tuple(_required(key, "parameter key") for key in parameter_keys)
    if not keys or len(set(keys)) != len(keys):
        raise ExecutionRoutingError("parameter_keys must be non-empty and unique")
    defaults = _parameter_values(provider_defaults or {}, "provider_defaults")
    _ensure_safe(profile, safety)
    for override in (per_job_override, saved_override):
        if override is not None and override.profile_key != profile.key:
            raise ExecutionRoutingError("override profile_key does not match the resolved profile")

    resolved: list[ResolvedParameter] = []
    for key in keys:
        if per_job_override is not None and key in per_job_override.values:
            resolved.append(ResolvedParameter(key, per_job_override.values[key], ResolutionSource.JOB_OVERRIDE, "per-job explicit override"))
        elif saved_override is not None and key in saved_override.values:
            resolved.append(ResolvedParameter(key, saved_override.values[key], ResolutionSource.USER_OVERRIDE, "saved provider+machine override"))
        elif profile.readiness is CapabilityReadiness.VERIFIED and key in profile.verified_parameters:
            resolved.append(ResolvedParameter(key, profile.verified_parameters[key], ResolutionSource.LOCAL_VERIFIED, "locally verified provider+machine evidence"))
        elif key in defaults:
            resolved.append(ResolvedParameter(key, defaults[key], ResolutionSource.PROVIDER_DEFAULT, "provider-known conservative default"))
        else:
            resolved.append(ResolvedParameter(key, None, ResolutionSource.UNKNOWN, "no override, local evidence, or provider default"))
    return ExecutionResolution(profile.key, tuple(resolved), "deterministic precedence: job > saved > local verified > provider default > unknown")


def _ensure_safe(profile: CapabilityProfile, safety: ExecutionSafetyContext) -> None:
    if not safety.consent_authorized:
        raise ExecutionSafetyError("execution routing requires confirmed consent/identity authorization")
    if not safety.budget_authorized:
        raise ExecutionSafetyError("execution routing requires the existing budget/paid-call gate")
    if not safety.runtime_integrity_verified:
        raise ExecutionSafetyError("execution routing requires runtime integrity checks")
    if safety.requires_provenance and (profile.provenance is None or not profile.provenance.evidence_reference):
        raise ExecutionSafetyError("execution routing requires provider/machine provenance")
    if safety.commercial_use and profile.commercial_status is not CommercialStatus.COMMERCIAL_SAFE:
        raise ExecutionSafetyError("commercial execution requires commercial-safe provider/model evidence")
