"""Independent fixed-source predictions and raw association matching only."""
from dataclasses import dataclass
import hashlib
from pathlib import Path
import sys

from app.domain.execution_runtime import RuntimeInventory
from .audio_context_observation import AudioContextObservation, RECIPE as OBSERVATION_RECIPE
from .audio_native_graph import owned_audio_graph_policy, inspect_audio_member
from .common_controls_binding import _read_stable
from .cpu_native_recipe import (PrivateRootPrediction, compare_private_root_metadata,
    compare_private_root_association)
from .runtime_primitives import NativeExecutionUnsupported, _exact_path
from .windows_activation import PE_LIMIT

RECIPE = 'audio-private-source-predictions-v1'


@dataclass(frozen=True)
class AudioSourcePredictions:
    recipe: str
    policy_sha256: str
    inventory_sha256: str
    executable: Path
    predictions: tuple[tuple[str, PrivateRootPrediction], ...]

    @property
    def dispatch_authorized(self): return False


def capture_audio_predictions(reader, inventory):
    """Reuse metadata-only source prediction ABI, never actual module handles.

    A production invocation would query source-created contexts; this interface
    is not connected to a child/loader and carries no authority to invoke it.
    """
    try:
        if type(inventory) is not RuntimeInventory: raise ValueError()
        executable = Path(sys.executable)
        if executable.name != 'python.exe': raise ValueError()
        root = executable.parent
        _exact_path(root, directory=True)
        policy = owned_audio_graph_policy()
        identity, inventory_identity = policy.identity_sha256, inventory.descriptor.sha256
        rows = tuple(row for row in policy.members
                     if row['resources']['kind'] == 'requires_private_root_context')
        entries = {row.name: row for row in inventory.files}
        sources, result = [], []
        # Hash/classify all independently selected sources before any query.
        for row in rows:
            slot = row['slot']; entry = entries.get(slot)
            if (entry is None or entry.role != 'dependency' or
                    (entry.size_bytes, entry.sha256) != (row['size_bytes'], row['sha256'])):
                raise ValueError()
            source = root / slot
            raw = _read_stable(source, limit=PE_LIMIT)
            resources, _, _ = inspect_audio_member(raw, slot)
            if (resources.kind != 'requires_private_root_context' or
                    (len(raw), hashlib.sha256(raw).hexdigest()) != (entry.size_bytes, entry.sha256)):
                raise ValueError()
            sources.append((row, source, raw, resources))
        for row, source, raw, resources in sources:
            predicted = reader.inspect_acquired_pe(source, row['slot'])
            if (type(predicted) is not PrivateRootPrediction or
                    predicted.source != str(source) or predicted.source_parent != str(source.parent) or
                    predicted.source_sha256 != row['sha256'] or
                    predicted.manifest_sha256 != resources.manifest_sha256 or
                    compare_private_root_metadata(predicted.metadata, source=source,
                        source_sha256=row['sha256'], manifest_sha256=resources.manifest_sha256) != predicted):
                raise ValueError()
            result.append((row['slot'], predicted))
        for _, source, raw, _ in sources:
            if _read_stable(source, limit=PE_LIMIT) != raw: raise ValueError()
        if (owned_audio_graph_policy().identity_sha256 != identity or
                inventory.descriptor.sha256 != inventory_identity or Path(sys.executable) != executable):
            raise ValueError()
        return AudioSourcePredictions(RECIPE, identity, inventory_identity, executable, tuple(result))
    except (OSError, ValueError, TypeError, AttributeError, RuntimeError, KeyError):
        raise NativeExecutionUnsupported('execution_audio_source_prediction_unsupported') from None


def match_audio_private_associations(observed, predicted):
    """Like-for-like private-root matching, not a full transition/load guard."""
    try:
        if (type(observed) is not AudioContextObservation or type(predicted) is not AudioSourcePredictions or
                observed.recipe != OBSERVATION_RECIPE or predicted.recipe != RECIPE or
                (observed.policy_sha256, observed.inventory_sha256, observed.executable) !=
                (predicted.policy_sha256, predicted.inventory_sha256, predicted.executable)):
            raise ValueError()
        policy = owned_audio_graph_policy()
        if policy.identity_sha256 != observed.policy_sha256: raise ValueError()
        private = {row['slot']: row for row in policy.members
                   if row['resources']['kind'] == 'requires_private_root_context'}
        predictions = dict(predicted.predictions)
        if len(predictions) != len(predicted.predictions) or set(predictions) != set(private): raise ValueError()
        associated = dict(observed.contexts.associated)
        if len(associated) != len(observed.contexts.associated): raise ValueError()
        root = observed.executable.parent
        origins = set(observed.origins)
        if len(origins) != len(observed.origins): raise ValueError()
        for slot, row in private.items():
            source = root / slot; value = predictions[slot]
            raw = _read_stable(source, limit=PE_LIMIT)
            if (len(raw), hashlib.sha256(raw).hexdigest()) != (row['size_bytes'], row['sha256']):
                raise ValueError()
            if (type(value) is not PrivateRootPrediction or value.source != str(source) or
                    value.source_parent != str(source.parent) or value.source_sha256 != row['sha256'] or
                    value.manifest_sha256 != row['resources']['manifest_sha256'] or
                    compare_private_root_metadata(value.metadata, source=source,
                        source_sha256=row['sha256'], manifest_sha256=value.manifest_sha256) != value):
                raise ValueError()
            actual = associated.get(str(source))
            if source in origins:
                if actual is None: raise ValueError()
                compare_private_root_association(value, actual)
            elif actual is not None:
                raise ValueError()
    except (OSError, ValueError, TypeError, AttributeError, RuntimeError, KeyError):
        raise NativeExecutionUnsupported('execution_audio_association_match_unsupported') from None
