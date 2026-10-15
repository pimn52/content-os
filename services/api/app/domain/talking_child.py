"""Owned Talking invocation inputs, independent of use/consent admission."""
from pathlib import Path
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field
from app.domain.models import AudioAsset, TalkingProfile, Sha256
from app.talking.slices import TalkingAudioSlicePlan, plan_talking_audio_slice


class TalkingChildRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    request_version: int = Field(default=1, ge=1, le=1)
    profile: TalkingProfile
    narration: AudioAsset
    reference_clip_id: UUID
    reference_path: str = Field(min_length=1, max_length=1000)
    reference_sha256: Sha256
    reference_start_ms: int = Field(ge=0)
    reference_end_ms: int = Field(gt=0)
    slice_plan: TalkingAudioSlicePlan | None = None

    def verify_inputs(self, data_root):
        from app.providers.runtime_tree import _exact_path, _digest
        from app.providers.prepared import ExecutionPreparationError
        root = Path(data_root)
        for name, expected in ((self.narration.source_file, self.narration.content_hash),
                               (self.reference_path, self.reference_sha256)):
            path = Path(name)
            if not path.is_absolute() or not path.is_relative_to(root):
                raise ExecutionPreparationError("maintained_talking_input_boundary_invalid")
            _exact_path(path, directory=False)
            if _digest(path, 4 * 1024**3)[1] != expected:
                raise ExecutionPreparationError("maintained_talking_input_changed")
        if self.reference_end_ms <= self.reference_start_ms or not self.profile.consent.confirmed:
            raise ExecutionPreparationError("maintained_talking_request_invalid")
        if self.slice_plan is not None:
            plan = self.slice_plan
            expected = plan_talking_audio_slice(self.narration,
                start_segment_index=plan.start_segment_index, end_segment_index=plan.end_segment_index,
                max_duration_ms=plan.duration_ms)
            if expected != plan or self.reference_end_ms-self.reference_start_ms < plan.duration_ms:
                raise ExecutionPreparationError("maintained_talking_slice_changed")
