"""Local execution inputs and model-use limits, independent of output QA."""


def local_generation_ready(capability) -> bool:
    return bool(capability is not None and capability.mode == "local"
        and capability.readiness in {"configured", "available", "verified"}
        and capability.evidence_reference)


def local_generation_licensed(capability) -> bool:
    return bool(capability is not None
        and capability.commercial_status in {"commercial_safe", "non_commercial_only"}
        and capability.license_evidence_reference)


def local_generation_usage(capability) -> dict:
    """Persist actual license facts; no capability or quality promotion."""
    return {"provider": capability.provider, "model": capability.model,
        "commercial_status": capability.commercial_status,
        "license_evidence_reference": capability.license_evidence_reference,
        "usage_restrictions": ["non_commercial_only"] if capability.commercial_status == "non_commercial_only" else []}
