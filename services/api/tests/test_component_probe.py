"""Policy2 prepared/owned-child integration: only synthetic files/fake APIs."""
from dataclasses import replace, asdict
import os
import hashlib
import importlib
import io
import json
import sys
from types import SimpleNamespace, ModuleType

import pytest

from app.providers import component_probe as probe, component_child, component_contexts, python_origin_child as child
from app.providers import windows_native, runtime_primitives
from app.providers.runtime_primitives import NativeExecutionUnsupported, ExecutionPreparationError
from app.providers.windows_component_policy import owned_component_policy_digest
from app.providers.runtime_tree import prepare_runtime_tree, OwnedRuntimeFile
from app.domain.execution_runtime import HostRuntimeObservationV3Policy2
from test_private_context_reader import private_contexts
from test_component_policy2 import policy2
from test_runtime_tree import layout
from test_pe_resources import manifest_image


@pytest.fixture
def contexts(private_contexts):
    reader, files, calls, baseline, effective, associated = private_contexts
    root = baseline[0][1].parent
    def inspect(source, digest, *, role, language_policy):
        assert role == 'bootstrap_exe' and language_policy == 'current_ui'
        return effective
    reader.inspect_pe = inspect
    prediction = component_contexts.created_binding(reader, root, files, reader._system_directory.parent)
    return reader, root, files, prediction


def test_normalized_cross_role_binding_and_opaque_root_flags(contexts):
    reader, root, files, prediction = contexts
    effective, associated = component_contexts.compare_current(reader, root, files, reader._system_directory.parent, prediction)
    assert effective.query_role == 'effective' and associated[0][1].query_role == 'associated'
    assert component_contexts.normalized_binding(replace(effective, root_flags=101)) == component_contexts.normalized_binding(prediction)
    assert prediction.component_policy_sha256 == owned_component_policy_digest()


@pytest.mark.skipif(os.name != 'nt', reason='Windows loaded-module path spelling')
def test_associated_path_case_preserves_inventory_name_and_actual_source(contexts):
    reader, root, files, prediction = contexts
    original = reader.inspect_private_contexts
    def observed(private_files):
        snapshot = original(private_files)
        name, metadata = snapshot.associated[0]
        reported = name.replace('python312.dll', 'PYTHON312.DLL')
        metadata = replace(metadata, root_manifest=reported,
            assemblies=(replace(metadata.assemblies[0], manifest=reported), *metadata.assemblies[1:]))
        return replace(snapshot, associated=((reported, metadata),))
    reader.inspect_private_contexts = observed
    effective, associated = component_contexts.compare_current(reader, root, files, reader._system_directory.parent, prediction)
    assert associated[0][0] == 'python312.dll'
    assert associated[0][1].source_sha256 == dict(files)['python312.dll']


@pytest.mark.parametrize('change', ['hash', 'language', 'raw_language'])
def test_cross_role_different_resolved_file_is_not_normalized_away(contexts, change):
    reader, root, files, prediction = contexts
    wrong = replace(prediction, common_controls=replace(prediction.common_controls,
        file=replace(prediction.common_controls.file, sha256='0'*64)))
    if change != 'hash':
        key = 'language' if change == 'language' else 'encoded_language'
        wrong = replace(prediction, common_controls=replace(prediction.common_controls,
            identity=replace(prediction.common_controls.identity, **{key: 'fr-FR'})))
    with pytest.raises(NativeExecutionUnsupported, match='context_mismatch') as error:
        component_contexts.compare_current(reader, root, files, reader._system_directory.parent, wrong)
    assert error.value.component_failure['checkpoint'] == 'binding_mismatch'
    assert error.value.component_failure['role'] == 'effective'


@pytest.mark.parametrize('directory', ['source_parent', 'exe_parent', 'foreign'])
def test_nested_associated_context_requires_its_own_source_directory(contexts, directory):
    reader, root, files, prediction = contexts
    source = root / 'DLLs' / 'pyexpat.pyd'
    source.parent.mkdir()
    source.write_bytes((root / 'python312.dll').read_bytes())
    files = (*files, ('DLLs/pyexpat.pyd', hashlib.sha256(source.read_bytes()).hexdigest()))
    original = reader.inspect_private_contexts
    def observed(private_files):
        snapshot = original(private_files)
        _, metadata = snapshot.associated[0]
        app_dir = {'source_parent': source.parent, 'exe_parent': root, 'foreign': root.parent}[directory]
        metadata = replace(metadata, root_manifest=str(source), application_directory=str(app_dir),
            assemblies=(replace(metadata.assemblies[0], manifest=str(source)), *metadata.assemblies[1:]))
        return replace(snapshot, associated=((str(source), metadata),))
    reader.inspect_private_contexts = observed
    if directory == 'source_parent':
        _, associated = component_contexts.compare_current(reader, root, files, reader._system_directory.parent, prediction)
        assert associated[0][0] == 'DLLs/pyexpat.pyd'
    else:
        with pytest.raises(NativeExecutionUnsupported):
            component_contexts.compare_current(reader, root, files, reader._system_directory.parent, prediction)


