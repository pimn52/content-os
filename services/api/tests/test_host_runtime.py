"""M3 mocked OS/CUDA facts + synthetic files only, no host/driver probe."""
from dataclasses import replace
import hashlib
import os
from pathlib import Path

import pytest

from app.domain.execution_runtime import RuntimeInventory, RuntimeInventoryFile
from app.providers import host_runtime as subject
from app.providers.prepared import NativeExecutionUnsupported

GPU = "GPU-00000000-0000-0000-0000-000000000001"
OTHER_GPU = "GPU-00000000-0000-0000-0000-000000000002"


@pytest.fixture
def host(tmp_path, monkeypatch):
    windows = tmp_path / "Windows"
    system = windows / "System32"
    system.mkdir(parents=True)
    for name in ("nvcuda.dll", "nvml.dll", "kernel32.dll", "kernelbase.dll", "ntdll.dll"):
        (system / name).write_bytes(b"synthetic " + name.encode())
    state = {"windows": subject.WindowsFacts(windows, 0x8664, 0, 26100, 1),
        "cuda": (subject.CudaPhysicalFacts(GPU, "00000000:01:00.0", GPU, "00000000:01:00.0", 5, "999.1"),)}
    monkeypatch.setattr(subject.sys, "platform", "win32")
    monkeypatch.setattr(subject, "_read_windows_facts", lambda: state["windows"])
    monkeypatch.setattr(subject, "_read_cuda_facts", lambda **_kwargs: state["cuda"])
    return state


def gpu():
    return subject.prepare_host_runtime(device="cuda:0", physical_uuid=GPU)


def test_cpu_os_typed_observation_is_not_env_label_or_gpu_mapping(host, monkeypatch):
    monkeypatch.setenv("SystemRoot", "C:\\forged")
    monkeypatch.setenv("PROCESSOR_ARCHITECTURE", "ARM64")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
    witness = subject.prepare_host_runtime(device="cpu")
    assert witness.verify() == witness.observation
    assert witness.observation.build == 26100 and witness.observation.revision == 1
    assert witness.observation.device.kind == "cpu" and witness.child_environment == ()
    assert witness.windows_root == host["windows"].root
    assert str(host["windows"].root) not in witness.observation.model_dump_json()


def test_independent_physical_cuda_mapping_pins_uuid_not_nvml_or_parent_index(host, monkeypatch):
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
    before = dict(os.environ)
    witness = gpu()
    assert dict(witness.child_environment) == {"CUDA_VISIBLE_DEVICES": GPU, "CUDA_DEVICE_ORDER": "PCI_BUS_ID"}
    assert witness.observation.device.uuid == GPU
    assert witness.observation.device.pci_address == "00000000:01:00.0"
    assert {item.slot for item in witness.observation.device.libraries} == {"driver/nvcuda.dll", "probe/nvml.dll"}
    assert witness.verify() == witness.observation and dict(os.environ) == before


@pytest.mark.parametrize("change", ["build", "revision", "arm", "wow64", "unknown_build", "bool_revision"])
def test_host_changes_consume_prepared_witness(host, change):
    witness = gpu()
    fields = {"build": {"build": 26200}, "revision": {"revision": 2}, "arm": {"native_machine": 0xaa64},
        "wow64": {"process_machine": 0x14c}, "unknown_build": {"build": 0}, "bool_revision": {"revision": True}}
    host["windows"] = replace(host["windows"], **fields[change])
    with pytest.raises(NativeExecutionUnsupported, match="host_runtime_changed"):
        witness.verify()
    with pytest.raises(NativeExecutionUnsupported, match="witness_consumed"):
        witness.verify()


@pytest.mark.parametrize("change", ["physical", "pci", "driver_version", "ordinal", "driver_bytes", "probe_bytes", "missing_driver"])
def test_gpu_driver_mapping_and_same_size_time_bytes_are_rechecked(host, change):
    witness = gpu()
    if change in ("driver_bytes", "probe_bytes", "missing_driver"):
        filename = "nvml.dll" if change == "probe_bytes" else "nvcuda.dll"
        path = host["windows"].root / "System32" / filename
        if change == "missing_driver": path.unlink()
        else:
            before = path.stat()
            path.write_bytes(b"x" * before.st_size)
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    else:
        fields = {"physical": {"physical_uuid": OTHER_GPU, "cuda_uuid": OTHER_GPU},
            "pci": {"physical_pci": "00000000:02:00.0", "cuda_pci": "00000000:02:00.0"},
            "driver_version": {"driver_version": "999.2"}, "ordinal": {"cuda_ordinal": 6}}
        host["cuda"] = (replace(host["cuda"][0], **fields[change]),)
    with pytest.raises(NativeExecutionUnsupported, match="host_runtime_changed"):
        witness.verify()


