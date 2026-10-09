"""Offline selection/parameter evidence; no native loading or admission."""
import json
from pathlib import Path
import subprocess
import sys

import pytest
from pydantic import ValidationError

from app.providers.omnivoice_local import OmniVoiceLocalParameters, select_local_omnivoice
from app.providers.prepared import ExecutionPreparationError, NativeExecutionUnsupported
from app.providers.voice import OmniVoiceProvider


@pytest.fixture
def model_tree(tmp_path):
    contents = {"config.json": {"model_type": "omnivoice"},
        "tokenizer_config.json": {"tokenizer_class": "Qwen2TokenizerFast"},
        "audio_tokenizer/config.json": {"model_type": "higgs_audio_v2_tokenizer"},
        "audio_tokenizer/preprocessor_config.json": {"sampling_rate": 24000},
        "tokenizer.json": {"model": {}},
        "model.safetensors": b"synthetic main weights",
        "audio_tokenizer/model.safetensors": b"synthetic auxiliary weights"}
    for name, content in contents.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else json.dumps(content).encode())
    return tmp_path


def test_selects_both_models_configs_and_tokenizer_no_runtime(model_tree, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: pytest.fail("must not spawn runtime"))
    before = set(sys.modules)
    selected = select_local_omnivoice(model_tree)
    assert len(selected.artifacts) == 7
    assert {item.role for item in selected.artifacts} == {"weights", "auxiliary_weights", "configuration", "tokenizer"}
    assert selected.loader_kwargs == {"local_files_only": True, "trust_remote_code": False, "load_asr": False, "device_map": "cpu"}
    assert not any(name.split(".")[0] in {"torch", "omnivoice", "transformers"} for name in set(sys.modules) - before)
    with pytest.raises(NativeExecutionUnsupported, match="dependency_closure"):
        selected.require_native_preparation()


@pytest.mark.parametrize("name", ["config.json", "model.safetensors", "tokenizer.json", "tokenizer_config.json",
    "audio_tokenizer/config.json", "audio_tokenizer/model.safetensors", "audio_tokenizer/preprocessor_config.json"])
def test_missing_required_file_never_falls_back_to_cache_or_hub(model_tree, name, monkeypatch):
    (model_tree / name).unlink()
    monkeypatch.setenv("HF_HOME", str(model_tree / "pretend-cache"))
    with pytest.raises(ExecutionPreparationError, match="artifact_missing"):
        select_local_omnivoice(model_tree)


@pytest.mark.parametrize("name", ["pytorch_model.bin", "model.safetensors.index.json", "other.safetensors", "custom.py"])
def test_unsupported_extra_weight_or_code_layout_is_not_guessed(model_tree, name):
    (model_tree / name).write_bytes(b"unexpected file")
    with pytest.raises(NativeExecutionUnsupported, match="layout_unsupported"):
        select_local_omnivoice(model_tree)


@pytest.mark.parametrize("value", [{"auto_map": {"AutoTokenizer": "repo.custom"}},
    {"tokenizer_class": "CustomTokenizer"}, {"tokenizer_class": "Qwen2TokenizerFast", "tokenizer_file": "../external.json"}])
def test_remote_code_and_external_tokenizer_are_not_local_selection(model_tree, value):
    (model_tree / "tokenizer_config.json").write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(NativeExecutionUnsupported):
        select_local_omnivoice(model_tree)


@pytest.mark.parametrize("content", [b"[]", b'{"model_type":"omnivoice","model_type":"other"}', b'{"value": NaN}', b"{" + b" " * (1024*1024)],
    ids=["nonobject", "duplicate", "nonfinite", "size_limit"])
def test_malformed_duplicate_nonfinite_or_unbounded_config_stops(model_tree, content):
    (model_tree / "config.json").write_bytes(content)
    with pytest.raises(ExecutionPreparationError, match="configuration_invalid"):
        select_local_omnivoice(model_tree)


def test_hub_id_and_relative_path_are_not_local_model_selection():
    with pytest.raises(ExecutionPreparationError, match="local_model_required"):
        select_local_omnivoice(Path("k2-fsa/OmniVoice"))


