"""Running-status read regression; synthetic rights, real Job rows, no child."""
from datetime import timedelta
import pytest

from app.db import JobRepository
from app.domain.models import JobStatus
from app.evaluation_talking_access import EvaluationTalkingAccess
from test_evaluation_talking_access import (
    call_case, ownership, qa_case, bound_job, child_case, setup, candidate, talking_case,
)


def test_running_status_does_not_treat_healthy_heartbeat_as_authority_change(call_case, monkeypatch):
    c = call_case
    before = JobRepository(c.db).get(c.job.id)
    assert before.status == JobStatus.RUNNING

    def retained(_operation):
        current = JobRepository(c.db).get(c.job.id)
        JobRepository(c.db).update(current.model_copy(update={"updated_at": current.updated_at + timedelta(seconds=30)}))
        return c.context

    monkeypatch.setattr(c.service, "require_retained_operation", retained)
    result = EvaluationTalkingAccess(c.service).status(c.project, c.operation.id)
    assert result["state"] == "in_progress", result
    assert result["current_eligible"] and result["reasons"] == []
    assert result["stages"] == {"generation": "running", "qa": "not_created", "preview": "not_created"}
    assert c.calls == []


@pytest.mark.parametrize("mutation", ["backward_time", "attempt", "status", "payload", "authority"])
def test_running_status_still_rejects_material_change_during_read(call_case, monkeypatch, mutation):
    c = call_case
    changed = [False]
    original_facts = c.service._facts

    def retained(_operation):
        current = JobRepository(c.db).get(c.job.id)
        updates = {"updated_at": current.updated_at + timedelta(seconds=30)}
        if mutation == "backward_time": updates["updated_at"] = current.updated_at - timedelta(seconds=1)
        elif mutation == "attempt": updates["attempt"] = current.attempt + 1
        elif mutation == "status": updates["status"] = JobStatus.FAILED
        elif mutation == "payload": updates["payload"] = current.payload.model_copy(update={"authorization_reference": "changed input rights"})
        else: changed[0] = True
        JobRepository(c.db).update(current.model_copy(update=updates))
        return c.context

    monkeypatch.setattr(c.service, "require_retained_operation", retained)
    if mutation == "authority":
        monkeypatch.setattr(c.service, "_facts", lambda *args: "changed authority" if changed[0] else original_facts(*args))
    result = EvaluationTalkingAccess(c.service).status(c.project, c.operation.id)
    assert result["state"] == "blocked" and not result["current_eligible"]
    assert result["reasons"] and c.calls == []
