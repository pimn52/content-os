"""Synthetic byte/command evidence only, NOT native loader completeness."""
from pathlib import Path
import os
import stat
import subprocess
from uuid import UUID

import pytest

from app.domain.models import ExecutionMachineObservation
from app.providers.prepared import (ExecutionPreparationError, NativeExecutionUnsupported,
    PendingInvocation, SelectedArtifact, prepare_selected_files)
from app.providers.voice import OmniVoiceProvider
from app.providers.latentsync import LatentSyncProvider


@pytest.fixture
def preparation(tmp_path):
    selected = []
    for group, role, name in (("model", "weights", "weights.bin"),
                             ("runtime", "interpreter", "python.exe"),
                             ("runtime", "entrypoint", "entry.py"),
                             ("runtime", "dependency", "lib/dependency.py")):
        source = tmp_path / "original" / group / name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes((role + " selected bytes").encode())
        selected.append(SelectedArtifact(group, role, name, source))
    def builder(files, parameters):
        interpreter = files[("runtime", "python.exe")]
        return PendingInvocation((str(interpreter), str(files[("runtime", "entry.py")]),
            "--model", str(files[("model", "weights.bin")]), "--steps", str(parameters["steps"])),
            interpreter.parent.parent, (("DEVICE", str(parameters["device"])),), 10)
    return dict(capability="voice", provider="synthetic", model="synthetic-model", runtime="runtime-index",
        machine_id="machine-index", recipe="synthetic-selection-test", recipe_version=1,
        machine=ExecutionMachineObservation(installation_id=UUID(int=1), host_sha256="a"*64, device_sha256="b"*64),
        parameters={"device": "cpu", "steps": 32}, artifacts=tuple(selected),
        build_invocation=builder, staging_parent=tmp_path, max_bytes=4096)


def test_preparation_copies_actual_bytes_and_builds_same_invocation(preparation, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append((a, k)) or subprocess.CompletedProcess(a[0], 0))
    with prepare_selected_files(**preparation) as prepared:
        snapshot = prepared.execution_use_snapshot()
        assert calls == []  # no loading/inference/probing subprocess before reservation
        spec = prepared.specification
        original_digest = spec.execution_sha256
        assert snapshot["execution_sha256"] == spec.execution_sha256
        assert spec.parameters == {"device": "cpu", "steps": 32}
        assert [item.role for item in spec.model_artifacts] == ["weights"]
        frozen_weights = Path(prepared.invocation.argv[3])
        assert frozen_weights != preparation["artifacts"][0].source
        assert frozen_weights.read_bytes() == preparation["artifacts"][0].source.read_bytes()
        # Original installation and ambient env changes cannot redirect launch.
        preparation["artifacts"][0].source.write_bytes(b"changed original")
        preparation["parameters"]["steps"] = 1
        monkeypatch.setenv("DEVICE", "gpu")
        spec.parameters["steps"] = 999  # no shallow-frozen dict alias
        snapshot["execution_specification"]["parameters"]["device"] = "gpu"
        assert prepared.specification.parameters == {"device": "cpu", "steps": 32}
        assert prepared.execution_use_snapshot()["execution_sha256"] == original_digest
        prepared.execute()  # simulated; caller reservation wiring is NOT implemented
        args, options = calls[0]
        assert args[0] == list(prepared.invocation.argv)
        assert options["env"] == {"DEVICE": "cpu"}
        assert options["shell"] is False
        with pytest.raises(ExecutionPreparationError, match="consumed"):
            prepared.execute()
    assert not frozen_weights.exists()
    assert preparation["artifacts"][0].source.read_bytes() == b"changed original"


