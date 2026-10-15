"""Read-only/atomic budget gates; input authority explicitly STUBBED."""
from datetime import datetime, timezone
import pytest
from app.db import BudgetPolicyRepository, JobRepository, ProviderCallRepository
from app.domain.models import BudgetPolicy
from app.costs import UnknownProviderCostEstimator
from app.domain.models import UsageCost, CostCategory
from app.evaluation_talking import EvaluationTalkingService
from types import SimpleNamespace
from app.execution_admission import UseAdmissionError
from test_evaluation_talking_ownership import ownership, qa_case, bound_job, child_case, setup, candidate, talking_case


def policy(c,**kwargs):
    return BudgetPolicyRepository(c.db).save(BudgetPolicy(updated_at=datetime.now(timezone.utc),**kwargs))


@pytest.mark.parametrize("kind",["missing","wrong_category","private_exception"])
def test_invalid_cost_adapter_is_rejected_with_fixed_reason(tmp_path,kind):
    def estimate(**_kwargs):
        if kind == "private_exception": raise RuntimeError("private operator path")
        if kind == "wrong_category": return UsageCost(category=CostCategory.LLM,amount="0",currency="USD")
        return None
    service=EvaluationTalkingService(None,tmp_path,cost_estimator=SimpleNamespace(estimate=estimate))
    with pytest.raises(UseAdmissionError,match="^evaluation_talking_cost_estimate_invalid$"):
        service._budget_estimate(dict(cap=SimpleNamespace(provider="fixture",model="fixture")))


def test_unknown_cost_preflight_blocks_before_job_or_ledger(ownership):
    c=ownership
    c.service.cost_estimator=UnknownProviderCostEstimator()
    policy(c,project_id=c.project,allow_unknown_cost=False)
    before=c.db.connection.total_changes
    plan=c.service.preflight(c.project,c.payload)
    assert plan["eligible"] is False and plan["reasons"] == ["budget_unknown_cost"]
    assert c.db.connection.total_changes == before
    with pytest.raises(UseAdmissionError,match="budget_unknown_cost"):
        c.service.create(c.project,c.payload)
    assert c.db.connection.execute("SELECT COUNT(*) FROM evaluation_talking_operations").fetchone()[0] == 0


@pytest.mark.parametrize("scope",["global","project"])
def test_current_call_limit_blocks_preflight_and_atomic_create(ownership,scope):
    c=ownership
    policy(c,project_id=None if scope == "global" else c.project,max_calls=0,allow_unknown_cost=True)
    before=c.db.connection.total_changes
    plan=c.service.preflight(c.project,c.payload)
    assert plan["reasons"] == ["budget_call_limit"] and not plan["eligible"]
    with pytest.raises(UseAdmissionError,match="budget_call_limit"): c.service.create(c.project,c.payload)
    assert c.db.connection.total_changes == before


def test_policy_changed_after_preflight_blocks_enqueue_without_spending(ownership):
    c=ownership
    plan=c.service.preflight(c.project,c.payload)
    assert plan["eligible"] and plan["budget_state"] == "checked_not_reserved"
    before_calls=ProviderCallRepository(c.db).list_all()
    policy(c,project_id=c.project,max_calls=0,allow_unknown_cost=True)
    with pytest.raises(UseAdmissionError,match="budget_call_limit"): c.service.create(c.project,c.payload)
    assert ProviderCallRepository(c.db).list_all() == before_calls
    assert c.db.connection.execute("SELECT COUNT(*) FROM evaluation_talking_operations").fetchone()[0] == 0


def test_existing_operation_recovery_needs_no_new_budget_or_ledger_reservation(ownership):
    c=ownership
    operation=c.service.create(c.project,c.payload)
    job=JobRepository(c.db).get(operation.generation_job_id)
    policy(c,max_calls=0,allow_unknown_cost=True)
    before=c.db.connection.total_changes
    plan=c.service.preflight(c.project,c.payload)
    assert plan["eligible"] and plan["existing_operation_id"] == str(operation.id)
    assert plan["budget_state"] == "existing_operation_no_new_reservation"
    assert c.service.create(c.project,c.payload.model_copy(update={"idempotency_key":"different-ui-key"})) == operation
    assert JobRepository(c.db).get(operation.generation_job_id) == job
    assert c.db.connection.total_changes == before
