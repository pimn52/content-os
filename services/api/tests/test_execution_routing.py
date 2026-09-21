from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.routing import (
    CapabilityProfile,
    CapabilityFeature,
    CapabilityReadiness,
    CapabilityType,
    CommercialStatus,
    EvidenceProvenance,
    EvidenceStatus,
    ExecutionMode,
    ExecutionOverride,
    ExecutionProfileKey,
    ExecutionSafetyContext,
    ExecutionSafetyError,
    FeatureSupport,
    OperatingBound,
    ResolutionSource,
    resolve_execution,
    resolve_feature_support,
    resolve_verified_operating_limit,
)


def _key(*, machine_id: str = "rtx3060-6gb") -> ExecutionProfileKey:
    return ExecutionProfileKey(
        capability=CapabilityType.TALKING,
        mode=ExecutionMode.LOCAL,
        provider="latentsync",
        model="LatentSync-1.5",
        runtime="compat-20step-guidance-1.5",
        machine_id=machine_id,
    )


def _profile(*, machine_id: str = "rtx3060-6gb", commercial: CommercialStatus = CommercialStatus.NON_COMMERCIAL_ONLY) -> CapabilityProfile:
    return CapabilityProfile(
        key=_key(machine_id=machine_id),
        readiness=CapabilityReadiness.VERIFIED,
        verified_parameters={"inference_steps": 20, "guidance_scale": 1.5},
        operating_bounds=(
            OperatingBound(
                metric="fresh_voice_talking_duration",
                unit="seconds",
                observed_pass_at=2.58,
                observed_fail_at=5.12,
                status=EvidenceStatus.VERIFIED,
            ),
        ),
        quality_status=EvidenceStatus.VERIFIED,
        continuity_status=EvidenceStatus.UNKNOWN,
        latency_ms=600_000,
        resource_notes={"gpu_vram_mib": 6144},
        commercial_status=commercial,
        provenance=EvidenceProvenance(
            source="local runtime evaluation",
            evidence_reference="latentsync-duration-boundary-20260915",
            last_verified_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
        ),
    )


def _safety(**changes: bool) -> ExecutionSafetyContext:
    values = {
        "consent_authorized": True,
        "budget_authorized": True,
        "runtime_integrity_verified": True,
        "requires_provenance": True,
        "commercial_use": False,
    }
    values.update(changes)
    return ExecutionSafetyContext(**values)


def test_execution_resolution_has_all_five_deterministic_precedence_levels() -> None:
    profile = _profile()
    job = ExecutionOverride(profile.key, {"job_value": 1}, ResolutionSource.JOB_OVERRIDE, "job-123")
    saved = ExecutionOverride(profile.key, {"saved_value": 2}, ResolutionSource.USER_OVERRIDE, "user-machine-setting")

    result = resolve_execution(
        profile,
        parameter_keys=["job_value", "saved_value", "inference_steps", "provider_default", "unknown_value"],
        per_job_override=job,
        saved_override=saved,
        provider_defaults={"provider_default": 4},
        safety=_safety(),
    )

    assert [(item.key, item.value, item.source) for item in result.parameters] == [
        ("job_value", 1, ResolutionSource.JOB_OVERRIDE),
        ("saved_value", 2, ResolutionSource.USER_OVERRIDE),
        ("inference_steps", 20, ResolutionSource.LOCAL_VERIFIED),
        ("provider_default", 4, ResolutionSource.PROVIDER_DEFAULT),
        ("unknown_value", None, ResolutionSource.UNKNOWN),
    ]
    assert result.parameter("unknown_value").reason == "no override, local evidence, or provider default"
    assert "job > saved > local verified > provider default > unknown" in result.routing_reason


