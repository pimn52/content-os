"""Owned local clone entry for a future fixed native runtime bundle.

Not wired to a Worker/HTTP route. The application must reserve first and use
the same prepared command. Current adapters still reject native preparation;
this entry alone cannot establish runtime closure, license or execution rights.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import random
from types import SimpleNamespace

from pydantic import BaseModel, ConfigDict, field_validator

from .omnivoice_local import OmniVoiceLocalParameters, select_local_omnivoice
from .prepared import ExecutionPreparationError, PendingInvocation


class LocalCloneInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    text: str
    reference_audio: str
    reference_text: str
    language: str
    output_path: str

    @field_validator("text", "reference_text", "language")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("omnivoice_clone_input_required")
        return value.strip()

    @field_validator("reference_audio", "output_path")
    @classmethod
    def absolute_path(cls, value):
        if not Path(value).is_absolute() or "\0" in value:
            raise ValueError("omnivoice_clone_path_invalid")
        return value


OFFLINE_ENVIRONMENT = (("HF_HUB_OFFLINE", "1"), ("TRANSFORMERS_OFFLINE", "1"))


def build_local_clone_invocation(files, parameters, *, inputs: LocalCloneInput,
                                timeout_seconds: float) -> PendingInvocation:
    """PreparedExecution builder: only fixed executable/model paths, no env reads.

    The future closed runtime must include this app module and its dependencies
    in its isolated Python installation. This does not package/certify it.
    Per-topic input JSON stays in Job identity, not execution-spec parameters.
    """
    try:
        effective = OmniVoiceLocalParameters.model_validate(dict(parameters))
        inputs = LocalCloneInput.model_validate_json(inputs.model_dump_json())
        interpreter = files[("runtime", "python.exe")]
        model_root = files[("model", "model.safetensors")].parent
        selection = select_local_omnivoice(model_root, effective)
        if {item.name for item in selection.artifacts} != {name for group, name in files if group == "model"}:
            raise ExecutionPreparationError("omnivoice_fixed_selection_mismatch")
        # The actual entry bytes must be part of the selected runtime too.
        files[("runtime", "Lib/site-packages/app/providers/omnivoice_entry.py")]
        return PendingInvocation((str(interpreter), "-I", "-m", "app.providers.omnivoice_entry",
            "--model-root", str(model_root), "--parameters", effective.model_dump_json(),
            "--inputs", inputs.model_dump_json()), interpreter.parent.parent,
            OFFLINE_ENVIRONMENT, timeout_seconds)
    except ExecutionPreparationError:
        raise
    except Exception:
        raise ExecutionPreparationError("omnivoice_invocation_invalid") from None


def _load_runtime():
    # Called only AFTER child input/config/environment checks, and after the
    # caller's durable reservation. Tests replace this function, not a native
    # adapter's authority/observation interface.
    import numpy
    import torch
    import soundfile
    from omnivoice.models.omnivoice import OmniVoice
    return SimpleNamespace(numpy=numpy, torch=torch, soundfile=soundfile, model_class=OmniVoice)


def require_loaded_configuration(model, torch, parameters):
    """Check registered loaded state, not just a model's first-tensor labels.

    No casts/moves/fallback, tensor reads or native preparation authority. The
    owned loader/source closure must still precede calling this post-load gate.
    Integer/bool buffers are not floating precision, but must use the device.
    Auxiliary floating state gets no implicit exception to declared precision.
    """
    try:
        if (not isinstance(model, torch.nn.Module) or str(model.device) != parameters.device
                or model.dtype != torch.float16):
            raise ExecutionPreparationError("omnivoice_loaded_configuration_changed")
        seen, parameter_count = set(), 0
        integer_buffers = (torch.bool, torch.uint8, torch.int8, torch.int16, torch.int32, torch.int64)
        for kind, iterator in (("parameter", model.named_parameters(recurse=True, remove_duplicate=False)),
                               ("buffer", model.named_buffers(recurse=True, remove_duplicate=False))):
            for row in iterator:
                if len(seen) >= 100_000:
                    raise ExecutionPreparationError("omnivoice_loaded_configuration_limit")
                if not isinstance(row, tuple) or len(row) != 2:
                    raise ValueError()
                name, tensor = row
                if (type(name) is not str or not name or len(name) > 1024 or name in seen
                        or not torch.is_tensor(tensor)):
                    raise ValueError()
                seen.add(name)
                if (torch.nn.parameter.is_lazy(tensor) or tensor.is_meta
                        or str(tensor.device) != parameters.device or tensor.is_complex()):
                    raise ExecutionPreparationError("omnivoice_loaded_configuration_changed")
                if tensor.is_floating_point():
                    if tensor.dtype != torch.float16:
                        raise ExecutionPreparationError("omnivoice_loaded_configuration_changed")
                elif kind != "buffer" or tensor.dtype not in integer_buffers:
                    raise ExecutionPreparationError("omnivoice_loaded_configuration_changed")
                if kind == "parameter":
                    parameter_count += 1
        if parameter_count == 0:
            raise ValueError()
    except ExecutionPreparationError:
        raise
    except Exception:
        # Do not expose tensor/model reprs, names, paths or loader exceptions.
        raise ExecutionPreparationError("omnivoice_loaded_configuration_unavailable") from None


def execute_local_clone(model_root: Path, parameters_json: str, inputs_json: str) -> Path:
    """Child execution only; never a preflight/model-load probe or permission.

    Whole-runtime file fixing and all source/consent/budget/receipt checks belong
    to the parent application. The Worker does NOT call this entry yet.
    """
    try:
        parameters = OmniVoiceLocalParameters.model_validate_json(parameters_json)
        inputs = LocalCloneInput.model_validate_json(inputs_json)
        selection = select_local_omnivoice(model_root, parameters)
        if any(os.environ.get(key) != value for key, value in OFFLINE_ENVIRONMENT):
            raise ExecutionPreparationError("omnivoice_offline_environment_required")
        reference = Path(inputs.reference_audio)
        target = Path(inputs.output_path)
        if (reference != reference.resolve() or not reference.is_file() or reference.stat().st_size <= 0
            or target != target.resolve() or reference == target or target.suffix.lower() != ".wav"):
            raise ExecutionPreparationError("omnivoice_clone_media_invalid")
        if target.exists():
            raise ExecutionPreparationError("omnivoice_clone_output_conflict")
        runtime = _load_runtime()
        torch = runtime.torch
        if parameters.device == "cuda:0" and not torch.cuda.is_available():
            raise ExecutionPreparationError("omnivoice_device_unavailable")
        random.seed(parameters.seed)
        runtime.numpy.random.seed(parameters.seed)
        torch.manual_seed(parameters.seed)
        if parameters.device == "cuda:0":
            torch.cuda.manual_seed_all(parameters.seed)
        model = runtime.model_class.from_pretrained(str(selection.root),
            **selection.loader_kwargs, dtype=torch.float16)
        require_loaded_configuration(model, torch, parameters)
        audio = model.generate(text=inputs.text, ref_audio=inputs.reference_audio,
            ref_text=inputs.reference_text, language=inputs.language,
            instruct=None, duration=None, voice_clone_prompt=None, generation_config=None,
            **parameters.generation_kwargs())
        if not isinstance(audio, (list, tuple)) or len(audio) != 1:
            raise ExecutionPreparationError("omnivoice_clone_output_invalid")
        if target != target.resolve():
            raise ExecutionPreparationError("omnivoice_clone_media_invalid")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Exclusive creation also protects an output that appeared during
            # inference. A partial new file on write failure stays unadmitted
            # for caller reconciliation; never delete/overwrite existing media.
            with target.open("xb") as output:
                runtime.soundfile.write(output, audio[0], model.sampling_rate,
                    format=parameters.output_format, subtype=parameters.output_subtype)
        except FileExistsError:
            raise ExecutionPreparationError("omnivoice_clone_output_conflict") from None
        if not target.is_file() or target.stat().st_size <= 0:
            raise ExecutionPreparationError("omnivoice_clone_output_invalid")
        return target
    except ExecutionPreparationError:
        raise
    except Exception:
        # Never expose model/command errors, prompt text or installed paths.
        raise ExecutionPreparationError("omnivoice_local_clone_failed") from None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-root", type=Path, required=True)
    parser.add_argument("--parameters", required=True)
    parser.add_argument("--inputs", required=True)
    args = parser.parse_args()
    try:
        execute_local_clone(args.model_root, args.parameters, args.inputs)
    except ExecutionPreparationError as exc:
        parser.exit(1, str(exc) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
