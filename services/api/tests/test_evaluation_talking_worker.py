"""Explicit CLI factory registration only; no real Worker/media child."""
import json
from uuid import UUID, uuid4
import pytest
from app.db import Database
from app.domain.models import JobType
from app.worker_cli import parse_config, build_runner
from app.jobs.evaluation_talking_handler import MaintainedEvaluationTalkingHandler
from app.jobs.evaluation_talking_qa_handler import EvaluationTalkingQaHandler
from app.jobs.evaluation_talking_preview_handler import EvaluationTalkingPreviewHandler
from app.execution_admission import UseAdmissionError
from app.providers.talking import TalkingConfigurationError
from app.provider_execution import ProviderExecutionInProgress, ProviderExecutionRecordedFailure
from app.domain.models import JobStatus
from test_job_runner import make_job, make_runner
from app.jobs.store import JobStore


def config(root):
    args=["--once","--db",str(root/"worker.sqlite"),"--data-root",str(root)]
    for kind in (JobType.GENERATE_TALKING,JobType.VERIFY_TALKING,JobType.PREPARE_TALKING_RUN_PREVIEW):
        args.extend(["--job-type",kind.value])
    return parse_config(args)


@pytest.mark.parametrize("version,policy",[(1,"evaluation-talking-run-v1"),(2,"evaluation-talking-run-v1"),(2,"evaluation-talking-calibration-v1"),(1,"evaluation-talking-calibration-v1")])
def test_explicit_policy_builds_only_exact_owned_handlers(tmp_path,monkeypatch,version,policy):
    monkeypatch.setenv("CONTENT_OS_TALKING_EXECUTION_POLICY",policy)
    options={key:str(tmp_path/key) for key in ("environment","base","vendor","checkpoint","hf","torch","tools")}
    options.update(origin_reference={"reference":"origin.json","sha256":"a"*64},
        installation_id=str(uuid4()),runtime_label="synthetic",machine_id="synthetic",gpu_uuid="synthetic",parameters={})
    if version==2:options.update(engine=str(tmp_path/"engine"),invocation_policy="maintained-latentsync-dual-runtime-v1")
    path=tmp_path/"synthetic-config.json"
    path.write_text(json.dumps({"config_version":version,"runtime_options":options}))
    monkeypatch.setenv("CONTENT_OS_MAINTAINED_TALKING_CONFIG",str(path))
    monkeypatch.setenv("CONTENT_OS_MAINTAINED_TALKING_OPERATOR_KEY","synthetic-key-not-used")
    monkeypatch.setattr("app.worker_cli._build_talking_provider",lambda *_a:pytest.fail("named policy cannot fall back"))
    with Database(tmp_path/"worker.sqlite") as db:
        if policy == "evaluation-talking-calibration-v1" and version == 1:
            with pytest.raises(UseAdmissionError,match="dual_runtime_required"):build_runner(config(tmp_path),db)
            return
        runner=build_runner(config(tmp_path),db)
        assert {key:type(value) for key,value in runner._handlers.items()} == {
            JobType.GENERATE_TALKING:MaintainedEvaluationTalkingHandler,
            JobType.VERIFY_TALKING:EvaluationTalkingQaHandler,
            JobType.PREPARE_TALKING_RUN_PREVIEW:EvaluationTalkingPreviewHandler}
        assert db.data_root == tmp_path.resolve()
        assert all(value.policy==policy for value in runner._handlers.values())
        # JSON configuration must reach strict machine observation as a UUID,
        # not fail only after an expensive complete runtime inventory.
        assert runner._handlers[JobType.GENERATE_TALKING].options["installation_id"] == UUID(options["installation_id"])


@pytest.mark.parametrize("installation", [None, 7, {}, "not-a-uuid"])
def test_invalid_installation_identifier_is_rejected_before_preparation(tmp_path,monkeypatch,installation):
    options={key:str(tmp_path/key) for key in ("environment","base","vendor","checkpoint","hf","torch","tools")}
    options.update(origin_reference={"reference":"origin.json","sha256":"a"*64},installation_id=installation,
        runtime_label="synthetic",machine_id="synthetic",gpu_uuid="synthetic",parameters={})
    path=tmp_path/"invalid-installation.json"
    path.write_text(json.dumps(dict(config_version=1,runtime_options=options)))
    monkeypatch.setenv("CONTENT_OS_TALKING_EXECUTION_POLICY","evaluation-talking-run-v1")
    monkeypatch.setenv("CONTENT_OS_MAINTAINED_TALKING_CONFIG",str(path))
    monkeypatch.setenv("CONTENT_OS_MAINTAINED_TALKING_OPERATOR_KEY","synthetic-not-used")
    with Database(tmp_path/"invalid-installation.sqlite") as db:
        with pytest.raises(UseAdmissionError,match="configuration_invalid"):
            build_runner(config(tmp_path),db)


