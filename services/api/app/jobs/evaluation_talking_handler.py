"""Exact maintained Talking factory selected by its explicit Worker policy.

Completed-ledger recovery never prepares or launches a child. Failed/unknown
calls are surfaced for reconciliation with their original immutable claim.
"""
import json
import re
from pathlib import Path
from uuid import UUID
from app.db import JobRepository, ProviderCallRepository
from app.domain.models import JobStatus, JobType
from app.domain.talking_child import TalkingChildRequest
from app.domain.maintained_runtime import MaintainedEvidenceReference
from app.costs import UnknownProviderCostEstimator
from app.execution_admission import UseAdmissionError
from app.budget import BudgetLimitError, ProviderCallError
from app.evaluation_talking import EvaluationTalkingService
from app.evaluation_talking_execution import execute_evaluation_talking, call_key, require_generation_result
from app.provider_execution import ProviderExecutionInProgress, ProviderExecutionRecordedFailure
from app.providers.maintained_talking_execution import prepare_maintained_talking_child
from app.providers.maintained_talking_dual import prepare_dual_talking_child
from app.providers.prepared import ExecutionPreparationError
from app.talking.planned_preview import _source_path
from .runner import JobExecutionError

REQUIRED_OPTIONS = {"environment","base","vendor","checkpoint","hf","torch","tools","origin_reference",
    "installation_id","runtime_label","machine_id","gpu_uuid","parameters"}
OPTIONAL_OPTIONS = {"timeout_seconds","max_bytes"}
DUAL_POLICY = "maintained-latentsync-dual-runtime-v1"


def require_options(options):
    dual = "engine" in options or "invocation_policy" in options
    required = REQUIRED_OPTIONS | ({"engine", "invocation_policy"} if dual else set())
    if (not required <= options.keys() or options.keys()-required-OPTIONAL_OPTIONS-{"operator_key"}
        or (dual and options["invocation_policy"] != DUAL_POLICY)):
        raise UseAdmissionError("maintained_talking_configuration_invalid")
    return dual


