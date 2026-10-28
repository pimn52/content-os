"""普通 Talking 使用现有 LatentSync 双运行时，预算和许可由 ProductionRun 核验。"""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import time

from app.domain.talking_child import TalkingChildRequest
from app.domain.talking_recipe import validate_parameters
from .latentsync import LATENTSYNC_MODEL
from .maintained_talking_dual import OwnedDualLatentSync, engine_bundle
from .maintained_talking_execution import (
    _artifact_path, _file_stamp, observe_talking_artifacts, require_reference_coverage,
)
from .prepared import ExecutionPreparationError
from .runtime_tree import _digest, _exact_path
from .talking import (
    TalkingConfigurationError, TalkingExecutionOptions, TalkingInputError,
    TalkingProviderResponseError, TalkingSynthesisResult, TalkingTimeout,
)


class MaintainedLatentSyncProvider:
    """在普通 Provider 合约内复用已运行的推理与媒体变换，不读取评估准入状态。"""

    provider_name = "latentsync"
    model = LATENTSYNC_MODEL
    is_local = True
    noncommercial_benchmark_only = True
    processing_resolution_px = 256
    stated_minimum_vram_gb = 6.5

    @staticmethod
    def validate_reference_duration(narration_duration_ms, reference_duration_ms):
        from .latentsync import LatentSyncProvider
        if reference_duration_ms < LatentSyncProvider._model_duration_ms(narration_duration_ms):
            raise TalkingInputError("talking_reference_alignment_not_covered")

    def __init__(self, data_root: str | Path, options: dict) -> None:
        self.data_root = Path(data_root).resolve()
        self.options = deepcopy(options)
        self.options["data_root"] = self.data_root
        self.options.setdefault("timeout_seconds", 3600)
        self.options.setdefault("max_bytes", 64 * 1024**3)
        try:
            if self.options.get("invocation_policy") != "maintained-latentsync-dual-runtime-v1":
                raise ValueError()
            for key in ("environment", "base", "engine", "vendor", "checkpoint", "hf", "torch", "tools"):
                path = Path(self.options[key])
                _exact_path(path, directory=key != "checkpoint")
                self.options[key] = path
            parameters = self.options["parameters"]
            validate_parameters({key: value for key, value in parameters.items()
                                 if key not in ("attention_policy", "allocator")})
            if (parameters.get("attention_policy") != "latentsync-retained-explicit-math-v1"
                    or parameters.get("allocator") != "expandable_segments:True"):
                raise ValueError()
            timeout = self.options["timeout_seconds"]
            if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                    or not math.isfinite(timeout) or timeout <= 0
                    or type(self.options["max_bytes"]) is not int or self.options["max_bytes"] <= 0):
                raise ValueError()
            for key in ("runtime_label", "machine_id"):
                if not isinstance(self.options[key], str) or not self.options[key].strip():
                    raise ValueError()
            for name in ("python.exe", "python310.dll", "Lib", "DLLs", "Lib/site-packages", "Library/bin"):
                _exact_path(self.options["engine"] / name, directory="." not in Path(name).name)
        except (KeyError, TypeError, ValueError, OSError, ExecutionPreparationError):
            raise TalkingConfigurationError("maintained_talking_configuration_invalid") from None
        self.runtime_label = self.options["runtime_label"]
        self.machine_id = self.options["machine_id"]
        self.execution_runtime = self.runtime_label
        self.execution_machine_id = self.machine_id

    @property
    def runtime_metadata(self):
        # Same local cost/capability facts as the underlying adapter.
        from .latentsync import LatentSyncRuntimeMetadata
        from app.domain.models import CostCategory, UsageCost
        from decimal import Decimal
        return LatentSyncRuntimeMetadata(self.provider_name, self.model, self.processing_resolution_px,
            self.stated_minimum_vram_gb, UsageCost(category=CostCategory.TALKING, amount=Decimal("0"),
                currency="USD", provider=self.provider_name, note="local LatentSync inference"))

    def synthesize(self, profile, narration, reference, output_path,
                   options: TalkingExecutionOptions | None = None) -> TalkingSynthesisResult:
        target = Path(output_path).resolve()
        if target.suffix.lower() != ".mp4" or not target.is_relative_to(self.data_root):
            raise TalkingInputError("maintained_talking_output_boundary_invalid")
        staging = self.data_root / "execution-staging"
        staging.mkdir(exist_ok=True)
        runtime = dict(self.options, staging_parent=staging)
        temporary = TemporaryDirectory(prefix="talking-", dir=staging)
        private = Path(temporary.name)
        try:
            request = TalkingChildRequest(profile=profile, narration=narration,
                reference_clip_id=reference.clip_id, reference_path=str(reference.source_path.resolve()),
                reference_sha256=_digest(reference.source_path, 4 * 1024**3)[1],
                reference_start_ms=reference.start_ms, reference_end_ms=reference.end_ms)
            request.verify_inputs(self.data_root)
            require_reference_coverage(request)
            artifacts = observe_talking_artifacts(runtime)
            stamps = {_artifact_path(runtime, item.name): _file_stamp(_artifact_path(runtime, item.name))
                      for item in artifacts}
            engine_files = []
            for name in ("python.exe", "python310.dll"):
                path = runtime["engine"] / name
                size, sha = _digest(path, 64 * 1024**2)
                stamps[path] = _file_stamp(path)
                engine_files.append(dict(name=name, size_bytes=size, sha256=sha))
            runtime["model_artifacts"] = [item.model_dump(mode="json") for item in artifacts]
            runtime["resource_copies"] = [dict(source_name=item.name, destination=item.name.removeprefix("vendor/"))
                for item in artifacts if item.name.startswith("vendor/configs/")
                or item.name in {"vendor/checkpoints/whisper/tiny.pt", "vendor/latentsync/utils/mask.png"}]
            (private / "engine.pyz").write_bytes(engine_bundle())
            owner = OwnedDualLatentSync(runtime, private, time.monotonic() + runtime["timeout_seconds"])
            result = owner.synthesize(request, reference=reference, execution_options=options)
            request.verify_inputs(self.data_root)
            for path, stamp in stamps.items():
                _exact_path(path, directory=False)
                if _file_stamp(path) != stamp:
                    raise ExecutionPreparationError("maintained_talking_runtime_changed")
            _exact_path(result.video_path, directory=False)
            output_size, output_sha = _digest(result.video_path, 4 * 1024**3)
            if output_size == 0:
                raise TalkingProviderResponseError("maintained_talking_output_empty")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(result.video_path, target)
            if _digest(target, 4 * 1024**3) != (output_size, output_sha):
                raise TalkingProviderResponseError("maintained_talking_output_changed")
            evidence = dict(provider=self.provider_name, model=self.model, runtime=self.runtime_label,
                machine_id=self.machine_id, parameters=runtime["parameters"],
                narration_audio_id=str(narration.id), narration_sha256=narration.content_hash,
                reference_clip_id=str(reference.clip_id), reference_sha256=request.reference_sha256,
                reference_start_ms=reference.start_ms, reference_end_ms=reference.end_ms,
                engine_files=engine_files, model_artifacts=runtime["model_artifacts"],
                output_sha256=output_sha, qa_state="pending")
            target.with_suffix(".execution.json").write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
            return TalkingSynthesisResult(target, provider_version=result.provider_version, expected_video_fps=result.expected_video_fps)
        except BaseException as exc:
            temporary._finalizer.detach()
            (private / "failure.json").write_text(json.dumps(dict(output_admitted=False, retry_automatically=False)), encoding="utf-8")
            if isinstance(exc, (KeyboardInterrupt, SystemExit, TalkingInputError, TalkingProviderResponseError, TalkingConfigurationError)):
                raise
            if isinstance(exc, (subprocess.TimeoutExpired, TimeoutError)):
                raise TalkingTimeout("maintained_talking_timeout") from None
            if isinstance(exc, ExecutionPreparationError):
                raise TalkingProviderResponseError(str(exc)) from None
            raise TalkingProviderResponseError("maintained_talking_generation_failed") from None
        finally:
            if temporary._finalizer.alive:
                temporary.cleanup()


def configured_maintained_talking_provider(config, environment):
    """读取已配置的本地路径；普通执行无需内部评估操作员凭据。"""
    try:
        path = Path(environment.get("CONTENT_OS_MAINTAINED_TALKING_CONFIG", ""))
        if not path.is_file() or not 0 < path.stat().st_size <= 128000:
            raise ValueError()
        document = json.loads(path.read_text(encoding="utf-8"))
        if (set(document) != {"config_version", "runtime_options"}
                or type(document["config_version"]) is not int or document["config_version"] != 2
                or not isinstance(document["runtime_options"], dict)
                or "operator_key" in document["runtime_options"]):
            raise ValueError()
        return MaintainedLatentSyncProvider(config.data_root, document["runtime_options"])
    except (OSError, ValueError, TypeError):
        raise TalkingConfigurationError("maintained_talking_configuration_invalid") from None
