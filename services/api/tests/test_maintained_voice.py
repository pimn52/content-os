"""Synthetic model/SoundFile; real NumPy + std-library PCM, no model inference."""
import hashlib
from pathlib import Path
from types import SimpleNamespace
import wave

import numpy as np
import pytest
from pydantic import ValidationError

from app.providers.maintained_voice import MaintainedVoiceParameters, execute_maintained_clone
from app.providers.omnivoice_entry_v2 import LocalCloneInputV2
from app.providers.prepared import ExecutionPreparationError
from test_maintained_runtime import candidate


@pytest.fixture
def recipe(candidate, monkeypatch):
    options = candidate["options"]
    root, model = options["data_root"], options["model_root"]
    reference = root / "reference.wav"
    with wave.open(str(reference), "wb") as wav:
        wav.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        wav.writeframes(np.array([1000, -1000, 2000, -2000], dtype="<i2").tobytes())
    inputs = LocalCloneInputV2(text="bounded fixture copy", reference_audio=str(reference),
        reference_text="fixture reference", language="Chinese", output_path=str(root / "output.wav"),
        reference_sha256=hashlib.sha256(reference.read_bytes()).hexdigest(),
        reference_start_frame=1, reference_end_frame=3, reference_max_frames=4, reference_max_bytes=100)
    parameters = MaintainedVoiceParameters(seed=7, device="cuda:0", num_step=16, speed=1.1).model_dump()
    calls = []
    loaded = SimpleNamespace(dtype=np.dtype("float16"), device="cuda:0", sampling_rate=24000,
        audio_tokenizer=SimpleNamespace(dtype=np.dtype("float32"), device="cuda:0"))
    output = np.array([0.25, -0.25, 0], dtype="float32")
    def generate(**kwargs):
        calls.append(("generate", kwargs)); return [output]
    loaded.generate = generate
    loaded.eval = lambda: calls.append(("eval", None))
    class Model:
        @classmethod
        def from_pretrained(cls, path, **kwargs):
            calls.append(("load", {"path": path, **kwargs})); return loaded
    class SoundFile:
        @staticmethod
        def write(stream, samples, rate, **kwargs):
            calls.append(("write", kwargs))
            with wave.open(stream, "wb") as wav:
                wav.setparams((1, 2, rate, 0, "NONE", "not compressed"))
                wav.writeframes((samples * 32767).astype("<i2").tobytes())
        @staticmethod
        def info(path):
            with wave.open(str(path), "rb") as wav:
                return SimpleNamespace(format="WAV", subtype="PCM_16", channels=wav.getnchannels(),
                    samplerate=wav.getframerate(), frames=wav.getnframes())
    runtime = SimpleNamespace(numpy=np, torch=SimpleNamespace(float16=np.dtype("float16"),
        float32=np.dtype("float32"), manual_seed=lambda seed: calls.append(("seed", seed))),
        soundfile=SoundFile, model_class=Model)
    def load_runtime():
        calls.append(("runtime", None)); return runtime
    monkeypatch.setattr("app.providers.maintained_voice._load_runtime", load_runtime)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1"); monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    return SimpleNamespace(root=root, model=model, inputs=inputs, parameters=parameters,
        calls=calls, loaded=loaded, runtime=runtime, output=output)


def execute(case):
    return execute_maintained_clone(data_root=case.root, model_root=case.model,
        parameters=case.parameters, inputs=case.inputs)


def test_fixed_vendor_loader_complete_settings_and_bounded_pcm_export(recipe):
    c = recipe; result = execute(c)
    load = next(value for name, value in c.calls if name == "load")
    assert load == dict(path=str(c.model), train=False, load_asr=False, dtype=np.dtype("float16"),
        device_map="cuda:0", attn_implementation="eager", local_files_only=True, trust_remote_code=False)
    generate = next(value for name, value in c.calls if name == "generate")
    assert generate["text"] == c.inputs.text and generate["ref_text"] == c.inputs.reference_text
    waveform, rate = generate.pop("ref_audio")
    assert waveform.dtype == np.dtype("float32") and rate == 24000
    np.testing.assert_array_equal(waveform, np.array([-1000, 2000], dtype="float32") / 32768)
    expected = MaintainedVoiceParameters.model_validate(c.parameters).generation_kwargs()
    assert {key: generate[key] for key in expected} == expected
    assert all(generate[key] is None for key in ("instruct", "duration", "voice_clone_prompt", "generation_config"))
    assert ("seed", 7) in c.calls
    assert next(value for name, value in c.calls if name == "write") == {"format": "WAV", "subtype": "PCM_16"}
    with wave.open(str(result), "rb") as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()) == (1, 2, 24000, 3)
    before = len(c.calls)
    with pytest.raises(ExecutionPreparationError, match="output_conflict"): execute(c)
    assert len(c.calls) == before  # no second load/generation or overwrite


