"""Temporary evidence/DB only: purpose permission never dispatches inference."""
from datetime import datetime, timezone
import hashlib
import json
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.db import Database, ProviderCallRepository, ProviderMachineCapabilityProfileRepository, ProviderMachineSettingRepository
from app.domain.models import ExecutionUseIdentity, ProviderMachineSetting, ProviderUseLicenseReview
from app.execution_admission import ProviderUseAdmissionService, configuration_digest, configured_admission_actor, resolve_provider_use
from app.main import create_app
from test_production_runs import _ready_fixture, _voice_dispatch_evidence

OPERATOR_KEY = 'unit-test-admission-key-0123456789-abcdefgh'


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('CONTENT_OS_EXECUTION_ADMISSION_KEY_HASHES', json.dumps({'operator':hashlib.sha256(OPERATOR_KEY.encode()).hexdigest()}))
    path = tmp_path / 'admission.sqlite3'
    with TestClient(create_app(path)) as client:
        project, _, _ = _ready_fixture(client, path, with_master=False)
        _, profile_id = _voice_dispatch_evidence(path, license_status='non_commercial_only')
        with Database(path) as db:
            repo = ProviderMachineCapabilityProfileRepository(db)
            profile = repo.get(UUID(profile_id)).model_copy(update={
                'provenance_source':'retained local runtime evidence', 'evidence_reference':'quality/local-report.json'})
            repo.save(profile)
            config = configuration_digest(profile)
        terms = tmp_path / 'terms.txt'
        terms.write_text('Synthetic local terms for automated permission tests only.', encoding='utf-8')
        identity = ExecutionUseIdentity(**{k:getattr(profile,k) for k in ('capability','mode','provider','model','runtime','machine_id')}, model_artifact_sha256='a'*64)
        report = ProviderUseLicenseReview(evidence_class='operator_verified', project_id=project,
            purpose='internal_evaluation', capability_profile_id=profile.id, configuration_sha256=config,
            identity=identity, intended_activity='Synthetic internal workflow verification', license_source='local:test-terms',
            license_version='test-v1', license_terms_reference='terms.txt', license_terms_sha256=hashlib.sha256(terms.read_bytes()).hexdigest(),
            permits_intended_activity=True, permitted_operations=['generation','review','download'], findings=['Synthetic applicability review.'])
        evidence = tmp_path / 'review.json'
        evidence.write_text(report.model_dump_json(), encoding='utf-8')
        payload = {'idempotency_key':'adopt-one', 'review_reference':'review.json','review_sha256':hashlib.sha256(evidence.read_bytes()).hexdigest()}
        yield client, path, UUID(project), profile, identity, report, payload


def headers():
    return {'x-content-os-execution-admission-key':OPERATOR_KEY}


def adopt(case):
    client, _, project, _, _, _, payload = case
    response = client.post(f'/projects/{project}/provider-use-admissions', json=payload, headers=headers())
    assert response.status_code == 201, response.text
    return response.json()


def resolve(case, receipt, **changes):
    client, _, project, profile, identity, _, _ = case
    request = {'purpose':'internal_evaluation','capability_profile_id':str(profile.id), 'identity':identity.model_dump(mode='json'), 'admission_id':receipt['id']}
    request.update(changes)
    return client.post(f'/projects/{project}/provider-use-resolution', json=request)


