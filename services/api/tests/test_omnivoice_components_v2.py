"""Synthetic files and fake registered modules; no model/native execution."""
import hashlib
import json
from pathlib import Path
import struct
import sys
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.providers import omnivoice_entry_v2 as entry
from app.providers.omnivoice_entry import require_loaded_configuration
from app.providers.omnivoice_local import OmniVoiceLocalParameters
from app.providers.omnivoice_recipe_v2 import (OmniVoiceLocalParametersV2,
    parse_local_parameters, select_local_omnivoice_v2)
from app.providers.omnivoice_state_v2 import require_component_state
from app.providers.prepared import ExecutionPreparationError, NativeExecutionUnsupported
from app.providers.voice import OmniVoiceProvider
from test_omnivoice_local import model_tree
from test_omnivoice_prepared import ready, raw_spec
from test_component_policy2 import policy2


def parameters(**updates):
    return OmniVoiceLocalParametersV2(**(dict(parameter_version=2, device="cpu", precision="float16",
        audio_tokenizer_precision="float32", rotary_buffer_precision="float32",
        attention_backend="eager") | updates))


@pytest.fixture
def v2_tree(model_tree):
    config = dict(model_type="omnivoice", llm_config=dict(model_type="qwen3",
        rope_parameters=dict(rope_type="default", rope_theta=10000.0)))
    (model_tree / "config.json").write_text(json.dumps(config), encoding="utf-8")
    return model_tree


@pytest.fixture
def inputs(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("pcm-input")
    pcm = struct.pack("<hhhh", 0, 1024, -2048, 32767)
    raw = struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36+len(pcm), b"WAVE", b"fmt ",
        16, 1, 1, 24000, 48000, 2, 16, b"data", len(pcm)) + pcm
    path = tmp_path / "reference.wav"
    path.write_bytes(raw)
    return entry.LocalCloneInputV2(text="new words", reference_audio=str(path.resolve()),
        reference_text="reference transcript", language="Chinese", output_path=str((tmp_path/"out.wav").resolve()),
        reference_sha256=hashlib.sha256(raw).hexdigest(), reference_start_frame=1,
        reference_end_frame=4, reference_max_bytes=1000, reference_max_frames=10)


class Tensor:
    def __init__(self, dtype="float16", device="cpu"):
        self.dtype, self.device = dtype, device
        self.is_meta = self.lazy = self.is_quantized = False
    def is_floating_point(self): return self.dtype in ("float16", "float32")
    def is_complex(self): return self.dtype == "complex64"


class Config(SimpleNamespace):
    def to_dict(self): return {k: v for k, v in vars(self).items() if not k.startswith("_")}


class Module:
    def __init__(self):
        self._parameters, self._buffers, self._modules = {}, {}, {}
        self._non_persistent_buffers_set = set()
        self.training = True
    def __setattr__(self, name, value):
        if "_modules" in vars(self) and isinstance(value, Module): self._modules[name] = value
        if "_buffers" in vars(self) and name in self._buffers: self._buffers[name] = value
        object.__setattr__(self, name, value)
    def named_modules(self, remove_duplicate=False, prefix=""):
        yield prefix, self
        for name, module in self._modules.items():
            yield from module.named_modules(remove_duplicate=False, prefix=(prefix+"."+name).lstrip("."))
    def named_parameters(self, recurse=True, remove_duplicate=False):
        for owner, module in self.named_modules():
            for name, value in module._parameters.items(): yield (owner+"."+name).lstrip("."), value
    def named_buffers(self, recurse=True, remove_duplicate=False):
        for owner, module in self.named_modules():
            for name, value in module._buffers.items(): yield (owner+"."+name).lstrip("."), value
    def get_submodule(self, path):
        module = self
        for name in path.split("."): module = module._modules[name]
        return module
    def eval(self):
        for _, module in self.named_modules(): module.training = False
        return self


