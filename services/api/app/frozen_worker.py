"""Application-only frozen identity seam; not a dispatch/admission token.

Expensive prepared/file/report/inventory/terms observation stays outside SQLite
write transactions. Reservation compares current durable facts only. Normal
evaluation dispatch remains structurally disabled until the whole lane closes.
"""
from types import SimpleNamespace

from app.db import JobRepository
from app.execution_admission import UseAdmissionError
from app.execution_scope import WorkerExecutionUseSnapshotV2, job_use_identity, _job_owner_scopes


def _outside_transaction(service):
    if service.db.connection.in_transaction:
        raise UseAdmissionError('execution_observation_inside_transaction')


def _facts(service, job, scope):
    row = service.db.connection.execute('SELECT project_id,idempotency_key FROM jobs WHERE id=?', (str(job.id),)).fetchone()
    if row is None or tuple(row) != (str(job.project_id), job.idempotency_key):
        raise UseAdmissionError('execution_use_job_changed')
    current = JobRepository(service.db).get(job.id)
    if current is None or job_use_identity(current) != job_use_identity(job):
        raise UseAdmissionError('execution_use_job_changed')
    actual = service.require_job_snapshot(current)
    if actual != scope:
        raise UseAdmissionError('execution_use_scope_changed')
    rows = [actual.model_dump_json()]
    rows.extend(value.model_dump_json() for value in _job_owner_scopes(service.db, current))
    for binding in scope.bindings:
        receipt = service.db.connection.execute(
            'SELECT * FROM provider_use_admissions WHERE id=? AND project_id=?',
            (str(binding.admission_id), str(job.project_id))).fetchone()
        profile = service.db.connection.execute(
            'SELECT * FROM provider_machine_capability_profiles WHERE id=?',
            (str(binding.capability_profile_id),)).fetchone()
        if receipt is None or profile is None:
            raise UseAdmissionError('provider_use_binding_changed')
        setting = service.db.connection.execute(
            'SELECT * FROM provider_machine_settings WHERE scope_key=?', (profile['scope_key'],)).fetchone()
        rows.extend((tuple(receipt), tuple(profile), None if setting is None else tuple(setting)))
    return tuple(rows)


def _snapshot(prepared):
    try:
        observed = prepared.execution_use_snapshot()
        import json
        raw = observed.model_dump_json() if isinstance(observed, WorkerExecutionUseSnapshotV2) else json.dumps(observed, allow_nan=False)
        return WorkerExecutionUseSnapshotV2.model_validate_json(raw)
    except Exception:
        raise UseAdmissionError('execution_runtime_identity_unavailable') from None


class FrozenWorkerIdentity:
    """Keep the same prepared object alive through one guarded reservation.

    There is deliberately no execute method. A successful comparison is not
    permission to bypass Worker, current source/QA/consent, budget or lane gates.
    """
    __slots__ = ('_service', '_job', '_provider', '_prepared', '_scope', '_facts',
        '_snapshot_json', '_profile_id', '_admission_id', '_state')

    def __init__(self, service, job, provider, prepared, scope, facts, snapshot, profile_id, admission_id):
        self._service, self._job, self._provider, self._prepared = service, job, provider, prepared
        self._scope, self._facts = scope, facts
        self._snapshot_json = snapshot.model_dump_json()
        self._profile_id, self._admission_id, self._state = profile_id, admission_id, 'ready'

    @property
    def prepared(self): return self._prepared

    @property
    def job(self): return type(self._job).model_validate_json(self._job.model_dump_json())

    @property
    def scope_fingerprint(self): return self._scope.fingerprint

    def belongs_to(self, db): return self._service.db is db

    @property
    def snapshot(self): return WorkerExecutionUseSnapshotV2.model_validate_json(self._snapshot_json)

    def reservation_guard(self):
        if self._state != 'ready': raise UseAdmissionError('execution_frozen_identity_consumed')
        self._state = 'failed'
        if not self._service.db.connection.in_transaction:
            raise UseAdmissionError('execution_reservation_transaction_required')
        if _facts(self._service, self._job, self._scope) != self._facts:
            raise UseAdmissionError('execution_frozen_authority_changed')
        # Only detached values and DB rows, never a provider/native/file call.
        if (getattr(self._provider, 'provider_name', None), getattr(self._provider, 'model', None)) != (
                self.snapshot.identity.provider, self.snapshot.identity.model):
            raise UseAdmissionError('execution_runtime_identity_changed')
        self._state = 'guarded'

    def verify_before_launch(self):
        if self._state != 'guarded': raise UseAdmissionError('execution_frozen_identity_consumed')
        self._state = 'failed'
        _outside_transaction(self._service)
        if _facts(self._service, self._job, self._scope) != self._facts:
            raise UseAdmissionError('execution_frozen_authority_changed')
        actual = _snapshot(self._prepared)
        if actual != self.snapshot: raise UseAdmissionError('execution_runtime_identity_changed')
        self._service.require_worker_identity(self._job, _observed(self._provider, actual),
            capability_profile_id=self._profile_id, execution_admission_id=self._admission_id)
        if _facts(self._service, self._job, self._scope) != self._facts:
            raise UseAdmissionError('execution_frozen_authority_changed')
        self._state = 'verified'

    def verify_for_replay(self):
        """Retained result is not a fresh call or an authority bypass."""
        if self._state != 'ready': raise UseAdmissionError('execution_frozen_identity_consumed')
        self._state = 'failed'
        _outside_transaction(self._service)
        if _facts(self._service, self._job, self._scope) != self._facts:
            raise UseAdmissionError('execution_frozen_authority_changed')
        self._service.require_current(self._scope, project_id=self._job.project_id,
            purpose=self._scope.purpose, operation='generation')
        if _facts(self._service, self._job, self._scope) != self._facts:
            raise UseAdmissionError('execution_frozen_authority_changed')
        self._state = 'replayed'


def _observed(provider, snapshot):
    return SimpleNamespace(provider_name=getattr(provider, 'provider_name', None),
        model=getattr(provider, 'model', None), execution_use_snapshot=lambda: snapshot)


def freeze_worker_identity(service, job, provider, prepared, *, capability_profile_id, execution_admission_id):
    """Owned adapter must supply an independently prepared object, not a receipt.

    Synthetic prepared objects are test evidence only; this helper is not
    exposed as an API and does not certify native/model dependency completeness.
    """
    _outside_transaction(service)
    scope = service.require_job_snapshot(job)
    if scope is None or scope.purpose != 'internal_evaluation':
        raise UseAdmissionError('execution_use_scope_required')
    before = _facts(service, job, scope)
    snapshot = _snapshot(prepared)
    service.require_worker_identity(job, _observed(provider, snapshot),
        capability_profile_id=capability_profile_id, execution_admission_id=execution_admission_id)
    if _facts(service, job, scope) != before:
        raise UseAdmissionError('execution_frozen_authority_changed')
    return FrozenWorkerIdentity(service, job, provider, prepared, scope, before, snapshot,
        capability_profile_id, execution_admission_id)
