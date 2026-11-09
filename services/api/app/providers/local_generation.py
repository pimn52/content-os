"""Local execution inputs and model-use limits, independent of output QA."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from app.domain.models import ProviderMachineCapabilityProfile


def local_generation_usage(capability) -> dict:
    """Persist actual license facts; no capability or quality promotion."""
    return {"provider": capability.provider, "model": capability.model,
        "commercial_status": capability.commercial_status,
        "license_evidence_reference": capability.license_evidence_reference,
        "usage_restrictions": ["non_commercial_only"] if capability.commercial_status == "non_commercial_only" else []}


def provider_execution_identity(provider, capability: str) -> tuple[str | None, str | None]:
    details = getattr(provider, "repair_execution", None)
    details = details if isinstance(details, dict) else {}
    runtime = details.get("runtime") or getattr(provider, "runtime_label", None)
    machine_id = details.get("machine_id") or getattr(provider, "machine_id", None)
    environment_prefix = "CONTENT_OS_VOICE" if capability == "voice" else "CONTENT_OS_TALKING"
    runtime = runtime or os.environ.get(f"{environment_prefix}_RUNTIME_ID")
    machine_id = machine_id or os.environ.get(f"{environment_prefix}_MACHINE_ID")
    runtime = runtime.strip() if isinstance(runtime, str) and runtime.strip() else None
    machine_id = machine_id.strip() if isinstance(machine_id, str) and machine_id.strip() else None
    return runtime, machine_id


def ensure_runtime_capability_profile(repository, provider, capability: str):
    """Reuse or record the configured runtime identity without making it a gate.

    Capability/license declarations remain useful provenance, but a missing
    evidence mark must not stop a creator whose provider is already configured.
    Actual provider/model identity is checked at the Worker boundary.
    """
    name = getattr(provider, "provider_name", None)
    model = getattr(provider, "model", None)
    if (not isinstance(name, str) or not name.strip() or len(name.strip()) > 100
            or not isinstance(model, str) or not model.strip() or len(model.strip()) > 200):
        return None
    mode = "remote" if getattr(provider, "is_local", True) is False else "local"
    runtime, machine_id = provider_execution_identity(provider, capability)
    existing = [
        value for value in repository.list()
        if value.capability == capability and value.mode == mode
        and value.provider == name.strip() and value.model == model.strip()
    ]
    if runtime is not None:
        existing = [value for value in existing if value.runtime == runtime]
    if machine_id is not None:
        existing = [value for value in existing if value.machine_id == machine_id]
    if existing:
        return max(existing, key=lambda value: value.updated_at)
    runtime = runtime or "configured-runtime"
    machine_id = machine_id or "configured-machine"
    if any(not isinstance(value, str) or not value.strip() for value in (runtime, machine_id)):
        runtime, machine_id = "configured-runtime", "configured-machine"
    scope = f"{capability}:{mode}:{name.strip()}:{model.strip()}:{runtime.strip()}:{machine_id.strip()}"
    value = ProviderMachineCapabilityProfile(
        id=uuid5(NAMESPACE_URL, f"content-os-runtime:{scope}"),
        capability=capability, mode=mode, provider=name.strip(), model=model.strip(),
        runtime=runtime.strip()[:200], machine_id=machine_id.strip()[:200],
        readiness="configured", provenance_source="configured runtime",
        updated_at=datetime.now(timezone.utc),
    )
    return repository.save(value)
