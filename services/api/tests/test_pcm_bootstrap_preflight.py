"""Real synthetic PE walking + fake component host only; no OS/loader calls."""
import hashlib
import json
import struct
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.providers import pcm_bootstrap_preflight as subject
from app.providers import pcm_static_probe, omnivoice_pcm_import as pcm
from app.providers.component_probe import PreparedComponentHost
from app.providers.prepared import ExecutionPreparationError, NativeExecutionUnsupported
from app.providers.runtime_tree import RuntimeTreeSelection, RuntimeFileSelection, prepare_runtime_tree
from test_omnivoice_pcm_import import layout, source_tree
from test_component_policy2 import policy2
from test_execution_specification_v3 import raw_spec, parse
from test_pe_imports import image
from test_pe_resources import manifest_image, forwarder_image


def pe(name, ordinary=('KERNEL32.dll',), delayed=()):
    data = image(ordinary, delayed)
    if name.endswith(('.dll', '.pyd')): struct.pack_into('<H', data, 150, 0x2000)
    return bytes(data)


@pytest.fixture
def selection(layout, raw_spec, monkeypatch):
    root = layout.source
    for name in subject.BASE:
        imports = ('python312.dll',) if name in ('python.exe', 'python3.dll') else (
            'vcruntime140.dll', 'KERNEL32.dll') if name == 'python312.dll' else ('KERNEL32.dll',)
        (root/name).write_bytes(pe(name, imports))
    for name in subject.STDLIB:
        path = root/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'# synthetic supporting stdlib source, never imported\n')
    def stage():
        tree = prepare_runtime_tree(
            trees=tuple(RuntimeTreeSelection(root/n, n) for n in ('Lib', 'DLLs', 'evidence')),
            files=tuple(RuntimeFileSelection(root/n, n, 'interpreter' if n == 'python.exe' else
                'entrypoint' if n == pcm.BOOTSTRAP_SLOT else 'dependency')
                for n in (*subject.BASE, pcm.BOOTSTRAP_SLOT, 'python._pth', 'python312._pth')),
            owned_files=pcm_static_probe.owned_pcm_static_files(), owned_recipe='pcm-static-probe-v7',
            staging_parent=layout.work.parent/'pcm-staging', max_bytes=2*1024**2)
        boundary = pcm.prepare_pcm_import_boundary(tree=tree, work_directory=layout.work, windows_root=layout.windows)
        host = PreparedComponentHost.__new__(PreparedComponentHost)
        host.tree, host._facts = tree, SimpleNamespace(root=layout.windows)
        host._observation = parse(raw_spec).host_runtime
        monkeypatch.setattr(PreparedComponentHost, 'verify', lambda self: self.observation)
        probe = pcm_static_probe.PreparedPCMStaticProbe(boundary, host=host)
        return tree, boundary, probe
    return SimpleNamespace(layout=layout, stage=stage)


@pytest.fixture
def prepared(selection):
    tree, boundary, probe = selection.stage()
    try:
        value = subject.prepare_pcm_bootstrap_preflight(probe)
        yield value, probe
        value.close()
    finally:
        probe.close()
        boundary.close()
        tree.close()


def test_fixed_real_walker_graphs_identity_detachment_and_native_stop(prepared):
    value, probe = prepared
    report = value.verify()
    assert tuple(r['target'] for r in report['targets']) == subject.TARGETS
    assert all(r['blocker'] is None for r in report['targets'])
    first = report['targets'][0]['plan']
    assert ['python312.dll', 'kernel32.dll', 'OS:kernel32.dll'] in first['edges']
    assert ['python.exe', 'python312.dll', 'python312.dll'] in first['edges']
    assert {r['name'] for r in first['nodes']} == {'python.exe', 'python312.dll', 'vcruntime140.dll'}
    assert all(r['plan']['requires_dynamic_closure'] for r in report['targets'])
    assert not report['loading_authorized'] and not report['dispatch_authorized']
    assert value.identity_sha256 == hashlib.sha256(subject.core.canonical(report)).hexdigest()
    report['pending'].clear()
    assert value.verify()['pending'] == list(subject.PENDING)
    with pytest.raises(NativeExecutionUnsupported): value.require_native_preparation()
    value.close()
    with pytest.raises(ExecutionPreparationError, match='consumed'): value.verify()
    assert probe.verify()  # Report close does not delete caller-owned runtime.


