"""Synthetic exact-form files and fake ActCtx ABI, no native execution."""
import ctypes as ct
import copy
import hashlib

import pytest

from app.providers import windows_activation as act, cpu_native_recipe as cpu
from app.providers.runtime_primitives import NativeExecutionUnsupported
from test_windows_activation import ABI
from test_pe_resources import resource_image


class RootABI(ABI):
    def QueryActCtxW(self, flags, handle, index, info, buffer, size, required):
        assert flags == 0 and handle == 99 and info in (2, 3)
        self.calls.append(('query', info, buffer is not None))
        if buffer is None:
            required._obj.value = 4096 if self.problem != 'oversize' else act.BUFFER_LIMIT + 1
            return int(self.problem == 'first_success')
        if self.problem == 'second_failure': return 0
        required._obj.value = 4096
        structure = act._Detail if info == 2 else act._Assembly
        row = structure.from_buffer(buffer)
        offset = ct.sizeof(structure)
        def string(name, value, length, chars=False):
            nonlocal offset
            raw = value.encode('utf-16-le')
            ct.memmove(ct.addressof(buffer)+offset, raw+b'\0\0', len(raw)+2)
            setattr(row, name, ct.addressof(buffer)+offset)
            setattr(row, length, len(raw)//2 if chars else len(raw))
            offset += len(raw)+2
        if info == 2:
            row.format, row.assemblies = 1, 2 if self.problem == 'roster' else 1
            row.manifest_type, row.configuration_type, row.directory_type = 2, 1, 2
            string('manifest', str(self.source), 'manifest_chars', True)
            string('directory', str(self.source.parent), 'directory_chars', True)
            if self.problem == 'pointer': row.manifest = 1
            if self.problem == 'flags': row.flags = 1
        else:
            assert index._obj.value == 1
            row.manifest_type, row.policy_type = 2, 1
            row.manifest_major, row.flags = 1, 17
            string('manifest', str(self.source), 'manifest_bytes')
            if self.problem == 'files': row.files = 1
            if self.problem == 'unstable' and len(self.calls) > 6: row.flags = 18
        return 1


@pytest.fixture(params=[0, 1, 2])
def setup(request, tmp_path, monkeypatch):
    policy, _ = cpu._owned()
    selected = copy.deepcopy(policy)
    form_id = sorted(policy['manifests'])[request.param]
    form = policy['manifests'][form_id]
    raw = resource_image([(24,2,1033,bytes.fromhex(form['hex']),form['codepage'])])
    source = tmp_path / 'representative.bin'; source.write_bytes(raw)
    member = next(m for m in selected['members'] if m['manifest_sha256'] == form_id)
    member.update(size_bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())
    monkeypatch.setattr(cpu, '_owned', lambda: (selected, 'f'*64))
    reader = object.__new__(act.WindowsActivationReader)
    reader._kernel, reader._last_error = RootABI(source), lambda: 122
    return reader, source, member, selected


def test_three_forms_repeat_root_only_with_exact_budget(setup):
    reader, source, member, _ = setup
    result = reader.inspect_cpu_pe(source, member['name'])
    assert not result.dispatch_authorized and result.root_flags == 17
    assert result.source_sha256 == member['sha256']
    assert reader._kernel.calls == [('create',9,9,0,2)] + [
        ('query',2,False),('query',2,True),('query',3,False),('query',3,True)]*2 + [('release',99)]


@pytest.mark.parametrize('problem',['create','oversize','first_success','second_failure',
    'roster','pointer','flags','files','unstable'])
def test_failure_releases_once_and_never_queries_files_or_extra_roster(setup,problem):
    reader, source, member, _ = setup
    reader._kernel.problem = problem
    with pytest.raises(NativeExecutionUnsupported): reader.inspect_cpu_pe(source,member['name'])
    calls = reader._kernel.calls
    assert sum(c[0]=='create' for c in calls)==1
    assert sum(c[0]=='release' for c in calls)==int(problem!='create')
    assert sum(c[0]=='query' for c in calls)<=8
    if problem=='roster': assert len(calls)==4


def test_changed_source_rejected_before_create(setup):
    reader,source,member,_=setup
    source.write_bytes(source.read_bytes()+b'changed')
    with pytest.raises(NativeExecutionUnsupported): reader.inspect_cpu_pe(source,member['name'])
    assert not reader._kernel.calls


def test_changed_source_during_query_stops_after_release(setup,monkeypatch):
    reader,source,member,_=setup
    original=reader._kernel.ReleaseActCtx
    def release(handle):
        original(handle)
        source.write_bytes(source.read_bytes()+b'changed')
    monkeypatch.setattr(reader._kernel,'ReleaseActCtx',release)
    with pytest.raises(NativeExecutionUnsupported): reader.inspect_cpu_pe(source,member['name'])
    assert reader._kernel.calls[-1]==('release',99)


def test_release_failure_has_no_prediction(setup,monkeypatch):
    reader,source,member,_=setup
    def release(handle): raise OSError('synthetic unavailable')
    monkeypatch.setattr(reader._kernel,'ReleaseActCtx',release)
    with pytest.raises(NativeExecutionUnsupported,match='release_unavailable'):
        reader.inspect_cpu_pe(source,member['name'])


def test_recipe_change_during_metadata_stops(setup,monkeypatch):
    reader,source,member,policy=setup
    original=reader._kernel.ReleaseActCtx
    def release(handle):
        original(handle)
        monkeypatch.setattr(cpu,'_owned',lambda:(policy,'e'*64))
    monkeypatch.setattr(reader._kernel,'ReleaseActCtx',release)
    with pytest.raises(NativeExecutionUnsupported): reader.inspect_cpu_pe(source,member['name'])


def test_unknown_member_has_zero_api_calls(setup):
    reader,source,_,_=setup
    with pytest.raises(NativeExecutionUnsupported): reader.inspect_cpu_pe(source,'unknown.pyd')
    assert not reader._kernel.calls