def test_receipt_roundtrip_restart_reads_are_pure_and_dispatch_stays_closed(setup):
    client, path, project, profile, _, _, payload = setup
    receipt = adopt(setup)
    assert client.post(f'/projects/{project}/provider-use-admissions', json=payload, headers=headers()).json() == receipt
    decision = resolve(setup, receipt).json()
    assert decision['state'] == 'allowed_for_scope'
    assert decision['dispatch_authorized'] is False and decision['integration_status'] == 'not_integrated'
    with Database(path) as db:
        before = db.connection.total_changes
        service = ProviderUseAdmissionService(db, path.parent)
        assert service.list(project)[0].id == UUID(receipt['id'])
        assert service.get(project, UUID(receipt['id'])).review_sha256 == payload['review_sha256']
        assert db.connection.total_changes == before
        assert ProviderMachineCapabilityProfileRepository(db).get(profile.id).commercial_status == 'non_commercial_only'
    with TestClient(create_app(path)) as other:
        assert other.get(f'/projects/{project}/provider-use-admissions/{receipt["id"]}').json() == receipt
        # Existing commercial lane does not use this new receipt.
        plan = other.post(f'/projects/{project}/production-preflight', json={}).json()
        assert 'provider_license_scope_unverified' in plan['stop_reasons']
        waiting = other.post(f'/projects/{project}/production-runs', json={'idempotency_key':'still-closed','expected_fingerprint':plan['fingerprint']}).json()
        with Database(path) as db:
            voice_id = db.connection.execute('SELECT id FROM voice_profiles').fetchone()[0]
        denied = other.post(f'/projects/{project}/production-runs/{waiting["id"]}/voice-dispatch', json={
            'voice_profile_id':voice_id,'capability_profile_id':str(profile.id),'authorization_reference':'internal-evaluation'})
        assert denied.status_code == 409
        with Database(path) as db:
            assert db.connection.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0
            assert ProviderCallRepository(db).list_for_project(project) == []


@pytest.mark.parametrize('sent_headers',[{}, {'x-content-os-talking-review-key':OPERATOR_KEY}, {'Authorization':'Bearer '+OPERATOR_KEY}, {'x-content-os-execution-admission-key':'short'}])
def test_general_access_or_source_review_role_cannot_adopt(setup, sent_headers):
    client, _, project, _, _, _, payload = setup
    response = client.post(f'/projects/{project}/provider-use-admissions', json=payload, headers=sent_headers)
    assert response.status_code == 403
    assert client.get(f'/projects/{project}/provider-use-admissions').json() == []


def test_body_actor_and_unknown_purpose_rejected(setup):
    client, _, project, profile, _, _, payload = setup
    assert client.post(f'/projects/{project}/provider-use-admissions', json={**payload,'actor_id':'operator'},headers=headers()).status_code == 422
    assert client.post(f'/projects/{project}/provider-use-resolution',json={'purpose':'anything','capability_profile_id':str(profile.id)}).status_code == 422


@pytest.mark.parametrize('reference',['../terms.txt','C:/terms.txt','/terms.txt','\\\\host\\terms.txt','missing.json'])
def test_evidence_boundary_and_safe_errors(setup, reference):
    client, _, project, _, _, _, payload = setup
    response = client.post(f'/projects/{project}/provider-use-admissions',json={**payload,'review_reference':reference},headers=headers())
    assert response.status_code == 409 and response.json()['detail'].startswith('provider_use_evidence_')
    assert reference not in response.text


@pytest.mark.parametrize('field,value',[('evidence_class','fixture'),('evidence_class','unknown'),('permits_intended_activity',False),('purpose','commercial_production'),('policy_version',2),('findings',[''])])
def test_review_schema_rejects_missing_authority_or_permission(setup, field, value):
    report = setup[5].model_dump(mode='json')
    report[field] = value
    with pytest.raises(ValidationError):
        ProviderUseLicenseReview.model_validate(report)


@pytest.mark.parametrize('change',['terms','review','settings','profile','weights','revoked','project','operation','missing_receipt'])
def test_current_evidence_identity_and_revocation_fail_closed(setup, change):
    client, path, project, profile, identity, _, payload = setup
    receipt = adopt(setup)
    changes = {}
    if change in ('terms','review'):
        (path.parent / ('terms.txt' if change == 'terms' else 'review.json')).write_text('changed', encoding='utf-8')
    if change == 'settings':
        with Database(path) as db:
            ProviderMachineSettingRepository(db).save(ProviderMachineSetting(capability=profile.capability,mode=profile.mode,
                provider=profile.provider,model=profile.model,runtime=profile.runtime,machine_id=profile.machine_id,
                values={'speed':2}, updated_at=datetime.now(timezone.utc)))
    if change == 'profile':
        with Database(path) as db:
            ProviderMachineCapabilityProfileRepository(db).save(profile.model_copy(update={'quality_status':'unknown'}))
    if change == 'weights':
        changes['identity'] = identity.model_copy(update={'model_artifact_sha256':'b'*64}).model_dump(mode='json')
    if change == 'revoked':
        response = client.post(f'/projects/{project}/provider-use-admissions/{receipt["id"]}/revoke',json={'reason':'Reconsidered terms'},headers=headers())
        assert response.status_code == 200
        again = client.post(f'/projects/{project}/provider-use-admissions',json=payload,headers=headers())
        assert again.status_code == 409 and again.json()['detail'] == 'provider_use_admission_revoked'
        assert client.post(f'/projects/{project}/provider-use-admissions/{receipt["id"]}/revoke',json={'reason':'Different'},headers=headers()).json() == response.json()
    if change == 'project':
        with TestClient(create_app(path)) as secondary:
            other, _, _ = _ready_fixture(secondary,path,with_master=False)
        assert client.get(f'/projects/{other}/provider-use-admissions/{receipt["id"]}').status_code == 404
        changed = client.post(f'/projects/{other}/provider-use-resolution',json={'purpose':'internal_evaluation','capability_profile_id':str(profile.id),'admission_id':receipt['id'],'identity':identity.model_dump(mode='json')})
        assert changed.json()['state'] == 'blocked'
        return
    if change == 'operation': changes['operation'] = 'derivation'
    if change == 'missing_receipt': changes['admission_id'] = str(uuid4())
    assert resolve(setup, receipt, **changes).json()['state'] == 'blocked'


