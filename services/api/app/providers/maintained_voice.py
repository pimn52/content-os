"""Owned maintained-process clone recipe; no normal dispatch selects it.

This primitive is not preparation, an authority issuer or a runtime witness.
Future integration must bind these bytes into a generation-specific origin and
retain current admission, source/consent, reservation and output lineage gates.
Schema4 import/PCM preparation deliberately cannot invoke this recipe.
"""
from pathlib import Path
import os
import random
from types import SimpleNamespace
from typing import Literal

from pydantic import Field, field_validator

from .maintained_runtime import _model_artifacts
from .omnivoice_local import OmniVoiceLocalParameters, _json_object
from .omnivoice_entry_v2 import LocalCloneInputV2, read_reference
from .prepared import ExecutionPreparationError
from .runtime_tree import _exact_path


class MaintainedVoiceParameters(OmniVoiceLocalParameters):
    recipe_version: Literal[1] = 1
    audio_tokenizer_precision: Literal["float32"] = "float32"
    attention_backend: Literal["eager"] = "eager"
    output_max_frames: int = Field(default=2_880_000, gt=0, le=32*1024**2)
    output_max_bytes: int = Field(default=5_760_044, gt=44, le=64*1024**2)

    @field_validator("recipe_version", mode="before")
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int:
            raise ValueError("maintained_voice_recipe_version_invalid")
        return value

    def generation_kwargs(self):
        return self.model_dump(exclude={"recipe_version", "device", "precision", "seed",
            "audio_tokenizer_precision", "attention_backend", "output_format", "output_subtype",
            "output_max_frames", "output_max_bytes"})


def _load_runtime():
    # Vendor/OS loading is the explicitly disclosed maintained trust boundary.
    # No custom component construction, source rewrite or native closure graph.
    import numpy
    import torch
    import soundfile
    from omnivoice.models.omnivoice import OmniVoice
    return SimpleNamespace(numpy=numpy, torch=torch, soundfile=soundfile, model_class=OmniVoice)


def execute_maintained_clone(*, data_root, model_root, parameters, inputs):
    """Internal recipe primitive, only for a future reserved generation child.

    No injected loader/command, automatic settings, model downloads or retries.
    Failed export can leave an unimported file; an existing path is never reused.
    This function does not claim operator permission or independently hash a
    complete runtime. The current API/Worker cannot reach it.
    """
    try:
        params = MaintainedVoiceParameters.model_validate(dict(parameters))
        request = LocalCloneInputV2.model_validate_json(inputs.model_dump_json())
        root, model, target, reference_path = map(Path,
            (data_root, model_root, request.output_path, request.reference_audio))
        for directory in (root, model, target.parent):
            _exact_path(directory, directory=True)
        _exact_path(reference_path, directory=False)
        if (not model.is_relative_to(root) or not reference_path.is_relative_to(root)
            or not target.is_relative_to(root) or target != target.resolve()
            or target.suffix.lower() != ".wav" or target == reference_path):
            raise ExecutionPreparationError("maintained_clone_boundary_invalid")
        if target.exists():
            raise ExecutionPreparationError("maintained_clone_output_conflict")
        if any(os.environ.get(name) != "1" for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")):
            raise ExecutionPreparationError("maintained_clone_offline_environment_required")
        _model_artifacts(model, 64*1024**3)
        rate = _json_object(model / "audio_tokenizer/preprocessor_config.json").get("sampling_rate")
        if type(rate) is not int or not 0 < rate <= 192_000:
            raise ExecutionPreparationError("maintained_clone_rate_invalid")
        reference = read_reference(request, rate)
        runtime = _load_runtime()
        np, torch, sf = runtime.numpy, runtime.torch, runtime.soundfile
        random.seed(params.seed)
        np.random.seed(params.seed)
        torch.manual_seed(params.seed)
        loaded = runtime.model_class.from_pretrained(str(model), train=False, load_asr=False,
            dtype=torch.float16, device_map=params.device, attn_implementation=params.attention_backend,
            local_files_only=True, trust_remote_code=False)
        # Observe rather than cast/repair loaded components to match a report.
        if (loaded.dtype != torch.float16 or str(loaded.device) != params.device
            or loaded.audio_tokenizer.dtype != torch.float32
            or str(loaded.audio_tokenizer.device) != params.device
            or type(loaded.sampling_rate) is not int or loaded.sampling_rate != rate):
            raise ExecutionPreparationError("maintained_clone_loaded_configuration_changed")
        loaded.eval()
        waveform = np.asarray(reference.samples, dtype=np.float32)
        audio = loaded.generate(text=request.text, language=request.language,
            ref_text=request.reference_text, ref_audio=(waveform, rate), instruct=None,
            duration=None, voice_clone_prompt=None, generation_config=None, **params.generation_kwargs())
        if not isinstance(audio, (list, tuple)) or len(audio) != 1:
            raise ExecutionPreparationError("maintained_clone_output_invalid")
        output = audio[0]
        if (type(output) is not np.ndarray or output.ndim != 1 or output.dtype != np.dtype("float32")
            or not 0 < output.size <= params.output_max_frames
            or 44 + 2*output.size > params.output_max_bytes
            or not np.isfinite(output).all() or (np.abs(output) > 1).any()):
            raise ExecutionPreparationError("maintained_clone_output_invalid")
        # Exclusive creation prevents overwriting a source or a previous attempt.
        with target.open("xb") as stream:
            sf.write(stream, output, rate, format=params.output_format, subtype=params.output_subtype)
        observed = sf.info(target)
        if (observed.format != "WAV" or observed.subtype != "PCM_16" or observed.channels != 1
            or observed.samplerate != rate or observed.frames != output.size
            or target.stat().st_size > params.output_max_bytes):
            raise ExecutionPreparationError("maintained_clone_export_invalid")
        return target
    except ExecutionPreparationError:
        raise
    except Exception:
        # Do not expose model/provider text, local paths or credential-bearing errors.
        raise ExecutionPreparationError("maintained_clone_failed") from None