@pytest.mark.parametrize('target', subject.TARGETS)
@pytest.mark.parametrize('delayed', [False, True])
def test_unknown_ordinary_or_delay_dependency_is_retained_stop(selection, delayed, target):
    (selection.layout.source/target).write_bytes(pe(target,
        () if delayed else ('unknown.dll',), ('unknown.dll',) if delayed else ()))
    tree, boundary, probe = selection.stage()
    try:
        value = subject.prepare_pcm_bootstrap_preflight(probe)
        report = value.verify()
        row = next(r for r in report['targets'] if r['target'] == target)
        assert row['blocker'] == 'execution_native_plan_dependency_unknown'
        assert row['plan'] is None
        assert not report['loading_authorized']
        with pytest.raises(NativeExecutionUnsupported): value.require_native_preparation()
    finally:
        probe.close(); boundary.close(); tree.close()


@pytest.mark.parametrize('change', ['pe', 'unknown-resource', 'stdlib-missing', 'shadow', 'local', 'policy'])
def test_bad_initial_prerequisite_never_authorizes(selection, change):
    root = selection.layout.source
    if change == 'pe': (root/'python3.dll').write_bytes(b'not a PE')
    elif change == 'unknown-resource': (root/'python312.dll').write_bytes(manifest_image(manifest=b'<unknown/>'))
    elif change == 'stdlib-missing': (root/'Lib/os.py').unlink()
    elif change == 'shadow': (root/'DLLs/kernel32.dll').write_bytes(bytes(image()))
    elif change == 'local': (root/'DLLs/python.exe.local').write_bytes(b'external redirection')
    else: (root/'Lib/site-packages/app/providers/windows_os_policy.json').write_bytes(b'{}')
    tree, boundary, probe = selection.stage()
    try:
        with pytest.raises((ExecutionPreparationError, NativeExecutionUnsupported)):
            subject.prepare_pcm_bootstrap_preflight(probe)
        with pytest.raises(NativeExecutionUnsupported): probe.require_native_preparation()
    finally:
        probe.close(); boundary.close(); tree.close()


@pytest.mark.parametrize('change', ['bytes', 'extra', 'stdlib', 'host', 'environment', 'invocation', 'probe-consumed', 'implementation'])
def test_reuse_recomputes_and_failure_consumes(prepared, monkeypatch, change):
    value, probe = prepared
    root = probe.boundary.tree.root
    if change == 'bytes': (root/'python312.dll').write_bytes(bytes(image(('other.dll',))))
    elif change == 'extra': (root/'DLLs/extra.pyd').write_bytes(bytes(image()))
    elif change == 'stdlib': (root/'Lib/os.py').write_bytes(b'changed')
    elif change == 'host': probe.host._observation = probe.host._observation.model_copy(update={'revision': 99999})
    elif change == 'environment': (root/'_cpu_disabled').mkdir()
    elif change == 'invocation': probe.invocation = None
    elif change == 'probe-consumed': probe.close()
    else: monkeypatch.setattr(subject, 'implementation_identity', lambda: '0'*64)
    with pytest.raises((ExecutionPreparationError, NativeExecutionUnsupported)): value.verify()
    with pytest.raises(ExecutionPreparationError, match='consumed'): value.verify()


def test_caller_target_graph_or_closure_flag_not_exposed(prepared):
    _, probe = prepared
    with pytest.raises(TypeError): subject.prepare_pcm_bootstrap_preflight(probe, target='DLLs/target.pyd')
    with pytest.raises(TypeError): subject.prepare_pcm_bootstrap_preflight(probe, dynamic_closure_known=True)
    with pytest.raises(ExecutionPreparationError): subject.prepare_pcm_bootstrap_preflight(SimpleNamespace())


def test_existing_bootstrap_manifest_keeps_context_obligation(selection):
    (selection.layout.source/'python.exe').write_bytes(manifest_image(exe=True))
    tree, boundary, probe = selection.stage()
    try:
        value = subject.prepare_pcm_bootstrap_preflight(probe)
        report = value.verify()
        assert all(r['plan']['requires_os_sxs_binding'] for r in report['targets'])
        assert all(r['plan']['requires_dynamic_closure'] for r in report['targets'])
        assert report['base'][0]['resources'] == 'requires_os_sxs_binding'
        assert not report['loading_authorized']
    finally:
        probe.close(); boundary.close(); tree.close()


@pytest.fixture
def hashlib_selection(selection):
    root = selection.layout.source
    (root/subject.HASHLIB_SOURCE).write_bytes(b'# synthetic identity only\nimport _hashlib\n')
    (root/subject.HASHLIB_TARGET).write_bytes(pe('_hashlib.pyd',
        (subject.HASHLIB_DEPENDENCY, 'python312.dll', 'vcruntime140.dll', 'KERNEL32.dll')))
    (root/'DLLs'/subject.HASHLIB_DEPENDENCY).write_bytes(pe(subject.HASHLIB_DEPENDENCY))
    return selection


