"""Fake runtime only; does not prove a closed native Python installation."""
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.domain.models import ExecutionMachineObservation
from app.providers import omnivoice_entry as entry
from app.providers.omnivoice_local import OmniVoiceLocalParameters, select_local_omnivoice
from app.providers.prepared import ExecutionPreparationError, SelectedArtifact, prepare_selected_files
from test_omnivoice_local import model_tree  # shared synthetic fixture


@pytest.fixture
def clone_input(tmp_path_factory):
    root = tmp_path_factory.mktemp("clone-input")
    reference = root / "reference.wav"
    reference.write_bytes(b"synthetic reference")
    return entry.LocalCloneInput(text="New topic text", reference_audio=str(reference),
        reference_text="Authorized transcript", language="Chinese", output_path=str(root / "output.wav"))


@pytest.fixture
def fake_runtime(monkeypatch):
    events = []
    class Tensor:
        def __init__(self, device="cpu", dtype="float16", *, meta=False, lazy=False):
            self.device, self.dtype, self.is_meta, self.lazy = device, dtype, meta, lazy
        def is_floating_point(self): return self.dtype in ("float16", "float32", "bfloat16")
        def is_complex(self): return self.dtype == "complex64"
    class Module(SimpleNamespace):
        def named_parameters(self, *, recurse, remove_duplicate):
            assert recurse is True and remove_duplicate is False
            return iter(self.parameters)
        def named_buffers(self, *, recurse, remove_duplicate):
            assert recurse is True and remove_duplicate is False
            return iter(self.buffers)
    model = Module(device="cpu", dtype="float16", sampling_rate=24000,
        parameters=[("llm.weight", Tensor()), ("audio_tokenizer.weight", Tensor())],
        buffers=[("codebook_layer_offsets", Tensor(dtype="int64"))])
    def generate(**kwargs):
        events.append(("generate", kwargs))
        return [[0.0, 0.1]]
    model.generate = generate
    def load(path, **kwargs):
        events.append(("load", path, kwargs))
        return model
    def write(output, samples, rate, **kwargs):
        events.append(("write", output.name, samples, rate, kwargs))
        output.write(b"synthetic output")
    torch = SimpleNamespace(float16="float16", bool="bool", uint8="uint8", int8="int8", int16="int16",
        int32="int32", int64="int64", is_tensor=lambda value: isinstance(value, Tensor),
        nn=SimpleNamespace(Module=Module, parameter=SimpleNamespace(is_lazy=lambda tensor: tensor.lazy)),
        manual_seed=lambda value: events.append(("torch_seed", value)),
        cuda=SimpleNamespace(is_available=lambda: True, manual_seed_all=lambda value: events.append(("cuda_seed", value))))
    runtime = SimpleNamespace(torch=torch, numpy=SimpleNamespace(random=SimpleNamespace(seed=lambda value: events.append(("numpy_seed", value)))),
        soundfile=SimpleNamespace(write=write), model_class=SimpleNamespace(from_pretrained=load))
    def factory():
        events.append(("runtime_import",))
        return runtime
    monkeypatch.setattr(entry, "_load_runtime", factory)
    monkeypatch.setattr(entry.random, "seed", lambda value: events.append(("python_seed", value)))
    for key, value in entry.OFFLINE_ENVIRONMENT:
        monkeypatch.setenv(key, value)
    return events, model, runtime


def test_fixed_model_builder_and_child_apply_exact_controls(model_tree, clone_input, fake_runtime, tmp_path_factory, monkeypatch):
    events, model, runtime = fake_runtime
    params = OmniVoiceLocalParameters(seed=17, num_step=48, speed=1.1)
    selected = select_local_omnivoice(model_tree, params)
    runtime_root = tmp_path_factory.mktemp("runtime-selected")
    artifacts = list(selected.artifacts)
    for name, role in (("python.exe", "interpreter"),
                       ("Lib/site-packages/app/providers/omnivoice_entry.py", "entrypoint"),
                       ("Lib/site-packages/dependency.py", "dependency")):
        source = runtime_root / name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"synthetic runtime bytes")
        artifacts.append(SelectedArtifact("runtime", role, name, source))
    prepared = prepare_selected_files(capability="voice", provider="omnivoice", model="synthetic",
        runtime="synthetic-runtime", machine_id="synthetic-host", recipe=selected.recipe, recipe_version=1,
        machine=ExecutionMachineObservation(installation_id=UUID(int=1), host_sha256="a"*64, device_sha256="b"*64),
        parameters=params.model_dump(), artifacts=tuple(artifacts), staging_parent=runtime_root,
        max_bytes=4096, build_invocation=lambda files, effective: entry.build_local_clone_invocation(
            files, effective, inputs=clone_input, timeout_seconds=10))
    with prepared:
        assert events == []  # preparation did not import a model/runtime
        argv = prepared.invocation.argv
        assert argv[1:4] == ("-I", "-m", "app.providers.omnivoice_entry")
        assert dict(prepared.invocation.environment) == dict(entry.OFFLINE_ENVIRONMENT)
        original_main = model_tree / "model.safetensors"
        original_main.write_bytes(b"changed original")
        prepared.verify_fixed_files()
        # Simulate the SAME prepared command with a fake child/runtime. Neither
        # a real subprocess nor a live dispatch is involved.
        child_runs = []
        def child(command, **kwargs):
            child_runs.append(tuple(command))
            assert kwargs["shell"] is False
            assert kwargs["env"] == dict(entry.OFFLINE_ENVIRONMENT)
            entry.execute_local_clone(Path(command[5]), command[7], command[9])
            return subprocess.CompletedProcess(command, 0)
        monkeypatch.setattr(subprocess, "run", child)
        assert prepared.execute().returncode == 0
        assert child_runs == [argv]
        output = Path(clone_input.output_path)
        assert output == Path(clone_input.output_path)
        with pytest.raises(ExecutionPreparationError, match="consumed"):
            prepared.execute()
        assert [event[0] for event in events] == ["runtime_import", "python_seed", "numpy_seed", "torch_seed", "load", "generate", "write"]
        assert events[4][1] == argv[5] and events[4][1] != str(model_tree)
        assert events[4][2] == {"local_files_only": True, "trust_remote_code": False,
            "load_asr": False, "device_map": "cpu", "dtype": "float16"}
        generated = events[5][1]
        assert all(generated[key] == value for key, value in params.generation_kwargs().items())
        assert generated["ref_text"] == clone_input.reference_text
        assert generated["generation_config"] is None
        assert generated["instruct"] is None and generated["duration"] is None
        assert events[-1][-1] == {"format": "WAV", "subtype": "PCM_16"}


