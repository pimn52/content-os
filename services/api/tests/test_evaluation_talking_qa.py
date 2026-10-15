"""Synthetic QA ownership/projection only; retained input resolver is stubbed.

Temporary SQLite/files, static probe, fake generation child. No normal-flow
admission, native tool invocation or subjective review evidence is claimed.
"""
import pytest
from app.db import JobRepository, AssetRepository
from app.domain.models import JobStatus
from app.execution_scope import job_use_identity, require_evaluation_lane_closed
from app.execution_admission import UseAdmissionError
from app.jobs.runner import JobExecutionError
from app.jobs import evaluation_talking_qa_handler as qa_module
from app import evaluation_talking_execution as execution
from test_evaluation_talking_execution import (
    call_case, ownership, qa_case, bound_job, child_case, setup, candidate, talking_case, run,
)
from test_media_import import _StaticProbe


@pytest.fixture
def qa_owned(call_case,monkeypatch):
    c = call_case
    audio = c.context["audio"]
    metadata = {"voice_generation":{"qa_state":"verified","qa":{
        "copy_coverage":1.0,"missing_token_count":0,"duplicate_token_count":0,"substitution_token_count":0}}}
    audio = audio.model_copy(update={"metadata":metadata})
    c.context["audio"] = audio
    c.request = c.request.model_copy(update={"narration":audio})
    # Synthetic graph fixture only; not an adopted Master or permission grant.
    c.db.connection.execute("INSERT INTO audio_assets(id,content_hash,payload) VALUES (?,?,?)",
        (str(audio.id),audio.content_hash,audio.model_dump_json()))
    c.job = JobRepository(c.db).update(c.job.model_copy(update={"payload":
        c.job.payload.model_copy(update={"narration_audio_id":audio.id})}))
    sha = job_use_identity(c.job)
    c.db.connection.execute("UPDATE evaluation_talking_owned_jobs SET job_sha256=? WHERE job_id=?",(sha,str(c.job.id)))
    c.db.connection.execute("UPDATE execution_use_scopes SET subject_sha256=? WHERE subject_kind='job' AND subject_id=?",(sha,str(c.job.id)))
    c.asset,c.result = run(c)
    JobRepository(c.db).update(c.job.model_copy(update={"status":JobStatus.COMPLETED}))
    monkeypatch.setattr(c.service,"require_retained_operation",lambda *_a:c.context)
    monkeypatch.setattr(qa_module,"EvaluationTalkingService",lambda *_a:c.service)
    qa = c.service.create_qa(c.operation)
    assert c.service.create_qa(c.operation) == qa
    c.qa = JobRepository(c.db).update(qa.model_copy(update={"status":JobStatus.RUNNING,"attempt":1}))
    class Probe:
        def probe(self,path):
            assert not c.db.connection.in_transaction
            value = _StaticProbe().probe(path)
            return value.__class__(duration_ms=c.qa.payload.master_end_ms-c.qa.payload.master_start_ms,
                width=value.width,height=value.height,fps=value.fps,has_audio=True,
                metadata={"streams":[{"codec_type":"video"},{"codec_type":"audio"}]})
    c.probe = Probe()
    c.handler = qa_module.EvaluationTalkingQaHandler(c.db,c.service.root,probe=c.probe)
    return c


def test_qa_persisted_projection_replay_and_parent_unchanged(qa_owned,monkeypatch):
    c = qa_owned
    parent = tuple(tuple(r) for r in c.db.connection.execute("SELECT * FROM production_runs WHERE id=?",(str(c.parent.id),)))
    output = c.handler(c.qa)
    assert output.metadata["talking_generation"]["qa_state"] == "verified"
    assert "human_review_state" not in output.metadata["talking_generation"]
    assert c.service.scopes.get("asset",output.id) == c.context["output_scope"]
    assert c.service.scopes.get("job",c.qa.id) == c.context["execution_scope"]
    assert execution.require_generation_result(c.service,c.operation,c.result.model_dump(mode="json"))[0] == output
    monkeypatch.setattr(c.probe,"probe",lambda *_a:pytest.fail("completed QA cannot reprobe"))
    assert c.handler(c.qa) == output
    assert len(c.calls) == 1
    assert tuple(tuple(r) for r in c.db.connection.execute("SELECT * FROM production_runs WHERE id=?",(str(c.parent.id),))) == parent
    with pytest.raises(UseAdmissionError): require_evaluation_lane_closed(c.db,c.qa)


def test_unrecorded_or_modified_projection_denied(qa_owned):
    c = qa_owned
    output = c.asset.model_copy(update={"metadata":dict(c.asset.metadata,talking_generation=
        dict(c.asset.metadata["talking_generation"],qa_state="verified",qa={"automated_verified":True}))})
    c.db.connection.execute("UPDATE assets SET payload=? WHERE id=?",(output.model_dump_json(),str(output.id)))
    with pytest.raises(UseAdmissionError,match="qa_projection_changed"):
        execution.require_generation_result(c.service,c.operation,c.result.model_dump(mode="json"))
    c.db.connection.execute("UPDATE assets SET payload=? WHERE id=?",(c.asset.model_dump_json(),str(c.asset.id)))
    output = c.handler(c.qa)
    metadata = dict(output.metadata)
    metadata["talking_generation"] = dict(metadata["talking_generation"],qa_state="failed")
    c.db.connection.execute("UPDATE assets SET payload=? WHERE id=?",(output.model_copy(update={"metadata":metadata}).model_dump_json(),str(output.id)))
    with pytest.raises(UseAdmissionError,match="qa_projection_changed"):
        execution.require_generation_result(c.service,c.operation,c.result.model_dump(mode="json"))


def test_failed_qa_retained_and_never_reprobed(qa_owned,monkeypatch):
    c = qa_owned
    monkeypatch.setattr(c.probe,"probe",lambda path:_StaticProbe().probe(path))
    with pytest.raises(JobExecutionError,match="未通过"): c.handler(c.qa)
    assert c.service.records.result(c.operation.id,"qa")["qa_state"] == "failed"
    monkeypatch.setattr(c.probe,"probe",lambda *_a:pytest.fail("failed QA cannot retry"))
    with pytest.raises(JobExecutionError,match="未通过"): c.handler(c.qa)
    assert len(c.calls) == 1


def test_late_cancel_and_atomic_sidecar_failure(qa_owned,monkeypatch):
    c = qa_owned
    original = c.service.records.save_result
    def fail(identity,role,payload):
        if role == "qa": raise ValueError("injected QA sidecar failure")
        return original(identity,role,payload)
    monkeypatch.setattr(c.service.records,"save_result",fail)
    with pytest.raises(JobExecutionError,match="QA 未完成"): c.handler(c.qa)
    assert AssetRepository(c.db).get(c.asset.id) == c.asset
    assert c.service.records.result(c.operation.id,"qa") is None
    monkeypatch.setattr(c.service.records,"save_result",original)
    probe = c.probe.probe
    def cancel(path):
        value = probe(path)
        c.service.cancel(c.project,c.operation.id)
        return value
    monkeypatch.setattr(c.probe,"probe",cancel)
    with pytest.raises(JobExecutionError): c.handler(c.qa)
    assert AssetRepository(c.db).get(c.asset.id) == c.asset
    assert c.service.records.result(c.operation.id,"qa") is None
