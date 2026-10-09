"""Frozen reservation identity, temporary DB/fake prepared only, no dispatch."""
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest

from app.budget import ProviderCallLedger, provider_call_input_digest
from app.db import Database, JobRepository, ProviderCallRepository
from app.domain.models import CostCategory, UsageCost
from app.execution_admission import UseAdmissionError
from app.execution_scope import ExecutionScopeService, WorkerExecutionUseSnapshotV2, job_use_identity, require_evaluation_lane_closed
from app.frozen_worker import freeze_worker_identity
from test_execution_admission import setup
from test_execution_attestation import v2
from test_execution_scope import _job


@pytest.fixture
def frozen_case(v2):
    case, spec, receipt, _, _ = v2
    client, path, project, profile, *_ = case
    with Database(path) as db:
        service = ExecutionScopeService(db, path.parent)
        scope = service.prepare(project, purpose='internal_evaluation', admission_ids=(receipt,))
        job = JobRepository(db).create(_job(project))
        service.pin('job', job.id, scope, operation='generation', subject_sha256=job_use_identity(job))
        snapshot = WorkerExecutionUseSnapshotV2(snapshot_version=2, identity=spec.identity,
            execution_specification=spec, execution_sha256=spec.execution_sha256)
        observations = []
        class Prepared:
            broken = False
            def execution_use_snapshot(self):
                assert not db.connection.in_transaction
                observations.append('observe')
                if self.broken: raise RuntimeError('changed file')
                return snapshot.model_dump(mode='json')
        prepared = Prepared()
        provider = SimpleNamespace(provider_name=profile.provider, model=profile.model)
        def freeze():
            return freeze_worker_identity(service, job, provider, prepared,
                capability_profile_id=profile.id, execution_admission_id=receipt)
        yield SimpleNamespace(client=client, db=db, service=service, job=job, profile=profile, receipt=receipt,
            path=path, provider=provider, prepared=prepared, observations=observations, freeze=freeze)


def reserve(case, guard):
    return ProviderCallLedger(case.db).reserve_execution(project_id=case.job.project_id,
        idempotency_key='frozen-test', operation='tts', mode='runtime', provider=case.provider.provider_name,
        model=case.provider.model, input_source='temporary-fake-prepared',
        input_digest=provider_call_input_digest({'job': str(case.job.id)}),
        estimated_cost=UsageCost(category=CostCategory.VOICE, amount=Decimal('0'), currency='USD'),
        reservation_guard=guard)


def test_capture_guard_and_prelaunch_keep_same_prepared_without_file_reads_under_lock(frozen_case, monkeypatch):
    c = frozen_case; frozen = c.freeze()
    assert frozen.prepared is c.prepared and c.observations == ['observe']
    detached = frozen.snapshot
    detached.execution_specification.parameters['steps'] = 999
    assert frozen.snapshot.execution_specification.parameters['steps'] != 999
    original_file = c.service.admissions._file
    def file_read(*args, **kwargs):
        assert not c.db.connection.in_transaction
        return original_file(*args, **kwargs)
    monkeypatch.setattr(c.service.admissions, '_file', file_read)
    result = reserve(c, frozen.reservation_guard)
    assert result.owner and c.observations == ['observe']
    frozen.verify_before_launch()
    assert c.observations == ['observe', 'observe']
    with pytest.raises(UseAdmissionError, match='frozen_identity_consumed'): frozen.verify_before_launch()
    assert not hasattr(frozen, 'execute')
    with pytest.raises(UseAdmissionError, match='evaluation_execution_not_integrated'):
        require_evaluation_lane_closed(c.db, c.job)


@pytest.mark.parametrize('change', ['receipt', 'profile', 'setting', 'job', 'job_payload', 'scope', 'provider'])
def test_changed_durable_authority_rejects_reservation_and_consumes_capture(frozen_case, change):
    c = frozen_case; frozen = c.freeze()
    if change == 'receipt':
        c.db.connection.execute("UPDATE provider_use_admissions SET payload=payload || ' ' WHERE id=?", (str(c.receipt),))
    elif change == 'profile':
        c.db.connection.execute("UPDATE provider_machine_capability_profiles SET payload=payload || ' ' WHERE id=?", (str(c.profile.id),))
    elif change == 'setting':
        c.db.connection.execute("INSERT INTO provider_machine_settings(id,scope_key,payload) VALUES ('fake',?,'{}')", (c.profile.scope_key,))
    elif change == 'job':
        c.db.connection.execute("UPDATE jobs SET idempotency_key='changed' WHERE id=?", (str(c.job.id),))
    elif change == 'job_payload':
        c.db.connection.execute("UPDATE jobs SET payload=json_set(payload,'$.idempotency_key','changed') WHERE id=?", (str(c.job.id),))
    elif change == 'scope':
        c.db.connection.execute("DELETE FROM execution_use_scopes WHERE subject_kind='job' AND subject_id=?", (str(c.job.id),))
    else: c.provider.model = 'different-model'
    with pytest.raises(UseAdmissionError): reserve(c, frozen.reservation_guard)
    assert ProviderCallRepository(c.db).list_for_project(c.job.project_id) == []
    assert c.observations == ['observe']
    with pytest.raises(UseAdmissionError, match='frozen_identity_consumed'): frozen.reservation_guard()


