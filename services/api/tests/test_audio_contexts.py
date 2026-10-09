"""Synthetic metadata only; no native loads or Windows context queries."""
from dataclasses import replace
from pathlib import Path

import pytest

from app.providers.audio_contexts import compare_audio_context_transition as compare
from app.providers.audio_native_graph import TARGETS
from app.providers.cpu_native_recipe import PrivateRootPrediction, _owned
from app.providers.native_load import NativeNode, NativeDependencyPlan
from app.providers.pe_resources import ResourceClassification
from app.providers.windows_activation import ActivationMetadata, ActivationAssembly
from app.providers.runtime_primitives import NativeExecutionUnsupported


@pytest.fixture
def transition(tmp_path):
    name = TARGETS[0]
    source = tmp_path / name
    source.parent.mkdir(parents=True)
    source.write_bytes(b'synthetic context source, never executable')
    root = ActivationAssembly('', str(source), '', '', 2, 1, (1, 0), (0, 0), 0, (), 17)
    metadata = ActivationMetadata(1, str(source), '', str(source.parent), (2, 1, 2), (root,), 0)
    manifest = next(iter(_owned()[0]['manifests']))
    node = NativeNode(name, 100, 'a' * 64, ResourceClassification('requires_private_root_context', 1, manifest))
    plan = NativeDependencyPlan(name, (node,), (), (), False, True)
    prediction = PrivateRootPrediction(node.sha256, node.resources.manifest_sha256,
        str(source), str(source.parent), 17, metadata)
    prior = ('retained-effective', (('python312.dll', 'retained-association'),))
    after = (prior[0], (*prior[1], (name, metadata)))
    return tmp_path, name, source, plan, prediction, prior, after


def test_loaded_private_root_requires_prediction_and_exact_actual(transition):
    root, name, source, plan, prediction, prior, after = transition
    assert compare(prior, after, plan, (source,), root, ((name, prediction),)) is None
    assert plan.requires_dynamic_closure and plan.dispatch_authorized is False
    compare(after, after, plan, (source,), root, ((name, prediction),))


@pytest.mark.parametrize('problem', ['missing_actual', 'missing_prediction', 'source', 'parent', 'hash', 'manifest',
    'metadata', 'effective', 'old', 'duplicate', 'unknown', 'unloaded', 'role', 'origin_duplicate', 'prediction_extra',
    'target', 'node_duplicate', 'parent_metadata'])
def test_transition_rejects_missing_changed_and_foreign_evidence(transition, problem):
    root, name, source, plan, prediction, prior, after = transition
    origins = (source,)
    predictions = ((name, prediction),)
    if problem == 'missing_actual': after = prior
    if problem == 'missing_prediction': predictions = ()
    changes = {'source': {'source': str(root / 'foreign.pyd')}, 'parent': {'source_parent': str(root)},
        'hash': {'source_sha256': 'c' * 64}, 'manifest': {'manifest_sha256': 'c' * 64}}
    if problem in changes: predictions = ((name, replace(prediction, **changes[problem])),)
    if problem == 'metadata': after = (prior[0], (*prior[1], (name, replace(prediction.metadata, flags=1))))
    if problem == 'effective': after = ('changed', after[1])
    if problem == 'old': after = (prior[0], ((name, prediction.metadata),))
    if problem == 'duplicate': after = (prior[0], (*after[1], (name, prediction.metadata)))
    if problem == 'unknown': after = (prior[0], (*after[1], ('unknown.dll', prediction.metadata)))
    if problem == 'unloaded': origins = ()
    if problem == 'role': predictions = ((name, prediction.metadata),)
    if problem == 'origin_duplicate': origins = (source, source)
    if problem == 'prediction_extra': predictions = (*predictions, ('unknown.dll', prediction))
    if problem == 'target': plan = replace(plan, target='foreign.pyd')
    if problem == 'node_duplicate': plan = replace(plan, nodes=plan.nodes * 2)
    if problem == 'parent_metadata':
        prediction = replace(prediction, metadata=replace(prediction.metadata, application_directory=str(root)))
        predictions = ((name, prediction),)
        after = (prior[0], (*prior[1], (name, prediction.metadata)))
    with pytest.raises(NativeExecutionUnsupported): compare(prior, after, plan, origins, root, predictions)


def test_unloaded_private_node_does_not_invent_association(transition):
    root, _, _, plan, _, prior, _ = transition
    compare(prior, prior, plan, (), root, ())


def test_old_unknown_association_is_preserved_not_newly_admitted(transition):
    root, _, _, plan, _, prior, _ = transition
    compare(prior, prior, plan, (), root, ())
    with pytest.raises(NativeExecutionUnsupported):
        compare(prior, (prior[0], (*prior[1], ('foreign.dll', object()))), plan, (), root, ())


@pytest.mark.parametrize('change', ['flags', 'roster', 'configuration', 'root_flags', 'path_types', 'root_role'])
def test_prediction_cannot_hide_invalid_raw_private_metadata(transition, change):
    root, name, source, plan, prediction, prior, _ = transition
    raw = prediction.metadata
    assembly = raw.assemblies[0]
    changed = {'flags': replace(raw, flags=1), 'roster': replace(raw, assemblies=(assembly, assembly)),
        'configuration': replace(raw, root_configuration='external'),
        'root_flags': replace(raw, assemblies=(replace(assembly, flags=True),)),
        'path_types': replace(raw, path_types=(1, 1, 1)),
        'root_role': replace(raw, assemblies=(replace(assembly, manifest_type=1),))}[change]
    prediction = replace(prediction, metadata=changed)
    after = (prior[0], (*prior[1], (name, changed)))
    with pytest.raises(NativeExecutionUnsupported):
        compare(prior, after, plan, (source,), root, ((name, prediction),))


@pytest.mark.parametrize('problem', [None, 'missing', 'source_role', 'query_role', 'source_hash', 'resolution'])
def test_commoncontrols_role_and_effective_resolution_preserved(tmp_path, problem):
    from app.providers.common_controls_binding import CommonControlsBindingV2
    effective = CommonControlsBindingV2(2, 'a' * 64, 'b' * 64, 'd' * 64,
        'bootstrap_exe', 'effective', 17, 0, (2, 1, 2), None, None)
    associated = replace(effective, source_sha256='c' * 64, source_role='dll', query_role='associated')
    node = NativeNode('python312.dll', 100, 'c' * 64, ResourceClassification('requires_os_sxs_binding', 2, 'e' * 64))
    target = NativeNode(TARGETS[0], 100, 'f' * 64, ResourceClassification('no_resources', 0))
    plan = NativeDependencyPlan(TARGETS[0], (node, target), (), (), True, True)
    changes = {'source_role': {'source_role': 'bootstrap_exe'}, 'query_role': {'query_role': 'created'},
        'source_hash': {'source_sha256': 'e' * 64}, 'resolution': {'component_policy_sha256': 'e' * 64}}
    if problem in changes: associated = replace(associated, **changes[problem])
    before = (effective, ())
    after = before if problem == 'missing' else (effective, (('python312.dll', associated),))
    arguments = (before, after, plan, (tmp_path / 'python312.dll',), tmp_path, ())
    if problem is None: compare(*arguments)
    else:
        with pytest.raises(NativeExecutionUnsupported): compare(*arguments)
