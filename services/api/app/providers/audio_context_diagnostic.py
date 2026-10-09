"""One read-only consumer, not an execution receipt or admission path."""
from dataclasses import dataclass, asdict
import hashlib
import json

from .audio_context_observation import capture_audio_contexts, AudioContextObservation
from .audio_context_prediction import (capture_audio_predictions, AudioSourcePredictions,
    match_audio_private_associations)
from .runtime_primitives import NativeExecutionUnsupported

RECIPE = 'audio-private-context-diagnostic-v1'


@dataclass(frozen=True)
class AudioContextDiagnostic:
    observation: AudioContextObservation
    predictions: AudioSourcePredictions
    record: bytes
    identity_sha256: str

    @property
    def dispatch_authorized(self): return False


def diagnose_audio_contexts(reader, inventory):
    """No target loading/retry; source-created predictions must leave state unchanged.

    The injected reader is the existing metadata-only ABI seam. This function
    is not a public API, a guarded child launch or a Worker/provider operation.
    Actual invocation still requires its own diagnostic experiment authority.
    """
    before = capture_audio_contexts(reader, inventory)
    predicted = capture_audio_predictions(reader, inventory)
    after = capture_audio_contexts(reader, inventory)
    if before != after:
        raise NativeExecutionUnsupported('execution_audio_context_diagnostic_changed')
    match_audio_private_associations(before, predicted)
    match_audio_private_associations(after, predicted)
    payload = {'recipe': RECIPE, 'observation_recipe': after.recipe,
        'prediction_recipe': predicted.recipe, 'policy_sha256': after.policy_sha256,
        'inventory_sha256': after.inventory_sha256, 'executable': str(after.executable),
        'origins': tuple(str(path) for path in after.origins), 'contexts': asdict(after.contexts),
        'predictions': tuple((slot, asdict(value)) for slot, value in predicted.predictions),
        'load_attempts': 0, 'dispatch_authorized': False}
    record = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    if len(record) > 256 * 1024:
        raise NativeExecutionUnsupported('execution_audio_context_diagnostic_limit')
    return AudioContextDiagnostic(after, predicted, record, hashlib.sha256(record).hexdigest())
