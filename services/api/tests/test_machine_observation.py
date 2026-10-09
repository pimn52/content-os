"""Synthetic marker/registry evidence; no real host probing or marker writes."""
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.providers import machine_observation as subject
from app.providers.prepared import NativeExecutionUnsupported
from app.domain.models import ExecutionArtifact, ExecutionSpecification


@pytest.fixture
def observed(tmp_path, monkeypatch):
    root = tmp_path / "data"
    marker = root / subject.INSTALLATION_MARKER
    marker.parent.mkdir(parents=True)
    marker.write_text(str(UUID(int=1)), encoding="ascii")
    facts = [str(UUID(int=2)), (("Intel64 Family 6 Model 1", "Synthetic CPU", "GenuineIntel"),) * 2]
    monkeypatch.setattr(subject.sys, "platform", "win32")
    monkeypatch.setattr(subject, "_read_windows_facts", lambda: tuple(facts))
    return root, marker, facts


def observe(root):
    return subject.observe_windows_cpu(data_root=root, device="cpu")


def specification(machine):
    def artifact(role, name):
        return ExecutionArtifact(role=role, name=name, size_bytes=1, sha256="a" * 64)
    return ExecutionSpecification(schema_version=1, capability="voice", provider="synthetic",
        model="synthetic", runtime="same-runtime-label", machine_id="same-machine-label",
        observation_recipe=subject.MACHINE_OBSERVATION_RECIPE,
        observation_version=subject.MACHINE_OBSERVATION_VERSION, machine=machine,
        model_artifacts=(artifact("weights", "model.safetensors"),),
        runtime_artifacts=(artifact("interpreter", "python.exe"), artifact("entrypoint", "entry.py"),
                           artifact("dependency", "dependency.py")), parameters={"device": "cpu"})


def test_stable_across_cwd_and_environment_labels(observed, monkeypatch, tmp_path):
    root, marker, _ = observed
    before = observe(root)
    monkeypatch.chdir(tmp_path)
    for name in ("COMPUTERNAME", "PROCESSOR_IDENTIFIER", "CONTENT_OS_MACHINE_ID", "CUDA_VISIBLE_DEVICES"):
        monkeypatch.setenv(name, "not-an-observation")
    assert observe(root) == before
    assert specification(observe(root)).execution_sha256 == specification(before).execution_sha256
    assert before.installation_id == UUID(int=1)
    assert marker.read_text() == str(UUID(int=1))
    assert str(UUID(int=2)) not in before.model_dump_json()


@pytest.mark.parametrize("changed", ["installation", "host", "cpu_identifier", "cpu_name", "cpu_vendor", "cpu_count"])
def test_independent_identity_changes(observed, changed):
    root, marker, facts = observed
    before = observe(root)
    if changed == "installation":
        marker.write_text(str(UUID(int=3)))
    elif changed == "host":
        facts[0] = str(UUID(int=4))
    elif changed == "cpu_count":
        facts[1] = facts[1][:1]
    else:
        cpu = list(facts[1][0])
        cpu[{"cpu_identifier": 0, "cpu_name": 1, "cpu_vendor": 2}[changed]] += " changed"
        facts[1] = (tuple(cpu), facts[1][1])
    after = observe(root)
    assert after != before
    # Unchanged configuration labels/files cannot hide a different actual host
    # or device; changing machine facts does not relabel model bytes.
    assert specification(after).execution_sha256 != specification(before).execution_sha256
    assert specification(after).model_artifact_sha256 == specification(before).model_artifact_sha256
    assert (after.host_sha256 != before.host_sha256) == (changed == "host")
    assert (after.device_sha256 != before.device_sha256) == changed.startswith("cpu_")
    assert (after.installation_id != before.installation_id) == (changed == "installation")


@pytest.mark.parametrize("raw", [b"", b"not uuid", str(UUID(int=0)).encode(),
    b" " + str(UUID(int=1)).encode(), b"x" * 129, b"\xff", b"00000000000000000000000000000001"])
def test_bad_marker_never_generates_replacement(observed, raw):
    root, marker, _ = observed
    marker.write_bytes(raw)
    with pytest.raises(NativeExecutionUnsupported, match="installation_observation_unavailable"):
        observe(root)
    assert marker.read_bytes() == raw


def test_missing_marker_and_relative_root_stop_without_writes(observed, monkeypatch):
    root, marker, _ = observed
    marker.unlink()
    with pytest.raises(NativeExecutionUnsupported, match="installation_observation_unavailable"):
        observe(root)
    assert not marker.exists()
    monkeypatch.chdir(root.parent)
    with pytest.raises(NativeExecutionUnsupported, match="installation_observation_unavailable"):
        observe(Path("data"))


