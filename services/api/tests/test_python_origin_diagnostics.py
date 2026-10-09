"""First failure evidence is bounded; origin eligibility is unchanged."""
from types import ModuleType, SimpleNamespace
import json

import pytest

from app.providers.windows_native import (python_origins, PythonOriginUnsupported,
    PYTHON_ORIGIN_CHECKPOINTS)
from app.providers.runtime_primitives import NativeExecutionUnsupported
from app.providers import component_child, component_probe
from test_component_probe import setup, contexts, run_child
from test_private_context_reader import private_contexts
from test_component_policy2 import policy2


@pytest.mark.parametrize('checkpoint,name,module', [
    ('module', 'missing', None), ('module', 'wrong', object()),
    ('missing_file', 'namespace', ModuleType('namespace')),
])
def test_first_rejection_is_safe_and_keeps_generic_error(checkpoint, name, module):
    with pytest.raises(PythonOriginUnsupported) as failure:
        python_origins({name: module, 'later': None})
    assert isinstance(failure.value, NativeExecutionUnsupported)
    assert str(failure.value) == 'execution_python_origin_unsupported'
    assert failure.value.diagnostic == dict(checkpoint=checkpoint, module_name=name)


@pytest.mark.parametrize('name', ['secret/key', 'token\nvalue', 'é', 'a'*256, 42, ''])
def test_untrusted_registry_names_are_not_disclosed(name):
    with pytest.raises(PythonOriginUnsupported) as failure:
        python_origins({name: None})
    assert failure.value.diagnostic['module_name'] == ''


@pytest.mark.parametrize('file,checkpoint', [(None, 'missing_file'),
    ('relative.py', 'exact_path'), ('', 'file_value'), (7, 'file_value')])
def test_rejected_file_values_are_not_disclosed(file, checkpoint):
    module = ModuleType('example')
    module.__file__ = file
    with pytest.raises(PythonOriginUnsupported) as failure:
        python_origins({'example.sub': module})
    assert failure.value.diagnostic == dict(checkpoint=checkpoint, module_name='example.sub')


def test_registry_bounds_and_fileless_frozen_still_require_real_source():
    for modules, checkpoint in [({}, 'count'), ({str(i): None for i in range(2049)}, 'count')]:
        with pytest.raises(PythonOriginUnsupported) as failure: python_origins(modules)
        assert failure.value.diagnostic == dict(checkpoint=checkpoint, module_name='')
    frozen = ModuleType('frozen')
    frozen.__spec__ = SimpleNamespace(origin='frozen')
    with pytest.raises(PythonOriginUnsupported) as failure: python_origins({'frozen': frozen})
    assert failure.value.diagnostic == dict(checkpoint='empty', module_name='')
    assert PYTHON_ORIGIN_CHECKPOINTS == frozenset(component_probe.PythonOriginFailure.model_fields['checkpoint'].annotation.__args__)


def test_real_expat_pseudo_module_and_same_name_impostor_both_reject():
    # Local test-interpreter evidence, not a private-child eligibility claim.
    import pyexpat
    for name in ('errors', 'model'):
        module = getattr(pyexpat, name)
        assert isinstance(module, ModuleType)
        assert getattr(module, '__file__', None) is None
        assert getattr(module, '__spec__', None) is None
        for child in (module, ModuleType('pyexpat.' + name)):
            with pytest.raises(PythonOriginUnsupported) as failure:
                python_origins({'pyexpat': pyexpat, 'pyexpat.' + name: child})
            assert failure.value.diagnostic == dict(checkpoint='missing_file', module_name='pyexpat.' + name)


def test_protocol3_safe_failure_and_historical_payload_compatibility(setup, monkeypatch):
    def fail(_): raise PythonOriginUnsupported('missing_file', 'example')
    monkeypatch.setattr(component_child, 'python_origins', fail)
    report = run_child(setup)
    typed = component_probe.ComponentProbeReport.model_validate_json(json.dumps(report))
    assert typed.python_origin_failure.module_name == 'example'
    assert not typed.dispatch_authorized and 'activation' not in setup[-1]
    parent = SimpleNamespace(inventory=SimpleNamespace(descriptor=SimpleNamespace(sha256='c'*64)))
    with pytest.raises(ValueError, match='component_protocol_invalid'):
        component_probe.PreparedComponentProbe._validate_extra(parent, typed)
    historical = dict(report)
    historical.pop('python_origin_failure')
    assert component_probe.ComponentProbeReport.model_validate_json(json.dumps(historical)).python_origin_failure is None
    for changes in [dict(passed=True), dict(stage='contexts'), dict(code='execution_other'),
        dict(python_origin_failure=dict(checkpoint='unknown', module_name='example')),
        dict(python_origin_failure=dict(checkpoint='module', module_name='secret/key'))]:
        with pytest.raises(ValueError): component_probe.ComponentProbeReport.model_validate_json(json.dumps(dict(report, **changes)))
