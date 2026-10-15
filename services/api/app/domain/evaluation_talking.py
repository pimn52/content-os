"""Role-separated exact use of a saved Master for one evaluation TalkingRun."""
from typing import Literal, Annotated
from uuid import UUID, uuid4
from pydantic import BaseModel, ConfigDict, Field, AwareDatetime, JsonValue, model_validator, field_validator, TypeAdapter
from .models import (Sha256, ExecutionSpecificationV10, TalkingPerformanceBrief, TalkingReviewDimensions,
    TalkingReviewFinding, TalkingReviewConcernAnswer)
from .evaluation_master import MasterLicenseEvidence

POLICY = "evaluation-talking-run-v1"
CALIBRATION_POLICY = "evaluation-talking-calibration-v1"


class TalkingSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy: Literal["evaluation-talking-run-v1"] = POLICY
    purpose: Literal["internal_evaluation"] = "internal_evaluation"
    master_run_id: UUID
    scene_plan_id: UUID
    talking_profile_id: UUID
    reference_clip_id: UUID
    source_admission_id: UUID
    capability_profile_id: UUID
    execution_admission_id: UUID
    authorization_reference: str = Field(min_length=1, max_length=500)
    brief: TalkingPerformanceBrief
    use_admission_id: UUID | None = None


class TalkingStart(TalkingSelection):
    idempotency_key: str = Field(min_length=1, max_length=500)
    expected_fingerprint: Sha256


class TalkingCalibrationSelection(TalkingSelection):
    policy: Literal["evaluation-talking-calibration-v1"]
    trial_duration_limit_ms: int = Field(strict=True, gt=0)
    trial_processing_limit_ms: int = Field(strict=True, gt=0)

    @model_validator(mode="after")
    def finite_envelope(self):
        if self.trial_processing_limit_ms < self.trial_duration_limit_ms:
            raise ValueError("talking_calibration_processing_bound_invalid")
        return self


class TalkingCalibrationCandidate(TalkingCalibrationSelection):
    """Pre-adoption selection cannot name its own execution/use receipt."""
    execution_admission_id: None = None
    use_admission_id: None = None


class TalkingCalibrationIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selection: TalkingCalibrationCandidate
    execution_specification: ExecutionSpecificationV10


class TalkingCalibrationStart(TalkingCalibrationSelection):
    idempotency_key: str = Field(min_length=1, max_length=500)
    expected_fingerprint: Sha256


TalkingSelectionRecord = Annotated[TalkingSelection | TalkingCalibrationSelection, Field(discriminator="policy")]
TalkingStartRecord = Annotated[TalkingStart | TalkingCalibrationStart, Field(discriminator="policy")]


def parse_selection(value):
    return TypeAdapter(TalkingSelectionRecord).validate_json(value.model_dump_json())


class EvaluationTalkingReviewRequest(BaseModel):
    """Explicit policy2 exact-preview judgment; no default approval/version."""
    model_config = ConfigDict(extra="forbid")
    approved: bool
    review_policy_version: Literal[2]
    preview_asset_id: UUID
    preview_sha256: Sha256
    evidence_reference: str = Field(min_length=1, max_length=2000)
    findings: list[str] = Field(default_factory=list, max_length=100)
    dimensions: TalkingReviewDimensions | None = None
    scoped_findings: list[TalkingReviewFinding] = Field(default_factory=list, max_length=100)
    concern_answers: list[TalkingReviewConcernAnswer] = Field(default_factory=list, max_length=100)


class EvaluationTalkingConcernRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preview_asset_id: UUID
    preview_sha256: Sha256
    idempotency_key: str = Field(min_length=1, max_length=500)
    evidence_reference: str = Field(min_length=1, max_length=2000)
    finding: TalkingReviewFinding


class EvaluationTalkingConcernAnswersRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preview_asset_id: UUID
    preview_sha256: Sha256
    answers: list[TalkingReviewConcernAnswer] = Field(min_length=1, max_length=100)


class TalkingDependencyFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, str_strip_whitespace=True)
    dependency: str = Field(min_length=1, max_length=200)
    source: str = Field(min_length=1, max_length=2000)
    version: str = Field(min_length=1, max_length=200)
    terms_reference: str = Field(min_length=1, max_length=1000)
    terms_sha256: Sha256
    applicability: str = Field(min_length=1, max_length=2000)
    permits_intended_activity: Literal[True]

    @field_validator("permits_intended_activity", mode="before")
    @classmethod
    def exact_acceptance(cls, value):
        if value is not True: raise ValueError("evaluation_talking_explicit_acceptance_required")
        return value


class EvaluationTalkingUseReview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, str_strip_whitespace=True)
    policy: Literal["evaluation-talking-run-v1"]
    purpose: Literal["internal_evaluation"]
    operation: Literal["transform_master_to_talking"]
    local_only: Literal[True]
    project_id: UUID
    selection: TalkingSelection
    request_sha256: Sha256
    proof: dict[str, JsonValue]
    intended_activity: str = Field(min_length=1, max_length=2000)
    license_evidence: tuple[MasterLicenseEvidence, ...] = Field(min_length=1, max_length=100)
    dependency_findings: tuple[TalkingDependencyFinding, ...] = Field(min_length=3, max_length=100)
    findings: tuple[str, ...] = Field(min_length=1, max_length=20)

    _local = field_validator("local_only", mode="before")(TalkingDependencyFinding.exact_acceptance.__func__)

    @model_validator(mode="after")
    def exact_roles(self):
        if self.selection.use_admission_id is not None or any(not v.strip() or len(v) > 2000 for v in self.findings):
            raise ValueError("evaluation_talking_use_review_invalid")
        if (len({v.admission_id for v in self.license_evidence}) != len(self.license_evidence)
            or len({v.dependency for v in self.dependency_findings}) != len(self.dependency_findings)):
            raise ValueError("evaluation_talking_license_evidence_duplicate")
        return self


class EvaluationTalkingCalibrationUseReview(EvaluationTalkingUseReview):
    policy: Literal["evaluation-talking-calibration-v1"]
    operation: Literal["calibrate_master_to_talking_and_admit_exact_output"]
    selection: TalkingCalibrationSelection


TalkingUseReviewRecord = Annotated[EvaluationTalkingUseReview | EvaluationTalkingCalibrationUseReview, Field(discriminator="policy")]


class EvaluationTalkingUseAdmission(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=500)
    review_reference: str = Field(min_length=1, max_length=1000)
    review_sha256: Sha256
    review: TalkingUseReviewRecord
    actor_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,100}$")
    created_at: AwareDatetime
    revoked_at: AwareDatetime | None = None
    revoked_by: str | None = None
    revocation_reason: str | None = None

    @model_validator(mode="after")
    def consistent(self):
        if self.project_id != self.review.project_id:
            raise ValueError("evaluation_talking_project_mismatch")
        values = (self.revoked_at, self.revoked_by, self.revocation_reason)
        if any(v is not None for v in values) and not all(v is not None for v in values):
            raise ValueError("evaluation_talking_revocation_invalid")
        return self


class EvaluationTalkingOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    id: UUID = Field(default_factory=uuid4)
    policy: Literal["evaluation-talking-run-v1"] = POLICY
    purpose: Literal["internal_evaluation"] = "internal_evaluation"
    review_policy_version: Literal[2] = 2
    project_id: UUID
    master_run_id: UUID
    request_sha256: Sha256
    # Excludes alias parent identity, includes immutable Master/source identity.
    inference_sha256: Sha256
    request: dict[str, JsonValue]
    proof: dict[str, JsonValue]
    selection: TalkingSelection
    admission_id: UUID
    admission_sha256: Sha256
    execution_scope_fingerprint: Sha256
    output_scope_fingerprint: Sha256
    generation_job_id: UUID
    qa_job_id: UUID
    preview_job_id: UUID
    series_id: UUID
    created_at: AwareDatetime

    @model_validator(mode="after")
    def exact_owner(self):
        if (self.master_run_id != self.selection.master_run_id or self.admission_id != self.selection.use_admission_id
            or len({self.generation_job_id, self.qa_job_id, self.preview_job_id}) != 3):
            raise ValueError("evaluation_talking_operation_invalid")
        return self


