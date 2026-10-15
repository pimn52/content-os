"""Owned technical QA, reusing the ordinary Talking verifier.

Selected only by the explicit evaluation Talking Worker policy.
No inference, ASR, human decision or new execution allowance is created here.
"""
from pathlib import Path
import re
from app.db import AssetRepository, JobRepository
from app.domain.models import JobStatus, JobType, execution_canonical_sha256 as digest
from app.domain.evaluation_talking import EvaluationTalkingQaResult
from app.evaluation_talking import EvaluationTalkingService
from app.evaluation_talking_execution import require_generation_result
from app.execution_admission import UseAdmissionError
from app.media.importer import MediaImporter
from app.talking.planned_preview import _source_path
from app.talking_qa import verify_talking_output, apply_talking_qa
from .runner import JobExecutionError


class EvaluationTalkingQaHandler:
    def __init__(self, db, root, *, probe, policy="evaluation-talking-run-v1"):
        if policy not in ("evaluation-talking-run-v1", "evaluation-talking-calibration-v1"):
            raise UseAdmissionError("talking_execution_policy_unsupported")
        self.db, self.root, self.probe = db, Path(root).resolve(), probe
        self.policy = policy

    def require_dispatch(self, job):
        if type(self) is not EvaluationTalkingQaHandler or getattr(self.db,"data_root",None) != self.root:
            raise UseAdmissionError("evaluation_talking_handler_unsupported")
        saved = JobRepository(self.db).get(job.id)
        # Worker heartbeat updates only the observation timestamp; retain every
        # other typed Job field and reject backward time/status/attempt changes.
        if (job.type != JobType.VERIFY_TALKING or saved is None or job.status != JobStatus.RUNNING
            or saved.updated_at < job.updated_at
            or saved.model_dump(exclude={"updated_at"}) != job.model_dump(exclude={"updated_at"})):
            raise UseAdmissionError("evaluation_talking_job_not_running")
        service = EvaluationTalkingService(self.db,self.root)
        operation = service.require_job_identity(job)
        if operation.policy != self.policy:
            raise UseAdmissionError("talking_execution_policy_mismatch")
        context = service.require_retained_operation(operation)
        output,result = require_generation_result(service,operation,service.records.result(operation.id,"generation"))
        generation = JobRepository(self.db).get(operation.generation_job_id)
        audio = context["audio"]
        if (generation.status != JobStatus.COMPLETED or audio.id != job.payload.master_audio_id
            or audio.content_hash != job.payload.master_sha256):
            raise UseAdmissionError("evaluation_talking_qa_input_changed")
        MediaImporter._verify_content_hash(_source_path(audio.source_file,self.root),audio.content_hash)
        service.generation_guard(operation,job,context)
        return service,operation,context,output,result

    def __call__(self, job):
        try:
            return self._execute(job)
        except JobExecutionError:
            raise
        except UseAdmissionError as exc:
            code = str(exc)
            if not re.fullmatch(r"[a-z0-9_]{1,100}",code): code = "evaluation_talking_qa_admission_failed"
            raise JobExecutionError(code,code,retryable=False) from None
        except Exception:
            raise JobExecutionError("evaluation_talking_qa_failed","Talking 技术 QA 未完成，需核对原记录",retryable=False) from None

    def _execute(self, job):
        service,operation,context,output,generation_result = self.require_dispatch(job)
        previous = service.records.result(operation.id,"qa")
        if previous is not None:
            if previous["qa_state"] == "failed":
                raise JobExecutionError("talking_qa_failed","Talking 技术 QA 未通过",retryable=False)
            return output
        path = _source_path(output.source_file,self.root)
        report = verify_talking_output(output.model_copy(update={"source_file":str(path)}),context["audio"],self.probe,
            evidence_reference=f"talking-qa-job:{job.id}:output:{output.content_hash}",
            expected_duration_ms=job.payload.master_end_ms-job.payload.master_start_ms)
        updated = apply_talking_qa(output,report)
        metadata = dict(updated.metadata)
        generation = dict(metadata["talking_generation"])
        generation["qa"] = dict(generation["qa"],job_id=str(job.id))
        metadata["talking_generation"] = generation
        updated = updated.model_copy(update={"metadata":metadata})
        result = EvaluationTalkingQaResult(operation_id=operation.id,job_id=job.id,asset_id=output.id,
            asset_sha256=output.content_hash,generation_result_sha256=digest(generation_result.model_dump(mode="json")),
            asset_payload_sha256=digest(updated.model_dump(mode="json")),qa_state=generation["qa_state"],qa=generation["qa"])
        # Re-observe rights and exact bytes outside SQLite's write reservation.
        current = service.require_retained_operation(operation)
        if current["facts"] != context["facts"]:
            raise UseAdmissionError("evaluation_talking_authority_changed")
        MediaImporter._verify_content_hash(path,output.content_hash)
        MediaImporter._verify_content_hash(_source_path(context["audio"].source_file,self.root),job.payload.master_sha256)
        if require_generation_result(service,operation,generation_result.model_dump(mode="json"))[0] != output:
            raise UseAdmissionError("evaluation_talking_qa_input_changed")
        with self.db.transaction(immediate=True):
            service.generation_guard(operation,job,context)
            if (AssetRepository(self.db).get(output.id) != output
                or service.records.result(operation.id,"generation") != generation_result.model_dump(mode="json")
                or JobRepository(self.db).get(operation.generation_job_id).status != JobStatus.COMPLETED):
                raise UseAdmissionError("evaluation_talking_qa_input_changed")
            if service.records.result(operation.id,"qa") is not None:
                raise UseAdmissionError("evaluation_talking_qa_already_decided")
            # Narrow role writer: only the verified projection above changes.
            # Full inherited media restrictions and original payload proof stay.
            self.db.connection.execute("UPDATE assets SET payload=? WHERE id=?",
                (updated.model_dump_json(),str(output.id)))
            service.records.save_result(operation.id,"qa",result.model_dump(mode="json"))
        if not report.automated_verified:
            raise JobExecutionError("talking_qa_failed","Talking 技术 QA 未通过",retryable=False)
        return updated