@pytest.mark.parametrize("ending", [b"\n", b"\r\n"])
def test_marker_standard_newline(observed, ending):
    root, marker, _ = observed
    marker.write_bytes(str(UUID(int=1)).encode() + ending)
    assert observe(root).installation_id == UUID(int=1)


def test_aliased_marker_outside_root_rejected(observed, tmp_path):
    root, marker, _ = observed
    external = tmp_path / "outside"
    external.write_bytes(marker.read_bytes())
    marker.unlink()
    try:
        marker.symlink_to(external)
    except OSError:
        pytest.skip("Windows symlink privilege unavailable")
    with pytest.raises(NativeExecutionUnsupported, match="installation_observation_unavailable"):
        observe(root)


@pytest.mark.parametrize("device", ["cuda:0", "cuda:1", "gpu", "unknown", None, 0])
def test_device_unsupported_no_probe_or_cpu_fallback(observed, monkeypatch, device):
    root, _, _ = observed
    monkeypatch.setattr(subject, "_read_windows_facts", lambda: pytest.fail("must not probe"))
    with pytest.raises(NativeExecutionUnsupported, match="device_observation_unsupported"):
        subject.observe_windows_cpu(data_root=root, device=device)


def test_platform_unsupported_no_probe(observed, monkeypatch):
    root, _, _ = observed
    monkeypatch.setattr(subject.sys, "platform", "linux")
    monkeypatch.setattr(subject, "_read_windows_facts", lambda: pytest.fail("must not probe"))
    with pytest.raises(NativeExecutionUnsupported, match="platform_unsupported"):
        observe(root)


@pytest.mark.parametrize("guid,cpus", [(None, ()), (str(UUID(int=0)), ()), ("private-host", ()),
    (str(UUID(int=2)), ()), (str(UUID(int=2)), (("", "cpu", "vendor"),)),
    (str(UUID(int=2)), (("id", "cpu", 1),)), (str(UUID(int=2)), (("id", "cpu", "v"),) * 257)])
def test_invalid_facts_no_label_fallback(observed, guid, cpus):
    root, _, facts = observed
    facts[:] = [guid, cpus]
    with pytest.raises(NativeExecutionUnsupported, match="machine_observation_unavailable"):
        observe(root)


def test_private_probe_failure(observed, monkeypatch):
    root, _, _ = observed
    def fail():
        raise PermissionError("private-host registry data")
    monkeypatch.setattr(subject, "_read_windows_facts", fail)
    with pytest.raises(NativeExecutionUnsupported) as error:
        observe(root)
    assert str(error.value) == "execution_machine_observation_unavailable"


class FakeKey:
    def __init__(self, name):
        self.name = name
    def __enter__(self):
        return self
    def __exit__(self, *_):
        pass


def fake_registry(monkeypatch, *, names=("1", "0"), enumeration_error=259, bad_kind=False):
    calls = []
    def open_key(parent, name, reserved, access):
        calls.append((name, reserved, access))
        return FakeKey(name)
    def query(key, field):
        return (str(UUID(int=2)) if field == "MachineGuid" else "CPU " + field, 99 if bad_kind else 1)
    def enum(key, index):
        if index < len(names):
            return names[index]
        error = OSError("private registry error")
        error.winerror = enumeration_error
        raise error
    module = SimpleNamespace(KEY_READ=1, KEY_WOW64_64KEY=256, REG_SZ=1,
        HKEY_LOCAL_MACHINE="HKLM", OpenKey=open_key, QueryValueEx=query, EnumKey=enum)
    monkeypatch.setitem(subject.sys.modules, "winreg", module)
    return calls


def test_registry_recipe_enumerates_actual_cpu_keys(monkeypatch):
    calls = fake_registry(monkeypatch)
    guid, cpus = subject._read_windows_facts()
    assert guid == str(UUID(int=2))
    assert cpus == (("CPU Identifier", "CPU ProcessorNameString", "CPU VendorIdentifier"),) * 2
    assert [name for name, _, _ in calls][-2:] == ["0", "1"]
    assert all(reserved == 0 and access == 257 for _, reserved, access in calls)


@pytest.mark.parametrize("options", [{"names": ()}, {"names": ("0", "2")},
    {"names": ("0", "0")}, {"names": ("label",)}, {"names": tuple(str(i) for i in range(257))},
    {"enumeration_error": 5}, {"bad_kind": True}])
def test_registry_missing_incomplete_or_wrong_type_stops(monkeypatch, options):
    fake_registry(monkeypatch, **options)
    with pytest.raises((ValueError, OSError)):
        subject._read_windows_facts()