@pytest.mark.parametrize('change', ['prepared_bytes', 'terms', 'review', 'revoke'])
def test_postreservation_changes_stop_before_launch_without_automatic_refund_or_retry(frozen_case, change):
    c = frozen_case; frozen = c.freeze(); reservation = reserve(c, frozen.reservation_guard)
    if change == 'prepared_bytes': c.prepared.broken = True
    elif change == 'terms': (c.path.parent / 'terms.txt').write_text('changed')
    elif change == 'review': (c.path.parent / 'adopt-v2.json').write_text('{}')
    else:
        row = c.db.connection.execute('SELECT payload FROM provider_use_admissions WHERE id=?', (str(c.receipt),)).fetchone()
        payload = json.loads(row[0]); payload.update(revoked_at='2026-10-07T00:00:00Z', revoked_by='operator', revocation_reason='test revoke')
        c.db.connection.execute('UPDATE provider_use_admissions SET payload=? WHERE id=?', (json.dumps(payload), str(c.receipt)))
    with pytest.raises(UseAdmissionError): frozen.verify_before_launch()
    with pytest.raises(UseAdmissionError, match='frozen_identity_consumed'): frozen.verify_before_launch()
    # Caller owns late-failure reconciliation; comparison never retries/refunds.
    calls = ProviderCallRepository(c.db).list_for_project(c.job.project_id)
    assert len(calls) == 1 and calls[0].id == reservation.record.id and calls[0].status == 'running'


def test_capture_and_prelaunch_refuse_to_observe_inside_a_transaction(frozen_case):
    c = frozen_case
    with c.db.transaction(immediate=True):
        with pytest.raises(UseAdmissionError, match='observation_inside_transaction'): c.freeze()
    assert c.observations == []
    frozen = c.freeze(); reserve(c, frozen.reservation_guard)
    with c.db.transaction(immediate=True):
        with pytest.raises(UseAdmissionError, match='observation_inside_transaction'): frozen.verify_before_launch()
    assert c.observations == ['observe']


def test_guard_requires_transaction_and_prelaunch_requires_prior_guard(frozen_case):
    c = frozen_case; frozen = c.freeze()
    with pytest.raises(UseAdmissionError, match='frozen_identity_consumed'): frozen.verify_before_launch()
    frozen = c.freeze()
    with pytest.raises(UseAdmissionError, match='reservation_transaction_required'): frozen.reservation_guard()
    with pytest.raises(UseAdmissionError, match='frozen_identity_consumed'): frozen.reservation_guard()


def test_job_attempt_and_status_are_not_execution_input_changes(frozen_case):
    c = frozen_case; frozen = c.freeze()
    c.db.connection.execute("UPDATE jobs SET status='running',attempt=1,payload=json_set(payload,'$.status','running','$.attempt',1) WHERE id=?", (str(c.job.id),))
    assert reserve(c, frozen.reservation_guard).owner
    frozen.verify_before_launch()


def test_changed_owning_run_scope_is_not_lost_by_the_frozen_guard(frozen_case):
    c = frozen_case
    # Attach an existing temporary Run to this Job and pin the identical scope.
    preflight = c.client.post(f'/projects/{c.job.project_id}/production-preflight', json={}).json()
    created = c.client.post(f'/projects/{c.job.project_id}/production-runs', json={
        'idempotency_key': 'frozen-owning-run', 'expected_fingerprint': preflight['fingerprint']})
    assert created.status_code == 201, created.text
    row = (created.json()['id'],)
    from uuid import UUID
    c.db.connection.execute('UPDATE production_runs SET voice_job_id=? WHERE id=?', (str(c.job.id), row[0]))
    scope = c.service.get('job', c.job.id)
    c.service.pin('run', UUID(row[0]), scope, operation='generation')
    frozen = c.freeze()
    # Same admission IDs but different immutable ancestor bytes still stop.
    # Change payload/fingerprint consistently while preserving admission IDs.
    ancestor = scope.model_dump(mode='json'); ancestor['bindings'][0]['review_sha256'] = 'b'*64
    from app.execution_scope import _make_scope
    changed = _make_scope(scope.project_id, scope.purpose, tuple(type(scope.bindings[0]).model_validate_json(json.dumps(b)) for b in ancestor['bindings']))
    c.db.connection.execute('UPDATE execution_use_scopes SET payload=?,fingerprint=? WHERE subject_kind=? AND subject_id=?',
        (changed.model_dump_json(), changed.fingerprint, 'run', row[0]))
    with pytest.raises(UseAdmissionError, match='frozen_authority_changed'):
        reserve(c, frozen.reservation_guard)
