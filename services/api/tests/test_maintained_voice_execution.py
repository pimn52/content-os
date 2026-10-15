"""Owned ZIP/PCM fixtures and mocked child only; no model or real installation."""
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
from types import SimpleNamespace
import wave
from zipfile import ZipFile

import pytest
from pydantic import TypeAdapter, ValidationError

from app.domain.models import ExecutionSpecificationRecord, ExecutionSpecificationV5, ExecutionSpecificationV6
from app.execution_scope import WorkerExecutionUseSnapshotV2
from app.providers.maintained_voice import MaintainedVoiceParameters
from app.providers.maintained_voice_bundle import bundle_bytes, BUNDLE_SLOT, MAIN_SOURCE
from app.providers.maintained_voice_child import MaintainedVoiceChildRequest, main, request_bytes
from app.providers.maintained_voice_execution import observe_child_inventory, prepare_maintained_voice_child
from app.providers.omnivoice_entry_v2 import LocalCloneInputV2
from app.providers.prepared import ExecutionPreparationError
from test_maintained_runtime import candidate, reference


@pytest.fixture
def child_case(candidate):
    options = dict(candidate["options"], max_bytes=16*1024**2, parameters=MaintainedVoiceParameters(seed=7).model_dump())
    data = options["data_root"]
    ref = data / "reference.wav"
    with wave.open(str(ref), "wb") as audio:
        audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        audio.writeframes(struct.pack("<4h", 1000, -1000, 2000, -2000))
    inputs = LocalCloneInputV2(text="synthetic bounded copy", reference_audio=str(ref),
        reference_text="synthetic reference", language="Chinese", output_path=str(data / "output.wav"),
        reference_sha256=hashlib.sha256(ref.read_bytes()).hexdigest(), reference_start_frame=1,
        reference_end_frame=3, reference_max_frames=4, reference_max_bytes=100)
    options.update(inputs=inputs, timeout_seconds=30)
    inventory = observe_child_inventory(options["environment"], options["base"], max_bytes=options["max_bytes"])
    origin = {key: value for key, value in candidate["origin"].items() if key != "diagnostic_scope"}
    origin.update(origin_version=2, generation_scope="voice_clone_only", accepts_generation_runtime_trust=True,
        runtime_inventory_sha256=inventory.descriptor.sha256)
    origin_path = data / "child-origin.json"
    origin_path.write_text(json.dumps(origin))
    options["origin_reference"] = reference(origin_path, data)
    return SimpleNamespace(options=options, inventory=inventory, origin=origin, origin_path=origin_path, candidate=candidate)


def prepare(case): return prepare_maintained_voice_child(**case.options)


def write_output(path, *, channels=1, rate=24000):
    with wave.open(str(path), "wb") as audio:
        audio.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
        audio.writeframes(struct.pack("<4h", 1000, -1000, 2000, -2000))


def test_zip_contains_complete_current_application_and_fixed_entry_without_imports():
    raw = bundle_bytes()
    assert raw == bundle_bytes()
    with ZipFile(BytesIO(raw)) as archive:
        assert archive.read("__main__.py") == MAIN_SOURCE
        root = Path(__file__).parents[1] / "app"
        names = {"app/" + path.relative_to(root).as_posix() for path in root.rglob("*.py")}
        assert set(archive.namelist()) == names | {"__main__.py"}
        for name in names:
            source = archive.read(name)
            assert source == (root / name.removeprefix("app/")).read_bytes()
            compile(source, name, "exec")


