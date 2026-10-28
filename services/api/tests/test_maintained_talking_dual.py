"""Synthetic installations only; fake process/probe, no torch/vendor execution."""
import ast
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import subprocess
import time
import pytest
from pydantic import TypeAdapter, ValidationError
from app.domain.models import ExecutionSpecificationRecord, ExecutionSpecificationV10, ExecutionSpecificationV9
from app.providers import maintained_talking_dual as dual
from app.providers.prepared import ExecutionPreparationError
from test_maintained_talking_execution import talking_case, candidate
from test_maintained_runtime import reference


@pytest.fixture
def dual_case(talking_case,monkeypatch):
    return make_dual_case(talking_case,monkeypatch)


def make_dual_case(talking_case,monkeypatch):
    c=talking_case;o=dict(c.options);root=c.root
    o.update(engine=root/"engine",invocation_policy="maintained-latentsync-dual-runtime-v1")
    o["parameters"]={**o["parameters"],"attention_policy":"latentsync-retained-explicit-math-v1","allocator":"expandable_segments:True"}
    for p in (o["base"]/"Lib",o["base"]/"DLLs",o["engine"]/"Lib/site-packages",o["engine"]/"DLLs",o["engine"]/"Library/bin",o["engine"]/"conda-meta"):
        p.mkdir(parents=True,exist_ok=True)
    (o["engine"]/"python.exe").write_bytes(b"synthetic engine")
    (o["engine"]/"python310.dll").write_bytes(b"synthetic DLL")
    (o["engine"]/"conda-meta/python.json").write_text(json.dumps(dict(name="python",version="3.10.13",build="fixture")))
    (o["engine"]/"Lib/site-packages/hook.pth").write_text("import definitely_not_executed\n")
    host=__import__('app.providers.maintained_talking_execution',fromlist=['observe_host']).observe_host()
    monkeypatch.setattr(dual,"observe_host",lambda *_a,**_kw:host)
    app=dual.observe_application_inventory(o["environment"],o["base"],max_bytes=o["max_bytes"])
    engine=dual.observe_engine_inventory(o["engine"],max_bytes=o["max_bytes"])
    evidence=reference(root/"source.txt",root).model_dump(mode="json")
    dist=dual.distributions(o)
    components=[]
    for role,inv in (("application",app),("engine",engine)):
        components.append(dict(role=role,inventory_sha256=inv.descriptor.sha256,
            startup_hooks=[r.name for r in inv.files if r.name.endswith(".pth")],
            distributions=[dict(root=k[0],kind=k[1],name=k[2],version=v[0],build=v[1],publisher="synthetic-only",source=evidence)
                for k,v in dist.items() if (k[0]=="engine")==(role=="engine")]))
    origin=dict(origin_version=5,trust_policy="maintained-local-process-v1",evidence_class="operator_verified",actor=c.origin["actor"],
        generation_scope="evaluation_talking_run_only",accepts_local_operator_vendor_os_trust=True,no_concurrent_runtime_updates=True,
        accepts_talking_runtime_trust=True,network_isolation="not_asserted",components=components)
    path=root/"dual-origin.json";path.write_text(json.dumps(origin));o["origin_reference"]=reference(path,root)
    from app.runtime_inventory import RuntimeInventoryStore
    inventories={app.descriptor.sha256:app,engine.descriptor.sha256:engine}
    original_read=RuntimeInventoryStore.read_v2
    monkeypatch.setattr(RuntimeInventoryStore,"read_v2",lambda _self,descriptor,**kwargs:
        inventories[descriptor.sha256] if descriptor.sha256 in inventories else original_read(_self,descriptor,**kwargs))
    return SimpleNamespace(options=o,root=root,request=c.request,origin=origin,origin_path=path,app=app,engine=engine)


def prepare(c):
    approved=dual.observe_dual(c.options)
    return dual.prepare_dual_talking_child(request=c.request,approved_spec=approved,**c.options)


def test_distinct_schema_complete_roles_and_retained_origin(dual_case,monkeypatch):
    c=dual_case
    monkeypatch.setattr(dual.subprocess,"Popen",lambda *_a,**_kw:pytest.fail("preparation launches nothing"))
    with prepare(c) as child:
        spec=TypeAdapter(ExecutionSpecificationRecord).validate_json(child.specification.model_dump_json())
        assert type(spec) is ExecutionSpecificationV10
        with pytest.raises(ValidationError):ExecutionSpecificationV9.model_validate_json(spec.model_dump_json())
        assert spec.startup.engine_root==str(c.options["engine"])
        assert next(r for r in c.engine.files if r.role=="interpreter").name=="engine/python.exe"
        assert len([r for r in c.app.files if r.role=="interpreter"])==1
        dual.require_dual_origin(c.root,c.options["origin_reference"],c.app,c.engine,current=False)
        with pytest.raises(ExecutionPreparationError):dual.require_dual_origin(c.root,c.options["origin_reference"],c.engine,c.app)
        bad=spec.model_dump(mode="json");bad["schema_version"]=True
        with pytest.raises(ValidationError):ExecutionSpecificationV10.model_validate_json(json.dumps(bad))


