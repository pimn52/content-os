"""Versioned Talking judgment and append-only, exact-subject concerns.

Capability/source/technical admission stays with the owning application services.
This module never converts an absent human decision into approval.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from app.db import Database
from app.domain.models import (
    TalkingReviewConcern, TalkingReviewConcernAnswer, TalkingReviewDimensions,
    TalkingReviewFinding, TalkingSliceSeriesContinuityReview,
)


class TalkingReviewPolicyError(ValueError):
    pass


def require_child_judgment(generation: dict, version: int) -> None:
    state, human = generation.get("human_review_state"), generation.get("human_review")
    if version not in {1, 2}:
        raise TalkingReviewPolicyError("talking_review_policy_unknown")
    if state == "rejected" or isinstance(human, dict) and human.get("approved") is False:
        raise TalkingReviewPolicyError("talking_child_review_rejected")
    approved = (state == "approved" and isinstance(human, dict)
                and human.get("approved") is True and bool(human.get("evidence_reference")))
    if version == 1:
        if not approved:
            raise TalkingReviewPolicyError("planned_talking_child_review_not_approved")
    elif not approved and not ((state is None or state == "pending") and human is None):
        raise TalkingReviewPolicyError("talking_child_review_state_unknown_or_inconsistent")


class TalkingReviewPolicyService:
    def __init__(self, db: Database):
        self.db = db

    def concerns(self, project_id: UUID, subject_sha256: str) -> list[TalkingReviewConcern]:
        # Subject hashes, not collection IDs, retain a concern across aliases.
        rows = self.db.connection.execute(
            "SELECT payload FROM talking_review_concerns WHERE project_id = ? ORDER BY id", (str(project_id),),
        ).fetchall()
        return [concern for row in rows
                if (concern := TalkingReviewConcern.model_validate_json(row["payload"])).subject_sha256 == subject_sha256]

    def answer(self, concern_id: UUID) -> TalkingReviewConcernAnswer | None:
        row = self.db.connection.execute(
            "SELECT payload FROM talking_review_concern_answers WHERE concern_id = ?", (str(concern_id),),
        ).fetchone()
        return None if row is None else TalkingReviewConcernAnswer.model_validate_json(row["payload"])

    def blockers(self, project_id: UUID, subject_sha256: str, *, pending: bool = True) -> list[str]:
        reasons = []
        for concern in self.concerns(project_id, subject_sha256):
            answer = self.answer(concern.id)
            if answer is None and pending:
                reasons.append(f"talking_local_review_pending:{concern.id}")
            elif answer is not None and not answer.approved:
                reasons.append(f"talking_local_review_rejected:{concern.id}")
        for row in self.db.connection.execute(
            "SELECT payload FROM talking_slice_series_continuity_reviews WHERE project_id = ?", (str(project_id),),
        ).fetchall():
            review = TalkingSliceSeriesContinuityReview.model_validate_json(row["payload"])
            if review.preview_sha256 == subject_sha256 and not review.approved:
                reasons.append("talking_exact_preview_previously_rejected")
        return reasons

    def require_clear(self, project_id: UUID, subject_sha256: str, *, pending: bool = True) -> None:
        reasons = self.blockers(project_id, subject_sha256, pending=pending)
        if reasons:
            raise TalkingReviewPolicyError(", ".join(reasons))

    def register(self, series, subject, finding: TalkingReviewFinding, evidence_reference: str,
                 idempotency_key: str) -> TalkingReviewConcern:
        if series.planned_origin is None or series.planned_origin.version != 2:
            raise TalkingReviewPolicyError("localized_review_requires_aggregate_policy")
        self._range(finding, subject.duration_ms)
        concern = TalkingReviewConcern(
            project_id=series.project_id, series_id=series.id,
            idempotency_key=idempotency_key,
            subject_asset_id=subject.id, subject_sha256=subject.content_hash,
            finding=finding, evidence_reference=evidence_reference,
            created_at=datetime.now(timezone.utc),
        )
        row = self.db.connection.execute(
            "SELECT payload FROM talking_review_concerns WHERE project_id = ? AND idempotency_key = ?",
            (str(series.project_id), idempotency_key),
        ).fetchone()
        if row is not None:
            prior = TalkingReviewConcern.model_validate_json(row["payload"])
            if prior.model_dump(exclude={"id", "created_at"}) != concern.model_dump(exclude={"id", "created_at"}):
                raise TalkingReviewPolicyError("local_review_concern_idempotency_conflict")
            return prior
        self.db.connection.execute(
            "INSERT INTO talking_review_concerns(id, project_id, series_id, idempotency_key, payload) VALUES (?, ?, ?, ?, ?)",
            (str(concern.id), str(concern.project_id), str(concern.series_id), idempotency_key, concern.model_dump_json()),
        )
        return concern

    def record_answers(self, project_id: UUID, subject, answers: list[TalkingReviewConcernAnswer]) -> None:
        if len({answer.concern_id for answer in answers}) != len(answers):
            raise TalkingReviewPolicyError("duplicate_local_review_answer")
        concerns = {concern.id: concern for concern in self.concerns(project_id, subject.content_hash)}
        for answer in answers:
            concern = concerns.get(answer.concern_id)
            if concern is None or concern.subject_asset_id != subject.id:
                raise TalkingReviewPolicyError("local_review_subject_mismatch")
            prior = self.answer(answer.concern_id)
            if prior is not None and prior != answer:
                raise TalkingReviewPolicyError("local_review_answer_immutable")
            self.db.connection.execute(
                "INSERT INTO talking_review_concern_answers(concern_id, payload) VALUES (?, ?) ON CONFLICT(concern_id) DO NOTHING",
                (str(answer.concern_id), answer.model_dump_json()),
            )

    def validate_review(self, series, subject, *, version: int, approved: bool,
                        dimensions: TalkingReviewDimensions | None,
                        findings: list[TalkingReviewFinding]) -> None:
        expected = series.planned_origin.version
        if version != expected:
            raise TalkingReviewPolicyError("talking_review_policy_mismatch")
        if version == 1:
            if dimensions is not None or findings:
                raise TalkingReviewPolicyError("legacy_review_cannot_claim_aggregate_scope")
            return
        if dimensions is None:
            raise TalkingReviewPolicyError("aggregate_review_requires_six_dimensions")
        for finding in findings:
            self._range(finding, subject.duration_ms)
        if approved:
            if any(value != "pass" for value in dimensions.model_dump(exclude={"schema_version"}).values()):
                raise TalkingReviewPolicyError("aggregate_review_dimensions_not_all_pass")
            self.require_clear(series.project_id, subject.content_hash)
        elif not findings:
            raise TalkingReviewPolicyError("aggregate_rejection_requires_scoped_findings")

    def require_admitted_review(self, series, subject, review, run=None) -> None:
        version = series.planned_origin.version
        if review.review_policy_version != version or (run is not None and run.review_policy_version != version):
            raise TalkingReviewPolicyError("talking_review_policy_mismatch")
        self.validate_review(series, subject, version=version, approved=review.approved,
                             dimensions=review.dimensions, findings=review.scoped_findings)
        if not review.approved:
            raise TalkingReviewPolicyError("talking_exact_preview_review_rejected")

    @staticmethod
    def _range(finding: TalkingReviewFinding, duration_ms: int) -> None:
        if finding.end_ms is not None and finding.end_ms > duration_ms:
            raise TalkingReviewPolicyError("local_review_interval_outside_subject")