def test_execution_resolution_keeps_machine_profiles_and_overrides_narrow_and_reversible() -> None:
    local = _profile(machine_id="rtx3060-6gb")
    other_machine = CapabilityProfile(
        key=_key(machine_id="other-machine"),
        readiness=CapabilityReadiness.VERIFIED,
        verified_parameters={"inference_steps": 12},
        provenance=EvidenceProvenance("other local evaluation", "other-machine-evidence"),
    )
    saved = ExecutionOverride(local.key, {"inference_steps": 16, "guidance_scale": 1.2}, ResolutionSource.USER_OVERRIDE, "creator-local-setting")

    local_result = resolve_execution(local, parameter_keys=["inference_steps", "guidance_scale"], saved_override=saved, safety=_safety())
    assert [item.value for item in local_result.parameters] == [16, 1.2]
    assert saved.without("guidance_scale") is not None
    reset = saved.without("guidance_scale")
    assert reset is not None and reset.values == {"inference_steps": 16}
    assert reset.without("inference_steps") is None

    other_result = resolve_execution(other_machine, parameter_keys=["inference_steps"], safety=_safety())
    assert other_result.parameter("inference_steps").value == 12
    assert other_result.parameter("inference_steps").source is ResolutionSource.LOCAL_VERIFIED
    with pytest.raises(ValueError, match="does not match"):
        resolve_execution(other_machine, parameter_keys=["inference_steps"], saved_override=saved, safety=_safety())


@pytest.mark.parametrize(
    ("safety", "message"),
    [
        (_safety(consent_authorized=False), "consent"),
        (_safety(budget_authorized=False), "budget"),
        (_safety(runtime_integrity_verified=False), "integrity"),
        (_safety(commercial_use=True), "commercial-safe"),
    ],
)
def test_execution_safety_gates_reject_overrides(safety: ExecutionSafetyContext, message: str) -> None:
    profile = _profile()
    override = ExecutionOverride(profile.key, {"inference_steps": 99}, ResolutionSource.JOB_OVERRIDE, "job-unsafe")

    with pytest.raises(ExecutionSafetyError, match=message):
        resolve_execution(profile, parameter_keys=["inference_steps"], per_job_override=override, safety=safety)


def test_unverified_profile_does_not_promote_configured_value_above_provider_default() -> None:
    profile = CapabilityProfile(
        key=_key(),
        readiness=CapabilityReadiness.CONFIGURED,
        provenance=EvidenceProvenance("environment configuration", "runtime-readiness"),
    )
    result = resolve_execution(profile, parameter_keys=["inference_steps", "missing"], provider_defaults={"inference_steps": 20}, safety=_safety())

    assert result.parameter("inference_steps").source is ResolutionSource.PROVIDER_DEFAULT
    assert result.parameter("missing").source is ResolutionSource.UNKNOWN


def test_terminal_face_closeout_is_content_os_adapter_protection_with_machine_scoped_quality_evidence() -> None:
    verified = _profile()
    verified = CapabilityProfile(
        key=verified.key,
        readiness=verified.readiness,
        verified_parameters=verified.verified_parameters,
        feature_support={CapabilityFeature.TERMINAL_FACE_CLOSEOUT: FeatureSupport.VERIFIED},
        provenance=verified.provenance,
    )
    other_machine = CapabilityProfile(
        key=_key(machine_id="other-machine"), readiness=CapabilityReadiness.CONFIGURED,
        provenance=EvidenceProvenance("other machine", "runtime-readiness"),
    )

    assert resolve_feature_support(
        verified, CapabilityFeature.TERMINAL_FACE_CLOSEOUT, adapter_declared_support=FeatureSupport.AVAILABLE,
    ).support is FeatureSupport.VERIFIED
    available = resolve_feature_support(
        other_machine, CapabilityFeature.TERMINAL_FACE_CLOSEOUT, adapter_declared_support=FeatureSupport.AVAILABLE,
    )
    assert available.support is FeatureSupport.AVAILABLE
    assert "Content OS adapter provides the protection" in available.reason
    assert resolve_feature_support(
        other_machine, CapabilityFeature.TERMINAL_FACE_CLOSEOUT,
    ).support is FeatureSupport.UNKNOWN


def test_verified_operating_limit_is_scoped_and_never_inferred() -> None:
    verified = _profile()
    limit = resolve_verified_operating_limit(
        verified, metric="fresh_voice_talking_duration", unit="seconds",
    )
    assert limit.value == 2.58 and limit.source is ResolutionSource.LOCAL_VERIFIED

    unknown = CapabilityProfile(
        key=_key(machine_id="other-machine"), readiness=CapabilityReadiness.CONFIGURED,
        provenance=EvidenceProvenance("other machine", "runtime-readiness"),
    )
    result = resolve_verified_operating_limit(
        unknown, metric="fresh_voice_talking_duration", unit="seconds",
    )
    assert result.value is None and result.source is ResolutionSource.UNKNOWN
