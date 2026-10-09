"""Application-only canonical inventory storage; no execution/file selection."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
from tempfile import NamedTemporaryFile

from app.domain.execution_runtime import (MAX_RUNTIME_MANIFEST_BYTES, OS_POLICY_SLOT, OS_POLICY_LIMIT, RuntimeInventory,
    RuntimeInventoryDescriptor, HostRuntimeObservation, HostRuntimeObservationV3Policy2)


class RuntimeInventoryError(ValueError):
    """Fixed diagnostic only; private paths/bytes are never included."""


def require_host_policy_inventory(host, inventory: RuntimeInventory):
    """Required declaration, never selection or proof of actual policy bytes."""
    if type(host) is HostRuntimeObservation:
        if host.trust_recipe == "windows-os-selected-driver-v1":
            return None
        if host.trust_recipe == "windows-os-selected-driver-v2":
            return require_os_policy_entry(inventory)
    if type(host) is not HostRuntimeObservationV3Policy2:
        raise RuntimeInventoryError("execution_host_recipe_unsupported")
    # Schema3 receipt adoption/revalidation checks this declaration against
    # implementation-owned resources, never caller-selected policy.
    # Actual prepared copies/tree and runtime context still need verification.
    try:
        host = HostRuntimeObservationV3Policy2.model_validate_json(host.model_dump_json())
    except ValueError:
        raise RuntimeInventoryError("execution_host_recipe_invalid") from None
    os_entry = require_os_policy_entry(inventory)
    from .providers.windows_component_policy import POLICY_SLOT, POLICY_LIMIT, owned_component_policy_bytes
    from .providers.windows_os_policy import owned_os_policy_bytes
    from .providers.runtime_primitives import NativeExecutionUnsupported
    entries = [item for item in inventory.files if item.name == POLICY_SLOT]
    if len(entries) != 1 or entries[0].role != "dependency" or not 0 < entries[0].size_bytes <= POLICY_LIMIT:
        raise RuntimeInventoryError("execution_component_policy_inventory_required")
    try:
        os_bytes, component_bytes = owned_os_policy_bytes(), owned_component_policy_bytes()
    except NativeExecutionUnsupported:
        raise RuntimeInventoryError("execution_owned_policy_unavailable") from None
    component_entry = entries[0]
    if (os_entry.size_bytes != len(os_bytes) or os_entry.sha256 != hashlib.sha256(os_bytes).hexdigest()
        or component_entry.size_bytes != len(component_bytes)
        or component_entry.sha256 != hashlib.sha256(component_bytes).hexdigest()
        or component_entry.sha256 != host.component_policy_sha256):
        raise RuntimeInventoryError("execution_policy_inventory_identity_changed")
    return os_entry


def require_os_policy_entry(inventory: RuntimeInventory):
    entries = [item for item in inventory.files if item.name == OS_POLICY_SLOT]
    if len(entries) != 1 or entries[0].role != "dependency" or not 0 < entries[0].size_bytes <= OS_POLICY_LIMIT:
        raise RuntimeInventoryError("execution_os_policy_inventory_required")
    return entries[0]


class RuntimeInventoryStore:
    def __init__(self, data_root: Path):
        try:
            root = Path(data_root)
            if not root.is_absolute() or root != root.resolve(strict=True) or not root.is_dir():
                raise ValueError()
            self.root = root
        except (OSError, ValueError, TypeError, RuntimeError):
            raise RuntimeInventoryError("execution_runtime_inventory_path_invalid") from None

    def _path(self, descriptor: RuntimeInventoryDescriptor) -> Path:
        path = self.root / "execution/runtime-manifests/sha256" / (descriptor.sha256 + ".json")
        try:
            resolved = path.resolve()
            if path != resolved or not resolved.is_relative_to(self.root):
                raise ValueError()
            return path
        except (OSError, ValueError, RuntimeError):
            raise RuntimeInventoryError("execution_runtime_inventory_path_invalid") from None

    @staticmethod
    def _descriptor(value):
        try:
            return RuntimeInventoryDescriptor.model_validate_json(value.model_dump_json())
        except (ValueError, TypeError, AttributeError):
            raise RuntimeInventoryError("execution_runtime_inventory_invalid") from None

    def read(self, descriptor: RuntimeInventoryDescriptor) -> RuntimeInventory:
        descriptor = self._descriptor(descriptor)
        path = self._path(descriptor)
        try:
            if not stat.S_ISREG(path.lstat().st_mode):
                raise OSError()
            with path.open("rb") as source:
                raw = source.read(min(descriptor.size_bytes, MAX_RUNTIME_MANIFEST_BYTES) + 1)
        except OSError:
            raise RuntimeInventoryError("execution_runtime_inventory_unavailable") from None
        if len(raw) != descriptor.size_bytes or hashlib.sha256(raw).hexdigest() != descriptor.sha256:
            raise RuntimeInventoryError("execution_runtime_inventory_changed")
        try:
            inventory = RuntimeInventory.model_validate_json(raw)
            # Also rejects duplicate JSON keys, noncanonical numbers/whitespace
            # and traversal order variants on disk. Producers canonicalize once.
            if inventory.canonical_bytes() != raw or inventory.descriptor != descriptor:
                raise ValueError()
            return inventory
        except (ValueError, TypeError):
            raise RuntimeInventoryError("execution_runtime_inventory_invalid") from None

    def install(self, inventory: RuntimeInventory) -> RuntimeInventoryDescriptor:
        """Publish complete bytes atomically without overwriting existing hashes.

        This local application extension has no HTTP write route. It adopts no
        license, selects no dependency, and never repairs conflicting bytes.
        """
        try:
            inventory = RuntimeInventory.model_validate_json(inventory.model_dump_json())
            raw = inventory.canonical_bytes()
            descriptor = inventory.descriptor
        except (ValueError, TypeError, AttributeError):
            raise RuntimeInventoryError("execution_runtime_inventory_invalid") from None
        path = self._path(descriptor)
        temporary = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._path(descriptor)  # detect aliased newly created ancestors
            with NamedTemporaryFile(prefix=".runtime-inventory-", dir=path.parent, delete=False) as target:
                temporary = Path(target.name)
                target.write(raw)
                target.flush()
                os.fsync(target.fileno())
            try:
                os.link(temporary, path)  # atomic exclusive publication, same directory/volume
            except FileExistsError:
                pass
            self.read(descriptor)  # concurrent/existing content must match too
            return descriptor
        except RuntimeInventoryError:
            raise
        except OSError:
            raise RuntimeInventoryError("execution_runtime_inventory_write_failed") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    raise RuntimeInventoryError("execution_runtime_inventory_cleanup_failed") from None