@pytest.fixture
def runtime(monkeypatch):
    events = []
    config = Config(model_type="qwen3", rope_parameters=dict(rope_type="default", rope_theta=10000.0),
        _attn_implementation="eager")
    class Rotary(Module):
        def __init__(self, config, device="cpu"):
            super().__init__()
            self.config = config
            self._buffers = {"inv_freq": Tensor("float32"), "original_inv_freq": Tensor("float32")}
            self._non_persistent_buffers_set = set(self._buffers)
            self.inv_freq, self.original_inv_freq = self._buffers.values()
            self.attention_scaling = 1.0
            events.append(("rotary_init",))
    class LLM(Module):
        def __init__(self):
            super().__init__()
            self.config, self.rotary_emb = config, Rotary(config)
            self._parameters["weight"] = Tensor()
    class Auxiliary(Module):
        def __init__(self):
            super().__init__()
            self.config = Config(_attn_implementation="eager")
            self._parameters["weight"] = Tensor("float32")
        @classmethod
        def from_pretrained(cls, path, **kwargs):
            events.append(("aux_load", path, kwargs))
            return cls()
    class Model(Module):
        def __init__(self):
            super().__init__()
            self.config = Config(model_type="omnivoice", _attn_implementation="eager")
            self.device, self.dtype, self.llm = "cpu", "float16", LLM()
            self._parameters["weight"] = Tensor()
            self._buffers["codebook_layer_offsets"] = Tensor("int64")
            self._asr_pipe = None
        @classmethod
        def from_pretrained(cls, path, **kwargs):
            events.append(("main_load", path, kwargs))
            return cls()
        def generate(self, **kwargs):
            events.append(("generate", kwargs))
            return [[0.1]]
    class Text:
        @classmethod
        def from_pretrained(cls, path, **kwargs):
            events.append(("text_load", path, kwargs))
            return cls()
    class Feature:
        sampling_rate = 24000
        @classmethod
        def from_pretrained(cls, path, **kwargs):
            events.append(("feature_load", path, kwargs))
            return cls()
    def write(output, *args, **kwargs):
        events.append(("write", kwargs))
        output.write(b"synthetic unadmitted output")
    torch = SimpleNamespace(float16="float16", float32="float32", bool="bool", uint8="uint8",
        int8="int8", int16="int16", int32="int32", int64="int64",
        nn=SimpleNamespace(Module=Module, parameter=SimpleNamespace(is_lazy=lambda t: t.lazy)),
        is_tensor=lambda t: isinstance(t, Tensor), manual_seed=lambda seed: events.append(("seed", seed)))
    value = SimpleNamespace(model_class=Model, audio_class=Auxiliary, llm_class=LLM, rotary_class=Rotary,
        text_class=Text, feature_class=Feature, duration_class=SimpleNamespace, torch=torch,
        numpy=SimpleNamespace(float32="float32", asarray=lambda data, dtype: list(data),
            random=SimpleNamespace(seed=lambda seed: None)), soundfile=SimpleNamespace(write=write), events=events)
    monkeypatch.setattr(entry, "_load_runtime", lambda: value)
    for key, val in entry.OFFLINE_ENVIRONMENT: monkeypatch.setenv(key, val)
    return value


def test_version_identity_controls_and_old_parser(v2_tree):
    before = set(sys.modules)
    params = parameters(seed=12)
    selected = select_local_omnivoice_v2(v2_tree, params)
    assert selected.recipe_version == 2 and selected.parameters == params
    assert not isinstance(params, OmniVoiceLocalParameters)
    assert params.generation_kwargs() == OmniVoiceLocalParameters(seed=12).generation_kwargs()
    assert type(parse_local_parameters(params.model_dump())) is OmniVoiceLocalParametersV2
    old = OmniVoiceLocalParameters().model_dump_json()
    assert parse_local_parameters(json.loads(old)).model_dump_json() == old
    with pytest.raises(ValidationError): OmniVoiceLocalParameters.model_validate(params.model_dump())
    with pytest.raises(NativeExecutionUnsupported): selected.require_native_preparation()
    assert not any(n.split('.')[0] in ('torch', 'transformers', 'omnivoice') for n in set(sys.modules)-before)


@pytest.mark.parametrize("change", [dict(parameter_version=1), dict(parameter_version=2.0),
    dict(device="cuda:0"), dict(audio_tokenizer_precision="float16"), dict(attention_backend="auto"),
    dict(seed=True), dict(speed=float("nan"))])
def test_parameters_reject_changed_contract(change):
    with pytest.raises(ValidationError): parameters(**change)


