"""Offline two-target graph preflight for the frozen SoundFile component.

This describes known target graphs only. It never loads a native module or
authorizes dispatch; unresolved edges remain explicit blockers.
"""
from dataclasses import dataclass
import hashlib
import json
import re
from pathlib import Path

from .acquired_native_recipe import parse_acquired_descriptor, owned_acquired_bytes
from .audio_native_graph import plan_audio_native_graph, owned_audio_graph_policy, RECIPE
from .native_load import NativeDependencyPlan
from .private_native_recipe import AUDIO_SLOTS, SOURCE_LIMIT, _owned as owned_private_recipe
from .runtime_primitives import NativeExecutionUnsupported, _exact_path
from .soundfile_binding import (SoundFileDerivation, UPSTREAM_SLOT, GENERATED_SLOT,
    derive_soundfile)


def plan_prepared_audio_graph(command, target):
    """Preserve fixed base, redirection and owned OS policy selection."""
    from .cpython_base import select_cpython312_base
    from .windows_loader import require_no_redirection
    from .windows_os_policy import owned_os_policy_bytes, parse_os_policy, read_prepared_os_policy
    command.verify()
    root = command.invocation.cwd
    select_cpython312_base(root)
    require_no_redirection(command, target_parent=target.parent.relative_to(root))
    raw = read_prepared_os_policy(runtime_root=root, inventory=command.inventory)
    if raw != owned_os_policy_bytes():
        raise NativeExecutionUnsupported('execution_native_plan_policy_changed')
    policy = parse_os_policy(raw)
    result = plan_audio_native_graph(root, command.inventory, target,
        policy.component_names | policy.contract_names)
    command.verify()
    return result


@dataclass(frozen=True)
class AudioTargetGraph:
    target: str
    plan: NativeDependencyPlan | None
    blocker: str | None


@dataclass(frozen=True)
class AudioNativePreflight:
    acquired_descriptor_sha256: str
    private_recipe_sha256: str
    inventory_sha256: str
    soundfile_derivation_sha256: str
    targets: tuple[AudioTargetGraph, ...]
    identity_sha256: str
    record: bytes

    @property
    def unresolved(self):
        return tuple((row.target, row.blocker or 'dynamic_closure_unverified')
                     for row in self.targets
                     if row.blocker is not None or row.plan.requires_dynamic_closure)

    @property
    def dispatch_authorized(self):
        return False


def _source_bytes(root, inventory, slot):
    rows = [item for item in inventory.files if item.name == slot]
    if len(rows) != 1 or rows[0].role != 'dependency':
        raise NativeExecutionUnsupported('execution_audio_source_identity_missing')
    entry = rows[0]
    if type(entry.size_bytes) is not int or not 0 < entry.size_bytes <= SOURCE_LIMIT:
        raise NativeExecutionUnsupported('execution_audio_source_identity_invalid')
    path = root / slot
    _exact_path(path, directory=False)
    with path.open('rb') as source:
        payload = source.read(entry.size_bytes + 1)
    if len(payload) != entry.size_bytes or hashlib.sha256(payload).hexdigest() != entry.sha256:
        raise NativeExecutionUnsupported('execution_audio_source_identity_changed')
    return entry, payload


def _validate_derivation(value):
    try:
        if (type(value.payload) is not bytes or not 0 < len(value.payload) <= 4 * 1024**2
                or type(value.descriptor) is not bytes or not 0 < len(value.descriptor) <= 64 * 1024):
            raise ValueError()
        def unique_object(pairs):
            result = {}
            for key, item in pairs:
                if key in result:
                    raise ValueError()
                result[key] = item
            return result

        descriptor = json.loads(value.descriptor, object_pairs_hook=unique_object)
        expected = {'recipe', 'source_kind', 'upstream_sha256', 'transform_sha256',
            'derived_sha256', 'binder_sha256', 'handle_core_sha256', 'private_policy_sha256',
            'dependencies', 'terms', 'dispatch_authorized', 'backend_handle_semantics'}
        if (type(descriptor) is not dict or set(descriptor) != expected
                or descriptor['recipe'] != 'soundfile-owned-handle-v2'
                or descriptor['source_kind'] != 'derived' or descriptor['dispatch_authorized'] is not False
                or descriptor['backend_handle_semantics'] != 'not_verified'
                or descriptor['derived_sha256'] != hashlib.sha256(value.payload).hexdigest()
                or any(type(descriptor[key]) is not str or
                    re.fullmatch('[a-f0-9]{64}', descriptor[key]) is None
                    for key in ('upstream_sha256', 'transform_sha256', 'derived_sha256',
                                'binder_sha256', 'handle_core_sha256', 'private_policy_sha256'))
                or type(descriptor['dependencies']) is not list or type(descriptor['terms']) is not list):
            raise ValueError()
        return hashlib.sha256(value.descriptor).hexdigest()
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        raise NativeExecutionUnsupported('execution_audio_derivation_invalid') from None