class MaintainedEvaluationTalkingHandler:
    def __init__(self, db, root, *, options, policy="evaluation-talking-run-v1"):
        if policy not in ("evaluation-talking-run-v1", "evaluation-talking-calibration-v1"):
            raise UseAdmissionError("talking_execution_policy_unsupported")
        self.db, self.root, self.options = db, Path(root).resolve(), dict(options)
        self.policy = policy

    def require_dispatch(self, job):
        if type(self) is not MaintainedEvaluationTalkingHandler or getattr(self.db,"data_root",None) != self.root:
            raise UseAdmissionError("evaluation_talking_handler_unsupported")
        saved = JobRepository(self.db).get(job.id)
        if (job.type != JobType.GENERATE_TALKING or saved is None or saved.status != JobStatus.RUNNING
            or job.status != JobStatus.RUNNING or saved.attempt != job.attempt):
            raise UseAdmissionError("evaluation_talking_job_not_running")
        service = EvaluationTalkingService(self.db,self.root)
        operation = service.require_job_identity(job)
        if operation.policy != self.policy:
            raise UseAdmissionError("talking_execution_policy_mismatch")
        if service.records.result(operation.id,"cancellation") is not None:
            raise UseAdmissionError("evaluation_talking_operation_cancelled")
        call = ProviderCallRepository(self.db).get_by_idempotency_key(call_key(operation))
        if call is not None:
            if call.status == "completed":
                context = service.require_retained_operation(operation)
                result = service.records.result(operation.id,"generation")
                require_generation_result(service,operation,result)
                return operation,context,call
            if call.status in ("reserved","running"):
                raise ProviderExecutionInProgress()
            raise ProviderExecutionRecordedFailure()
        return operation,service.require_current_operation(operation,job=job),None

    def __call__(self, job):
        try:
            operation,context,call = self.require_dispatch(job)
            if call is not None:
                return require_generation_result(EvaluationTalkingService(self.db,self.root),operation,
                    call.result_payload["result"])[0]
            from .lease_fence import require_owned_lease
            require_owned_lease(self.db,job)
            options = dict(self.options)
            dual = require_options(options)
            if operation.policy == "evaluation-talking-calibration-v1" and not dual:
                raise UseAdmissionError("talking_calibration_dual_runtime_required")
            options.setdefault("timeout_seconds",3600)
            options.setdefault("max_bytes",64*1024**3)
            staging = self.root/"execution-staging"
            staging.mkdir(exist_ok=True)
            request = TalkingChildRequest(profile=context["profile"],
                narration=context["audio"].model_copy(update={"source_file":str(_source_path(context["audio"].source_file,self.root))}),
                reference_clip_id=context["clip"].id,reference_path=str(_source_path(context["source"].source_file,self.root)),
                reference_sha256=context["source"].content_hash,reference_start_ms=context["clip"].start_ms,
                reference_end_ms=context["clip"].end_ms,slice_plan=context["slice_plan"])
            factory = prepare_dual_talking_child if dual else prepare_maintained_talking_child
            with factory(**options,data_root=self.root,staging_parent=staging,request=request) as prepared:
                return execute_evaluation_talking(db=self.db,root=self.root,job=job,prepared=prepared,cost_estimator=UnknownProviderCostEstimator())[0]
        except (ProviderExecutionInProgress,ProviderExecutionRecordedFailure):
            raise JobExecutionError("provider_call_reconciliation_required","已有 Talking 调用需要核对，不会重复生成",retryable=False) from None
        except (UseAdmissionError,ExecutionPreparationError) as exc:
            code = str(exc)
            if not re.fullmatch(r"[a-z0-9_]{1,100}",code): code = "evaluation_talking_admission_failed"
            raise JobExecutionError(code,code,retryable=False) from None
        except (BudgetLimitError,ProviderCallError):
            raise JobExecutionError("evaluation_talking_budget_or_accounting_blocked","预算或账本阻止本次生成",retryable=False) from None
        except Exception:
            raise JobExecutionError("evaluation_talking_generation_failed","生成未完成，需核对原调用记录",retryable=False) from None


def configured_evaluation_talking_handler(config,db,environment):
    reference = environment.get("CONTENT_OS_MAINTAINED_TALKING_CONFIG","")
    key = environment.get("CONTENT_OS_MAINTAINED_TALKING_OPERATOR_KEY","")
    try:
        source = Path(reference)
        if not reference or not source.is_file() or source.stat().st_size > 128000 or not key:
            raise ValueError()
        document = json.loads(source.read_text(encoding="utf-8"))
        if set(document) != {"config_version","runtime_options"} or type(document["config_version"]) is not int or document["config_version"] not in (1,2):
            raise ValueError()
        options = document["runtime_options"]
        if not isinstance(options,dict) or "operator_key" in options: raise ValueError()
        dual = require_options(options)
        if environment.get("CONTENT_OS_TALKING_EXECUTION_POLICY") == "evaluation-talking-calibration-v1" and not dual:
            raise UseAdmissionError("talking_calibration_dual_runtime_required")
        if dual != (document["config_version"] == 2): raise ValueError()
        options = dict(options)
        options["origin_reference"] = MaintainedEvidenceReference.model_validate(options["origin_reference"])
        if not isinstance(options["installation_id"], str): raise ValueError()
        options["installation_id"] = UUID(options["installation_id"])
        for name in ("environment","base","vendor","checkpoint","hf","torch","tools") + (("engine",) if dual else ()):
            options[name] = Path(options[name])
            if not options[name].is_absolute(): raise ValueError()
        options["operator_key"] = key
    except UseAdmissionError:
        raise
    except (ValueError,TypeError,OSError):
        raise UseAdmissionError("maintained_talking_configuration_invalid") from None
    return MaintainedEvaluationTalkingHandler(db,config.data_root,options=options,
        policy=environment.get("CONTENT_OS_TALKING_EXECUTION_POLICY","evaluation-talking-run-v1"))
