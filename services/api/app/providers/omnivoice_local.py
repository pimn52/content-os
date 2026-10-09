"""Bounded local model-selection seam for a future prepared OmniVoice recipe.

No runtime imports, loading or downloads. This is NOT a PreparedExecution or
execution authority: runtime/dependency/device observation and application
reservation remain required. Legacy benchmark synthesis is unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import stat

from pydantic import BaseModel, ConfigDict, Field

from .prepared import ExecutionPreparationError, NativeExecutionUnsupported, SelectedArtifact


class OmniVoiceLocalParameters(BaseModel):
    """Fully expanded bounded clone recipe; no automatic device/seed choice."""
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)
    device: str = Field(pattern=r"^(cpu|cuda:0)$", default="cpu")
    precision: str = Field(pattern=r"^float16$", default="float16")
    seed: int = Field(ge=0, le=2**32-1, default=0)
    num_step: int = Field(ge=1, le=1000, default=32)
    speed: float = Field(gt=0, default=1.0)
    guidance_scale: float = Field(ge=0, default=2.0)
    t_shift: float = Field(ge=0, default=0.1)
    layer_penalty_factor: float = Field(ge=0, default=5.0)
    position_temperature: float = Field(ge=0, default=5.0)
    class_temperature: float = Field(ge=0, default=0.0)
    denoise: bool = True
    preprocess_prompt: bool = True
    postprocess_output: bool = True
    audio_chunk_duration: float = Field(gt=0, default=15.0)
    audio_chunk_threshold: float = Field(gt=0, default=30.0)
    pad_duration: float = Field(ge=0, default=0.1)
    fade_duration: float = Field(ge=0, default=0.1)
    normalize_text: bool = False
    output_format: str = Field(pattern=r"^WAV$", default="WAV")
    output_subtype: str = Field(pattern=r"^PCM_16$", default="PCM_16")

    def generation_kwargs(self) -> dict:
        # No environment reads or library defaults. Device/dtype/seed are
        # loader controls; speed/normalize_text are direct generate controls.
        return self.model_dump(exclude={"device", "precision", "seed", "output_format", "output_subtype"})


def _json_object(path: Path) -> dict:
    def distinct_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result
    try:
        with path.open("rb") as source:
            raw = source.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("size limit")
        value = json.loads(raw, object_pairs_hook=distinct_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("nonfinite")))
        if not isinstance(value, dict):
            raise ValueError("object required")
        def reject_remote_code(item):
            if isinstance(item, dict):
                if "auto_map" in item:
                    raise NativeExecutionUnsupported("omnivoice_remote_code_unsupported")
                for nested in item.values():
                    reject_remote_code(nested)
            elif isinstance(item, list):
                for nested in item:
                    reject_remote_code(nested)
        reject_remote_code(value)
        return value
    except NativeExecutionUnsupported:
        raise
    except (OSError, ValueError, RecursionError):
        raise ExecutionPreparationError("omnivoice_local_configuration_invalid") from None


@dataclass(frozen=True)
class OmniVoiceLocalSelection:
    """Actual selected paths, NOT frozen files or a completeness certificate."""
    root: Path
    artifacts: tuple[SelectedArtifact, ...]
    parameters_json: str
    recipe: str = "omnivoice-local-single-safetensors-clone"
    recipe_version: int = 1

    @property
    def parameters(self) -> OmniVoiceLocalParameters:
        return OmniVoiceLocalParameters.model_validate_json(self.parameters_json)

    @property
    def loader_kwargs(self) -> dict:
        # Upstream's auxiliary loader still needs the staged tokenizer tree;
        # this alone cannot certify that the Python implementation is fixed.
        return {"local_files_only": True, "trust_remote_code": False,
                "load_asr": False, "device_map": self.parameters.device}

    def require_native_preparation(self) -> None:
        # A selected model tree never supplies runtime/host proof by itself.
        raise NativeExecutionUnsupported("execution_native_dependency_closure_unsupported")


def select_local_omnivoice(root: Path, parameters: OmniVoiceLocalParameters | None = None) -> OmniVoiceLocalSelection:
    """Select one supported on-disk model/tokenizer tree, without inference.

    First recipe intentionally rejects sharding/alternate weight formats and
    unknown files rather than guessing Transformers' weight precedence. Hub
    IDs, aliases and external tokenizer paths are not selections. Hashing/fixing
    the returned files is a subsequent preparation step, not implied here.
    """
    root = Path(root)
    if not root.is_absolute():
        raise ExecutionPreparationError("omnivoice_local_model_required")
    if root != root.resolve() or not root.is_dir():
        raise ExecutionPreparationError("omnivoice_local_model_required")
    if parameters is not None and not isinstance(parameters, OmniVoiceLocalParameters):
        raise ExecutionPreparationError("omnivoice_effective_parameters_invalid")
    parameters = OmniVoiceLocalParameters.model_validate_json(
        (parameters or OmniVoiceLocalParameters()).model_dump_json())
    required = {
        "config.json": "configuration", "model.safetensors": "weights",
        "tokenizer.json": "tokenizer", "tokenizer_config.json": "configuration",
        "audio_tokenizer/config.json": "configuration",
        "audio_tokenizer/model.safetensors": "auxiliary_weights",
        "audio_tokenizer/preprocessor_config.json": "configuration",
    }
    optional = {"generation_config.json": "configuration", "special_tokens_map.json": "tokenizer",
                "vocab.json": "tokenizer", "merges.txt": "tokenizer",
                "audio_tokenizer/generation_config.json": "configuration"}
    metadata = {"README.md", "LICENSE", "LICENSE.txt", ".gitattributes"}
    selected = {}
    try:
        # Only the two audited levels. No recursive discovery, imports or Hub.
        for directory in (root, root / "audio_tokenizer"):
            if directory != directory.resolve() or not directory.is_dir():
                raise ExecutionPreparationError("omnivoice_local_artifact_missing")
            for path in directory.iterdir():
                name = path.relative_to(root).as_posix()
                if name == "audio_tokenizer" and path.is_dir():
                    continue
                if path != path.resolve() or not stat.S_ISREG(path.lstat().st_mode):
                    raise NativeExecutionUnsupported("omnivoice_local_artifact_layout_unsupported")
                if path.name in metadata:
                    continue
                role = required.get(name) or optional.get(name)
                if role is None:
                    raise NativeExecutionUnsupported("omnivoice_local_artifact_layout_unsupported")
                if path.stat().st_size <= 0:
                    raise ExecutionPreparationError("omnivoice_local_artifact_missing")
                selected[name] = SelectedArtifact("model", role, name, path)
        if not required.keys() <= selected.keys():
            raise ExecutionPreparationError("omnivoice_local_artifact_missing")
        # Reject mixed slow-tokenizer files instead of falling back implicitly.
        if ("vocab.json" in selected) != ("merges.txt" in selected):
            raise ExecutionPreparationError("omnivoice_local_artifact_missing")
        configs = {name: _json_object(item.source) for name, item in selected.items()
                   if name.endswith(".json") and name != "tokenizer.json"}
        if configs["config.json"].get("model_type") != "omnivoice":
            raise NativeExecutionUnsupported("omnivoice_local_configuration_unsupported")
        if configs["audio_tokenizer/config.json"].get("model_type") != "higgs_audio_v2_tokenizer":
            raise NativeExecutionUnsupported("omnivoice_local_configuration_unsupported")
        tokenizer = configs["tokenizer_config.json"]
        if tokenizer.get("tokenizer_class") not in ("Qwen2Tokenizer", "Qwen2TokenizerFast"):
            raise NativeExecutionUnsupported("omnivoice_local_configuration_unsupported")
        if tokenizer.get("tokenizer_file", "tokenizer.json") not in (None, "tokenizer.json"):
            raise NativeExecutionUnsupported("omnivoice_external_tokenizer_unsupported")
    except (OSError, ValueError):
        raise ExecutionPreparationError("omnivoice_local_artifact_unavailable") from None
    return OmniVoiceLocalSelection(root, tuple(selected[name] for name in sorted(selected)),
        parameters.model_dump_json())
