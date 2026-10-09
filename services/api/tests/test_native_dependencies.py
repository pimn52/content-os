"""Temporary fixed trees and fake readers, never live native/model calls."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.providers import host_runtime, native_dependencies as mod
from app.providers.native_dependencies import plan_base_native_dependencies, compare_plan_loaded_base
from app.providers.prepared import NativeExecutionUnsupported
from app.providers.python_startup import prepare_python_command_v2
from app.providers.runtime_tree import RuntimeFileSelection
from app.providers.windows_loader import require_resource_free_target
from app.providers.windows_os_policy import owned_os_policy_bytes, parse_os_policy
from test_python_startup import layout
from test_pe_resources import resource_image, manifest_image, forwarder_image


@pytest.fixture
def sources(layout, monkeypatch):
    system = layout['windows_root'] / 'System32'
    for name in parse_os_policy(owned_os_policy_bytes()).component_names:
        (system / name).write_bytes(b'synthetic OS')
    monkeypatch.setattr(host_runtime.sys, 'platform', 'win32')
    monkeypatch.setattr(host_runtime, '_read_windows_facts', lambda:
        host_runtime.WindowsFacts(system.parent, 0x8664, 0, 26100, 1, system))
    base = layout['files'][0].source.parent
    source = layout['trees'][0].source / 'site-packages/app/providers'
    (source / 'windows_os_policy.json').write_bytes(owned_os_policy_bytes())
    for name in ('python312.dll', 'python3.dll', 'vcruntime140.dll', 'vcruntime140_1.dll'):
        (base / name).write_bytes(resource_image())
    (base / 'python.exe').write_bytes(resource_image(exe=True))
    layout['files'] += tuple(RuntimeFileSelection(base / name, name, 'dependency')
        for name in ('vcruntime140.dll', 'vcruntime140_1.dll'))
    (base / 'DLLs/target.pyd').write_bytes(resource_image(ordinary=('python312.dll', 'kernel32.dll')))
    # Unreferenced optional file has an unsupported import/resource; retained.
    (base / 'DLLs/optional.pyd').write_bytes(resource_image([(10, 1, 1033, b'unknown', 0)],
        ordinary=('unknown.dll',)))
    layout['max_bytes'] = 200_000
    return layout, base


def open_command(sources): return prepare_python_command_v2(device='cpu', **sources[0])


def reader(command, *, paths=None):
    root = command.invocation.cwd
    return SimpleNamespace(native_origins=lambda: paths if paths is not None else
        (root / 'python.exe', root / 'python312.dll'))


def test_reachable_graph_retains_optional_inventory_and_requires_exact_loaded_base(sources):
    with open_command(sources) as command:
        root = command.invocation.cwd
        plan = plan_base_native_dependencies(command, root / 'DLLs/target.pyd')
        assert {n.name for n in plan.nodes} == {'DLLs/target.pyd', 'python.exe', 'python312.dll'}
        assert plan.required_loaded_base == ('python312.dll',)
        assert plan.requires_dynamic_closure and not plan.dispatch_authorized
        assert 'DLLs/optional.pyd' in {e.name for e in command.inventory.files}
        compare_plan_loaded_base(command, plan, reader(command))


@pytest.mark.parametrize('problem', ['missing', 'elsewhere', 'changed_identity', 'duplicate', 'list'])
def test_loaded_baseline_and_plan_identity_never_replaced_by_basename(sources, problem):
    with open_command(sources) as command:
        root = command.invocation.cwd
        plan = plan_base_native_dependencies(command, root / 'DLLs/target.pyd')
        paths = (root / 'python.exe',)
        if problem == 'elsewhere': paths += (root / 'DLLs/python312.dll',)
        if problem == 'duplicate': paths += paths
        if problem == 'list': paths = list(paths)
        if problem == 'changed_identity':
            plan = replace(plan, nodes=tuple(replace(n, sha256='0' * 64) if n.name == 'python312.dll' else n
                for n in plan.nodes))
        with pytest.raises(RuntimeError): compare_plan_loaded_base(command, plan, reader(command, paths=paths))


def test_plan_does_not_change_existing_manifest_loader_gate(sources):
    layout, base = sources
    (base / 'DLLs/target.pyd').write_bytes(manifest_image())
    with open_command(sources) as command:
        path = command.invocation.cwd / 'DLLs/target.pyd'
        plan = plan_base_native_dependencies(command, path, dynamic_closure_known=True)
        assert plan.requires_os_sxs_binding and not plan.requires_dynamic_closure
        assert not plan.dispatch_authorized
        entry = next(e for e in command.inventory.files if e.name == plan.target)
        with pytest.raises(NativeExecutionUnsupported, match='target_resources_unsupported'):
            require_resource_free_target(path, entry)


@pytest.mark.parametrize('contract', sorted(parse_os_policy(owned_os_policy_bytes()).contract_names))
def test_exact_reviewed_contract_is_a_graph_edge_not_a_loaded_file_requirement(sources, contract):
    (sources[1] / 'DLLs/target.pyd').write_bytes(resource_image(ordinary=(contract,)))
    with open_command(sources) as command:
        plan = plan_base_native_dependencies(command, command.invocation.cwd / 'DLLs/target.pyd')
        assert ('DLLs/target.pyd', contract, 'OS:' + contract) in plan.edges
        assert not plan.required_loaded_base and not plan.dispatch_authorized


@pytest.mark.parametrize('contract', ['api-ms-win-crt-private-l1-2-0.dll',
    'api-ms-win-crt-heap-l1-2-0.dll', 'api-ms-win-core-path-l1-2-0.dll'])
def test_api_prefix_and_near_version_do_not_close_unknown_graph_edges(sources, contract):
    (sources[1] / 'DLLs/target.pyd').write_bytes(resource_image(ordinary=(contract,)))
    with open_command(sources) as command:
        with pytest.raises(NativeExecutionUnsupported, match='native_plan_dependency_unknown'):
            plan_base_native_dependencies(command, command.invocation.cwd / 'DLLs/target.pyd')


def test_private_forwarder_is_reachable_even_without_imports(sources):
    (sources[1] / 'DLLs/target.pyd').write_bytes(forwarder_image())
    with open_command(sources) as command:
        plan = plan_base_native_dependencies(command, command.invocation.cwd / 'DLLs/target.pyd')
        assert ('DLLs/target.pyd', 'python312.dll', 'python312.dll') in plan.edges
        assert plan.required_loaded_base == ('python312.dll',)


def test_declared_dynamic_edges_cycles_and_delays_are_checked(sources):
    base = sources[1]
    (base / 'DLLs/target.pyd').write_bytes(resource_image(delayed=('child.dll',)))
    (base / 'DLLs/child.dll').write_bytes(resource_image(ordinary=('kernel32.dll',)))
    with open_command(sources) as command:
        plan = plan_base_native_dependencies(command, command.invocation.cwd / 'DLLs/target.pyd',
            dynamic_dependencies={'DLLs/child.dll': ('child.dll', 'vcruntime140.dll')}, dynamic_closure_known=True)
        assert 'DLLs/child.dll' in {n.name for n in plan.nodes}
        assert 'vcruntime140.dll' in plan.required_loaded_base
        assert ('DLLs/child.dll', 'child.dll', 'DLLs/child.dll') in plan.edges
        assert not plan.dispatch_authorized


@pytest.mark.parametrize('problem', ['unknown', 'cross_directory', 'shadow', 'collision', 'refer_optional'])
def test_unknown_edges_search_collisions_and_reachable_optional_still_block(sources, problem):
    layout, base = sources
    dependency = {'unknown': 'unknown.dll', 'cross_directory': 'python3.dll',
        'refer_optional': 'optional.dll'}.get(problem, 'kernel32.dll')
    (base / 'DLLs/target.pyd').write_bytes(resource_image(ordinary=(dependency,)))
    if problem == 'shadow': (base / 'DLLs/kernel32.dll').write_bytes(resource_image())
    if problem == 'collision': (base / 'DLLs/python312.dll').write_bytes(resource_image())
    if problem == 'refer_optional':
        (base / 'DLLs/optional.dll').write_bytes(resource_image(ordinary=('unknown.dll',)))
    with open_command(sources) as command:
        with pytest.raises(RuntimeError):
            plan_base_native_dependencies(command, command.invocation.cwd / 'DLLs/target.pyd')


def test_full_verify_keeps_changed_unreachable_member_visible(sources):
    with open_command(sources) as command:
        root = command.invocation.cwd
        (root / 'DLLs/optional.pyd').write_bytes(b'changed')
        with pytest.raises(RuntimeError): plan_base_native_dependencies(command, root / 'DLLs/target.pyd')


@pytest.mark.parametrize('kind', ['files', 'edges', 'bytes'])
def test_plan_limits_fail_without_loading(sources, monkeypatch, kind):
    monkeypatch.setattr(mod, {'files': 'FILE_LIMIT', 'edges': 'EDGE_LIMIT', 'bytes': 'BYTE_LIMIT'}[kind], 1)
    with open_command(sources) as command:
        with pytest.raises(NativeExecutionUnsupported, match='native_plan_limit'):
            plan_base_native_dependencies(command, command.invocation.cwd / 'DLLs/target.pyd')


@pytest.mark.parametrize('declaration', [{'DLLs/target.pyd': ('../bad.dll',)},
    {'not-selected.dll': ()}, {'DLLs/target.pyd': ['kernel32.dll']}, []])
def test_dynamic_recipe_inputs_are_bounded_owned_declarations(sources, declaration):
    with open_command(sources) as command:
        with pytest.raises(NativeExecutionUnsupported, match='recipe_unsupported'):
            plan_base_native_dependencies(command, command.invocation.cwd / 'DLLs/target.pyd',
                dynamic_dependencies=declaration)