def test_prepared_child_reuses_admitted_spec_without_full_observation(dual_case,monkeypatch):
    with prepare(dual_case) as child:
        spec=child.specification
        monkeypatch.setattr(dual,"observe_dual",lambda *_a,**_kw:pytest.fail("normal execution must not rescan the runtime"))
        assert child.verify()==spec
        assert child.execution_use_snapshot()["execution_sha256"]==spec.execution_sha256


@pytest.mark.parametrize("mutation",["engine","conda","application","bootstrap","source","request","pth","distribution","compatibility","parameters"])
def test_dual_drift_prevents_launch(dual_case,monkeypatch,mutation):
    c=dual_case
    with prepare(c) as child:
        if mutation=="engine":p=c.options["engine"]/"python.exe"
        elif mutation=="conda":p=c.options["engine"]/"conda-meta/python.json"
        elif mutation=="application":p=c.options["environment"]/"Scripts/python.exe"
        elif mutation=="bootstrap":p=Path(child._temporary.name)/"engine.pyz"
        elif mutation=="source":p=Path(c.request.reference_path)
        elif mutation=="request":p=Path(child._temporary.name)/"request.json"
        elif mutation=="pth":p=c.options["engine"]/"python310._pth"
        elif mutation=="distribution":p=c.root/"source.txt"
        elif mutation=="compatibility":
            monkeypatch.setattr(dual,"engine_bundle",lambda:b"different owned source")
            p=None
        else:
            c.options["parameters"]["seed"]=100;p=None
        if p:p.write_bytes((p.read_bytes() if p.exists() else b"")+b"changed")
        monkeypatch.setattr(dual.subprocess,"Popen",lambda *_a,**_kw:pytest.fail("drift cannot launch"))
        with pytest.raises(ExecutionPreparationError,match="identity_changed"):child.execute()


def test_once_owned_app_argv_environment_and_quarantine(dual_case,monkeypatch):
    c=dual_case;calls=[];monkeypatch.setattr(dual,"_system_root",lambda:c.root)
    with prepare(c) as child:
        def launch(argv,**kw):
            calls.append((argv,kw));(Path(kw["cwd"])/"output.mp4").write_bytes(b"synthetic output")
            return SimpleNamespace(wait=lambda **_kw:0,poll=lambda:0)
        monkeypatch.setattr(dual.subprocess,"Popen",launch)
        assert child.execute().read_bytes()==b"synthetic output"
        with pytest.raises(ExecutionPreparationError,match="consumed"):child.execute()
    assert len(calls)==1 and calls[0][0][1:4]==["-I","-S","-B"] and calls[0][1]["shell"] is False
    assert "PYTHONPATH" not in calls[0][1]["env"] and "CONTENT_OS_MAINTAINED_TALKING_OPERATOR_KEY" not in calls[0][1]["env"]
    with prepare(c) as failed:
        path=Path(failed._temporary.name)
        stopped=[]
        monkeypatch.setattr(dual.subprocess,"Popen",lambda *_a,**_kw:SimpleNamespace(wait=lambda **_kw:(_ for _ in ()).throw(subprocess.TimeoutExpired('fake',1)),poll=lambda:None))
        monkeypatch.setattr(dual,"stop_maintained_process_tree",lambda p:stopped.append(p))
        with pytest.raises(ExecutionPreparationError,match="timeout"):failed.execute()
    assert path.exists() and stopped
    assert json.loads((path/'quarantine.json').read_text())['reconciliation_required'] is True


def test_private_vendor_resources_and_safe_shell_operands(dual_case,monkeypatch):
    c=dual_case;private=c.root/"space & parent";private.mkdir();(private/"engine.pyz").write_bytes(dual.engine_bundle())
    source=private/"source & title.mp4";source.write_bytes(b"video")
    audio=private/"audio title.wav";audio.write_bytes(b"audio")
    spec=dual.observe_dual(c.options)
    options={**c.options,"model_artifacts":[r.model_dump(mode='json') for r in spec.model_artifacts],
        "resource_copies":[r.model_dump(mode='json') for r in spec.resource_copies]}
    owner=dual.OwnedDualLatentSync(options,private,time.monotonic()+20)
    monkeypatch.setattr(dual,"_system_root",lambda:c.root)
    calls=[]
    def process(argv,cwd,env):
        calls.append((argv,cwd,env));(cwd/"temp").mkdir();(cwd/"generated.mp4").write_bytes(b"result")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(owner,"process",process)
    target=private/"result.mp4";owner.inference(source,audio,target)
    assert target.read_bytes()==b"result"
    record=json.loads((private/"engine-request.json").read_text())
    assert record["work"]==str(private/"work") and not (c.options["vendor"]/"temp").exists()
    assert (private/"work/reference.mp4").read_bytes()==b"video"
    assert (private/"work/narration-model.wav").read_bytes()==b"audio"
    assert calls[0][0][0]==str(c.options["engine"]/"python.exe") and calls[0][0][1:4]==["-I","-S","-B"]
    assert calls[0][2]["PYTORCH_CUDA_ALLOC_CONF"]=="expandable_segments:True"


