"""Application-only fixed-base dependency plan; no loading or admission API."""
from pathlib import Path

from .cpython_base import select_cpython312_base
from .runtime_primitives import NativeExecutionUnsupported
from .windows_loader import require_no_redirection
from .windows_os_policy import owned_os_policy_bytes, parse_os_policy, read_prepared_os_policy
from .native_load import (NativeNode, NativeDependencyPlan, ROOT_PRIVATE, read_member as _read,
    plan_native_graph, FILE_LIMIT, EDGE_LIMIT, BYTE_LIMIT)


def plan_base_native_dependencies(command, target: Path, *, dynamic_dependencies=None,
                                 dynamic_closure_known=False):
    """Own base/profile selection; the shared stdlib graph preserves frozen rules."""
    command.verify()
    root, inventory = command.invocation.cwd, command.inventory
    select_cpython312_base(root)
    if not isinstance(target, Path) or not target.is_relative_to(root):
        raise NativeExecutionUnsupported('execution_target_selection_unsupported')
    require_no_redirection(command, target_parent=target.parent.relative_to(root))
    raw_policy = read_prepared_os_policy(runtime_root=root, inventory=inventory)
    if raw_policy != owned_os_policy_bytes():
        raise NativeExecutionUnsupported('execution_native_plan_policy_changed')
    policy = parse_os_policy(raw_policy)
    plan = plan_native_graph(root, inventory, target, policy.component_names | policy.contract_names,
        dynamic_dependencies=dynamic_dependencies, dynamic_closure_known=dynamic_closure_known,
        limits=(FILE_LIMIT, EDGE_LIMIT, BYTE_LIMIT))
    command.verify()
    return plan


def diagnose_acquired_source_dependencies(facts=(), **observations):
    """Explicit retained-source diagnostic; never feeds the controlled loader."""
    from .acquired_native_recipe import describe_acquired_sources
    return describe_acquired_sources(facts, **observations)


def compare_plan_loaded_base(command, plan: NativeDependencyPlan, reader):
    """Internal owned-reader comparison; no saved origin list grants authority."""
    command.verify()
    root = command.invocation.cwd
    from .runtime_primitives import require_unique_native_origins
    origins = reader.native_origins()
    if type(origins) is not tuple or not 1 <= len(origins) <= 2048 or len(set(origins)) != len(origins):
        raise NativeExecutionUnsupported('execution_module_observation_unavailable')
    require_unique_native_origins(origins)
    command.verify_loaded_origins(origins)
    entries = {entry.name: entry for entry in command.inventory.files}
    nodes = {node.name: node for node in plan.nodes}
    for name in plan.required_loaded_base:
        entry, node = entries.get(name), nodes.get(name)
        if name not in ROOT_PRIVATE or entry is None or node is None or (
                entry.size_bytes != node.size_bytes or entry.sha256 != node.sha256):
            raise NativeExecutionUnsupported('execution_native_plan_source_changed')
        _read(root, entry)
        if root / name not in origins:
            raise NativeExecutionUnsupported('execution_native_plan_base_not_loaded')
    command.verify()
