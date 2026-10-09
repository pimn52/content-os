"""Real temporary file staging, synthetic host/machine only; no native load."""
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.domain.models import ExecutionMachineObservation
from app.providers import omnivoice_prepared as subject
from app.providers.component_probe import PreparedComponentHost
from app.providers.omnivoice_local import OmniVoiceLocalParameters
from app.providers.prepared import ExecutionPreparationError, NativeExecutionUnsupported
from app.providers.runtime_tree import RuntimeTreeSelection, RuntimeFileSelection, prepare_runtime_tree
from app.providers.voice import OmniVoiceProvider
from app.providers.windows_component_policy import owned_component_policy_bytes
from app.providers.windows_os_policy import owned_os_policy_bytes
from test_component_policy2 import policy2
from test_execution_specification_v3 import raw_spec, parse
from test_execution_admission import setup


@pytest.fixture
def ready(tmp_path, raw_spec, monkeypatch):
    model = tmp_path / 'model-source'
    model.mkdir()
    from test_omnivoice_local import model_tree
    # Call fixture's pure synthetic layout builder on a separate source root.
    model_tree.__wrapped__(model)
    (model / 'LICENSE').write_bytes(b'synthetic metadata, not a use license')
    runtime_source = tmp_path / 'runtime-source'
    providers = runtime_source / 'Lib/site-packages/app/providers'
    providers.mkdir(parents=True)
    (providers / 'windows_component_policy.json').write_bytes(owned_component_policy_bytes())
    (providers / 'windows_os_policy.json').write_bytes(owned_os_policy_bytes())
    (runtime_source / 'python.exe').write_bytes(b'synthetic interpreter')
    (runtime_source / 'entry.py').write_bytes(b'synthetic entry')
    staging = tmp_path / 'staging'
    staging.mkdir()
    runtime = prepare_runtime_tree(trees=(RuntimeTreeSelection(runtime_source / 'Lib','Lib'),),
        files=(RuntimeFileSelection(runtime_source / 'python.exe','python.exe','interpreter'),
            RuntimeFileSelection(runtime_source / 'entry.py','entry.py','entrypoint')),
        staging_parent=staging,max_bytes=100_000)
    host = PreparedComponentHost.__new__(PreparedComponentHost)
    host.tree = runtime
    observations = [parse(raw_spec).host_runtime]
    machine = [ExecutionMachineObservation(installation_id=uuid4(),host_sha256='a'*64,device_sha256='b'*64)]
    monkeypatch.setattr(PreparedComponentHost,'verify',lambda self: observations[0])
    monkeypatch.setattr(subject,'observe_windows_cpu',lambda **_kw: machine[0])
    def prepare(**updates):
        kwargs = dict(model_root=model,parameters=OmniVoiceLocalParameters(),runtime=runtime,
            host=host,data_root=tmp_path,staging_parent=staging,max_model_bytes=10_000,
            provider='omnivoice',model='selected-local-model',runtime_label='synthetic-runtime',machine_id='test')
        kwargs.update(updates)
        return subject.prepare_omnivoice_identity(**kwargs)
    yield SimpleNamespace(model=model,runtime=runtime,host=host,staging=staging,prepare=prepare,
        observations=observations,machine=machine,data_root=tmp_path)
    runtime.close()


def test_actual_copies_full_identity_detachment_cleanup_without_native_import(ready):
    c = ready
    before = set(sys.modules)
    with c.prepare() as prepared:
        root = prepared.model_root
        assert root != c.model and (root/'LICENSE').read_bytes() == (c.model/'LICENSE').read_bytes()
        snapshot = prepared.execution_use_snapshot()
        assert snapshot['execution_specification']['schema_version'] == 3
        assert len(snapshot['execution_specification']['model_artifacts']) == 7
        assert snapshot['execution_specification']['runtime_inventory'] == c.runtime.inventory.descriptor.model_dump(mode='json')
        detached = prepared.specification
        detached.parameters['seed'] = 123
        assert prepared.specification.parameters['seed'] == 0
        (c.model/'model.safetensors').write_bytes(b'original changed after copy')
        assert prepared.execution_use_snapshot()['execution_sha256'] == snapshot['execution_sha256']
        assert not hasattr(prepared,'execute')
        with pytest.raises(NativeExecutionUnsupported,match='dependency_closure'):
            prepared.require_native_preparation()
    assert not root.exists() and c.runtime.verify()
    assert not any(n.split('.')[0] in ('torch','transformers','omnivoice') for n in set(sys.modules)-before)


@pytest.mark.parametrize('change',['weights','metadata','extra','removed','runtime','host','machine','ownership'])
def test_changes_consume_identity_without_launch(ready,change):
    c = ready
    with c.prepare() as prepared:
        if change == 'weights': (prepared.model_root/'model.safetensors').write_bytes(b'changed')
        elif change == 'metadata': (prepared.model_root/'LICENSE').write_bytes(b'changed')
        elif change == 'extra': (prepared.model_root/'extra.bin').write_bytes(b'new')
        elif change == 'removed': (prepared.model_root/'tokenizer.json').unlink()
        elif change == 'runtime': (c.runtime.root/'entry.py').write_bytes(b'changed')
        elif change == 'host': c.observations[0] = c.observations[0].model_copy(update={'revision':99999})
        elif change == 'machine': c.machine[0] = c.machine[0].model_copy(update={'host_sha256':'c'*64})
        else: c.host.tree = object()
        with pytest.raises(ExecutionPreparationError): prepared.execution_use_snapshot()
        with pytest.raises(ExecutionPreparationError,match='preparation_consumed'):
            prepared.execution_use_snapshot()


