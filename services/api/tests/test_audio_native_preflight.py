import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.providers import audio_native_preflight as preflight
from app.providers.native_load import NativeDependencyPlan
from app.providers.pe_resources import ResourceClassification
from app.providers.private_native_recipe import AUDIO_SLOTS, _owned
from app.providers.soundfile_binding import SoundFileDerivation


def _setup(monkeypatch, *, failed_target=None, dynamic=True):
    acquired, acquired_id = preflight.parse_acquired_descriptor(preflight.owned_acquired_bytes())
    private, private_id = _owned()
    targets = {row['slot']: row for row in acquired['members'] if row['slot'] in AUDIO_SLOTS}
    files = tuple(SimpleNamespace(name=slot, role='dependency',
        size_bytes=targets[slot]['size_bytes'], sha256=targets[slot]['sha256']) for slot in AUDIO_SLOTS)
    inventory = SimpleNamespace(files=files, descriptor=SimpleNamespace(sha256='1' * 64))
    root = Path('C:/unused-runtime')
    command = SimpleNamespace(invocation=SimpleNamespace(cwd=root), inventory=inventory,
                              verify=lambda: None)
    descriptor = {
        'recipe': 'soundfile-owned-handle-v2', 'source_kind': 'derived',
        'upstream_sha256': 'a' * 64, 'transform_sha256': 'b' * 64,
        'derived_sha256': hashlib.sha256(b'generated').hexdigest(),
        'binder_sha256': 'c' * 64, 'handle_core_sha256': 'd' * 64,
        'private_policy_sha256': private_id, 'dependencies': [], 'terms': [],
        'dispatch_authorized': False, 'backend_handle_semantics': 'not_verified',
    }
    derivation = SoundFileDerivation(b'generated', json.dumps(descriptor, sort_keys=True,
                                                              separators=(',', ':')).encode())
    upstream = next(row for row in private['sources'] if row['slot'] == preflight.UPSTREAM_SLOT)

    def source_bytes(_root, _inventory, slot):
        if slot == preflight.UPSTREAM_SLOT:
            return SimpleNamespace(size_bytes=upstream['size_bytes'], sha256=upstream['sha256']), b'upstream'
        assert slot == preflight.GENERATED_SLOT
        generated = next(row for row in private['sources'] if row['slot'] == slot)
        return SimpleNamespace(size_bytes=generated['size_bytes'], sha256=generated['sha256']), b'ffi-source'

    def plan(_command, target):
        name = target.relative_to(root).as_posix()
        if name == failed_target:
            from app.providers.runtime_primitives import NativeExecutionUnsupported
            raise NativeExecutionUnsupported('execution_native_plan_dependency_unknown')
        row = targets[name]
        node = SimpleNamespace(name=name, size_bytes=row['size_bytes'], sha256=row['sha256'],
            resources=ResourceClassification(row['resources']['kind'], row['resources']['leaf_count'],
                                             row['resources']['manifest_sha256']))
        edges = tuple((name, dependency, dependency) for dependency in row['dependencies'])
        return NativeDependencyPlan(name, (node,), edges, (), False, dynamic)

    monkeypatch.setattr(preflight, '_source_bytes', source_bytes)
    monkeypatch.setattr(preflight, 'plan_prepared_audio_graph', plan)
    monkeypatch.setattr(preflight, 'derive_soundfile', lambda _payload: derivation)
    return command, derivation, acquired_id, private_id


def test_audio_preflight_keeps_two_targets_separate_and_blocks_dynamic_unknown(monkeypatch):
    command, derivation, acquired_id, private_id = _setup(monkeypatch)

    result = preflight.preflight_audio_native_targets(command, derivation)

    assert tuple(row.target for row in result.targets) == AUDIO_SLOTS
    assert all(row.plan is not None and row.blocker is None for row in result.targets)
    assert result.unresolved == tuple((slot, 'dynamic_closure_unverified') for slot in AUDIO_SLOTS)
    assert not result.dispatch_authorized
    assert result.acquired_descriptor_sha256 == acquired_id
    assert result.private_recipe_sha256 == private_id


def test_audio_preflight_preserves_unknown_target_and_derivation_identity(monkeypatch):
    command, derivation, *_ = _setup(monkeypatch, failed_target=AUDIO_SLOTS[0], dynamic=False)

    first = preflight.preflight_audio_native_targets(command, derivation)
    changed_descriptor = json.loads(derivation.descriptor)
    changed_descriptor['handle_core_sha256'] = 'e' * 64
    changed = SoundFileDerivation(derivation.payload,
        json.dumps(changed_descriptor, sort_keys=True, separators=(',', ':')).encode())
    monkeypatch.setattr(preflight, 'derive_soundfile', lambda _payload: changed)
    second = preflight.preflight_audio_native_targets(command, changed)

    assert first.targets[0].plan is None
    assert first.targets[0].blocker == 'execution_native_plan_dependency_unknown'
    assert first.targets[1].plan is not None
    assert first.identity_sha256 != second.identity_sha256
    assert not second.dispatch_authorized