@contextmanager
def hashlib_prepared(selection):
    tree, boundary, probe = selection.stage()
    try:
        yield subject.prepare_pcm_hashlib_preflight(probe), probe
    finally:
        probe.close(); boundary.close(); tree.close()


def test_v2_fixed_hashlib_chain_binds_source_and_private_crypto_without_authority(hashlib_selection):
    with hashlib_prepared(hashlib_selection) as (value, probe):
        report = value.verify()
        assert report['version'] == 2 and report['recipe'].endswith('-v2')
        facts = report['hashlib_prerequisites']
        assert facts['source']['blocker'] is None and facts['extension']['blocker'] is None
        raw = (probe.boundary.tree.root/subject.HASHLIB_SOURCE).read_bytes()
        assert facts['source']['sha256'] == hashlib.sha256(raw).hexdigest()
        graph = facts['target']['plan']
        assert facts['target']['blocker'] is None
        assert [subject.HASHLIB_TARGET, subject.HASHLIB_DEPENDENCY,
                'DLLs/'+subject.HASHLIB_DEPENDENCY] in graph['edges']
        assert {'python312.dll', 'vcruntime140.dll'} <= set(graph['required_loaded_base'])
        assert graph['requires_dynamic_closure']
        assert not facts['publisher_build_correspondence_verified']
        assert not facts['initializer_closure_verified']
        assert report['pending'] == list(subject.PENDING)
        assert not report['loading_authorized'] and not report['dispatch_authorized']
        with pytest.raises(NativeExecutionUnsupported): value.require_native_preparation()
        facts['source']['sha256'] = '0'*64
        assert value.verify()['hashlib_prerequisites']['source']['sha256'] != '0'*64


@pytest.mark.parametrize('missing,location,code', [
    (subject.HASHLIB_SOURCE, 'source', 'execution_pcm_hashlib_source_missing'),
    (subject.HASHLIB_TARGET, 'extension', 'execution_pcm_hashlib_extension_missing'),
    ('DLLs/'+subject.HASHLIB_DEPENDENCY, 'target', 'execution_native_plan_dependency_unknown'),
])
def test_v2_missing_prerequisite_retained_as_named_block(hashlib_selection, missing, location, code):
    (hashlib_selection.layout.source/missing).unlink()
    with hashlib_prepared(hashlib_selection) as (value, _):
        report = value.verify()
        assert report['hashlib_prerequisites'][location]['blocker'] == code
        assert not report['loading_authorized']


@pytest.mark.parametrize('imports,delayed,code', [
    (('KERNEL32.dll',), (), 'execution_pcm_hashlib_dependency_changed'),
    ((), (subject.HASHLIB_DEPENDENCY,), 'execution_pcm_hashlib_dependency_changed'),
    ((subject.HASHLIB_DEPENDENCY, 'unknown.dll'), (), 'execution_native_plan_dependency_unknown'),
    ((subject.HASHLIB_DEPENDENCY,), ('unknown.dll',), 'execution_native_plan_dependency_unknown'),
])
def test_v2_changed_or_unknown_edges_do_not_select_alternate_backend(hashlib_selection, imports, delayed, code):
    (hashlib_selection.layout.source/subject.HASHLIB_TARGET).write_bytes(pe('_hashlib.pyd', imports, delayed))
    with hashlib_prepared(hashlib_selection) as (value, _):
        target = value.verify()['hashlib_prerequisites']['target']
        assert target['blocker'] == code and target['plan'] is None


@pytest.mark.parametrize('change', ['crypto-unknown', 'crypto-malformed', 'extension-malformed',
                                  'extension-resource', 'source-empty', 'shadow', 'duplicate', 'crypto-forwarder'])
