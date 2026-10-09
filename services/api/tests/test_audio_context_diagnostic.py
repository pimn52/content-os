"""Combined diagnostic with real synthetic PE, fake ABI; no OS calls."""
from dataclasses import replace
import hashlib
import json

import pytest

from app.providers import audio_context_diagnostic as diagnostic
from app.providers.runtime_primitives import NativeExecutionUnsupported
from test_audio_context_prediction import captured, exact_graph


def test_one_diagnostic_binds_source_observation_without_loading(captured):
    result = diagnostic.diagnose_audio_contexts(captured.reader, captured.inventory)
    assert result.dispatch_authorized is False
    assert hashlib.sha256(result.record).hexdigest() == result.identity_sha256
    payload = json.loads(result.record)
    assert payload['load_attempts'] == 0
    assert payload['dispatch_authorized'] is False
    assert payload['inventory_sha256'] == captured.inventory.descriptor.sha256
    assert len(payload['predictions']) == 2
    assert diagnostic.diagnose_audio_contexts(captured.reader, captured.inventory).record == result.record


def test_source_prediction_must_not_change_context_state(captured):
    original = captured.reader.inspect_acquired_pe
    query = captured.reader._metadata
    def inspect(source, slot):
        value = original(source, slot)
        captured.reader._metadata = lambda handle, flags: replace(query(handle, flags), flags=1)
        return value
    captured.reader.inspect_acquired_pe = inspect
    with pytest.raises(NativeExecutionUnsupported, match='diagnostic_changed'):
        diagnostic.diagnose_audio_contexts(captured.reader, captured.inventory)


def test_source_changed_during_predictions_stops_combined_consumer(captured):
    original = captured.reader.inspect_acquired_pe
    def inspect(source, slot):
        value = original(source, slot)
        source.write_bytes(b'changed')
        return value
    captured.reader.inspect_acquired_pe = inspect
    with pytest.raises(NativeExecutionUnsupported):
        diagnostic.diagnose_audio_contexts(captured.reader, captured.inventory)
