"""Static bytes and fake transport only; no native loader or real runtime staging."""
import copy
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.providers import audio_graph_child as child, audio_graph_probe as transport
from app.providers import python_origin_child as scanner, soundfile_binding as binding
from app.providers.component_probe import owned_native_load_probe_files
from app.providers.private_native_recipe import _owned
from app.providers.acquired_native_recipe import owned_acquired_bytes
from app.providers.runtime_primitives import NativeExecutionUnsupported, ExecutionPreparationError


def digest(raw): return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def packet(tmp_path):
    prefix = b'# notice\n' * 161
    block = (b'try:\n    _snd = _ffi.dlopen(_full_path)\n'
        b'except (OSError, ImportError, TypeError):\n' + b'    # branch\n' * 53 +
        b'    _snd = _ffi.dlopen(_explicit_libname)\n')
    upstream = prefix + block + b'\nvalue = 1\n'
    generated = b'from _cffi_backend import FFI\nffi = FFI()\n'
    binder = Path(binding.__file__).read_bytes().replace(binding.UPSTREAM_SHA256.encode(), digest(upstream).encode())
    binder = binder.replace(binding.BLOCK_SHA256.encode(), digest(block).encode())
    core = Path(binding.__file__).with_name('soundfile_handle.py').read_bytes()
    private, _ = _owned()
    private = copy.deepcopy(private)
    sources = {binding.UPSTREAM_SLOT: upstream, binding.GENERATED_SLOT: generated}
    for row in private['sources']:
        if row['slot'] in sources:
            row.update(size_bytes=len(sources[row['slot']]), sha256=digest(sources[row['slot']]))
    # Synthetic native bytes deliberately remain unverified; frozen catalogs are
    # adjusted for this fixture, not used as real-source evidence.
    acquired = json.loads(owned_acquired_bytes())
    for slot in child.TARGETS:
        sources[slot] = b'not executable: ' + slot.encode()
        for catalog in (private, acquired):
            row = next(row for row in catalog['members'] if row['slot'] == slot)
            row.update(size_bytes=len(sources[slot]), sha256=digest(sources[slot]))
    private_raw, acquired_raw = child.canonical(private), child.canonical(acquired)
    provider_dir = Path(binding.__file__).parent
    cpu_raw = (provider_dir / 'cpu_native_recipe.json').read_bytes()
    graph_raw = (provider_dir / 'audio_native_graph.py').read_bytes()
    sources.update({child.PREFIX + 'soundfile_binding.py': binder,
        child.PREFIX + 'soundfile_handle.py': core,
        child.PREFIX + 'private_native_recipe.json': private_raw,
        child.PREFIX + 'acquired_native_recipe.json': acquired_raw,
        child.PREFIX + 'cpu_native_recipe.json': cpu_raw,
        child.PREFIX + 'audio_native_graph.py': graph_raw,
        'Lib/site-packages/app/providers/windows_os_policy.json': child.canonical({'components': [], 'api_contracts': []})})
    for name, raw in sources.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    inventory = {'inventory_version': 1,
        'directories': sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob('*') if path.is_dir()),
        'files': sorted(({'name': name, 'role': 'dependency', 'size_bytes': len(raw), 'sha256': digest(raw)}
            for name, raw in sources.items()), key=lambda row: row['name'])}
    derived = prefix + binding.REPLACEMENT + b'\nvalue = 1\n'
    descriptor = {'recipe': binding.TRANSFORM_VERSION, 'source_kind': 'derived',
        'upstream_sha256': digest(upstream), 'transform_sha256': digest(binding.TRANSFORM_VERSION.encode() +
            digest(block).encode() + binding.REPLACEMENT), 'derived_sha256': digest(derived),
        'binder_sha256': digest(binder), 'handle_core_sha256': digest(core), 'private_policy_sha256': digest(private_raw),
        'dependencies': sorted((row['slot'], row['sha256']) for row in (*private['members'], *private['sources'])
            if row['slot'] in (*child.TARGETS, binding.UPSTREAM_SLOT, binding.GENERATED_SLOT)),
        'terms': [(row['slot'], row['sha256']) for row in private['terms']],
        'dispatch_authorized': False, 'backend_handle_semantics': 'not_verified'}
    preflight = {'recipe': 'soundfile-audio-two-target-preflight-v2',
        'audio_graph_policy_sha256': digest(child.canonical({
            'recipe': 'soundfile-audio-two-target-preflight-v2', 'acquired_sha256': digest(acquired_raw),
            'cpu_sha256': digest(cpu_raw), 'implementation_sha256': digest(graph_raw)})),
        'acquired_descriptor_sha256': digest(acquired_raw), 'private_recipe_sha256': digest(private_raw),
        'inventory_sha256': digest(child.canonical(inventory)), 'soundfile_derivation_sha256': digest(child.canonical(descriptor)),
        'targets': [{'target': slot, 'blocker': 'execution_native_plan_dependency_unknown', 'plan': None}
            for slot in child.TARGETS], 'dispatch_authorized': False}
    return tmp_path, {'probe_version': 6, 'nonce': 'a' * 64, 'inventory': inventory,
        'derivation': json.loads(child.canonical(descriptor)), 'preflight': preflight}


