"""Fixed rejection checkpoints, synthetic data only; no new eligibility."""
from dataclasses import replace

import pytest

from app.providers.common_controls_binding import (ComponentComparisonUnsupported,
    COMPARISON_CHECKPOINTS, _compare_common_controls_v2, capture_component_metadata)
from app.providers.runtime_primitives import NativeExecutionUnsupported
from test_component_policy2 import policy2


@pytest.mark.parametrize('field,value,checkpoint', [
    ('format_version', 2, 'context_format'), ('flags', 1, 'context_flags'),
    ('root_manifest', 'C:/secret/not-the-source.exe', 'root_manifest'),
    ('root_configuration', 'secret-config', 'root_configuration'),
    ('application_directory', 'C:/secret/wrong', 'application_directory'),
    ('path_types', (2, 0, 2), 'path_types'), ('assemblies', (), 'roster'),
])
def test_first_metadata_rejection_has_fixed_detail_and_unchanged_public_error(policy2, field, value, checkpoint):
    metadata, kwargs, _ = policy2
    with pytest.raises(ComponentComparisonUnsupported) as error:
        _compare_common_controls_v2(replace(metadata, **{field: value}), **kwargs)
    assert str(error.value) == 'execution_common_controls_policy2_unsupported'
    assert isinstance(error.value, NativeExecutionUnsupported)
    assert error.value.diagnostic_checkpoint == checkpoint
    assert not any(text in str(error.value) for text in ('secret', 'C:', str(kwargs['source'])))
    assert error.value.__cause__ is None


def test_first_failure_order_is_not_last_observed_or_caller_selected(policy2):
    metadata, kwargs, _ = policy2
    bad = replace(metadata, flags=1, root_manifest='wrong', application_directory='wrong', path_types=(0, 0, 0))
    with pytest.raises(ComponentComparisonUnsupported) as error: _compare_common_controls_v2(bad, **kwargs)
    assert error.value.diagnostic_checkpoint == 'context_flags'


@pytest.mark.parametrize('change,checkpoint', [
    ('source_bytes', 'source_identity'), ('root_flags', 'root_assembly'),
    ('assembly_flags', 'assembly_metadata'), ('identity', 'encoded_identity'),
    ('missing_directory', 'component_directory'), ('manifest_path', 'manifest_path'),
    ('manifest_bytes', 'manifest_definition'), ('component_bytes', 'code_layout'),
    ('wrong_definition', 'definition_identity'), ('mui_code', 'resource_data'),
])
def test_component_and_file_errors_remain_named_and_locatable(policy2, change, checkpoint):
    metadata, kwargs, paths = policy2
    root, main, resource = metadata.assemblies
    if change == 'source_bytes': kwargs = dict(kwargs, expected_source_sha256='0'*64)
    if change == 'root_flags': root = replace(root, flags=True)
    if change == 'assembly_flags': main = replace(main, flags=1)
    if change == 'identity': main = replace(main, identity='not an assembly')
    if change == 'missing_directory': main = replace(main, directory='missing')
    if change == 'manifest_path': main = replace(main, manifest=str(paths[1][1]))
    if change == 'manifest_bytes': paths[0][1].write_bytes(b'invalid XML')
    if change == 'component_bytes': paths[0][0].write_bytes(b'invalid code')
    if change == 'wrong_definition':
        paths[0][1].write_bytes(paths[0][1].read_bytes().replace(b'6.0.26100.9278', b'6.0.26100.9279'))
    if change == 'mui_code': paths[1][0].write_bytes(b'invalid data')
    with pytest.raises(ComponentComparisonUnsupported) as error:
        _compare_common_controls_v2(replace(metadata, assemblies=(root, main, resource)), **kwargs)
    assert error.value.diagnostic_checkpoint == checkpoint
    assert str(error.value) == 'execution_common_controls_policy2_unsupported'


@pytest.mark.parametrize('value', ['C:/secret', 'caller_policy', '', None, 1])
def test_unknown_or_sensitive_checkpoint_cannot_enter_exception(value):
    with pytest.raises(ValueError, match='^component_checkpoint_invalid$'): ComponentComparisonUnsupported(value)


def test_pass_is_same_inert_binding_without_diagnostic_metadata(policy2):
    metadata, kwargs, _ = policy2
    result = _compare_common_controls_v2(metadata, **kwargs)
    assert not result.dispatch_authorized and not hasattr(result, 'diagnostic_checkpoint')


def test_actual_application_directory_trailing_separator_is_same_canonical_root(policy2):
    import os
    metadata, kwargs, _ = policy2
    plain = _compare_common_controls_v2(metadata, **kwargs)
    observed = replace(metadata, application_directory=str(kwargs['application_directory']) + os.sep)
    assert _compare_common_controls_v2(observed, **kwargs) == plain


@pytest.mark.parametrize('value', ['relative', 'parent', 'child', 'traversal', 'empty', 'type'])
def test_application_directory_fix_does_not_resolve_alternative_roots(policy2, value):
    metadata, kwargs, _ = policy2
    root = kwargs['application_directory']
    choices = dict(relative='.', parent=str(root.parent), child=str(root / 'child'),
        traversal=str(root / 'child' / '..'), empty='', type=None)
    with pytest.raises(ComponentComparisonUnsupported) as error:
        _compare_common_controls_v2(replace(metadata, application_directory=choices[value]), **kwargs)
    assert error.value.diagnostic_checkpoint == 'application_directory'


def test_raw_capture_is_detached_preserves_observed_values_without_comparison(policy2):
    metadata, kwargs, _ = policy2
    bad = replace(metadata, flags=17, application_directory='observed-not-approved')
    record = capture_component_metadata(bad)
    assert record['flags'] == 17 and record['application_directory'] == 'observed-not-approved'
    assert record['assemblies'][0]['flags'] == 17
    record['assemblies'][0]['flags'] = 0
    assert metadata.assemblies[0].flags == 17
    with pytest.raises(ComponentComparisonUnsupported): _compare_common_controls_v2(bad, **kwargs)


def test_raw_capture_is_bounded_and_named_without_truncation(policy2):
    metadata, kwargs, _ = policy2
    for value in (replace(metadata, root_configuration='secret'*200000), object()):
        with pytest.raises(NativeExecutionUnsupported, match='^execution_diagnostic_capture_limit$'):
            capture_component_metadata(value)
