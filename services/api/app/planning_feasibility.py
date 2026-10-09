"""Read-only planning hints, never production admission or a cost quotation."""
from __future__ import annotations

from uuid import UUID

from app.budget import snapshot
from app.db import (
    BudgetPolicyRepository, Database, ProviderCallRepository,
    ProviderMachineCapabilityProfileRepository,
)


def planning_feasibility(db: Database, project_id: UUID) -> dict[str, object]:
    """Separate stable constraints from live observations for replay identity.

    Usage is sampled before the planning reservation. It may change during this
    call and must be checked again by normal preflight/dispatch, not used as a
    content-evidence version or an idempotency identity component.
    """
    profiles = ProviderMachineCapabilityProfileRepository(db).list()
    capabilities = {}
    for capability in ("voice", "talking"):
        candidates = [p for p in profiles if p.capability == capability]
        capabilities[capability] = {
            "evidence_state": "verified" if any(
                p.readiness == "verified" and p.quality_status == "verified"
                and p.evidence_reference for p in candidates
            ) else "unverified" if candidates else "unknown",
            # Only public, typed status fields: no free-form provenance,
            # parameters, paths, credentials or evidence content.
            "profiles": [{
                "id": str(p.id), "updated_at": p.updated_at.isoformat(),
                "readiness": p.readiness, "quality_status": p.quality_status,
                "commercial_status": p.commercial_status,
                "license_evidence_present": bool(p.license_evidence_reference),
            } for p in sorted(candidates, key=lambda p: str(p.id))],
            "current_execution_license": "unverified",
            "dispatch_authorized": False,
            "estimated_cost": "unknown",
        }
    policies = []
    observations = []
    policy_repo = BudgetPolicyRepository(db)
    calls = ProviderCallRepository(db)
    for scope, scope_id in (("global", None), ("project", project_id)):
        policy = policy_repo.get_by_project(scope_id)
        if policy is None:
            policies.append({"scope": scope, "state": "unknown"})
            continue
        policies.append({
            "scope": scope, "state": "configured", "currency": policy.currency,
            "max_calls": policy.max_calls,
            "max_amount": str(policy.max_amount) if policy.max_amount is not None else None,
            "allow_unknown_cost": policy.allow_unknown_cost,
        })
        spent = snapshot(calls.list() if scope_id is None else calls.list_for_project(project_id), policy.currency)
        observations.append({
            "scope": scope, "calls": spent.calls, "reserved_calls": spent.reserved_calls,
            "remaining_calls": max(0, policy.max_calls - spent.calls) if policy.max_calls is not None else None,
            "known_amount": str(spent.known_amount), "unknown_cost_calls": spent.unknown_cost_calls,
        })
    return {
        "authority": "planning_hint_not_admission_or_authorization",
        "capabilities": capabilities, "budget_policies": policies,
        "live_usage_observation": observations,
        "usage_observation_timing": "before_this_planning_call_reservation",
        "recheck": "normal_production_preflight_and_dispatch",
        "local_compute_time": "unknown", "manual_work_time": "unknown",
    }


def planning_identity_context(context: dict[str, object]) -> dict[str, object]:
    """Exclude only live counters; retain capability and budget policy fences."""
    brief = context.get("production_feasibility")
    if not isinstance(brief, dict):
        return context
    return {**context, "production_feasibility": {
        key: value for key, value in brief.items() if key != "live_usage_observation"
    }}
