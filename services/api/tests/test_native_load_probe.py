"""Protocol5 isolated target mapping: fake ABI/transport and synthetic files."""
from dataclasses import replace
import hashlib
import io
import json
from types import SimpleNamespace

import pytest

from app.providers import component_probe as probe, component_child, python_origin_child as child
from app.providers import python_origin_probe as transport
from app.providers.component_load_child import Bz2MappingProbe, TARGET, plan_digest
from app.providers.native_load import plan_native_graph
from app.providers.windows_os_policy import owned_os_policy_bytes, parse_os_policy
from test_component_probe import setup, contexts
from test_private_context_reader import private_contexts
from test_component_policy2 import policy2
from test_component_owner_probe import witness
from test_component_transport import prepared_layout, response
from test_pe_resources import manifest_image, resource_image


def test_protocol5_requires_all_owned_flags_and_does_not_accept_a_requested_target():
    request = dict(probe_version=5, nonce='a'*64, inventory={}, host_runtime={})
    for flags in ({}, dict(allow_component=True), dict(allow_component=True, allow_owner=True), dict(allow_native=True)):
        with pytest.raises(child.ProbeFailure): child.read_request(io.BytesIO(child.canonical(request)), **flags)
    flags = dict(allow_component=True, allow_owner=True, allow_native=True)
    assert child.read_request(io.BytesIO(child.canonical(request)), **flags) == request
    for change in (dict(probe_version=4), dict(target='other.dll')):
        with pytest.raises(child.ProbeFailure): child.read_request(io.BytesIO(child.canonical(dict(request, **change))), **flags)
    rows = probe.owned_native_load_probe_files()
    assert len(rows) == 22 and b'NATIVE_PROTOCOL = True' in next(r.payload for r in rows if r.destination == probe.ENTRY)
    for factory in (probe.owned_probe_files, probe.owned_component_probe_files, probe.owned_component_owner_probe_files):
        assert b'NATIVE_PROTOCOL = False' in next(r.payload for r in factory() if r.destination == probe.ENTRY)


@pytest.mark.parametrize('problem', ['none', 'unknown_import', 'post_unknown', 'target_not_dll', 'release'])
def test_child_real_graph_and_context_boundaries_with_fake_module_handles(setup, problem):
    root, request, facts, activation_reader, scanner, module_reader, activation, trace = setup
    request['probe_version'] = 5
    target = root / TARGET; target.parent.mkdir()
    payload = manifest_image() if problem != 'target_not_dll' else resource_image(exe=True)
    if problem == 'unknown_import': payload = resource_image(ordinary=('unknown.dll',))
    target.write_bytes(payload)
    for row in request['inventory']['files']:
        row.update(size_bytes=(root / row['name']).stat().st_size, role='interpreter' if row['name'] == 'python.exe' else 'dependency')
    request['inventory']['files'].append(dict(name=TARGET, role='dependency', size_bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest()))
    request['inventory']['directories'] = ['DLLs']
    scanner.load_profile = lambda root, **options: frozenset(('kernel32.dll',))
    original_metadata = activation_reader._metadata
    original_snapshot = activation_reader._modules._snapshot
    loaded = [False]
    def target_metadata():
        metadata = original_metadata(2, flags=8)
        return replace(metadata, root_manifest=str(target), application_directory=str(target.parent),
            assemblies=(replace(metadata.assemblies[0], manifest=str(target)), *metadata.assemblies[1:]))
    activation_reader.inspect_pe = lambda source, digest, **options: target_metadata() if source == target else original_metadata(None, flags=4)
    activation_reader._metadata = lambda handle, **options: target_metadata() if handle == 99 else original_metadata(handle, **options)
    activation_reader._modules._snapshot = lambda: original_snapshot() + (((99, target),) if loaded[0] else ())
    module_reader.native_origins = lambda: (root / 'python.exe', *((target,) if loaded[0] else ()))
    def load(path): assert path == target; trace.append('load'); loaded[0] = True; return 99
    def release(handle):
        assert handle == 99; trace.append('release')
        if problem == 'release': raise child.ProbeFailure('execution_target_release_failed')
        loaded[0] = False
    module_reader.load, module_reader.release = load, release
    if problem == 'post_unknown':
        scanner.blocked_origins = lambda *args: ([dict(kind='native', name='unknown.dll', category='outside', origin_id='c'*64, reason='unlisted_origin')] if loaded[0] else [])
    class Owner:
        def observe(self, modules, native): return native, witness()
    report = component_child.observe(root, request, {}, lambda _: module_reader, lambda: facts, activation, scanner,
        owner=Owner(), load_recipe=Bz2MappingProbe())
    typed = probe.NativeLoadProbeReport.model_validate_json(child.canonical(report))
    if problem == 'none':
        assert typed.passed and typed.native_load.release_verified
        assert typed.native_load.target_binding.query_role == 'associated'
        assert trace.count('load') == trace.count('release') == 1
        for cls in (probe.ComponentProbeReport, probe.ComponentOwnerProbeReport):
            with pytest.raises(ValueError): cls.model_validate_json(child.canonical(report))
    else:
        assert not typed.passed and typed.code
        if problem in ('unknown_import', 'target_not_dll'): assert 'load' not in trace
        else: assert trace.count('release') == 1


