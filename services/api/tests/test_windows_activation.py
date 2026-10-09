"""Synthetic PE/temp files and fake ABI only: no real ActCtx/loader calls."""
import ctypes as ct
import hashlib
from types import SimpleNamespace

import pytest

from app.providers import windows_activation as mod
from app.providers.runtime_primitives import NativeExecutionUnsupported
from test_pe_resources import manifest_image, resource_image


class ABI:
    def __init__(self, source):
        self.source, self.calls, self.problem = source, [], None

    def CreateActCtxW(self, pointer):
        row = pointer._obj
        self.calls.append(('create', row.flags, row.architecture, row.language, row.resource))
        assert row.size == ct.sizeof(mod._ActCtx) and row.source == str(self.source)
        assert not row.assembly_directory and not row.application and not row.module
        if self.problem == 'create': return ct.c_void_p(-1).value
        return 99

    def ReleaseActCtx(self, handle):
        assert handle == 99
        self.calls.append(('release', handle))

    def QueryActCtxW(self, flags, handle, index, information, buffer, size, required):
        assert flags == 0 and handle == 99
        self.calls.append(('query', information, buffer is not None))
        if buffer is None:
            required._obj.value = 1024 if self.problem != 'oversize' else mod.BUFFER_LIMIT + 1
            return int(self.problem == 'first_success')
        if self.problem == 'second_failure': return 0
        required._obj.value = 1024
        if self.problem == 'written': required._obj.value = 1025
        structure = {2: mod._Detail, 3: mod._Assembly, 4: mod._File}[information]
        row = structure.from_buffer(buffer)
        offset = ct.sizeof(structure)
        def string(name, value, length, *, chars=False):
            nonlocal offset
            raw = value.encode('utf-16-le')
            ct.memmove(ct.addressof(buffer) + offset, raw + b'\0\0', len(raw) + 2)
            setattr(row, name, ct.addressof(buffer) + offset)
            setattr(row, length, len(raw) // 2 if chars else len(raw))
            offset += len(raw) + 2
        if information == 2:
            row.format, row.assemblies = 1, 2
            if self.problem == 'observed_root_flags': row.assemblies = 3
            string('manifest', str(self.source), 'manifest_chars', chars=True)
            if self.problem == 'assemblies': row.assemblies = 17
            if self.problem == 'format': row.format = 2
            if self.problem == 'pointer': row.manifest = 1
            if self.problem == 'structure_pointer': row.manifest = ct.addressof(buffer)
            if self.problem == 'unaligned_pointer': row.manifest += 1
            if self.problem == 'length': row.manifest_chars = 0xffffffff
            if self.problem == 'nul':
                ct.memmove(row.manifest, b'\0\0', 2)
            if self.problem == 'surrogate': ct.memmove(row.manifest, b'\0\xd8', 2)
            if self.problem == 'terminator': ct.memmove(row.manifest + row.manifest_chars * 2, b'xx', 2)
        elif information == 3:
            number = index._obj.value
            assert number in ((1, 2, 3) if self.problem == 'observed_root_flags' else (1, 2))
            # Only count3/rootflags17 came from the real record. The unread
            # identities below are deliberately invented, untrusted test data.
            string('identity', '' if number == 1 else 'synthetic untrusted assembly ' + str(number), 'identity_bytes')
            string('manifest', str(self.source), 'manifest_bytes')
            row.files = 1 if number == 2 else 0
            if self.problem == 'observed_root_flags' and number == 1:
                row.flags = 0x11
                row.identity = None
                row.identity_bytes = 0
            if self.problem == 'files': row.files = 129
            if self.problem == 'odd_bytes': row.identity_bytes = 1
        else:
            assert (index._obj.assembly, index._obj.file) == (2, 0)
            string('name', 'comctl32.dll', 'name_bytes')
            string('path', 'synthetic component path', 'path_bytes')
            if self.problem == 'file_flags': row.flags = 1
        if self.problem == 'flags': row.flags = 1
        return 1


@pytest.fixture
def setup(tmp_path):
    source = tmp_path / 'target.dll'
    source.write_bytes(manifest_image())
    reader = object.__new__(mod.WindowsActivationReader)
    reader._kernel = ABI(source)
    reader._last_error = lambda: 122
    return reader, source, hashlib.sha256(source.read_bytes()).hexdigest()


def test_supported_amd64_structure_layouts():
    assert ct.sizeof(ct.c_void_p) == 8
    assert [ct.sizeof(x) for x in (mod._ActCtx, mod._Detail, mod._Assembly, mod._File, mod._Index)] == [56, 64, 104, 32, 8]
    assert mod._Assembly.identity.offset == 64


def test_explicit_resource_no_activation_no_target_load_and_release(setup):
    reader, source, digest = setup
    result = reader.inspect_pe(source, digest)
    assert result.root_manifest == str(source)
    assert not result.dispatch_authorized
    assert result.assemblies[1].files[0].name == 'comctl32.dll'
    assert reader._kernel.calls == [('create', 11, 9, 1033, 2),
        ('query', 2, False), ('query', 2, True), ('query', 3, False), ('query', 3, True),
        ('query', 3, False), ('query', 3, True), ('query', 4, False), ('query', 4, True), ('release', 99)]


def test_current_ui_language_is_explicit_and_does_not_set_process_default(setup):
    reader, source, digest = setup
    reader.inspect_pe(source, digest, language_policy='current_ui')
    assert reader._kernel.calls[0] == ('create', 9, 9, 0, 2)
    assert reader._kernel.calls[-1] == ('release', 99)


@pytest.mark.parametrize('language', ['zh-CN', 'en', '', None, True])
def test_unknown_language_policy_has_no_api_calls(setup, language):
    reader, source, digest = setup
    with pytest.raises(NativeExecutionUnsupported):
        reader.inspect_pe(source, digest, language_policy=language)
    assert not reader._kernel.calls


def test_exe_uses_resource_one(setup):
    reader, source, _ = setup
    source.write_bytes(manifest_image(exe=True))
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    reader.inspect_pe(source, digest, role='bootstrap_exe')
    assert reader._kernel.calls[0][-1] == 1


@pytest.mark.parametrize('problem', ['oversize', 'first_success', 'second_failure', 'written',
    'assemblies', 'format', 'pointer', 'structure_pointer', 'unaligned_pointer', 'length',
    'nul', 'surrogate', 'terminator', 'files', 'odd_bytes'])
def test_bounded_failures_release_created_context_once(setup, problem):
    reader, source, digest = setup
    reader._kernel.problem = problem
    with pytest.raises(NativeExecutionUnsupported, match='activation_metadata_unsupported'):
        reader.inspect_pe(source, digest)
    assert reader._kernel.calls[-1] == ('release', 99)
    assert reader._kernel.calls.count(('release', 99)) == 1


@pytest.mark.parametrize('problem', ['hash', 'role', 'data', 'create', 'error'])
def test_source_and_api_failures_never_advance(setup, problem):
    reader, source, digest = setup
    if problem == 'hash': digest = '0' * 64
    if problem == 'data': source.write_bytes(resource_image()); digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if problem == 'create': reader._kernel.problem = problem
    if problem == 'error': reader._last_error = lambda: 5
    with pytest.raises(NativeExecutionUnsupported, match='activation_metadata_unsupported'):
        reader.inspect_pe(source, digest, role='bootstrap_exe' if problem == 'role' else 'dll')
    if problem in ('hash', 'role', 'data'): assert not reader._kernel.calls
    if problem == 'create': assert len(reader._kernel.calls) == 1
    if problem == 'error': assert reader._kernel.calls[-1] == ('release', 99)


def test_source_changed_after_query_stops_and_releases(setup, monkeypatch):
    reader, source, digest = setup
    original = reader._metadata
    def mutate(handle):
        result = original(handle)
        source.write_bytes(b'changed')
        return result
    monkeypatch.setattr(reader, '_metadata', mutate)
    with pytest.raises(NativeExecutionUnsupported): reader.inspect_pe(source, digest)
    assert reader._kernel.calls[-1] == ('release', 99)


def test_no_production_abi_injection_constructor_and_missing_api(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, 'WindowsModuleReader', lambda system: SimpleNamespace(_kernel=object()))
    monkeypatch.setattr(ct, 'get_last_error', lambda: 122, raising=False)
    with pytest.raises(NativeExecutionUnsupported, match='activation_api_unavailable'):
        mod.WindowsActivationReader(tmp_path)


def test_context_snapshot_uses_owned_handles_and_explicit_effective_flag(setup, monkeypatch):
    reader, source, digest = setup
    sample = reader.inspect_pe(source, digest)
    reader._modules = SimpleNamespace(_snapshot=lambda: ((77, source),))
    calls = []
    def metadata(handle, *, flags=0):
        calls.append((handle, flags))
        return sample
    monkeypatch.setattr(reader, '_metadata', metadata)
    snapshot = reader.inspect_current_contexts()
    assert calls == [(None, 4), (77, 8), (None, 4)]
    assert snapshot.associated == ((str(source), sample),) and not snapshot.dispatch_authorized
    assert reader._kernel.calls.count(('release', 99)) == 1  # Only the created context is owned.


@pytest.mark.parametrize('problem', ['count', 'modules_changed', 'effective_changed', 'missing_context'])
def test_snapshot_changes_and_unavailable_contexts_stop(setup, monkeypatch, problem):
    reader, source, digest = setup
    sample = reader.inspect_pe(source, digest)
    snapshots = [((77, source),), ((78, source),) if problem == 'modules_changed' else ((77, source),)]
    reader._modules = SimpleNamespace(_snapshot=lambda: snapshots.pop(0))
    if problem == 'count': reader._modules._snapshot = lambda: tuple((i + 1, source) for i in range(65))
    count = 0
    def metadata(handle, *, flags=0):
        nonlocal count
        count += 1
        if problem == 'missing_context': raise ValueError()
        if problem == 'effective_changed' and count == 3: return None
        return sample
    monkeypatch.setattr(reader, '_metadata', metadata)
    with pytest.raises(NativeExecutionUnsupported, match='context_snapshot_unsupported'):
        reader.inspect_current_contexts()


def test_release_failure_is_named_and_never_returns_metadata(setup, monkeypatch):
    reader, source, digest = setup
    def release(handle): raise OSError('unsafe platform detail')
    monkeypatch.setattr(reader._kernel, 'ReleaseActCtx', release)
    with pytest.raises(NativeExecutionUnsupported, match='activation_release_unavailable'):
        reader.inspect_pe(source, digest)


def test_32bit_reader_stops_before_os_api(monkeypatch, tmp_path):
    monkeypatch.setattr(ct, 'sizeof', lambda value: 4)
    def forbidden(system): pytest.fail('must stop before any API initialization')
    monkeypatch.setattr(mod, 'WindowsModuleReader', forbidden)
    with pytest.raises(NativeExecutionUnsupported, match='activation_architecture_unsupported'):
        mod.WindowsActivationReader(tmp_path)


def test_observed_three_assembly_root_flags_decode_without_authority(setup):
    """Known real shape plus synthetic unread rows; not a real roster replay."""
    reader, source, _ = setup
    source.write_bytes(manifest_image(exe=True))
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    reader._kernel.problem = 'observed_root_flags'
    result = reader.inspect_pe(source, digest, role='bootstrap_exe')
    assert len(result.assemblies) == 3 and result.assemblies[0].flags == 0x11
    assert result.assemblies[0].identity == '' and not result.dispatch_authorized
    assert reader._kernel.calls == [('create', 11, 9, 1033, 1),
        ('query', 2, False), ('query', 2, True), ('query', 3, False),
        ('query', 3, True), ('query', 3, False), ('query', 3, True),
        ('query', 4, False), ('query', 4, True), ('query', 3, False),
        ('query', 3, True), ('release', 99)]


@pytest.mark.parametrize('problem', ['flags', 'file_flags'])
def test_nonzero_flags_retained_as_diagnostic_metadata_only(setup, problem):
    from dataclasses import asdict
    reader, source, digest = setup
    reader._kernel.problem = problem
    result = reader.inspect_pe(source, digest)
    assert not result.dispatch_authorized
    values = asdict(result)
    assert values['assemblies'][1]['files'][0]['flags'] == 1
    if problem == 'flags':
        assert values['flags'] == 1 and values['assemblies'][0]['flags'] == 1
    else:
        assert values['flags'] == values['assemblies'][0]['flags'] == 0
    assert reader._kernel.calls[-1] == ('release', 99)


def test_nonzero_flags_do_not_skip_string_pointer_validation(setup, monkeypatch):
    reader, source, digest = setup
    original = reader._kernel.QueryActCtxW
    def query(flags, handle, index, info, buffer, size, required):
        result = original(flags, handle, index, info, buffer, size, required)
        if info == 3 and buffer is not None and result:
            row = mod._Assembly.from_buffer(buffer)
            row.flags, row.identity = 0x11, 1
        return result
    monkeypatch.setattr(reader._kernel, 'QueryActCtxW', query)
    with pytest.raises(NativeExecutionUnsupported, match='activation_metadata_unsupported'):
        reader.inspect_pe(source, digest)
    assert reader._kernel.calls[-1] == ('release', 99)
