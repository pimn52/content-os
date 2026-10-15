"""Synthetic mux/owned preview tests. Input rights/frozen resolver are stubbed."""
from pathlib import Path
from types import SimpleNamespace
import pytest
from app.db import JobRepository, AssetRepository
from app.domain.models import JobStatus
from app.execution_admission import UseAdmissionError
from app.jobs.runner import JobExecutionError
from app.jobs import evaluation_talking_preview_handler as preview_module
from app.talking import planned_preview
from test_evaluation_talking_qa import (
    qa_owned, call_case, ownership, qa_case, bound_job, child_case, setup, candidate, talking_case,
)


@pytest.fixture
def preview_owned(qa_owned,monkeypatch):
    c = qa_owned
    c.handler(c.qa)
    JobRepository(c.db).update(c.qa.model_copy(update={"status":JobStatus.COMPLETED}))
    job = c.service.create_preview(c.operation)
    assert c.service.create_preview(c.operation) == job
    c.preview_job = JobRepository(c.db).update(job.model_copy(update={"status":JobStatus.RUNNING,"attempt":1}))
    monkeypatch.setattr(preview_module,"EvaluationTalkingService",lambda *_a:c.service)
    c.mux_calls = []
    def mux(argv,**kwargs):
        assert not c.db.connection.in_transaction
        assert kwargs["shell"] is False and kwargs["timeout"] == 300
        c.mux_calls.append(argv)
        Path(argv[-1]).write_bytes(b"synthetic preview exact Master mux")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(planned_preview.subprocess,"run",mux)
    c.preview_handler = preview_module.EvaluationTalkingPreviewHandler(c.db,c.service.root,
        ffmpeg_command="fake-ffmpeg-never-launched",probe=c.probe)
    return c


def test_exact_preview_full_scope_recovery_without_mux(preview_owned,monkeypatch):
    c = preview_owned
    before = tuple(tuple(r) for r in c.db.connection.execute("SELECT * FROM production_runs WHERE id=?",(str(c.parent.id),)))
    asset = c.preview_handler(c.preview_job)
    report = asset.metadata["talking_run_preview"]
    assert report["state"] == "review_only" and report["review_policy_version"] == 2
    assert "human_review_state" not in report
    assert c.service.scopes.get("asset",asset.id) == c.context["output_scope"]
    argv = c.mux_calls[0]
    assert argv[argv.index("-map")+1] == "0:v:0" and "1:a:0" in argv
    assert c.preview_handler(c.preview_job) == asset
    assert len(c.mux_calls) == 1 and len(c.calls) == 1
    assert tuple(tuple(r) for r in c.db.connection.execute("SELECT * FROM production_runs WHERE id=?",(str(c.parent.id),))) == before
    with pytest.raises(UseAdmissionError): AssetRepository(c.db).update(asset)


def test_preview_atomic_failure_quarantines_restrictions(preview_owned,monkeypatch):
    c = preview_owned
    save = c.service.records.save_result
    def fail(identity,role,payload):
        if role == "preview": raise ValueError("injected preview sidecar failure")
        return save(identity,role,payload)
    monkeypatch.setattr(c.service.records,"save_result",fail)
    with pytest.raises(JobExecutionError,match="预览未完成"): c.preview_handler(c.preview_job)
    assert c.service.records.result(c.operation.id,"preview") is None
    result = c.service.records.result(c.operation.id,"quarantine")
    assert (c.service.root/result["path"]).is_file()
    assert AssetRepository(c.db).get_by_content_hash(result["content_hash"]) is None
    assert c.service.scopes.scopes_for_hash(result["content_hash"]) == (c.context["output_scope"],)
    with pytest.raises(JobExecutionError,match="reconciliation"): c.preview_handler(c.preview_job)
    assert len(c.mux_calls) == 1


def test_preview_late_cancel_rejects_admission(preview_owned,monkeypatch):
    c = preview_owned
    mux = planned_preview.subprocess.run
    def cancel(*args,**kwargs):
        result = mux(*args,**kwargs)
        c.service.cancel(c.project,c.operation.id)
        return result
    monkeypatch.setattr(planned_preview.subprocess,"run",cancel)
    with pytest.raises(JobExecutionError): c.preview_handler(c.preview_job)
    assert c.service.records.result(c.operation.id,"preview") is None
    assert c.service.records.result(c.operation.id,"quarantine")["role"] == "preview"


@pytest.mark.parametrize("produced",[True,False])
def test_failed_probe_or_mux_has_durable_closeout_without_repeat(preview_owned,monkeypatch,produced):
    c = preview_owned
    if produced:
        monkeypatch.setattr(c.probe,"probe",lambda *_a:(_ for _ in ()).throw(ValueError("injected malformed preview probe")))
    else:
        def fail(argv,**kwargs):
            c.mux_calls.append(argv)
            return SimpleNamespace(returncode=1)
        monkeypatch.setattr(planned_preview.subprocess,"run",fail)
    with pytest.raises(JobExecutionError): c.preview_handler(c.preview_job)
    result = c.service.records.result(c.operation.id,"quarantine")
    assert result["state"] == "failed" and result["role"] == "preview"
    if produced:
        assert (c.service.root/result["path"]).read_bytes() == b"synthetic preview exact Master mux"
        assert c.service.scopes.scopes_for_hash(result["content_hash"]) == (c.context["output_scope"],)
        assert AssetRepository(c.db).get_by_content_hash(result["content_hash"]) is None
    else:
        assert "content_hash" not in result and "path" not in result
    with pytest.raises(JobExecutionError,match="reconciliation"): c.preview_handler(c.preview_job)
    assert len(c.mux_calls) == 1