def test_retained_function_ast_and_bootstrap_never_imports_app():
    source=Path(dual.__file__).with_name('talking_attention_compatibility.py').read_text()
    function=lambda s:next(n for n in ast.parse(s).body if isinstance(n,ast.FunctionDef) and n.name=='explicit_scaled_dot_product_attention')
    # Frozen AST of the retained operator function, independent of local assets.
    assert hashlib.sha256(ast.dump(function(source),include_attributes=False).encode()).hexdigest()=='996014f29c2e3799158d5cc57e2e545f2e2a4723f30570c0876a3531b2a17c16'
    boot=Path(dual.__file__).with_name('talking_engine_bootstrap.py').read_text()
    ast.parse(boot,feature_version=(3,10))
    assert 'from app' not in boot and '"--video_path", "reference.mp4"' in boot and '"--video_out_path", "generated.mp4"' in boot


@pytest.mark.parametrize("mutation",[None,"native_case","version","flags","executable","foreign_root","reordered","duplicate","malformed","site","env","cwd","record"])
def test_engine_startup_checks_before_vendor_import(dual_case,monkeypatch,mutation):
    import importlib.util
    c=dual_case;private=c.root/"startup & parent";private.mkdir();work=private/"work";work.mkdir()
    archive=private/"engine.pyz";archive.write_bytes(dual.engine_bundle())
    for name in ("reference.mp4","narration-model.wav","configs/unet/stage2.yaml","configs/scheduler_config.json","checkpoints/whisper/tiny.pt","latentsync/utils/mask.png"):
        p=work/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b"synthetic resource")
    o=c.options
    record=dict(policy="latentsync-dual-no-site-v1",engine=str(o["engine"]),vendor=str(o["vendor"]),work=str(work),hf=str(o["hf"]),
        torch=str(o["torch"]),tools=str(o["tools"]),checkpoint=str(o["checkpoint"]),steps=20,guidance=1.5,seed=7,
        attention=o["parameters"]["attention_policy"],allocator=o["parameters"]["allocator"])
    request=private/"engine-request.json";request.write_text(json.dumps(record))
    source=Path(dual.__file__).with_name('talking_engine_bootstrap.py')
    spec=importlib.util.spec_from_file_location('synthetic_engine_bootstrap',source)
    boot=importlib.util.module_from_spec(spec);spec.loader.exec_module(boot)
    expected=[str(archive),*(str(o["engine"]/n) for n in ('python310.zip','DLLs','Lib',''))]
    flags=SimpleNamespace(**{k:1 for k in ('isolated','no_site','ignore_environment','no_user_site','dont_write_bytecode')})
    fake=SimpleNamespace(version_info=(3,10,13),argv=[str(archive),str(request)],flags=flags,modules={},executable=str(o['engine']/'python.exe'),path=list(expected))
    monkeypatch.setattr(boot,'sys',fake);monkeypatch.chdir(work)
    for k,v in {'HF_HOME':str(o['hf']),'TORCH_HOME':str(o['torch']),'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1',
                'PYTORCH_CUDA_ALLOC_CONF':o['parameters']['allocator'],'PYTHONDONTWRITEBYTECODE':'1'}.items():monkeypatch.setenv(k,v)
    if mutation=='native_case':
        import os
        if os.name!='nt':pytest.skip('Windows native case semantics')
        fake.path=[str(p).replace('Lib','lib') for p in expected]
    elif mutation=='version':fake.version_info=(3,12,0)
    elif mutation=='flags':fake.flags.no_site=0
    elif mutation=='executable':fake.executable=str(o['environment']/'Scripts/python.exe')
    elif mutation=='foreign_root':fake.path=[*expected,'foreign']
    elif mutation=='reordered':fake.path=[expected[0],expected[2],expected[1],*expected[3:]]
    elif mutation=='duplicate':fake.path=[*expected,expected[-1]]
    elif mutation=='malformed':fake.path=[None,*expected[1:]]
    elif mutation=='site':fake.modules={'site':object()}
    elif mutation=='env':monkeypatch.setenv('HF_HOME','foreign')
    elif mutation=='cwd':monkeypatch.chdir(private)
    elif mutation=='record':record['attention']='stock';request.write_text(json.dumps(record))
    if mutation not in (None,'native_case'):
        with pytest.raises(ValueError,match='maintained_talking_startup'):boot.configure()
    else:
        assert boot.configure()==record
        assert fake.path==[*expected,str(o['engine']/'Lib/site-packages'),str(o['vendor'])]


