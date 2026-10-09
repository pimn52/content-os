from datetime import datetime, timezone
import json
from uuid import uuid4

import pytest

from app.db import Database, BudgetPolicyRepository, ProviderMachineCapabilityProfileRepository
from app.domain.models import BudgetPolicy, ProviderMachineCapabilityProfile
from app.planning_feasibility import planning_feasibility, planning_identity_context


def test_empty_read_only_brief_preserves_unknowns(tmp_path):
    with Database(tmp_path / "brief.sqlite") as db:
        before = db.connection.total_changes
        brief = planning_feasibility(db, uuid4())
        assert db.connection.total_changes == before
    for capability in brief["capabilities"].values():
        assert capability["evidence_state"] == "unknown"
        assert capability["estimated_cost"] == "unknown"
        assert capability["dispatch_authorized"] is False
    assert brief["budget_policies"] == [
        {"scope": "global", "state": "unknown"}, {"scope": "project", "state": "unknown"},
    ]
    assert brief["live_usage_observation"] == []


@pytest.mark.parametrize("readiness,quality,evidence,expected", [
    ("configured", "unknown", None, "unverified"),
    ("available", "observed", "private-evidence", "unverified"),
    ("verified", "unknown", "private-evidence", "unverified"),
    ("verified", "verified", "private-evidence", "verified"),
])
def test_profile_evidence_never_grants_execution_or_exposes_free_text(tmp_path, readiness, quality, evidence, expected):
    with Database(tmp_path / "brief.sqlite") as db:
        ProviderMachineCapabilityProfileRepository(db).save(ProviderMachineCapabilityProfile(
            capability="talking", mode="local", provider="local", model="model", runtime="runtime",
            machine_id="machine", readiness=readiness, quality_status=quality,
            evidence_reference=evidence, provenance_source="private-provenance",
            commercial_status="commercial_safe", license_evidence_reference="private-license",
            updated_at=datetime.now(timezone.utc),
        ))
        brief = planning_feasibility(db, uuid4())
    talking = brief["capabilities"]["talking"]
    assert talking["evidence_state"] == expected
    assert talking["current_execution_license"] == "unverified"
    assert talking["dispatch_authorized"] is False
    assert "private-" not in json.dumps(brief)


def test_both_budget_scopes_and_live_counter_identity_exclusion(tmp_path):
    project_id = uuid4()
    with Database(tmp_path / "brief.sqlite") as db:
        for scope_id, max_calls in ((None, 0), (project_id, 10)):
            BudgetPolicyRepository(db).save(BudgetPolicy(
                project_id=scope_id, max_calls=max_calls,
                allow_unknown_cost=False, updated_at=datetime.now(timezone.utc),
            ))
        brief = planning_feasibility(db, project_id)
    assert [row["remaining_calls"] for row in brief["live_usage_observation"]] == [0, 10]
    assert all(row["max_amount"] is None for row in brief["budget_policies"])
    context = {"production_feasibility": brief, "evidence_refs": ["ip:v1"]}
    identity = planning_identity_context(context)
    assert "live_usage_observation" not in identity["production_feasibility"]
    assert "live_usage_observation" in context["production_feasibility"]  # never mutate prompt
    changed_usage = {**context, "production_feasibility": {**brief, "live_usage_observation": []}}
    assert planning_identity_context(changed_usage) == identity
    changed_policy = {**context, "production_feasibility": {**brief, "budget_policies": []}}
    assert planning_identity_context(changed_policy) != identity