def unknown_graph(*_):
    raise NativeExecutionUnsupported('execution_native_plan_dependency_unknown')


def test_independent_source_transform_and_two_blockers(packet):
    root, request = packet
    report = child.recheck(root, request, scanner.scan_private, unknown_graph, NativeExecutionUnsupported)
    assert report['report_sha256'] == digest(child.canonical(request['preflight']))
    assert report['preflight']['targets'] == request['preflight']['targets']
    assert report['load_attempts'] == 0 and report['dispatch_authorized'] is False


@pytest.mark.parametrize('name', ['soundfile_handle.py', 'soundfile_binding.py',
                                '../../_cffi_backend.cp312-win_amd64.pyd'])
def test_changed_file_rejected_by_full_tree_scan(packet, name):
    root, request = packet
    target = (root / child.PREFIX / name).resolve() if not name.startswith('../') else root / child.TARGETS[0]
    target.write_bytes(target.read_bytes() + b'changed')
    with pytest.raises(RuntimeError, match='tree_changed'):
        child.recheck(root, request, scanner.scan_private, unknown_graph, NativeExecutionUnsupported)


@pytest.mark.parametrize('field', ['binder_sha256', 'handle_core_sha256', 'derived_sha256'])
def test_parent_cannot_change_derivation_identity(packet, field):
    root, request = packet
    request['derivation'][field] = '0' * 64
    with pytest.raises(ValueError, match='derivation_changed'):
        child.recheck(root, request, scanner.scan_private, unknown_graph, NativeExecutionUnsupported)


def test_parent_cannot_erase_blockers_or_change_target(packet):
    root, request = packet
    request['preflight']['targets'][0]['blocker'] = None
    with pytest.raises(ValueError, match='graph_changed'):
        child.recheck(root, request, scanner.scan_private, unknown_graph, NativeExecutionUnsupported)


def test_real_graph_parser_runs_read_only_and_preserves_its_failure(packet):
    from app.providers.native_load import plan_native_graph
    root, request = packet
    inventory = SimpleNamespace(files=tuple(SimpleNamespace(**row) for row in request['inventory']['files']),
                                directories=tuple(request['inventory']['directories']))
    for row in request['preflight']['targets']:
        with pytest.raises(NativeExecutionUnsupported) as failure:
            plan_native_graph(root, inventory, root / row['target'], frozenset())
        row['blocker'] = str(failure.value)
    report = child.recheck(root, request, scanner.scan_private, plan_native_graph, NativeExecutionUnsupported)
    assert report['preflight']['targets'] == request['preflight']['targets']
    assert report['load_attempts'] == 0


def test_successful_static_graph_still_preserves_dynamic_unknown(packet):
    from app.providers.native_load import NativeDependencyPlan, NativeNode
    from app.providers.pe_resources import ResourceClassification
    root, request = packet
    acquired = json.loads((root / child.PREFIX / 'acquired_native_recipe.json').read_bytes())
    plans = {}
    for row in request['preflight']['targets']:
        recipe = next(item for item in acquired['members'] if item['slot'] == row['target'])
        node = NativeNode(row['target'], recipe['size_bytes'], recipe['sha256'], ResourceClassification('none', 0, None))
        edges = tuple((row['target'], name, name) for name in recipe['dependencies'])
        plans[row['target']] = NativeDependencyPlan(row['target'], (node,), edges, (), False, True)
        row.update(blocker=None, plan={'nodes': [(node.name, node.size_bytes, node.sha256, 'none', 0, None)],
            'edges': edges, 'required_loaded_base': (), 'requires_os_sxs_binding': False, 'requires_dynamic_closure': True})
    report = child.recheck(root, request, scanner.scan_private,
        lambda _root, _inventory, target, _names: plans[target.relative_to(root).as_posix()], NativeExecutionUnsupported)
    assert all(row['plan']['requires_dynamic_closure'] for row in report['preflight']['targets'])
    assert report['dispatch_authorized'] is False


