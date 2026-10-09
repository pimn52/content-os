"""Explicit new source-observation recipe and unchanged historical protocols."""
import copy
from dataclasses import replace
import io
import json
from types import SimpleNamespace

import pytest

from app.providers import component_probe as probe, component_child
from app.providers import python_origin_child as child, python_origin_probe as transport
from app.providers.runtime_primitives import ExecutionPreparationError
from test_component_transport import prepared_layout, response
from test_component_policy2 import policy2
from test_component_probe import setup, contexts
from test_private_context_reader import private_contexts


def witness():
    return dict(recipe='cpython312-expat-owner-v1', owner='DLLs/pyexpat.pyd', owner_sha256='c'*64,
        modules=['pyexpat.errors', 'pyexpat.model'], data_sha256=['a'*64, 'b'*64], wrapper_sha256=None)


def test_new_request_requires_explicit_owned_branch():
    request = dict(probe_version=4, nonce='a'*64, inventory={}, host_runtime={})
    for options in ({}, dict(allow_component=True), dict(allow_owner=True)):
        with pytest.raises(child.ProbeFailure): child.read_request(io.BytesIO(child.canonical(request)), **options)
    assert child.read_request(io.BytesIO(child.canonical(request)), allow_component=True, allow_owner=True) == request
    request['probe_version'] = 3
    with pytest.raises(child.ProbeFailure): child.read_request(io.BytesIO(child.canonical(request)), allow_component=True, allow_owner=True)


def test_owned_bundle_does_not_enable_legacy_branch():
    for factory, enabled in ((probe.owned_component_probe_files, False), (probe.owned_component_owner_probe_files, True)):
        rows = factory()
        entry = next(row.payload for row in rows if row.destination == probe.ENTRY)
        assert (b'OWNER_PROTOCOL = True' in entry) == enabled
        assert any(row.destination.endswith('/python_owner_origins.py') for row in rows) == enabled
        assert len(rows) <= 24


def test_v4_child_orders_native_check_owner_and_recheck(setup):
    root, request, facts, reader, scanner, module_reader, activation, trace = setup
    request['probe_version'] = 4
    def blocked(*args): trace.append('native' if len(args[-1]) == 1 else 'all_origins'); return []
    scanner.blocked_origins = blocked
    class Owner:
        def observe(self, modules, native): trace.append('owner'); return native, witness()
    report = component_child.observe(root, request, {}, lambda _: module_reader, lambda: facts, activation, scanner, owner=Owner())
    typed = probe.ComponentOwnerProbeReport.model_validate_json(child.canonical(report))
    assert typed.passed and not typed.dispatch_authorized
    assert trace == ['tree', 'native', 'owner', 'all_origins', 'activation', 'search', 'native', 'owner', 'all_origins', 'tree']
    with pytest.raises(ValueError): probe.ComponentProbeReport.model_validate_json(child.canonical(report))
    report['python_owner'] = None
    with pytest.raises(ValueError): probe.ComponentOwnerProbeReport.model_validate_json(child.canonical(report))


def test_v4_native_failure_precedes_owner_observation(setup):
    root, request, facts, reader, scanner, module_reader, activation, trace = setup
    request['probe_version'] = 4
    scanner.blocked_origins = lambda *args: [dict(kind='native', name='bad.dll', category='outside', origin_id='c'*64, reason='unlisted_origin')]
    class Owner:
        def observe(self, *args): pytest.fail('must stop before attribution')
    report = component_child.observe(root, request, {}, lambda _: module_reader, lambda: facts, activation, scanner, owner=Owner())
    assert report['code'] == 'execution_loaded_origin_unsupported' and 'activation' not in trace
    probe.ComponentOwnerProbeReport.model_validate_json(child.canonical(report))


