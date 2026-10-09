"""Frozen CPU rules with synthetic files/metadata; no native/vendor imports."""
from dataclasses import replace
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.providers import cpu_native_recipe as cpu
from app.providers.pe_bounded import open_bounded_pe
from app.providers.pe_resources import ResourceClassification, classify_pe_resources
from app.providers.prepared import NativeExecutionUnsupported
from app.providers.windows_activation import ActivationMetadata, ActivationAssembly
from app.providers.windows_os_policy import owned_os_policy_bytes, parse_os_policy
from test_pe_resources import resource_image


def digest(raw): return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def policy(): return cpu._owned()[0]


def test_owned_pair_all_member_roles_and_policy_identity(policy):
    assert [p['version'] for p in policy['packages']] == ['2.5.1+cpu','2.5.1+cpu']
    assert len(policy['members']) == 23
    assert sum(x['manifest_sha256'] is not None for x in policy['members']) == 21
    plan = cpu.plan_cpu_facts(())
    assert len(plan.roots) == 15 and len(plan.excluded_members) == 9
    assert not plan.dispatch_authorized
    assert ('dynamic_closure','not_verified') in plan.pending
    assert plan.os_policy_sha256 == digest(owned_os_policy_bytes())


@pytest.fixture(params=[0,1,2])
def manifest(request, policy):
    form_id = sorted(policy['manifests'])[request.param]
    return form_id, policy['manifests'][form_id]


def synthetic_member(tmp_path, monkeypatch, policy, manifest, *, changes=None):
    form_id, form = manifest
    options = dict(ids=(24,2,1033), raw=bytes.fromhex(form['hex']), codepage=form['codepage'], exe=False)
    options.update(changes or {})
    raw = resource_image([(*options['ids'],options['raw'],options['codepage'])], exe=options['exe'])
    path = tmp_path / 'synthetic.bin'; path.write_bytes(raw)
    selected = copy.deepcopy(policy)
    member = next(x for x in selected['members'] if x['manifest_sha256'] == form_id)
    member.update(size_bytes=len(raw), sha256=digest(raw))
    # Only synthetic whole-member identity is changed, never manifest rules.
    monkeypatch.setattr(cpu, '_owned', lambda: (selected,'f'*64))
    return path, member


def classify(path, member):
    with open_bounded_pe(path,size_bytes=member['size_bytes'],sha256=member['sha256']) as view:
        return cpu.classify_cpu_resources(view,member_name=member['name'],size_bytes=member['size_bytes'],sha256=member['sha256'])


def test_exact_forms_get_pending_private_context_not_legacy_admission(tmp_path,monkeypatch,policy,manifest):
    path, member = synthetic_member(tmp_path,monkeypatch,policy,manifest)
    result = classify(path,member)
    assert result.kind == 'requires_private_root_context' and result.manifest_sha256 == manifest[0]
    with pytest.raises(NativeExecutionUnsupported,match='resources_unsupported'):
        classify_pe_resources(path.read_bytes())


@pytest.mark.parametrize('change',['raw','codepage','id','language','exe'])
def test_changed_resource_content_or_role_rejected(tmp_path,monkeypatch,policy,manifest,change):
    changes = {'raw':{'raw':bytes.fromhex(manifest[1]['hex'])+b' '},
        'codepage':{'codepage':42},'id':{'ids':(24,3,1033)},
        'language':{'ids':(24,2,1031)},'exe':{'exe':True}}[change]
    path,member=synthetic_member(tmp_path,monkeypatch,policy,manifest,changes=changes)
    with pytest.raises(NativeExecutionUnsupported): classify(path,member)


def test_claimed_member_hash_cannot_replace_actual_view_identity(tmp_path,monkeypatch,policy,manifest):
    path,member=synthetic_member(tmp_path,monkeypatch,policy,manifest)
    other=tmp_path/'different.bin'; other.write_bytes(resource_image())
    with open_bounded_pe(other,size_bytes=other.stat().st_size,sha256=digest(other.read_bytes())) as view:
        with pytest.raises(NativeExecutionUnsupported,match='identity_changed'):
            cpu.classify_cpu_resources(view,member_name=member['name'],size_bytes=member['size_bytes'],sha256=member['sha256'])


@pytest.fixture
def metadata(tmp_path,policy):
    source=tmp_path/'source.bin'; source.write_bytes(b'synthetic queried source')
    root=ActivationAssembly('',str(source),'','',2,1,(1,0),(0,0),0,(),17)
    return source, next(iter(policy['manifests'])), ActivationMetadata(1,str(source),'',str(source.parent),(2,1,2),(root,),0)


def test_private_prediction_keeps_opaque_flags_exact_roles_and_pending(metadata):
    source,manifest,raw=metadata
    p=cpu.compare_private_root_metadata(raw,source=source,source_sha256=digest(source.read_bytes()),manifest_sha256=manifest)
    assert p.root_flags==17 and not p.dispatch_authorized
    cpu.compare_private_root_association(p,raw)
    with pytest.raises(NativeExecutionUnsupported,match='changed'):
        cpu.compare_private_root_association(p,replace(raw,assemblies=(replace(raw.assemblies[0],flags=18),)))


