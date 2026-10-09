"""Explicit nested schema migration, temporary DB/fake observation only."""
import hashlib
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from app.db import Database, JobRepository, ProviderCallRepository
from app.domain.models import ExecutionSpecificationV3, ProviderUseAdmission
from app.execution_admission import UseAdmissionError, ProviderUseAdmissionService
from app.execution_scope import (ExecutionScopeService, ExecutionUseBindingV2,
    WorkerExecutionUseSnapshotV2, job_use_identity, require_evaluation_lane_closed)
from app.frozen_worker import freeze_worker_identity
from app.runtime_inventory import RuntimeInventoryStore
from app.provider_execution import ProviderExecutionService
from test_component_policy2 import policy2
from test_execution_admission import setup, headers
from test_execution_attestation import specification, report_v2, adopt_v2
from test_execution_scope import _job
from test_execution_specification_v3 import raw_spec
from test_host_policy_inventory import declared, validated
from test_runtime_inventory import inventory, spec2
from test_prepared_provider_execution import Cost


@pytest.fixture
def pending3(setup, raw_spec, declared):
    case = setup
    raw = specification(case[3]).model_dump(mode='json')
    raw.pop('runtime_artifacts')
    value = validated(declared[1])
    raw.update(schema_version=3, runtime_inventory=value.descriptor.model_dump(mode='json'),
        host_runtime=raw_spec['host_runtime'])
    spec = ExecutionSpecificationV3.model_validate_json(json.dumps(raw))
    report = report_v2(case, spec)
    source = case[1].parent / 'schema3-review.json'
    source.write_text(report.model_dump_json(), encoding='utf-8')
    payload = dict(idempotency_key='schema3-adopt', review_reference=source.name,
        review_sha256=hashlib.sha256(source.read_bytes()).hexdigest())
    return SimpleNamespace(case=case, value=value, spec=spec, report=report, source=source, payload=payload)


@pytest.fixture
def admitted3(pending3):
    c = pending3
    RuntimeInventoryStore(c.case[1].parent).install(c.value)
    response = c.case[0].post(f'/projects/{c.case[2]}/provider-use-admissions',
        json=c.payload, headers=headers())
    assert response.status_code == 201, response.text
    c.receipt = UUID(response.json()['id'])
    return c


def test_adoption_requires_current_inventory_before_any_receipt(pending3):
    c = pending3
    response = c.case[0].post(f'/projects/{c.case[2]}/provider-use-admissions', json=c.payload, headers=headers())
    assert response.status_code == 409 and 'inventory_unavailable' in response.text
    with Database(c.case[1]) as db:
        assert ProviderUseAdmissionService(db, c.case[1].parent).list(c.case[2]) == []
        assert ProviderCallRepository(db).list_for_project(c.case[2]) == []


@pytest.mark.parametrize('policy', ['os', 'component'])
def test_matching_report_hash_does_not_hide_wrong_owned_policy(pending3, policy):
    from app.domain.execution_runtime import OS_POLICY_SLOT
    from app.providers.windows_component_policy import POLICY_SLOT
    c = pending3
    raw = c.value.model_dump(mode='json')
    name = OS_POLICY_SLOT if policy == 'os' else POLICY_SLOT
    next(item for item in raw['files'] if item['name'] == name)['sha256'] = 'e'*64
    changed = validated(raw)
    RuntimeInventoryStore(c.case[1].parent).install(changed)
    spec = c.spec.model_copy(update={'runtime_inventory':changed.descriptor})
    report = report_v2(c.case, spec)
    c.source.write_text(report.model_dump_json(), encoding='utf-8')
    payload = dict(c.payload, review_sha256=hashlib.sha256(c.source.read_bytes()).hexdigest())
    response = c.case[0].post(f'/projects/{c.case[2]}/provider-use-admissions', json=payload, headers=headers())
    assert response.status_code == 409 and 'policy_inventory_identity_changed' in response.text
    assert c.case[0].get(f'/projects/{c.case[2]}/provider-use-admissions').json() == []


