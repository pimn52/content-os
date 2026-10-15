"""Operator-attested administrative closure, not a call/refund grant."""
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field
from .models import Sha256
from .evaluation_talking import TalkingCalibrationSelection, TalkingCalibrationStart
from .maintained_runtime import MaintainedEvidenceReference

RECOVERY_POLICY = "evaluation-talking-no-dispatch-continuation-v1"


class TalkingRecoveryReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    policy: Literal["evaluation-talking-no-dispatch-continuation-v1"]
    project_id: UUID
    batch_id: UUID
    predecessor_id: UUID
    predecessor_sha256: Sha256
    selection: TalkingCalibrationSelection
    fingerprint: Sha256
    closure_evidence: MaintainedEvidenceReference
    findings: tuple[str, ...] = Field(min_length=1, max_length=10)


class TalkingRecoveryPreflight(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selection: TalkingCalibrationSelection


class TalkingRecoveryStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: TalkingCalibrationStart
    review_reference: str = Field(min_length=1, max_length=1000)
    review_sha256: Sha256
