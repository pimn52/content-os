"""Owned exact Master-audio preview; uses the existing bounded mux routine.

Selected only by the explicit evaluation Talking Worker policy.
"""
import json
import tempfile
import hashlib
import os
import re
from uuid import uuid4
from pathlib import Path
from app.db import AssetRepository, JobRepository, TalkingSliceSeriesRepository
from app.domain.models import JobType, JobStatus, SourceKind, execution_canonical_sha256 as digest
from app.domain.evaluation_talking import EvaluationTalkingPreviewResult
from app.evaluation_talking import EvaluationTalkingService
from app.evaluation_talking_execution import require_generation_result
from app.execution_admission import UseAdmissionError
from app.media.importer import MediaImporter
from app.talking.planned_preview import _source_path, prepare_master_audio_preview
from app.talking.review_policy import TalkingReviewPolicyService
from .runner import JobExecutionError


def require_preview_result(service,operation,value):
    result = EvaluationTalkingPreviewResult.model_validate_json(json.dumps(value))
    series = TalkingSliceSeriesRepository(service.db).get(operation.series_id)
    if (series is None or result.operation_id != operation.id or result.job_id != operation.preview_job_id
        or result.series_id != series.id or result.origin_sha256 != digest(series.planned_origin.model_dump(mode="json"))
        or result.qa_result_sha256 != digest(service.records.result(operation.id,"qa"))
        or result.output_scope_fingerprint != operation.output_scope_fingerprint
        or service.records.result(operation.id,"preview") != result.model_dump(mode="json")):
        raise UseAdmissionError("evaluation_talking_preview_result_changed")
    job = JobRepository(service.db).get(result.job_id)
    if job is None or service.require_job_identity(job) != operation:
        raise UseAdmissionError("evaluation_talking_preview_job_changed")
    asset = AssetRepository(service.db).get(result.asset_id)
    original = asset
    if asset is not None and ("talking_run" in asset.metadata
        or asset.metadata.get("talking_run_preview",{}).get("state") == "promoted"):
        from app.talking.evaluation_admission import require_admission_projection
        require_admission_projection(service,operation,asset)
        metadata = dict(asset.metadata)
        metadata.pop("talking_run",None)
        metadata["talking_run_preview"] = dict(metadata["talking_run_preview"],state="review_only")
        original = asset.model_copy(update={"metadata":metadata})
    if (asset is None or asset.content_hash != result.asset_sha256
        or digest(original.model_dump(mode="json")) != result.asset_payload_sha256
        or service.scopes.get("asset",asset.id) is None
        or service.scopes.get("asset",asset.id).fingerprint != result.output_scope_fingerprint):
        raise UseAdmissionError("evaluation_talking_preview_asset_changed")
    MediaImporter._verify_content_hash(_source_path(asset.source_file,service.root),asset.content_hash)
    TalkingReviewPolicyService(service.db).require_clear(operation.project_id,asset.content_hash,pending=False)
    return asset,result