def test_receipt_roundtrip_replay_restart_and_same_prepared_identity(admitted3):
    c = admitted3
    client, path, project, profile, *_ = c.case
    replay = client.post(f'/projects/{project}/provider-use-admissions', json=c.payload, headers=headers())
    assert replay.status_code == 201 and UUID(replay.json()['id']) == c.receipt
    assert ProviderUseAdmission.model_validate_json(json.dumps(replay.json())).review == c.report
    snapshot = WorkerExecutionUseSnapshotV2(snapshot_version=2, identity=c.spec.identity,
        execution_specification=c.spec, execution_sha256=c.spec.execution_sha256)
    observations = []
    prepared = SimpleNamespace(execution_use_snapshot=lambda: observations.append('fake-observe') or snapshot)
    provider = SimpleNamespace(provider_name=profile.provider, model=profile.model)
    with Database(path) as db:
        service = ExecutionScopeService(db, path.parent)
        scope = service.prepare(project, purpose='internal_evaluation', admission_ids=(c.receipt,))
        job = JobRepository(db).create(_job(project))
        service.pin('job', job.id, scope, operation='generation', subject_sha256=job_use_identity(job))
        frozen = freeze_worker_identity(service, job, provider, prepared,
            capability_profile_id=profile.id, execution_admission_id=c.receipt)
        assert frozen.prepared is prepared and frozen.snapshot.execution_specification == c.spec
        with db.transaction(immediate=True): frozen.reservation_guard()
        frozen.verify_before_launch()
        assert observations == ['fake-observe', 'fake-observe']
        with pytest.raises(UseAdmissionError, match='evaluation_execution_not_integrated'):
            require_evaluation_lane_closed(db, job)
    with Database(path) as db:
        service = ExecutionScopeService(db, path.parent)
        assert service.get('job', job.id).model_dump_json() == scope.model_dump_json()
        assert ProviderCallRepository(db).list_for_project(project) == []


def test_schema1_2_receipts_and_mixed_output_restrictions_remain_exact(admitted3):
    c = admitted3
    client, path, project, profile, *_ = c.case
    one, _, _ = adopt_v2(c.case, specification(profile), key='old-schema1')
    descriptor = RuntimeInventoryStore(path.parent).install(inventory(1))
    two, _, _ = adopt_v2(c.case, spec2(c.case, descriptor), key='old-schema2')
    with Database(path) as db:
        old_rows = [tuple(row) for row in db.connection.execute(
            'SELECT * FROM provider_use_admissions WHERE id IN (?,?) ORDER BY id', (str(one),str(two)))]
        service = ExecutionScopeService(db, path.parent)
        scope = service.prepare(project, purpose='internal_evaluation', admission_ids=(one,two,c.receipt))
        assert {b.execution_specification.schema_version for b in scope.bindings} == {1,2,3}
        job = JobRepository(db).create(_job(project))
        service.pin('job', job.id, scope, operation='generation', subject_sha256=job_use_identity(job))
        audio, preview, render = uuid4(), uuid4(), uuid4()
        service.inherit('audio', audio, project_id=project, purpose=scope.purpose,
            parents=(('job',job.id),), operation='generation', content_hash='7'*64)
        service.inherit('preview', preview, project_id=project, purpose=scope.purpose,
            parents=(('audio',audio),), operation='derivation', content_hash='8'*64)
        service.inherit('render', render, project_id=project, purpose=scope.purpose,
            parents=(('preview',preview),), operation='derivation', content_hash='9'*64)
        assert old_rows == [tuple(row) for row in db.connection.execute(
            'SELECT * FROM provider_use_admissions WHERE id IN (?,?) ORDER BY id', (str(one),str(two)))]
    with Database(path) as db:
        service = ExecutionScopeService(db, path.parent)
        assert service.get('render',render) == scope
        assert all(isinstance(b,ExecutionUseBindingV2) for b in scope.bindings)
        for digest in ('7'*64,'8'*64,'9'*64):
            with pytest.raises(UseAdmissionError, match='evaluation_scope_not_commercial'):
                service.require_content(digest, project_id=project)
        manifest = service.manifest('render',render,content_hash='9'*64)
        assert manifest['commercial_authorized'] is False and manifest['execution_authorized'] is False
    revoke = client.post(f'/projects/{project}/provider-use-admissions/{c.receipt}/revoke',
        json={'reason':'synthetic revocation'}, headers=headers())
    assert revoke.status_code == 200
    with Database(path) as db:
        service = ExecutionScopeService(db, path.parent)
        with pytest.raises(UseAdmissionError, match='provider_use_admission_revoked'):
            service.require_current(scope, project_id=project, purpose=scope.purpose, operation='download')
        assert service.get('render',render) == scope


def test_changed_current_inventory_blocks_receipt_consumption_without_rewriting(admitted3):
    c = admitted3
    path, project = c.case[1:3]
    with Database(path) as db:
        service = ExecutionScopeService(db, path.parent)
        scope = service.prepare(project, purpose='internal_evaluation', admission_ids=(c.receipt,))
        before = tuple(db.connection.execute('SELECT * FROM provider_use_admissions WHERE id=?',(str(c.receipt),)).fetchone())
        source = RuntimeInventoryStore(path.parent)._path(c.value.descriptor)
        source.write_bytes(b'changed inventory')
        with pytest.raises(UseAdmissionError, match='provider_use_evidence_not_current'):
            service.require_current(scope, project_id=project, purpose=scope.purpose, operation='generation')
        assert before == tuple(db.connection.execute('SELECT * FROM provider_use_admissions WHERE id=?',(str(c.receipt),)).fetchone())