def test_isolated_stdlib_child_harness_rechecks_actual_fixture_files(packet):
    import subprocess
    import sys
    from app.providers.native_load import plan_native_graph
    root, request = packet
    inventory = SimpleNamespace(files=tuple(SimpleNamespace(**row) for row in request['inventory']['files']),
                                directories=tuple(request['inventory']['directories']))
    for row in request['preflight']['targets']:
        with pytest.raises(NativeExecutionUnsupported) as failure:
            plan_native_graph(root, inventory, root / row['target'], frozenset())
        row['blocker'] = str(failure.value)
    # An isolated fixture harness, NOT the guarded Windows runtime entry or a
    # real prepared runtime. Native target imports/loads are prohibited.
    code = '''
import builtins, importlib, pathlib, sys, types
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in {'app', 'pydantic', 'numpy', 'torch', 'soundfile', '_soundfile', '_cffi_backend'}:
        raise AssertionError('forbidden import')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
package = types.ModuleType('_fixture_owned')
package.__path__ = [sys.argv[1]]
sys.modules[package.__name__] = package
child = importlib.import_module('_fixture_owned.audio_graph_child')
scanner = importlib.import_module('_fixture_owned.python_origin_child')
graph = importlib.import_module('_fixture_owned.native_load')
errors = importlib.import_module('_fixture_owned.runtime_primitives')
request = child.read_request(sys.stdin.buffer)
report = child.recheck(pathlib.Path(sys.argv[2]), request, scanner.scan_private,
    graph.plan_native_graph, errors.NativeExecutionUnsupported)
sys.stdout.buffer.write(child.canonical(report))
'''
    result = subprocess.run([sys.executable, '-I', '-S', '-B', '-c', code,
        str(Path(child.__file__).parent), str(root)], input=child.canonical(request),
        capture_output=True, timeout=15, check=False)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report['preflight'] == request['preflight'] and report['load_attempts'] == 0


@pytest.mark.parametrize('change', [None, 'bundle', 'argv', 'timeout', 'environment'])
def test_real_parent_verification_requires_exact_opt_in_bundle(tmp_path, change):
    rows = transport.owned_audio_graph_probe_files()
    files = [SimpleNamespace(name=row.destination, role=row.role, size_bytes=len(row.payload), sha256=digest(row.payload))
             for row in rows]
    if change == 'bundle': files[0].sha256 = '0' * 64
    tree = SimpleNamespace(root=tmp_path, inventory=SimpleNamespace(files=tuple(files)), verify=lambda: None)
    invocation = SimpleNamespace(argv=(str(tmp_path / 'python.exe'), '-I', '-S', '-B', str(tmp_path / transport.ENTRY)),
        cwd=tmp_path, timeout_seconds=15, environment=())
    if change == 'argv': invocation.argv = ('other-python',)
    if change == 'timeout': invocation.timeout_seconds = 100
    if change == 'environment': invocation.environment = (('PythonPath', 'outside'),)
    probe = transport.PreparedAudioGraphProbe(tree, SimpleNamespace(verify=lambda: None), invocation, None)
    if change is None: probe.verify()
    else:
        with pytest.raises(ExecutionPreparationError): probe.verify()


def test_host_observation_is_available_to_existing_redirection_guard(tmp_path, monkeypatch):
    from app.providers import windows_loader, windows_os_policy
    from app.providers.windows_os_policy import owned_os_policy_bytes
    rows = transport.owned_audio_graph_probe_files()
    inventory = SimpleNamespace(files=tuple(SimpleNamespace(name=row.destination, role=row.role,
        size_bytes=len(row.payload), sha256=digest(row.payload)) for row in rows), directories=())
    tree = SimpleNamespace(root=tmp_path, inventory=inventory, verify=lambda: None)
    host = SimpleNamespace(verify=lambda: None, observation=SimpleNamespace(device=SimpleNamespace(kind='cpu')))
    invocation = SimpleNamespace(argv=(str(tmp_path / 'python.exe'), '-I', '-S', '-B', str(tmp_path / transport.ENTRY)),
        cwd=tmp_path, timeout_seconds=15, environment=())
    probe = transport.PreparedAudioGraphProbe(tree, host, invocation, None)
    monkeypatch.setattr(windows_os_policy, 'read_prepared_os_policy', lambda **_: owned_os_policy_bytes())
    windows_loader.require_no_redirection(probe)