@pytest.mark.parametrize('owner', [False, True, 'native'])
def test_owned_bundle_is_importable_without_app_or_third_party(tmp_path, monkeypatch, owner):
    files = (probe.owned_native_load_probe_files() if owner == 'native' else
        probe.owned_component_owner_probe_files() if owner else probe.owned_component_probe_files())
    assert 8 < len(files) <= 24
    package = tmp_path / '_content_os_host'
    package.mkdir()
    for row in files:
        if '/_content_os_host/' in row.destination:
            (package / row.destination.rsplit('/', 1)[-1]).write_bytes(row.payload)
    # Deliberately poison providers/dependency names; owned imports must not touch them.
    monkeypatch.syspath_prepend(str(tmp_path))
    for name in ('_content_os_host', *('_content_os_host.' + p.stem for p in package.glob('*.py'))):
        monkeypatch.delitem(sys.modules, name, raising=False)
    original = __import__('builtins').__import__
    def guarded(name, *args, **kwargs):
        if name.startswith(('app', 'pydantic')) or name in ('prepared', 'runtime_tree'):
            raise AssertionError('owned helper dependency leak: ' + name)
        return original(name, *args, **kwargs)
    monkeypatch.setattr('builtins.__import__', guarded)
    imported = importlib.import_module('_content_os_host.component_child')
    importlib.import_module('_content_os_host.windows_activation')
    if owner: importlib.import_module('_content_os_host.python_owner_origins')
    if owner == 'native': importlib.import_module('_content_os_host.component_load_child')
    assert callable(imported.observe)


def test_legacy_request_reader_rejects_protocol3_unless_owned_branch():
    request = dict(probe_version=3, nonce='a'*64, inventory={}, host_runtime={})
    raw = child.canonical(request)
    with pytest.raises(child.ProbeFailure, match='protocol_invalid'): child.read_request(io.BytesIO(raw))
    assert child.read_request(io.BytesIO(raw), allow_component=True) == request
    with pytest.raises(child.ProbeFailure, match='protocol_invalid'):
        child.read_request(io.BytesIO(child.canonical(dict(request, probe_version=2))), allow_component=True)
    legacy = next(row.payload for row in probe.owned_probe_files() if row.destination == probe.ENTRY)
    new = next(row.payload for row in probe.owned_component_probe_files() if row.destination == probe.ENTRY)
    assert b'COMPONENT_PROTOCOL = False' in legacy and b'COMPONENT_PROTOCOL = True' in new


@pytest.mark.parametrize('version', [3, 5])
def test_component_main_tree_failure_reports_before_any_helper_import(tmp_path, monkeypatch, version):
    request = dict(probe_version=version, nonce='a'*64, inventory={}, host_runtime={})
    output = io.BytesIO()
    monkeypatch.setattr(child, 'sys', SimpleNamespace(stdin=SimpleNamespace(buffer=io.BytesIO(child.canonical(request))),
        stdout=SimpleNamespace(buffer=output)))
    monkeypatch.setattr(child, 'COMPONENT_PROTOCOL', True)
    monkeypatch.setattr(child, 'OWNER_PROTOCOL', version == 5)
    monkeypatch.setattr(child, 'NATIVE_PROTOCOL', version == 5)
    monkeypatch.setattr(child, 'guard_startup', lambda _: str(tmp_path))
    monkeypatch.setattr(child, 'scan_private', lambda *args: (_ for _ in ()).throw(child.ProbeFailure('execution_origin_probe_tree_changed')))
    original = __import__('builtins').__import__
    def guarded(name, *args, **kwargs):
        if name.startswith('_content_os_host'): raise AssertionError('helper imported before full tree validation')
        return original(name, *args, **kwargs)
    monkeypatch.setattr('builtins.__import__', guarded)
    with pytest.raises(SystemExit) as error: child.main()
    assert error.value.code == 1
    report = (probe.NativeLoadProbeReport if version == 5 else probe.ComponentProbeReport).model_validate_json(output.getvalue())
    assert report.stage == 'tree' and not report.passed and not report.dispatch_authorized


def test_new_bundle_bound_does_not_expand_legacy_limit(layout):
    owned = tuple(OwnedRuntimeFile('owned/' + str(i) + '.py', b'pass\n') for i in range(9))
    with pytest.raises(ExecutionPreparationError, match='owned_limit'):
        prepare_runtime_tree(**layout, owned_files=owned)
    with prepare_runtime_tree(**layout, owned_files=owned, owned_recipe='component-probe-v3') as tree:
        assert tree.verify().descriptor.file_count == 14
    with pytest.raises(ExecutionPreparationError, match='owned_limit'):
        prepare_runtime_tree(**layout, owned_files=owned*3, owned_recipe='component-probe-v3')


