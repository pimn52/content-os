"""Trusted local adoption of an exact assisted Talking-source review claim.

This receipt is reusable evidence, never a generation authorization by itself.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.db import Database
from app.talking.source_suitability import TalkingSourceSuitabilityAssessment, TalkingSourceSuitabilityRepository


_ACTOR = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def configured_review_actor(supplied_key: str | None) -> str | None:
    """Resolve a distinct configured local reviewer credential, never a body claim.

    The environment contains only SHA-256 digests of high-entropy keys. A
    missing or malformed trust root never falls back to general API access.
    """
    configured = os.environ.get("CONTENT_OS_TALKING_REVIEW_KEY_HASHES")
    if not configured or not supplied_key:
        return None
    try:
        values = json.loads(configured)
    except json.JSONDecodeError:
        return None
    if (not isinstance(values, dict) or not values
        or any(not isinstance(actor, str) or not _ACTOR.fullmatch(actor)
               or not isinstance(digest, str) or not _SHA256.fullmatch(digest)
               for actor, digest in values.items())
        or len(set(values.values())) != len(values)):
        return None
    if len(supplied_key) < 32:
        return None
    digest = hashlib.sha256(supplied_key.encode("utf-8")).hexdigest()
    matches = [actor for actor, expected in values.items() if secrets.compare_digest(digest, expected)]
    return matches[0] if len(matches) == 1 else None


def legacy_talking_evaluation_enabled() -> bool:
    """Explicit local evaluation escape hatch for pre-ProductionRun Talking APIs."""
    return os.environ.get("CONTENT_OS_ALLOW_LEGACY_TALKING_EVALUATION") == "1"


class TalkingSourceAdmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID = Field(default_factory=uuid4)
    idempotency_key: str = Field(min_length=1, max_length=500)
    assessment_id: UUID
    assessment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    actor_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,100}$")
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    revoked_at: AwareDatetime | None = None
    revoked_by: str | None = None
    revocation_reason: str | None = None

    @property
    def active(self) -> bool:
        return self.revoked_at is None


class TalkingSourceAdmissionRepository:
    def __init__(self, db: Database, data_root: str | Path):
        self.db = db
        self.data_root = Path(data_root).resolve()
        self.assessments = TalkingSourceSuitabilityRepository(db, self.data_root)

    def get(self, admission_id: UUID) -> TalkingSourceAdmission | None:
        row = self.db.connection.execute(
            "SELECT payload FROM talking_source_admissions WHERE id = ?", (str(admission_id),),
        ).fetchone()
        return None if row is None else TalkingSourceAdmission.model_validate_json(row["payload"])

    def get_for_assessment(self, assessment_id: UUID) -> TalkingSourceAdmission | None:
        row = self.db.connection.execute(
            "SELECT payload FROM talking_source_admissions WHERE assessment_id = ?", (str(assessment_id),),
        ).fetchone()
        return None if row is None else TalkingSourceAdmission.model_validate_json(row["payload"])

    def admit(self, assessment_id: UUID, *, idempotency_key: str, actor_id: str,
              evidence_sha256: str) -> TalkingSourceAdmission:
        if not _ACTOR.fullmatch(actor_id) or not _SHA256.fullmatch(evidence_sha256):
            raise ValueError("Talking source admission actor or evidence digest is invalid")
        assessment = self.assessments.get(assessment_id)
        if assessment is None or assessment.decision != "review_claimed_suitable":
            raise ValueError("Talking source admission requires a complete assisted positive claim")
        self.assessments.require_current_source(assessment)
        observed_hash = self._evidence_hash(assessment.evidence_reference)
        if observed_hash != evidence_sha256:
            raise ValueError("Talking source review evidence bytes changed or digest differs")
        assessment_hash = self._assessment_hash(assessment)
        existing = self.get_for_assessment(assessment_id)
        if existing is not None:
            if (not existing.active or existing.idempotency_key != idempotency_key
                or existing.actor_id != actor_id or existing.evidence_sha256 != evidence_sha256
                or existing.assessment_sha256 != assessment_hash):
                raise ValueError("Talking source assessment already has a different or revoked admission")
            return existing
        if self.db.connection.execute(
            "SELECT 1 FROM talking_source_admissions WHERE idempotency_key = ?", (idempotency_key,),
        ).fetchone():
            raise ValueError("Talking source admission idempotency key belongs to different evidence")
        admission = TalkingSourceAdmission(
            idempotency_key=idempotency_key, assessment_id=assessment_id,
            assessment_sha256=assessment_hash, evidence_sha256=evidence_sha256,
            actor_id=actor_id,
        )
        self.db.connection.execute(
            "INSERT INTO talking_source_admissions(id, idempotency_key, assessment_id, payload) VALUES (?, ?, ?, ?)",
            (str(admission.id), idempotency_key, str(assessment_id), admission.model_dump_json()),
        )
        return admission

    def require_current(self, admission: TalkingSourceAdmission) -> TalkingSourceSuitabilityAssessment:
        if not admission.active:
            raise ValueError("Talking source admission has been revoked")
        assessment = self.assessments.get(admission.assessment_id)
        if (assessment is None or assessment.decision != "review_claimed_suitable"
            or self._assessment_hash(assessment) != admission.assessment_sha256):
            raise ValueError("Talking source assessment changed or is no longer positive")
        self.assessments.require_current_source(assessment)
        if self._evidence_hash(assessment.evidence_reference) != admission.evidence_sha256:
            raise ValueError("Talking source review evidence bytes changed")
        return assessment

    def revoke(self, admission_id: UUID, *, actor_id: str, reason: str) -> TalkingSourceAdmission | None:
        if not _ACTOR.fullmatch(actor_id) or not reason.strip():
            raise ValueError("Talking source admission revocation requires an actor and reason")
        admission = self.get(admission_id)
        if admission is None:
            return None
        if not admission.active:
            return admission
        revoked = admission.model_copy(update={
            "revoked_at": datetime.now(timezone.utc), "revoked_by": actor_id,
            "revocation_reason": reason.strip(),
        })
        self.db.connection.execute(
            "UPDATE talking_source_admissions SET payload = ? WHERE id = ?",
            (revoked.model_dump_json(), str(admission_id)),
        )
        return revoked

    def _evidence_hash(self, reference: str) -> str:
        source = self.evidence_path(reference)
        with source.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()

    def evidence_path(self, reference: str) -> Path:
        """Resolve only an existing evidence file inside the configured data root."""
        path = Path(reference)
        if path.is_absolute():
            raise ValueError("Talking source review evidence must use a portable data-root path")
        parts = path.parts
        source = (self.data_root.parent / path if parts and parts[0].casefold() == self.data_root.name.casefold()
                  else self.data_root / path).resolve()
        if not source.is_relative_to(self.data_root) or not source.is_file():
            raise ValueError("Talking source review evidence file is unavailable inside data root")
        return source

    @staticmethod
    def _assessment_hash(assessment: TalkingSourceSuitabilityAssessment) -> str:
        encoded = json.dumps(assessment.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
