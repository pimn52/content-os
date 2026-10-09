"""Exact-candidate continuity review and admission for one planned Talking child."""
from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from app.db import (
    AssetRepository, ClipRepository, Database, JobRepository, TalkingRunRepository,
    TalkingSliceSeriesContinuityReviewRepository, TalkingSliceSeriesRepository,
)
from app.domain.models import (
    Asset, Clip, JobStatus, JobType, SourceKind, TalkingGenerationJobPayload,
    TalkingRun, TalkingRunChildEvidence, TalkingSliceSeries,
    TalkingSliceSeriesContinuityReview, TalkingRunPreviewJobPayload,
)
from app.production_runs import (
    ProductionRunNotReady, ProductionRunService, TalkingSourceBinding, _snapshot_sha256,
)
from app.talking.planned_preview import _source_path
from app.domain.models import TalkingReviewDimensions, TalkingReviewFinding, TalkingReviewConcernAnswer
from app.talking.review_policy import TalkingReviewPolicyService, require_child_judgment


class PlannedTalkingAdmissionError(ValueError):
    pass


class PlannedTalkingAdmissionService:
    def __init__(self, db: Database, data_root: str | Path):
        self.db = db
        self.data_root = Path(data_root).resolve()
        self.production = ProductionRunService(db, self.data_root)
        self.assets = AssetRepository(db)
        self.clips = ClipRepository(db)
        self.jobs = JobRepository(db)
        self.series = TalkingSliceSeriesRepository(db)
        self.reviews = TalkingSliceSeriesContinuityReviewRepository(db)
        self.runs = TalkingRunRepository(db)
        self.policy = TalkingReviewPolicyService(db)

    def review_status(self, series: TalkingSliceSeries) -> dict[str, object]:
        """Project-level read projection; never treats a planned child as legacy membership."""
        origin = series.planned_origin
        review = self.reviews.get_by_series_id(series.id)
        if origin is None:
            return {
                "id": series.id, "readiness": "blocked", "ready_for_human_continuity_review": False,
                "children": [], "blockers": ["planned_origin_missing"],
                "continuity_review_state": "not_submitted", "continuity_review_evidence_reference": None,
                "continuity_review_findings": [], "planned_origin_sha256": None,
                "preview_asset_id": None, "preview_sha256": None,
            }
        child_job = self.jobs.get(origin.generation_job_id)
        child_asset = self.assets.get(origin.output_asset_id)
        generation = child_asset.metadata.get("talking_generation") if child_asset is not None else None
        child_qa = generation.get("qa_state") if isinstance(generation, dict) else None
        child_human = generation.get("human_review_state") if isinstance(generation, dict) else None
        blockers: list[str] = []
        candidate: Asset | None = None
        try:
            candidate, _ = self.require_current_candidate(series)
        except (PlannedTalkingAdmissionError, ValueError, OSError) as exc:
            blockers.append(str(exc))
        run = self.production.get(series.project_id, origin.run_id)
        dependency = None if run is None else next((item for item in run.talking_preview_dependencies
                                                    if item.series_id == series.id), None)
        if candidate is not None:
            readiness = "ready_for_human_continuity_review"
            if origin.version == 2:
                blockers.extend(self.policy.blockers(series.project_id, candidate.content_hash))
        elif dependency is not None and dependency.state in {"pending", "running"}:
            readiness = "preview_preparing"
        elif dependency is not None and dependency.state == "failed":
            readiness = "preview_failed"
        else:
            readiness = "blocked"
        return {
            "id": series.id, "readiness": readiness,
            "ready_for_human_continuity_review": candidate is not None and review is None,
            "children": [{
                "series_index": 0, "job_id": origin.generation_job_id,
                "job_status": None if child_job is None else child_job.status,
                "output_asset_id": None if child_asset is None else child_asset.id,
                "automated_qa_state": child_qa, "human_review_state": child_human,
                "blockers": self._child_blockers(generation, origin.version),
            }],
            "blockers": blockers,
            "continuity_review_state": "not_submitted" if review is None else "approved" if review.approved else "rejected",
            "continuity_review_evidence_reference": None if review is None else review.evidence_reference,
            "continuity_review_findings": [] if review is None else list(review.findings),
            "planned_origin_sha256": _snapshot_sha256(origin.model_dump(mode="json")),
            "preview_asset_id": None if candidate is None else candidate.id,
            "preview_sha256": None if candidate is None else candidate.content_hash,
            "review_policy_version": origin.version,
            "required_dimensions": [] if origin.version == 1 else [key for key in TalkingReviewDimensions.model_fields if key != "schema_version"],
            "review_concerns": [] if candidate is None or origin.version == 1 else [
                {**item.model_dump(mode="json"), "answer": None if self.policy.answer(item.id) is None else self.policy.answer(item.id).model_dump(mode="json")}
                for item in self.policy.concerns(series.project_id, candidate.content_hash)
            ],
        }

    @staticmethod
    def _child_blockers(generation, version):
        if not isinstance(generation, dict) or generation.get("qa_state") != "verified":
            return ["child_technical_qa_not_verified"]
        try:
            require_child_judgment(generation, version)
        except ValueError as exc:
            return [str(exc)]
        return []

    def require_current_candidate(self, series: TalkingSliceSeries) -> tuple[Asset, str]:
        origin = series.planned_origin
        if origin is None or series.child_job_ids != [origin.generation_job_id]:
            raise PlannedTalkingAdmissionError("planned Talking origin is missing or mixed")
        try:
            self.production.require_current_planned_run_origin(
                series.project_id, origin.run_id, origin.scene_plan_id, expected=origin,
            )
        except ProductionRunNotReady as exc:
            raise PlannedTalkingAdmissionError("planned Talking origin is no longer current: " + ", ".join(exc.reasons)) from exc
        run = self.production.get(series.project_id, origin.run_id)
        binding = next((item for item in run.talking_source_bindings if item.scene_plan_id == origin.scene_plan_id), None)
        if binding is None or binding.talking_series_id != series.id or binding.talking_preview_job_id is None:
            raise PlannedTalkingAdmissionError("planned preview is not bound to the production run")
        preview_job = self.jobs.get(binding.talking_preview_job_id)
        origin_sha = _snapshot_sha256(origin.model_dump(mode="json"))
        if (preview_job is None or preview_job.project_id != series.project_id
            or preview_job.type is not JobType.PREPARE_TALKING_RUN_PREVIEW
            or preview_job.status is not JobStatus.COMPLETED
            or not isinstance(preview_job.payload, TalkingRunPreviewJobPayload)
            or preview_job.payload.series_id != series.id
            or preview_job.payload.origin_sha256 != origin_sha):
            raise PlannedTalkingAdmissionError("planned preview Job is incomplete or mismatched")
        candidates = [asset for asset in self.assets.list()
                      if isinstance(asset.metadata.get("talking_run_preview"), dict)
                      and asset.metadata["talking_run_preview"].get("job_id") == str(preview_job.id)]
        if len(candidates) != 1:
            raise PlannedTalkingAdmissionError("planned preview output is missing or ambiguous")
        asset = candidates[0]
        report = asset.metadata["talking_run_preview"]
        if report.get("review_policy_version", 1) != origin.version:
            raise PlannedTalkingAdmissionError("planned preview review policy mismatch")
        streams = asset.metadata.get("streams")
        stream_types = {item.get("codec_type") for item in streams if isinstance(item, dict)} if isinstance(streams, list) else set()
        if (asset.source_kind is not SourceKind.AI_VIDEO or asset.authorization_reference != origin.authorization_reference
            or not asset.has_audio or {"video", "audio"} - stream_types
            or abs(asset.duration_ms - (origin.master_end_ms - origin.master_start_ms)) > 80
            or report.get("state") not in {"review_only", "promoted"}
            or report.get("technical_qa_state") != "verified"
            or report.get("series_id") != str(series.id)
            or report.get("origin_sha256") != origin_sha
            or report.get("input_output_sha256") != origin.output_sha256
            or report.get("master_sha256") != origin.master_sha256
            or report.get("master_start_ms") != origin.master_start_ms
            or report.get("master_end_ms") != origin.master_end_ms
            or report.get("duration_ms") != asset.duration_ms
            or report.get("has_audio") is not True
            or report.get("video_width") != asset.width or report.get("video_height") != asset.height):
            raise PlannedTalkingAdmissionError("planned preview technical evidence is mismatched")
        try:
            path = _source_path(asset.source_file, self.data_root)
        except ValueError as exc:
            raise PlannedTalkingAdmissionError("planned preview file is unavailable") from exc
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != asset.content_hash:
                raise PlannedTalkingAdmissionError("planned preview bytes changed")
        return asset, _snapshot_sha256({key: value for key, value in report.items() if key != "state"})

    def record_review(
        self, series: TalkingSliceSeries, *, approved: bool, evidence_reference: str,
        findings: list[str], preview_asset_id: UUID, preview_sha256: str,
        review_policy_version: int = 1, dimensions: TalkingReviewDimensions | None = None,
        scoped_findings: list[TalkingReviewFinding] | None = None,
        concern_answers: list[TalkingReviewConcernAnswer] | None = None,
    ) -> TalkingSliceSeriesContinuityReview:
        with (nullcontext() if self.db.connection.in_transaction else self.db.transaction(immediate=True)):
            current = self.series.get(series.id)
            if current is None or current.project_id != series.project_id or current.planned_origin is None:
                raise PlannedTalkingAdmissionError("planned Talking collection is unavailable")
            preview, qa_sha = self.require_current_candidate(current)
            if preview.id != preview_asset_id or preview.content_hash != preview_sha256:
                raise PlannedTalkingAdmissionError("review does not identify the current exact preview")
            if review_policy_version != current.planned_origin.version:
                raise PlannedTalkingAdmissionError("talking_review_policy_mismatch")
            if review_policy_version == 1 and concern_answers:
                raise PlannedTalkingAdmissionError("legacy_review_cannot_answer_aggregate_concerns")
            self.policy.record_answers(current.project_id, preview, concern_answers or [])
            self.policy.validate_review(current, preview, version=review_policy_version, approved=approved,
                                        dimensions=dimensions, findings=scoped_findings or [])
            review = TalkingSliceSeriesContinuityReview(
                project_id=current.project_id, series_id=current.id,
                approved=approved, evidence_reference=evidence_reference, findings=findings,
                child_job_ids=current.child_job_ids,
                planned_origin_sha256=_snapshot_sha256(current.planned_origin.model_dump(mode="json")),
                preview_asset_id=preview.id, preview_sha256=preview.content_hash,
                preview_qa_sha256=qa_sha, reviewed_at=datetime.now(timezone.utc),
                review_policy_version=review_policy_version, dimensions=dimensions,
                scoped_findings=scoped_findings or [],
            )
            existing = self.reviews.get_by_series_id(current.id)
            if existing is not None:
                if existing.model_dump(exclude={"id", "reviewed_at"}) != review.model_dump(exclude={"id", "reviewed_at"}):
                    raise PlannedTalkingAdmissionError("planned continuity review is immutable")
                return existing
            persisted = self.reviews.create(review)
            if persisted.id != review.id:
                raise PlannedTalkingAdmissionError("planned continuity review idempotency conflict")
            return persisted

    def register_concern(self, series, *, preview_asset_id, preview_sha256, finding,
                         evidence_reference, idempotency_key):
        with self.db.transaction(immediate=True):
            preview = self._concern_subject(series, preview_asset_id, preview_sha256)
            return self.policy.register(series, preview, finding, evidence_reference, idempotency_key)

    def answer_concerns(self, series, *, preview_asset_id, preview_sha256, answers):
        with self.db.transaction(immediate=True):
            preview = self._concern_subject(series, preview_asset_id, preview_sha256)
            self.policy.record_answers(series.project_id, preview, answers)
            return self.review_status(series)

    def _concern_subject(self, series, asset_id, sha256):
        if series.planned_origin is None or series.planned_origin.version != 2:
            raise PlannedTalkingAdmissionError("localized_review_requires_aggregate_policy")
        preview, _ = self.require_current_candidate(series)
        if preview.id != asset_id or preview.content_hash != sha256:
            raise PlannedTalkingAdmissionError("local_review_subject_mismatch")
        return preview

    def admit(self, series: TalkingSliceSeries) -> TalkingRun:
        with self.db.transaction(immediate=True):
            current = self.series.get(series.id)
            if current is None or current.project_id != series.project_id or current.planned_origin is None:
                raise PlannedTalkingAdmissionError("planned Talking collection is unavailable")
            origin = current.planned_origin
            preview, qa_sha = self.require_current_candidate(current)
            review = self.reviews.get_by_series_id(current.id)
            if review is None or not review.approved:
                raise PlannedTalkingAdmissionError("exact preview continuity approval is missing or rejected")
            self.policy.require_admitted_review(current, preview, review)
            if (review.project_id != current.project_id or review.child_job_ids != current.child_job_ids
                or review.planned_origin_sha256 != _snapshot_sha256(origin.model_dump(mode="json"))
                or review.preview_asset_id != preview.id or review.preview_sha256 != preview.content_hash
                or review.preview_qa_sha256 != qa_sha):
                raise PlannedTalkingAdmissionError("continuity review does not cover the current preview")
            production_run = self.production.get(current.project_id, origin.run_id)
            binding = next(item for item in production_run.talking_source_bindings if item.scene_plan_id == origin.scene_plan_id)
            existing = self.runs.get_by_series_id(current.id)
            if existing is not None:
                self._require_existing_admission(existing, current, preview, review, binding)
                return existing
            if binding.talking_run_id is not None or self.clips.list_by_asset(preview.id) or "talking_run" in preview.metadata:
                raise PlannedTalkingAdmissionError("planned preview is already linked to another admission")
            generated = self.jobs.get(origin.generation_job_id)
            if generated is None or not isinstance(generated.payload, TalkingGenerationJobPayload):
                raise PlannedTalkingAdmissionError("planned generation child is missing")
            generation = self.assets.get(origin.output_asset_id).metadata["talking_generation"]
            child = TalkingRunChildEvidence(
                series_index=0, job_id=generated.id, output_asset_id=origin.output_asset_id,
                master_start_ms=origin.master_start_ms, master_end_ms=origin.master_end_ms,
                reference_window_start_ms=origin.source_start_ms, reference_window_end_ms=origin.source_end_ms,
                provider=generation["provider"], model=generation["model"],
                provider_version=generation.get("provider_version") if isinstance(generation.get("provider_version"), str) else None,
            )
            clip = Clip(asset_id=preview.id, start_ms=0, end_ms=preview.duration_ms,
                        asset_duration_ms=preview.duration_ms, talking_candidate=True)
            run = TalkingRun(
                project_id=current.project_id, series_id=current.id,
                master_narration_audio_id=origin.master_audio_id,
                master_start_ms=origin.master_start_ms, master_end_ms=origin.master_end_ms,
                authorized_reference_clip_id=origin.source_clip_id, child_evidence=[child],
                continuity_review_id=review.id, assembled_asset_id=preview.id,
                assembled_clip_id=clip.id, automated_qa_state="verified", admission_state="admitted",
                planned_origin_sha256=review.planned_origin_sha256,
                reviewed_preview_sha256=preview.content_hash,
                created_at=datetime.now(timezone.utc),
                review_policy_version=origin.version,
            )
            metadata = dict(preview.metadata)
            preview_report = dict(metadata["talking_run_preview"])
            preview_report["state"] = "promoted"
            metadata["talking_run_preview"] = preview_report
            metadata["talking_run"] = {
                "run_id": str(run.id), "series_id": str(current.id),
                "master_narration_audio_id": str(origin.master_audio_id),
                "master_start_ms": origin.master_start_ms, "master_end_ms": origin.master_end_ms,
                "authorized_reference_clip_id": str(origin.source_clip_id),
                "automated_qa_state": "verified", "continuity_review_id": str(review.id),
                "continuity_review_state": "approved", "admission_state": "admitted",
                "child_job_ids": [str(origin.generation_job_id)],
                "planned_origin_sha256": review.planned_origin_sha256,
                "reviewed_preview_sha256": preview.content_hash,
            }
            if origin.version == 2:
                metadata["talking_run"]["review_policy_version"] = 2
            self.assets.update(preview.model_copy(update={"metadata": metadata}))
            self.clips.create(clip)
            persisted = self.runs.create(run)
            if persisted.id != run.id:
                raise PlannedTalkingAdmissionError("TalkingRun admission conflicted with another result")
            updated = binding.model_copy(update={"talking_run_id": run.id})
            bindings = tuple(updated if item.scene_plan_id == origin.scene_plan_id else item
                             for item in production_run.talking_source_bindings)
            reasons = tuple(reason for reason in production_run.waiting_stop_reasons
                            if reason not in {f"talking_u_review_run_not_implemented:{origin.scene_plan_id}",
                                              f"talking_exact_preview_review_required:{origin.scene_plan_id}"})
            reasons = tuple(dict.fromkeys((*reasons, "talking_render_resume_not_implemented")))
            self.db.connection.execute(
                "UPDATE production_runs SET talking_source_bindings = ?, waiting_stop_reasons = ? WHERE id = ? AND stage = 'awaiting_talking'",
                (json.dumps([item.model_dump(mode="json") for item in bindings]), json.dumps(reasons), str(origin.run_id)),
            )
            return persisted

    def _require_existing_admission(
        self, run: TalkingRun, series: TalkingSliceSeries, preview: Asset,
        review: TalkingSliceSeriesContinuityReview, binding: TalkingSourceBinding,
    ) -> None:
        origin = series.planned_origin
        self.policy.require_admitted_review(series, preview, review, run)
        clip = self.clips.get(run.assembled_clip_id)
        metadata = preview.metadata.get("talking_run")
        if (origin is None or run.project_id != series.project_id or run.series_id != series.id
            or run.master_narration_audio_id != origin.master_audio_id
            or run.master_start_ms != origin.master_start_ms or run.master_end_ms != origin.master_end_ms
            or run.authorized_reference_clip_id != origin.source_clip_id
            or len(run.child_evidence) != 1 or run.child_evidence[0].job_id != origin.generation_job_id
            or run.child_evidence[0].output_asset_id != origin.output_asset_id
            or run.continuity_review_id != review.id or run.assembled_asset_id != preview.id
            or run.planned_origin_sha256 != review.planned_origin_sha256
            or run.reviewed_preview_sha256 != preview.content_hash
            or binding.talking_run_id != run.id
            or clip is None or clip.asset_id != preview.id or clip.start_ms != 0
            or clip.end_ms != preview.duration_ms or clip.asset_duration_ms != preview.duration_ms
            or not isinstance(metadata, dict) or metadata.get("run_id") != str(run.id)
            or metadata.get("reviewed_preview_sha256") != preview.content_hash
            or metadata.get("continuity_review_id") != str(review.id)
            or metadata.get("planned_origin_sha256") != review.planned_origin_sha256
            or metadata.get("admission_state") != "admitted"):
            raise PlannedTalkingAdmissionError("existing planned TalkingRun admission is stale or mismatched")
        if metadata.get("review_policy_version", 1) != origin.version:
            raise PlannedTalkingAdmissionError("existing planned TalkingRun review policy mismatch")
