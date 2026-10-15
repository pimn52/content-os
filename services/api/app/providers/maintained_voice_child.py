"""Fixed ZIP application child; caller must reserve/admit before invocation."""
from pathlib import Path
import sys

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .maintained_voice import MaintainedVoiceParameters, execute_maintained_clone
from .omnivoice_entry_v2 import LocalCloneInputV2
from .prepared import ExecutionPreparationError
from .runtime_tree import _exact_path

REQUEST_MAX_BYTES = 256 * 1024


class MaintainedVoiceChildRequest(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    data_root: str = Field(min_length=1, max_length=4096)
    model_root: str = Field(min_length=1, max_length=4096)
    parameters: MaintainedVoiceParameters
    inputs: LocalCloneInputV2

    @model_validator(mode="after")
    def bounded_inputs(self):
        limits = {"text": 65536, "reference_text": 65536, "language": 100,
            "reference_audio": 4096, "output_path": 4096}
        if any(len(getattr(self.inputs, name)) > limit for name, limit in limits.items()):
            raise ValueError("maintained_voice_input_limit")
        return self


def request_bytes(request):
    raw = request.model_dump_json().encode("utf-8")
    if len(raw) > REQUEST_MAX_BYTES:
        raise ExecutionPreparationError("maintained_voice_request_limit")
    return raw


def main():
    # No arbitrary command, source directory, environment or loader argument.
    try:
        if len(sys.argv) != 2:
            raise ValueError()
        path = Path(sys.argv[1])
        _exact_path(path, directory=False)
        if path.parent != Path.cwd() or path.name != "request.json" or path.stat().st_size > REQUEST_MAX_BYTES:
            raise ValueError()
        raw = path.read_bytes()
        request = MaintainedVoiceChildRequest.model_validate_json(raw)
        # Canonical owned request also rejects duplicate JSON fields.
        if request_bytes(request) != raw:
            raise ValueError()
        execute_maintained_clone(data_root=request.data_root, model_root=request.model_root,
            parameters=request.parameters.model_dump(), inputs=request.inputs)
        return 0
    except Exception:
        # Parent receives a fixed failure code; no traceback, text or credentials.
        return 1
