"""Private source representation using synthetic PE/files and owned descriptors."""
import copy
import hashlib
import json
import struct
from types import SimpleNamespace

import pytest

from app.providers import private_native_recipe as private, cpu_native_recipe as cpu
from app.providers.pe_bounded import open_bounded_pe
from app.providers.pe_resources import (PEView, ResourceClassification, classify_pe_resources,
    read_private_resource_leaves)
from app.providers.runtime_primitives import NativeExecutionUnsupported
from test_pe_resources import resource_image
from test_cpu_native_recipe import facts


def digest(raw): return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def policy(): return private._owned()[0]


def strings(): return struct.pack('<H',1) + b'A\x00' + bytes(30)


def synthetic(tmp_path,monkeypatch,policy, *, change=None):
    chosen = copy.deepcopy(policy)
    member = next(row for row in chosen['members'] if row['slot']=='vcomp140.dll')
    form = cpu._owned()[0]['manifests'][member['manifest_sha256']]
    leaves = [(6,63,1033,strings(),0),(16,1,3081,b'version',0),
        (24,2,1033,bytes.fromhex(form['hex']),form['codepage'])]
    if change=='type': leaves[0]=(10,63,1033,strings(),0)
    if change=='codepage': leaves[0]=(6,63,1033,strings(),1252)
    if change=='raw': leaves[-1]=(24,2,1033,bytes.fromhex(form['hex'])+b' ',0)
    if change=='id': leaves[-1]=(24,1,1033,bytes.fromhex(form['hex']),0)
    if change=='language': leaves[-1]=(24,2,3081,bytes.fromhex(form['hex']),0)
    if change=='manifest_cp': leaves[-1]=(24,2,1033,bytes.fromhex(form['hex']),1252)
    if change=='double': leaves.append((24,3,1033,bytes.fromhex(form['hex']),0))
    raw=resource_image(leaves,exe=change=='exe')
    member.update(size_bytes=len(raw),sha256=digest(raw))
    path=tmp_path/'synthetic.bin';path.write_bytes(raw)
    monkeypatch.setattr(private,'_owned',lambda:(chosen,'f'*64))
    return path,member


def classify(path,member, **kwargs):
    with open_bounded_pe(path,size_bytes=member['size_bytes'],sha256=member['sha256']) as view:
        options=dict(slot=member['slot'],size_bytes=member['size_bytes'],sha256=member['sha256'])
        options.update(kwargs)
        return private.classify_private_resources(view,**options)


def test_owned_sources_terms_versions_are_observed_not_admitted(policy):
    assert len(policy['members'])==8 and len(policy['sources'])==3 and len(policy['terms'])==4
    assert {row['declared_version'] for row in policy['members'] if row['slot'] in private.COMPILER_SLOTS}=={
        '14.44.35211','14.42.34438'}
    gaps=private.private_pending(policy)
    assert ('compiler_set','coherent_source_compatibility_terms_required') in gaps
    assert ('backend_handle_source','build_correspondence_not_verified') in gaps
    assert {slot for kind,slot in gaps if kind=='terms_missing'}==set(private.COMPILER_SLOTS)


def test_numeric_strings_and_version_language_do_not_inherit_context(tmp_path,monkeypatch,policy):
    path,member=synthetic(tmp_path,monkeypatch,policy)
    result=classify(path,member)
    assert result.kind=='requires_private_root_context' and result.leaf_count==3
    with pytest.raises(NativeExecutionUnsupported): classify_pe_resources(path.read_bytes())


@pytest.mark.parametrize('change',['type','codepage','raw','id','language','manifest_cp','double','exe'])
def test_private_resource_unknown_or_changed_form_refuses(tmp_path,monkeypatch,policy,change):
    path,member=synthetic(tmp_path,monkeypatch,policy,change=change)
    with pytest.raises(NativeExecutionUnsupported): classify(path,member)


@pytest.mark.parametrize('payload',[bytes(30),bytes(33),b'\xff\xff'+bytes(30),
    b'\x01\x00\x00\xd8'+bytes(30)])
def test_malformed_utf16_string_tables_fail_closed(payload):
    with pytest.raises(NativeExecutionUnsupported):
        read_private_resource_leaves(PEView(resource_image([(6,63,1033,payload,0)])))


@pytest.mark.parametrize('field,value',[('slot','unknown.dll'),('sha256','b'*64),('size_bytes',1)])
def test_claim_cannot_replace_owned_whole_source_identity(tmp_path,monkeypatch,policy,field,value):
    path,member=synthetic(tmp_path,monkeypatch,policy)
    with pytest.raises(NativeExecutionUnsupported,match='identity_changed'):
        classify(path,member,**{field:value})


def test_view_identity_cannot_be_replaced_by_descriptor(tmp_path,monkeypatch,policy):
    _,member=synthetic(tmp_path,monkeypatch,policy)
    other=tmp_path/'other.bin';other.write_bytes(resource_image())
    with open_bounded_pe(other,size_bytes=other.stat().st_size,sha256=digest(other.read_bytes())) as view:
        with pytest.raises(NativeExecutionUnsupported,match='identity_changed'):
            private.classify_private_resources(view,slot=member['slot'],size_bytes=member['size_bytes'],sha256=member['sha256'])


