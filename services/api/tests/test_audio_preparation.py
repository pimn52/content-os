"""Real synthetic copies/fake host only; no OS API, subprocess or native bytes."""
from dataclasses import replace
import hashlib
from types import SimpleNamespace

import pytest

from app.providers import audio_preparation as preparation
from app.providers import audio_graph_probe as probe
from app.providers.runtime_tree import _with_owned, OwnedRuntimeFile
from app.providers.runtime_primitives import ExecutionPreparationError
from app.providers.soundfile_binding import SoundFileDerivation


@pytest.fixture
def sources(tmp_path, monkeypatch):
    base, audio, staging, work = [tmp_path / name for name in ('base', 'audio', 'staging', 'work')]
    for path in (base, audio, staging, work, base / 'Lib', base / 'DLLs'):
        path.mkdir()
    for name in ('python.exe', 'python312.dll', 'python3.dll', 'vcruntime140.dll', 'vcruntime140_1.dll'):
        (base / name).write_bytes(b'synthetic base')
    (base / 'Lib' / 'os.py').write_bytes(b'# synthetic stdlib')
    prefix = 'Lib/site-packages/'
    slots = (*preparation.AUDIO_SLOTS, preparation.UPSTREAM_SLOT,
             prefix + '_soundfile.py', prefix + '_soundfile_data/__init__.py',
             prefix + '_soundfile_data/COPYING', 'LICENSE.txt')
    rows = []
    for slot in slots:
        path = audio / slot[len(prefix):] if slot.startswith(prefix) else base / slot
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = ('synthetic:' + slot).encode()
        path.write_bytes(raw)
        rows.append(dict(slot=slot, size_bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest()))
    monkeypatch.setattr(preparation, '_audio_rows', lambda: tuple(rows))
    monkeypatch.setattr(preparation, 'derive_soundfile', lambda raw: SoundFileDerivation(b'derived:' + raw, b'{}'))
    def host(**kwargs):
        return SimpleNamespace(_runtime_root=kwargs['runtime_root'],
            _inventory_json=kwargs['inventory'].model_dump_json(), windows_root=tmp_path,
            observation=SimpleNamespace(canonical=lambda: {'synthetic_host': True}), verify=lambda: None)
    monkeypatch.setattr(preparation, 'prepare_host_runtime_v2', host)
    # Any launch is a test failure, including accidental diagnostic launch.
    monkeypatch.setattr(probe.subprocess, 'run', lambda *a, **k: pytest.fail('launch prohibited'))
    return dict(base=base, audio_root=audio, staging_parent=staging, work_directory=work)


def test_factory_real_copy_bundle_derivation_and_owned_cleanup(sources):
    with preparation.prepare_audio_graph_probe(**sources) as prepared:
        prepared.verify()
        root = prepared._tree.root
        assert root != sources['base']
        assert len(prepared.preparation_identity_sha256) == 64
        assert (root / preparation.UPSTREAM_SLOT).read_bytes() != prepared.derivation.payload
        assert (root / 'Lib/site-packages/_soundfile.py').read_bytes().startswith(b'synthetic:')
        assert len(probe.owned_audio_graph_probe_files()) > 8
        assert set(row.destination for row in probe.owned_audio_graph_probe_files()) <= {
            row.name for row in prepared.inventory.files}
    assert not root.exists()
    assert sources['audio_root'].exists()
    assert not list(sources['staging_parent'].iterdir())
    with pytest.raises(ExecutionPreparationError, match='consumed'):
        prepared.verify()


@pytest.mark.parametrize('change', ['source', 'missing', 'shadow', 'empty_directory',
    'private_bytes', 'private_shadow', 'host', 'host_tree', 'host_inventory', 'host_observation', 'argv', 'env', 'derivation'])
