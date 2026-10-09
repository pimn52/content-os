"""Bounded Windows CPU observation, not execution or hardware attestation.

No marker creation, model import, subprocess, environment-label fallback or
receipt input. Native runtime dependency closure remains a separate gate.
Cloned OS GUIDs/installation markers and hostile administrators are outside
this local accidental-change detection recipe's guarantees.
"""
from __future__ import annotations

from pathlib import Path
import stat
import sys
from uuid import UUID

from app.domain.models import ExecutionMachineObservation, execution_canonical_sha256
from app.providers.prepared import NativeExecutionUnsupported


MACHINE_OBSERVATION_RECIPE = "windows-machine-guid-registry-cpu"
MACHINE_OBSERVATION_VERSION = 1
INSTALLATION_MARKER = "execution/installation-id.txt"
_MAX_PROCESSORS = 256


def _installation_id(data_root: Path) -> UUID:
    try:
        root = Path(data_root)
        if not root.is_absolute() or root != root.resolve(strict=True) or not root.is_dir():
            raise ValueError()
        marker = root / INSTALLATION_MARKER
        if marker != marker.resolve(strict=True) or not stat.S_ISREG(marker.lstat().st_mode):
            raise ValueError()
        with marker.open("rb") as source:
            raw = source.read(129)
        if len(raw) > 128:
            raise ValueError()
        value = raw.decode("ascii").removesuffix("\n").removesuffix("\r")
        identifier = UUID(value)
        if identifier.int == 0 or value != str(identifier):
            raise ValueError()
        return identifier
    except (OSError, ValueError, UnicodeError, TypeError):
        raise NativeExecutionUnsupported("execution_installation_observation_unavailable") from None


def _read_windows_facts() -> tuple[str, tuple[tuple[str, str, str], ...]]:
    """OS registry only; never environment-derived hostname/CPU labels."""
    import winreg

    access = winreg.KEY_READ | winreg.KEY_WOW64_64KEY
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0, access) as key:
        guid, kind = winreg.QueryValueEx(key, "MachineGuid")
        if kind != winreg.REG_SZ:
            raise ValueError()
    processors = []
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                        r"HARDWARE\DESCRIPTION\System\CentralProcessor", 0, access) as key:
        names = []
        for index in range(_MAX_PROCESSORS + 1):
            try:
                names.append(winreg.EnumKey(key, index))
            except OSError as error:
                if getattr(error, "winerror", None) != 259:  # ERROR_NO_MORE_ITEMS only
                    raise
                break
        if not names or len(names) > _MAX_PROCESSORS or set(names) != {str(i) for i in range(len(names))}:
            raise ValueError()
        for name in sorted(names, key=int):
            with winreg.OpenKey(key, name, 0, access) as cpu:
                values = []
                for field in ("Identifier", "ProcessorNameString", "VendorIdentifier"):
                    value, kind = winreg.QueryValueEx(cpu, field)
                    if kind != winreg.REG_SZ:
                        raise ValueError()
                    values.append(value)
                processors.append(tuple(values))
    return guid, tuple(processors)


def observe_windows_cpu(*, data_root: Path, device: str) -> ExecutionMachineObservation:
    """Read existing installation binding and actual Windows CPU facts.

    CPU only. CUDA ordinal/physical-device/driver mapping is not established by
    this recipe, so GPU requests cannot silently become CPU observations.
    Raw machine GUID and CPU strings are never returned or included in errors.
    Caller must retain this recipe/version in its complete execution recipe;
    these selected-host facts do not prove loaded runtime/device identity.
    """
    if type(device) is not str or device != "cpu":
        raise NativeExecutionUnsupported("execution_device_observation_unsupported")
    if sys.platform != "win32":
        raise NativeExecutionUnsupported("execution_machine_platform_unsupported")
    installation_id = _installation_id(data_root)
    try:
        guid, processors = _read_windows_facts()
        if type(guid) is not str:
            raise ValueError()
        host = UUID(guid)
        if host.int == 0 or guid.lower() != str(host):
            raise ValueError()
        if (type(processors) is not tuple or not 1 <= len(processors) <= _MAX_PROCESSORS
            or any(type(cpu) is not tuple or len(cpu) != 3
                   or any(type(value) is not str or not value.strip() or len(value) > 1024
                          or "\0" in value for value in cpu) for cpu in processors)):
            raise ValueError()
        prefix = {"recipe": MACHINE_OBSERVATION_RECIPE, "version": MACHINE_OBSERVATION_VERSION}
        return ExecutionMachineObservation(installation_id=installation_id,
            host_sha256=execution_canonical_sha256({**prefix, "machine_guid": str(host)}),
            device_sha256=execution_canonical_sha256({**prefix, "device": "cpu",
                "logical_processors": [list(cpu) for cpu in processors]}))
    except (OSError, ValueError, TypeError, ImportError):
        raise NativeExecutionUnsupported("execution_machine_observation_unavailable") from None