@pytest.mark.parametrize('problem',['closure','duplicate','bool','unknown_slot','unknown_manifest','terms','role','source_path'])
def test_descriptor_cannot_self_declare_authority(tmp_path,monkeypatch,policy,problem):
    value=copy.deepcopy(policy)
    if problem=='closure': value['closure']=True
    if problem=='duplicate': value['members'].append(value['members'][0])
    if problem=='bool': value['members'][0]['size_bytes']=True
    if problem=='unknown_slot': value['members'][0]['slot']='arbitrary.dll'
    if problem=='unknown_manifest': value['members'][0]['manifest_sha256']='a'*64
    if problem=='terms': value['members'][0]['terms_slots']=['missing.txt']
    if problem=='role': value['members'][0]['graph_role']='trusted'
    if problem=='source_path': value['sources'][0]['slot']='../soundfile.py'
    path=tmp_path/'private.json';path.write_text(json.dumps(value))
    monkeypatch.setattr(private,'__file__',str(path.with_suffix('.py')))
    with pytest.raises(NativeExecutionUnsupported,match='owned_recipe_invalid'): private._owned()


def private_facts(policy):
    common=digest(__import__('app.providers.native_manifest',fromlist=['CPYTHON_COMMON_CONTROLS']).CPYTHON_COMMON_CONTROLS)
    return tuple(cpu.CPUFileFact(row['slot'],row['size_bytes'],row['sha256'],(),
        ResourceClassification('requires_os_sxs_binding' if row['manifest_sha256']==common else
            'requires_private_root_context' if row['manifest_sha256'] else 'data_resources',1,row['manifest_sha256']))
        for row in policy['members'])


def combined(policy):
    return tuple(row for row in facts(cpu._owned()[0]) if row.name not in cpu.BASE)+private_facts(policy)


def test_explicit_graph_resolves_audio_os_edge_and_retains_every_runtime_gap(policy):
    from dataclasses import replace
    rows=combined(policy)
    rows=tuple(replace(row,imports=('shlwapi.dll','vcomp140.dll')) if row.name==private.AUDIO_SLOTS[1] else row for row in rows)
    plan=cpu.plan_cpu_private_facts(rows)
    assert (private.AUDIO_SLOTS[1],'shlwapi.dll','OS:shlwapi.dll') in plan.edges
    assert (private.AUDIO_SLOTS[1],'vcomp140.dll','vcomp140.dll') in plan.edges
    assert plan.private_policy_sha256==private._owned()[1] and not plan.dispatch_authorized
    assert private.private_pending(policy)<=set(plan.pending)
    assert ('dynamic_closure','not_verified') in plan.pending
    with pytest.raises(NativeExecutionUnsupported): cpu.plan_cpu_facts(rows)


@pytest.mark.parametrize('problem',['hash','duplicate','missing','claimed_resource'])
def test_private_graph_source_gaps_and_collisions_are_not_hidden(policy,problem):
    from dataclasses import replace
    rows=combined(policy)
    index=next(i for i,row in enumerate(rows) if row.name=='vcomp140.dll')
    if problem=='hash': rows=(*rows[:index],replace(rows[index],sha256='b'*64),*rows[index+1:])
    if problem=='duplicate': rows=(*rows,rows[index])
    if problem=='claimed_resource': rows=(*rows[:index],replace(rows[index],resources=ResourceClassification('data_resources',1)),*rows[index+1:])
    if problem=='missing':
        rows=tuple(row for row in rows if row.name!=private.AUDIO_SLOTS[0])
        plan=cpu.plan_cpu_private_facts(rows)
        assert ('missing_private_member',private.AUDIO_SLOTS[0]) in plan.pending
        assert not plan.dispatch_authorized
    else:
        with pytest.raises(NativeExecutionUnsupported): cpu.plan_cpu_private_facts(rows)


def test_source_terms_missing_are_explicit_and_changed_bytes_reject(tmp_path,monkeypatch,policy):
    selected=copy.deepcopy(policy)
    row=selected['sources'][0];raw=b'raise RuntimeError("never execute")\n'
    row.update(size_bytes=len(raw),sha256=digest(raw))
    monkeypatch.setattr(private,'_owned',lambda:(selected,'f'*64))
    path=tmp_path/row['slot'];path.parent.mkdir(parents=True);path.write_bytes(raw)
    entry=SimpleNamespace(name=row['slot'],role='dependency',size_bytes=len(raw),sha256=digest(raw))
    inventory=SimpleNamespace(files=(entry,))
    result=private.inspect_private_runtime(tmp_path,inventory)
    assert result.verified_sources==(row['slot'],) and not result.dispatch_authorized
    assert any(kind=='missing_terms' for kind,_ in result.pending)
    path.write_bytes(raw.replace(b'never',b'other'))
    with pytest.raises(NativeExecutionUnsupported,match='source_changed'):
        private.inspect_private_runtime(tmp_path,inventory)