class EvaluationTalkingCalibrationOperation(EvaluationTalkingOperation):
    policy: Literal["evaluation-talking-calibration-v1"]
    selection: TalkingCalibrationSelection

    @model_validator(mode="after")
    def calibration_roles(self):
        if self.request.get("policy") != self.policy or self.request.get("selection") != self.selection.model_dump(mode="json", exclude={"use_admission_id"}):
            raise ValueError("talking_calibration_operation_policy_changed")
        return self


TalkingOperationRecord = Annotated[EvaluationTalkingOperation | EvaluationTalkingCalibrationOperation, Field(discriminator="policy")]


class EvaluationTalkingGenerationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    result_version: Literal[1] = 1
    operation_id: UUID
    job_id: UUID
    asset_id: UUID
    asset_sha256: Sha256
    asset_payload_sha256: Sha256
    proof_sha256: Sha256
    execution_sha256: Sha256
    output_scope_fingerprint: Sha256
    provider_call_key: str = Field(min_length=1, max_length=500)


class EvaluationTalkingQaResult(BaseModel):
    """Immutable technical projection; no human judgment or inference grant."""
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    result_version: Literal[1] = 1
    operation_id: UUID
    job_id: UUID
    asset_id: UUID
    asset_sha256: Sha256
    generation_result_sha256: Sha256
    asset_payload_sha256: Sha256
    qa_state: Literal["verified", "failed"]
    qa: dict[str, JsonValue]

    @model_validator(mode="after")
    def exact_projection(self):
        if (self.qa.get("job_id") != str(self.job_id)
            or type(self.qa.get("automated_verified")) is not bool
            or self.qa["automated_verified"] != (self.qa_state == "verified")):
            raise ValueError("evaluation_talking_qa_projection_invalid")
        return self


class EvaluationTalkingPreviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    result_version: Literal[1] = 1
    operation_id: UUID
    job_id: UUID
    series_id: UUID
    origin_sha256: Sha256
    qa_result_sha256: Sha256
    asset_id: UUID
    asset_sha256: Sha256
    asset_payload_sha256: Sha256
    output_scope_fingerprint: Sha256


class EvaluationTalkingAdmissionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    result_version: Literal[1] = 1
    operation_id: UUID
    series_id: UUID
    preview_result_sha256: Sha256
    review_id: UUID
    review_sha256: Sha256
    run_id: UUID
    run_sha256: Sha256
    clip_id: UUID
    clip_sha256: Sha256
    asset_id: UUID
    asset_payload_sha256: Sha256


class TalkingExactQualityObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    observation_version: Literal[1]
    policy: Literal["evaluation-talking-calibration-v1"]
    scope: Literal["exact_sample_only"] = "exact_sample_only"
    observed_pass_duration_ms: int = Field(strict=True, gt=0)
    trial_input_sha256: Sha256
    execution_sha256: Sha256
    configuration_sha256: Sha256
    operation_sha256: Sha256
    generation_result_sha256: Sha256
    qa_result_sha256: Sha256
    preview_sha256: Sha256
    provider_call_sha256: Sha256
    use_admission_sha256: Sha256
    output_scope_fingerprint: Sha256
    review_sha256: Sha256
    run_sha256: Sha256

    @field_validator("observation_version", mode="before")
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int:
            raise ValueError("talking_calibration_observation_version_invalid")
        return value


class EvaluationTalkingCalibrationAdmissionResult(EvaluationTalkingAdmissionResult):
    result_version: Literal[2] = 2
    quality_observation: TalkingExactQualityObservation

    @model_validator(mode="before")
    @classmethod
    def exact_version(cls, value):
        if isinstance(value,dict) and type(value.get("result_version",2)) is not int:
            raise ValueError("talking_calibration_result_version_invalid")
        return value


TalkingAdmissionResultRecord = Annotated[EvaluationTalkingAdmissionResult | EvaluationTalkingCalibrationAdmissionResult, Field(discriminator="result_version")]
