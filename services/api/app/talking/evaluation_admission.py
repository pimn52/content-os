"""Owned operation context for the existing policy2 preview/Run review.

File/evidence observation happens before locks. Inside locks only detached
DB fences and the existing review policy execute. No Master-parent mutation.
"""
import json
from contextlib import contextmanager
from app.db import JobRepository, AssetRepository, TalkingSliceSeriesRepository
from app.domain.models import JobStatus, execution_canonical_sha256 as digest
from app.domain.evaluation_talking import (EvaluationTalkingAdmissionResult, EvaluationTalkingCalibrationAdmissionResult,
    TalkingAdmissionResultRecord, TalkingExactQualityObservation, CALIBRATION_POLICY)
from pydantic import TypeAdapter
from app.evaluation_talking import EvaluationTalkingService
from app.evaluation_talking_execution import require_generation_result
from app.execution_admission import UseAdmissionError
from app.jobs.evaluation_talking_preview_handler import require_preview_result
from .planned_admission import PlannedTalkingAdmissionService, PlannedTalkingAdmissionError
from .review_policy import require_child_judgment


def require_admission_projection(service, operation, asset):
    """DB-only authentication of the additive promotion, not a read grant."""
    from app.db import ClipRepository, TalkingRunRepository, TalkingSliceSeriesContinuityReviewRepository
    from .review_policy import TalkingReviewPolicyService
    value = service.records.result(operation.id,"admission")
    if value is None:
        raise UseAdmissionError("evaluation_talking_admission_result_required")
    result = TypeAdapter(TalkingAdmissionResultRecord).validate_json(json.dumps(value))
    if (operation.policy == CALIBRATION_POLICY) != (type(result) is EvaluationTalkingCalibrationAdmissionResult):
        raise UseAdmissionError("talking_calibration_result_policy_changed")
    if type(result) is EvaluationTalkingCalibrationAdmissionResult and result.model_dump(mode="json") != value:
        raise UseAdmissionError("talking_calibration_result_changed")
    run = TalkingRunRepository(service.db).get(result.run_id)
    clip = ClipRepository(service.db).get(result.clip_id)
    review = TalkingSliceSeriesContinuityReviewRepository(service.db).get(result.review_id)
    series = TalkingSliceSeriesRepository(service.db).get(operation.series_id)
    if (result.operation_id != operation.id or result.series_id != operation.series_id or series is None
        or result.preview_result_sha256 != digest(service.records.result(operation.id,"preview"))
        or result.asset_id != asset.id or result.asset_payload_sha256 != digest(asset.model_dump(mode="json"))
        or run is None or result.run_sha256 != digest(run.model_dump(mode="json"))
        or clip is None or result.clip_sha256 != digest(clip.model_dump(mode="json"))
        or review is None or result.review_sha256 != digest(review.model_dump(mode="json"))
        or run.project_id != operation.project_id or run.series_id != operation.series_id
        or run.assembled_asset_id != asset.id or run.assembled_clip_id != clip.id
        or run.continuity_review_id != review.id or review.series_id != series.id
        or review.project_id != operation.project_id or review.preview_asset_id != asset.id
        or review.preview_sha256 != asset.content_hash or clip.asset_id != asset.id
        or clip.start_ms != 0 or clip.end_ms != asset.duration_ms):
        raise UseAdmissionError("evaluation_talking_admission_projection_changed")
    scope = service.scopes.get("talking_run",run.id)
    row = service.db.connection.execute("SELECT subject_sha256 FROM execution_use_scopes WHERE subject_kind='talking_run' AND subject_id=?",(str(run.id),)).fetchone()
    origins = service.db.connection.execute("SELECT content_hash FROM execution_use_content_origins WHERE subject_kind='talking_run' AND subject_id=?",(str(run.id),)).fetchall()
    if (scope is None or scope.project_id != operation.project_id or scope.fingerprint != operation.output_scope_fingerprint
        or row is None or row[0] != result.run_sha256 or [r[0] for r in origins] != [asset.content_hash]):
        raise UseAdmissionError("evaluation_talking_run_scope_changed")
    if (review.review_policy_version != 2 or run.review_policy_version != 2
        or series.planned_origin.version != 2 or not review.approved or review.dimensions is None
        or any(value != "pass" for value in review.dimensions.model_dump(exclude={"schema_version"}).values())):
        raise UseAdmissionError("evaluation_talking_admission_review_changed")
    # Authentication must still allow answering a newly pending concern.
    # Current use/admission eligibility separately includes pending blockers.
    TalkingReviewPolicyService(service.db).require_clear(operation.project_id,asset.content_hash,pending=False)
    if type(result) is EvaluationTalkingCalibrationAdmissionResult and result.quality_observation != exact_quality_observation(service,operation,asset,review,run):
        raise UseAdmissionError("talking_calibration_quality_observation_changed")
    return run,clip,review,result


