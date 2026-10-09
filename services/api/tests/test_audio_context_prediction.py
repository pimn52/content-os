"""Real synthetic PE/source facts plus fake source/query ABI, no OS calls."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.domain.execution_runtime import RuntimeInventory, RuntimeInventoryFile
from app.providers import audio_context_prediction as prediction
from app.providers import audio_context_observation as observation
from app.providers.audio_native_graph import TARGETS
from app.providers.cpu_native_recipe import compare_private_root_metadata
from app.providers.windows_activation import ActivationMetadata, ActivationAssembly, USE_ACTIVE
from app.providers.runtime_primitives import NativeExecutionUnsupported
from test_audio_native_graph import exact_graph


@pytest.fixture
def captured(exact_graph, monkeypatch):
    root = exact_graph.root
    system = root / 'synthetic-system'; system.mkdir()
    files = tuple(RuntimeInventoryFile(name=row.name, role=row.role,
        size_bytes=row.size_bytes, sha256=row.sha256) for row in exact_graph.inventory.files)
    inventory = RuntimeInventory(inventory_version=1, directories=exact_graph.inventory.directories,
        files=(*files, RuntimeInventoryFile(name='entry.py', role='entrypoint', size_bytes=1, sha256='a' * 64)))
    process = SimpleNamespace(executable=str(root / 'python.exe'))
    monkeypatch.setattr(prediction, 'sys', process)
    monkeypatch.setattr(observation, 'sys', process)
    modules = tuple((index, root / row.name) for index, row in enumerate(exact_graph.inventory.files, 1))
    by_handle = dict(modules)
    def metadata(source):
        assembly = ActivationAssembly('', str(source), '', '', 2, 1, (1, 0), (0, 0), 0, (), 17)
        return ActivationMetadata(1, str(source), '', str(source.parent), (2, 1, 2), (assembly,), 0)
    calls = []
    def inspect(source, slot):
        calls.append(slot)
        row = exact_graph.members[slot]
        return compare_private_root_metadata(metadata(source), source=source,
            source_sha256=row['sha256'], manifest_sha256=row['resources']['manifest_sha256'])
    reader = SimpleNamespace(inspect_acquired_pe=inspect, _system_directory=system,
        _modules=SimpleNamespace(_snapshot=lambda: modules),
        _metadata=lambda handle, flags: metadata(root / 'python.exe' if flags == USE_ACTIVE else by_handle[handle]))
    return SimpleNamespace(graph=exact_graph, root=root, inventory=inventory,
        reader=reader, calls=calls, process=process)


def test_independent_predictions_match_same_source_observations(captured):
    predicted = prediction.capture_audio_predictions(captured.reader, captured.inventory)
    observed = observation.capture_audio_contexts(captured.reader, captured.inventory)
    assert captured.calls == list(TARGETS)
    assert predicted.dispatch_authorized is False
    assert prediction.match_audio_private_associations(observed, predicted) is None


@pytest.mark.parametrize('problem', ['source', 'parent', 'hash', 'manifest', 'raw', 'type', 'bytes', 'root', 'policy'])
def test_changed_or_foreign_prediction_rejected(captured, problem, monkeypatch):
    original = captured.reader.inspect_acquired_pe
    def inspect(source, slot):
        value = original(source, slot)
        changes = {'source': {'source': 'foreign'}, 'parent': {'source_parent': 'foreign'},
            'hash': {'source_sha256': 'c' * 64}, 'manifest': {'manifest_sha256': 'c' * 64},
            'raw': {'metadata': replace(value.metadata, flags=1)}}
        if problem in changes: return replace(value, **changes[problem])
        if problem == 'type': return value.metadata
        if problem == 'bytes': source.write_bytes(b'changed during query')
        if problem == 'root': captured.process.executable = str(captured.root / 'foreign.exe')
        if problem == 'policy':
            original_policy = prediction.owned_audio_graph_policy
            monkeypatch.setattr(prediction, 'owned_audio_graph_policy',
                lambda: replace(original_policy(), identity_sha256='c' * 64))
        return value
    captured.reader.inspect_acquired_pe = inspect
    with pytest.raises(NativeExecutionUnsupported):
        prediction.capture_audio_predictions(captured.reader, captured.inventory)


def test_missing_source_stops_before_prediction_queries(captured):
    (captured.root / TARGETS[1]).unlink()
    with pytest.raises(NativeExecutionUnsupported):
        prediction.capture_audio_predictions(captured.reader, captured.inventory)
    assert captured.calls == []


@pytest.mark.parametrize('problem', ['role', 'missing'])
def test_inventory_stops_before_queries(captured, problem):
    rows = list(captured.inventory.files)
    index = next(index for index, row in enumerate(rows) if row.name == TARGETS[0])
    if problem == 'missing': rows.pop(index)
    else: rows[index] = rows[index].model_copy(update={'role': 'entrypoint'})
    invalid = captured.inventory.model_copy(update={'files': tuple(rows)})
    with pytest.raises(NativeExecutionUnsupported):
        prediction.capture_audio_predictions(captured.reader, invalid)
    assert captured.calls == []


def test_source_changed_after_capture_cannot_match(captured):
    predicted = prediction.capture_audio_predictions(captured.reader, captured.inventory)
    observed = observation.capture_audio_contexts(captured.reader, captured.inventory)
    (captured.root / TARGETS[0]).write_bytes(b'changed after observations')
    with pytest.raises(NativeExecutionUnsupported):
        prediction.match_audio_private_associations(observed, predicted)


@pytest.mark.parametrize('problem', ['policy', 'inventory', 'root', 'recipe', 'duplicate', 'missing',
    'unknown', 'missing_actual', 'changed_actual', 'unloaded_actual', 'duplicate_actual', 'duplicate_origin'])
def test_match_rejects_mismatched_or_missing_identity(captured, problem):
    predicted = prediction.capture_audio_predictions(captured.reader, captured.inventory)
    observed = observation.capture_audio_contexts(captured.reader, captured.inventory)
    if problem == 'policy': predicted = replace(predicted, policy_sha256='c' * 64)
    elif problem == 'inventory': predicted = replace(predicted, inventory_sha256='c' * 64)
    elif problem == 'root': predicted = replace(predicted, executable=captured.root / 'foreign.exe')
    elif problem == 'recipe': predicted = replace(predicted, recipe='unknown')
    elif problem == 'duplicate': predicted = replace(predicted, predictions=predicted.predictions * 2)
    elif problem == 'missing': predicted = replace(predicted, predictions=predicted.predictions[:1])
    elif problem == 'unknown': predicted = replace(predicted, predictions=(*predicted.predictions, ('unknown', predicted.predictions[0][1])))
    elif problem == 'missing_actual': observed = replace(observed, contexts=replace(observed.contexts, associated=()))
    elif problem == 'changed_actual':
        rows = tuple((path, replace(raw, flags=1)) for path, raw in observed.contexts.associated)
        observed = replace(observed, contexts=replace(observed.contexts, associated=rows))
    elif problem == 'unloaded_actual': observed = replace(observed, origins=(observed.executable,))
    elif problem == 'duplicate_actual': observed = replace(observed, contexts=replace(observed.contexts,
        associated=observed.contexts.associated * 2))
    else: observed = replace(observed, origins=observed.origins * 2)
    with pytest.raises(NativeExecutionUnsupported):
        prediction.match_audio_private_associations(observed, predicted)


def test_unloaded_sources_are_predictions_not_actual_associations(captured):
    predicted = prediction.capture_audio_predictions(captured.reader, captured.inventory)
    observed = observation.capture_audio_contexts(captured.reader, captured.inventory)
    private_paths = {str(captured.root / slot) for slot in TARGETS}
    observed = replace(observed, origins=tuple(path for path in observed.origins if str(path) not in private_paths),
        contexts=replace(observed.contexts, associated=tuple(row for row in observed.contexts.associated if row[0] not in private_paths)))
    assert prediction.match_audio_private_associations(observed, predicted) is None