def test_commercial_default_and_dependency_scope_never_upgraded(setup):
    client, path, project, profile, _, _, _ = setup
    receipt = adopt(setup)
    request = {'capability_profile_id':str(profile.id)}
    assert client.post(f'/projects/{project}/provider-use-resolution',json=request).json()['state'] == 'blocked'
    with Database(path) as db:
        ProviderMachineCapabilityProfileRepository(db).save(profile.model_copy(update={'commercial_status':'commercial_safe','license_evidence_reference':'operator:commercial-rights'}))
    assert client.post(f'/projects/{project}/provider-use-resolution',json=request).json()['state'] == 'allowed_for_scope'
    for extra in ({'dependency_purposes':['internal_evaluation']},{'admission_id':receipt['id']},{'admission_id':str(uuid4())}):
        assert client.post(f'/projects/{project}/provider-use-resolution',json={**request,**extra}).json()['state'] == 'blocked'


def test_idempotency_conflict_and_role_configuration(setup, monkeypatch):
    client, path, project, _, _, report, payload = setup
    adopt(setup)
    changed = report.model_copy(update={'intended_activity':'Another activity'})
    raw = changed.model_dump_json().encode()
    (path.parent/'other.json').write_bytes(raw)
    response = client.post(f'/projects/{project}/provider-use-admissions',json={**payload,'review_reference':'other.json','review_sha256':hashlib.sha256(raw).hexdigest()},headers=headers())
    assert response.status_code == 409 and response.json()['detail'] == 'provider_use_idempotency_conflict'
    monkeypatch.delenv('CONTENT_OS_EXECUTION_ADMISSION_KEY_HASHES')
    monkeypatch.setenv('CONTENT_OS_TALKING_REVIEW_KEY_HASHES',json.dumps({'operator':hashlib.sha256(OPERATOR_KEY.encode()).hexdigest()}))
    assert configured_admission_actor(OPERATOR_KEY) is None
    monkeypatch.setenv('CONTENT_OS_EXECUTION_ADMISSION_KEY_HASHES','invalid')
    assert configured_admission_actor(OPERATOR_KEY) is None


def test_migration_is_additive_and_repeatable_without_rewriting_existing_payloads(tmp_path, monkeypatch):
    from app.db import migrations
    path = tmp_path / 'old.sqlite3'
    with monkeypatch.context() as patch:
        patch.setattr(migrations, '_MIGRATIONS', tuple((v, statements) for v, statements in migrations._MIGRATIONS if v <= 42))
        with TestClient(create_app(path)) as client:
            _ready_fixture(client, path, with_master=False)
        with Database(path) as db:
            originals = {table:list(map(tuple,db.connection.execute(f'SELECT payload FROM {table}'))) for table in ('projects','project_drafts','audio_assets')}
            assert db.connection.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == 42
    for _ in range(2):
        with Database(path) as db:
            assert db.connection.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == migrations.CURRENT_SCHEMA_VERSION
            assert db.connection.execute('SELECT COUNT(*) FROM provider_use_admissions').fetchone()[0] == 0
            for table, rows in originals.items():
                assert list(map(tuple,db.connection.execute(f'SELECT payload FROM {table}'))) == rows