def test_changed_preparation_rejected(sources, change):
    with preparation.prepare_audio_graph_probe(**sources) as prepared:
        if change == 'source': (sources['audio_root'] / 'soundfile.py').write_bytes(b'changed')
        elif change == 'missing': (sources['audio_root'] / '_soundfile.py').unlink()
        elif change == 'shadow': (sources['audio_root'] / 'shadow.py').write_bytes(b'x')
        elif change == 'empty_directory': (sources['audio_root'] / 'extra').mkdir()
        elif change == 'private_bytes': (prepared._tree.root / preparation.UPSTREAM_SLOT).write_bytes(b'changed')
        elif change == 'private_shadow': (prepared._tree.root / 'shadow.py').write_bytes(b'x')
        elif change == 'host': prepared._host = SimpleNamespace(verify=lambda: None)
        elif change == 'host_tree': prepared._host._runtime_root = sources['base']
        elif change == 'host_inventory': prepared._host._inventory_json = '{}'
        elif change == 'host_observation': prepared._host.observation = SimpleNamespace(canonical=lambda: {})
        elif change == 'argv': prepared.invocation = replace(prepared.invocation, argv=('foreign',))
        elif change == 'env': prepared.invocation = replace(prepared.invocation, environment=(('OTHER', '1'),))
        else: prepared.derivation = SoundFileDerivation(b'foreign', b'{}')
        with pytest.raises((ExecutionPreparationError, OSError)):
            prepared.verify()
        with pytest.raises(ExecutionPreparationError, match='consumed'):
            prepared.verify()


@pytest.mark.parametrize('change', ['payload', 'role', 'duplicate', 'missing', 'extra'])
def test_audio_owned_recipe_is_exact_not_expanded_general_limit(change):
    owned = list(probe.owned_audio_graph_probe_files())
    if change == 'payload': owned[0] = replace(owned[0], payload=b'arbitrary')
    elif change == 'role': owned[0] = replace(owned[0], role='entrypoint')
    elif change == 'duplicate': owned.append(owned[0])
    elif change == 'missing': owned.pop()
    else: owned.append(OwnedRuntimeFile('arbitrary.py', b'x'))
    with pytest.raises(ExecutionPreparationError, match='bundle_changed'):
        _with_owned(set(), [], tuple(owned), preparation.TREE_LIMIT, 'audio-graph-probe-v6')


def test_legacy_and_unknown_recipe_reject_audio_bundle():
    for recipe in ('legacy', 'unknown-audio-v6'):
        with pytest.raises(ExecutionPreparationError, match='owned_limit'):
            _with_owned(set(), [], probe.owned_audio_graph_probe_files(), preparation.TREE_LIMIT, recipe)


@pytest.mark.parametrize('failure', ['host', 'transform', 'foreign_host'])
def test_factory_failure_cleans_only_owned_tree(sources, monkeypatch, failure):
    def fail(*a, **k): raise ExecutionPreparationError('synthetic failure')
    if failure == 'host': monkeypatch.setattr(preparation, 'prepare_host_runtime_v2', fail)
    elif failure == 'transform': monkeypatch.setattr(preparation, 'derive_soundfile', fail)
    else:
        original = preparation.prepare_host_runtime_v2
        def foreign(**kwargs):
            result = original(**kwargs)
            result._runtime_root = sources['base']
            return result
        monkeypatch.setattr(preparation, 'prepare_host_runtime_v2', foreign)
    with pytest.raises(ExecutionPreparationError):
        preparation.prepare_audio_graph_probe(**sources)
    assert not list(sources['staging_parent'].iterdir())
    assert (sources['audio_root'] / 'soundfile.py').exists()


def test_source_changes_between_selection_and_copy_rejected(sources, monkeypatch):
    original = preparation.prepare_runtime_tree
    def changed(**kwargs):
        (sources['audio_root'] / '_soundfile.py').write_bytes(b'changed before copy')
        return original(**kwargs)
    monkeypatch.setattr(preparation, 'prepare_runtime_tree', changed)
    with pytest.raises(ExecutionPreparationError, match='source_changed'):
        preparation.prepare_audio_graph_probe(**sources)
    assert not list(sources['staging_parent'].iterdir())


@pytest.mark.parametrize('value', [0, True, -1, 16, float('nan')])
def test_timeout_bounds_before_staging(sources, value):
    with pytest.raises(ExecutionPreparationError, match='bounds_invalid'):
        preparation.prepare_audio_graph_probe(**sources, timeout_seconds=value)
    assert not list(sources['staging_parent'].iterdir())
