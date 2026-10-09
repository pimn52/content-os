"""Explicit PCM-only recipe; not a native execution admission."""
from dataclasses import dataclass
from typing import Literal

from pydantic import Field

from .omnivoice_audio_pcm import CONVERSION, TRANSFORM, MAX_BYTES, MAX_FRAMES
from .omnivoice_recipe_v2 import (OmniVoiceLocalParametersV2,
    OmniVoiceLocalSelectionV2, select_local_omnivoice_v2)
from .prepared import ExecutionPreparationError


class OmniVoiceLocalParametersV3(OmniVoiceLocalParametersV2):
    parameter_version: Literal[3]
    audio_transform: Literal[TRANSFORM]
    pcm_conversion: Literal[CONVERSION]
    output_max_frames: int = Field(gt=0, le=MAX_FRAMES)
    output_max_bytes: int = Field(gt=44, le=MAX_BYTES)

    def generation_kwargs(self):
        return self.model_dump(exclude={
            "parameter_version", "device", "precision", "audio_tokenizer_precision",
            "rotary_buffer_precision", "attention_backend", "seed", "output_format",
            "output_subtype", "audio_transform", "pcm_conversion", "output_max_frames",
            "output_max_bytes"})


@dataclass(frozen=True)
class OmniVoiceLocalSelectionV3(OmniVoiceLocalSelectionV2):
    recipe: str = "omnivoice-local-cpu-pcm"
    recipe_version: int = 3

    @property
    def parameters(self):
        return OmniVoiceLocalParametersV3.model_validate_json(self.parameters_json)


def select_local_omnivoice_v3(root, parameters):
    if type(parameters) is not OmniVoiceLocalParametersV3:
        raise ExecutionPreparationError("omnivoice_effective_parameters_invalid")
    params = OmniVoiceLocalParametersV3.model_validate_json(parameters.model_dump_json())
    old = params.model_dump(exclude={"audio_transform", "pcm_conversion",
        "output_max_frames", "output_max_bytes"}) | {"parameter_version": 2}
    selected = select_local_omnivoice_v2(root, OmniVoiceLocalParametersV2.model_validate(old))
    return OmniVoiceLocalSelectionV3(selected.root, selected.artifacts,
        params.model_dump_json(), selected.sampling_rate)
