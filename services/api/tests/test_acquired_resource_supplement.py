import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import struct

import pytest
from app.providers import acquired_native_recipe as acquired, cpu_native_recipe as cpu
from app.providers.pe_bounded import open_bounded_pe
from app.providers.pe_resources import ResourceClassification
from app.providers.runtime_primitives import NativeExecutionUnsupported
from test_acquired_native_recipe import policy
from test_pe_resources import resource_image


def synthetic(tmp_path,monkeypatch,policy,change=None):
    chosen=copy.deepcopy(policy)
    row=next(r for r in chosen['members'] if r['slot']=='vcomp140.dll')
    form=cpu._owned()[0]['manifests'][row['resources']['manifest_sha256']]
    strings=struct.pack('<H',1)+b'A\0'+bytes(30)
    leaves=[(6,63,1033,strings,0),(16,1,1033,b'version',0),
        (24,2,1033,bytes.fromhex(form['hex']),form['codepage'])]
    if change=='raw':leaves[-1]=(24,2,1033,bytes.fromhex(form['hex'])+b' ',0)
    if change=='id':leaves[-1]=(24,1,1033,bytes.fromhex(form['hex']),0)
    if change=='language':leaves[-1]=(24,2,3081,bytes.fromhex(form['hex']),0)
    if change=='codepage':leaves[-1]=(24,2,1033,bytes.fromhex(form['hex']),1252)
    if change=='type':leaves[0]=(10,63,1033,strings,0)
    if change=='string_cp':leaves[0]=(6,63,1033,strings,1252)
    if change=='extra':leaves.append((16,2,1033,b'extra',0))
    raw=resource_image(leaves,exe=change=='exe')
    row.update(size_bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())
    row['resources']['leaf_count']=3
    descriptor=json.dumps(chosen).encode();monkeypatch.setattr(acquired,'owned_acquired_bytes',lambda:descriptor)
    path=tmp_path/'synthetic.bin';path.write_bytes(raw)
    return path,row


def classify(path,row,**updates):
    options=dict(slot=row['slot'],size_bytes=row['size_bytes'],sha256=row['sha256']);options.update(updates)
    with open_bounded_pe(path,size_bytes=row['size_bytes'],sha256=row['sha256']) as view:
        return acquired.classify_acquired_resources(view,**options)


def test_finite_private_resources_keep_context_pending(tmp_path,monkeypatch,policy):
    path,row=synthetic(tmp_path,monkeypatch,policy)
    assert classify(path,row)==ResourceClassification('requires_private_root_context',3,row['resources']['manifest_sha256'])


@pytest.mark.parametrize('change',['raw','id','language','codepage','type','string_cp','extra','exe'])
def test_unknown_resource_or_changed_form(tmp_path,monkeypatch,policy,change):
    path,row=synthetic(tmp_path,monkeypatch,policy,change)
    with pytest.raises(NativeExecutionUnsupported):classify(path,row)


@pytest.mark.parametrize('field,value',[('sha256','a'*64),('size_bytes',1),('slot','unknown.dll')])
def test_claim_does_not_replace_source(tmp_path,monkeypatch,policy,field,value):
    path,row=synthetic(tmp_path,monkeypatch,policy)
    with pytest.raises(NativeExecutionUnsupported,match='identity_changed'):classify(path,row,**{field:value})


def test_closed_view_denied(tmp_path,monkeypatch,policy):
    path,row=synthetic(tmp_path,monkeypatch,policy)
    with open_bounded_pe(path,size_bytes=row['size_bytes'],sha256=row['sha256']) as view:pass
    with pytest.raises(NativeExecutionUnsupported,match='identity_changed'):
        acquired.classify_acquired_resources(view,slot=row['slot'],size_bytes=row['size_bytes'],sha256=row['sha256'])


@pytest.mark.parametrize('slot',['python.exe','python312.dll'])
def test_bootstrap_common_controls_preserve_legacy_branch(tmp_path,monkeypatch,policy,slot):
    from app.providers.native_manifest import CPYTHON_COMMON_CONTROLS
    chosen=copy.deepcopy(policy);row=next(r for r in chosen['members'] if r['slot']==slot)
    raw=resource_image([(24,1 if slot=='python.exe' else 2,1033,CPYTHON_COMMON_CONTROLS,0)],exe=slot=='python.exe')
    row.update(size_bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest());row['resources']['leaf_count']=1
    descriptor=json.dumps(chosen).encode();monkeypatch.setattr(acquired,'owned_acquired_bytes',lambda:descriptor)
    path=tmp_path/'bootstrap.bin';path.write_bytes(raw)
    assert classify(path,row).kind=='requires_os_sxs_binding'


def test_no_resource_member_is_not_a_context(tmp_path,monkeypatch,policy):
    chosen=copy.deepcopy(policy);row=next(r for r in chosen['members'] if r['slot']=='vcruntime140.dll')
    raw=resource_image([]);row.update(size_bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())
    row['resources']={'kind':'no_resources','leaf_count':0,'manifest_sha256':None}
    descriptor=json.dumps(chosen).encode();monkeypatch.setattr(acquired,'owned_acquired_bytes',lambda:descriptor)
    path=tmp_path/'no-manifest.bin';path.write_bytes(raw)
    assert classify(path,row).manifest_sha256 is None


@pytest.fixture
def supplement(monkeypatch,policy):
    raw=Path(acquired.__file__).with_name('acquired_import_supplement.json').read_bytes()
    monkeypatch.setattr(acquired,'owned_import_supplement_bytes',lambda:raw)
    return acquired.parse_import_supplement(raw)[0]


def test_supplement_does_not_upgrade_base(supplement):
    result=acquired.describe_acquired_import_supplement()
    assert not result.dispatch_authorized and not result.base.dispatch_authorized
    assert len(result.retained_dependencies)==14 and result.compared_fact is None
    assert ('recorded_imports_unknown',result.slot) in result.base.pending
    assert ('unverified','normal_worker_integration') in result.base.pending


def test_exact_supplement_fact_is_not_admission(supplement):
    source=supplement['source']
    value=cpu.CPUFileFact(source['slot'],source['size_bytes'],source['sha256'],
        tuple(supplement['dependencies']),ResourceClassification('no_resources',0))
    result=acquired.describe_acquired_import_supplement(value)
    assert result.compared_fact==value and not result.dispatch_authorized
    for changed in (replace(value,sha256='a'*64),replace(value,imports=()),
                    replace(value,resources=ResourceClassification('no_resources',False))):
        with pytest.raises(NativeExecutionUnsupported):acquired.describe_acquired_import_supplement(changed)
    with pytest.raises(NativeExecutionUnsupported):
        acquired.describe_acquired_import_supplement(supplement_sha256='a'*64)


@pytest.mark.parametrize('change',['base','source','unknown_field','authority','bool_version','dependencies'])
def test_supplement_wrong_identity_or_claim(supplement,monkeypatch,change):
    changed=copy.deepcopy(supplement)
    if change=='base':changed['base_descriptor_sha256']='a'*64
    if change=='source':changed['source']['sha256']='a'*64
    if change=='unknown_field':changed['closed']=True
    if change=='authority':changed['dispatch_authorized']=True
    if change=='bool_version':changed['version']=True
    if change=='dependencies':changed['dependencies']=['../bad.dll']
    raw=json.dumps(changed).encode();monkeypatch.setattr(acquired,'owned_import_supplement_bytes',lambda:raw)
    with pytest.raises(NativeExecutionUnsupported):acquired.parse_import_supplement(raw)
