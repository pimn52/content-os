import copy
import hashlib
import json
from pathlib import Path
import pytest
from app.providers import acquired_native_recipe as acquired, cpu_native_recipe as cpu, windows_activation as act
from app.providers.runtime_primitives import NativeExecutionUnsupported
from test_cpu_activation import RootABI
from test_pe_resources import resource_image


@pytest.fixture(params=[0,1,2])
def setup(request,tmp_path,monkeypatch):
    policy=json.loads(Path(acquired.__file__).with_suffix('.json').read_bytes())
    cpu_policy,_=cpu._owned();manifest=sorted(cpu_policy['manifests'])[request.param]
    form=cpu_policy['manifests'][manifest]
    raw=resource_image([(24,2,1033,bytes.fromhex(form['hex']),form['codepage'])])
    row=next(r for r in policy['members'] if r['slot']=='vcomp140.dll')
    row.update(size_bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())
    row['resources']={'kind':'requires_private_root_context','leaf_count':1,'manifest_sha256':manifest}
    encoded=json.dumps(policy).encode();monkeypatch.setattr(acquired,'owned_acquired_bytes',lambda:encoded)
    source=tmp_path/'representative.bin';source.write_bytes(raw)
    reader=object.__new__(act.WindowsActivationReader)
    reader._kernel,reader._last_error=RootABI(source),lambda:122
    return reader,source,row,policy


def test_exact_root_only_prediction_budget(setup):
    reader,source,row,_=setup
    result=reader.inspect_acquired_pe(source,row['slot'])
    assert not result.dispatch_authorized and result.source_sha256==row['sha256']
    assert result.source_parent==str(source.parent)
    assert reader._kernel.calls==[('create',9,9,0,2)]+[
        ('query',2,False),('query',2,True),('query',3,False),('query',3,True)]*2+[('release',99)]


@pytest.mark.parametrize('problem',['create','roster','files','unstable','flags','oversize','second_failure'])
def test_metadata_failure_releases(setup,problem):
    reader,source,row,_=setup;reader._kernel.problem=problem
    with pytest.raises(NativeExecutionUnsupported):reader.inspect_acquired_pe(source,row['slot'])
    assert sum(c[0]=='release' for c in reader._kernel.calls)==int(problem!='create')
    assert sum(c[0]=='query' for c in reader._kernel.calls)<=8


def test_unknown_slot_and_changed_bytes_before_api(setup):
    reader,source,row,_=setup
    with pytest.raises(NativeExecutionUnsupported):reader.inspect_acquired_pe(source,'unknown.dll')
    source.write_bytes(source.read_bytes()+b'changed')
    with pytest.raises(NativeExecutionUnsupported):reader.inspect_acquired_pe(source,row['slot'])
    assert not reader._kernel.calls


def test_release_error_returns_no_prediction(setup,monkeypatch):
    reader,source,row,_=setup
    def release(handle):raise OSError('release failure')
    monkeypatch.setattr(reader._kernel,'ReleaseActCtx',release)
    with pytest.raises(NativeExecutionUnsupported,match='release_unavailable'):reader.inspect_acquired_pe(source,row['slot'])


def test_descriptor_change_after_release(setup,monkeypatch):
    reader,source,row,policy=setup;original=reader._kernel.ReleaseActCtx
    def release(handle):
        original(handle);changed=copy.deepcopy(policy);changed['pending'].append('new_gate')
        monkeypatch.setattr(acquired,'owned_acquired_bytes',lambda:json.dumps(changed).encode())
    monkeypatch.setattr(reader._kernel,'ReleaseActCtx',release)
    with pytest.raises(NativeExecutionUnsupported):reader.inspect_acquired_pe(source,row['slot'])