@pytest.mark.parametrize('mutation',['role','foreign_origin','limits','conda_conflict','missing_local'])
def test_dual_invalid_components_and_resources(dual_case,mutation):
    c=dual_case
    if mutation=='limits':
        spec=dual.observe_dual(c.options).model_dump(mode='json')
        spec['engine_inventory']['file_count']=100000
        with pytest.raises(ValidationError,match='dual_inventory_limit'):ExecutionSpecificationV10.model_validate_json(json.dumps(spec))
        return
    if mutation=='foreign_origin':c.origin['origin_version']=4
    elif mutation=='role':c.origin['components'][0]['role']='engine'
    elif mutation=='conda_conflict':
        p=c.options['engine']/'conda-meta/duplicate.json';p.write_text(json.dumps(dict(name='python',version='3.10.13',build='different')))
    else:(c.options['vendor']/'checkpoints/whisper/tiny.pt').unlink()
    c.origin_path.write_text(json.dumps(c.origin));c.options['origin_reference']=reference(c.origin_path,c.root)
    with pytest.raises(ExecutionPreparationError):prepare(c)


@pytest.mark.parametrize('stop_fails',[False,True])
def test_nested_deadline_and_stop_failure_propagate(dual_case,monkeypatch,stop_fails):
    c=dual_case;private=c.root/'process-test';private.mkdir()
    owner=dual.OwnedDualLatentSync(c.options,private,time.monotonic()+10)
    calls=[];stops=[]
    def launch(argv,**kw):
        calls.append((argv,kw))
        return SimpleNamespace(wait=lambda **_kw:(_ for _ in ()).throw(subprocess.TimeoutExpired('fake',1)),poll=lambda:None)
    def stop(child):
        stops.append(child)
        if stop_fails:raise ExecutionPreparationError('maintained_process_tree_stop_unverified')
    monkeypatch.setattr(dual.subprocess,'Popen',launch);monkeypatch.setattr(dual,'stop_maintained_process_tree',stop)
    expected=ExecutionPreparationError if stop_fails else subprocess.TimeoutExpired
    with pytest.raises(expected):owner.process(['synthetic-absolute-executable'],private,{'PATH':'explicit-only'})
    assert len(calls)==len(stops)==1 and calls[0][1]['shell'] is False
    owner.deadline=time.monotonic()-1
    with pytest.raises(ExecutionPreparationError,match='timeout'):owner.remaining()


@pytest.mark.parametrize('mutation',[None,'pip','conda','missing_origin'])
def test_manager_namespaces_preserve_both_versions_and_current_identity(talking_case,monkeypatch,mutation):
    engine=talking_case.root/'engine'
    conda=engine/'conda-meta/tzdata.json'
    pip=engine/'Lib/site-packages/tzdata-2026.4.dist-info/METADATA'
    conda.parent.mkdir(parents=True);pip.parent.mkdir(parents=True)
    conda.write_text(json.dumps(dict(name='tzdata',version='2026c',build='fixture')))
    pip.write_text('Name: tzdata\nVersion: 2026.4\n')
    c=make_dual_case(talking_case,monkeypatch)
    observed=dual.distributions(c.options)
    assert observed[('engine','conda','tzdata')]==('2026c','fixture')
    assert observed[('engine','pip','tzdata')]==('2026.4','')
    monkeypatch.setattr(dual.subprocess,'Popen',lambda *_a,**_kw:pytest.fail('metadata never launches a runtime'))
    if mutation=='missing_origin':
        component=next(r for r in c.origin['components'] if r['role']=='engine')
        component['distributions']=[r for r in component['distributions'] if r['kind']!='pip']
        c.origin_path.write_text(json.dumps(c.origin));c.options['origin_reference']=reference(c.origin_path,c.root)
        with pytest.raises(ExecutionPreparationError,match='distribution_origin_mismatch'):prepare(c)
        return
    with prepare(c) as child:
        assert child.verify()==child.specification
        if mutation:
            target=pip if mutation=='pip' else conda
            target.write_bytes(target.read_bytes()+b'changed')
            with pytest.raises(ExecutionPreparationError,match='identity_changed'):child.verify()
