"""Explicit owned QA factory; generic evaluation dispatch remains closed."""
import json
import re
from pathlib import Path
from app.costs import UnknownProviderCostEstimator
from app.domain.asr_child import ASRChildRequest
from app.domain.maintained_runtime import MaintainedEvidenceReference
from app.domain.models import JobStatus
from app.execution_admission import UseAdmissionError, configured_admission_actor
from app.provider_execution import ProviderExecutionInProgress, ProviderExecutionRecordedFailure
from app.providers.maintained_asr_execution import prepare_maintained_asr_child
from app.providers.prepared import ExecutionPreparationError
from app.voice_evaluation_qa import VoiceEvaluationQAService, execute_evaluation_qa, require_completed_qa
from app.db import AudioAssetRepository, ProviderCallRepository, JobRepository
from app.jobs.runner import JobExecutionError
from app.budget import BudgetLimitError, ProviderCallError

class MaintainedVoiceEvaluationQAHandler:
    def __init__(self, db, root, *, options):
        self.db, self.root, self.options = db, Path(root).resolve(), dict(options)

    def require_dispatch(self, job):
        if type(self) is not MaintainedVoiceEvaluationQAHandler or getattr(self.db, "data_root", None) != self.root:
            raise UseAdmissionError("voice_qa_handler_unsupported")
        saved = JobRepository(self.db).get(job.id)
        if saved is None or saved.status != JobStatus.RUNNING or saved.attempt != job.attempt:
            raise UseAdmissionError("voice_qa_job_not_running")
        call = ProviderCallRepository(self.db).get_by_idempotency_key(f"voice-evaluation-qa:{job.id}")
        return VoiceEvaluationQAService(self.db, self.root).require_job(job, retained=bool(call and call.status == "completed"))

    def __call__(self, job):
        try:
            claim, _, receipt, source = self.require_dispatch(job)
            call = ProviderCallRepository(self.db).get_by_idempotency_key(f"voice-evaluation-qa:{job.id}")
            if call is not None:
                if call.status == "completed":
                    audio = AudioAssetRepository(self.db).get(claim.input_binding.audio_id)
                    require_completed_qa(self.db, self.root, audio, project_id=job.project_id, purpose="internal_evaluation")
                    return audio
                if call.status in ("reserved", "running"):
                    raise ProviderExecutionInProgress()
                raise ProviderExecutionRecordedFailure()
            required = {"environment", "base", "model_root", "origin_reference", "operator_key", "installation_id", "runtime_label", "machine_id"}
            if not required <= self.options.keys() or self.options.keys() - required - {"timeout_seconds", "max_bytes", "invocation_policy"}:
                raise UseAdmissionError("maintained_asr_configuration_invalid")
            if str(self.options["model_root"]) != receipt.review.identity.model:
                raise UseAdmissionError("voice_qa_model_changed")
            origin = json.loads(VoiceEvaluationQAService(self.db, self.root).scopes.admissions._file(
                receipt.review.execution_specification.runtime_origin.reference))
            if configured_admission_actor(self.options["operator_key"]) != origin["actor"]:
                raise UseAdmissionError("maintained_asr_operator_required")
            staging = self.root/"execution-staging"
            staging.mkdir(exist_ok=True)
            request = ASRChildRequest(model_root=receipt.review.identity.model, audio_path=str(source),
                audio_sha256=claim.input_binding.audio_sha256, language=claim.input_binding.language)
            with prepare_maintained_asr_child(**self.options, data_root=self.root, staging_parent=staging, request=request,
                    deployment_specification=receipt.review.execution_specification) as prepared:
                return execute_evaluation_qa(db=self.db, root=self.root, job=job, prepared=prepared,
                    cost_estimator=UnknownProviderCostEstimator())
        except UseAdmissionError as exc:
            raise JobExecutionError(str(exc), str(exc), retryable=False) from None
        except (ProviderExecutionInProgress, ProviderExecutionRecordedFailure):
            raise JobExecutionError("provider_call_reconciliation_required", "已有 QA 调用需核对；不会再次识别", retryable=False) from None
        except ExecutionPreparationError as exc:
            code = str(exc)
            if not re.fullmatch(r"[a-z0-9_]{1,100}", code): code = "maintained_asr_preparation_failed"
            raise JobExecutionError(code, code, retryable=False) from None
        except (BudgetLimitError, ProviderCallError):
            raise JobExecutionError("voice_qa_budget_or_accounting_blocked", "当前预算或账本阻止 QA", retryable=False) from None
        except Exception:
            raise JobExecutionError("voice_evaluation_qa_failed", "QA 未完成，请核对已有调用；不会再次识别", retryable=False) from None


def configured_voice_evaluation_qa_handler(config, db, environment):
    reference = environment.get("CONTENT_OS_MAINTAINED_ASR_CONFIG", "")
    key = environment.get("CONTENT_OS_MAINTAINED_ASR_OPERATOR_KEY", "")
    try:
        source = Path(reference)
        if not reference or not source.is_file() or source.stat().st_size > 128000 or not key:
            raise ValueError()
        document = json.loads(source.read_text(encoding="utf-8"))
        if set(document) != {"config_version", "runtime_options"} or type(document["config_version"]) is not int or document["config_version"] not in (1, 2):
            raise ValueError()
        options = document["runtime_options"]
        required = {"environment", "base", "model_root", "origin_reference", "installation_id", "runtime_label", "machine_id"}
        if not isinstance(options, dict) or not required <= options.keys() or options.keys() - required - {"timeout_seconds", "max_bytes", "invocation_policy"}:
            raise ValueError()
        expected = "isolated-zip-asr-v2" if document["config_version"] == 2 else "isolated-zip-asr-v1"
        if options.get("invocation_policy", "isolated-zip-asr-v1") != expected:
            raise ValueError()
        options = dict(options)
        options["origin_reference"] = MaintainedEvidenceReference.model_validate(options["origin_reference"])
        for name in ("environment", "base", "model_root"):
            value = Path(options[name])
            if not value.is_absolute(): raise ValueError()
            options[name] = value
        options["operator_key"] = key
    except (ValueError, TypeError, OSError):
        raise UseAdmissionError("maintained_asr_configuration_invalid") from None
    return MaintainedVoiceEvaluationQAHandler(db, config.data_root, options=options)
