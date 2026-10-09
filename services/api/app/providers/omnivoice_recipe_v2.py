"""Explicit CPU component recipe. Selection never imports a model runtime."""
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .omnivoice_local import OmniVoiceLocalParameters, _json_object, select_local_omnivoice
from .prepared import ExecutionPreparationError, NativeExecutionUnsupported


class OmniVoiceLocalParametersV2(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)
    parameter_version: Literal[2]
    device: Literal["cpu"]
    precision: Literal["float16"]
    audio_tokenizer_precision: Literal["float32"]
    rotary_buffer_precision: Literal["float32"]
    attention_backend: Literal["eager"]
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
    output_format: Literal["WAV"] = "WAV"
    output_subtype: Literal["PCM_16"] = "PCM_16"

    @field_validator("parameter_version", mode="before")
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise ValueError("omnivoice_parameter_version_invalid")
        return value

    def generation_kwargs(self):
        return self.model_dump(exclude={"parameter_version", "device", "precision",
            "audio_tokenizer_precision", "rotary_buffer_precision", "attention_backend",
            "seed", "output_format", "output_subtype"})


@dataclass(frozen=True)
class OmniVoiceLocalSelectionV2:
    root: Path
    artifacts: tuple
    parameters_json: str
    sampling_rate: int
    recipe: str = "omnivoice-local-cpu-components"
    recipe_version: int = 2

    @property
    def parameters(self):
        return OmniVoiceLocalParametersV2.model_validate_json(self.parameters_json)

    def require_native_preparation(self):
        raise NativeExecutionUnsupported("execution_native_dependency_closure_unsupported")


def select_local_omnivoice_v2(root, parameters):
    if type(parameters) is not OmniVoiceLocalParametersV2:
        raise ExecutionPreparationError("omnivoice_effective_parameters_invalid")
    parameters = OmniVoiceLocalParametersV2.model_validate_json(parameters.model_dump_json())
    # Reuse only the old bounded artifact layout; do not reinterpret V1 parameters.
    selected = select_local_omnivoice(root, OmniVoiceLocalParameters())
    config = _json_object(selected.root / "config.json")
    llm = config.get("llm_config", {})
    rope = llm.get("rope_parameters", {}) if isinstance(llm, dict) else {}
    if (not isinstance(llm, dict) or llm.get("model_type") != "qwen3"
            or not isinstance(rope, dict) or rope.get("rope_type") != "default"
            or llm.get("rope_scaling") not in (None, {})):
        raise NativeExecutionUnsupported("omnivoice_component_configuration_unsupported")
    rate = _json_object(selected.root / "audio_tokenizer/preprocessor_config.json").get("sampling_rate")
    if type(rate) is not int or not 0 < rate <= 192_000:
        raise ExecutionPreparationError("omnivoice_reference_rate_invalid")
    return OmniVoiceLocalSelectionV2(selected.root, selected.artifacts,
        parameters.model_dump_json(), rate)


def parse_local_parameters(values):
    """Explicit dispatch, with old V1 parsing/serialization left intact."""
    if "parameter_version" in values:
        if values["parameter_version"] == 3:
            from .omnivoice_recipe_v3 import OmniVoiceLocalParametersV3
            return OmniVoiceLocalParametersV3.model_validate(values)
        return OmniVoiceLocalParametersV2.model_validate(values)
    return OmniVoiceLocalParameters.model_validate(values)


def select_parameterized_model(root, parameters):
    from .omnivoice_recipe_v3 import OmniVoiceLocalParametersV3, select_local_omnivoice_v3
    if type(parameters) is OmniVoiceLocalParametersV3:
        return select_local_omnivoice_v3(root, parameters)
    if type(parameters) is OmniVoiceLocalParametersV2:
        return select_local_omnivoice_v2(root, parameters)
    if type(parameters) is OmniVoiceLocalParameters:
        return select_local_omnivoice(root, parameters)
    raise ExecutionPreparationError("omnivoice_effective_parameters_invalid")