@pytest.fixture
def setup(contexts, monkeypatch):
    reader, root, files, prediction = contexts
    facts = windows_native.WindowsFacts(reader._system_directory.parent, 0x8664, 0, 26100, 1, reader._system_directory)
    inventory = dict(inventory_version=1, files=[dict(name=name, sha256=digest) for name, digest in files], directories=[])
    request = dict(probe_version=3, nonce='a'*64, inventory=inventory,
        host_runtime=component_contexts.host_observation(facts, prediction))
    trace = []
    scanner = SimpleNamespace(canonical=child.canonical, scan_private=lambda *args: trace.append('tree') or 'b'*64,
        cpu_observation=child.cpu_observation, load_profile=lambda _: frozenset(),
        _exact_path=child._exact_path, blocked_origins=lambda *args: [], ProbeFailure=child.ProbeFailure)
    module_reader = SimpleNamespace(native_origins=lambda: (root / 'python.exe',), restrict_search=lambda: trace.append('search'))
    monkeypatch.setattr(component_child, 'python_origins', lambda _: (root / 'plain.dll',))
    def activation(_): trace.append('activation'); return reader
    return root, request, facts, reader, scanner, module_reader, activation, trace


def run_child(setup):
    root, request, facts, reader, scanner, module_reader, activation, trace = setup
    return component_child.observe(root, request, {}, lambda _: module_reader, lambda: facts, activation, scanner)


def test_child_composes_independent_contexts_origin_recheck_and_typed_protocol(setup):
    report = run_child(setup)
    assert report['passed'], report
    typed = probe.ComponentProbeReport.model_validate_json(child.canonical(report))
    assert typed.effective.query_role == 'effective'
    assert setup[-1] == ['tree', 'activation', 'search', 'tree']
    assert typed.host_runtime.binding.query_role == 'created'


def test_child_compares_actual_json_wire_host_with_dataclass_observation(setup):
    # Actual stdin has JSON arrays; asdict(observation) retains tuples.
    request = setup[1]
    decoded = json.loads(child.canonical(request))
    request.clear()
    request.update(decoded)
    report = run_child(setup)
    assert report['passed'], report['code']


@pytest.mark.parametrize('change', ['build', 'bool', 'missing', 'extra', 'file_hash'])
def test_wire_normalization_keeps_all_host_fields_authoritative(setup, change):
    request = setup[1]
    decoded = json.loads(child.canonical(request))
    host = decoded['host_runtime']
    if change == 'build': host['build'] += 1
    elif change == 'bool': host['component_policy_version'] = True
    elif change == 'missing': del host['binding']['resource']
    elif change == 'extra': host['unexpected'] = None
    else: host['binding']['common_controls']['file']['sha256'] = '0'*64
    request.clear()
    request.update(decoded)
    report = run_child(setup)
    assert not report['passed'] and report['code'] == 'execution_host_runtime_changed'
    assert 'search' not in setup[-1]


@pytest.mark.parametrize('failure', ['tree', 'baseline', 'host', 'effective', 'post_context', 'post_tree'])
def test_child_named_stop_no_search_after_bad_baseline_or_binding(setup, failure):
    root, request, facts, reader, scanner, module_reader, activation, trace = setup
    if failure == 'tree': scanner.scan_private = lambda *args: (_ for _ in ()).throw(child.ProbeFailure('execution_origin_probe_tree_changed'))
    if failure == 'baseline':
        scanner.blocked_origins = lambda *args: [dict(kind='native', name='bad.dll', category='outside',
            origin_id='c'*64, reason='unlisted_origin')]
    if failure == 'host': request['host_runtime']['revision'] += 1
    original = reader._metadata
    if failure == 'effective': reader._metadata = lambda *args, **kwargs: replace(original(*args, **kwargs), flags=1)
    if failure == 'post_context':
        original_search = module_reader.restrict_search
        def search():
            original_search()
            reader._metadata = lambda *args, **kwargs: replace(original(*args, **kwargs), flags=1)
        module_reader.restrict_search = search
    if failure == 'post_tree':
        hits = [0]
        def scan(*args): hits[0] += 1; return ('b' if hits[0] == 1 else 'c')*64
        scanner.scan_private = scan
    report = run_child(setup)
    assert not report['passed'] and report['code'].startswith('execution_')
    probe.ComponentProbeReport.model_validate_json(child.canonical(report))
    if failure in ('tree', 'baseline', 'host', 'effective'): assert 'search' not in trace
    if failure in ('tree', 'baseline'): assert 'activation' not in trace
