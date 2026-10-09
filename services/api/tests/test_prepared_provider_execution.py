"""Temporary durable ledger + synthetic prepared action, not normal dispatch."""
from decimal import Decimal

import pytest

from app.db import Database, ProviderCallRepository
from app.domain.models import UsageCost
from app.execution_admission import UseAdmissionError
from app.execution_scope import require_evaluation_lane_closed
from app.provider_execution import ProviderExecutionService, ProviderExecutionRecordedFailure, ProviderExecutionIdempotencyConflict, ProviderExecutionInProgress
from test_execution_admission import setup
from test_execution_attestation import v2
from test_frozen_worker import frozen_case


class Cost:
    change = None
    def estimate(self, **options):
        if self.change: self.change()
        return UsageCost(category=options['category'], amount=Decimal('0'), currency='USD')


def execute(case, service, frozen, action, *, key='prepared-call', input_document=None):
    return service.execute_prepared_voice(frozen=frozen, idempotency_key=key,
        input_source='temporary-voice', input_document={'text': 'synthetic'} if input_document is None else input_document,
        action=action, encode_result=lambda value: value, decode_result=lambda value: value,
        error_code=lambda exc: str(exc) if isinstance(exc, UseAdmissionError) else 'prepared_action_failed')


def test_only_reservation_owner_receives_same_prepared_and_replay_does_not_execute(frozen_case):
    c = frozen_case; service = ProviderExecutionService(c.db, Cost()); actions = []
    def action(prepared):
        assert prepared is c.prepared and not c.db.connection.in_transaction
        calls = ProviderCallRepository(c.db).list_for_project(c.job.project_id)
        assert len(calls) == 1 and calls[0].status == 'running'
        actions.append(prepared)
        return {'audio_id': 'synthetic-only'}
    assert execute(c, service, c.freeze(), action) == {'audio_id': 'synthetic-only'}
    replay = c.freeze(); before = len(c.observations)
    # Retained result replay rechecks authority, not a new native invocation.
    c.prepared.broken = True
    assert execute(c, service, replay, action) == {'audio_id': 'synthetic-only'}
    assert len(actions) == 1 and len(c.observations) == before
    assert ProviderCallRepository(c.db).list_for_project(c.job.project_id)[0].status == 'completed'
    with pytest.raises(UseAdmissionError, match='evaluation_execution_not_integrated'):
        require_evaluation_lane_closed(c.db, c.job)


@pytest.mark.parametrize('failure', ['bytes', 'terms', 'action'])
def test_late_failure_is_reconciled_once_and_failed_call_cannot_be_retried(frozen_case, failure):
    c = frozen_case; cost = Cost(); service = ProviderExecutionService(c.db, cost); actions = []
    frozen = c.freeze()
    def change():
        if failure == 'bytes': c.prepared.broken = True
        elif failure == 'terms': (c.path.parent / 'terms.txt').write_text('changed')
    cost.change = change
    def action(prepared):
        actions.append(prepared)
        raise RuntimeError('synthetic failure')
    with pytest.raises((UseAdmissionError, RuntimeError)): execute(c, service, frozen, action)
    call = ProviderCallRepository(c.db).list_for_project(c.job.project_id)[0]
    assert call.status == 'failed' and call.error_code
    assert len(actions) == (1 if failure == 'action' else 0)
    if failure == 'bytes': c.prepared.broken = False
    if failure == 'terms': (c.path.parent / 'terms.txt').write_text('Synthetic local terms for automated permission tests only.', encoding='utf-8')
    cost.change = None
    with pytest.raises(ProviderExecutionRecordedFailure): execute(c, service, c.freeze(), action)
    assert len(ProviderCallRepository(c.db).list_for_project(c.job.project_id)) == 1
    assert len(actions) == (1 if failure == 'action' else 0)


def test_pre_reservation_authority_change_writes_no_call(frozen_case):
    c = frozen_case; cost = Cost(); service = ProviderExecutionService(c.db, cost)
    frozen = c.freeze()
    cost.change = lambda: c.db.connection.execute("UPDATE provider_use_admissions SET payload=payload || ' ' WHERE id=?", (str(c.receipt),))
    with pytest.raises(UseAdmissionError, match='frozen_authority_changed'):
        execute(c, service, frozen, lambda _: pytest.fail('must not act'))
    assert ProviderCallRepository(c.db).list_for_project(c.job.project_id) == []


def test_replay_does_not_bypass_changed_license_evidence(frozen_case):
    c = frozen_case; service = ProviderExecutionService(c.db, Cost())
    execute(c, service, c.freeze(), lambda _: {'synthetic': True})
    frozen = c.freeze(); (c.path.parent / 'terms.txt').write_text('changed')
    with pytest.raises(UseAdmissionError, match='evidence_not_current'):
        execute(c, service, frozen, lambda _: pytest.fail('no replay action'))
    assert ProviderCallRepository(c.db).list_for_project(c.job.project_id)[0].status == 'completed'


def test_request_identity_conflict_and_cross_database_guard_are_rejected(frozen_case):
    c = frozen_case; service = ProviderExecutionService(c.db, Cost())
    execute(c, service, c.freeze(), lambda _: {'synthetic': True})
    with pytest.raises(ProviderExecutionIdempotencyConflict):
        execute(c, service, c.freeze(), lambda _: pytest.fail('no conflict action'), input_document={'text': 'different'})
    with Database(c.path) as other:
        with pytest.raises(UseAdmissionError, match='frozen_database_mismatch'):
            execute(c, ProviderExecutionService(other, Cost()), c.freeze(), lambda _: pytest.fail('wrong DB'))


def test_crash_keeps_unknown_side_effects_running_without_a_second_prepared_action(frozen_case):
    c = frozen_case; service = ProviderExecutionService(c.db, Cost()); actions = []
    def crash(prepared):
        assert prepared is c.prepared
        actions.append(prepared)
        raise KeyboardInterrupt('simulated crash')
    with pytest.raises(KeyboardInterrupt): execute(c, service, c.freeze(), crash)
    with pytest.raises(ProviderExecutionInProgress): execute(c, service, c.freeze(), crash)
    assert len(actions) == 1
    calls = ProviderCallRepository(c.db).list_for_project(c.job.project_id)
    assert len(calls) == 1 and calls[0].status == 'running'


def test_budget_rejection_never_observes_or_executes_after_reservation(frozen_case):
    from datetime import datetime, timezone
    from app.budget import BudgetLimitError
    from app.db import BudgetPolicyRepository
    from app.domain.models import BudgetPolicy
    c = frozen_case; frozen = c.freeze()
    BudgetPolicyRepository(c.db).save(BudgetPolicy(project_id=c.job.project_id,
        max_calls=0, updated_at=datetime.now(timezone.utc)))
    with pytest.raises(BudgetLimitError):
        execute(c, ProviderExecutionService(c.db, Cost()), frozen, lambda _: pytest.fail('budget must stop'))
    assert c.observations == ['observe']
    assert ProviderCallRepository(c.db).list_for_project(c.job.project_id) == []
