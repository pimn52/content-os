"""Independent complete selected-tree copies; no native support or authority.

Adapter recipes select actual roots, never an expected receipt's file list.
This primitive closes directory membership only. Bootstrap, host/device/DLL
origin checks and application reservation remain separate requirements.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import stat
from tempfile import TemporaryDirectory

from app.domain.execution_runtime import (MAX_RUNTIME_DIRECTORIES, MAX_RUNTIME_FILES,
    MAX_RUNTIME_FILE_BYTES, RuntimeInventory, RuntimeInventoryFile, runtime_path)
from .prepared import ExecutionPreparationError


@dataclass(frozen=True)
class RuntimeTreeSelection:
    source: Path
    destination: str
    source_policy: str = "complete"


@dataclass(frozen=True)
class RuntimeFileSelection:
    source: Path
    destination: str
    role: str


@dataclass(frozen=True)
class OwnedRuntimeFile:
    """Recipe-owned bootstrap/config bytes, not an expected evidence manifest."""
    destination: str
    payload: bytes
    role: str = "dependency"


def _with_owned(directories: set[str], files: list[RuntimeInventoryFile],
                owned: tuple[OwnedRuntimeFile, ...], max_bytes: int, owned_recipe: str = 'legacy') -> RuntimeInventory:
    directories, files = set(directories), list(files)
    if owned_recipe == 'pcm-static-probe-v7':
        from .pcm_static_probe import owned_pcm_static_files
        expected = owned_pcm_static_files()
        if (len(owned) != len(expected) or sorted(owned, key=lambda row: row.destination) !=
                sorted(expected, key=lambda row: row.destination)):
            raise ExecutionPreparationError('execution_pcm_bundle_changed')
    elif owned_recipe == 'audio-graph-probe-v6':
        from .audio_graph_probe import owned_audio_graph_probe_files
        expected = owned_audio_graph_probe_files()
        if (len(owned) != len(expected) or
                sorted(owned, key=lambda row: row.destination) !=
                sorted(expected, key=lambda row: row.destination)):
            raise ExecutionPreparationError('execution_audio_bundle_changed')
    elif owned_recipe not in ('legacy', 'component-probe-v3', 'component-owner-probe-v4', 'native-load-probe-v5') or len(owned) > (8 if owned_recipe == 'legacy' else 24):
        raise ExecutionPreparationError("execution_runtime_owned_limit")
    for item in owned:
        runtime_path(item.destination)
        if type(item.payload) is not bytes or not 0 < len(item.payload) <= 1024 * 1024:
            raise ExecutionPreparationError("execution_runtime_owned_limit")
        parts = item.destination.split("/")
        directories.update("/".join(parts[:i]) for i in range(1, len(parts)))
        files.append(RuntimeInventoryFile(name=item.destination, role=item.role,
            size_bytes=len(item.payload), sha256=hashlib.sha256(item.payload).hexdigest()))
    result = RuntimeInventory(inventory_version=1, directories=tuple(sorted(directories)), files=tuple(files))
    if result.descriptor.total_file_bytes > max_bytes:
        raise ExecutionPreparationError("execution_runtime_size_limit")
    return result


# Re-export the same shared path guard for existing callers/tests.
from .runtime_primitives import _exact_path


def _digest(path: Path, limit: int) -> tuple[int, str]:
    _exact_path(path, directory=False)
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            size += len(chunk)
            if size > limit:
                raise ExecutionPreparationError("execution_runtime_size_limit")
            digest.update(chunk)
    return size, digest.hexdigest()


def _members(root: Path, prefix: str, files: dict[str, Path], directories: set[str],
             source_policy: str = "complete") -> None:
    """Visit all members, including empty dirs; never follow aliases/caches."""
    _exact_path(root, directory=True)
    if source_policy not in ("complete", "cpython312-stdlib-source", "cpython312-package-source") or (
        source_policy == "cpython312-stdlib-source" and prefix != "Lib") or (
        source_policy == "cpython312-package-source" and prefix != "Lib/site-packages"):
        raise ExecutionPreparationError("execution_runtime_source_policy_invalid")
    pending = [(root, prefix)]
    omitted_cache_count = 0
    while pending:
        current, logical = pending.pop()
        _exact_path(current, directory=True)
        if logical:
            runtime_path(logical)
            directories.add(logical)
        if len(directories) > MAX_RUNTIME_DIRECTORIES:
            raise ExecutionPreparationError("execution_runtime_member_limit")
        with os.scandir(current) as entries:
            for entry in entries:
                name = runtime_path((logical + "/" if logical else "") + entry.name)
                path = Path(entry.path)
                mode = entry.stat(follow_symlinks=False).st_mode
                if stat.S_ISDIR(mode):
                    _exact_path(path, directory=True)
                    if source_policy != "complete":
                        if source_policy == "cpython312-stdlib-source" and current == root and entry.name == "site-packages":
                            continue  # explicitly excluded third-party root, never imported
                        if entry.name == "__pycache__":
                            with os.scandir(path) as cache:
                                for item in cache:
                                    omitted_cache_count += 1
                                    if omitted_cache_count > MAX_RUNTIME_FILES:
                                        raise ExecutionPreparationError("execution_runtime_member_limit")
                                    matched = re.fullmatch(r"(.+)\.cpython-312(?:\.opt-[12])?\.pyc", item.name)
                                    _exact_path(Path(item.path), directory=False)
                                    if not matched:
                                        raise ExecutionPreparationError("execution_runtime_bytecode_unsupported")
                                    _exact_path(current / (matched[1] + ".py"), directory=False)
                            continue  # omitted only when corresponding source is selected
                    pending.append((path, name))
                    if len(pending) + len(directories) > MAX_RUNTIME_DIRECTORIES:
                        raise ExecutionPreparationError("execution_runtime_member_limit")
                elif stat.S_ISREG(mode):
                    _exact_path(path, directory=False)
                    # Initial conservative layout: no source-less or ambient
                    # bytecode. A later recipe may explicitly select source and
                    # omit rebuildable caches, but this helper does not guess.
                    if path.suffix.casefold() in (".pyc", ".pyo"):
                        raise ExecutionPreparationError("execution_runtime_bytecode_unsupported")
                    if name in files:
                        raise ExecutionPreparationError("execution_runtime_members_duplicate")
                    files[name] = path
                    if len(files) > MAX_RUNTIME_FILES:
                        raise ExecutionPreparationError("execution_runtime_member_limit")
                else:
                    raise ExecutionPreparationError("execution_runtime_path_invalid")


def _inventory(files: dict[str, Path], directories: set[str], roles: dict[str, str],
               max_bytes: int, owned: tuple[OwnedRuntimeFile, ...] = (), owned_recipe: str = 'legacy') -> RuntimeInventory:
    artifacts, total = [], 0
    for name in sorted(files):
        size, digest = _digest(files[name], max_bytes - total)
        total += size
        artifacts.append(RuntimeInventoryFile(role=roles.get(name, "dependency"), name=name,
            size_bytes=size, sha256=digest))
    value = _with_owned(directories, artifacts, owned, max_bytes, owned_recipe)
    value.descriptor  # enforce canonical manifest byte cap too
    return value


def _scan_private(root: Path, roles: dict[str, str], max_bytes: int) -> RuntimeInventory:
    files, directories = {}, set()
    _members(root, "", files, directories)
    return _inventory(files, directories, roles, max_bytes)


class PreparedRuntimeTree:
    """Single-use private directory; caller still owns launch/accounting.

    Immutable detached inventory plus exact membership and fresh byte hashes.
    claim_for_launch must follow reservation, and does not itself launch.
    Copies are writable for bounded cleanup; immutability is enforced by
    revalidation, not claimed against a hostile OS administrator.
    """
    def __init__(self, temporary: TemporaryDirectory, inventory: RuntimeInventory, max_bytes: int):
        self._temporary = temporary
        self._root = Path(temporary.name)
        self._inventory_json = inventory.model_dump_json()
        self._max_bytes = max_bytes
        self._state = "prepared"

    @property
    def root(self) -> Path:
        return self._root

    @property
    def inventory(self) -> RuntimeInventory:
        return RuntimeInventory.model_validate_json(self._inventory_json)

    def verify(self) -> RuntimeInventory:
        if self._state != "prepared":
            raise ExecutionPreparationError("execution_preparation_consumed")
        try:
            expected = self.inventory
            actual = _scan_private(self.root, {item.name: item.role for item in expected.files}, self._max_bytes)
            if actual.descriptor != expected.descriptor:
                raise ValueError()
            return actual
        except (OSError, ValueError, RuntimeError):
            self._state = "failed"
            raise ExecutionPreparationError("execution_fixed_runtime_changed") from None

    def claim_for_launch(self) -> Path:
        self.verify()
        self._state = "consumed"
        return self.root

    def close(self) -> None:
        self._state = "closed"
        # Reject a replaced/redirected private root before recursive cleanup.
        # TemporaryDirectory owns exactly this unique directory, not sources.
        try:
            _exact_path(self.root, directory=True)
            self._temporary.cleanup()
        except (OSError, ValueError, RuntimeError):
            self._temporary._finalizer.detach()  # do not later delete a redirected root
            raise ExecutionPreparationError("execution_runtime_cleanup_failed") from None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def prepare_runtime_tree(*, trees: tuple[RuntimeTreeSelection, ...],
                         files: tuple[RuntimeFileSelection, ...],
                         staging_parent: Path, max_bytes: int,
                         owned_files: tuple[OwnedRuntimeFile, ...] = (),
                         max_files: int = MAX_RUNTIME_FILES, owned_recipe: str = 'legacy') -> PreparedRuntimeTree:
    """Copy selected complete roots, independently hash before/after copying.

    No expected inventory/specification/receipt input or public route. Full-tree
    work belongs outside SQLite locks. All errors use fixed diagnostics.
    """
    temporary = None
    try:
        if (type(max_bytes) is not int or not 0 < max_bytes <= MAX_RUNTIME_FILE_BYTES
            or type(max_files) is not int or not 3 <= max_files <= MAX_RUNTIME_FILES
            or not 1 <= len(trees) <= 32 or not 2 <= len(files) <= 32):
            raise ExecutionPreparationError("execution_runtime_selection_invalid")
        _exact_path(staging_parent, directory=True)
        selected, directories, roles = {}, set(), {}
        destinations = [item.destination for item in (*trees, *files)]
        for destination in destinations:
            runtime_path(destination)
        # Exactly one intentional composition: source policy excludes the
        # base's site-packages, and another complete selected root supplies it.
        # No arbitrary nested overlay or same-path replacement is allowed.
        for index, first in enumerate((*trees, *files)):
            for second in (*trees, *files)[index + 1:]:
                a, b = first.destination.casefold(), second.destination.casefold()
                if a == b or a.startswith(b + "/") or b.startswith(a + "/"):
                    pair = {first.destination, second.destination}
                    composed = (pair == {"Lib", "Lib/site-packages"}
                        and any(isinstance(item, RuntimeTreeSelection) and item.destination == "Lib"
                                and item.source_policy == "cpython312-stdlib-source" for item in (first, second))
                        and all(isinstance(item, RuntimeTreeSelection) for item in (first, second)))
                    if not composed:
                        raise ExecutionPreparationError("execution_runtime_selection_overlap")
        for item in trees:
            _members(item.source, item.destination, selected, directories, item.source_policy)
        for item in files:
            _exact_path(item.source, directory=False)
            if item.source.suffix.casefold() in (".pyc", ".pyo"):
                raise ExecutionPreparationError("execution_runtime_bytecode_unsupported")
            roles[item.destination] = item.role
            selected[item.destination] = item.source
        # Include implicit parents of separately selected roots/files.
        for name in (*directories, *selected):
            parts = name.split("/")
            directories.update("/".join(parts[:i]) for i in range(1, len(parts)))
        if len(selected) + len(owned_files) > max_files:
            raise ExecutionPreparationError("execution_runtime_member_limit")
        before = _inventory(selected, directories, roles, max_bytes, owned_files, owned_recipe)
        roles.update({item.destination: item.role for item in owned_files})
        temporary = TemporaryDirectory(prefix="content-os-runtime-", dir=staging_parent)
        root = Path(temporary.name)
        for name in before.directories:
            (root / name).mkdir(parents=True, exist_ok=True)
        total = 0
        for name, source in selected.items():
            _exact_path(source, directory=False)
            with source.open("rb") as incoming, (root / name).open("xb") as outgoing:
                for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
                    total += len(chunk)
                    if total > max_bytes:
                        raise ExecutionPreparationError("execution_runtime_size_limit")
                    outgoing.write(chunk)
            (root / name).chmod(stat.S_IREAD | stat.S_IWRITE | (source.stat().st_mode & 0o111))
        for item in owned_files:
            with (root / item.destination).open("xb") as target:
                target.write(item.payload)
        # Rescan source membership and hashes, not just the initial list.
        after_files, after_dirs = {}, set()
        for item in trees:
            _members(item.source, item.destination, after_files, after_dirs, item.source_policy)
        for item in files:
            after_files[item.destination] = item.source
        for name in (*after_dirs, *after_files):
            parts = name.split("/")
            after_dirs.update("/".join(parts[:i]) for i in range(1, len(parts)))
        if _inventory(after_files, after_dirs, roles, max_bytes, owned_files, owned_recipe).descriptor != before.descriptor:
            raise ExecutionPreparationError("execution_runtime_source_changed")
        actual = _scan_private(root, roles, max_bytes)
        if actual.descriptor != before.descriptor:
            raise ExecutionPreparationError("execution_runtime_source_changed")
        return PreparedRuntimeTree(temporary, actual, max_bytes)
    except Exception as error:
        if temporary is not None:
            try:
                _exact_path(Path(temporary.name), directory=True)
                temporary.cleanup()
            except (OSError, ValueError, RuntimeError):
                temporary._finalizer.detach()
                raise ExecutionPreparationError("execution_runtime_cleanup_failed") from None
        if isinstance(error, ExecutionPreparationError):
            raise
        raise ExecutionPreparationError("execution_runtime_selection_invalid") from None