@pytest.mark.parametrize("failure", ["missing_auxiliary", "remote_code", "missing_transcript", "missing_text",
    "missing_reference", "offline_changed", "existing_output", "bad_parameters", "output_format"])
def test_child_rechecks_input_and_config_before_runtime_import(model_tree, clone_input, fake_runtime, monkeypatch, failure):
    events, _, _ = fake_runtime
    values = clone_input.model_dump()
    params = OmniVoiceLocalParameters().model_dump()
    if failure == "missing_auxiliary":
        (model_tree / "audio_tokenizer/model.safetensors").unlink()
    elif failure == "remote_code":
        (model_tree / "config.json").write_text('{"model_type":"omnivoice","auto_map":{}}', encoding="utf-8")
    elif failure == "missing_transcript":
        values["reference_text"] = " "
    elif failure == "missing_text":
        values["text"] = " "
    elif failure == "missing_reference":
        Path(values["reference_audio"]).unlink()
    elif failure == "offline_changed":
        monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    elif failure == "existing_output":
        Path(values["output_path"]).write_bytes(b"retained output")
    elif failure == "bad_parameters":
        params["device"] = "auto"
    elif failure == "output_format":
        values["output_path"] = str(Path(values["output_path"]).with_suffix(".mp3"))
    with pytest.raises(ExecutionPreparationError):
        entry.execute_local_clone(model_tree, json.dumps(params), json.dumps(values))
    assert events == []
    if failure == "existing_output":
        assert Path(values["output_path"]).read_bytes() == b"retained output"


def test_cuda_seed_and_no_automatic_device_fallback(model_tree, clone_input, fake_runtime):
    events, model, runtime = fake_runtime
    params = OmniVoiceLocalParameters(device="cuda:0", seed=9)
    runtime.torch.cuda.is_available = lambda: False
    with pytest.raises(ExecutionPreparationError, match="device_unavailable"):
        entry.execute_local_clone(model_tree, params.model_dump_json(), clone_input.model_dump_json())
    assert events == [("runtime_import",)]
    events.clear()
    runtime.torch.cuda.is_available = lambda: True
    model.device = "cuda:0"
    for _, tensor in model.parameters + model.buffers:
        tensor.device = "cuda:0"
    entry.execute_local_clone(model_tree, params.model_dump_json(), clone_input.model_dump_json())
    assert ("cuda_seed", 9) in events
    load = next(event for event in events if event[0] == "load")
    assert load[2]["device_map"] == "cuda:0"


@pytest.mark.parametrize("change", ["device", "precision"])
def test_loaded_configuration_mismatch_stops_generation(model_tree, clone_input, fake_runtime, change):
    events, model, _ = fake_runtime
    if change == "device":
        model.device = "cuda:0"
    else:
        model.dtype = "float32"
    with pytest.raises(ExecutionPreparationError, match="loaded_configuration_changed"):
        entry.execute_local_clone(model_tree, OmniVoiceLocalParameters().model_dump_json(), clone_input.model_dump_json())
    assert not any(event[0] in ("generate", "write") for event in events)


def test_runtime_failure_is_private_safe_no_retry(model_tree, clone_input, fake_runtime):
    events, _, runtime = fake_runtime
    def fail(*args, **kwargs):
        events.append(("load_failed",))
        raise RuntimeError("secret prompt and installed path")
    runtime.model_class.from_pretrained = fail
    with pytest.raises(ExecutionPreparationError, match="local_clone_failed") as caught:
        entry.execute_local_clone(model_tree, OmniVoiceLocalParameters().model_dump_json(), clone_input.model_dump_json())
    assert "secret" not in str(caught.value)
    assert len([event for event in events if event[0] == "load_failed"]) == 1