@pytest.mark.parametrize("problem", ["no_uuid", "wrong_uuid", "no_records", "list_records", "duplicate",
    "cuda_uuid", "cuda_pci", "bool_ordinal", "negative_ordinal", "unknown_version"])
def test_missing_ambiguous_or_unknown_gpu_information_never_falls_back_to_cpu(host, problem):
    uuid = GPU
    if problem == "no_uuid": uuid = None
    elif problem == "wrong_uuid": uuid = OTHER_GPU
    elif problem == "no_records": host["cuda"] = ()
    elif problem == "list_records": host["cuda"] = list(host["cuda"])
    elif problem == "duplicate": host["cuda"] = host["cuda"] * 2
    else:
        fields = {"cuda_uuid": {"cuda_uuid": OTHER_GPU}, "cuda_pci": {"cuda_pci": "00000000:02:00.0"},
            "bool_ordinal": {"cuda_ordinal": True}, "negative_ordinal": {"cuda_ordinal": -1},
            "unknown_version": {"driver_version": "unknown"}}
        host["cuda"] = (replace(host["cuda"][0], **fields[problem]),)
    with pytest.raises(NativeExecutionUnsupported):
        subject.prepare_host_runtime(device="cuda:0", physical_uuid=uuid)


def test_actual_cuda_backend_remains_unsupported_and_has_no_cpu_fallback(host, monkeypatch):
    def stop(**_kwargs):
        raise NativeExecutionUnsupported("execution_cuda_backend_unsupported")
    monkeypatch.setattr(subject, "_read_cuda_facts", stop)
    with pytest.raises(NativeExecutionUnsupported, match="cuda_backend_unsupported"):
        gpu()


def test_selected_driver_bytes_are_verified_before_probe_and_rechecked_after_it(host, monkeypatch):
    def mutate(**kwargs):
        assert {item.slot for item in kwargs["driver_sources"]} == {"driver/nvcuda.dll", "probe/nvml.dll"}
        for source, descriptor in zip(kwargs["driver_sources"], kwargs["libraries"]):
            assert hashlib.sha256(source.path.read_bytes()).hexdigest() == descriptor.sha256
        kwargs["driver_sources"][0].path.write_bytes(b"changed during probe")
        return host["cuda"]
    monkeypatch.setattr(subject, "_read_cuda_facts", mutate)
    with pytest.raises(NativeExecutionUnsupported, match="driver_probe_changed"):
        gpu()
    (host["windows"].root / "System32/nvml.dll").unlink()
    calls = []
    monkeypatch.setattr(subject, "_read_cuda_facts", lambda **kwargs: calls.append(kwargs))
    with pytest.raises(NativeExecutionUnsupported):
        gpu()
    assert calls == []


def inventory(root):
    artifacts = []
    for role, name in (("interpreter", "python.exe"), ("entrypoint", "entry.py"), ("dependency", "fixed.dll")):
        path = root / name
        path.write_bytes(b"fixed " + name.encode())
        artifacts.append(RuntimeInventoryFile(role=role, name=name, size_bytes=path.stat().st_size,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    return RuntimeInventory(inventory_version=1, directories=(), files=tuple(artifacts))


def test_loaded_origin_gate_accepts_exact_fixed_driver_and_minimal_os_components(host, tmp_path):
    root = tmp_path / "private"
    root.mkdir()
    value = inventory(root)
    witness = gpu()
    paths = (root / "fixed.dll", host["windows"].root / "System32/nvcuda.dll",
        host["windows"].root / "System32/kernel32.dll")
    witness.verify_loaded_origins(runtime_root=root, inventory=value, loaded_paths=paths)


@pytest.mark.parametrize("problem", ["system_third_party", "path", "driverstore", "private_extra", "changed_fixed", "empty"])
def test_directory_labels_do_not_authorize_unknown_loaded_libraries(host, tmp_path, problem):
    root = tmp_path / "private"
    root.mkdir()
    value = inventory(root)
    witness = gpu()
    if problem == "system_third_party": path = host["windows"].root / "System32/cuda-user.dll"
    elif problem == "driverstore": path = host["windows"].root / "System32/DriverStore/shadow.dll"
    elif problem == "private_extra": path = root / "unlisted.dll"
    elif problem == "changed_fixed": path = root / "fixed.dll"
    else: path = tmp_path / "ambient/shadow.dll"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"changed or unknown")
    with pytest.raises(NativeExecutionUnsupported, match="loaded_origin_unsupported"):
        witness.verify_loaded_origins(runtime_root=root, inventory=value,
            loaded_paths=() if problem == "empty" else (path,))
    with pytest.raises(NativeExecutionUnsupported, match="witness_consumed"):
        witness.verify()
