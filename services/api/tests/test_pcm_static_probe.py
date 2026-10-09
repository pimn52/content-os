"""Protocol 7 synthetic bytes/fake peer; never selected-child runtime evidence."""
import ast
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace

import pytest

from app.domain.execution_runtime import RuntimeInventory
from app.providers import omnivoice_audio_pcm as audio
from app.providers import omnivoice_pcm_import as pcm
from app.providers import pcm_static_core as core
from app.providers import pcm_static_probe as subject
from app.providers import pcm_static_wire as wire
from app.providers.prepared import ExecutionPreparationError, NativeExecutionUnsupported
from app.providers.runtime_tree import RuntimeTreeSelection, RuntimeFileSelection, prepare_runtime_tree
from test_omnivoice_pcm_import import layout, source_tree
from test_component_policy2 import policy2
from test_execution_specification_v3 import raw_spec, parse
from app.providers.component_probe import PreparedComponentHost


@pytest.fixture
def probe(layout, raw_spec, monkeypatch):
    tree = prepare_runtime_tree(
        trees=tuple(RuntimeTreeSelection(layout.source/name, name) for name in ('Lib', 'DLLs', 'evidence')),
        files=(RuntimeFileSelection(layout.source/'python.exe', 'python.exe', 'interpreter'),
            RuntimeFileSelection(layout.source/pcm.BOOTSTRAP_SLOT, pcm.BOOTSTRAP_SLOT, 'entrypoint'),
            *(RuntimeFileSelection(layout.source/n, n, 'dependency') for n in ('python._pth', 'python312._pth'))),
        owned_files=subject.owned_pcm_static_files(), owned_recipe='pcm-static-probe-v7',
        staging_parent=layout.work.parent/'pcm-staging', max_bytes=2*1024**2)
    boundary = pcm.prepare_pcm_import_boundary(tree=tree, work_directory=layout.work, windows_root=layout.windows)
    host = PreparedComponentHost.__new__(PreparedComponentHost)
    host.tree = tree
    host._facts = SimpleNamespace(root=layout.windows)
    host._observation = parse(raw_spec).host_runtime
    monkeypatch.setattr(PreparedComponentHost, 'verify', lambda self: self.observation)
    value = subject.PreparedPCMStaticProbe(boundary, host=host)
    yield value
    value.close()
    boundary.close()
    tree.close()


def good_peer(probe, monkeypatch):
    bundle = subject.owned_pcm_static_files()
    entry = next(r.payload for r in bundle if r.destination == wire.ENTRY)
    namespace = {'__name__': '_synthetic_owned_pcm_child'}
    exec(compile(entry, '<synthetic-owned-pcm-child>', 'exec'), namespace)
    import re, unicodedata
    namespace.update(hashlib=hashlib, json=json, re=re, unicodedata=unicodedata)
    # Explicit fake modules for a fake transport. Production child has no injection.
    rules = ModuleType('_content_os_pcm.rules')
    rules.RULES = pcm.owned_core_rules()
    package = ModuleType('_content_os_pcm')
    package.__path__ = []
    for name, module in {'_content_os_pcm': package,
        '_content_os_pcm.pcm_static_core': core,
        '_content_os_pcm.omnivoice_audio_pcm': audio,
        '_content_os_pcm.rules': rules}.items():
        monkeypatch.setitem(sys.modules, name, module)
    def peer(raw, invocation):
        value = wire.request(raw)
        namespace['TREE_LIMIT'] = value['caps']['bytes']
        namespace['FILE_LIMIT'] = value['caps']['files']
        namespace['DIRECTORY_LIMIT'] = value['caps']['directories']
        report = namespace['diagnostic'](probe.boundary.tree.root, value,
            dict(invocation.environment), str(invocation.cwd), [str(invocation.argv[-1])])
        return wire.canonical(report)
    return peer


def test_parent_child_static_facts_and_one_use(probe, monkeypatch):
    report = probe.exchange_synthetic(good_peer(probe, monkeypatch))
    assert report['facts']['inventory_sha256'] == probe.boundary.verify().inventory_sha256
    assert report['facts']['environment_sha256'] == probe.boundary.verify().environment_sha256
    assert report['load_attempts'] == 0
    assert not report['dispatch_authorized'] and not report['loading_authorized']
    assert 'required_native_source_graph_and_origins' in report['facts']['pending']
    with pytest.raises(ExecutionPreparationError, match='consumed'):
        probe.exchange_synthetic(lambda *_: pytest.fail('replay reached transport'))
    with pytest.raises(NativeExecutionUnsupported): probe.require_native_preparation()


@pytest.mark.parametrize('change', ['nonce', 'version', 'phase', 'binding', 'extra', 'bool', 'pending',
                                   'duplicate', 'whitespace', 'partial', 'oversize', 'timeout'])
def test_bad_response_consumes(probe, monkeypatch, change):
    peer = good_peer(probe, monkeypatch)
    def bad(raw, invocation):
        response = peer(raw, invocation)
        if change == 'timeout': raise TimeoutError('synthetic timeout')
        if change == 'duplicate': return response[:-1]+b',"load_attempts":0}'
        if change == 'whitespace': return response+b'\n'
        if change == 'partial': return response[:50]
        if change == 'oversize': return b'x'*(wire.OUTPUT_LIMIT+1)
        value = json.loads(response)
        if change == 'nonce': value['nonce'] = '0'*64
        if change == 'version': value['probe_version'] = 6
        if change == 'phase': value['phase'] = 'generation'
        if change == 'binding': value['binding_sha256'] = '0'*64
        if change == 'extra': value['extra'] = False
        if change == 'bool': value['load_attempts'] = False
        if change == 'pending': value['facts']['pending'] = []
        return wire.canonical(value)
    with pytest.raises(ExecutionPreparationError): probe.exchange_synthetic(bad)
    with pytest.raises(ExecutionPreparationError): probe.verify()