def test_output_created_during_generation_is_not_overwritten(model_tree, clone_input, fake_runtime):
    events, model, _ = fake_runtime
    target = Path(clone_input.output_path)
    def racing_generate(**kwargs):
        target.write_bytes(b"another existing output")
        return [[0.0]]
    model.generate = racing_generate
    with pytest.raises(ExecutionPreparationError, match="output_conflict"):
        entry.execute_local_clone(model_tree, OmniVoiceLocalParameters().model_dump_json(), clone_input.model_dump_json())
    assert target.read_bytes() == b"another existing output"
    assert not any(event[0] == "write" for event in events)


def test_partial_write_failure_is_not_successful_output(model_tree, clone_input, fake_runtime):
    _, _, runtime = fake_runtime
    def fail(output, *args, **kwargs):
        output.write(b"partial")
        raise RuntimeError("private write failure")
    runtime.soundfile.write = fail
    with pytest.raises(ExecutionPreparationError, match="local_clone_failed"):
        entry.execute_local_clone(model_tree, OmniVoiceLocalParameters().model_dump_json(), clone_input.model_dump_json())
    assert Path(clone_input.output_path).read_bytes() == b"partial"


@pytest.mark.parametrize("change", ["aux_device", "aux_precision", "buffer_device", "buffer_precision",
    "meta", "lazy", "complex", "integer_parameter", "unknown_buffer", "no_parameters",
    "duplicate", "parameter_buffer_collision", "malformed", "not_tensor", "empty_name",
    "oversized_name", "non_string_name", "missing_enumerator", "enumerator_failure", "limit"])
def test_registered_state_mismatch_stops_before_generate_or_write(
        model_tree, clone_input, fake_runtime, change):
    events, model, _ = fake_runtime
    tensor = model.parameters[1][1]
    if change == "aux_device": tensor.device = "cuda:1"
    elif change == "aux_precision": tensor.dtype = "float32"
    elif change == "buffer_device": model.buffers[0][1].device = "cuda:0"
    elif change == "buffer_precision": model.buffers[0][1].dtype = "bfloat16"
    elif change == "meta": tensor.is_meta = True
    elif change == "lazy": tensor.lazy = True
    elif change == "complex": tensor.dtype = "complex64"
    elif change == "integer_parameter": tensor.dtype = "int64"
    elif change == "unknown_buffer": model.buffers[0][1].dtype = "uint16"
    elif change == "no_parameters": model.parameters = []
    elif change == "duplicate": model.parameters.append(model.parameters[0])
    elif change == "parameter_buffer_collision": model.buffers[0] = ("llm.weight", model.buffers[0][1])
    elif change == "malformed": model.parameters = [("llm.weight",)]
    elif change == "not_tensor": model.parameters = [("llm.weight", object())]
    elif change == "empty_name": model.parameters = [("", tensor)]
    elif change == "oversized_name": model.parameters = [("x" * 1025, tensor)]
    elif change == "non_string_name": model.parameters = [(42, tensor)]
    elif change == "missing_enumerator": model.named_buffers = None
    elif change == "enumerator_failure":
        def broken(**kwargs):
            yield ("llm.weight", tensor)
            raise RuntimeError("private tensor name and source path")
        model.named_parameters = broken
    elif change == "limit":
        model.named_parameters = lambda **kwargs: ((f"weight.{n}", tensor) for n in range(100_001))
    with pytest.raises(ExecutionPreparationError, match="omnivoice_loaded_configuration") as caught:
        entry.execute_local_clone(model_tree, OmniVoiceLocalParameters().model_dump_json(), clone_input.model_dump_json())
    assert "private" not in str(caught.value)
    assert not any(event[0] in ("generate", "write") for event in events)
    assert not Path(clone_input.output_path).exists()
    assert len([event for event in events if event[0] == "load"]) == 1


@pytest.mark.parametrize("dtype", ["bool", "uint8", "int8", "int16", "int32", "int64", "float16"])
def test_tied_parameters_and_supported_buffers_keep_all_device_checks(
        model_tree, clone_input, fake_runtime, dtype):
    events, model, _ = fake_runtime
    model.parameters[1] = ("audio_tokenizer.weight", model.parameters[0][1])
    model.buffers[0][1].dtype = dtype
    entry.execute_local_clone(model_tree, OmniVoiceLocalParameters().model_dump_json(), clone_input.model_dump_json())
    assert [event[0] for event in events].count("generate") == 1


def test_buffer_traversal_failure_after_valid_parameters_is_not_ignored(
        model_tree, clone_input, fake_runtime):
    events, model, _ = fake_runtime
    def broken(**kwargs):
        yield model.buffers[0]
        raise RuntimeError("private source")
    model.named_buffers = broken
    with pytest.raises(ExecutionPreparationError, match="configuration_unavailable"):
        entry.execute_local_clone(model_tree, OmniVoiceLocalParameters().model_dump_json(), clone_input.model_dump_json())
    assert not any(event[0] == "generate" for event in events)