def test_real_base_python_zip_bootstrap_rejects_empty_request_before_runtime(tmp_path):
    # Baseline test Python, not the provider installation. Empty request cannot
    # reach the recipe or lazy vendor imports. A top-level import error would
    # produce stderr, so this checks that the owned packaged entry is reached.
    archive, request = tmp_path / "application.pyz", tmp_path / "request.json"
    archive.write_bytes(bundle_bytes())
    request.write_bytes(b"{}")
    result = subprocess.run([sys.executable, "-I", "-B", str(archive), str(request)], cwd=tmp_path,
        env={"SystemRoot": os.environ.get("SystemRoot", "C:\\Windows"), "PATH": str(Path(sys.executable).parent),
            "PYTHONPATH": "ignored-unobserved-path", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"},
        shell=False, capture_output=True, timeout=30, check=False)
    assert result.returncode == 1 and result.stdout == result.stderr == b""


def test_child_has_distinct_detached_identity_inputs_and_fixed_command(child_case, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "unobserved")
    monkeypatch.setenv("SECRET_FIXTURE_TOKEN", "must not inherit")
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("no child during prepare"))
    with prepare(child_case) as prepared:
        spec = prepared.specification
        assert type(spec) is ExecutionSpecificationV6
        assert TypeAdapter(ExecutionSpecificationRecord).validate_json(spec.model_dump_json()) == spec
        with pytest.raises(ValidationError): ExecutionSpecificationV5.model_validate_json(spec.model_dump_json())
        assert next(row for row in child_case.inventory.files if row.role == "entrypoint").name == BUNDLE_SLOT
        call = prepared.invocation
        assert call.argv[1:3] == ("-I", "-B") and len(call.argv) == 5
        assert Path(call.argv[3]) == call.cwd / "application.pyz"
        assert Path(call.argv[4]) == call.cwd / "request.json"
        env = dict(call.environment)
        assert "PYTHONPATH" not in env and "SECRET_FIXTURE_TOKEN" not in env
        assert env["HF_HUB_OFFLINE"] == env["TRANSFORMERS_OFFLINE"] == "1"
        assert Path(env["TEMP"]) == call.cwd and call.timeout_seconds == spec.timeout_seconds == 30
        snapshot = WorkerExecutionUseSnapshotV2.model_validate_json(json.dumps(prepared.execution_use_snapshot()))
        snapshot.execution_specification.parameters["seed"] = 999
        assert prepared.specification.parameters["seed"] == 7
        assert prepared.input_sha256 == hashlib.sha256(Path(call.argv[4]).read_bytes()).hexdigest()
        child_case.options["parameters"]["seed"] = 888
        assert prepared.verify() == spec


@pytest.mark.parametrize("change", ["bundle", "request", "reference", "runtime", "model", "origin", "application"])
def test_prelaunch_drift_consumes_child_and_never_launches(child_case, monkeypatch, change):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("invalid identity launched"))
    with prepare(child_case) as prepared:
        if change in ("bundle", "request"):
            (prepared.invocation.cwd / ("application.pyz" if change == "bundle" else "request.json")).write_bytes(b"changed")
        elif change == "reference": Path(child_case.options["inputs"].reference_audio).write_bytes(b"changed")
        elif change == "runtime": (child_case.options["environment"] / "Scripts/python.exe").write_bytes(b"changed")
        elif change == "model": (child_case.options["model_root"] / "model.safetensors").write_bytes(b"changed")
        elif change == "origin": child_case.origin_path.write_text("{}")
        else: monkeypatch.setattr("app.providers.maintained_voice_execution.bundle_bytes", lambda: b"changed application")
        with pytest.raises(ExecutionPreparationError, match="identity_changed"): prepared.execute()
        with pytest.raises(ExecutionPreparationError, match="consumed"): prepared.execute()


@pytest.mark.parametrize("mode", ["success", "nonzero", "missing", "stereo", "rate", "truncated", "timeout", "cancel"])
def test_single_use_child_output_and_process_recovery(child_case, monkeypatch, mode):
    launches, stops = [], []
    class Child:
        running = False
        def wait(self, timeout):
            assert timeout == 30
            target = Path(child_case.options["inputs"].output_path)
            if mode in ("timeout", "cancel"):
                self.running = True
                if mode == "cancel": raise KeyboardInterrupt()
                raise subprocess.TimeoutExpired("fixed", timeout)
            if mode == "nonzero": return 1
            if mode != "missing":
                write_output(target, channels=2 if mode == "stereo" else 1, rate=16000 if mode == "rate" else 24000)
                if mode == "truncated": target.write_bytes(target.read_bytes()[:-2])
            return 0
        def poll(self): return None if self.running else 0
    def launch(argv, **kwargs):
        launches.append((argv, kwargs))
        assert kwargs["shell"] is False and kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
        return Child()
    monkeypatch.setattr(subprocess, "Popen", launch)
    monkeypatch.setattr("app.providers.maintained_voice_execution.stop_maintained_process_tree", lambda child: stops.append(child))
    with prepare(child_case) as prepared:
        if mode == "success":
            assert prepared.execute() == Path(child_case.options["inputs"].output_path)
        elif mode == "cancel":
            with pytest.raises(KeyboardInterrupt): prepared.execute()
        else:
            with pytest.raises(ExecutionPreparationError): prepared.execute()
        assert len(launches) == 1
        assert len(stops) == (1 if mode in ("timeout", "cancel") else 0)
        with pytest.raises(ExecutionPreparationError, match="consumed"): prepared.execute()


