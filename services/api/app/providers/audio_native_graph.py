"""Fixed audio static graph with exact private manifests; no load authority.

The legacy native walker is intentionally unchanged. This small walker owns
the two audio targets and uses its parsers and finite graph bounds.
"""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

from .native_load import (NativeDependencyPlan, NativeNode, ROOT_PRIVATE,
                         FILE_LIMIT, EDGE_LIMIT, BYTE_LIMIT, read_member)
from .native_manifest import CPYTHON_COMMON_CONTROLS
from .pe_imports import parse_pe_imports
from .pe_resources import (PEView, ResourceClassification, classify_pe_resources,
                           read_private_resource_leaves, private_export_dependencies)
from .runtime_primitives import NativeExecutionUnsupported, _exact_path, require_search_topology

TARGETS = ('Lib/site-packages/_cffi_backend.cp312-win_amd64.pyd',
           'Lib/site-packages/_soundfile_data/libsndfile_x64.dll')
SLOTS = ('python.exe', *sorted(ROOT_PRIVATE), *TARGETS)
RECIPE = 'soundfile-audio-two-target-preflight-v2'


@dataclass(frozen=True)
class AudioGraphPolicy:
    members: tuple[dict, ...]
    manifests: dict
    identity_sha256: str


def _owned_bytes(name):
    path = Path(__file__).with_name(name)
    _exact_path(path, directory=False)
    with path.open('rb') as source: raw = source.read(65537)
    if not 0 < len(raw) <= 65536:
        raise NativeExecutionUnsupported('execution_audio_graph_policy_invalid')
    return raw