def exact_quality_observation(service, operation, preview, review, run):
    """Server-only projection of authenticated completed records, no promotion."""
    from app.db import ProviderCallRepository
    from app.evaluation_talking_execution import call_key
    service.require_calibration_claim(operation)
    receipt = service.admissions.get(operation.project_id,operation.selection.execution_admission_id)
    call = ProviderCallRepository(service.db).get_by_idempotency_key(call_key(operation))
    if receipt is None or call is None or call.status != "completed":
        raise UseAdmissionError("talking_calibration_completed_call_required")
    return TalkingExactQualityObservation(observation_version=1,policy=CALIBRATION_POLICY,
        observed_pass_duration_ms=operation.proof["master_end_ms"]-operation.proof["master_start_ms"],
        trial_input_sha256=operation.proof["calibration_intent"]["trial_input_sha256"],
        execution_sha256=receipt.review.execution_sha256,configuration_sha256=receipt.review.configuration_sha256,
        operation_sha256=digest(operation.model_dump(mode="json")),
        generation_result_sha256=digest(service.records.result(operation.id,"generation")),
        qa_result_sha256=digest(service.records.result(operation.id,"qa")),preview_sha256=preview.content_hash,
        provider_call_sha256=digest(call.model_dump(mode="json")),use_admission_sha256=operation.admission_sha256,
        output_scope_fingerprint=operation.output_scope_fingerprint,review_sha256=digest(review.model_dump(mode="json")),
        run_sha256=digest(run.model_dump(mode="json")))