def test_operator_configuration_fails_closed_for_duplicate_or_invalid_digests(monkeypatch):
    digest = hashlib.sha256(OPERATOR_KEY.encode()).hexdigest()
    for value in ({'a':digest,'b':digest},{'bad actor':digest},{'a':'wrong'},[],{}):
        monkeypatch.setenv('CONTENT_OS_EXECUTION_ADMISSION_KEY_HASHES',json.dumps(value))
        assert configured_admission_actor(OPERATOR_KEY) is None


def test_symlink_outside_data_root_is_rejected(setup, tmp_path):
    from app.execution_admission import UseAdmissionError
    _, path, _, _, _, _, _ = setup
    target = tmp_path.parent / (tmp_path.name+'-outside.txt')
    target.write_text('outside',encoding='utf-8')
    link = tmp_path/'linked.txt'
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip('Windows symlink creation unavailable for this test user')
    with Database(path) as db, pytest.raises(UseAdmissionError,match='provider_use_evidence_unavailable'):
        ProviderUseAdmissionService(db,tmp_path)._file('linked.txt')


@pytest.mark.parametrize('case',['invalid_report','wrong_project','fixture_capability','missing_terms'])
def test_adoption_does_not_trust_a_report_label_or_arbitrary_reference(setup, case):
    client, path, project, profile, _, report, payload = setup
    if case == 'fixture_capability':
        with Database(path) as db:
            profile = profile.model_copy(update={'provenance_source':'fixture', 'evidence_reference':'fixture:capability'})
            ProviderMachineCapabilityProfileRepository(db).save(profile)
            report = report.model_copy(update={'configuration_sha256':configuration_digest(profile)})
    if case == 'wrong_project': report = report.model_copy(update={'project_id':uuid4()})
    if case == 'missing_terms': report = report.model_copy(update={'license_terms_reference':'absent.txt'})
    raw = b'{"private-content": "must-not-leak"}' if case == 'invalid_report' else report.model_dump_json().encode()
    (path.parent/'review.json').write_bytes(raw)
    response = client.post(f'/projects/{project}/provider-use-admissions',json={**payload,'review_sha256':hashlib.sha256(raw).hexdigest()},headers=headers())
    assert response.status_code == 409
    assert 'must-not-leak' not in response.text
    assert client.get(f'/projects/{project}/provider-use-admissions').json() == []


def test_budget_changes_do_not_supply_license_and_missing_quality_stays_unknown(setup):
    client, path, project, profile, identity, report, payload = setup
    receipt = adopt(setup)
    assert client.put(f'/projects/{project}/budget',json={'currency':'USD','max_calls':100,'allow_unknown_cost':True}).status_code == 200
    assert resolve(setup, receipt).json()['state'] == 'allowed_for_scope'
    assert resolve(setup, receipt, purpose='commercial_production').json()['state'] == 'blocked'
    # Permission evidence can be adopted separately from quality evidence.
    with Database(path) as db:
        profile = profile.model_copy(update={'quality_status':'unknown'})
        ProviderMachineCapabilityProfileRepository(db).save(profile)
        report = report.model_copy(update={'configuration_sha256':configuration_digest(profile)})
    raw = report.model_dump_json().encode()
    (path.parent/'unknown-quality.json').write_bytes(raw)
    created = client.post(f'/projects/{project}/provider-use-admissions',json={**payload,
        'idempotency_key':'unknown-quality','review_reference':'unknown-quality.json','review_sha256':hashlib.sha256(raw).hexdigest()},headers=headers())
    assert created.status_code == 201
    decision = resolve(setup, created.json()).json()
    assert decision['state'] == 'blocked' and 'exact_capability_not_verified' in decision['reasons']


def test_prefixed_portable_paths_ignore_process_cwd(setup, monkeypatch, tmp_path):
    client, path, project, _, _, _, payload = setup
    elsewhere = tmp_path/'elsewhere'
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    response = client.post(f'/projects/{project}/provider-use-admissions',json={**payload,
        'review_reference':f'{path.parent.name}/review.json'},headers=headers())
    assert response.status_code == 201, response.text
