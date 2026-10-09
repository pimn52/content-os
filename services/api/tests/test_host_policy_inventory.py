"""Declared policy identity only; no staged execution or new receipt rights."""
import hashlib
import json
from types import SimpleNamespace

import pytest

from app.domain.execution_runtime import HostRuntimeObservation, RuntimeInventory, OS_POLICY_SLOT
from app.providers.windows_component_policy import POLICY_SLOT, owned_component_policy_bytes
from app.providers.windows_os_policy import owned_os_policy_bytes
from app.runtime_inventory import RuntimeInventoryError, require_host_policy_inventory
from test_component_policy2 import policy2
from test_execution_specification_v3 import raw_spec, parse
from test_runtime_inventory import inventory


@pytest.fixture
def declared(raw_spec):
    value = inventory(1).model_dump(mode='json')
    value['directories'].extend(['Lib/site-packages', 'Lib/site-packages/app',
        'Lib/site-packages/app/providers'])
    for name, payload in ((OS_POLICY_SLOT, owned_os_policy_bytes()),
                          (POLICY_SLOT, owned_component_policy_bytes())):
        value['files'].append(dict(role='dependency', name=name, size_bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest()))
    return parse(raw_spec).host_runtime, value


def validated(raw): return RuntimeInventory.model_validate_json(json.dumps(raw))


def test_exact_declaration_pairing_is_not_dispatch_or_receipt_permission(declared):
    host, raw = declared
    entry = require_host_policy_inventory(host, validated(raw))
    assert entry.name == OS_POLICY_SLOT
    assert host.binding.component_policy_sha256 == host.component_policy_sha256
    # This returns an inventory declaration, not a native/prepared authority.
    assert not hasattr(entry, 'execute') and not hasattr(entry, 'dispatch_authorized')


@pytest.mark.parametrize('slot', [OS_POLICY_SLOT, POLICY_SLOT])
@pytest.mark.parametrize('problem', ['missing', 'case', 'role', 'size', 'digest'])
def test_missing_or_changed_policy_declaration_refuses(declared, slot, problem):
    host, raw = declared
    entry = next(item for item in raw['files'] if item['name'] == slot)
    if problem == 'missing': raw['files'].remove(entry)
    elif problem == 'case': entry['name'] = entry['name'].replace('windows_', 'Windows_')
    elif problem == 'role': entry['role'] = 'entrypoint'
    elif problem == 'size': entry['size_bytes'] += 1
    else: entry['sha256'] = 'e'*64
    with pytest.raises(RuntimeInventoryError):
        require_host_policy_inventory(host, validated(raw))


def test_host_and_binding_digest_must_both_match_owned_component_bytes(declared):
    host, raw = declared
    changed = host.model_dump(mode='json')
    changed['component_policy_sha256'] = 'e'*64
    changed['binding']['component_policy_sha256'] = 'e'*64
    changed = type(host).model_validate_json(json.dumps(changed))
    with pytest.raises(RuntimeInventoryError, match='execution_policy_inventory_identity_changed'):
        require_host_policy_inventory(changed, validated(raw))


def test_forged_copy_and_unknown_recipe_do_not_fall_through(declared):
    host, raw = declared
    with pytest.raises(RuntimeInventoryError, match='execution_host_recipe_invalid'):
        require_host_policy_inventory(host.model_copy(update={'component_policy_version':1}), validated(raw))
    for name in ('windows-os-component-bindings-v3', 'unknown', 'windows-os-selected-driver-v1'):
        with pytest.raises(RuntimeInventoryError, match='execution_host_recipe_unsupported'):
            require_host_policy_inventory(SimpleNamespace(trust_recipe=name), validated(raw))


def test_implementation_resource_failure_is_a_fixed_diagnostic(declared, monkeypatch):
    from app.providers.runtime_primitives import NativeExecutionUnsupported
    import app.providers.windows_component_policy as policy
    def unavailable(): raise NativeExecutionUnsupported('private implementation details')
    monkeypatch.setattr(policy, 'owned_component_policy_bytes', unavailable)
    with pytest.raises(RuntimeInventoryError, match='^execution_owned_policy_unavailable$'):
        require_host_policy_inventory(declared[0], validated(declared[1]))


def test_old_valid_host_recipes_keep_original_declaration_rules():
    host = HostRuntimeObservation.model_validate_json(json.dumps(dict(
        observation_recipe='synthetic-windows-host', observation_version=1,
        trust_recipe='windows-os-selected-driver-v1', os_family='windows', architecture='amd64',
        build=26100, revision=1, device={'kind':'cpu'})))
    assert require_host_policy_inventory(host, inventory(1)) is None
    host2 = host.model_copy(update=dict(observation_recipe='windows-platform-ubr-selected-device',
        observation_version=2, trust_recipe='windows-os-selected-driver-v2'))
    with pytest.raises(RuntimeInventoryError, match='execution_os_policy_inventory_required'):
        require_host_policy_inventory(host2, inventory(1))