@pytest.mark.parametrize("change", ["reference_hash", "reference_range", "outside_output", "offline", "model_missing", "remote_code"])
def test_invalid_inputs_stop_before_runtime_import(recipe, monkeypatch, change):
    c = recipe
    if change == "reference_hash": c.inputs = c.inputs.model_copy(update={"reference_sha256": "f"*64})
    elif change == "reference_range": c.inputs = c.inputs.model_copy(update={"reference_end_frame": 5})
    elif change == "outside_output": c.inputs = c.inputs.model_copy(update={"output_path": str(c.root.parent / "outside.wav")})
    elif change == "offline": monkeypatch.delenv("HF_HUB_OFFLINE")
    elif change == "model_missing": (c.model / "audio_tokenizer/model.safetensors").unlink()
    else: (c.model / "config.json").write_text('{"model_type":"omnivoice","auto_map":{}}')
    with pytest.raises(ExecutionPreparationError): execute(c)
    assert c.calls == [] and not Path(c.inputs.output_path).exists()


@pytest.mark.parametrize("change", ["main_dtype", "aux_dtype", "main_device", "aux_device", "rate", "rate_type"])
def test_observed_component_mismatch_stops_before_generation_without_casting(recipe, change):
    c = recipe
    if change == "main_dtype": c.loaded.dtype = np.dtype("float32")
    elif change == "aux_dtype": c.loaded.audio_tokenizer.dtype = np.dtype("float16")
    elif change == "main_device": c.loaded.device = "cpu"
    elif change == "aux_device": c.loaded.audio_tokenizer.device = "cpu"
    elif change == "rate": c.loaded.sampling_rate = 48000
    else: c.loaded.sampling_rate = 24000.0
    with pytest.raises(ExecutionPreparationError, match="loaded_configuration_changed"): execute(c)
    assert not any(name in ("generate", "write") for name, _ in c.calls)
    assert not Path(c.inputs.output_path).exists()


@pytest.mark.parametrize("output", [np.array([], dtype="float32"), np.array([np.nan], dtype="float32"),
    np.array([np.inf], dtype="float32"), np.array([1.1], dtype="float32"),
    np.array([0.1], dtype="float64"), np.array([[0.1]], dtype="float32"), [0.1]])
def test_invalid_output_is_never_coerced_clipped_or_exported(recipe, output):
    c = recipe; c.loaded.generate = lambda **kwargs: [output]
    with pytest.raises(ExecutionPreparationError, match="output_invalid"): execute(c)
    assert not Path(c.inputs.output_path).exists()
    assert not any(name == "write" for name, _ in c.calls)


@pytest.mark.parametrize("limit,value", [("output_max_frames", 2), ("output_max_bytes", 48)])
def test_output_budgets_are_independent(recipe, limit, value):
    c = recipe; c.parameters[limit] = value
    with pytest.raises(ExecutionPreparationError, match="output_invalid"): execute(c)
    assert not Path(c.inputs.output_path).exists()


@pytest.mark.parametrize("field,value", [("recipe_version", True), ("recipe_version", 2),
    ("device", "auto"), ("precision", "float32"), ("audio_tokenizer_precision", "float16"),
    ("seed", -1), ("speed", float("nan")), ("output_max_frames", 0), ("output_max_bytes", 44)])
def test_unknown_or_invalid_recipe_controls_fail_strictly(field, value):
    with pytest.raises(ValidationError): MaintainedVoiceParameters.model_validate({field: value})


def test_export_failure_retains_unimported_attempt_and_never_reuses_path(recipe):
    c = recipe
    c.runtime.soundfile.info = lambda path: SimpleNamespace(format="WAV", subtype="FLOAT", channels=1, samplerate=24000, frames=3)
    with pytest.raises(ExecutionPreparationError, match="export_invalid"): execute(c)
    assert Path(c.inputs.output_path).is_file()
    before = len(c.calls)
    with pytest.raises(ExecutionPreparationError, match="output_conflict"): execute(c)
    assert len(c.calls) == before


def test_provider_exception_is_sanitized_and_creates_no_output(recipe):
    c = recipe
    def fail(**kwargs): raise RuntimeError("secret-provider-error /private/path")
    c.loaded.generate = fail
    with pytest.raises(ExecutionPreparationError, match="^maintained_clone_failed$"): execute(c)
    assert not Path(c.inputs.output_path).exists()