def test_parent_separator_is_path_representation_but_raw_stability_remains(metadata):
    source,manifest,raw=metadata
    observed=replace(raw,application_directory=str(source.parent)+'/')
    p=cpu.compare_private_root_metadata(observed,source=source,
        source_sha256=digest(source.read_bytes()),manifest_sha256=manifest)
    assert p.metadata==observed
    with pytest.raises(NativeExecutionUnsupported,match='changed'):
        cpu.compare_private_root_association(p,raw)


@pytest.mark.parametrize('change',['roster','file','identity','policy','config','parent','source','flags','root_flags','paths','version'])
def test_private_context_extra_binding_or_identity_rejected(metadata,change):
    source,manifest,raw=metadata
    root=raw.assemblies[0]
    values={'roster':replace(raw,assemblies=(root,root)),
        'file':replace(raw,assemblies=(replace(root,files=('unexpected',)),)),
        'identity':replace(raw,assemblies=(replace(root,identity='vendor'),)),
        'policy':replace(raw,assemblies=(replace(root,policy='external'),)),
        'config':replace(raw,root_configuration='external'),
        'parent':replace(raw,application_directory='external'),
        'source':replace(raw,root_manifest='external'),
        'flags':replace(raw,flags=1),'root_flags':replace(raw,assemblies=(replace(root,flags=True),)),
        'paths':replace(raw,path_types=(1,0,1)),
        'version':replace(raw,assemblies=(replace(root,manifest_version=(2,0)),))}
    with pytest.raises(NativeExecutionUnsupported,match='context_unsupported'):
        cpu.compare_private_root_metadata(values[change],source=source,source_sha256=digest(source.read_bytes()),manifest_sha256=manifest)


@pytest.fixture
def environment(tmp_path):
    root=tmp_path/'runtime'; root.mkdir()
    work=tmp_path/'work'; work.mkdir()
    windows=tmp_path/'windows'; windows.mkdir()
    return cpu.cpu_environment(root=root,work_directory=work,windows_root=windows)


def test_empty_version_survives_serialization_and_prefix_checks(environment):
    encoded=json.loads(environment.canonical_bytes())
    assert encoded['environment']['TORIO_USE_FFMPEG_VERSION']==''
    assert encoded['environment']['TORCH_DEVICE_BACKEND_AUTOLOAD']=='0'
    cpu.compare_cpu_prefixes(environment,exec_prefix=str(environment.root),base_exec_prefix=str(environment.root),userbase=str(environment.expected_userbase))
    with pytest.raises(NativeExecutionUnsupported,match='prefix_observation'):
        cpu.compare_cpu_prefixes(environment,exec_prefix=str(environment.root),base_exec_prefix=str(environment.root),userbase='ambient')


@pytest.mark.parametrize('value',[None,'4','5','6','0'])
def test_unset_or_other_ffmpeg_version_breaks_environment(environment,value):
    values=tuple((k,v if k!='TORIO_USE_FFMPEG_VERSION' else value) for k,v in environment.environment)
    if value is None: values=tuple((k,v) for k,v in values if k!='TORIO_USE_FFMPEG_VERSION')
    with pytest.raises(NativeExecutionUnsupported,match='environment_invalid'):
        replace(environment,environment=values).verify()


def test_appearing_disabled_probe_root_stops(environment):
    environment.absent_prefix.mkdir()
    with pytest.raises(NativeExecutionUnsupported,match='probe_changed'): environment.verify()


def facts(policy):
    members=[m for m in policy['members'] if not cpu._excluded(m['name'])]
    result=[cpu.CPUFileFact(cpu.PREFIX+m['name'],m['size_bytes'],m['sha256'],(),
        ResourceClassification('requires_private_root_context' if m['manifest_sha256'] else 'data_resources',1,m['manifest_sha256'])) for m in members]
    result += [cpu.CPUFileFact(name,4096,'a'*64,(),ResourceClassification('no_resources',0)) for name in cpu.BASE]
    return tuple(result)


def test_graph_cross_directory_pyd_edge_unique_resolution_and_pending(policy):
    rows=facts(policy); index=next(i for i,f in enumerate(rows) if f.name.endswith('/_torchaudio.pyd'))
    rows=(*rows[:index],replace(rows[index],imports=('c10.dll','libtorchaudio.pyd','msvcp140.dll')), *rows[index+1:])
    result=cpu.plan_cpu_facts(rows)
    assert (rows[index].name,'c10.dll',cpu.PREFIX+'torch/lib/c10.dll') in result.edges
    assert (rows[index].name,'libtorchaudio.pyd',cpu.PREFIX+'torchaudio/lib/libtorchaudio.pyd') in result.edges
    assert not result.dispatch_authorized and ('compiler_sources','fixed_vc140_set_required') in result.pending


