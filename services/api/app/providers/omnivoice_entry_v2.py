"""Future reserved-child CPU clone entry; no normal Worker selects this entry."""
import argparse
from array import array
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import random
import struct
import sys
from types import SimpleNamespace

from pydantic import Field, model_validator

from .omnivoice_entry import LocalCloneInput, OFFLINE_ENVIRONMENT
from .omnivoice_local import _json_object
from .omnivoice_recipe_v2 import OmniVoiceLocalParametersV2, select_local_omnivoice_v2
from .omnivoice_state_v2 import require_component_state
from .prepared import ExecutionPreparationError, PendingInvocation

SOURCE_HASHES = {
    "omnivoice/models/omnivoice.py": "631050ba94775b9c8a72ec3b2d84777c38317e2e502b1998d3128fedf88846ff",
    "omnivoice/utils/audio.py": "6e2154cb8936138ee6c417c253dc9b222500ed322e6fbb2c0377a0b6850b8b48",
    "transformers/modeling_utils.py": "38c2bd02ed7af229f54e2f02dae65663be7af29ed2c9498bd1c460cf60fbb62d",
    "transformers/models/qwen3/modeling_qwen3.py": "cbb7f2dc274c2f5592746c0dc6985ca50353efa07376f92cc922b77680a74f69",
}


class LocalCloneInputV2(LocalCloneInput):
    reference_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reference_start_frame: int = Field(ge=0)
    reference_end_frame: int = Field(gt=0)
    reference_max_bytes: int = Field(gt=44, le=64*1024**2)
    reference_max_frames: int = Field(gt=0, le=32*1024**2)

    @model_validator(mode="after")
    def interval(self):
        if self.reference_end_frame <= self.reference_start_frame:
            raise ValueError("omnivoice_reference_interval_invalid")
        return self


@dataclass(frozen=True)
class PCMReference:
    samples: array
    sampling_rate: int
    total_frames: int


def read_reference(inputs, sampling_rate):
    """Bounded canonical PCM16, hash of full file and explicit selected frames."""
    try:
        path = Path(inputs.reference_audio)
        if path != path.resolve() or not path.is_file():
            raise ValueError()
        size = path.stat().st_size
        if not 44 < size <= inputs.reference_max_bytes:
            raise ValueError()
        with path.open("rb") as source:
            raw = source.read(inputs.reference_max_bytes + 1)
        if len(raw) != size or hashlib.sha256(raw).hexdigest() != inputs.reference_sha256:
            raise ValueError()
        riff, length, wave, fmt, fmt_len, encoding, channels, rate, byte_rate, align, bits, data, data_len = struct.unpack("<4sI4s4sIHHIIHH4sI", raw[:44])
        if ((riff, wave, fmt, data) != (b"RIFF", b"WAVE", b"fmt ", b"data")
                or length != size - 8 or fmt_len != 16 or encoding != 1 or channels != 1
                or bits != 16 or align != 2 or byte_rate != rate * 2 or rate != sampling_rate
                or data_len != size - 44 or data_len % 2):
            raise ValueError()
        frames = data_len // 2
        if not frames <= inputs.reference_max_frames or inputs.reference_end_frame > frames:
            raise ValueError()
        pcm = array("h", raw[44 + 2*inputs.reference_start_frame:44 + 2*inputs.reference_end_frame])
        if sys.byteorder != "little":
            pcm.byteswap()
        if not pcm or not any(pcm):
            raise ValueError()
        return PCMReference(array("f", (v / 32768.0 for v in pcm)), rate, frames)
    except Exception:
        raise ExecutionPreparationError("omnivoice_reference_pcm_invalid") from None


def require_recipe_sources(site_packages):
    try:
        for name, digest in SOURCE_HASHES.items():
            path = site_packages / name
            if path != path.resolve() or not path.is_file() or path.stat().st_size > 4*1024**2:
                raise ValueError()
            with path.open("rb") as source:
                raw = source.read(4*1024**2 + 1)
            if len(raw) > 4*1024**2 or hashlib.sha256(raw).hexdigest() != digest:
                raise ValueError()
    except Exception:
        raise ExecutionPreparationError("omnivoice_recipe_source_changed") from None


def _load_runtime():
    require_recipe_sources(Path(__file__).resolve().parents[2])
    import numpy
    import torch
    import soundfile
    from omnivoice.models.omnivoice import OmniVoice
    from omnivoice.utils.duration import RuleDurationEstimator
    from transformers import AutoTokenizer, AutoFeatureExtractor, HiggsAudioV2TokenizerModel
    from transformers.models.qwen3.modeling_qwen3 import Qwen3Model, Qwen3RotaryEmbedding
    return SimpleNamespace(numpy=numpy, torch=torch, soundfile=soundfile,
        model_class=OmniVoice, audio_class=HiggsAudioV2TokenizerModel,
        text_class=AutoTokenizer, feature_class=AutoFeatureExtractor,
        duration_class=RuleDurationEstimator, llm_class=Qwen3Model, rotary_class=Qwen3RotaryEmbedding)


