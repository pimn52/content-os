"""Owned static catalog tests; no DLL loading or runtime admission."""
import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

from app.providers import acquired_native_recipe as acquired
from app.providers.cpu_native_recipe import CPUFileFact
from app.providers.pe_resources import ResourceClassification
from app.providers.native_dependencies import diagnose_acquired_source_dependencies
from app.providers.runtime_primitives import NativeExecutionUnsupported


@pytest.fixture
def policy(monkeypatch):
    raw=Path(acquired.__file__).with_suffix('.json').read_bytes()
    monkeypatch.setattr(acquired,'owned_acquired_bytes',lambda:raw)
    return acquired.parse_acquired_descriptor(raw)[0]


def fact(row):
    resource=row['resources']
    return CPUFileFact(row['slot'],row['size_bytes'],row['sha256'],tuple(row['dependencies'] or ()),
        ResourceClassification(resource['kind'],resource['leaf_count'],resource['manifest_sha256']))


def test_catalog_and_unknown_remain_distinct(policy):
    result=diagnose_acquired_source_dependencies()
    assert len(policy['members'])==29 and len(policy['sources'])==10 and len(policy['terms'])==5
    assert not result.dispatch_authorized and not result.verified_facts
    assert any(dependencies is None for _,dependencies in result.recorded_dependencies)
    assert ('unverified','normal_worker_integration') in result.pending
    assert any(name=='recorded_imports_unknown' for name,_ in result.pending)
    assert result.recorded_edges and any(kind=='private_recorded' for _,_,kind in result.recorded_edges)
    assert len(result.os_policy_sha256)==64


def test_finite_facts_do_not_clear_runtime_gates(policy):
    facts=tuple(fact(row) for row in policy['members'] if row['dependencies'] is not None)
    sources=tuple((r['slot'],r['size_bytes'],r['sha256']) for r in policy['sources']+policy['terms'])
    result=diagnose_acquired_source_dependencies(facts,source_identities=sources,
        search_directories=(policy['numpy_loader']['directory'],))
    assert result.verified_facts==facts and not result.dispatch_authorized
    assert not any(name=='missing_source_fact' for name,_ in result.pending)
    assert ('unverified','actual_current_source_identity') in result.pending
    assert ('unverified','numpy_directory_registration_lifetime') in result.pending


@pytest.mark.parametrize('change',['hash','size','imports','manifest','leaf_bool','name','unhashable_name'])
def test_changed_facts_rejected(policy,change):
    value=fact(next(r for r in policy['members'] if r['slot']=='msvcp140.dll'))
    changed={'hash':replace(value,sha256='a'*64),'size':replace(value,size_bytes=value.size_bytes+1),
        'imports':replace(value,imports=()),'manifest':replace(value,resources=ResourceClassification('requires_private_root_context',1,'a'*64)),
        'leaf_bool':replace(value,resources=ResourceClassification('no_resources',False)),
        'name':replace(value,name='other/msvcp140.dll'),
        'unhashable_name':replace(value,name=[])}[change]
    with pytest.raises(NativeExecutionUnsupported):diagnose_acquired_source_dependencies((changed,))


def test_duplicates_unknown_and_old_identity_rejected(policy):
    value=fact(next(r for r in policy['members'] if r['slot']=='msvcp140.dll'))
    for facts in ((value,value),(fact(next(r for r in policy['members'] if r['dependencies'] is None)),)):
        with pytest.raises(NativeExecutionUnsupported):diagnose_acquired_source_dependencies(facts)
    with pytest.raises(NativeExecutionUnsupported):diagnose_acquired_source_dependencies(descriptor_sha256='0'*64)


@pytest.mark.parametrize('directories',[('numpy.libs',),('../numpy.libs',),('Lib/site-packages/numpy.libs','other'),['Lib/site-packages/numpy.libs']])
def test_search_scope_no_prefix_or_extra_root(policy,directories):
    with pytest.raises(NativeExecutionUnsupported):diagnose_acquired_source_dependencies(search_directories=directories)


def test_source_identity_and_duplicate(policy):
    row=policy['sources'][0];value=(row['slot'],row['size_bytes'],row['sha256'])
    for sources in ((value,value),((row['slot'],row['size_bytes'],'a'*64),),(({},1,'a'*64),)):
        with pytest.raises(NativeExecutionUnsupported):diagnose_acquired_source_dependencies(source_identities=sources)


def test_descriptor_cannot_be_overridden(policy):
    changed=copy.deepcopy(policy);changed['pending']=[]
    with pytest.raises(NativeExecutionUnsupported):acquired.parse_acquired_descriptor(json.dumps(changed).encode())


@pytest.mark.parametrize('change',['path','basename','manifest','package','cycle','numpy_root'])
def test_owned_structural_invalidity(policy,monkeypatch,change):
    changed=copy.deepcopy(policy)
    if change=='path':changed['members'][0]['slot']='../bad.dll'
    if change=='basename':changed['members'][1]['slot']='extra/'+Path(changed['members'][0]['slot']).name
    if change=='manifest':changed['members'][0]['resources']['manifest_sha256']='a'*64
    if change=='package':changed['members'][0]['container']='missing'
    if change=='cycle':changed['containers'][0]['parent']=changed['containers'][0]['id']
    if change=='numpy_root':changed['numpy_loader']['directory']='outside'
    raw=json.dumps(changed).encode();monkeypatch.setattr(acquired,'owned_acquired_bytes',lambda:raw)
    with pytest.raises(NativeExecutionUnsupported):acquired.parse_acquired_descriptor(raw)


def test_owned_identity_is_separate(policy):
    raw=acquired.owned_acquired_bytes()
    assert acquired.parse_acquired_descriptor(raw)[1]==hashlib.sha256(raw).hexdigest()
    assert policy['recipe']!='omnivoice-private-native-observed-v1'


def test_os_shadow_cannot_become_private(policy,monkeypatch):
    changed=copy.deepcopy(policy)
    changed['members'][0]['slot']='kernel32.dll'
    raw=json.dumps(changed).encode();monkeypatch.setattr(acquired,'owned_acquired_bytes',lambda:raw)
    with pytest.raises(NativeExecutionUnsupported,match='os_shadow'):
        diagnose_acquired_source_dependencies()


def test_new_recorded_unknown_remains_pending(policy,monkeypatch):
    changed=copy.deepcopy(policy)
    row=next(r for r in changed['members'] if r['slot']=='msvcp140.dll')
    row['dependencies']=sorted(row['dependencies']+['unknown.dll'])
    raw=json.dumps(changed).encode();monkeypatch.setattr(acquired,'owned_acquired_bytes',lambda:raw)
    result=diagnose_acquired_source_dependencies()
    assert ('recorded_edge_unknown','msvcp140.dll:unknown.dll') in result.pending
    assert ('msvcp140.dll','unknown.dll','unknown_recorded') in result.recorded_edges
    assert not result.dispatch_authorized
