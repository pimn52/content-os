"""New inert carrier only; old canonical observations must remain identical."""
from dataclasses import asdict
import copy

import pytest
from pydantic import ValidationError

from app.domain.execution_runtime import HostRuntimeObservation, HostRuntimeObservationV3
from app.providers.common_controls_binding import _compare_common_controls
from test_common_controls_binding import binding


def old(version=2):
    return dict(observation_recipe='windows-platform-ubr-selected-device', observation_version=version,
        trust_recipe='windows-os-selected-driver-v' + str(version), os_family='windows',
        architecture='amd64', build=26100, revision=9457, device={'kind': 'cpu'})


def new(binding):
    metadata, kwargs, _, _ = binding
    value = old()
    value.update(observation_recipe='windows-platform-ubr-component-bindings', observation_version=3,
        trust_recipe='windows-os-component-bindings-v3', component_policy_version=1,
        manifest_contract_sha256='bd76c737191489222cc219b605648fee50ae2721a7d1af7ebd756b429dee7ec2',
        common_controls=asdict(_compare_common_controls(metadata, **kwargs)))
    return value


@pytest.mark.parametrize('version', [1, 2])
def test_old_serialization_and_no_new_default_fields(version):
    value = old(version)
    assert HostRuntimeObservation.model_validate(value).canonical() == value
    with pytest.raises(ValidationError): HostRuntimeObservationV3.model_validate(value)


def test_v3_explicit_version_carrier_cannot_be_parsed_as_old_host(binding):
    value = new(binding)
    result = HostRuntimeObservationV3.model_validate(value)
    assert result.common_controls.code.windows_relative_path.startswith('WinSxS/')
    assert result.canonical()['observation_version'] == 3
    assert HostRuntimeObservationV3.model_validate_json(result.model_dump_json()) == result
    with pytest.raises(ValidationError): HostRuntimeObservation.model_validate(value)
    with pytest.raises(ValidationError): HostRuntimeObservation.model_validate(result)


@pytest.mark.parametrize('problem', ['recipe', 'version', 'trust', 'policy', 'manifest_contract',
    'cuda', 'foreign_code', 'component_version', 'language', 'manifest_path', 'extra_binding', 'tuple_bounds'])
def test_new_version_pair_and_exact_component_shape(binding, problem):
    value = copy.deepcopy(new(binding))
    fields = {'recipe': ('observation_recipe', 'windows-platform-ubr-selected-device'),
        'version': ('observation_version', 2), 'trust': ('trust_recipe', 'windows-os-selected-driver-v2'),
        'policy': ('component_policy_version', 2), 'manifest_contract': ('manifest_contract_sha256', '0' * 64),
        'cuda': ('device', {'kind': 'cuda'})}
    if problem in fields:
        key, item = fields[problem]; value[key] = item
    if problem == 'foreign_code': value['common_controls']['code']['windows_relative_path'] = 'System32/comctl32.dll'
    if problem == 'component_version': value['common_controls']['version'] = '6.0.0.0'
    if problem == 'language': value['common_controls']['language'] = 'en-US'
    if problem == 'manifest_path': value['common_controls']['manifest']['windows_relative_path'] = 'WinSxS/Manifests/foreign.manifest'
    if problem == 'extra_binding': value['common_controls']['another_dll'] = 'extra.dll'
    if problem == 'tuple_bounds': value['common_controls']['path_types'] = (-1, 0, 1, 1, 0)
    with pytest.raises(ValidationError): HostRuntimeObservationV3.model_validate(value)


def test_boolean_policy_version_cannot_equal_integer_one(binding):
    value = new(binding)
    value['component_policy_version'] = True
    with pytest.raises(ValidationError): HostRuntimeObservationV3.model_validate(value)
