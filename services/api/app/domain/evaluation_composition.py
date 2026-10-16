"""Exact retained-media composition; no provider or commercial permission."""
from typing import Literal
from uuid import UUID, uuid4
from pydantic import BaseModel, ConfigDict, Field, AwareDatetime, JsonValue, model_validator, field_validator
from .models import Sha256, VisualStyleTokens
from .evaluation_master import MasterAdoption, MasterLicenseEvidence
from .evaluation_talking import TalkingDependencyFinding

POLICY = "evaluation-composition-v1"


class CompositionSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy: Literal["evaluation-composition-v1"] = POLICY
    purpose: Literal["internal_evaluation"] = "internal_evaluation"
    master_run_id: UUID | None = None
    master_audio_id: UUID | None = None
    style_tokens: VisualStyleTokens = Field(default_factory=VisualStyleTokens)
    # Absence is an explicit missing dependency, never permission to generate.
    talking_runs: dict[UUID, UUID] = Field(default_factory=dict, max_length=100)
    tail: Literal["neutral_background"] = "neutral_background"

    @model_validator(mode="after")
    def one_master(self):
        if (self.master_run_id is None) == (self.master_audio_id is None):
            raise ValueError("composition_one_master_required")
        return self

    @property
    def parent_id(self):
        return self.master_run_id or self.master_audio_id


class CompositionBatchReview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    policy: Literal["evaluation-composition-v1"]
    project_id: UUID
    master_run_id: UUID | None = None
    master_audio_id: UUID | None = None
    authorization_reference: str = Field(min_length=1, max_length=1000)
    authorization_sha256: Sha256
    scene_ids: tuple[UUID, ...] = Field(max_length=4)
    max_renders: Literal[1]
    findings: tuple[str, ...] = Field(min_length=1, max_length=20)

    @field_validator("max_renders",mode="before")
    @classmethod
    def integer_allowance(cls,value):
        if type(value) is not int or value != 1:
            raise ValueError("composition_integer_one_render_required")
        return value

    @model_validator(mode="after")
    def unique_scenes(self):
        if (self.master_run_id is None) == (self.master_audio_id is None):
            raise ValueError("composition_one_master_required")
        if len(set(self.scene_ids)) != len(self.scene_ids) or any(not x.strip() for x in self.findings):
            raise ValueError("composition_batch_invalid")
        return self

    @property
    def parent_id(self):
        return self.master_run_id or self.master_audio_id


class CompositionUseReview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    policy: Literal["evaluation-composition-v1"]
    purpose: Literal["internal_evaluation"]
    operation: Literal["compose_retained_media_and_review"]
    local_only: Literal[True]
    project_id: UUID
    selection: CompositionSelection
    request_sha256: Sha256
    proof: dict[str, JsonValue]
    intended_activity: str = Field(min_length=1, max_length=2000)
    license_evidence: tuple[MasterLicenseEvidence, ...] = Field(default=(), max_length=100)
    dependency_findings: tuple[TalkingDependencyFinding, ...] = Field(min_length=1, max_length=100)
    findings: tuple[str, ...] = Field(min_length=1, max_length=20)

    @field_validator("local_only",mode="before")
    @classmethod
    def local_activity(cls,value):
        if value is not True:
            raise ValueError("composition_explicit_local_activity_required")
        return value

    @field_validator("license_evidence",mode="before")
    @classmethod
    def explicit_license_findings(cls,values):
        for item in values:
            if (item.get("permits_intended_activity") if isinstance(item,dict) else item.permits_intended_activity) is not True:
                raise ValueError("composition_explicit_license_findings_required")
        return tuple(values)

    @model_validator(mode="after")
    def explicit_activity(self):
        if self.local_only is not True or any(not s.strip() for s in self.findings):
            raise ValueError("composition_use_review_invalid")
        if len({(v.admission_id, v.terms_reference) for v in self.license_evidence}) != len(self.license_evidence):
            raise ValueError("composition_license_evidence_duplicate")
        if len({v.dependency for v in self.dependency_findings}) != len(self.dependency_findings):
            raise ValueError("composition_dependency_evidence_duplicate")
        return self


class CompositionAdmission(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    idempotency_key: str
    review_reference: str
    review_sha256: Sha256
    review: CompositionUseReview
    actor_id: str
    created_at: AwareDatetime
    revoked_at: AwareDatetime | None = None
    revoked_by: str | None = None
    revocation_reason: str | None = None


CompositionAdoption = MasterAdoption
