"""Reserved PCM-only child entry. Normal Worker/native admission stays closed."""
import argparse
from array import array
import hashlib
import os
from pathlib import Path
import random
import stat
from types import SimpleNamespace

from . import omnivoice_audio_pcm as pcm
from .omnivoice_entry_v2 import (LocalCloneInputV2, OFFLINE_ENVIRONMENT,
    SOURCE_HASHES as V2_SOURCE_HASHES, read_reference, load_components)
from .omnivoice_recipe_v3 import OmniVoiceLocalParametersV3, select_local_omnivoice_v3
from .omnivoice_pcm_import import require_pcm_boundary, BOOTSTRAP_SLOT
from .prepared import ExecutionPreparationError, PendingInvocation

UPSTREAM_SLOT = "evidence/omnivoice/audio.py.source"
DESCRIPTOR_SLOT = "evidence/omnivoice/audio-pcm.json"
DERIVED_SLOT = "Lib/site-packages/omnivoice/utils/audio.py"
SOURCE_HASHES = {name: digest for name, digest in V2_SOURCE_HASHES.items()
                 if name != "omnivoice/utils/audio.py"}
OWNED_MODULES = ("omnivoice_audio_pcm.py", "omnivoice_entry_v3.py", "omnivoice_recipe_v3.py",
    "omnivoice_entry_v2.py", "omnivoice_recipe_v2.py", "omnivoice_state_v2.py",
    "omnivoice_entry.py", "omnivoice_local.py", "prepared.py", "omnivoice_pcm_import.py")


def _read_source(path):
    """Canonical regular source, bounded read and unchanged same-file witness."""
    if path != path.resolve(strict=True):
        raise ValueError()
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= 4*1024**2:
        raise ValueError()
    def identity(value):
        return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
    with path.open("rb") as source:
        opened = os.fstat(source.fileno())
        # Windows lstat ctime may expose creation time while fstat exposes
        # metadata-change time. Compare common identity across APIs, then keep
        # each API's full witness stable independently through the read.
        if identity(opened)[:4] != identity(before)[:4]:
            raise ValueError()
        raw = source.read(4*1024**2 + 1)
        source.seek(0)
        if (len(raw) != before.st_size or source.read(4*1024**2 + 1) != raw
                or identity(os.fstat(source.fileno())) != identity(opened)):
            raise ValueError()
    if identity(path.lstat()) != identity(before):
        raise ValueError()
    return raw


def require_pcm_recipe_sources(root, *, inventory=None):
    """Recompute the derivation, never trust caller hashes or sidecar authority.

    Parent preparation also compares owned copies to the current repository and
    binds these bytes in the whole runtime inventory. A child self-check alone
    is not independent parent/loaded-origin/native evidence.
    """
    try:
        root = Path(root)
        if root != root.resolve(strict=True):
            raise ValueError()
        checked = {}
        def read(name):
            raw = _read_source(root / name)
            checked[name] = raw
            return raw
        derived = pcm.derive_pcm_audio(read(UPSTREAM_SLOT))
        if read(DERIVED_SLOT) != derived.payload or read(DESCRIPTOR_SLOT) != derived.descriptor:
            raise ValueError()
        for name, digest in SOURCE_HASHES.items():
            if hashlib.sha256(read("Lib/site-packages/" + name)).hexdigest() != digest:
                raise ValueError()
        for name in OWNED_MODULES:
            current = _read_source(Path(__file__).resolve().parent / name)
            if read("Lib/site-packages/app/providers/" + name) != current:
                raise ValueError()
        if inventory is not None:
            rows = {item.name: item for item in inventory.files}
            for name, raw in checked.items():
                row = rows[name]
                if row.size_bytes != len(raw) or row.sha256 != hashlib.sha256(raw).hexdigest():
                    raise ValueError()
        return derived.identity_sha256
    except Exception:
        raise ExecutionPreparationError("omnivoice_pcm_recipe_source_changed") from None


def _load_runtime(pcm_boundary=None):
    root = Path(__file__).resolve().parents[4]
    require_pcm_recipe_sources(root)
    require_pcm_boundary(pcm_boundary, root=root)
    pcm_boundary.require_native_preparation()
    import numpy
    import torch
    from omnivoice.models.omnivoice import OmniVoice
    from omnivoice.utils.duration import RuleDurationEstimator
    from transformers import AutoTokenizer, AutoFeatureExtractor, HiggsAudioV2TokenizerModel
    from transformers.models.qwen3.modeling_qwen3 import Qwen3Model, Qwen3RotaryEmbedding
    return SimpleNamespace(numpy=numpy, torch=torch, model_class=OmniVoice,
        audio_class=HiggsAudioV2TokenizerModel, text_class=AutoTokenizer,
        feature_class=AutoFeatureExtractor, duration_class=RuleDurationEstimator,
        llm_class=Qwen3Model, rotary_class=Qwen3RotaryEmbedding)