def test_v2_unsafe_native_or_source_still_rejects_or_blocks(hashlib_selection, change):
    root = hashlib_selection.layout.source
    if change == 'crypto-unknown':
        (root/'DLLs'/subject.HASHLIB_DEPENDENCY).write_bytes(pe('crypto.dll', ('unknown.dll',)))
    elif change == 'crypto-forwarder':
        (root/'DLLs'/subject.HASHLIB_DEPENDENCY).write_bytes(forwarder_image(b'unknown.Call\0'))
    elif change == 'crypto-malformed': (root/'DLLs'/subject.HASHLIB_DEPENDENCY).write_bytes(b'invalid PE')
    elif change == 'extension-malformed': (root/subject.HASHLIB_TARGET).write_bytes(b'invalid PE')
    elif change == 'extension-resource':
        (root/subject.HASHLIB_TARGET).write_bytes(manifest_image(manifest=b'<unknown/>'))
    elif change == 'source-empty': (root/subject.HASHLIB_SOURCE).write_bytes(b'')
    elif change == 'shadow': (root/'DLLs/kernel32.dll').write_bytes(pe('kernel32.dll'))
    else: (root/'Lib/site-packages'/subject.HASHLIB_DEPENDENCY).write_bytes(pe('crypto.dll'))
    blocked = {'crypto-unknown': 'execution_native_plan_dependency_unknown',
               'crypto-forwarder': 'execution_native_plan_dependency_unknown',
               'crypto-malformed': 'execution_pe_imports_unsupported',
               'shadow': 'execution_dll_basename_collision',
               'duplicate': 'execution_dll_basename_collision'}
    try:
        with hashlib_prepared(hashlib_selection) as (value, _):
            assert change in blocked
            assert value.verify()['hashlib_prerequisites']['target']['blocker'] == blocked[change]
    except (ExecutionPreparationError, NativeExecutionUnsupported):
        assert change not in ('crypto-unknown', 'crypto-forwarder', 'crypto-malformed')


@pytest.mark.parametrize('change', ['source', 'extension', 'crypto', 'implementation', 'version'])
def test_v2_added_identity_mutation_consumes(hashlib_selection, monkeypatch, change):
    with hashlib_prepared(hashlib_selection) as (value, probe):
        root = probe.boundary.tree.root
        if change == 'source': (root/subject.HASHLIB_SOURCE).write_bytes(b'changed source\n')
        elif change == 'extension': (root/subject.HASHLIB_TARGET).write_bytes(pe('extension.pyd'))
        elif change == 'crypto': (root/'DLLs'/subject.HASHLIB_DEPENDENCY).write_bytes(pe('crypto.dll', ('unknown.dll',)))
        elif change == 'implementation': monkeypatch.setattr(subject, 'implementation_identity', lambda: '0'*64)
        else: value._version = 1
        with pytest.raises((ExecutionPreparationError, NativeExecutionUnsupported)): value.verify()
        with pytest.raises(ExecutionPreparationError, match='consumed'): value.verify()


def test_v1_default_report_and_child_bundle_unchanged_by_v2(hashlib_selection):
    before = pcm_static_probe.owned_pcm_static_files()
    with hashlib_prepared(hashlib_selection) as (v2, probe):
        v1 = subject.prepare_pcm_bootstrap_preflight(probe)
        first = v1.verify()
        assert first['version'] == 1 and first['recipe'].endswith('-v1')
        assert 'hashlib_prerequisites' not in first
        assert tuple(r['target'] for r in first['targets']) == subject.TARGETS
        second = v2.verify()
        for key in first.keys() - {'version', 'recipe'}:
            assert first[key] == second[key]
        assert before == pcm_static_probe.owned_pcm_static_files()
        for arguments in ({'target': subject.HASHLIB_TARGET}, {'dynamic_closure_known': True}, {'version': 2}):
            with pytest.raises(TypeError): subject.prepare_pcm_hashlib_preflight(probe, **arguments)
        with pytest.raises(ExecutionPreparationError): subject.prepare_pcm_hashlib_preflight(SimpleNamespace())
        for version in (True, '2', 3):
            with pytest.raises(ExecutionPreparationError): subject.PreparedPCMBootstrapPreflight(probe, version=version)


@pytest.mark.parametrize('slot', [subject.HASHLIB_SOURCE, subject.HASHLIB_TARGET, 'DLLs/'+subject.HASHLIB_DEPENDENCY])
def test_v2_foreign_role_rejected(hashlib_selection, monkeypatch, slot):
    tree, boundary, probe = hashlib_selection.stage()
    try:
        inventory = probe.verify()
        rows = tuple(row.model_copy(update={'role': 'entrypoint'}) if row.name == slot else row
                     for row in inventory.files)
        foreign = inventory.model_copy(update={'files': rows})
        monkeypatch.setattr(probe, 'verify', lambda: foreign)
        # Tampered role also contradicts the protocol's bound inventory identity.
        with pytest.raises((ExecutionPreparationError, NativeExecutionUnsupported, ValueError)):
            subject.prepare_pcm_hashlib_preflight(probe)
    finally:
        probe.close(); boundary.close(); tree.close()