class EvaluationTalkingAdmissionService(PlannedTalkingAdmissionService):
    def __init__(self, db, root):
        super().__init__(db,root)
        self.owner = EvaluationTalkingService(db,root)
        self._observed = None

    def _snapshot(self, operation):
        jobs = [self.jobs.get(identity) for identity in
                (operation.generation_job_id,operation.qa_job_id,operation.preview_job_id)]
        return dict(operation=self.owner.records.operation(operation.id),
            facts=self.owner._facts(operation.project_id,operation.selection),
            series=self.series.get(operation.series_id),jobs=jobs,
            results={role:self.owner.records.result(operation.id,role)
                for role in ("generation","qa","preview","admission","cancellation")})

    def _observe(self, series):
        if self.db.connection.in_transaction:
            raise UseAdmissionError("execution_observation_inside_transaction")
        if series.planned_origin is None or series.planned_origin.version != 2:
            raise UseAdmissionError("evaluation_talking_aggregate_policy_required")
        operation = self.owner.records.operation(series.planned_origin.run_id)
        if (operation is None or operation.series_id != series.id or operation.project_id != series.project_id
            or self.series.get(series.id) != series
            or series != self.owner._preview_series(operation,created_at=series.created_at)):
            raise UseAdmissionError("evaluation_talking_preview_series_changed")
        context = self.owner.require_retained_operation(operation)
        before = self._snapshot(operation)
        if before["operation"] != operation or before["results"]["cancellation"] is not None:
            raise UseAdmissionError("evaluation_talking_operation_cancelled")
        output,_ = require_generation_result(self.owner,operation,self.owner.records.result(operation.id,"generation"))
        require_child_judgment(output.metadata["talking_generation"],2)
        for identity in (operation.generation_job_id,operation.qa_job_id,operation.preview_job_id):
            job = self.jobs.get(identity)
            if job is None or job.status != JobStatus.COMPLETED or self.owner.require_job_identity(job) != operation:
                raise UseAdmissionError("evaluation_talking_preview_not_completed")
        preview,_ = require_preview_result(self.owner,operation,self.owner.records.result(operation.id,"preview"))
        report = preview.metadata["talking_run_preview"]
        qa_sha = digest({key:value for key,value in report.items() if key != "state"})
        snapshot = self._snapshot(operation)
        if snapshot != before or snapshot["facts"] != context["facts"]:
            raise UseAdmissionError("evaluation_talking_authority_changed")
        return dict(operation=operation,series=series,preview=preview,qa_sha=qa_sha,snapshot=snapshot)

    def require_current_candidate(self, series):
        if self.db.connection.in_transaction:
            observed = self._observed
            if (observed is None or observed["series"] != series
                or self._snapshot(observed["operation"]) != observed["snapshot"]):
                raise UseAdmissionError("evaluation_talking_authority_changed")
        else:
            observed = self._observe(series)
        return observed["preview"],observed["qa_sha"]

    @contextmanager
    def _mutation(self, series):
        if self._observed is not None:
            raise UseAdmissionError("evaluation_talking_nested_mutation_denied")
        observed = self._observe(series)
        try:
            with self.db.transaction(immediate=True):
                if self._snapshot(observed["operation"]) != observed["snapshot"]:
                    raise UseAdmissionError("evaluation_talking_authority_changed")
                self._observed = observed
                yield observed
        finally:
            self._observed = None

    def record_review(self, series, **kwargs):
        with self._mutation(series):
            return super().record_review(series,**kwargs)

    def register_concern(self, series, **kwargs):
        with self._mutation(series):
            return super().register_concern(series,**kwargs)

    def answer_concerns(self, series, **kwargs):
        with self._mutation(series):
            return super().answer_concerns(series,**kwargs)

    def admit(self, series):
        with self._mutation(series) as observed:
            operation,preview = observed["operation"],observed["preview"]
            review = self.reviews.get_by_series_id(series.id)
            if review is None or not review.approved:
                raise PlannedTalkingAdmissionError("exact preview continuity approval is missing or rejected")
            self.policy.require_admitted_review(series,preview,review)
            if (review.project_id != series.project_id or review.child_job_ids != series.child_job_ids
                or review.planned_origin_sha256 != digest(series.planned_origin.model_dump(mode="json"))
                or review.preview_asset_id != preview.id or review.preview_sha256 != preview.content_hash
                or review.preview_qa_sha256 != observed["qa_sha"]):
                raise UseAdmissionError("evaluation_talking_review_changed")
            existing = self.runs.get_by_series_id(series.id)
            if existing is not None:
                authenticated = require_admission_projection(self.owner,operation,preview)[0]
                self._require_existing_admission(authenticated,series,preview,review,None)
                if existing != authenticated:
                    raise UseAdmissionError("evaluation_talking_run_changed")
                return existing
            if self.owner.records.result(operation.id,"admission") is not None or self.clips.list_by_asset(preview.id) or "talking_run" in preview.metadata:
                raise UseAdmissionError("evaluation_talking_preview_already_admitted")
            run,clip,promoted = self._build_admission(series,preview,review)
            result = EvaluationTalkingAdmissionResult(operation_id=operation.id,series_id=series.id,
                preview_result_sha256=digest(self.owner.records.result(operation.id,"preview")),
                review_id=review.id,review_sha256=digest(review.model_dump(mode="json")),
                run_id=run.id,run_sha256=digest(run.model_dump(mode="json")),
                clip_id=clip.id,clip_sha256=digest(clip.model_dump(mode="json")),asset_id=preview.id,
                asset_payload_sha256=digest(promoted.model_dump(mode="json")))
            if operation.policy == CALIBRATION_POLICY:
                result = EvaluationTalkingCalibrationAdmissionResult(**result.model_dump(exclude={"result_version"}),
                    quality_observation=exact_quality_observation(self.owner,operation,preview,review,run))
            # Exact additive promotion only; restrictions/original technical
            # proof stay. Generic evaluation media mutations remain forbidden.
            self.db.connection.execute("UPDATE assets SET payload=? WHERE id=?",(promoted.model_dump_json(),str(preview.id)))
            self.clips.create(clip)
            if self.runs.create(run) != run:
                raise UseAdmissionError("evaluation_talking_run_conflict")
            scope = self.owner.scopes.get("asset",preview.id)
            if scope is None or scope.fingerprint != operation.output_scope_fingerprint:
                raise UseAdmissionError("evaluation_talking_output_scope_changed")
            self.db.connection.execute("INSERT INTO execution_use_scopes(subject_kind,subject_id,project_id,fingerprint,payload,subject_sha256) VALUES ('talking_run',?,?,?,?,?)",
                (str(run.id),str(run.project_id),scope.fingerprint,scope.model_dump_json(),result.run_sha256))
            self.db.connection.execute("INSERT INTO execution_use_content_origins(content_hash,subject_kind,subject_id) VALUES (?,'talking_run',?)",
                (preview.content_hash,str(run.id)))
            self.owner.records.save_result(operation.id,"admission",result.model_dump(mode="json"))
            return run
