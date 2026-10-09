"""Fresh-child query selection with fake handles/APIs and fixed temp bytes."""
from dataclasses import replace
import hashlib
from types import SimpleNamespace

import pytest

from app.providers.windows_activation import WindowsActivationReader
from app.providers.runtime_primitives import NativeExecutionUnsupported
from test_component_policy2 import policy2
from test_pe_resources import manifest_image, resource_image


@pytest.fixture
def private_contexts(policy2, monkeypatch):
    metadata, kwargs, _ = policy2
    root = kwargs['application_directory']
    exe = root / 'python.exe'; exe.write_bytes(manifest_image(exe=True))
    active = root / 'python312.dll'; active.write_bytes(manifest_image())
    inactive = root / 'plain.dll'; inactive.write_bytes(resource_image())
    system = kwargs['windows_root'] / 'System32'; system.mkdir()
    os_file = system / 'kernel32.dll'; os_file.write_bytes(b'trusted substrate placeholder')
    monkeypatch.setattr('sys.executable', str(exe))
    reader = object.__new__(WindowsActivationReader)
    reader._system_directory = system
    baseline = ((1, exe), (2, active), (3, inactive), (4, os_file))
    reader._modules = SimpleNamespace(_snapshot=lambda: baseline)
    calls = []
    effective = replace(metadata, root_manifest=str(exe),
        assemblies=(replace(metadata.assemblies[0], manifest=str(exe)), *metadata.assemblies[1:]))
    associated = replace(metadata, root_manifest=str(active),
        assemblies=(replace(metadata.assemblies[0], manifest=str(active)), *metadata.assemblies[1:]))
    def query(handle, *, flags=0):
        calls.append((handle, flags))
        if (handle, flags) == (None, 4): return effective
        if (handle, flags) == (2, 8): return associated
        pytest.fail('No query of EXE default as module, resource-free DLL or OS context')
    reader._metadata = query
    files = tuple((path.name, hashlib.sha256(path.read_bytes()).hexdigest()) for path in (exe, active, inactive))
    return reader, files, calls, baseline, effective, associated


def test_only_relevant_private_resource2_has_associated_query(private_contexts):
    reader, files, calls, _, effective, associated = private_contexts
    result = reader.inspect_private_contexts(files)
    assert result.effective == effective and result.associated == ((associated.root_manifest, associated),)
    assert calls == [(None, 4), (2, 8), (None, 4), (2, 8), (None, 4)]
    assert not result.dispatch_authorized


@pytest.mark.parametrize('problem', ['source_changed', 'dll_changed', 'unlisted_private', 'outside',
    'effective_wrong_root', 'associated_wrong_root', 'query_error', 'effective_changed',
    'associated_changed', 'snapshot_changed', 'snapshot_missing_exe', 'unsafe_relative', 'missing_exe', 'duplicate'])
def test_context_selection_stops_without_inventing_absence(private_contexts, problem):
    reader, files, calls, baseline, effective, associated = private_contexts
    if problem == 'source_changed': baseline[0][1].write_bytes(b'changed')
    if problem == 'dll_changed': baseline[1][1].write_bytes(b'changed')
    if problem == 'unlisted_private': files = tuple(row for row in files if row[0] != 'python312.dll')
    if problem == 'missing_exe': files = files[1:]
    if problem == 'duplicate': files = (*files, files[0])
    if problem == 'unsafe_relative': files = (*files, ('../outside.dll', '0'*64))
    if problem == 'outside': reader._modules._snapshot = lambda: (*baseline, (5, baseline[0][1].parent.parent / 'outside.dll'))
    if problem == 'snapshot_missing_exe': reader._modules._snapshot = lambda: baseline[1:]
    if problem == 'snapshot_changed':
        count = [0]
        def snapshot():
            count[0] += 1
            return baseline if count[0] == 1 else tuple(reversed(baseline))
        reader._modules._snapshot = snapshot
    original = reader._metadata
    hits = {}
    def query(handle, *, flags=0):
        key = (handle, flags); hits[key] = hits.get(key, 0) + 1
        if key == (2, 8) and problem == 'query_error': raise OSError()
        value = original(handle, flags=flags)
        if (problem == 'effective_wrong_root' and flags == 4) or (problem == 'associated_wrong_root' and flags == 8):
            return replace(value, root_manifest='unverified-root')
        if (problem == 'effective_changed' and flags == 4 and hits[key] > 1) or (
                problem == 'associated_changed' and flags == 8 and hits[key] > 1):
            return replace(value, flags=17)
        return value
    reader._metadata = query
    with pytest.raises(NativeExecutionUnsupported, match='private_context_snapshot_unsupported'):
        reader.inspect_private_contexts(files)
