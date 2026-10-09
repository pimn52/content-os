from dataclasses import replace
import hashlib
from pathlib import PurePosixPath
import pytest
from app.domain.execution_runtime import RuntimeInventory, RuntimeInventoryFile
from app.providers import cpu_loader_requirements as loader
from app.providers.runtime_primitives import ExecutionPreparationError


@pytest.fixture
def layout(tmp_path,monkeypatch):
    root = tmp_path/'root'
    root.mkdir()
    required = loader.original_cpu_loader_requirements()
    raw = b'x'*64
    digest = hashlib.sha256(raw).hexdigest()
    def native(rows): return tuple((name,64,digest) for name,_,_ in rows)
    required = replace(required,torch_glob_members=native(required.torch_glob_members),
                       compiler_roots=native(required.compiler_roots),numpy_private_members=native(required.numpy_private_members),
                       source_identities=tuple((name,digest) for name,_ in required.source_identities))
    monkeypatch.setattr(loader,'original_cpu_loader_requirements',lambda:required)
    rows = [('python.exe','interpreter'),('entry.py','entrypoint')]
    rows += [(name,'dependency') for name,_,_ in required.torch_glob_members+required.compiler_roots+required.numpy_private_members]
    rows += [(name,'dependency') for name,_ in required.source_identities]
    files = []
    dirs = set()
    for name,role in rows:
        path = root/name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(raw)
        files.append(RuntimeInventoryFile(role=role,name=name,size_bytes=64,sha256=digest))
        parts = name.split('/')
        dirs.update('/'.join(parts[:i]) for i in range(1,len(parts)))
    inventory = RuntimeInventory(inventory_version=1, directories=tuple(sorted(dirs)), files=tuple(files))
    return root,inventory,required


def test_selected_current_layout_does_not_authorize_load(layout):
    root,inventory,required = layout
    value = loader.inspect_original_cpu_layout(root,inventory)
    assert value.requirements_sha256 == required.sha256
    assert value.inventory_sha256 == inventory.descriptor.sha256
    assert len(value.verified_native) == 17 and len(value.verified_sources) == 22
    assert not value.loading_authorized and not value.dispatch_authorized


@pytest.mark.parametrize('change',['missing','bytes','extra','redirect','unlisted_redirect','root_redirect','disabled','library','forged','source','role'])
def test_layout_rejects_unverified_shape(layout,change):
    root,inventory,required = layout
    target = root/required.torch_glob_members[0][0]
    if change == 'missing': target.unlink()
    if change == 'bytes': target.write_bytes(b'y'*64)
    if change == 'extra': (target.parent/'extra.dll').write_bytes(b'x'*64)
    if change == 'unlisted_redirect': (target.parent/'asmjit.dll.manifest').write_bytes(b'x'*64)
    if change == 'root_redirect': (root/'python.exe.local').mkdir()
    if change == 'redirect':
        name = 'Lib/site-packages/torch/lib/asmjit.dll.local'
        (root/name).mkdir()
        inventory = inventory.model_copy(update={'directories':inventory.directories+(name,)})
    if change == 'disabled': (root/'_cpu_disabled').mkdir()
    if change == 'library': (root/'Library/bin').mkdir(parents=True)
    if change == 'forged': inventory = inventory.model_copy(update={'directories':('../escape',)})
    if change == 'source': (root/required.source_identities[0][0]).write_bytes(b'y'*64)
    if change == 'role':
        slot = required.source_identities[0][0]
        inventory = inventory.model_copy(update={'files':tuple(row.model_copy(update={'role':'entrypoint'}) if row.name==slot else row for row in inventory.files)})
    with pytest.raises(ExecutionPreparationError): loader.inspect_original_cpu_layout(root,inventory)


def test_same_fd_mutation_is_not_accepted(layout,monkeypatch):
    root,inventory,required = layout
    original = loader._PEFile
    def changed(path,size,digest):
        value = original(path,size,digest)
        path.write_bytes(b'y'*size)
        return value
    monkeypatch.setattr(loader,'_PEFile',changed)
    with pytest.raises(ExecutionPreparationError): loader.inspect_original_cpu_layout(root,inventory)


def test_requirements_change_during_observation_stops(layout,monkeypatch):
    root,inventory,required = layout
    values = iter((required,replace(required,cpu_policy_sha256='0'*64)))
    monkeypatch.setattr(loader,'original_cpu_loader_requirements',lambda:next(values))
    with pytest.raises(ExecutionPreparationError): loader.inspect_original_cpu_layout(root,inventory)


def test_new_search_member_during_hashing_stops(layout,monkeypatch):
    root,inventory,required = layout
    original = loader._PEFile
    def changed(path,size,digest):
        value = original(path,size,digest)
        (root/'Lib/site-packages/torch/lib/extra.dll').write_bytes(b'y'*64)
        return value
    monkeypatch.setattr(loader,'_PEFile',changed)
    with pytest.raises(ExecutionPreparationError): loader.inspect_original_cpu_layout(root,inventory)
