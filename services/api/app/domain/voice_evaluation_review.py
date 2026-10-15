"""An asset-specific human judgment, separate from immutable technical QA."""
import json
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4
from pydantic import BaseModel, ConfigDict, Field
from app.domain.models import Sha256, VoiceHumanReview, VoiceReviewOutcome, execution_canonical_sha256

POLICY = "voice-evaluation-human-review-v1"


class EvaluationVoiceReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    policy: Literal["voice-evaluation-human-review-v1"]
    purpose: Literal["internal_evaluation"]
    expected_qa_fingerprint: Sha256
    approved: bool
    evidence_reference: str = Field(min_length=1, max_length=500)
    findings: list[str] = Field(min_length=1, max_length=100)
    likeness: VoiceReviewOutcome = Field(strict=False)
    naturalness: VoiceReviewOutcome = Field(strict=False)
    emphasis: VoiceReviewOutcome = Field(strict=False)
    pace: VoiceReviewOutcome = Field(strict=False)
    pauses: VoiceReviewOutcome = Field(strict=False)
    rhythm: VoiceReviewOutcome = Field(strict=False)

    def review(self, now: datetime) -> VoiceHumanReview:
        return VoiceHumanReview(**self.model_dump(exclude={"policy", "purpose", "expected_qa_fingerprint"}), reviewed_at=now)

    def submission_digest(self, project_id: UUID, audio_id: UUID) -> str:
        return execution_canonical_sha256(dict(project_id=str(project_id), audio_id=str(audio_id), **self.model_dump(mode="json")))


class EvaluationVoiceReviewRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    id: UUID = Field(default_factory=uuid4)
    policy: Literal["voice-evaluation-human-review-v1"] = POLICY
    project_id: UUID
    audio_id: UUID
    audio_sha256: Sha256
    qa_job_id: UUID
    qa_claim_sha256: Sha256
    qa_call_id: UUID
    qa_result_sha256: Sha256
    projection_sha256: Sha256
    submission_sha256: Sha256
    review: VoiceHumanReview

    def binding(self) -> dict:
        return self.model_dump(mode="json", exclude={"id", "review", "submission_sha256"})

    def validate_submission(self) -> None:
        body = self.review.model_dump(mode="json", exclude={"schema_version", "reviewed_at"})
        value = EvaluationVoiceReviewInput.model_validate_json(json.dumps(dict(
            policy=self.policy, purpose="internal_evaluation",
            expected_qa_fingerprint=execution_canonical_sha256(self.binding()), **body)))
        if value.submission_digest(self.project_id, self.audio_id) != self.submission_sha256:
            raise ValueError("voice_human_review_submission_changed")
