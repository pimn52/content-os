"""Read already-loaded fixed audio contexts; never load or grant authority.

Separate from the legacy CommonControls reader and diagnostic bundles. The
process executable and actual module enumeration select every queried source;
the supplied inventory cannot nominate a handle or create an association.
"""
from dataclasses import dataclass
import hashlib
from pathlib import Path
import sys

from app.domain.execution_runtime import RuntimeInventory
from .audio_native_graph import owned_audio_graph_policy, inspect_audio_member
from .common_controls_binding import _read_stable
from .runtime_primitives import NativeExecutionUnsupported, _exact_path
from .windows_activation import (ActivationContextSnapshot, ActivationMetadata,
    CONTEXT_MODULE_LIMIT, PE_LIMIT, USE_ACTIVE, IS_MODULE)

RECIPE = 'audio-private-context-observation-v1'


@dataclass(frozen=True)
class AudioContextObservation:
    recipe: str
    policy_sha256: str
    inventory_sha256: str
    executable: Path
    origins: tuple[Path, ...]
    contexts: ActivationContextSnapshot

    @property
    def dispatch_authorized(self): return False


def capture_audio_contexts(reader, inventory):
    """Bounded ABI seam; caller still owes full tree/OS/origin validation.

    OS modules are excluded only from private context queries, never admitted
    as trusted origins here. No CreateActCtx, activation or loader is invoked.
    """
    try:
        if type(inventory) is not RuntimeInventory:
            raise ValueError()
        executable = Path(sys.executable)
        root = executable.parent
        system_directory = reader._system_directory
        inventory_sha256 = inventory.descriptor.sha256
        _exact_path(root, directory=True)
        _exact_path(reader._system_directory, directory=True)
        policy = owned_audio_graph_policy()
        policy_sha256 = policy.identity_sha256
        rows = {row['slot']: row for row in policy.members}
        entries = {row.name: row for row in inventory.files}
        before = reader._modules._snapshot()
        if (type(before) is not tuple or not 1 <= len(before) <= CONTEXT_MODULE_LIMIT or
                any(type(row) is not tuple or len(row) != 2 or type(row[0]) is not int or
                    not 0 < row[0] <= 0xffffffffffffffff or not isinstance(row[1], Path)
                    for row in before) or
                len({handle for handle, _ in before}) != len(before) or
                len({path for _, path in before}) != len(before) or
                executable not in {path for _, path in before} or executable.name != 'python.exe'):
            raise ValueError()
        sources, queried = [], []
        # Every private source is checked before even the effective query.
        for handle, path in before:
            _exact_path(path, directory=False)
            if path != executable and path.parent == system_directory:
                continue
            slot = path.relative_to(root).as_posix()
            member, entry = rows.get(slot), entries.get(slot)
            if (member is None or entry is None or
                    entry.role != ('interpreter' if path == executable else 'dependency') or
                    (entry.size_bytes, entry.sha256) != (member['size_bytes'], member['sha256'])):
                raise ValueError()
            raw = _read_stable(path, limit=PE_LIMIT)
            if (len(raw), hashlib.sha256(raw).hexdigest()) != (entry.size_bytes, entry.sha256):
                raise ValueError()
            resources, _, _ = inspect_audio_member(raw, slot)
            sources.append((path, raw))
            if path == executable:
                if resources.kind != 'requires_os_sxs_binding': raise ValueError()
            elif resources.kind in ('requires_private_root_context', 'requires_os_sxs_binding'):
                queried.append((handle, path))
            elif resources.kind not in ('no_resources', 'data_resources'):
                raise ValueError()
        effective = reader._metadata(None, flags=USE_ACTIVE)
        if type(effective) is not ActivationMetadata or effective.root_manifest != str(executable):
            raise ValueError()
        associated = []
        for handle, path in queried:
            metadata = reader._metadata(handle, flags=IS_MODULE)
            if type(metadata) is not ActivationMetadata or metadata.root_manifest != str(path):
                raise ValueError()
            associated.append((str(path), metadata))
        for (handle, _), (_, metadata) in zip(queried, associated):
            if reader._metadata(handle, flags=IS_MODULE) != metadata: raise ValueError()
        if reader._metadata(None, flags=USE_ACTIVE) != effective or reader._modules._snapshot() != before:
            raise ValueError()
        for path, raw in sources:
            if _read_stable(path, limit=PE_LIMIT) != raw: raise ValueError()
        if (owned_audio_graph_policy().identity_sha256 != policy_sha256 or
                inventory.descriptor.sha256 != inventory_sha256 or
                Path(sys.executable) != executable or reader._system_directory != system_directory or
                reader._metadata(None, flags=USE_ACTIVE) != effective or
                reader._modules._snapshot() != before):
            raise ValueError()
        return AudioContextObservation(RECIPE, policy_sha256, inventory_sha256,
            executable, tuple(path for _, path in before),
            ActivationContextSnapshot(effective, tuple(associated)))
    except (OSError, ValueError, TypeError, AttributeError, RuntimeError, KeyError):
        raise NativeExecutionUnsupported('execution_audio_context_observation_unsupported') from None
