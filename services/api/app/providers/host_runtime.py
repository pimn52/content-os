"""M3 application-only host witness and origin gate; no native admission.

Windows CPU reader is used by application-only preparation, not normal Workers.
CUDA backend deliberately remains unsupported. Tests substitute owned reader
boundaries; a receipt or user label is never the observation source.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys

from app.domain.execution_runtime import (CPUHostDevice, CUDAHostDevice, HostDriverLibrary,
    HostRuntimeObservation, RuntimeInventory)
from .prepared import NativeExecutionUnsupported
from .runtime_tree import _digest, _exact_path

RECIPE = "windows-platform-ubr-selected-device"
VERSION = 1
LIBRARY_LIMIT = 64 * 1024 * 1024
OS_COMPONENTS = frozenset(("ntdll.dll", "kernel32.dll", "kernelbase.dll"))


from .windows_native import WindowsFacts, _read_windows_facts


@dataclass(frozen=True)
class CudaPhysicalFacts:
    # These independent backend observations must bridge CUDA and physical
    # inventory. An NVML index is intentionally not part of the interface.
    physical_uuid: str
    physical_pci: str
    cuda_uuid: str
    cuda_pci: str
    cuda_ordinal: int
    driver_version: str




def _read_cuda_facts(*, windows_root: Path, driver_sources: tuple[DriverSource, ...],
                     libraries: tuple[HostDriverLibrary, ...], profile=None) -> tuple[CudaPhysicalFacts, ...]:
    from .pe_imports import read_pe_imports, require_known_imports

    # Static preflight can disprove closure before any native load, never prove
    # dynamic imports/forwarders or permit a GPU call. No directory exemptions.
    allowed = (OS_COMPONENTS if profile is None else profile.component_names | profile.contract_names
        ) | frozenset(("nvcuda.dll", "nvml.dll"))
    for source, descriptor in zip(driver_sources, libraries, strict=True):
        value = read_pe_imports(source.path, size_bytes=descriptor.size_bytes, sha256=descriptor.sha256)
        require_known_imports(value, allowed=allowed)
    # Actual verified loader/device observer remains a separate required seam.
    raise NativeExecutionUnsupported("execution_cuda_backend_unsupported")


@dataclass(frozen=True)
class DriverSource:
    slot: str
    path: Path


@dataclass(frozen=True)
class _Observed:
    observation: HostRuntimeObservation
    windows_root: Path
    driver_sources: tuple[DriverSource, ...]
    child_environment: tuple[tuple[str, str], ...]
    source_cuda_ordinal: int | None
    system_directory: Path | None = None


def _observe(device: str, physical_uuid: str | None, *, profile=None) -> _Observed:
    if sys.platform != "win32":
        raise NativeExecutionUnsupported("execution_host_platform_unsupported")
    if device not in ("cpu", "cuda:0") or (device == "cpu" and physical_uuid is not None):
        raise NativeExecutionUnsupported("execution_host_device_unsupported")
    try:
        facts = _read_windows_facts()
        if (not isinstance(facts, WindowsFacts) or type(facts.native_machine) is not int
            or type(facts.process_machine) is not int or facts.native_machine != 0x8664
            or facts.process_machine != 0):
            raise NativeExecutionUnsupported("execution_host_architecture_unsupported")
        _exact_path(facts.root, directory=True)
        _exact_path(facts.root / "System32", directory=True)
        system_directory = facts.root / "System32"
        if profile is not None:
            if facts.system_directory != system_directory:
                raise NativeExecutionUnsupported("execution_os_system_directory_unsupported")
            _exact_path(facts.system_directory, directory=True)
            for name in profile.component_names:
                _exact_path(system_directory / name, directory=False)
        sources, environment, ordinal = (), (), None
        selected = CPUHostDevice(kind="cpu")
        if device == "cuda:0":
            if type(physical_uuid) is not str or not physical_uuid:
                raise NativeExecutionUnsupported("execution_cuda_selection_required")
            # Verify specifically selected probe/driver bytes BEFORE calling
            # the (still unsupported) owned backend. Never load first/search
            # broadly and retroactively approve the resolved library.
            descriptors, source_list = [], []
            for role, filename in (("driver", "nvcuda.dll"), ("probe", "nvml.dll")):
                path = facts.root / "System32" / filename
                size, digest = _digest(path, LIBRARY_LIMIT)
                slot = role + "/" + filename
                descriptors.append(HostDriverLibrary(role=role, slot=slot, size_bytes=size, sha256=digest))
                source_list.append(DriverSource(slot, path))
            sources = tuple(source_list)
            probe_args = dict(windows_root=facts.root, driver_sources=sources, libraries=tuple(descriptors))
            if profile is not None:
                probe_args["profile"] = profile
            records = _read_cuda_facts(**probe_args)
            for source, descriptor in zip(sources, descriptors):
                if _digest(source.path, LIBRARY_LIMIT) != (descriptor.size_bytes, descriptor.sha256):
                    raise NativeExecutionUnsupported("execution_driver_probe_changed")
            if (type(records) is not tuple or not 1 <= len(records) <= 32
                or any(not isinstance(item, CudaPhysicalFacts) for item in records)
                or len({item.physical_uuid for item in records}) != len(records)
                or len({item.physical_pci for item in records}) != len(records)
                or len({item.cuda_ordinal for item in records}) != len(records)
                or any(type(item.cuda_ordinal) is not int or not 0 <= item.cuda_ordinal < 32
                       or item.physical_uuid != item.cuda_uuid or item.physical_pci != item.cuda_pci
                       for item in records)):
                raise NativeExecutionUnsupported("execution_cuda_mapping_invalid")
            matches = [item for item in records if item.physical_uuid == physical_uuid]
            if len(matches) != 1:
                raise NativeExecutionUnsupported("execution_cuda_device_unavailable")
            physical = matches[0]
            ordinal = physical.cuda_ordinal
            # First recipe deliberately accepts only these exact OS-resolved
            # locations. DriverStore/NVSMI/PATH alternatives are unsupported,
            # not generic exemptions or dynamically broadened search roots.
            selected = CUDAHostDevice(kind="cuda", uuid=physical.physical_uuid,
                pci_address=physical.physical_pci, driver_version=physical.driver_version,
                libraries=tuple(descriptors))
            # UUID pinning intentionally maps any selected physical ordinal to
            # child cuda:0. Never reuse the parent's index or mutate its env.
            environment = (("CUDA_DEVICE_ORDER", "PCI_BUS_ID"), ("CUDA_VISIBLE_DEVICES", selected.uuid))
        observation = HostRuntimeObservation(observation_recipe=RECIPE, observation_version=VERSION if profile is None else 2,
            trust_recipe="windows-os-selected-driver-v1" if profile is None else "windows-os-selected-driver-v2",
            os_family="windows", architecture="amd64",
            build=facts.build, revision=facts.revision, device=selected)
        return _Observed(observation, facts.root, sources, environment, ordinal, system_directory)
    except NativeExecutionUnsupported:
        raise
    except (OSError, ValueError, TypeError, AttributeError, RuntimeError, ImportError):
        raise NativeExecutionUnsupported("execution_host_observation_unavailable") from None


class PreparedHostRuntime:
    """Retained actual-source locations + detached typed identity for recheck.

    This provides no license/capability or Worker permission. Re-observe outside
    SQLite locks before reservation/launch; failure consumes the witness.
    """
    def __init__(self, device: str, physical_uuid: str | None, observed: _Observed):
        self._device, self._uuid = device, physical_uuid
        self._observation_json = observed.observation.model_dump_json()
        self._root = observed.windows_root
        self._sources = observed.driver_sources
        self._environment = observed.child_environment
        self._ordinal = observed.source_cuda_ordinal
        self._state = "prepared"

    @property
    def observation(self):
        return HostRuntimeObservation.model_validate_json(self._observation_json)

    @property
    def child_environment(self):
        return self._environment

    @property
    def windows_root(self):
        return self._root

    def verify(self):
        if self._state != "prepared":
            raise NativeExecutionUnsupported("execution_host_witness_consumed")
        try:
            current = _observe(self._device, self._uuid)
            if (current.observation.canonical() != self.observation.canonical()
                or current.windows_root != self._root or current.driver_sources != self._sources
                or current.child_environment != self._environment or current.source_cuda_ordinal != self._ordinal):
                raise ValueError()
            return self.observation
        except (OSError, ValueError, RuntimeError):
            self._state = "failed"
            raise NativeExecutionUnsupported("execution_host_runtime_changed") from None

    def verify_loaded_origins(self, *, runtime_root: Path, inventory: RuntimeInventory,
                              loaded_paths: tuple[Path, ...]):
        self.verify()
        self._verify_origins(runtime_root=runtime_root, inventory=inventory, loaded_paths=loaded_paths,
            os_components={self._root / "System32" / name for name in OS_COMPONENTS})

    def _verify_origins(self, *, runtime_root, inventory, loaded_paths, os_components):
        try:
            _exact_path(runtime_root, directory=True)
            inventory = RuntimeInventory.model_validate_json(inventory.model_dump_json())
            if type(loaded_paths) is not tuple or not 1 <= len(loaded_paths) <= 2048:
                raise ValueError()
            fixed = {runtime_root / item.name: item for item in inventory.files}
            drivers = {source.path: next(item for item in self.observation.device.libraries
                       if item.slot == source.slot) for source in self._sources}
            for path in loaded_paths:
                _exact_path(path, directory=False)
                artifact = fixed.get(path) or drivers.get(path)
                if artifact is not None:
                    if _digest(path, LIBRARY_LIMIT if path in drivers else artifact.size_bytes) != (
                            artifact.size_bytes, artifact.sha256):
                        raise ValueError()
                elif path not in os_components:
                    raise ValueError()
        except (OSError, ValueError, RuntimeError, TypeError, AttributeError):
            self._state = "failed"
            raise NativeExecutionUnsupported("execution_loaded_origin_unsupported") from None


def prepare_host_runtime(*, device: str, physical_uuid: str | None = None) -> PreparedHostRuntime:
    return PreparedHostRuntime(device, physical_uuid, _observe(device, physical_uuid))


class PreparedHostRuntimeV2(PreparedHostRuntime):
    """Same independently selected profile bytes must survive tree/host recheck."""
    def __init__(self, *, runtime_root, inventory, profile_bytes, device, physical_uuid, observed):
        super().__init__(device, physical_uuid, observed)
        self._runtime_root = runtime_root
        self._inventory_json = inventory.model_dump_json()
        self._profile_bytes = profile_bytes
        self._system_directory = observed.system_directory

    def verify(self):
        from .windows_os_policy import read_prepared_os_policy, parse_os_policy
        if self._state != "prepared":
            raise NativeExecutionUnsupported("execution_host_witness_consumed")
        try:
            payload = read_prepared_os_policy(runtime_root=self._runtime_root,
                inventory=RuntimeInventory.model_validate_json(self._inventory_json))
            if payload != self._profile_bytes:
                raise ValueError()
            current = _observe(self._device, self._uuid, profile=parse_os_policy(payload))
            if (current.observation.canonical() != self.observation.canonical()
                or current.windows_root != self._root or current.system_directory != self._system_directory
                or current.driver_sources != self._sources or current.child_environment != self._environment
                or current.source_cuda_ordinal != self._ordinal):
                raise ValueError()
            return self.observation
        except (OSError, ValueError, RuntimeError):
            self._state = "failed"
            raise NativeExecutionUnsupported("execution_host_runtime_changed") from None

    def verify_loaded_origins(self, *, runtime_root, inventory, loaded_paths):
        from .windows_os_policy import parse_os_policy
        from .runtime_primitives import require_unique_native_origins
        self.verify()
        try:
            inventory = RuntimeInventory.model_validate_json(inventory.model_dump_json())
            if runtime_root != self._runtime_root or inventory.descriptor != RuntimeInventory.model_validate_json(
                self._inventory_json).descriptor:
                raise ValueError()
            policy = parse_os_policy(self._profile_bytes)
            if type(loaded_paths) is not tuple or not 1 <= len(loaded_paths) <= 2048:
                raise ValueError()
            require_unique_native_origins(loaded_paths)
            reserved = {name: self._system_directory / name for name in policy.component_names}
            reserved.update({item.path.name.casefold(): item.path for item in self._sources})
            for path in loaded_paths:
                name = path.name.casefold()
                if name in policy.contract_names or name in reserved and path != reserved[name]:
                    raise ValueError()
        except (ValueError, TypeError, AttributeError, RuntimeError):
            self._state = "failed"
            raise NativeExecutionUnsupported("execution_loaded_origin_unsupported") from None
        self._verify_origins(runtime_root=runtime_root, inventory=inventory, loaded_paths=loaded_paths,
            os_components=set(reserved.values()) - {item.path for item in self._sources})


def prepare_host_runtime_v2(*, runtime_root: Path, inventory: RuntimeInventory,
                            device: str, physical_uuid: str | None = None) -> PreparedHostRuntimeV2:
    from .windows_os_policy import owned_os_policy_bytes, parse_os_policy, read_prepared_os_policy
    inventory = RuntimeInventory.model_validate_json(inventory.model_dump_json())
    # Independently select owned implementation policy before any host probe.
    payload = owned_os_policy_bytes()
    read_prepared_os_policy(runtime_root=runtime_root, inventory=inventory)
    observed = _observe(device, physical_uuid, profile=parse_os_policy(payload))
    return PreparedHostRuntimeV2(runtime_root=runtime_root, inventory=inventory, profile_bytes=payload,
        device=device, physical_uuid=physical_uuid, observed=observed)
