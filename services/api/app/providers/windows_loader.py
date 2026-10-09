"""Owned current-process loading gates; application-only, not native admission.

Only a future fresh prepared child may instantiate the live reader. No caller
origin list is accepted as the observed baseline. Native adapters/launch remain
unsupported until recipe-specific dynamic closure, device and M4 gates close.
"""
from __future__ import annotations

from pathlib import Path
import hashlib
import struct
import sys
from types import ModuleType

from .prepared import NativeExecutionUnsupported
from .runtime_tree import _exact_path
from .runtime_primitives import require_search_topology, require_unique_native_origins

MODULE_LIMIT = 2048
PATH_LIMIT = 32768
SYSTEM32 = 0x800
TARGET_FLAGS = SYSTEM32 | 0x100  # DLL_LOAD_DIR; no PATH/CWD/user-directory search.


from .windows_native import WindowsModuleReader, python_origins


def require_no_redirection(command, *, target_parent=None):
    """Conservative external redirection/native-basename collision rejection.

    Embedded activation-context/dynamic vendor closure is NOT certified here.
    Unlisted external files are additionally rejected by full tree verification.
    """
    from .windows_os_policy import read_prepared_os_policy, parse_os_policy
    command.verify()
    root, inventory = command.invocation.cwd, command.inventory
    profile = parse_os_policy(read_prepared_os_policy(runtime_root=root, inventory=inventory))
    reserved = profile.component_names | profile.contract_names
    device = command.host_observation.device
    reserved |= frozenset(Path(item.slot).name.casefold() for item in getattr(device, "libraries", ()))
    require_search_topology(tuple(entry.name for entry in inventory.files), inventory.directories,
        reserved, target_parent=target_parent)


def require_resource_free_target(path, entry):
    """First loader recipe rejects all PE resources, including embedded manifests.

    Deliberately conservative: resources are not automatically SxS-safe just
    because a static import table is supported. No XML/resource loader here.
    """
    from .pe_imports import PE_LIMIT, parse_pe_imports
    try:
        _exact_path(path, directory=False)
        with path.open("rb") as source:
            payload = source.read(min(entry.size_bytes, PE_LIMIT) + 1)
        if len(payload) != entry.size_bytes or hashlib.sha256(payload).hexdigest() != entry.sha256:
            raise ValueError()
        parse_pe_imports(payload)
        optional = struct.unpack_from("<I", payload, 60)[0] + 24
        count = struct.unpack_from("<I", payload, optional + 108)[0]
        if count > 2 and any(struct.unpack_from("<II", payload, optional + 112 + 2 * 8)):
            raise NativeExecutionUnsupported("execution_target_resources_unsupported")
    except (OSError, ValueError, struct.error):
        raise NativeExecutionUnsupported("execution_target_identity_changed") from None


class ControlledLoader:
    """Ordered application-only gate for one target, failure consumes session.

    Dependencies still need adapter-specific pre-load closure. This primitive
    cannot grant receipt/reservation/launch permission or run a model callback.
    """
    def __init__(self, command, reader, modules):
        self._command, self._reader, self._modules = command, reader, modules
        self._state, self._handle = "new", None

    def _origins(self):
        native = self._reader.native_origins()
        if type(native) is not tuple or not 1 <= len(native) <= MODULE_LIMIT or len(set(native)) != len(native):
            raise NativeExecutionUnsupported("execution_module_observation_unavailable")
        self._command.verify_loaded_origins(native)
        require_unique_native_origins(native)
        self._command.verify_loaded_origins(python_origins(self._modules))
        return native

    def begin(self):
        if self._state != "new":
            raise NativeExecutionUnsupported("execution_loader_consumed")
        self._state = "failed"
        require_no_redirection(self._command)
        self._origins()  # Before search changes or target LoadLibrary/DllMain.
        self._reader.require_no_activation()
        self._reader.restrict_search()
        self._origins()  # Search setup cannot obscure an unexpected module.
        self._state = "ready"

    def load(self, path):
        if self._state != "ready":
            raise NativeExecutionUnsupported("execution_loader_consumed")
        self._state = "failed"
        try:
            from .pe_imports import read_pe_imports, require_known_imports
            from .windows_os_policy import read_prepared_os_policy, parse_os_policy
            require_no_redirection(self._command)
            root, inventory = self._command.invocation.cwd, self._command.inventory
            if not isinstance(path, Path) or path.suffix.casefold() not in (".dll", ".pyd"):
                raise NativeExecutionUnsupported("execution_target_selection_unsupported")
            entry = next((item for item in inventory.files if root / item.name == path), None)
            if entry is None:
                raise NativeExecutionUnsupported("execution_target_selection_unsupported")
            require_no_redirection(self._command, target_parent=path.parent.relative_to(root))
            policy = parse_os_policy(read_prepared_os_policy(runtime_root=root, inventory=inventory))
            # Only fixed sibling libraries and finite OS names are resolvable
            # by this initial explicit load-dir/System32 recipe.
            siblings = frozenset(Path(item.name).name.casefold() for item in inventory.files
                if (root / item.name).parent == path.parent and Path(item.name).suffix.casefold() in (".dll", ".pyd"))
            allowed = siblings | policy.component_names | policy.contract_names
            # Check every resolvable private sibling, not just the top DLL:
            # a dependency's delay imports/resources can also redirect loading.
            for member in inventory.files:
                sibling = root / member.name
                if sibling.parent != path.parent or sibling.suffix.casefold() not in (".dll", ".pyd"):
                    continue
                imports = read_pe_imports(sibling, size_bytes=member.size_bytes, sha256=member.sha256)
                require_known_imports(imports, allowed=allowed)
                require_resource_free_target(sibling, member)
            self._origins()
            self._reader.require_no_activation()
            self._handle = self._reader.load(path)
            if not self._handle:
                raise NativeExecutionUnsupported("execution_target_load_failed")
            origins = self._origins()
            if path not in origins:
                raise NativeExecutionUnsupported("execution_target_origin_missing")
            # A handle is not exposed until the post-load gate passes.
            self._state = "loaded"
            return self._handle
        except Exception:
            self.close()
            raise

    def close(self):
        self._state = "closed"
        if self._handle is not None:
            handle, self._handle = self._handle, None
            self._reader.release(handle)

    def __enter__(self):
        self.begin()
        return self

    def __exit__(self, *_args):
        self.close()


def begin_current_child(command):
    """No caller-provided reader/origin claims on the future actual child path."""
    from .python_bootstrap import verify_startup
    root = Path(verify_startup(sys))
    if root != command.invocation.cwd or sys.executable != command.invocation.argv[0]:
        raise NativeExecutionUnsupported("execution_loader_child_mismatch")
    require_no_redirection(command)
    # No native target before both baseline gates pass. This is not wired into
    # bootstrap/Worker yet; native dispatch remains structurally unsupported.
    reader = WindowsModuleReader(command.system_directory)
    result = ControlledLoader(command, reader, sys.modules)
    result.begin()
    return result