@pytest.mark.parametrize("index", range(4))
def test_all_frozen_artifacts_rehashed_even_if_size_and_mtime_unchanged(preparation, monkeypatch, index):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    with prepare_selected_files(**preparation) as prepared:
        selected = preparation["artifacts"][index]
        target = prepared.invocation.cwd / selected.group / selected.name
        previous = target.stat()
        target.chmod(stat.S_IREAD | stat.S_IWRITE)
        target.write_bytes(b"x" * previous.st_size)
        os.utime(target, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        with pytest.raises(ExecutionPreparationError, match="fixed_artifact_changed"):
            prepared.execute()
        assert calls == []
        with pytest.raises(ExecutionPreparationError, match="consumed"):
            prepared.execute()


def test_snapshot_detects_missing_fixed_file(preparation):
    with prepare_selected_files(**preparation) as prepared:
        target = Path(prepared.invocation.argv[1])
        target.chmod(stat.S_IREAD | stat.S_IWRITE)
        target.unlink()
        with pytest.raises(ExecutionPreparationError, match="fixed_artifact_changed"):
            prepared.execution_use_snapshot()


def test_closed_preparation_cannot_supply_a_snapshot(preparation):
    prepared = prepare_selected_files(**preparation)
    prepared.close()
    with pytest.raises(ExecutionPreparationError, match="consumed"):
        prepared.execution_use_snapshot()


def test_builder_side_effect_changing_copy_is_rejected(preparation):
    original = preparation["build_invocation"]
    def builder(files, params):
        target = files[("model", "weights.bin")]
        target.chmod(stat.S_IREAD | stat.S_IWRITE)
        target.write_bytes(b"builder changed bytes")
        return original(files, params)
    with pytest.raises(ExecutionPreparationError, match="fixed_artifact_changed"):
        prepare_selected_files(**{**preparation, "build_invocation": builder})
    assert list(preparation["staging_parent"].glob("content-os-prepared-*")) == []


def test_failed_verification_cannot_be_repaired_in_place(preparation):
    with prepare_selected_files(**preparation) as prepared:
        target = Path(prepared.invocation.argv[1])
        original = target.read_bytes()
        target.chmod(stat.S_IREAD | stat.S_IWRITE)
        target.write_bytes(b"invalid")
        with pytest.raises(ExecutionPreparationError, match="fixed_artifact_changed"):
            prepared.execution_use_snapshot()
        target.write_bytes(original)
        with pytest.raises(ExecutionPreparationError, match="consumed"):
            prepared.execution_use_snapshot()


def test_selected_symlink_is_not_treated_as_a_fixed_artifact(preparation, tmp_path):
    first = preparation["artifacts"][0]
    alias = tmp_path / "linked-weights"
    try:
        alias.symlink_to(first.source)
    except OSError:
        pytest.skip("symlink creation privilege unavailable")
    with pytest.raises(ExecutionPreparationError, match="artifact_unavailable"):
        prepare_selected_files(**{**preparation,
            "artifacts": (SelectedArtifact(first.group, first.role, first.name, alias),) + preparation["artifacts"][1:]})
    assert list(tmp_path.glob("content-os-prepared-*")) == []


def test_selected_path_location_and_manifest_order_do_not_change_digest(preparation, tmp_path):
    with prepare_selected_files(**preparation) as first:
        digest = first.specification.execution_sha256
    artifacts = []
    for artifact in reversed(preparation["artifacts"]):
        source = tmp_path / "relocated" / artifact.group / artifact.name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(artifact.source.read_bytes())
        artifacts.append(SelectedArtifact(artifact.group, artifact.role, artifact.name, source))
    with prepare_selected_files(**{**preparation, "artifacts": tuple(artifacts)}) as second:
        assert second.specification.execution_sha256 == digest


@pytest.mark.parametrize("index", range(4))
def test_changed_selected_bytes_change_snapshot(preparation, index):
    with prepare_selected_files(**preparation) as first:
        digest = first.specification.execution_sha256
    preparation["artifacts"][index].source.write_bytes(b"new selected bytes")
    with prepare_selected_files(**preparation) as second:
        assert second.specification.execution_sha256 != digest


@pytest.mark.parametrize("change", ["too_large", "empty", "duplicate", "parent", "absolute", "missing", "null", "nan", "numeric_key", "bad_env", "original_executable", "bad_timeout"])
def test_invalid_preparations_stop_and_clean_copies(preparation, change):
    modified = dict(preparation)
    if change == "too_large":
        modified["max_bytes"] = 1
    elif change == "empty":
        preparation["artifacts"][0].source.write_bytes(b"")
    elif change == "duplicate":
        modified["artifacts"] += (preparation["artifacts"][0],)
    elif change in ("parent", "absolute"):
        first = preparation["artifacts"][0]
        modified["artifacts"] = (SelectedArtifact(first.group, first.role, "../escape" if change == "parent" else "C:/escape", first.source),) + preparation["artifacts"][1:]
    elif change == "missing":
        preparation["artifacts"][0].source.unlink()
    elif change in ("null", "nan"):
        modified["parameters"] = {"steps": None if change == "null" else float("nan")}
    elif change == "numeric_key":
        modified["parameters"] = {1: 32}
    else:
        original = modified["build_invocation"]
        def bad_builder(files, params):
            result = original(files, params)
            return PendingInvocation(
                (str(preparation["artifacts"][1].source),) + result.argv[1:] if change == "original_executable" else result.argv,
                result.cwd, (("A", "1"), ("A", "2")) if change == "bad_env" else result.environment,
                float("inf") if change == "bad_timeout" else result.timeout_seconds)
        modified["build_invocation"] = bad_builder
    with pytest.raises(ExecutionPreparationError):
        prepare_selected_files(**modified)
    assert list(preparation["staging_parent"].glob("content-os-prepared-*")) == []


def test_builder_cannot_mutate_parameters_or_file_selection(preparation):
    original = preparation["build_invocation"]
    def builder(files, params):
        with pytest.raises(TypeError):
            params["steps"] = 1
        with pytest.raises(TypeError):
            files[("model", "weights.bin")] = Path("elsewhere")
        return original(files, params)
    with prepare_selected_files(**{**preparation, "build_invocation": builder}) as prepared:
        assert prepared.specification.parameters["steps"] == 32


@pytest.mark.parametrize("failure", ["timeout", "oserror"])
def test_launch_failure_is_private_safe_consumed_no_retry(preparation, monkeypatch, failure):
    calls = []
    def failing(*args, **kwargs):
        calls.append(args)
        if failure == "timeout":
            raise subprocess.TimeoutExpired("secret path", 10, output="secret output")
        raise OSError("secret path")
    monkeypatch.setattr(subprocess, "run", failing)
    with prepare_selected_files(**preparation) as prepared:
        with pytest.raises(ExecutionPreparationError, match="execution_prepared_") as caught:
            prepared.execute()
        assert "secret" not in str(caught.value)
        with pytest.raises(ExecutionPreparationError, match="consumed"):
            prepared.execute()
    assert len(calls) == 1


def test_native_adapters_do_not_promote_incomplete_or_opaque_loaders(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    voice = OmniVoiceProvider(synthesizer=lambda *args: calls.append(args))
    with pytest.raises(NativeExecutionUnsupported, match="opaque_callable"):
        voice.prepare_execution(object())
    executable = tmp_path / "python.exe"
    executable.write_bytes(b"fake runtime")
    checkpoint = tmp_path / "checkpoint.bin"
    checkpoint.write_bytes(b"fake weights")
    entry = tmp_path / "entry.py"
    entry.write_bytes(b"fake entry")
    config = tmp_path / "unet.yaml"
    config.write_bytes(b"fake config")
    for custom in (False, True):
        talking = LatentSyncProvider(executable, tmp_path, checkpoint, runner_path=entry,
            unet_config_path=config, command_runner=(lambda *args: calls.append(args)) if custom else None)
        with pytest.raises(NativeExecutionUnsupported, match="opaque_runner" if custom else "dependency_closure"):
            talking.prepare_execution(object())
    assert calls == []