def owned_audio_graph_policy():
    """Read implementation-owned catalogs only, never request-supplied policy."""
    try:
        acquired_raw = _owned_bytes('acquired_native_recipe.json')
        cpu_raw = _owned_bytes('cpu_native_recipe.json')
        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value: raise ValueError()
                value[key] = item
            return value
        acquired = json.loads(acquired_raw, object_pairs_hook=unique)
        cpu = json.loads(cpu_raw, object_pairs_hook=unique)
        if (acquired['recipe'] != 'omnivoice-acquired-native-static-v1' or
            type(acquired['version']) is not int or acquired['version'] != 1 or
            cpu['recipe'] != 'omnivoice-cpu-native-static-v1' or
            type(cpu['version']) is not int or cpu['version'] != 1 or len(cpu['manifests']) != 3):
            raise ValueError()
        rows = tuple(row for row in acquired['members'] if row['slot'] in SLOTS)
        if len(rows) != len(SLOTS) or {row['slot'] for row in rows} != set(SLOTS): raise ValueError()
        common = hashlib.sha256(CPYTHON_COMMON_CONTROLS).hexdigest()
        for digest, form in cpu['manifests'].items():
            payload = bytes.fromhex(form['hex'])
            if (hashlib.sha256(payload).hexdigest() != digest or form['size_bytes'] != len(payload) or
                form['ids'] != [24, 2, 1033] or type(form['codepage']) is not int or form['codepage'] not in (0, 1252)):
                raise ValueError()
        for row in rows:
            resource, deps = row['resources'], row['dependencies']
            manifest = resource['manifest_sha256']
            if (type(row['size_bytes']) is not int or not 0 < row['size_bytes'] <= 64 * 1024**2 or
                not re.fullmatch('[a-f0-9]{64}', row['sha256']) or
                type(resource['leaf_count']) is not int or not 0 <= resource['leaf_count'] <= 256 or
                type(deps) is not list or deps != sorted(set(deps)) or len(deps) > EDGE_LIMIT or
                any(not isinstance(name, str) or not re.fullmatch(r'[a-z0-9_][a-z0-9_.-]*\.dll', name)
                    or '..' in name for name in deps) or
                manifest not in {None, common, *cpu['manifests']} or
                (manifest == common and (row['slot'] not in ('python.exe', 'python312.dll') or
                    resource['kind'] != 'requires_os_sxs_binding')) or
                (manifest in cpu['manifests'] and resource['kind'] != 'requires_private_root_context') or
                (manifest is None and resource['kind'] not in ('no_resources', 'data_resources'))):
                raise ValueError()
        implementation = _owned_bytes('audio_native_graph.py')
        identity = hashlib.sha256(json.dumps({'recipe': RECIPE,
            'acquired_sha256': hashlib.sha256(acquired_raw).hexdigest(),
            'cpu_sha256': hashlib.sha256(cpu_raw).hexdigest(),
            'implementation_sha256': hashlib.sha256(implementation).hexdigest()},
            sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return AudioGraphPolicy(rows, cpu['manifests'], identity)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise NativeExecutionUnsupported('execution_audio_graph_policy_invalid') from None


def _inspect(payload, row, manifests):
    if (len(payload), hashlib.sha256(payload).hexdigest()) != (row['size_bytes'], row['sha256']):
        raise NativeExecutionUnsupported('execution_audio_target_identity_changed')
    expected = row['resources']
    manifest = expected['manifest_sha256']
    if manifest == hashlib.sha256(CPYTHON_COMMON_CONTROLS).hexdigest():
        resources = classify_pe_resources(payload, role='bootstrap_exe' if row['slot'] == 'python.exe' else 'dll')
    else:
        leaves = read_private_resource_leaves(PEView(payload))
        selected = [leaf for leaf in leaves or () if leaf.ids[0] == 24]
        if manifest is None:
            if selected: raise NativeExecutionUnsupported('execution_audio_resource_changed')
            resources = ResourceClassification('no_resources' if leaves is None else 'data_resources', len(leaves or ()))
        else:
            form = manifests.get(manifest)
            if (form is None or len(selected) != 1 or selected[0].ids != tuple(form['ids']) or
                selected[0].codepage != form['codepage'] or selected[0].data != bytes.fromhex(form['hex'])):
                raise NativeExecutionUnsupported('execution_audio_resource_changed')
            resources = ResourceClassification('requires_private_root_context', len(leaves or ()), manifest)
    if (resources.kind, resources.leaf_count, resources.manifest_sha256) != (
        expected['kind'], expected['leaf_count'], manifest):
        raise NativeExecutionUnsupported('execution_audio_resource_changed')
    imports = parse_pe_imports(payload)
    if imports.dependencies != tuple(row['dependencies']):
        raise NativeExecutionUnsupported('execution_audio_direct_imports_changed')
    return resources, imports, private_export_dependencies(payload)


def inspect_audio_member(payload, slot):
    """Classify one fixed member from bytes; never resolves or executes it."""
    policy = owned_audio_graph_policy()
    row = next((row for row in policy.members if row['slot'] == slot), None)
    if row is None: raise NativeExecutionUnsupported('execution_audio_target_identity_changed')
    return _inspect(payload, row, policy.manifests)


def plan_audio_native_graph(root, inventory, target, os_names):
    """Two fixed targets, exact catalogs, known static edges; dynamic stays unknown."""
    if not isinstance(target, Path) or target not in tuple(root / slot for slot in TARGETS):
        raise NativeExecutionUnsupported('execution_target_selection_unsupported')
    _exact_path(target, directory=False)
    policy = owned_audio_graph_policy()
    expected = {row['slot']: row for row in policy.members}
    entries = {row.name: row for row in inventory.files}
    if len(entries) != len(inventory.files): raise NativeExecutionUnsupported('execution_audio_target_identity_changed')
    require_search_topology(tuple(entries), inventory.directories, os_names,
                            target_parent=target.parent.relative_to(root))
    candidates = {}
    for slot in entries:
        path = root / slot
        if path.suffix.casefold() in ('.dll', '.pyd') and (path.parent == target.parent or slot in ROOT_PRIVATE):
            name = path.name.casefold()
            if name in candidates: raise NativeExecutionUnsupported('execution_dll_basename_collision')
            if slot not in expected or (slot in TARGETS and path != target):
                raise NativeExecutionUnsupported('execution_audio_graph_member_unsupported')
            candidates[name] = slot
    if len(candidates) > FILE_LIMIT: raise NativeExecutionUnsupported('execution_native_plan_limit')
    nodes, edges, required, total = {}, set(), set(), 0
    pending = [target.relative_to(root).as_posix(), 'python.exe']
    while pending:
        slot = pending.pop()
        if slot in nodes: continue
        if len(nodes) >= FILE_LIMIT: raise NativeExecutionUnsupported('execution_native_plan_limit')
        entry, row = entries.get(slot), expected.get(slot)
        if entry is None or row is None or entry.role != ('interpreter' if slot == 'python.exe' else 'dependency'):
            raise NativeExecutionUnsupported('execution_native_plan_member_unsupported')
        payload = read_member(root, entry)
        total += len(payload)
        if total > BYTE_LIMIT: raise NativeExecutionUnsupported('execution_native_plan_limit')
        resources, imports, forwarders = _inspect(payload, row, policy.manifests)
        nodes[slot] = NativeNode(slot, len(payload), entry.sha256, resources)
        for name in sorted(set(imports.dependencies) | set(forwarders)):
            if name in os_names: resolved = 'OS:' + name
            elif name in candidates:
                resolved = candidates[name]
                if (root / resolved).parent != target.parent: required.add(resolved)
                pending.append(resolved)
            else: raise NativeExecutionUnsupported('execution_native_plan_dependency_unknown')
            edges.add((slot, name, resolved))
            if len(edges) > EDGE_LIMIT: raise NativeExecutionUnsupported('execution_native_plan_limit')
    return NativeDependencyPlan(target.relative_to(root).as_posix(), tuple(nodes[k] for k in sorted(nodes)),
        tuple(sorted(edges)), tuple(sorted(required)),
        any(node.resources.kind == 'requires_os_sxs_binding' for node in nodes.values()), True)