def test_alias_cannot_supply_a_model_tree(model_tree, tmp_path_factory):
    alias = tmp_path_factory.mktemp("alias-parent") / "model-alias"
    try:
        alias.symlink_to(model_tree, target_is_directory=True)
    except OSError:
        pytest.skip("symlink privilege unavailable")
    with pytest.raises(ExecutionPreparationError, match="local_model_required"):
        select_local_omnivoice(alias)


@pytest.mark.parametrize("values", [{"device": "auto"}, {"device": "cuda"}, {"precision": "auto"},
    {"seed": None}, {"seed": True}, {"seed": -1}, {"num_step": "32"}, {"num_step": True},
    {"speed": float("nan")}, {"speed": float("inf")}, {"speed": 0.0}, {"denoise": "false"}, {"unknown": 1}])
def test_parameters_are_explicit_strict_and_finite(values):
    with pytest.raises(ValidationError):
        OmniVoiceLocalParameters(**values)


def test_parameters_detached_and_all_generation_defaults_expanded(model_tree, monkeypatch):
    selected = select_local_omnivoice(model_tree, OmniVoiceLocalParameters(num_step=48, speed=1.1, seed=7))
    monkeypatch.setenv("CONTENT_OS_OMNIVOICE_DEVICE", "cuda")
    values = selected.parameters.generation_kwargs()
    assert values == {"num_step": 48, "speed": 1.1, "guidance_scale": 2.0, "t_shift": 0.1,
        "layer_penalty_factor": 5.0, "position_temperature": 5.0, "class_temperature": 0.0,
        "denoise": True, "preprocess_prompt": True, "postprocess_output": True,
        "audio_chunk_duration": 15.0, "audio_chunk_threshold": 30.0, "pad_duration": 0.1,
        "fade_duration": 0.1, "normalize_text": False}
    values["num_step"] = 1
    assert selected.parameters.num_step == 48
    assert selected.parameters.device == "cpu"
    assert selected.parameters.seed == 7
    with pytest.raises(ValidationError):
        selected.parameters.seed = 999


def test_selection_is_not_fixed_byte_snapshot(model_tree):
    selected = select_local_omnivoice(model_tree)
    (model_tree / "model.safetensors").write_bytes(b"changed after selection")
    # Selection cannot attest or authorize these files; actual hashing/copying
    # and runtime observation must follow, even for valid model layouts.
    with pytest.raises(NativeExecutionUnsupported, match="dependency_closure"):
        selected.require_native_preparation()


def test_adapter_selection_extension_cannot_promote_opaque_callable(model_tree):
    calls = []
    provider = OmniVoiceProvider(model=str(model_tree), synthesizer=lambda *args: calls.append(args))
    selected = provider.select_local_execution_model(OmniVoiceLocalParameters(num_step=48))
    assert selected.parameters.num_step == 48
    with pytest.raises(NativeExecutionUnsupported, match="opaque_callable"):
        provider.prepare_execution(selected)
    assert calls == []


def test_parameter_model_copy_cannot_bypass_revalidation(model_tree):
    invalid = OmniVoiceLocalParameters().model_copy(update={"device": "auto"})
    with pytest.raises(ValidationError):
        select_local_omnivoice(model_tree, invalid)


def test_missing_auxiliary_directory_stops_without_cache_lookup(model_tree):
    for path in (model_tree / "audio_tokenizer").iterdir():
        path.unlink()
    (model_tree / "audio_tokenizer").rmdir()
    with pytest.raises(ExecutionPreparationError, match="artifact_missing"):
        select_local_omnivoice(model_tree)


def test_nested_remote_code_mapping_is_rejected(model_tree):
    (model_tree / "config.json").write_text(json.dumps({"model_type": "omnivoice",
        "llm_config": {"auto_map": {"AutoModel": "remote.custom"}}}), encoding="utf-8")
    with pytest.raises(NativeExecutionUnsupported, match="remote_code"):
        select_local_omnivoice(model_tree)


@pytest.mark.parametrize("parameters", [False, "32", {"num_step": 32}])
def test_invalid_parameter_object_is_not_a_default(model_tree, parameters):
    with pytest.raises(ExecutionPreparationError, match="parameters_invalid"):
        select_local_omnivoice(model_tree, parameters)


def test_adapter_requires_explicit_resolved_parameters(model_tree):
    provider = OmniVoiceProvider(model=str(model_tree))
    with pytest.raises(ExecutionPreparationError, match="parameters_required"):
        provider.select_local_execution_model(None)