@pytest.mark.parametrize('change', ['source', 'environment', 'invocation', 'owned-role', 'host', 'host-tree', 'boundary'])
def test_current_preparation_mutation_stops_before_peer(probe, change):
    if change == 'source':
        (probe.boundary.tree.root/'Lib/site-packages/transformers/optional.py').write_bytes(b'changed')
    elif change == 'environment':
        (probe.boundary.tree.root/'_cpu_disabled').mkdir()
    elif change == 'invocation':
        probe.invocation = None
    elif change == 'host':
        probe.host._observation = probe.host._observation.model_copy(update={'revision': 999999})
    elif change == 'host-tree':
        probe.host.tree = object()
    elif change == 'boundary':
        probe.boundary = object()
    else:
        value = json.loads(probe.boundary.tree._inventory_json)
        next(r for r in value['files'] if r['name'] == wire.ENTRY)['role'] = 'dependency'
        probe.boundary.tree._inventory_json = json.dumps(value)
    with pytest.raises(ExecutionPreparationError):
        probe.exchange_synthetic(lambda *_: pytest.fail('mutation reached peer'))


def test_mutation_during_peer_consumes(probe, monkeypatch):
    peer = good_peer(probe, monkeypatch)
    def mutate(raw, invocation):
        result = peer(raw, invocation)
        (probe.boundary.tree.root/'python._pth').write_bytes(b'changed')
        return result
    with pytest.raises(ExecutionPreparationError): probe.exchange_synthetic(mutate)
    with pytest.raises(ExecutionPreparationError): probe.verify()


@pytest.mark.parametrize('change', ['bool-version', 'bool-size', 'case-alias', 'parent', 'reserved',
                                   'extra', 'empty-entry', 'roles', 'depth', 'nfc', 'digest', 'total'])
def test_wire_and_typed_parent_reject_same_inventory(probe, change):
    value = json.loads(probe.boundary.tree.inventory.canonical_bytes())
    row = value['files'][0]
    if change == 'bool-version': value['inventory_version'] = True
    if change == 'bool-size': row['size_bytes'] = True
    if change == 'case-alias': value['files'].append(dict(row, name=row['name'].swapcase()))
    if change == 'parent': value['directories'].remove('Lib')
    if change == 'reserved': row['name'] = 'CON.txt'
    if change == 'extra': row['extra'] = 1
    if change == 'empty-entry':
        row = next(r for r in value['files'] if r['role'] == 'entrypoint')
        row.update(size_bytes=0, sha256=wire.digest(b''))
    if change == 'roles': next(r for r in value['files'] if r['role'] == 'interpreter')['role'] = 'dependency'
    if change == 'depth': row['name'] = '/'.join(['a']*33)
    if change == 'nfc': row['name'] = 'e\u0301.py'
    if change == 'digest': row['sha256'] = 'F'*64
    if change == 'total': row['size_bytes'] = wire.TREE_LIMIT
    with pytest.raises(ValueError): wire.validate_inventory(value)
    with pytest.raises(ValueError): RuntimeInventory.model_validate_json(json.dumps(value))


def test_wire_caps_and_noncanonical_request(probe):
    raw = probe._request()
    value = wire.request(raw)
    value['caps']['bytes'] += 1
    with pytest.raises(ValueError): wire.request(wire.canonical(value))
    with pytest.raises(ValueError): wire.request(raw+b'\n')
    with pytest.raises(ValueError): wire.request(b'x'*(wire.INPUT_LIMIT+1))


def test_owned_bundle_no_application_imports_and_guard_order(probe):
    rows = subject.owned_pcm_static_files()
    entry = next(r.payload for r in rows if r.destination == wire.ENTRY)
    tree = ast.parse(entry)
    names = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    names += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    assert not any(n and n.split('.')[0] in ('app', 'pydantic', 'torch', 'numpy', 'omnivoice') for n in names)
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'main')
    assert ast.unparse(main.body[0].value).startswith('verify_startup(sys)')
    diagnostic = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'diagnostic')
    scan = next(i for i, n in enumerate(diagnostic.body) if isinstance(n, ast.Expr)
        and isinstance(n.value, ast.Call) and ast.unparse(n.value.func) == 'scan_private')
    first_helper = next(i for i, n in enumerate(diagnostic.body)
        if isinstance(n, ast.ImportFrom) and n.module.startswith('_content_os_pcm'))
    assert scan < first_helper
    assert b'runpy' not in entry and b'run_module' not in entry


def test_core_in_isolated_current_python_stdlib_harness():
    # Current test interpreter only, not the selected Windows/vendor runtime.
    code = ('import runpy,sys; m=runpy.run_path('+repr(str(Path(core.__file__).resolve()))+'); '
        'assert not any(n.split(".")[0] in ("app","pydantic","numpy","torch") for n in sys.modules); '
        'assert m["environment_values"]')
    result = subprocess.run([sys.executable, '-I', '-B', '-c', code], capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode()