@pytest.mark.parametrize("policy",["evaluation-talking-run-v1","evaluation-talking-calibration-v1","unknown"])
def test_invalid_named_configuration_has_no_legacy_fallback(tmp_path,monkeypatch,policy):
    monkeypatch.setenv("CONTENT_OS_TALKING_EXECUTION_POLICY",policy)
    monkeypatch.delenv("CONTENT_OS_MAINTAINED_TALKING_CONFIG",raising=False)
    monkeypatch.setattr("app.worker_cli._build_talking_provider",lambda *_a:pytest.fail("invalid named policy cannot fall back"))
    with Database(tmp_path/"worker.sqlite") as db:
        with pytest.raises((UseAdmissionError,TalkingConfigurationError)): build_runner(config(tmp_path),db)


@pytest.mark.parametrize("version,policy",[(1,"maintained-latentsync-dual-runtime-v1"),(2,None),(2,"unknown"),(True,None)])
def test_mixed_configuration_versions_cannot_fall_back(tmp_path,monkeypatch,version,policy):
    monkeypatch.setenv("CONTENT_OS_TALKING_EXECUTION_POLICY","evaluation-talking-run-v1")
    options={key:str(tmp_path/key) for key in ("environment","base","vendor","checkpoint","hf","torch","tools")}
    options.update(origin_reference={"reference":"origin.json","sha256":"a"*64},installation_id=str(uuid4()),
        runtime_label="synthetic",machine_id="synthetic",gpu_uuid="synthetic",parameters={})
    if policy:options.update(engine=str(tmp_path/"engine"),invocation_policy=policy)
    path=tmp_path/"mixed-config.json";path.write_text(json.dumps(dict(config_version=version,runtime_options=options)))
    monkeypatch.setenv("CONTENT_OS_MAINTAINED_TALKING_CONFIG",str(path));monkeypatch.setenv("CONTENT_OS_MAINTAINED_TALKING_OPERATOR_KEY","synthetic")
    monkeypatch.setattr("app.worker_cli._build_talking_provider",lambda *_a:pytest.fail("no fallback"))
    with Database(tmp_path/"mixed.sqlite") as db:
        with pytest.raises(UseAdmissionError):build_runner(config(tmp_path),db)


@pytest.mark.parametrize("failure",[ProviderExecutionInProgress(),ProviderExecutionRecordedFailure(),ValueError("private?key=SECRET")])
def test_owned_dispatch_failure_is_terminal_and_private(tmp_path,monkeypatch,failure):
    # Focused runner-boundary reproduction: authority resolution is deliberately
    # replaced here only to isolate its exception semantics, not its validity.
    with Database(tmp_path/"dispatch.sqlite") as db:
        store=JobStore(db);job=store.enqueue(make_job("synthetic-dispatch-error"))
        handler=MaintainedEvaluationTalkingHandler(db,tmp_path,options={})
        def reject(_job): raise failure
        monkeypatch.setattr(handler,"require_dispatch",reject)
        result=make_runner(store,{job.type:handler}).run_once()
        assert result.status == JobStatus.FAILED
        assert "SECRET" not in result.error_code and "SECRET" not in result.error_message


def test_preflight_malformed_input_has_fixed_private_reason(tmp_path,monkeypatch):
    from app.evaluation_talking import EvaluationTalkingService
    from app.domain.evaluation_talking import TalkingSelection
    from test_evaluation_talking_use import review
    project,_,value=review(tmp_path)
    with Database(tmp_path/"preflight.sqlite") as db:
        service=EvaluationTalkingService(db,tmp_path)
        def invalid(*_a): raise ValueError("private/path?key=SECRET")
        monkeypatch.setattr(service,"_context",invalid)
        result=service.preflight(project,TalkingSelection(**value["selection"]))
        assert result["eligible"] is False
        assert result["reasons"] == ["evaluation_talking_evidence_invalid"]
