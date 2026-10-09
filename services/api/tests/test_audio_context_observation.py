"""Actual synthetic file hashes, fake enumeration/query; zero OS/native calls."""
from dataclasses import replace
import hashlib
from types import SimpleNamespace

import pytest

from app.domain.execution_runtime import RuntimeInventory, RuntimeInventoryFile
from app.providers import audio_context_observation as observation
from app.providers.audio_native_graph import TARGETS
from app.providers.pe_resources import ResourceClassification
from app.providers.windows_activation import ActivationMetadata, USE_ACTIVE, IS_MODULE
from app.providers.runtime_primitives import NativeExecutionUnsupported
from test_audio_native_graph import exact_graph


@pytest.fixture
def captured(tmp_path, monkeypatch):
    root, system = tmp_path / 'runtime', tmp_path / 'System32'
    root.mkdir(); system.mkdir()
    slots = ('python.exe', TARGETS[0], TARGETS[1])
    members, entries, modules, metadata = [], [], [], {}
    for handle, slot in enumerate(slots, 1):
        path = root / slot
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = ('never executable:' + slot).encode()
        path.write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        members.append(dict(slot=slot, size_bytes=len(raw), sha256=digest))
        entries.append(RuntimeInventoryFile(name=slot, role='interpreter' if handle == 1 else 'dependency',
            size_bytes=len(raw), sha256=digest))
        modules.append((handle, path))
        metadata[handle] = ActivationMetadata(1, str(path), '', str(path.parent), (2, 1, 2), (), 0)
    os_path = system / 'kernel32.dll'
    os_path.write_bytes(b'fake OS file, not trusted by this observation')
    modules.append((4, os_path))
    (root / 'entry.py').write_bytes(b'# synthetic entry, never launched')
    entries.append(RuntimeInventoryFile(name='entry.py', role='entrypoint', size_bytes=len((root / 'entry.py').read_bytes()),
        sha256=hashlib.sha256((root / 'entry.py').read_bytes()).hexdigest()))
    inventory = RuntimeInventory(inventory_version=1,
        directories=('Lib', 'Lib/site-packages', 'Lib/site-packages/_soundfile_data'), files=tuple(entries))
    policy = SimpleNamespace(members=tuple(members), identity_sha256='a' * 64)
    monkeypatch.setattr(observation, 'sys', SimpleNamespace(executable=str(root / 'python.exe')))
    monkeypatch.setattr(observation, 'owned_audio_graph_policy', lambda: policy)
    classifications = {'python.exe': 'requires_os_sxs_binding',
        TARGETS[0]: 'requires_private_root_context', TARGETS[1]: 'requires_private_root_context'}
    def classify(raw, slot):
        member = next(row for row in members if row['slot'] == slot)
        assert (len(raw), hashlib.sha256(raw).hexdigest()) == (member['size_bytes'], member['sha256'])
        return ResourceClassification(classifications[slot], 1, 'b' * 64), None, ()
    monkeypatch.setattr(observation, 'inspect_audio_member', classify)
    calls = []
    def query(handle, *, flags):
        calls.append((handle, flags))
        return metadata[1 if flags == USE_ACTIVE else handle]
    reader = SimpleNamespace(_system_directory=system,
        _modules=SimpleNamespace(_snapshot=lambda: tuple(modules)), _metadata=query)
    return SimpleNamespace(root=root, system=system, inventory=inventory, modules=modules,
        metadata=metadata, reader=reader, calls=calls, policy=policy, classifications=classifications)


def test_private_audio_associations_from_observed_handles_not_inventory(captured):
    result = observation.capture_audio_contexts(captured.reader, captured.inventory)
    assert result.recipe == observation.RECIPE
    assert result.inventory_sha256 == captured.inventory.descriptor.sha256
    assert result.dispatch_authorized is False
    assert result.executable == captured.root / 'python.exe'
    assert len(result.origins) == 4  # OS origin retained, not certified here.
    assert tuple(path for path, _ in result.contexts.associated) == tuple(
        str(captured.root / slot) for slot in TARGETS)
    assert set(handle for handle, flags in captured.calls if flags == IS_MODULE) == {2, 3}


def test_unloaded_target_has_no_invented_query(captured):
    captured.modules.pop(2)
    result = observation.capture_audio_contexts(captured.reader, captured.inventory)
    assert len(result.contexts.associated) == 1
    assert all(handle != 3 for handle, _ in captured.calls)


@pytest.mark.parametrize('change', ['bytes', 'missing_inventory', 'role', 'unknown_module', 'foreign_root',
    'duplicate_handle', 'duplicate_path', 'bool_handle', 'missing_executable', 'unknown_resources'])
