"""Exact-source preparation for protocol6 diagnostics, never load authority."""
from pathlib import Path
import hashlib

from .cpython_base import select_cpython312_base
from .host_runtime import prepare_host_runtime_v2
from .prepared import PendingInvocation
from .private_native_recipe import AUDIO_SLOTS, _owned
from .runtime_primitives import ExecutionPreparationError, _exact_path
from .runtime_tree import (RuntimeTreeSelection, RuntimeFileSelection,
    prepare_runtime_tree, _members, _digest)
from .soundfile_binding import derive_soundfile, UPSTREAM_SLOT
from .audio_graph_probe import (PreparedAudioGraphProbe, owned_audio_graph_probe_files,
    ENTRY, TREE_LIMIT, canonical)


def _audio_rows():
    policy, _ = _owned()
    return tuple(row for category in ('members', 'sources', 'terms')
                 for row in policy[category]
                 if category != 'members' or row['slot'] in AUDIO_SLOTS)


def _check_sources(base, audio_root):
    """Complete dedicated component directory; no installed-package overlays."""
    _exact_path(audio_root, directory=True)
    selected, directories = {}, set()
    prefix = 'Lib/site-packages/'
    _members(audio_root, prefix[:-1], selected, directories)
    rows = _audio_rows()
    expected = {row['slot']: row for row in rows if row['slot'].startswith(prefix)}
    parents = {prefix[:-1]}
    for name in expected:
        parts = name.split('/')
        parents.update('/'.join(parts[:i]) for i in range(3, len(parts)))
    if set(selected) != set(expected) or directories != parents:
        raise ExecutionPreparationError('execution_audio_source_members_changed')
    for row in rows:
        path = selected.get(row['slot'], base / row['slot'])
        if _digest(path, TREE_LIMIT) != (row['size_bytes'], row['sha256']):
            raise ExecutionPreparationError('execution_audio_source_changed')
    return rows


def prepare_audio_graph_probe(*, base: Path, audio_root: Path,
        staging_parent: Path, work_directory: Path, max_bytes: int = TREE_LIMIT,
        timeout_seconds: float = 15):
    """Copy only fixed component sources and a complete independent base.

    Source selection comes from the owned recipe, not an evidence inventory.
    This factory does not launch a child, import SoundFile or load a DLL.
    Actual host observation is separate from the static source correspondence.
    """
    tree = None
    try:
        if (type(max_bytes) is not int or not 0 < max_bytes <= TREE_LIMIT or
                type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 15):
            raise ExecutionPreparationError('execution_audio_bounds_invalid')
        _exact_path(work_directory, directory=True)
        rows = _check_sources(base, audio_root)
        trees, files = select_cpython312_base(base)
        files += tuple(RuntimeFileSelection(base / row['slot'], row['slot'], 'dependency')
                       for row in rows if not row['slot'].startswith('Lib/site-packages/'))
        trees += (RuntimeTreeSelection(audio_root, 'Lib/site-packages'),)
        tree = prepare_runtime_tree(trees=trees, files=files,
            staging_parent=staging_parent, max_bytes=max_bytes, max_files=10000,
            owned_files=owned_audio_graph_probe_files(), owned_recipe='audio-graph-probe-v6')
        _check_sources(base, audio_root)
        entries = {row.name: row for row in tree.inventory.files}
        for row in rows:
            actual = entries.get(row['slot'])
            if (actual is None or actual.role != 'dependency' or
                    (actual.size_bytes, actual.sha256) != (row['size_bytes'], row['sha256'])):
                raise ExecutionPreparationError('execution_audio_source_changed')
        if len(tree.inventory.directories) > 3000 or work_directory.is_relative_to(tree.root):
            raise ExecutionPreparationError('execution_audio_bounds_invalid')
        path = tree.root / UPSTREAM_SLOT
        _exact_path(path, directory=False)
        with path.open('rb') as source:
            upstream = source.read(4 * 1024**2 + 1)
        derivation = derive_soundfile(upstream)
        host = prepare_host_runtime_v2(runtime_root=tree.root, inventory=tree.inventory, device='cpu')
        if (host._runtime_root != tree.root or
                host._inventory_json != tree.inventory.model_dump_json()):
            raise ExecutionPreparationError('execution_audio_host_tree_changed')
        environment = (('SystemRoot', str(host.windows_root)), ('WINDIR', str(host.windows_root)),
            ('TEMP', str(work_directory)), ('TMP', str(work_directory)),
            ('HF_HUB_OFFLINE', '1'), ('TRANSFORMERS_OFFLINE', '1'))
        invocation = PendingInvocation((str(tree.root / 'python.exe'), '-I', '-S', '-B',
            str(tree.root / ENTRY)), tree.root, environment, timeout_seconds)
        result = PreparedAudioGraphProbe(tree, host, invocation, derivation)
        result._preparation_binding = (host, invocation, derivation, tree.inventory.descriptor,
            canonical(host.observation.canonical()))
        def check():
            _check_sources(base, audio_root)
            _exact_path(work_directory, directory=True)
            with (tree.root / UPSTREAM_SLOT).open('rb') as source:
                if derive_soundfile(source.read(4 * 1024**2 + 1)) != derivation:
                    raise ExecutionPreparationError('execution_audio_derivation_changed')
        result._source_check = check
        result._preparation_identity_sha256 = hashlib.sha256(canonical({
            'recipe': 'audio-graph-probe-v6', 'inventory': tree.inventory.descriptor.sha256,
            'host': host.observation.canonical(), 'argv': invocation.argv,
            'environment': invocation.environment, 'cwd': str(invocation.cwd),
            'timeout': invocation.timeout_seconds,
            'derivation': hashlib.sha256(derivation.descriptor).hexdigest(),
            'derived_source': hashlib.sha256(derivation.payload).hexdigest(),
            'dispatch_authorized': False})).hexdigest()
        result.verify()
        return result
    except Exception:
        if tree is not None:
            tree.close()
        raise
