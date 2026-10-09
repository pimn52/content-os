"""Real PE parsers/walker, synthetic files only; never imports native targets."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.providers import audio_native_graph as graph
from app.providers import audio_graph_child as child, python_origin_child as scanner
from app.providers.runtime_primitives import NativeExecutionUnsupported
from test_pe_resources import resource_image, manifest_image, forwarder_image
from test_audio_graph_probe import packet


def digest(raw): return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def exact_graph(tmp_path, monkeypatch):
    directory = Path(graph.__file__).parent
    raw = {name: (directory / name).read_bytes() for name in
        ('acquired_native_recipe.json', 'cpu_native_recipe.json', 'audio_native_graph.py')}
    catalog = json.loads(raw['acquired_native_recipe.json'])
    manifests = json.loads(raw['cpu_native_recipe.json'])['manifests']
    members = {row['slot']: row for row in catalog['members'] if row['slot'] in graph.SLOTS}
    payloads = {}
    for slot, row in members.items():
        if slot in ('python.exe', 'python312.dll'):
            payload = manifest_image(exe=slot == 'python.exe')
            dependencies = ['kernel32.dll']
            kind, leaves = 'requires_os_sxs_binding', 2
        elif slot in graph.TARGETS:
            form = manifests[row['resources']['manifest_sha256']]
            leaves_data = [(24, 2, 1033, bytes.fromhex(form['hex']), form['codepage'])]
            if row['resources']['leaf_count'] == 2:
                leaves_data.insert(0, (16, 1, 1033, b'version', 0))
            dependencies = ['kernel32.dll', 'python312.dll', 'vcruntime140.dll', 'vcruntime140_1.dll']
            payload = resource_image(leaves_data, ordinary=tuple(dependencies))
            kind, leaves = 'requires_private_root_context', len(leaves_data)
        else:
            payload = resource_image(ordinary=('kernel32.dll',))
            dependencies = ['kernel32.dll']
            kind, leaves = 'no_resources', 0
            row['resources']['manifest_sha256'] = None
        row.update(size_bytes=len(payload), sha256=digest(payload), dependencies=dependencies)
        row['resources'].update(kind=kind, leaf_count=leaves)
        payloads[slot] = payload

    def refresh():
        raw['acquired_native_recipe.json'] = child.canonical(catalog)
        for slot, payload in payloads.items():
            path = tmp_path / slot
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        inventory = SimpleNamespace(files=tuple(SimpleNamespace(name=slot,
            role='interpreter' if slot == 'python.exe' else 'dependency',
            size_bytes=len(payload), sha256=digest(payload)) for slot, payload in payloads.items()),
            directories=tuple(sorted(path.relative_to(tmp_path).as_posix()
                for path in tmp_path.rglob('*') if path.is_dir())))
        return inventory

    monkeypatch.setattr(graph, '_owned_bytes', lambda name: raw[name])
    inventory = refresh()
    return SimpleNamespace(root=tmp_path, inventory=inventory, payloads=payloads,
        members=members, raw=raw, manifests=manifests, refresh=refresh)


def walk(fixture, target=graph.TARGETS[0]):
    return graph.plan_audio_native_graph(fixture.root, fixture.inventory,
        fixture.root / target, frozenset({'kernel32.dll'}))


def test_both_exact_private_manifests_walk_bootstrap_and_vc(exact_graph):
    for slot in graph.TARGETS:
        plan = walk(exact_graph, slot)
        assert {node.name for node in plan.nodes} == set(graph.SLOTS) - (set(graph.TARGETS) - {slot})
        node = next(node for node in plan.nodes if node.name == slot)
        assert node.resources.kind == 'requires_private_root_context'
        assert node.resources.manifest_sha256 in exact_graph.manifests
        assert plan.requires_dynamic_closure and plan.requires_os_sxs_binding
        assert plan.dispatch_authorized is False
        assert set(plan.required_loaded_base) == set(graph.ROOT_PRIVATE)
        assert {name for source, name, _ in plan.edges if source == slot} == set(exact_graph.members[slot]['dependencies'])


@pytest.mark.parametrize('problem', ['bytes', 'hash', 'size', 'id', 'language', 'codepage', 'raw', 'leaf',
    'ordinary', 'delay', 'catalog', 'unknown', 'forwarder', 'extra', 'collision'])
def test_exact_member_manifest_and_edge_failures(exact_graph, problem):
    f, slot = exact_graph, graph.TARGETS[0]
    row = f.members[slot]
    form = f.manifests[row['resources']['manifest_sha256']]
    if problem in ('id', 'language', 'codepage', 'raw', 'leaf', 'ordinary', 'delay', 'unknown'):
        manifest = bytes.fromhex(form['hex']) + (b' ' if problem == 'raw' else b'')
        leaves = [(24, 1 if problem == 'id' else 2, 0 if problem == 'language' else 1033,
            manifest, 65001 if problem == 'codepage' else form['codepage'])]
        if problem == 'leaf': leaves.append((16, 1, 1033, b'extra', 0))
        ordinary = tuple(row['dependencies'])
        delayed = ()
        if problem == 'ordinary': ordinary += ('unexpected.dll',)
        if problem == 'delay': delayed = ('unexpected.dll',)
        if problem == 'unknown':
            ordinary += ('unexpected.dll',)
            row['dependencies'] = sorted(ordinary)
        f.payloads[slot] = resource_image(leaves, ordinary=ordinary, delayed=delayed)
        row.update(size_bytes=len(f.payloads[slot]), sha256=digest(f.payloads[slot]))
    elif problem == 'forwarder':
        # A catalog direct import cannot erase an unresolved export-forwarder.
        slot = 'vcruntime140.dll'
        f.payloads[slot] = forwarder_image(b'unknown.X\0')
        f.members[slot].update(size_bytes=len(f.payloads[slot]), sha256=digest(f.payloads[slot]), dependencies=[])
    elif problem == 'catalog': row['dependencies'] = []
    elif problem == 'bytes': f.payloads[slot] += b'changed'
    elif problem == 'hash': row['sha256'] = 'a' * 64
    elif problem == 'size': row['size_bytes'] += 1
    elif problem in ('extra', 'collision'):
        f.payloads['Lib/site-packages/' + ('VCRUNTIME140.DLL' if problem == 'collision' else 'extra.dll')] = resource_image()
    f.inventory = f.refresh()
    with pytest.raises(NativeExecutionUnsupported): walk(f)


def test_forwarder_is_graph_edge_not_catalog_direct_import(exact_graph):
    f, slot = exact_graph, 'vcruntime140.dll'
    f.payloads[slot] = forwarder_image(b'python312.PyObject_Call\0')
    f.members[slot].update(size_bytes=len(f.payloads[slot]), sha256=digest(f.payloads[slot]), dependencies=[])
    f.inventory = f.refresh()
    plan = walk(f)
    assert (slot, 'python312.dll', 'python312.dll') in plan.edges
    assert graph.inspect_audio_member(f.payloads[slot], slot)[1].dependencies == ()


@pytest.mark.parametrize('source', ['cpu_native_recipe.json', 'audio_native_graph.py', 'acquired_native_recipe.json'])
def test_policy_identity_binds_complete_helper_and_manifest_catalog(exact_graph, source):
    before = graph.owned_audio_graph_policy().identity_sha256
    exact_graph.raw[source] += b'\n'
    assert graph.owned_audio_graph_policy().identity_sha256 != before


def test_inventory_and_source_mutation_rejected(exact_graph):
    f = exact_graph
    path = f.root / graph.TARGETS[0]
    path.write_bytes(path.read_bytes() + b'changed')
    with pytest.raises(NativeExecutionUnsupported): walk(f)


def test_parent_and_child_independently_walk_real_pe_files(exact_graph, packet, monkeypatch):
    from app.providers import audio_native_preflight as parent
    from app.providers import cpython_base, windows_loader, windows_os_policy
    from app.providers.soundfile_binding import SoundFileDerivation
    f, (root, request) = exact_graph, packet
    assert root == f.root
    f.refresh()  # packet's negative native bytes are replaced by the real PE fixtures.
    private_path = root / child.PREFIX / 'private_native_recipe.json'
    private = json.loads(private_path.read_bytes())
    acquired = json.loads(f.raw['acquired_native_recipe.json'])
    for slot in graph.TARGETS:
        row = next(row for row in private['members'] if row['slot'] == slot)
        row.update(size_bytes=len(f.payloads[slot]), sha256=digest(f.payloads[slot]))
    private_raw = child.canonical(private)
    private_path.write_bytes(private_raw)
    for name, raw in f.raw.items(): (root / child.PREFIX / name).write_bytes(raw)
    os_raw = windows_os_policy.owned_os_policy_bytes()
    (root / 'Lib/site-packages/app/providers/windows_os_policy.json').write_bytes(os_raw)
    inventory = {'inventory_version': 1,
        'directories': sorted(path.relative_to(root).as_posix() for path in root.rglob('*') if path.is_dir()),
        'files': sorted(({'name': path.relative_to(root).as_posix(),
            'role': 'interpreter' if path.name == 'python.exe' else 'dependency',
            'size_bytes': path.stat().st_size, 'sha256': digest(path.read_bytes())}
            for path in root.rglob('*') if path.is_file()), key=lambda row: row['name'])}
    request['inventory'] = inventory
    descriptor = request['derivation']
    descriptor['private_policy_sha256'] = digest(private_raw)
    descriptor['dependencies'] = sorted((row['slot'], row['sha256']) for row in (*private['members'], *private['sources'])
        if row['slot'] in (*graph.TARGETS, parent.UPSTREAM_SLOT, parent.GENERATED_SLOT))
    upstream = (root / parent.UPSTREAM_SLOT).read_bytes()
    lines = upstream.splitlines(keepends=True)
    from app.providers.soundfile_binding import REPLACEMENT
    derived = b''.join(lines[:161]) + REPLACEMENT + b''.join(lines[218:])
    derivation = SoundFileDerivation(derived, child.canonical(descriptor))
    command = SimpleNamespace(invocation=SimpleNamespace(cwd=root), verify=lambda: None,
        inventory=SimpleNamespace(files=tuple(SimpleNamespace(**row) for row in inventory['files']),
            directories=tuple(inventory['directories']), descriptor=SimpleNamespace(sha256=digest(child.canonical(inventory)))))
    monkeypatch.setattr(parent, 'parse_acquired_descriptor', lambda _: (acquired, digest(f.raw['acquired_native_recipe.json'])))
    monkeypatch.setattr(parent, 'owned_private_recipe', lambda: (private, digest(private_raw)))
    monkeypatch.setattr(parent, 'derive_soundfile', lambda _: derivation)
    # Only preparation/base authority is a fixture. Parent's wrapper, PE parser,
    # whole-file reader and graph walker are real; child receives no plan callback.
    monkeypatch.setattr(cpython_base, 'select_cpython312_base', lambda _: None)
    monkeypatch.setattr(windows_loader, 'require_no_redirection', lambda *_, **__: None)
    monkeypatch.setattr(windows_os_policy, 'read_prepared_os_policy', lambda **_: os_raw)
    result = parent.preflight_audio_native_targets(command, derivation)
    assert all(row.plan is not None and row.blocker is None for row in result.targets)
    request['preflight'] = json.loads(result.record)
    report = child.recheck(root, request, scanner.scan_private, graph.plan_audio_native_graph, NativeExecutionUnsupported)
    assert child.canonical(report['preflight']) == result.record
    assert report['report_sha256'] == result.identity_sha256
    assert len(result.unresolved) == 2 and report['load_attempts'] == 0
    import subprocess
    import sys
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
request = child.read_request(sys.stdin.buffer)
root = pathlib.Path(sys.argv[2])
scanner.scan_private(root, request['inventory'])
graph = importlib.import_module('_fixture_owned.audio_native_graph')
graph.__file__ = str(root / child.PREFIX / 'audio_native_graph.py')
errors = importlib.import_module('_fixture_owned.runtime_primitives')
report = child.recheck(root, request, scanner.scan_private,
    graph.plan_audio_native_graph, errors.NativeExecutionUnsupported)
sys.stdout.buffer.write(child.canonical(report))
'''
    isolated = subprocess.run([sys.executable, '-I', '-S', '-B', '-c', code,
        str(Path(graph.__file__).parent), str(root)], input=child.canonical(request),
        capture_output=True, timeout=15, check=False)
    assert isolated.returncode == 0, isolated.stderr
    assert json.loads(isolated.stdout)['report_sha256'] == result.identity_sha256
    # Child must discover a changed source even after parent made a valid plan.
    target = root / graph.TARGETS[0]
    target.write_bytes(target.read_bytes() + b'changed')
    with pytest.raises(RuntimeError, match='tree_changed'):
        child.recheck(root, request, scanner.scan_private, graph.plan_audio_native_graph, NativeExecutionUnsupported)