def build_local_clone_invocation_v3(files, parameters, *, inputs, timeout_seconds, pcm_boundary=None):
    try:
        params = OmniVoiceLocalParametersV3.model_validate(dict(parameters))
        inputs = LocalCloneInputV2.model_validate_json(inputs.model_dump_json())
        interpreter = Path(files[("runtime", "python.exe")])
        root = interpreter.parent
        if interpreter != interpreter.resolve(strict=True) or not interpreter.is_file():
            raise ValueError()
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 3600:
            raise ValueError()
        selected = select_local_omnivoice_v3(files[("model", "model.safetensors")].parent, params)
        if {a.name: a.source for a in selected.artifacts} != {n: p for (g, n), p in files.items() if g == "model"}:
            raise ValueError()
        for name in (UPSTREAM_SLOT, DESCRIPTOR_SLOT, DERIVED_SLOT,
                *("Lib/site-packages/" + n for n in SOURCE_HASHES),
                *("Lib/site-packages/app/providers/" + n for n in OWNED_MODULES)):
            if files[("runtime", name)] != root / name:
                raise ValueError()
        require_pcm_recipe_sources(root)
        require_pcm_boundary(pcm_boundary, root=root)
        if {n: p for (g, n), p in files.items() if g == 'runtime'} != {
                row.name: root/row.name for row in pcm_boundary.tree.inventory.files}:
            raise ValueError()
        return PendingInvocation((str(interpreter), "-I", "-B", str(root/BOOTSTRAP_SLOT),
            "--model-root", str(selected.root), "--parameters", params.model_dump_json(),
            "--inputs", inputs.model_dump_json()), root, pcm_boundary.environment.environment, timeout_seconds)
    except Exception:
        raise ExecutionPreparationError("omnivoice_pcm_invocation_invalid") from None


def execute_local_clone_v3(model_root, parameters_json, inputs_json, *, pcm_boundary=None):
    try:
        params = OmniVoiceLocalParametersV3.model_validate_json(parameters_json)
        inputs = LocalCloneInputV2.model_validate_json(inputs_json)
        selection = select_local_omnivoice_v3(model_root, params)
        if any(os.environ.get(k) != v for k, v in OFFLINE_ENVIRONMENT):
            raise ExecutionPreparationError("omnivoice_offline_environment_required")
        target = Path(inputs.output_path)
        if (target != target.resolve() or target == Path(inputs.reference_audio)
                or target.suffix.lower() != ".wav"):
            raise ExecutionPreparationError("omnivoice_clone_media_invalid")
        if target.exists():
            raise ExecutionPreparationError("omnivoice_clone_output_conflict")
        reference = read_reference(inputs, selection.sampling_rate)
        if pcm_boundary is not None:
            require_pcm_boundary(pcm_boundary)
        root = pcm_boundary.tree.root if pcm_boundary is not None else Path(__file__).resolve().parents[4]
        require_pcm_recipe_sources(root)
        require_pcm_boundary(pcm_boundary, root=root)
        if pcm_boundary is not None and any(os.environ.get(k) != v for k,v in pcm_boundary.environment.environment):
            raise ExecutionPreparationError('omnivoice_pcm_environment_changed')
        runtime = _load_runtime(pcm_boundary)
        random.seed(params.seed)
        runtime.numpy.random.seed(params.seed)
        runtime.torch.manual_seed(params.seed)
        model = load_components(runtime, selection)
        waveform = runtime.numpy.asarray(reference.samples, dtype=runtime.numpy.float32)
        audio = model.generate(text=inputs.text, ref_audio=(waveform, reference.sampling_rate),
            ref_text=inputs.reference_text, language=inputs.language, instruct=None,
            duration=None, voice_clone_prompt=None, generation_config=None, **params.generation_kwargs())
        if not isinstance(audio, (list, tuple)) or len(audio) != 1:
            raise ExecutionPreparationError("omnivoice_pcm_output_invalid")
        output = audio[0]
        # No implicit shape/dtype coercion. Only actual mono float32 NumPy output.
        if (type(output) is not runtime.numpy.ndarray or output.ndim != 1
                or output.dtype != runtime.numpy.dtype("float32")
                or not 0 < output.size <= params.output_max_frames
                or 44 + 2*output.size > params.output_max_bytes):
            raise ExecutionPreparationError("omnivoice_pcm_output_invalid")
        pcm.write_pcm16_wav(target, array("f", output), model.sampling_rate,
            max_frames=params.output_max_frames, max_bytes=params.output_max_bytes)
        return target
    except ExecutionPreparationError:
        raise
    except pcm.PCMContractError as error:
        raise ExecutionPreparationError(str(error)) from None
    except RuntimeError as error:
        if str(error) == pcm.DECODE_STOP:
            raise ExecutionPreparationError(pcm.DECODE_STOP) from None
        raise ExecutionPreparationError("omnivoice_local_clone_failed") from None
    except Exception:
        raise ExecutionPreparationError("omnivoice_local_clone_failed") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-root", type=Path, required=True)
    parser.add_argument("--parameters", required=True)
    parser.add_argument("--inputs", required=True)
    args = parser.parse_args()
    try:
        execute_local_clone_v3(args.model_root, args.parameters, args.inputs)
    except ExecutionPreparationError as error:
        parser.exit(1, str(error) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