def build_local_clone_invocation_v2(files, parameters, *, inputs, timeout_seconds):
    try:
        params = OmniVoiceLocalParametersV2.model_validate(dict(parameters))
        inputs = LocalCloneInputV2.model_validate_json(inputs.model_dump_json())
        interpreter = files[("runtime", "python.exe")]
        root = files[("model", "model.safetensors")].parent
        selected = select_local_omnivoice_v2(root, params)
        if {a.name for a in selected.artifacts} != {n for g, n in files if g == "model"}:
            raise ValueError()
        for name in ("omnivoice_entry_v2.py", "omnivoice_recipe_v2.py", "omnivoice_state_v2.py"):
            files[("runtime", "Lib/site-packages/app/providers/" + name)]
        return PendingInvocation((str(interpreter), "-I", "-m", "app.providers.omnivoice_entry_v2",
            "--model-root", str(root), "--parameters", params.model_dump_json(),
            "--inputs", inputs.model_dump_json()), interpreter.parent.parent,
            OFFLINE_ENVIRONMENT, timeout_seconds)
    except Exception:
        raise ExecutionPreparationError("omnivoice_invocation_invalid") from None


def load_components(runtime, selection):
    torch, root = runtime.torch, str(selection.root)
    local = dict(local_files_only=True, trust_remote_code=False)
    model = runtime.model_class.from_pretrained(root, train=True, load_asr=False,
        dtype=torch.float16, device_map="cpu", attn_implementation="eager", **local)
    if type(model) is not runtime.model_class or type(model.llm) is not runtime.llm_class:
        raise ExecutionPreparationError("omnivoice_loaded_component_configuration_changed")
    # These two computed, nonpersistent buffers are explicitly initialized from
    # the reviewed constructor/config, never repaired by casting loaded weights.
    rotary = model.llm.rotary_emb
    if (type(rotary) is not runtime.rotary_class
            or model.llm.config.rope_parameters.get("rope_type") != "default"
            or set(rotary._buffers) != {"inv_freq", "original_inv_freq"}
            or rotary._parameters
            or not {"inv_freq", "original_inv_freq"} <= rotary._non_persistent_buffers_set):
        raise ExecutionPreparationError("omnivoice_loaded_component_configuration_changed")
    expected = _json_object(selection.root / "config.json")["llm_config"]
    loaded = model.llm.config.to_dict()
    if any(loaded.get(k) != v for k, v in expected.items() if k not in ("dtype", "torch_dtype", "_name_or_path")):
        raise ExecutionPreparationError("omnivoice_loaded_component_configuration_changed")
    rebuilt = runtime.rotary_class(model.llm.config, device="cpu")
    for name in ("inv_freq", "original_inv_freq"):
        setattr(rotary, name, getattr(rebuilt, name))
    rotary.attention_scaling = rebuilt.attention_scaling
    model.text_tokenizer = runtime.text_class.from_pretrained(root, **local)
    aux_root = str(selection.root / "audio_tokenizer")
    model.audio_tokenizer = runtime.audio_class.from_pretrained(aux_root,
        dtype=torch.float32, device_map="cpu", attn_implementation="eager", **local)
    model.feature_extractor = runtime.feature_class.from_pretrained(aux_root, **local)
    model.duration_estimator = runtime.duration_class()
    model.sampling_rate = model.feature_extractor.sampling_rate
    model.eval()
    require_component_state(model, runtime, sampling_rate=selection.sampling_rate)
    return model


def execute_local_clone_v2(model_root, parameters_json, inputs_json):
    try:
        params = OmniVoiceLocalParametersV2.model_validate_json(parameters_json)
        inputs = LocalCloneInputV2.model_validate_json(inputs_json)
        selection = select_local_omnivoice_v2(model_root, params)
        if any(os.environ.get(k) != v for k, v in OFFLINE_ENVIRONMENT):
            raise ExecutionPreparationError("omnivoice_offline_environment_required")
        target = Path(inputs.output_path)
        if target != target.resolve() or target == Path(inputs.reference_audio) or target.suffix.lower() != ".wav":
            raise ExecutionPreparationError("omnivoice_clone_media_invalid")
        if target.exists():
            raise ExecutionPreparationError("omnivoice_clone_output_conflict")
        reference = read_reference(inputs, selection.sampling_rate)
        runtime = _load_runtime()
        random.seed(params.seed)
        runtime.numpy.random.seed(params.seed)
        runtime.torch.manual_seed(params.seed)
        model = load_components(runtime, selection)
        waveform = runtime.numpy.asarray(reference.samples, dtype=runtime.numpy.float32)
        audio = model.generate(text=inputs.text, ref_audio=(waveform, reference.sampling_rate),
            ref_text=inputs.reference_text, language=inputs.language, instruct=None,
            duration=None, voice_clone_prompt=None, generation_config=None, **params.generation_kwargs())
        if not isinstance(audio, (list, tuple)) or len(audio) != 1:
            raise ExecutionPreparationError("omnivoice_clone_output_invalid")
        if target != target.resolve():
            raise ExecutionPreparationError("omnivoice_clone_media_invalid")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("xb") as output:
                runtime.soundfile.write(output, audio[0], model.sampling_rate,
                    format=params.output_format, subtype=params.output_subtype)
        except FileExistsError:
            raise ExecutionPreparationError("omnivoice_clone_output_conflict") from None
        if target.stat().st_size <= 0:
            raise ExecutionPreparationError("omnivoice_clone_output_invalid")
        return target
    except ExecutionPreparationError:
        raise
    except Exception:
        raise ExecutionPreparationError("omnivoice_local_clone_failed") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-root", type=Path, required=True)
    parser.add_argument("--parameters", required=True)
    parser.add_argument("--inputs", required=True)
    args = parser.parse_args()
    try:
        execute_local_clone_v2(args.model_root, args.parameters, args.inputs)
    except ExecutionPreparationError as error:
        parser.exit(1, str(error) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