def test_normal_api_does_not_dispatch_from_schema3_receipt(admitted3):
    c = admitted3
    client,path,project,*_ = c.case
    request = dict(purpose='internal_evaluation',use_admission_ids=[str(c.receipt)])
    preview = client.post(f'/projects/{project}/production-preflight',json=request)
    assert preview.status_code == 200 and 'evaluation_execution_not_integrated' in preview.json()['stop_reasons']
    response = client.post(f'/projects/{project}/production-runs',json=dict(request,
        idempotency_key='schema3-still-closed',expected_fingerprint=preview.json()['fingerprint']))
    assert response.status_code == 409
    with Database(path) as db:
        for table in ('production_runs','jobs','execution_use_scopes'):
            assert db.connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0
        assert ProviderCallRepository(db).list_for_project(project) == []


def test_legacy_policy1_ancestor_keeps_its_operation_limit(admitted3):
    c = admitted3
    client,path,project,*_ = c.case
    old = client.post(f'/projects/{project}/provider-use-admissions',json=c.case[6],headers=headers())
    assert old.status_code == 201
    old_id = UUID(old.json()['id'])
    with Database(path) as db:
        before = tuple(db.connection.execute('SELECT * FROM provider_use_admissions WHERE id=?',(str(old_id),)).fetchone())
        service = ExecutionScopeService(db,path.parent)
        scope = service.prepare(project,purpose='internal_evaluation',admission_ids=(old_id,c.receipt))
        audio = uuid4()
        service.pin('audio',audio,scope,operation='generation',content_hash='5'*64)
        with pytest.raises(UseAdmissionError,match='provider_use_operation_not_permitted'):
            service.inherit('preview',uuid4(),project_id=project,purpose=scope.purpose,
                parents=(('audio',audio),),operation='derivation',content_hash='6'*64)
        assert before == tuple(db.connection.execute('SELECT * FROM provider_use_admissions WHERE id=?',(str(old_id),)).fetchone())
        assert service.scopes_for_hash('6'*64) == ()


@pytest.mark.parametrize('late_change',[False,True])
def test_schema3_same_prepared_owner_replay_or_changed_host_reconciliation(admitted3,late_change):
    c = admitted3
    _,path,project,profile,*_ = c.case
    current = [c.spec]
    actions = []
    with Database(path) as db:
        scope_service = ExecutionScopeService(db,path.parent)
        scope = scope_service.prepare(project,purpose='internal_evaluation',admission_ids=(c.receipt,))
        job = JobRepository(db).create(_job(project))
        scope_service.pin('job',job.id,scope,operation='generation',subject_sha256=job_use_identity(job))
        def observe():
            assert not db.connection.in_transaction
            spec = current[0]
            return WorkerExecutionUseSnapshotV2(snapshot_version=2,identity=spec.identity,
                execution_specification=spec,execution_sha256=spec.execution_sha256)
        prepared = SimpleNamespace(execution_use_snapshot=observe)
        provider = SimpleNamespace(provider_name=profile.provider,model=profile.model)
        def freeze():
            return freeze_worker_identity(scope_service,job,provider,prepared,
                capability_profile_id=profile.id,execution_admission_id=c.receipt)
        cost = Cost()
        frozen = freeze()
        if late_change:
            raw = c.spec.model_dump(mode='json')
            raw['host_runtime']['revision'] += 1
            changed = ExecutionSpecificationV3.model_validate_json(json.dumps(raw))
            cost.change = lambda: current.__setitem__(0,changed)
        executor = ProviderExecutionService(db,cost)
        audio = uuid4()
        def action(retained):
            assert retained is prepared and not db.connection.in_transaction
            actions.append(retained)
            return {'audio_id':str(audio)}
        def execute(frozen):
            return executor.execute_prepared_voice(frozen=frozen,idempotency_key='schema3-fake-owner',
                input_source='synthetic-only',input_document={'text':'synthetic'},action=action,
                encode_result=lambda value:value,decode_result=lambda value:value,
                error_code=lambda exc:str(exc))
        if late_change:
            with pytest.raises(UseAdmissionError,match='execution_runtime_identity_changed'):
                execute(frozen)
            assert actions == []
            call = ProviderCallRepository(db).list_for_project(project)[0]
            assert call.status == 'failed' and call.error_code == 'execution_runtime_identity_changed'
        else:
            result = execute(frozen)
            assert result == execute(freeze()) and len(actions) == 1
            scope_service.inherit('audio',audio,project_id=project,purpose=scope.purpose,
                parents=(('job',job.id),),operation='generation',content_hash='4'*64)
            assert scope_service.get('audio',audio) == scope
            with pytest.raises(UseAdmissionError,match='evaluation_scope_not_commercial'):
                scope_service.require_content('4'*64,project_id=project)
            assert ProviderCallRepository(db).list_for_project(project)[0].status == 'completed'
        assert len(ProviderCallRepository(db).list_for_project(project)) == 1
        with pytest.raises(UseAdmissionError,match='evaluation_execution_not_integrated'):
            require_evaluation_lane_closed(db,job)