class EvaluationTalkingPreviewHandler:
    def __init__(self, db, root, *, ffmpeg_command, probe, policy="evaluation-talking-run-v1"):
        if policy not in ("evaluation-talking-run-v1", "evaluation-talking-calibration-v1"):
            raise UseAdmissionError("talking_execution_policy_unsupported")
        self.db, self.root = db, Path(root).resolve()
        self.ffmpeg_command,self.probe = ffmpeg_command,probe
        self.policy = policy

    def _prepare(self, service, operation, context, output, origin, stage):
        """Close failed mux/probe before private staging can disappear."""
        try:
            prepare_master_audio_preview(video_path=_source_path(output.source_file,self.root),
                master_path=_source_path(context["audio"].source_file,self.root),stage=stage,
                start_ms=origin.master_start_ms,end_ms=origin.master_end_ms,
                ffmpeg_command=self.ffmpeg_command,probe=self.probe)
            return MediaImporter(self.db,self.root,self.probe).prepare_path(
                stage,origin.authorization_reference,source_kind=SourceKind.AI_VIDEO)
        except BaseException:
            failure = dict(role="preview",state="failed",error_code="evaluation_talking_preview_processing_failed")
            identity = uuid4()
            if stage.is_file() and not stage.is_symlink() and 0 < stage.stat().st_size <= 4*1024**3:
                folder = self.root/"execution-quarantine"/str(operation.id)
                folder.mkdir(parents=True,exist_ok=True)
                retained = folder/f"{identity}.quarantine"
                os.link(stage,retained)
                with retained.open("rb") as stream:
                    sha = hashlib.file_digest(stream,"sha256").hexdigest()
                failure.update(content_hash=sha,path=str(retained.relative_to(self.root)))
            with self.db.transaction(immediate=True):
                if "content_hash" in failure:
                    service._pin_quarantine(operation,context["output_scope"],identity,failure["content_hash"])
                service.records.save_result(operation.id,"quarantine",failure)
            raise

    def require_dispatch(self, job):
        if type(self) is not EvaluationTalkingPreviewHandler or getattr(self.db,"data_root",None) != self.root:
            raise UseAdmissionError("evaluation_talking_handler_unsupported")
        saved = JobRepository(self.db).get(job.id)
        if (saved is None or job.status != JobStatus.RUNNING
            or saved.updated_at < job.updated_at
            or saved.model_dump(exclude={"updated_at"}) != job.model_dump(exclude={"updated_at"})
            or job.type != JobType.PREPARE_TALKING_RUN_PREVIEW):
            raise UseAdmissionError("evaluation_talking_job_not_running")
        service = EvaluationTalkingService(self.db,self.root)
        operation = service.require_job_identity(job)
        if operation.policy != self.policy:
            raise UseAdmissionError("talking_execution_policy_mismatch")
        context = service.require_retained_operation(operation)
        output,result = require_generation_result(service,operation,service.records.result(operation.id,"generation"))
        qa_job = JobRepository(self.db).get(operation.qa_job_id)
        generation_job = JobRepository(self.db).get(operation.generation_job_id)
        if (qa_job is None or qa_job.status != JobStatus.COMPLETED or generation_job.status != JobStatus.COMPLETED
            or output.metadata["talking_generation"]["qa_state"] != "verified"):
            raise UseAdmissionError("evaluation_talking_technical_qa_required")
        service.generation_guard(operation,job,context)
        return service,operation,context,output,result

    def __call__(self, job):
        try:
            return self._execute(job)
        except JobExecutionError:
            raise
        except UseAdmissionError as exc:
            code = str(exc)
            if not re.fullmatch(r"[a-z0-9_]{1,100}",code): code = "evaluation_talking_preview_admission_failed"
            raise JobExecutionError(code,code,retryable=False) from None
        except Exception:
            raise JobExecutionError("evaluation_talking_preview_failed","预览未完成，需核对原记录",retryable=False) from None

    def _execute(self, job):
        service,operation,context,output,generation_result = self.require_dispatch(job)
        previous = service.records.result(operation.id,"preview")
        if previous is not None:
            return require_preview_result(service,operation,previous)[0]
        # A failed preview is retained, not an implicit second processing run.
        if service.records.result(operation.id,"quarantine") is not None:
            raise UseAdmissionError("evaluation_talking_quarantine_reconciliation_required")
        series = TalkingSliceSeriesRepository(self.db).get(operation.series_id)
        origin = series.planned_origin
        staging = self.root/"execution-staging"
        staging.mkdir(exist_ok=True)
        item = None
        try:
            with tempfile.TemporaryDirectory(prefix="talking-preview-",dir=staging) as directory:
                stage = Path(directory)/"preview.mp4"
                item = self._prepare(service,operation,context,output,origin,stage)
            item.verify()
            asset = item.asset
            streams = asset.metadata.get("streams",[])
            if (not {"video","audio"} <= {s.get("codec_type") for s in streams if isinstance(s,dict)}
                or not asset.has_audio or abs(asset.duration_ms-(origin.master_end_ms-origin.master_start_ms)) > 80):
                raise UseAdmissionError("evaluation_talking_preview_streams_invalid")
            metadata = dict(asset.metadata)
            metadata["talking_run_preview"] = dict(state="review_only",technical_qa_state="verified",
                job_id=str(job.id),series_id=str(series.id),origin_sha256=job.payload.origin_sha256,
                input_output_sha256=origin.output_sha256,master_sha256=origin.master_sha256,
                master_start_ms=origin.master_start_ms,master_end_ms=origin.master_end_ms,
                duration_ms=asset.duration_ms,has_audio=asset.has_audio,video_width=asset.width,video_height=asset.height,
                review_policy_version=2,evaluation_talking_operation_id=str(operation.id))
            asset = asset.model_copy(update={"metadata":metadata})
            result = EvaluationTalkingPreviewResult(operation_id=operation.id,job_id=job.id,series_id=series.id,
                origin_sha256=job.payload.origin_sha256,qa_result_sha256=digest(service.records.result(operation.id,"qa")),
                asset_id=asset.id,asset_sha256=asset.content_hash,asset_payload_sha256=digest(asset.model_dump(mode="json")),
                output_scope_fingerprint=operation.output_scope_fingerprint)
            current = service.require_retained_operation(operation)
            if (current["facts"] != context["facts"]
                or require_generation_result(service,operation,generation_result.model_dump(mode="json"))[0] != output):
                raise UseAdmissionError("evaluation_talking_authority_changed")
            MediaImporter._verify_content_hash(_source_path(context["audio"].source_file,self.root),origin.master_sha256)
            item.verify()
            with self.db.transaction(immediate=True):
                service.generation_guard(operation,job,context)
                if (TalkingSliceSeriesRepository(self.db).get(series.id) != series
                    or digest(service.records.result(operation.id,"qa")) != result.qa_result_sha256
                    or service.records.result(operation.id,"preview") is not None
                    or JobRepository(self.db).get(operation.qa_job_id).status != JobStatus.COMPLETED):
                    raise UseAdmissionError("evaluation_talking_preview_input_changed")
                item._retain_evaluation_talking(service,operation,job,context,metadata)
                service.records.save_result(operation.id,"preview",result.model_dump(mode="json"))
            return asset
        except BaseException:
            if item is not None and AssetRepository(self.db).get(item.asset.id) is None:
                path = item.quarantine(self.root/"execution-quarantine"/str(operation.id))
                with self.db.transaction(immediate=True):
                    service._pin_quarantine(operation,context["output_scope"],item.asset.id,item.asset.content_hash)
                    service.records.save_result(operation.id,"quarantine",dict(role="preview",content_hash=item.asset.content_hash,
                        path=str(path.relative_to(self.root))))
            raise
        finally:
            if item is not None:
                item.close()