def preflight_audio_native_targets(command, derivation: SoundFileDerivation) -> AudioNativePreflight:
    """Build independent plans for CFFI and libsndfile from one frozen tree.

    The tree retains the original soundfile.py and unchanged generated FFI
    source. The derivation is a separate byte-transform artifact, not _soundfile.py.
    This is static/read-only preflight. A target with unknown imports or
    unverified dynamic closure remains blocked, even if its known graph parses.
    """
    if type(derivation) is not SoundFileDerivation or derivation.dispatch_authorized:
        raise NativeExecutionUnsupported('execution_audio_derivation_invalid')
    derivation_identity = _validate_derivation(derivation)
    if (type(AUDIO_SLOTS) is not tuple or len(AUDIO_SLOTS) != 2
            or len({slot.casefold() for slot in AUDIO_SLOTS}) != 2
            or any(first.casefold().startswith(second.casefold() + '/')
                   or second.casefold().startswith(first.casefold() + '/')
                   for index, first in enumerate(AUDIO_SLOTS) for second in AUDIO_SLOTS[index + 1:])):
        raise NativeExecutionUnsupported('execution_audio_recipe_mismatch')
    command.verify()
    root, inventory = command.invocation.cwd, command.inventory
    acquired, acquired_identity = parse_acquired_descriptor(owned_acquired_bytes())
    private, private_identity = owned_private_recipe()
    acquired_rows = {row['slot']: row for row in acquired['members']}
    private_rows = {row['slot']: row for row in private['members']}
    if tuple(AUDIO_SLOTS) != tuple(row['slot'] for row in sorted(
            (row for row in private['members'] if row['slot'] in AUDIO_SLOTS),
            key=lambda row: AUDIO_SLOTS.index(row['slot']))):
        raise NativeExecutionUnsupported('execution_audio_recipe_mismatch')

    upstream, upstream_payload = _source_bytes(root, inventory, UPSTREAM_SLOT)
    generated, generated_payload = _source_bytes(root, inventory, GENERATED_SLOT)
    if (derive_soundfile(upstream_payload) != derivation or
            (upstream.size_bytes, upstream.sha256) != next(
                (row['size_bytes'], row['sha256']) for row in private['sources']
                if row['slot'] == UPSTREAM_SLOT) or
            (generated.size_bytes, generated.sha256) != next(
                (row['size_bytes'], row['sha256']) for row in private['sources']
                if row['slot'] == GENERATED_SLOT)):
        raise NativeExecutionUnsupported('execution_audio_derivation_changed')

    entries = {row.name: row for row in inventory.files}
    result = []
    for target in AUDIO_SLOTS:
        recipe = acquired_rows.get(target)
        private_row = private_rows.get(target)
        entry = entries.get(target)
        if (recipe is None or private_row is None or entry is None or
                (recipe['size_bytes'], recipe['sha256']) !=
                (private_row['size_bytes'], private_row['sha256']) or
                (entry.size_bytes, entry.sha256) !=
                (recipe['size_bytes'], recipe['sha256']) or entry.role != 'dependency'):
            raise NativeExecutionUnsupported('execution_audio_target_identity_changed')
        try:
            plan = plan_prepared_audio_graph(command, root / target)
            target_nodes = [node for node in plan.nodes if node.name == target]
            if (plan.target != target or len(target_nodes) != 1 or
                    (target_nodes[0].size_bytes, target_nodes[0].sha256) !=
                    (recipe['size_bytes'], recipe['sha256'])):
                raise NativeExecutionUnsupported('execution_audio_target_graph_changed')
            result.append(AudioTargetGraph(target, plan, None))
        except NativeExecutionUnsupported as failure:
            # Diagnostics use existing fixed codes; preserve no exception text.
            result.append(AudioTargetGraph(target, None, str(failure)))
    command.verify()

    base = {
        'recipe': RECIPE,
        'audio_graph_policy_sha256': owned_audio_graph_policy().identity_sha256,
        'acquired_descriptor_sha256': acquired_identity,
        'private_recipe_sha256': private_identity,
        'inventory_sha256': inventory.descriptor.sha256,
        'soundfile_derivation_sha256': derivation_identity,
        'targets': [
            {'target': row.target, 'blocker': row.blocker,
             'plan': None if row.plan is None else {
                 'nodes': [(node.name, node.size_bytes, node.sha256,
                    node.resources.kind, node.resources.leaf_count,
                    node.resources.manifest_sha256) for node in row.plan.nodes],
                 'edges': row.plan.edges,
                 'required_loaded_base': row.plan.required_loaded_base,
                 'requires_os_sxs_binding': row.plan.requires_os_sxs_binding,
                 'requires_dynamic_closure': row.plan.requires_dynamic_closure,
             }} for row in result
        ],
        'dispatch_authorized': False,
    }
    record = json.dumps(base, sort_keys=True, separators=(',', ':')).encode()
    identity = hashlib.sha256(record).hexdigest()
    return AudioNativePreflight(acquired_identity, private_identity, inventory.descriptor.sha256,
        derivation_identity, tuple(result), identity, record)