def test_v4_binding_failure_retains_bounded_role_and_raw_observation(setup):
    root, request, facts, reader, scanner, module_reader, activation, trace = setup
    request['probe_version'] = 4
    original = reader._metadata
    reader._metadata = lambda *args, **kwargs: replace(original(*args, **kwargs), flags=1)
    class Owner:
        def observe(self, modules, native): return native, witness()
    report = component_child.observe(root, request, {}, lambda _: module_reader, lambda: facts, activation, scanner, owner=Owner())
    typed = probe.ComponentOwnerProbeReport.model_validate_json(child.canonical(report))
    assert not typed.passed and typed.component_failure.role == 'effective'
    assert typed.component_failure.checkpoint == 'context_flags'
    assert typed.component_failure.metadata['flags'] == 1 and 'search' not in trace
    for update in [dict(passed=True), dict(code='execution_other'), dict(stage='baseline')]:
        with pytest.raises(ValueError): probe.ComponentOwnerProbeReport.model_validate_json(child.canonical(dict(report, **update)))
    for update in [dict(checkpoint='unknown'), dict(source='../escape'), dict(metadata={'value': 'x'*49153})]:
        invalid = dict(report, component_failure=dict(report['component_failure'], **update))
        with pytest.raises(ValueError): probe.ComponentOwnerProbeReport.model_validate_json(child.canonical(invalid))


def test_mismatch_capture_is_failure_only_and_role_specific(setup, monkeypatch):
    root, request, facts, reader, scanner, module_reader, activation, trace = setup
    original = component_child.compare_current
    def compare(reader, root, files, windows, prediction):
        different = replace(prediction, common_controls=replace(prediction.common_controls,
            file=replace(prediction.common_controls.file, sha256='0'*64)))
        return original(reader, root, files, windows, different)
    monkeypatch.setattr(component_child, 'compare_current', compare)
    class Owner:
        def observe(self, modules, native): return native, witness()
    report = component_child.observe(root, request, {}, lambda _: module_reader, lambda: facts, activation, scanner, owner=Owner())
    typed = probe.ComponentOwnerProbeReport.model_validate_json(child.canonical(report))
    assert not typed.passed and typed.component_failure.checkpoint == 'binding_mismatch'
    assert typed.component_failure.role == 'effective' and 'search' not in trace
    for update in (dict(passed=True), dict(code='execution_common_controls_policy2_unsupported'), dict(stage='baseline')):
        with pytest.raises(ValueError):
            probe.ComponentOwnerProbeReport.model_validate_json(child.canonical(dict(report, **update)))


@pytest.mark.parametrize('problem', ['none', 'downgrade', 'wrong_owner_hash', 'missing_witness', 'wrong_recipe', 'unknown_alias'])
def test_new_factory_typed_transport_and_negative_pairings(prepared_layout, monkeypatch, problem):
    selection, facts, paths = prepared_layout
    (selection['base'] / 'DLLs/pyexpat.pyd').write_bytes(b'synthetic owner')
    def run(argv, **kwargs):
        request = json.loads(kwargs['input'])
        assert request['probe_version'] == 4
        report = response(request)
        report.update(probe_version=4, python_source_recipe='cpython312-expat-owner-v1', python_owner=witness())
        owner = report['python_owner']
        owner['owner_sha256'] = next(row['sha256'] for row in request['inventory']['files'] if row['name'] == owner['owner'])
        if problem == 'downgrade': report['probe_version'] = 3
        elif problem == 'wrong_owner_hash': owner['owner_sha256'] = '0'*64
        elif problem == 'missing_witness': report['python_owner'] = None
        elif problem == 'wrong_recipe': report['python_source_recipe'] = 'unknown'
        elif problem == 'unknown_alias': owner['modules'].append('other.errors')
        kwargs['stdout'].write(child.canonical(report))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(transport.subprocess, 'run', run)
    with probe.prepare_component_owner_probe(**selection) as command:
        if problem == 'none': assert command.run_probe().passed
        else:
            with pytest.raises(ExecutionPreparationError, match='protocol_invalid'): command.run_probe()
        with pytest.raises(ExecutionPreparationError, match='consumed'): command.run_probe()
    assert not list(selection['staging_parent'].iterdir())
