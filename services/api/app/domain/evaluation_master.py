"""Exact completed-output use; deliberately not a provider execution grant."""
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator
from app.domain.models import Sha256, VisualStyleTokens

POLICY = "evaluation-run-voice-master-v1"
OPERATION = "bind_existing_voice_master"


class MasterSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy: Literal["evaluation-run-voice-master-v1"]
    purpose: Literal["internal_evaluation"]
    audio_id: UUID
    style_tokens: VisualStyleTokens = Field(default_factory=VisualStyleTokens)
    talking_review_policy_version: Literal[1, 2] = 2
    admission_id: UUID | None = None


class MasterStart(MasterSelection):
    idempotency_key: str = Field(min_length=1, max_length=500)
    expected_fingerprint: Sha256


class MasterLicenseEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, str_strip_whitespace=True)
    admission_id: UUID
    source: str = Field(min_length=1, max_length=2000)
    version: str = Field(min_length=1, max_length=200)
    terms_reference: str = Field(min_length=1, max_length=1000)
    terms_sha256: Sha256
    applicability: str = Field(min_length=1, max_length=2000)
    permits_intended_activity: Literal[True]


class EvaluationMasterUseReviewV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, str_strip_whitespace=True)
    policy: Literal["evaluation-run-voice-master-v1"]
    purpose: Literal["internal_evaluation"]
    operation: Literal["bind_existing_voice_master"]
    local_only: Literal[True]
    project_id: UUID
    selection: MasterSelection
    master_use_request_sha256: Sha256
    # Exact server-produced proof; adoption reconstructs and compares every field.
    proof: dict[str, JsonValue]
    intended_activity: str = Field(min_length=1, max_length=2000)
    license_evidence: tuple[MasterLicenseEvidence, ...] = Field(min_length=1, max_length=100)
    findings: tuple[str, ...] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def exact_consumer(self):
        if self.selection.admission_id is not None or any(not x.strip() or len(x) > 2000 for x in self.findings):
            raise ValueError("evaluation_master_use_review_invalid")
        if len({(x.admission_id, x.terms_reference) for x in self.license_evidence}) != len(self.license_evidence):
            raise ValueError("evaluation_master_license_evidence_duplicate")
        return self


class EvaluationMasterUseAdmission(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=500)
    review_reference: str = Field(min_length=1, max_length=1000)
    review_sha256: Sha256
    review: EvaluationMasterUseReviewV1
    actor_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,100}$")
    created_at: AwareDatetime
    revoked_at: AwareDatetime | None = None
    revoked_by: str | None = None
    revocation_reason: str | None = None

    @model_validator(mode="after")
    def consistent(self):
        if self.project_id != self.review.project_id:
            raise ValueError("evaluation_master_use_project_mismatch")
        values = (self.revoked_at, self.revoked_by, self.revocation_reason)
        if any(x is not None for x in values) and not all(x is not None for x in values):
            raise ValueError("evaluation_master_use_revocation_invalid")
        return self


class EvaluationRunMasterBindingV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    run_id: UUID
    project_id: UUID
    policy: Literal["evaluation-run-voice-master-v1"] = POLICY
    purpose: Literal["internal_evaluation"] = "internal_evaluation"
    request_sha256: Sha256
    request: dict[str, JsonValue]
    admission_id: UUID
    admission_sha256: Sha256
    selection: MasterSelection
    proof: dict[str, JsonValue]
    scope_fingerprint: Sha256
    reason: Literal["unchanged_complete_approved_narration"] = "unchanged_complete_approved_narration"


class MasterAdoption(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=1, max_length=500)
    review_reference: str = Field(min_length=1, max_length=1000)
    review_sha256: Sha256


class MasterRevocation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    reason: str = Field(min_length=1, max_length=1000)