@pytest.mark.parametrize('change',['excluded','identity','path_import','uppercase','duplicate','byte_limit'])
def test_graph_unknown_or_ambiguous_facts_cannot_be_admitted(policy,change):
    rows=facts(policy)
    if change=='excluded': rows=(replace(rows[0],name=cpu.PREFIX+'torch/bin/asmjit.dll'), *rows[1:])
    if change=='identity': rows=(replace(rows[0],sha256='b'*64),*rows[1:])
    if change=='path_import': rows=(replace(rows[0],imports=('../external.dll',)),*rows[1:])
    if change=='uppercase': rows=(replace(rows[0],imports=('KERNEL32.dll',)),*rows[1:])
    if change=='duplicate': rows=(*rows,rows[0])
    if change=='byte_limit': rows=tuple(replace(r,size_bytes=cpu.GRAPH_BYTES+1) if r.name=='python.exe' else r for r in rows)
    with pytest.raises(NativeExecutionUnsupported): cpu.plan_cpu_facts(rows)


def test_missing_private_dependency_and_new_policy_do_not_grant_runtime(policy):
    rows=facts(policy); rows=(replace(rows[0],imports=('avcodec-60.dll','vcomp140.dll')),*rows[1:-1])
    plan=cpu.plan_cpu_facts(rows)
    assert any(kind=='unresolved_dependency' and name.endswith(':avcodec-60.dll') for kind,name in plan.pending)
    assert any(kind=='unresolved_dependency' and name.endswith(':vcomp140.dll') for kind,name in plan.pending)
    owned=parse_os_policy(owned_os_policy_bytes())
    assert {'dbghelp.dll','iphlpapi.dll','psapi.dll','userenv.dll'} <= owned.component_names
    assert 'api-ms-win-crt-utility-l1-1-0.dll' in owned.contract_names
    assert not {'msvcp140.dll','vcomp140.dll'} & owned.component_names


@pytest.fixture
def sources(tmp_path,monkeypatch,policy):
    selected=copy.deepcopy(policy); selected['sources']={'torio/_extension/utils.py':digest(b'synthetic source')}
    monkeypatch.setattr(cpu,'_owned',lambda:(selected,'d'*64))
    path=tmp_path/(cpu.PREFIX+'torio/_extension/utils.py'); path.parent.mkdir(parents=True)
    path.write_bytes(b'synthetic source')
    row=SimpleNamespace(name=cpu.PREFIX+'torio/_extension/utils.py',role='dependency',sha256=digest(path.read_bytes()),size_bytes=path.stat().st_size)
    return tmp_path,SimpleNamespace(files=(row,),directories=()),path


def test_loader_source_identity_and_missing_unversioned_module(sources):
    root,inventory,path=sources
    assert cpu.require_cpu_sources(root,inventory)=='d'*64
    path.write_bytes(b'changed source')
    with pytest.raises(NativeExecutionUnsupported,match='source_changed'):
        cpu.require_cpu_sources(root,inventory)


@pytest.mark.parametrize('name',['_torio_ffmpeg.py','_torio_ffmpeg.pyd',
    '_torio_ffmpeg.cp312-win_amd64.pyd','_torio_ffmpeg','libtorio_ffmpeg.pyd'])
def test_new_module_or_package_breaks_ffmpeg_exclusion(sources,name):
    root,inventory,_=sources
    inventory.directories=(cpu.PREFIX+'torio/lib/'+name,)
    with pytest.raises(NativeExecutionUnsupported,match='ffmpeg_exclusion_changed'):
        cpu.require_cpu_sources(root,inventory)


def test_versioned_modules_retained_without_native_admission(sources):
    root,inventory,_=sources
    inventory.directories=(cpu.PREFIX+'torio/lib/_torio_ffmpeg6.pyd',)
    assert cpu.require_cpu_sources(root,inventory)=='d'*64


def test_manifest_extra_leaf_and_graph_resource_forgery_rejected(tmp_path,monkeypatch,policy,manifest):
    form_id,form=manifest
    raw=resource_image([(24,2,1033,bytes.fromhex(form['hex']),form['codepage']),
        (24,3,1033,b'additional policy',0)])
    selected=copy.deepcopy(policy); member=next(m for m in selected['members'] if m['manifest_sha256']==form_id)
    member.update(size_bytes=len(raw),sha256=digest(raw))
    monkeypatch.setattr(cpu,'_owned',lambda:(selected,'f'*64))
    path=tmp_path/'extra.bin';path.write_bytes(raw)
    with pytest.raises(NativeExecutionUnsupported,match='manifest_changed'):classify(path,member)
    rows=facts(selected)
    with pytest.raises(NativeExecutionUnsupported,match='resource_changed'):
        cpu.plan_cpu_facts((replace(rows[0],resources=ResourceClassification('admitted',0)),*rows[1:]))