def test_precision_rules_accept_auxiliary_but_old_entry_rejects(v2_tree, runtime):
    model = entry.load_components(runtime, select_local_omnivoice_v2(v2_tree, parameters()))
    require_component_state(model, runtime, sampling_rate=24000)
    with pytest.raises(ExecutionPreparationError):
        require_loaded_configuration(model, runtime.torch, OmniVoiceLocalParameters())


@pytest.mark.parametrize("change", ["main_fp32", "aux_fp16", "unknown_fp32", "persistent_rotary",
    "rotary_parameter", "missing_buffer", "cross_alias", "foreign_owner", "device", "meta", "lazy",
    "quantized", "complex", "asr", "attention", "rate", "training", "empty_aux", "rotary_class"])
def test_registered_ownership_and_state_fail_closed(v2_tree, runtime, change):
    model = entry.load_components(runtime, select_local_omnivoice_v2(v2_tree, parameters()))
    if change == "main_fp32": model._parameters["weight"].dtype = "float32"
    elif change == "aux_fp16": model.audio_tokenizer._parameters["weight"].dtype = "float16"
    elif change == "unknown_fp32": model._buffers["unknown"] = Tensor("float32")
    elif change == "persistent_rotary": model.llm.rotary_emb._non_persistent_buffers_set.clear()
    elif change == "rotary_parameter": model.llm.rotary_emb._parameters["inv_freq"] = Tensor("float32")
    elif change == "missing_buffer": del model.llm.rotary_emb._buffers["inv_freq"]
    elif change == "cross_alias": model._buffers["aux_alias"] = model.audio_tokenizer._parameters["weight"]
    elif change == "foreign_owner": model.foreign = model.audio_tokenizer
    elif change == "device": model._parameters["weight"].device = "cuda:0"
    elif change in ("meta", "lazy", "quantized"):
        setattr(model._parameters["weight"], {"meta": "is_meta", "lazy": "lazy", "quantized": "is_quantized"}[change], True)
    elif change == "complex": model._parameters["weight"].dtype = "complex64"
    elif change == "asr": model._asr_pipe = object()
    elif change == "attention": model.llm.config._attn_implementation = "sdpa"
    elif change == "rate": model.sampling_rate = 16000
    elif change == "training": model.training = True
    elif change == "empty_aux": model.audio_tokenizer._parameters.clear()
    elif change == "rotary_class": model.llm.rotary_emb.__class__ = Module
    with pytest.raises(ExecutionPreparationError, match="configuration"):
        require_component_state(model, runtime, sampling_rate=24000)


def test_same_role_tied_weights_supported(v2_tree, runtime):
    model = entry.load_components(runtime, select_local_omnivoice_v2(v2_tree, parameters()))
    model.llm._parameters["alias"] = model.llm._parameters["weight"]
    require_component_state(model, runtime, sampling_rate=24000)


def test_reference_hash_frames_and_tuple_generation(v2_tree, inputs, runtime):
    ref = entry.read_reference(inputs, 24000)
    assert list(ref.samples) == pytest.approx([1024/32768, -2048/32768, 32767/32768])
    assert ref.total_frames == 4
    output = entry.execute_local_clone_v2(v2_tree, parameters().model_dump_json(), inputs.model_dump_json())
    assert output.exists()
    events = runtime.events
    main = next(e for e in events if e[0] == "main_load")[2]
    assert main == dict(train=True, load_asr=False, dtype="float16", device_map="cpu",
        attn_implementation="eager", local_files_only=True, trust_remote_code=False)
    aux = next(e for e in events if e[0] == "aux_load")[2]
    assert aux == dict(dtype="float32", device_map="cpu", attn_implementation="eager",
        local_files_only=True, trust_remote_code=False)
    for name in ("text_load", "feature_load"):
        assert next(e for e in events if e[0] == name)[2] == dict(local_files_only=True, trust_remote_code=False)
    generated = next(e for e in events if e[0] == "generate")[1]
    assert type(generated["ref_audio"]) is tuple and generated["ref_audio"][1] == 24000
    assert "parameter_version" not in generated and "attention_backend" not in generated
    assert events[-1] == ("write", dict(format="WAV", subtype="PCM_16"))