@pytest.mark.parametrize("timeout", [True, 0, 3601, 1.5, "30"])
def test_timeout_has_no_implicit_cast(child_case, timeout):
    child_case.options["timeout_seconds"] = timeout
    with pytest.raises(ExecutionPreparationError, match="timeout_invalid"): prepare(child_case)


def test_oversized_text_rejects_before_packaging_or_launch(child_case, monkeypatch):
    child_case.options["inputs"] = child_case.options["inputs"].model_copy(update={"text": "x" * 65537})
    monkeypatch.setattr("app.providers.maintained_voice_execution._observe", lambda *a, **kw: pytest.fail("observe oversized input"))
    with pytest.raises(ExecutionPreparationError, match="preparation_invalid"): prepare(child_case)


@pytest.mark.parametrize("change", ["bundle", "request", "reference", "model"])
def test_postlaunch_drift_retains_unimported_output_and_consumes_attempt(child_case, monkeypatch, change):
    with prepare(child_case) as prepared:
        target = Path(child_case.options["inputs"].output_path)
        class Child:
            def wait(self, timeout):
                write_output(target)
                if change in ("bundle", "request"):
                    (prepared.invocation.cwd / ("application.pyz" if change == "bundle" else "request.json")).write_bytes(b"changed")
                elif change == "reference": Path(child_case.options["inputs"].reference_audio).write_bytes(b"changed")
                else: (child_case.options["model_root"] / "model.safetensors").write_bytes(b"changed")
                return 0
            def poll(self): return 0
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: Child())
        with pytest.raises(ExecutionPreparationError): prepared.execute()
        assert target.is_file()
        with pytest.raises(ExecutionPreparationError, match="consumed"): prepared.execute()


def test_launch_failure_is_sanitized_without_retry(child_case, monkeypatch):
    calls = []
    def launch(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("private creator text and token")
    monkeypatch.setattr(subprocess, "Popen", launch)
    with prepare(child_case) as prepared:
        with pytest.raises(ExecutionPreparationError, match="^maintained_voice_child_failed$"): prepared.execute()
        with pytest.raises(ExecutionPreparationError, match="consumed"): prepared.execute()
        assert calls == [1]


@pytest.mark.parametrize("problem", ["outside", "model", "existing", "hash", "old_origin"])
def test_invalid_input_or_diagnostic_origin_rejects_before_child(child_case, monkeypatch, problem):
    c = child_case
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("no child"))
    inputs = c.options["inputs"]
    if problem == "outside": inputs = inputs.model_copy(update={"output_path": str(c.options["data_root"].parent / "outside.wav")})
    elif problem == "model": inputs = inputs.model_copy(update={"output_path": str(c.options["model_root"] / "output.wav")})
    elif problem == "existing": Path(inputs.output_path).write_bytes(b"prior unimported attempt")
    elif problem == "hash": inputs = inputs.model_copy(update={"reference_sha256": "a"*64})
    else: c.options["origin_reference"] = c.candidate["options"]["origin_reference"]
    c.options["inputs"] = inputs
    with pytest.raises(ExecutionPreparationError): prepare(c)


@pytest.mark.parametrize("change", ["none", "extra_arg", "duplicate", "partial", "provider_failure"])
def test_fixed_child_request_parser_has_no_loader_or_command_override(child_case, monkeypatch, capsys, change):
    c = child_case
    with prepare(c) as prepared:
        call = prepared.invocation
        path = Path(call.argv[4])
        if change == "duplicate": path.write_bytes(path.read_bytes().replace(b'{', b'{"data_root":"ignored",', 1))
        elif change == "partial": path.write_text("{}")
        calls = []
        def recipe(**kwargs):
            calls.append(kwargs)
            if change == "provider_failure": raise RuntimeError("private fixture text")
        monkeypatch.setattr("app.providers.maintained_voice_child.execute_maintained_clone", recipe)
        with monkeypatch.context() as context:
            context.chdir(call.cwd)
            context.setattr("sys.argv", [call.argv[3], str(path)] + (["override"] if change == "extra_arg" else []))
            assert main() == (0 if change == "none" else 1)
        assert len(calls) == (1 if change in ("none", "provider_failure") else 0)
        assert capsys.readouterr().out == "" and capsys.readouterr().err == ""