def test_audio_preflight_rejects_missing_target_and_overlapping_recipe(monkeypatch):
    command, derivation, *_ = _setup(monkeypatch, dynamic=False)
    command.inventory.files = command.inventory.files[:1]

    from app.providers.runtime_primitives import NativeExecutionUnsupported
    with pytest.raises(NativeExecutionUnsupported, match='target_identity_changed'):
        preflight.preflight_audio_native_targets(command, derivation)

    command, derivation, *_ = _setup(monkeypatch, dynamic=False)
    monkeypatch.setattr(preflight, 'AUDIO_SLOTS', (AUDIO_SLOTS[0], AUDIO_SLOTS[0]))
    with pytest.raises(NativeExecutionUnsupported, match='recipe_mismatch'):
        preflight.preflight_audio_native_targets(command, derivation)


def test_audio_preflight_rejects_target_digest_and_edge_mismatches(monkeypatch):
    command, derivation, *_ = _setup(monkeypatch, dynamic=False)
    changed = list(command.inventory.files)
    changed[0] = SimpleNamespace(name=changed[0].name, role='dependency',
        size_bytes=changed[0].size_bytes, sha256='f' * 64)
    command.inventory.files = tuple(changed)
    from app.providers.runtime_primitives import NativeExecutionUnsupported
    with pytest.raises(NativeExecutionUnsupported, match='target_identity_changed'):
        preflight.preflight_audio_native_targets(command, derivation)

    command, derivation, *_ = _setup(monkeypatch, dynamic=False)
    native_plan = preflight.plan_prepared_audio_graph
    def remove_edges(cmd, target):
        return replace(native_plan(cmd, target), target='wrong-target')
    monkeypatch.setattr(preflight, 'plan_prepared_audio_graph', remove_edges)
    result = preflight.preflight_audio_native_targets(command, derivation)
    assert all(row.plan is None and row.blocker == 'execution_audio_target_graph_changed'
               for row in result.targets)


def test_real_source_transform_is_separate_from_generated_ffi(tmp_path, monkeypatch):
    import copy
    from app.providers import soundfile_binding as binding
    from app.providers.runtime_primitives import NativeExecutionUnsupported

    # Use the real bounded file reader and transform, not the old source mocks.
    command, _, *_ = _setup(monkeypatch)
    prefix = b'# notice\n' * 161
    block = (b'try:\n    _snd = _ffi.dlopen(_full_path)\n'
        b'except (OSError, ImportError, TypeError):\n' + b'    # branch\n' * 53 +
        b'    _snd = _ffi.dlopen(_explicit_libname)\n')
    upstream = prefix + block + b'\nvalue = 1\n'
    generated = b'from _cffi_backend import FFI\nffi = FFI()\n'
    monkeypatch.setattr(binding, 'UPSTREAM_SHA256', hashlib.sha256(upstream).hexdigest())
    monkeypatch.setattr(binding, 'BLOCK_SHA256', hashlib.sha256(block).hexdigest())
    policy, identity = _owned()
    policy = copy.deepcopy(policy)
    entries = list(command.inventory.files)
    for slot, payload in ((preflight.UPSTREAM_SLOT, upstream), (preflight.GENERATED_SLOT, generated)):
        path = tmp_path / slot
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        row = next(row for row in policy['sources'] if row['slot'] == slot)
        row.update(size_bytes=len(payload), sha256=digest)
        entries.append(SimpleNamespace(name=slot, role='dependency', size_bytes=len(payload), sha256=digest))
    command.invocation.cwd = tmp_path
    command.inventory.files = tuple(entries)
    monkeypatch.setattr(preflight, 'owned_private_recipe', lambda: (policy, identity))
    monkeypatch.setattr(preflight, 'derive_soundfile', binding.derive_soundfile)
    # Recover the real reader after _setup replaces it.
    monkeypatch.setattr(preflight, '_source_bytes', REAL_SOURCE_BYTES)
    # This test concerns source identity; target graph parsing is separately covered.
    monkeypatch.setattr(preflight, 'plan_prepared_audio_graph', lambda *_:
        (_ for _ in ()).throw(NativeExecutionUnsupported('execution_native_plan_dependency_unknown')))
    derivation = binding.derive_soundfile(upstream)
    assert derivation.payload != generated
    result = preflight.preflight_audio_native_targets(command, derivation)
    assert len(result.targets) == 2 and not result.dispatch_authorized
    (tmp_path / preflight.GENERATED_SLOT).write_bytes(derivation.payload)
    with pytest.raises(NativeExecutionUnsupported, match='source_identity_changed'):
        preflight.preflight_audio_native_targets(command, derivation)


REAL_SOURCE_BYTES = preflight._source_bytes


@pytest.mark.parametrize('size', [0, -1, True, preflight.SOURCE_LIMIT + 1])
def test_source_size_rejected_before_file_access(tmp_path, size):
    from app.providers.runtime_primitives import NativeExecutionUnsupported
    inventory = SimpleNamespace(files=(SimpleNamespace(name='missing.py', role='dependency', size_bytes=size),))
    with pytest.raises(NativeExecutionUnsupported, match='source_identity_invalid'):
        REAL_SOURCE_BYTES(tmp_path, inventory, 'missing.py')
