import copy
from dataclasses import replace
import hashlib
import pytest
from app.providers import cpu_loader_requirements as loader
from app.providers import cpu_native_recipe as cpu
from app.providers.runtime_primitives import NativeExecutionUnsupported


def test_exact_original_loader_declarations_are_inert():
    value = loader.original_cpu_loader_requirements()
    assert len(value.torch_glob_members) == 11
    assert len(value.compiler_roots) == 4 and len(value.numpy_private_members) == 2
    assert len(value.source_identities) == 22
    assert all('/torch/lib/' in slot and '/torch/bin/' not in slot for slot,_,_ in value.torch_glob_members)
    assert value.private_search_directories == ('Lib/site-packages/torch/lib','Lib/site-packages/numpy.libs')
    assert not value.dispatch_authorized and not value.loading_authorized
    assert 'whole_python_and_native_dynamic_closure' in value.pending
    assert value.sha256 == hashlib.sha256(value.canonical_bytes()).hexdigest()


def test_canonical_order_and_changed_identity(monkeypatch):
    original = loader.original_cpu_loader_requirements()
    policy, identity = cpu._owned()
    changed = copy.deepcopy(policy)
    changed['members'].reverse()
    changed['sources'] = dict(reversed(list(changed['sources'].items())))
    monkeypatch.setattr(cpu, '_owned', lambda:(changed,identity))
    assert loader.original_cpu_loader_requirements().sha256 == original.sha256
    assert replace(original, cpu_policy_sha256='0'*64).sha256 != original.sha256
    assert replace(original, acquired_policy_sha256='0'*64).sha256 != original.sha256
    assert replace(original, os_policy_sha256='0'*64).sha256 != original.sha256


@pytest.mark.parametrize('change',['missing','extra','duplicate','case','virtual','size','total','source'])
def test_invalid_owned_requirement_shape_stops(monkeypatch,change):
    policy, identity = cpu._owned()
    policy = copy.deepcopy(policy)
    row = next(r for r in policy['members'] if r['name'] == 'torch/lib/asmjit.dll')
    if change == 'missing': policy['members'].remove(row)
    if change == 'extra': policy['members'].append(dict(row,name='torch/lib/extra.dll'))
    if change == 'duplicate': row['name']='torch/lib/c10.dll'
    if change == 'case': row['name']='torch/lib/C10.dll'
    if change == 'virtual': row['name']='torch/lib/kernel32.dll'
    if change == 'size': row['size_bytes']=True
    if change == 'total':
        for member in policy['members']:
            if member['name'].startswith('torch/lib/'): member['size_bytes']=128*1024**2
    if change == 'source': policy['sources'].pop('torch/__init__.py')
    monkeypatch.setattr(cpu,'_owned',lambda:(policy,identity))
    with pytest.raises(NativeExecutionUnsupported): loader.original_cpu_loader_requirements()