@pytest.mark.parametrize("change", ["hash", "rate", "truncated", "frames", "bytes", "header", "silent", "interval"])
def test_bad_reference_stops_before_import(v2_tree, inputs, monkeypatch, change):
    values = inputs.model_dump()
    path = Path(inputs.reference_audio)
    raw = bytearray(path.read_bytes())
    if change == "hash": values["reference_sha256"] = "0"*64
    elif change == "rate": struct.pack_into("<I", raw, 24, 16000)
    elif change == "truncated": raw = raw[:-1]
    elif change == "frames": values["reference_max_frames"] = 3
    elif change == "bytes": values["reference_max_bytes"] = 45
    elif change == "header": raw[12:16] = b"JUNK"
    elif change == "silent": raw[44:] = bytes(len(raw)-44)
    elif change == "interval": values["reference_end_frame"] = 5
    if change in ("rate", "truncated", "header", "silent"):
        path.write_bytes(raw)
        values["reference_sha256"] = hashlib.sha256(raw).hexdigest()
    for key, value in entry.OFFLINE_ENVIRONMENT: monkeypatch.setenv(key, value)
    monkeypatch.setattr(entry, "_load_runtime", lambda: pytest.fail("invalid input must precede import"))
    with pytest.raises(ExecutionPreparationError):
        entry.execute_local_clone_v2(v2_tree, parameters().model_dump_json(), json.dumps(values))


def test_prepared_v2_identity_and_changed_config(ready, monkeypatch):
    from app.providers import omnivoice_prepared
    # Synthetic runtime has no vendor package; source acceptance is tested
    # separately. This replacement cannot give production runtime authority.
    monkeypatch.setattr(omnivoice_prepared, "require_recipe_sources", lambda root: None)
    config = dict(model_type="omnivoice", llm_config=dict(model_type="qwen3", rope_parameters=dict(rope_type="default")))
    (ready.model/"config.json").write_text(json.dumps(config), encoding="utf-8")
    with ready.prepare(parameters=parameters()) as prepared:
        spec = prepared.specification
        assert spec.schema_version == 3 and spec.observation_version == 2
        assert spec.observation_recipe == "omnivoice-local-cpu-components"
        first = prepared.execution_use_snapshot()
        (prepared.model_root/"config.json").write_text(json.dumps(config | dict(extra=True)), encoding="utf-8")
        with pytest.raises(ExecutionPreparationError): prepared.execution_use_snapshot()
        assert first["execution_sha256"] == spec.execution_sha256


def test_source_change_rejects_before_runtime_import(tmp_path):
    with pytest.raises(ExecutionPreparationError, match="source_changed"):
        entry.require_recipe_sources(tmp_path)


def test_invocation_uses_explicit_version_and_requires_owned_entry(v2_tree, inputs, runtime, tmp_path_factory):
    selected = select_local_omnivoice_v2(v2_tree, parameters())
    files = {("model", a.name): a.source for a in selected.artifacts}
    root = tmp_path_factory.mktemp("v2-command")
    files[("runtime", "python.exe")] = root / "python.exe"
    for name in ("omnivoice_entry_v2.py", "omnivoice_recipe_v2.py", "omnivoice_state_v2.py"):
        files[("runtime", "Lib/site-packages/app/providers/" + name)] = root / name
    before = list(runtime.events)
    command = entry.build_local_clone_invocation_v2(files, parameters().model_dump(), inputs=inputs, timeout_seconds=10)
    assert command.argv[1:4] == ("-I", "-m", "app.providers.omnivoice_entry_v2")
    assert runtime.events == before
    assert dict(command.environment) == dict(entry.OFFLINE_ENVIRONMENT)
    del files[("runtime", "Lib/site-packages/app/providers/omnivoice_state_v2.py")]
    with pytest.raises(ExecutionPreparationError):
        entry.build_local_clone_invocation_v2(files, parameters().model_dump(), inputs=inputs, timeout_seconds=10)


def test_adapter_keeps_v1_selection_explicit(v2_tree):
    adapter = OmniVoiceProvider(model=str(v2_tree))
    assert adapter.select_local_execution_model_v2(parameters()).recipe_version == 2
    with pytest.raises(ExecutionPreparationError): adapter.select_local_execution_model(parameters())
    with pytest.raises(ExecutionPreparationError): adapter.select_local_execution_model_v2(OmniVoiceLocalParameters())


