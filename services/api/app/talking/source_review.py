"""Narrow product operations around D024's existing source-review admission."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.db import AssetRepository, ClipRepository, Database
from app.talking.source_admission import TalkingSourceAdmission, TalkingSourceAdmissionRepository
from app.talking.source_suitability import TalkingSourceSuitabilityAssessment, TalkingSourceSuitabilityRepository


class ManagedTalkingSourceReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=1, max_length=450)
    expected_source_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_start_ms: int = Field(ge=0)
    expected_end_ms: int = Field(gt=0)
    presentation: Literal["source_native_portrait", "unspecified"]
    coverage: Literal["full_interval_continuous", "partial"]
    face_head_clearance: Literal["pass", "fail", "unknown"]
    motion_continuity: Literal["pass", "fail", "unknown"]
    subtitle_clearance: Literal["pass", "fail", "unknown"]
    effective_quality: Literal["pass", "fail", "unknown"]
    findings: list[str] = Field(min_length=1, max_length=100)
    confirmed_inspection: Literal[True]
    adopt_for_reuse: bool


class SourceReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assessment: TalkingSourceSuitabilityAssessment
    decision: Literal["review_claimed_suitable", "unusable", "unknown"]
    admission: TalkingSourceAdmission | None
    evidence_sha256: str


class SourceReviewChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assessment_id: str
    assessment: TalkingSourceSuitabilityAssessment | None
    decision: str | None
    admission: TalkingSourceAdmission | None
    state: Literal["current", "missing_admission", "revoked", "source_stale", "evidence_unavailable", "evidence_changed", "corrupt_record"]
    evidence_sha256: str | None
    reasons: list[str] = Field(default_factory=list)


class TalkingSourceReviewService:
    def __init__(self, db: Database, data_root: str | Path):
        self.db = db
        self.data_root = Path(data_root).resolve()
        self.assessments = TalkingSourceSuitabilityRepository(db, self.data_root)
        self.admissions = TalkingSourceAdmissionRepository(db, self.data_root)

    def list_for_clip(self, clip_id: UUID) -> list[SourceReviewChoice]:
        if ClipRepository(self.db).get(clip_id) is None:
            raise ValueError("Talking source Clip is missing")
        result: list[SourceReviewChoice] = []
        for row_id, raw in self.assessments.list_rows_for_clip(clip_id):
            try:
                assessment = TalkingSourceSuitabilityAssessment.model_validate_json(raw)
                if assessment.source_clip_id != clip_id or str(assessment.id) != row_id:
                    raise ValueError("Talking source review row identity changed")
            except (ValueError, TypeError):
                result.append(SourceReviewChoice(assessment_id=row_id, assessment=None, decision=None,
                                                 admission=None, state="corrupt_record", evidence_sha256=None,
                                                 reasons=["review_record_invalid"]))
                continue
            try:
                receipt = self.admissions.get_for_assessment(assessment.id)
                if receipt is not None and receipt.assessment_id != assessment.id:
                    raise ValueError("Talking source admission row identity changed")
            except (ValueError, TypeError):
                result.append(SourceReviewChoice(assessment_id=row_id, assessment=assessment,
                                                 decision=assessment.decision, admission=None,
                                                 state="corrupt_record", evidence_sha256=None,
                                                 reasons=["admission_record_invalid"]))
                continue
            state = "missing_admission"
            reasons: list[str] = []
            evidence_hash = None
            try:
                self.assessments.require_current_source(assessment)
            except ValueError:
                state, reasons = "source_stale", ["source_changed_or_unavailable"]
            else:
                try:
                    evidence_hash = self.admissions._evidence_hash(assessment.evidence_reference)
                    self._require_managed_report_hash(assessment, evidence_hash)
                except ValueError as exc:
                    if "bytes changed" in str(exc):
                        state, reasons = "evidence_changed", ["evidence_bytes_changed"]
                    else:
                        state, reasons = "evidence_unavailable", ["evidence_unavailable_or_outside_data_root"]
                else:
                    if receipt is not None:
                        if not receipt.active:
                            state, reasons = "revoked", ["admission_revoked"]
                        else:
                            try:
                                self.admissions.require_current(receipt)
                            except ValueError:
                                state, reasons = "evidence_changed", ["admission_evidence_changed"]
                            else:
                                state = "current"
            result.append(SourceReviewChoice(
                assessment_id=row_id, assessment=assessment, decision=assessment.decision,
                admission=receipt, state=state, evidence_sha256=evidence_hash, reasons=reasons,
            ))
        return result

    def evidence_bytes(self, assessment_id: UUID) -> tuple[bytes, str]:
        assessment = self.assessments.get(assessment_id)
        if assessment is None:
            raise ValueError("Talking source review is missing")
        source = self.admissions.evidence_path(assessment.evidence_reference)
        try:
            if source.stat().st_size > 10_000_000:
                raise ValueError("Talking source review evidence exceeds the download limit")
            data = source.read_bytes()
        except OSError as exc:
            raise ValueError("Talking source review evidence is unavailable") from exc
        if len(data) > 10_000_000:
            raise ValueError("Talking source review evidence exceeds the download limit")
        digest = hashlib.sha256(data).hexdigest()
        self._require_managed_report_hash(assessment, digest)
        return data, digest

    @staticmethod
    def _require_managed_report_hash(assessment: TalkingSourceSuitabilityAssessment, digest: str) -> None:
        if assessment.evidence_reference.startswith("talking-source-reviews/"):
            expected = assessment.reviewer_reference.rsplit(";sha256:", 1)
            if len(expected) != 2 or expected[1] != digest:
                raise ValueError("Talking source review evidence bytes changed")

    def record(self, clip_id: UUID, payload: ManagedTalkingSourceReviewRequest, *, actor_id: str) -> SourceReviewResult:
        """Persist a human assertion, optionally adopting only its positive decision."""
        with self.db.transaction(immediate=True):
            clip = ClipRepository(self.db).get(clip_id)
            asset = None if clip is None else AssetRepository(self.db).get(clip.asset_id)
            if (clip is None or asset is None or asset.content_hash != payload.expected_source_content_hash
                or clip.start_ms != payload.expected_start_ms or clip.end_ms != payload.expected_end_ms):
                raise ValueError("Talking source review source identity or exact Clip interval changed")
            request_digest = hashlib.sha256(json.dumps({
                "actor_id": actor_id, "clip_id": str(clip_id), "asset_id": str(asset.id),
                "payload": payload.model_dump(mode="json"),
            }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
            key = f"managed:{payload.idempotency_key}"
            existing = self.assessments.get_by_idempotency_key(key)
            if existing is not None:
                self.assessments.require_current_source(existing)
                report_bytes, digest = self.evidence_bytes(existing.id)
                try:
                    report = json.loads(report_bytes)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError("Talking source review report is invalid") from exc
                if not isinstance(report, dict) or report.get("request_sha256") != request_digest:
                    raise ValueError("Talking source review idempotency key belongs to different content or reviewer")
                receipt = self.admissions.get_for_assessment(existing.id)
                if receipt is not None:
                    self.admissions.require_current(receipt)
                if bool(receipt) != bool(payload.adopt_for_reuse and existing.decision == "review_claimed_suitable"):
                    raise ValueError("Talking source review adoption state changed")
                return SourceReviewResult(assessment=existing, decision=existing.decision,
                                          admission=receipt, evidence_sha256=digest)

            assessment_id = uuid4()
            reference = f"talking-source-reviews/{assessment_id}.json"
            assessment = TalkingSourceSuitabilityAssessment(
                id=assessment_id, idempotency_key=key, source_asset_id=asset.id, source_clip_id=clip.id,
                source_content_hash=asset.content_hash, start_ms=clip.start_ms, end_ms=clip.end_ms,
                presentation=payload.presentation, evidence_class="assisted_test",
                method="human_full_interval_review", method_version="managed-human-v1",
                coverage=payload.coverage, face_head_clearance=payload.face_head_clearance,
                motion_continuity=payload.motion_continuity, subtitle_clearance=payload.subtitle_clearance,
                effective_quality=payload.effective_quality, reviewer_reference=f"trusted:{actor_id}",
                evidence_reference=reference,
            )
            self.assessments.require_current_source(assessment)
            report = {
                "schema_version": "talking-source-review/v1", "request_sha256": request_digest,
                "actor_id": actor_id, "reviewed_at": datetime.now(timezone.utc).isoformat(),
                "source_asset_id": str(asset.id), "source_clip_id": str(clip.id),
                "source_content_hash": asset.content_hash, "start_ms": clip.start_ms, "end_ms": clip.end_ms,
                "presentation": payload.presentation, "coverage": payload.coverage,
                "face_head_clearance": payload.face_head_clearance,
                "motion_continuity": payload.motion_continuity,
                "subtitle_clearance": payload.subtitle_clearance,
                "effective_quality": payload.effective_quality,
                "findings": payload.findings, "confirmed_inspection": True,
                "adopt_for_reuse": payload.adopt_for_reuse,
            }
            encoded = json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            digest = hashlib.sha256(encoded).hexdigest()
            assessment = assessment.model_copy(update={
                "reviewer_reference": f"trusted:{actor_id};sha256:{digest}",
            })
            directory = (self.data_root / "talking-source-reviews").resolve()
            if not directory.is_relative_to(self.data_root):
                raise ValueError("Talking source review directory escapes data root")
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / f"{assessment_id}.json"
            if target.exists():
                raise ValueError("Talking source review report identity collision")
            temporary = directory / f".{assessment_id}.{uuid4()}.tmp"
            created = False
            try:
                with temporary.open("xb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
                created = True
                self.assessments.create(assessment)
                receipt = None
                if payload.adopt_for_reuse and assessment.decision == "review_claimed_suitable":
                    receipt = self.admissions.admit(assessment.id, idempotency_key=f"managed:{assessment.id}",
                                                    actor_id=actor_id, evidence_sha256=digest)
            except Exception:
                temporary.unlink(missing_ok=True)
                if created:
                    target.unlink(missing_ok=True)
                raise
            return SourceReviewResult(assessment=assessment, decision=assessment.decision,
                                      admission=receipt, evidence_sha256=digest)