@pytest.mark.parametrize('change', ['none', 'plan_hash', 'target_hash', 'missing_release', 'version', 'target', 'post_verified'])
def test_new_factory_parent_recomputes_graph_and_consumes_invalid_transport(prepared_layout, monkeypatch, change):
    selection, facts, _ = prepared_layout
    target = selection['base'] / TARGET; target.write_bytes(resource_image())
    (selection['base'] / 'DLLs/pyexpat.pyd').write_bytes(b'owner fixture')
    def run(argv, **options):
        request = json.loads(options['input'])
        assert request['probe_version'] == 5 and options['timeout'] == 20
        report = response(request)
        report.update(probe_version=5, python_source_recipe='cpython312-expat-owner-v1', python_owner=witness())
        report['python_owner']['owner_sha256'] = next(r['sha256'] for r in request['inventory']['files'] if r['name'] == 'DLLs/pyexpat.pyd')
        inventory = SimpleNamespace(files=tuple(SimpleNamespace(**r) for r in request['inventory']['files']), directories=tuple(request['inventory']['directories']))
        policy = parse_os_policy(owned_os_policy_bytes())
        plan = plan_native_graph(options['cwd'], inventory, options['cwd'] / TARGET,
            policy.component_names | policy.contract_names, dynamic_dependencies={}, dynamic_closure_known=True)
        evidence = Bz2MappingProbe().evidence
        evidence.update(plan_sha256=plan_digest(plan), target_sha256=next(n.sha256 for n in plan.nodes if n.name == TARGET),
            node_count=len(plan.nodes), edge_count=len(plan.edges), load_attempts=1, release_attempts=1,
            postload_verified=True, release_verified=True)
        report['native_load'] = evidence
        if change == 'plan_hash': evidence['plan_sha256'] = '0'*64
        if change == 'target_hash': evidence['target_sha256'] = '0'*64
        if change == 'missing_release': evidence['release_verified'] = False
        if change == 'post_verified': evidence['postload_verified'] = False
        if change == 'version': report['probe_version'] = 4
        if change == 'target': evidence['target'] = 'other.dll'
        options['stdout'].write(child.canonical(report))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(transport.subprocess, 'run', run)
    with probe.prepare_native_load_probe(**selection) as command:
        if change == 'none': assert command.run_probe().passed
        else:
            with pytest.raises(transport.ExecutionPreparationError, match='execution_origin_probe_protocol_invalid'): command.run_probe()
        with pytest.raises(RuntimeError, match='consumed'): command.run_probe()
    assert not list(selection['staging_parent'].iterdir())