@pytest.mark.parametrize('problem',['size','cuda','fake_runtime','fake_host','inside_source'])
def test_invalid_preparation_creates_no_private_model(ready,problem):
    c = ready
    updates = {'size':{'max_model_bytes':1},'cuda':{'parameters':OmniVoiceLocalParameters(device='cuda:0')},
        'fake_runtime':{'runtime':SimpleNamespace()},'fake_host':{'host':SimpleNamespace(tree=c.runtime)},
        'inside_source':{'staging_parent':c.model}}[problem]
    with pytest.raises(ExecutionPreparationError): c.prepare(**updates)
    assert list(c.staging.glob('content-os-model-*')) == []


def test_source_change_during_copy_stops_and_cleans(ready,monkeypatch):
    original = subject._manifest
    count = []
    def changing(root,parameters,limit):
        result = original(root,parameters,limit)
        count.append(root)
        if root != ready.model: (ready.model/'model.safetensors').write_bytes(b'different original')
        return result
    monkeypatch.setattr(subject,'_manifest',changing)
    with pytest.raises(ExecutionPreparationError,match='model_source_changed'): ready.prepare()
    assert list(ready.staging.glob('content-os-model-*')) == []


def test_guarded_cleanup_refuses_changed_root_and_detaches_finalizer(ready,monkeypatch):
    prepared = ready.prepare()
    original = subject._exact_path
    root = prepared.model_root
    def reject(path,**kwargs):
        if path == root: raise ExecutionPreparationError('synthetic redirected root')
        return original(path,**kwargs)
    monkeypatch.setattr(subject,'_exact_path',reject)
    with pytest.raises(ExecutionPreparationError,match='execution_model_cleanup_failed'):
        prepared.close()
    assert root.exists() and (ready.model/'model.safetensors').exists()
    assert not prepared._temporary._finalizer.alive
    # Restore and explicitly clean this exact owned temp root, not source.
    monkeypatch.setattr(subject,'_exact_path',original)
    subject._cleanup(prepared._temporary)


def test_owned_runtime_requires_both_policy_bytes_before_model_copy(ready):
    (ready.runtime.root/'Lib/site-packages/app/providers/windows_os_policy.json').write_bytes(b'changed')
    with pytest.raises(ExecutionPreparationError): ready.prepare()
    assert list(ready.staging.glob('content-os-model-*')) == []


def test_adapter_exposes_identity_only_and_legacy_native_stop_remains(ready):
    c = ready
    provider = OmniVoiceProvider(model=str(c.model))
    with provider.prepare_local_execution_identity(OmniVoiceLocalParameters(),runtime=c.runtime,
            host=c.host,data_root=c.data_root,staging_parent=c.staging,max_model_bytes=10_000,
            runtime_label='synthetic-runtime',machine_id='test') as prepared:
        assert prepared.specification.provider == provider.provider_name
        assert prepared.specification.model == provider.model
    with pytest.raises(NativeExecutionUnsupported,match='opaque_callable'):
        provider.prepare_execution(object())


@pytest.mark.parametrize('late_change',[False,True])
def test_actual_prepared_snapshot_adopts_freezes_and_reconciles_without_native_execution(ready,setup,late_change):
    from app.db import Database, JobRepository, ProviderCallRepository
    from app.execution_admission import UseAdmissionError
    from app.execution_scope import ExecutionScopeService, job_use_identity, require_evaluation_lane_closed
    from app.frozen_worker import freeze_worker_identity
    from app.provider_execution import ProviderExecutionService
    from app.runtime_inventory import RuntimeInventoryStore
    from test_execution_attestation import adopt_v2
    from test_execution_scope import _job
    from test_prepared_provider_execution import Cost
    _,path,project,profile,*_ = setup
    with ready.prepare(provider=profile.provider,model=profile.model,
            runtime_label=profile.runtime,machine_id=profile.machine_id) as prepared:
        RuntimeInventoryStore(path.parent).install(ready.runtime.inventory)
        receipt,_,_ = adopt_v2(setup,prepared.specification,key='actual-staged-identity')
        with Database(path) as db:
            service = ExecutionScopeService(db,path.parent)
            scope = service.prepare(project,purpose='internal_evaluation',admission_ids=(receipt,))
            job = JobRepository(db).create(_job(project))
            service.pin('job',job.id,scope,operation='generation',subject_sha256=job_use_identity(job))
            provider = SimpleNamespace(provider_name=profile.provider,model=profile.model)
            frozen = freeze_worker_identity(service,job,provider,prepared,
                capability_profile_id=profile.id,execution_admission_id=receipt)
            assert frozen.prepared is prepared
            cost = Cost()
            if late_change:
                cost.change = lambda: (prepared.model_root/'model.safetensors').write_bytes(b'changed')
            actions = []
            def action(retained):
                assert retained is prepared and not db.connection.in_transaction
                actions.append(retained)
                retained.require_native_preparation()
                pytest.fail('unclosed native recipe must never execute')
            with pytest.raises((UseAdmissionError,NativeExecutionUnsupported)):
                ProviderExecutionService(db,cost).execute_prepared_voice(frozen=frozen,
                    idempotency_key='actual-staged-owner',input_source='temporary-only',input_document={'text':'synthetic'},
                    action=action,encode_result=lambda x:x,decode_result=lambda x:x,error_code=lambda exc:str(exc))
            calls = ProviderCallRepository(db).list_for_project(project)
            assert len(calls) == 1 and calls[0].status == 'failed'
            assert len(actions) == (0 if late_change else 1)
            assert calls[0].error_code == ('execution_runtime_identity_unavailable' if late_change
                else 'execution_native_dependency_closure_unsupported')
            with pytest.raises(UseAdmissionError,match='evaluation_execution_not_integrated'):
                require_evaluation_lane_closed(db,job)