@pytest.mark.parametrize('change', ['nonce', 'version', 'extra', 'duplicate', 'noncanonical', 'oversize'])
def test_strict_request(packet, change):
    _, request = packet
    if change == 'nonce': request['nonce'] = 'x'
    if change == 'version': request['probe_version'] = True
    if change == 'extra': request['extra'] = 1
    raw = child.canonical(request)
    if change == 'duplicate': raw = b'{"nonce":"x",' + raw[1:]
    if change == 'noncanonical': raw += b'\n'
    if change == 'oversize': raw = b' ' * (child.INPUT_LIMIT + 1)
    with pytest.raises(ValueError): child.read_request(io.BytesIO(raw))


def test_opt_in_bundle_preserves_old_bytes_and_embeds_preimport_scanner():
    before = owned_native_load_probe_files()
    rows = transport.owned_audio_graph_probe_files()
    after = owned_native_load_probe_files()
    assert before == after
    entry = next(row.payload for row in rows if row.destination == transport.ENTRY)
    assert b'def scan_private(' in entry and b'from _content_os_host.python_origin_child' not in entry
    compile(entry, '<fixture entry syntax only>', 'exec')
    assert len({row.destination for row in rows}) == len(rows)


@pytest.mark.parametrize('problem', [None, 'nonce', 'version', 'hash', 'dispatch', 'attempts', 'exit',
    'extra', 'duplicate', 'oversize', 'timeout', 'post_tree', 'preverify'])
def test_one_shot_transport_consumes_success_and_every_failure(packet, monkeypatch, problem):
    root, request = packet
    report = child.recheck(root, request, scanner.scan_private, unknown_graph, NativeExecutionUnsupported)
    inventory = SimpleNamespace(model_dump=lambda **_: copy.deepcopy(request['inventory']),
        descriptor=SimpleNamespace(sha256=request['preflight']['inventory_sha256']),
        files=tuple(SimpleNamespace(**row) for row in request['inventory']['files']))
    claimed = []
    tree = SimpleNamespace(root=root, inventory=inventory, claim_for_launch=lambda: claimed.append(1))
    host = SimpleNamespace(verify=lambda: None)
    invocation = SimpleNamespace(argv=('fixed-python',), cwd=root, environment=(), timeout_seconds=15)
    probe = transport.PreparedAudioGraphProbe(tree, host, invocation, SimpleNamespace(descriptor=child.canonical(request['derivation'])))
    probe._nonce = request['nonce']
    def verify():
        if problem == 'preverify': raise ExecutionPreparationError('execution_audio_bundle_changed')
    monkeypatch.setattr(probe, 'verify', verify)
    monkeypatch.setattr(transport, 'require_no_redirection', lambda *_: None)
    monkeypatch.setattr(transport, 'preflight_audio_native_targets', lambda *_: SimpleNamespace(
        record=child.canonical(request['preflight']), identity_sha256=report['report_sha256']))
    monkeypatch.setattr(transport, '_scan_private', lambda *_: SimpleNamespace(descriptor=
        SimpleNamespace(sha256='0' * 64) if problem == 'post_tree' else inventory.descriptor))
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        assert kwargs['shell'] is False and kwargs['stderr'] == transport.subprocess.DEVNULL
        if problem == 'timeout': raise transport.subprocess.TimeoutExpired(argv, 15)
        changed = copy.deepcopy(report)
        if problem in ('nonce', 'hash'): changed['nonce' if problem == 'nonce' else 'report_sha256'] = '0' * 64
        if problem == 'version': changed['probe_version'] = 5
        if problem == 'dispatch': changed['dispatch_authorized'] = True
        if problem == 'attempts': changed['load_attempts'] = 1
        if problem == 'extra': changed['extra'] = 1
        raw = child.canonical(changed)
        if problem == 'duplicate': raw = b'{"load_attempts":1,' + raw[1:]
        if problem == 'oversize': raw = b' ' * (child.OUTPUT_LIMIT + 1)
        kwargs['stdout'].write(raw)
        return SimpleNamespace(returncode=1 if problem == 'exit' else 0)
    monkeypatch.setattr(transport.subprocess, 'run', run)
    if problem is None: assert probe.run_probe() == report
    else:
        with pytest.raises(ExecutionPreparationError): probe.run_probe()
    with pytest.raises(ExecutionPreparationError, match='consumed'): probe.run_probe()
    assert len(calls) == (0 if problem == 'preverify' else 1)