def test_invalid_source_rejected_before_any_query(captured, change, monkeypatch):
    if change == 'bytes': (captured.root / TARGETS[0]).write_bytes(b'changed')
    elif change == 'missing_inventory': captured.inventory = captured.inventory.model_copy(update={'files': captured.inventory.files[:1]})
    elif change == 'role':
        files = list(captured.inventory.files)
        files[1] = files[1].model_copy(update={'role': 'entrypoint'})
        captured.inventory = captured.inventory.model_copy(update={'files': tuple(files)})
    elif change == 'unknown_module':
        path = captured.root / 'unknown.dll'; path.write_bytes(b'unknown')
        captured.modules.append((9, path))
    elif change == 'foreign_root': observation.sys.executable = str(captured.system / 'kernel32.dll')
    elif change == 'duplicate_handle': captured.modules.append((2, captured.modules[0][1]))
    elif change == 'duplicate_path': captured.modules.append((9, captured.modules[1][1]))
    elif change == 'bool_handle': captured.modules[1] = (True, captured.modules[1][1])
    elif change == 'missing_executable': captured.modules.pop(0)
    else: captured.classifications[TARGETS[0]] = 'unknown'
    with pytest.raises(NativeExecutionUnsupported, match='audio_context_observation_unsupported'):
        observation.capture_audio_contexts(captured.reader, captured.inventory)
    assert captured.calls == []


@pytest.mark.parametrize('change', ['foreign_metadata', 'missing_metadata', 'effective_root', 'associated_query',
    'effective_query', 'snapshot', 'source', 'policy', 'inventory', 'system_directory', 'executable'])
def test_during_query_changes_rejected(captured, change):
    original = captured.reader._metadata
    count = 0
    def query(handle, *, flags):
        nonlocal count
        count += 1
        value = original(handle, flags=flags)
        if change == 'foreign_metadata' and flags == IS_MODULE: return replace(value, root_manifest='foreign')
        if change == 'missing_metadata' and flags == IS_MODULE: return None
        if change == 'effective_root' and flags == USE_ACTIVE: return replace(value, root_manifest='foreign')
        if change == 'associated_query' and count > 3 and flags == IS_MODULE: return replace(value, flags=1)
        if change == 'effective_query' and count > 1 and flags == USE_ACTIVE: return replace(value, flags=1)
        if count == 1:
            if change == 'snapshot': captured.modules.pop()
            elif change == 'source': (captured.root / TARGETS[0]).write_bytes(b'changed after hash')
            elif change == 'policy': captured.policy.identity_sha256 = 'c' * 64
            elif change == 'inventory': captured.inventory.files = captured.inventory.files[:1]
            elif change == 'system_directory': captured.reader._system_directory = captured.root
            elif change == 'executable': observation.sys.executable = str(captured.root / 'foreign.exe')
        return value
    captured.reader._metadata = query
    with pytest.raises(NativeExecutionUnsupported):
        observation.capture_audio_contexts(captured.reader, captured.inventory)


def test_real_synthetic_pe_classification_with_fake_queries(exact_graph, monkeypatch):
    """Use unmocked audio resource/import parsers, not just classifier fakes."""
    root = exact_graph.root
    system = root / 'synthetic-system'
    system.mkdir()
    files = tuple(RuntimeInventoryFile(name=row.name, role=row.role,
        size_bytes=row.size_bytes, sha256=row.sha256) for row in exact_graph.inventory.files)
    files += (RuntimeInventoryFile(name='entry.py', role='entrypoint', size_bytes=1, sha256='a' * 64),)
    inventory = RuntimeInventory(inventory_version=1,
        directories=exact_graph.inventory.directories, files=files)
    modules = tuple((index, root / row.name) for index, row in enumerate(exact_graph.inventory.files, 1))
    by_handle = dict(modules)
    monkeypatch.setattr(observation, 'sys', SimpleNamespace(executable=str(root / 'python.exe')))
    def query(handle, *, flags):
        source = root / 'python.exe' if flags == USE_ACTIVE else by_handle[handle]
        return ActivationMetadata(1, str(source), '', str(source.parent), (2, 1, 2), (), 0)
    reader = SimpleNamespace(_system_directory=system,
        _modules=SimpleNamespace(_snapshot=lambda: modules), _metadata=query)
    result = observation.capture_audio_contexts(reader, inventory)
    assert {path for path, _ in result.contexts.associated} == {
        str(root / slot) for slot in ('python312.dll', *TARGETS)}
    assert result.dispatch_authorized is False