@pytest.mark.parametrize("change", ["dynamic_rope", "other_llm", "invalid_rate", "malformed_llm"])
def test_configuration_changes_stop_during_selection(v2_tree, change):
    path = v2_tree / "config.json"
    config = json.loads(path.read_text())
    if change == "dynamic_rope": config["llm_config"]["rope_parameters"]["rope_type"] = "dynamic"
    elif change == "other_llm": config["llm_config"]["model_type"] = "qwen2"
    elif change == "malformed_llm": config["llm_config"] = []
    elif change == "invalid_rate":
        (v2_tree/"audio_tokenizer/preprocessor_config.json").write_text('{"sampling_rate":true}')
    path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ExecutionPreparationError): select_local_omnivoice_v2(v2_tree, parameters())


def test_loaded_config_change_stops_before_generate_or_output(v2_tree, inputs, runtime, monkeypatch):
    original = runtime.model_class.from_pretrained
    def altered(*args, **kwargs):
        model = original(*args, **kwargs)
        model.llm.config.rope_parameters = dict(rope_type="default", rope_theta=20000.0)
        return model
    monkeypatch.setattr(runtime.model_class, "from_pretrained", altered)
    with pytest.raises(ExecutionPreparationError):
        entry.execute_local_clone_v2(v2_tree, parameters().model_dump_json(), inputs.model_dump_json())
    assert not any(e[0] in ("generate", "write", "aux_load") for e in runtime.events)
    assert not Path(inputs.output_path).exists()


def test_existing_and_racing_outputs_are_preserved(v2_tree, inputs, runtime):
    path = Path(inputs.output_path)
    path.write_bytes(b"existing output")
    with pytest.raises(ExecutionPreparationError, match="output_conflict"):
        entry.execute_local_clone_v2(v2_tree, parameters().model_dump_json(), inputs.model_dump_json())
    assert runtime.events == [] and path.read_bytes() == b"existing output"
    # Reuse a fresh output path; no deletion of existing artifacts.
    updated = entry.LocalCloneInputV2.model_validate(inputs.model_dump() | dict(output_path=str(path.with_name("race.wav"))))
    race = Path(updated.output_path)
    def racing_generate(self, **kwargs):
        race.write_bytes(b"concurrent output")
        return [[0.1]]
    runtime.model_class.generate = racing_generate
    with pytest.raises(ExecutionPreparationError, match="output_conflict"):
        entry.execute_local_clone_v2(v2_tree, parameters().model_dump_json(), updated.model_dump_json())
    assert race.read_bytes() == b"concurrent output"


def test_v1_and_v2_identity_do_not_share_digest(ready, monkeypatch):
    from app.providers import omnivoice_prepared
    monkeypatch.setattr(omnivoice_prepared, "require_recipe_sources", lambda root: None)
    config = dict(model_type="omnivoice", llm_config=dict(model_type="qwen3", rope_parameters=dict(rope_type="default")))
    (ready.model/"config.json").write_text(json.dumps(config), encoding="utf-8")
    with ready.prepare() as old, ready.prepare(parameters=parameters()) as new:
        assert old.specification.observation_version == 1
        assert "parameter_version" not in old.specification.parameters
        assert old.specification.execution_sha256 != new.specification.execution_sha256
        with pytest.raises(NativeExecutionUnsupported): new.require_native_preparation()


def test_v2_preparation_requires_audited_sources_before_staging(ready):
    before = set(ready.staging.iterdir())
    with pytest.raises(ExecutionPreparationError, match="source_changed"):
        ready.prepare(parameters=parameters())
    assert set(ready.staging.iterdir()) == before


def test_source_guard_checks_actual_bytes_and_rejects_changes(tmp_path, monkeypatch):
    path = tmp_path / "package/source.py"
    path.parent.mkdir()
    path.write_bytes(b"audited synthetic source")
    monkeypatch.setattr(entry, "SOURCE_HASHES", {"package/source.py": hashlib.sha256(path.read_bytes()).hexdigest()})
    entry.require_recipe_sources(tmp_path)
    path.write_bytes(b"changed source")
    with pytest.raises(ExecutionPreparationError, match="source_changed"):
        entry.require_recipe_sources(tmp_path)
