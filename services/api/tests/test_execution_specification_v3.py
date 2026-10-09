"""Explicit host3/policy2 representation; no model, native or admission calls."""
import json
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from app.domain.models import (ExecutionSpecification, ExecutionSpecificationV2,
    ExecutionSpecificationV3, ExecutionSpecificationRecord, ProviderUseLicenseReviewV2)
from app.execution_scope import ExecutionUseBindingV2, WorkerExecutionUseSnapshotV2
from app.providers.common_controls_binding import _compare_common_controls_v2
from test_component_policy2 import policy2, host
from test_runtime_inventory import inventory


@pytest.fixture
def raw_spec(policy2):
    metadata, kwargs, _ = policy2
    return dict(schema_version=3, capability='voice', mode='local', provider='synthetic',
        model='synthetic-model', runtime='synthetic-runtime', machine_id='synthetic-machine',
        observation_recipe='synthetic-only', observation_version=1,
        model_artifacts=[dict(role='weights', name='model/weights.bin', size_bytes=1, sha256='a'*64),
            dict(role='tokenizer', name='model/tokenizer.json', size_bytes=1, sha256='b'*64)],
        runtime_inventory=inventory(1).descriptor.model_dump(mode='json'),
        host_runtime=host(_compare_common_controls_v2(metadata, **kwargs)),
        machine=dict(installation_id=str(uuid4()), host_sha256='c'*64, device_sha256='d'*64),
        parameters=dict(device='cpu', precision='float16', seed=0))


def parse(raw):
    return ExecutionSpecificationV3.model_validate_json(json.dumps(raw))


def test_roundtrip_order_and_old_parsers_reject_even_typed_instances(raw_spec):
    value = parse(raw_spec)
    assert parse(value.model_dump(mode='json')) == value
    raw_spec['model_artifacts'].reverse()
    assert parse(raw_spec).execution_sha256 == value.execution_sha256
    assert parse(raw_spec).model_artifact_sha256 == value.model_artifact_sha256
    for parser in (ExecutionSpecification, ExecutionSpecificationV2):
        for raw in (value, value.model_dump(mode='json')):
            with pytest.raises(ValidationError):
                parser.model_validate(raw)
    assert TypeAdapter(ExecutionSpecificationRecord).validate_python(value) == value
    assert TypeAdapter(ExecutionSpecificationRecord).validate_json(value.model_dump_json()) == value


@pytest.mark.parametrize('change', ['revision', 'binding', 'inventory', 'parameters', 'weights'])
def test_execution_digest_covers_each_independent_evidence_dimension(raw_spec, change):
    original = parse(raw_spec)
    if change == 'revision': raw_spec['host_runtime']['revision'] += 1
    elif change == 'binding': raw_spec['host_runtime']['binding']['root_flags'] += 1
    elif change == 'inventory': raw_spec['runtime_inventory']['sha256'] = 'e'*64
    elif change == 'parameters': raw_spec['parameters']['seed'] += 1
    else: raw_spec['model_artifacts'][0]['sha256'] = 'e'*64
    changed = parse(raw_spec)
    assert changed.execution_sha256 != original.execution_sha256
    assert (changed.model_artifact_sha256 != original.model_artifact_sha256) == (change == 'weights')


@pytest.mark.parametrize('problem', ['schema2', 'schema_bool', 'schema_float', 'host2',
    'policy1', 'policy_bool', 'policy_digest', 'device', 'missing_weights', 'extra'])
def test_exact_pairing_cannot_relabel_old_or_incomplete_evidence(raw_spec, problem):
    if problem.startswith('schema'):
        raw_spec['schema_version'] = {'schema2':2, 'schema_bool':True, 'schema_float':3.0}[problem]
    elif problem == 'host2': raw_spec['host_runtime']['observation_version'] = 2
    elif problem == 'policy1': raw_spec['host_runtime']['component_policy_version'] = 1
    elif problem == 'policy_bool': raw_spec['host_runtime']['component_policy_version'] = True
    elif problem == 'policy_digest': raw_spec['host_runtime']['component_policy_sha256'] = 'e'*64
    elif problem == 'device': raw_spec['parameters']['device'] = 'cuda:0'
    elif problem == 'missing_weights': raw_spec['model_artifacts'] = raw_spec['model_artifacts'][1:]
    else: raw_spec['dispatch_authorized'] = True
    with pytest.raises(ValidationError): parse(raw_spec)


@pytest.mark.parametrize('carrier', [ExecutionUseBindingV2, WorkerExecutionUseSnapshotV2,
    ProviderUseLicenseReviewV2])
def test_authority_carriers_refuse_unknown_nested_version(carrier, raw_spec):
    spec = parse(raw_spec)
    # Other required fields may be absent: specifically assert rejection at
    # the specification discriminator, not an unrelated missing-field error.
    unknown = spec.model_dump(mode='json')
    unknown['schema_version'] = 4
    for value in (spec.model_copy(update={'schema_version':4}), unknown):
        with pytest.raises(ValidationError) as caught:
            carrier.model_validate(dict(execution_specification=value,
                execution_sha256=spec.execution_sha256, identity=spec.identity))
        assert any(error['loc'] == ('execution_specification',)
            and error['type'] == 'union_tag_invalid' for error in caught.value.errors())
