"""New recipe factory/one-use transport with fake metadata/subprocess only."""
from dataclasses import replace
import copy
import json
from types import SimpleNamespace

import pytest

from app.providers import component_probe as probe, host_runtime, python_origin_probe as transport
from app.providers import python_origin_child as child, windows_native
from app.providers.windows_os_policy import parse_os_policy, owned_os_policy_bytes
from app.providers.runtime_primitives import ExecutionPreparationError
from test_component_policy2 import policy2
from test_pe_resources import manifest_image


@pytest.fixture
def prepared_layout(policy2, tmp_path, monkeypatch):
    metadata, kwargs, paths = policy2
    base, staging, work = (tmp_path / name for name in ('base', 'staging', 'work'))
    for path in (base / 'Lib/encodings', base / 'Lib/site-packages', base / 'DLLs', staging, work):
        path.mkdir(parents=True)
    (base / 'Lib/os.py').write_bytes(b'pass\n')
    (base / 'Lib/encodings/__init__.py').write_bytes(b'pass\n')
    for name in ('python.exe', 'python312.dll', 'python3.dll', 'vcruntime140.dll', 'vcruntime140_1.dll'):
        (base / name).write_bytes(manifest_image(exe=True) if name == 'python.exe' else b'fake binary')
    system = kwargs['windows_root'] / 'System32'; system.mkdir()
    for name in parse_os_policy(owned_os_policy_bytes()).component_names:
        (system / name).write_bytes(b'fake OS')
    facts = windows_native.WindowsFacts(kwargs['windows_root'], 0x8664, 0, 26100, 1, system)
    monkeypatch.setattr(host_runtime.sys, 'platform', 'win32')
    monkeypatch.setattr(host_runtime, '_read_windows_facts', lambda: facts)
    monkeypatch.setattr(probe, '_read_windows_facts', lambda: facts)
    class Activation:
        def __init__(self, directory): assert directory == system
        def inspect_pe(self, source, digest, *, role, language_policy):
            assert language_policy == 'current_ui'
            assert role == 'bootstrap_exe'
            return replace(metadata, root_manifest=str(source), application_directory=str(source.parent),
                assemblies=(replace(metadata.assemblies[0], manifest=str(source)), *metadata.assemblies[1:]))
    monkeypatch.setattr(probe, 'WindowsActivationReader', Activation)
    return dict(base=base, staging_parent=staging, work_directory=work), facts, paths


def response(request):
    effective = copy.deepcopy(request['host_runtime']['binding'])
    effective['query_role'] = 'effective'
    return dict(probe_version=3, nonce=request['nonce'], passed=True, stage='complete',
        inventory_sha256=__import__('hashlib').sha256(child.canonical(request['inventory'])).hexdigest(),
        host_runtime=request['host_runtime'], native_count=1, python_count=1, code='', unknown_module='',
        blocked_origins=[], effective=effective, associated=[])


def test_factory_full_tree_owned_recipe_fixed_transport_one_use(prepared_layout, monkeypatch):
    selection, facts, paths = prepared_layout
    calls = []
    def run(argv, **kwargs):
        request = json.loads(kwargs['input'])
        assert request['probe_version'] == 3 and request['host_runtime']['component_policy_version'] == 2
        names = {row['name'] for row in request['inventory']['files']}
        assert 'Lib/site-packages/_content_os_host/component_child.py' in names
        assert not any('/app/' in name and name.endswith('.py') for name in names)
        assert kwargs['shell'] is False and kwargs['timeout'] == 15
        assert not {'PATH', 'PYTHONPATH', 'PYTHONHOME'} & set(kwargs['env'])
        kwargs['stdout'].write(child.canonical(response(request)))
        calls.append(argv)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(transport.subprocess, 'run', run)
    with probe.prepare_component_probe(**selection) as command:
        assert command.host_observation.component_policy_version == 2
        assert command.run_probe().passed
        with pytest.raises(ExecutionPreparationError, match='consumed'): command.run_probe()
    assert len(calls) == 1 and not list(selection['staging_parent'].iterdir())


def test_changed_owned_helper_stops_before_launch_and_consumes(prepared_layout, monkeypatch):
    selection, facts, paths = prepared_layout
    monkeypatch.setattr(transport.subprocess, 'run', lambda *args, **kwargs: pytest.fail('changed helper must never launch'))
    with probe.prepare_component_probe(**selection) as command:
        (command.invocation.cwd / 'Lib/site-packages/_content_os_host/component_child.py').write_bytes(b'changed')
        with pytest.raises(ExecutionPreparationError, match='fixed_runtime_changed'): command.run_probe()
        with pytest.raises(ExecutionPreparationError, match='consumed'): command.run_probe()


def test_independent_host_facts_must_match_base_host_and_cleanup(prepared_layout, monkeypatch):
    selection, facts, paths = prepared_layout
    monkeypatch.setattr(probe, '_read_windows_facts', lambda: replace(facts, revision=facts.revision + 1))
    with pytest.raises(ExecutionPreparationError, match='host_runtime_changed'):
        probe.prepare_component_probe(**selection)
    assert not list(selection['staging_parent'].iterdir())


@pytest.mark.parametrize('change', ['nonce', 'legacy_version', 'digest', 'host', 'effective_role',
    'effective_source', 'missing_effective', 'associated_source', 'post_tree', 'post_manifest', 'timeout'])
def test_protocol_or_post_launch_change_consumes_without_retry(prepared_layout, monkeypatch, change):
    selection, facts, paths = prepared_layout
    calls = []
    def run(argv, **kwargs):
        request = json.loads(kwargs['input']); report = response(request); calls.append(argv)
        if change == 'nonce': report['nonce'] = '0'*64
        if change == 'legacy_version': report['probe_version'] = 2
        if change == 'digest': report['inventory_sha256'] = '0'*64
        if change == 'host': report['host_runtime']['revision'] += 1
        if change == 'effective_role': report['effective']['query_role'] = 'created'
        if change == 'effective_source': report['effective']['source_sha256'] = '0'*64
        if change == 'missing_effective': report['effective'] = None
        if change == 'associated_source':
            binding = copy.deepcopy(report['effective']); binding.update(query_role='associated', source_role='dll', source_sha256='0'*64)
            report['associated'] = [dict(name='python312.dll', binding=binding)]
        if change == 'post_tree': (kwargs['cwd'] / 'new.py').write_bytes(b'changed')
        if change == 'post_manifest': paths[0][1].write_bytes(b'changed definition')
        if change == 'timeout': raise transport.subprocess.TimeoutExpired(argv, 15)
        kwargs['stdout'].write(child.canonical(report))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(transport.subprocess, 'run', run)
    with probe.prepare_component_probe(**selection) as command:
        with pytest.raises((ExecutionPreparationError, ValueError)): command.run_probe()
        with pytest.raises(ExecutionPreparationError, match='consumed'): command.run_probe()
    assert len(calls) == 1 and not list(selection['staging_parent'].iterdir())
