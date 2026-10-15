"""Fixed application-owned handler. No API-supplied runner or permit flags."""
import json
import re
from pathlib import Path

from app.costs import UnknownProviderCostEstimator
from app.execution_admission import UseAdmissionError
from app.maintained_voice_job import execute_configured_maintained_voice_job
from app.media.audio_importer import AudioImporter
from app.media.ffprobe import FFProbeAdapter
from app.provider_execution import ProviderExecutionInProgress, ProviderExecutionRecordedFailure
from app.voice_draft import VoiceDraftService
from app.jobs.runner import JobExecutionError
from app.budget import BudgetLimitError, ProviderCallError
from app.domain.maintained_runtime import MaintainedEvidenceReference
from app.providers.prepared import ExecutionPreparationError


class MaintainedVoiceDraftHandler:
    def __init__(self, db, root, *, options, ffmpeg, ffprobe):
        self.db, self.root = db, Path(root).resolve()
        self.options = dict(options)
        self.ffmpeg, self.ffprobe = ffmpeg, ffprobe

    def require_dispatch(self, job):
        if type(self) is not MaintainedVoiceDraftHandler or getattr(self.db, "data_root", None) != self.root:
            raise UseAdmissionError("voice_draft_handler_unsupported")
        return VoiceDraftService(self.db, self.root).require_job(job)

    def __call__(self, job):
        try:
            binding = self.require_dispatch(job)
            outputs, staging = self.root / "generated", self.root / "execution-staging"
            outputs.mkdir(exist_ok=True); staging.mkdir(exist_ok=True)
            return execute_configured_maintained_voice_job(db=self.db, data_root=self.root,
                output_root=outputs, staging_parent=staging, ffmpeg=self.ffmpeg, job=job,
                execution_admission_id=binding.admission_id,
                importer=AudioImporter(self.db, self.root, FFProbeAdapter(self.ffprobe)),
                cost_estimator=UnknownProviderCostEstimator(), runtime_options=self.options)
        except UseAdmissionError as exc:
            raise JobExecutionError(str(exc), str(exc), retryable=False) from None
        except (ProviderExecutionInProgress, ProviderExecutionRecordedFailure):
            raise JobExecutionError("provider_call_reconciliation_required",
                "existing provider call requires explicit reconciliation; no new generation attempted", retryable=False) from None
        except ExecutionPreparationError as exc:
            code = str(exc)
            if not re.fullmatch(r"[a-z0-9_]{1,100}", code): code = "maintained_voice_preparation_failed"
            raise JobExecutionError(code, code, retryable=False) from None
        except (BudgetLimitError, ProviderCallError):
            raise JobExecutionError("voice_draft_budget_or_accounting_blocked",
                "current budget or accounting blocks generation; inspect existing records", retryable=False) from None


def configured_voice_draft_handler(config, db, environment):
    """Operator-owned JSON configuration, independent of HTTP request fields."""
    reference = environment.get("CONTENT_OS_MAINTAINED_VOICE_CONFIG", "")
    key = environment.get("CONTENT_OS_MAINTAINED_VOICE_OPERATOR_KEY", "")
    try:
        source = Path(reference)
        if not reference or not source.is_file() or source.stat().st_size > 128_000 or not key:
            raise ValueError()
        document = json.loads(source.read_text(encoding="utf-8"))
        if set(document) != {"config_version", "runtime_options"} or type(document["config_version"]) is not int or document["config_version"] != 1:
            raise ValueError()
        options = document["runtime_options"]
        required = {"environment", "base", "model_root", "origin_reference", "parameters", "installation_id", "runtime_label", "machine_id"}
        if not isinstance(options, dict) or not required <= options.keys() or options.keys() - required - {"gpu_uuid", "max_bytes", "timeout_seconds"}:
            raise ValueError()
        options = dict(options)
        options["origin_reference"] = MaintainedEvidenceReference.model_validate(options["origin_reference"])
        for name in ("environment", "base", "model_root"):
            value = Path(options[name])
            if not value.is_absolute(): raise ValueError()
            options[name] = value
        options["operator_key"] = key
    except (ValueError, TypeError, OSError):
        raise UseAdmissionError("maintained_voice_configuration_invalid") from None
    return MaintainedVoiceDraftHandler(db, config.data_root, options=options, ffmpeg=config.ffmpeg, ffprobe=config.ffprobe)
