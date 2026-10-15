"""Normal authority/ownership with synthetic installations, never model calls.

The real input, evidence, scope, adoption and retained resolvers are exercised.
Only inherited Voice/ASR children and host observation use existing fakes.
"""
import hashlib
import json
from pathlib import Path
import shutil
import asyncio
import httpx
from datetime import timedelta
from types import SimpleNamespace
import pytest
from app.db import ProviderCallRepository, JobRepository
from app.domain.models import ProviderUseLicenseReviewV2, JobStatus
from app.domain.evaluation_master import MasterAdoption
from app.domain.evaluation_talking import TalkingStart
from app.execution_admission import configuration_digest, UseAdmissionError
from app.execution_scope import read_job_scope, require_evaluation_lane_closed
from test_execution_admission import OPERATOR_KEY
from test_evaluation_talking_normal_inputs import normal_inputs, talking_case, qa_case, bound_job, child_case, setup, candidate


def http(c,method,path,**kwargs):
    from fastapi import FastAPI
    from app.evaluation_talking_api import router
    app=FastAPI();app.include_router(router)
    app.state.database=c.db;app.state.data_root=c.root;app.state.cost_estimator=c.service.cost_estimator
    async def send():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://synthetic") as client:
            return await client.request(method,path,**kwargs)
    return asyncio.run(send())


def save(root,name,report):
    path=root/name
    path.write_text(json.dumps(report),encoding="utf-8")
    return MasterAdoption(idempotency_key=name,review_reference=name,review_sha256=hashlib.sha256(path.read_bytes()).hexdigest())


@pytest.fixture
def normal_flow(normal_inputs):
    c=normal_inputs
    execution,terms,sha=admit_normal_execution(c)
    selected=c.selection.model_copy(update={"execution_admission_id":execution.id})
    state=c.service.preflight(c.project,selected)
    assert state["reasons"] == ["evaluation_talking_use_admission_required"],state
    context=c.service._context(c.project,selected)
    evidence=dict(source="synthetic",version="test-v1",terms_reference=terms.name,terms_sha256=sha,
        applicability="Synthetic exact input/output test only",permits_intended_activity=True)
    report=dict(policy="evaluation-talking-run-v1",purpose="internal_evaluation",operation="transform_master_to_talking",local_only=True,
        project_id=str(c.project),selection=selected.model_dump(mode="json"),request_sha256=context["request_sha256"],proof=context["proof"],
        intended_activity="Synthetic normal Talking ownership",findings=["No actual runtime or human judgment"],
        license_evidence=[dict(evidence,admission_id=str(b.admission_id)) for b in context["output_scope"].bindings],
        dependency_findings=[dict(evidence,dependency=role) for role in
            ("narration_input_use","source_person_video","talking_output_processing_review")])
    adoption=save(c.root,"normal-talking-use.json",report)
    receipt=c.service.adopt(c.project,adoption,OPERATOR_KEY)
    selected=selected.model_copy(update={"use_admission_id":receipt.id})
    c.selection=selected
    return c


def admit_normal_execution(c):
    terms=c.root/"normal-talking-terms.txt"
    terms.write_bytes(b"Synthetic test permissions only, not a real license")
    sha=hashlib.sha256(terms.read_bytes()).hexdigest()
    report=dict(policy_version=2,evidence_class="operator_verified",project_id=str(c.project),purpose="internal_evaluation",
        capability_profile_id=str(c.cap.id),configuration_sha256=configuration_digest(c.cap),
        identity=c.spec.identity.model_dump(mode="json"),execution_specification=c.spec.model_dump(mode="json"),
        execution_sha256=c.spec.execution_sha256,intended_activity="Synthetic one-child Talking test",
        license_source="synthetic test only",license_version="test-v1",license_terms_reference=terms.name,
        license_terms_sha256=sha,permits_intended_activity=True,permitted_operations=["generation","derivation","review"],
        findings=["Synthetic report; issues no real permission"])
    ProviderUseLicenseReviewV2.model_validate_json(json.dumps(report))
    adoption=save(c.root,"normal-talking-execution.json",report)
    execution=c.service.admissions.adopt(c.project,**adoption.model_dump(),operator_key=OPERATOR_KEY)
    return execution,terms,sha


def test_normal_preflight_create_reload_retained_and_cancel_without_calls(normal_flow):
    c=normal_flow
    before_calls=ProviderCallRepository(c.db).list_all()
    parent=tuple(tuple(r) for r in c.db.connection.execute("SELECT * FROM production_runs WHERE id=?",(str(c.parent.id),)))
    before=c.db.connection.total_changes
    base=f"/projects/{c.project}/evaluation-talking"
    response=http(c,"POST",base+"/preflight",json=c.selection.model_dump(mode="json"))
    assert response.status_code == 200,response.text
    state=response.json()
    assert state["eligible"],state
    assert c.db.connection.total_changes == before
    request=TalkingStart(**c.selection.model_dump(),idempotency_key="normal-once",expected_fingerprint=state["fingerprint"])
    response=http(c,"POST",base+"/operations",json=request.model_dump(mode="json"))
    assert response.status_code == 201,response.text
    value=c.service.records.operation(response.json()["id"])
    assert c.service.create(c.project,request.model_copy(update={"idempotency_key":"alias"})) == value
    assert c.service.require_current_operation(value)["request_sha256"] == value.request_sha256
    assert c.service.require_retained_operation(value)["output_scope"].fingerprint == value.output_scope_fingerprint
    job=JobRepository(c.db).get(value.generation_job_id)
    assert len(read_job_scope(c.db,job).bindings) == 1
    with pytest.raises(UseAdmissionError): require_evaluation_lane_closed(c.db,job)
    assert ProviderCallRepository(c.db).list_all() == before_calls
    assert tuple(tuple(r) for r in c.db.connection.execute("SELECT * FROM production_runs WHERE id=?",(str(c.parent.id),))) == parent
    c.service.cancel(c.project,value.id)
    with pytest.raises(UseAdmissionError,match="operation_cancelled"): c.service.require_retained_operation(value)


def test_normal_owned_handler_through_preview_review_admission_with_real_authority(normal_flow,monkeypatch):
    from app.providers import maintained_talking_execution as runtime
    from app import evaluation_talking_execution as execution
    from app.jobs.evaluation_talking_handler import MaintainedEvaluationTalkingHandler
    from app.jobs.evaluation_talking_qa_handler import EvaluationTalkingQaHandler
    from app.jobs.evaluation_talking_preview_handler import EvaluationTalkingPreviewHandler
    from app.talking import planned_preview
    from app.talking.evaluation_admission import EvaluationTalkingAdmissionService
    from app.db import TalkingSliceSeriesRepository
    from app.domain.models import TalkingReviewDimensions, TalkingReviewFinding, TalkingReviewConcernAnswer
    from app.jobs.runner import JobRunner
    from app.jobs.store import JobStore
    from app.db import AssetRepository
    from app.media.ffprobe import ProbeMetadata
    c=normal_flow
    state=c.service.preflight(c.project,c.selection)
    assert state["eligible"],state
    op=c.service.create(c.project,TalkingStart(**c.selection.model_dump(),idempotency_key="normal-generation",
        expected_fingerprint=state["fingerprint"]))
    context=c.service.require_current_operation(op)
    duration=context["slice_plan"].duration_ms
    options=dict(c.talking_options)
    for label in ("vendor","hf","torch","tools"):
        target=c.root/label
        if Path(options[label]) != target:
            shutil.copytree(options[label],target)
        options[label]=target
    checkpoint=c.root/"checkpoint.pt"
    checkpoint.write_bytes(Path(options["checkpoint"]).read_bytes());options["checkpoint"]=checkpoint
    options.pop("data_root");options.pop("staging_parent")
    class Probe:
        def probe(self,path):
            assert not c.db.connection.in_transaction
            return ProbeMetadata(duration_ms=duration,width=1080,height=1920,fps=c.asset.fps,has_audio=True,
                metadata={"streams":[{"codec_type":"video"},{"codec_type":"audio"}]})
    monkeypatch.setattr(execution,"FFProbeAdapter",lambda *_a:Probe())
    monkeypatch.setattr(runtime,"_system_root",lambda:c.root)
    calls=[]
    def launch(argv,**kw):
        assert not c.db.connection.in_transaction
        calls.append(argv)
        (Path(kw["cwd"])/"output.mp4").write_bytes(b"synthetic normal owned Talking output")
        return SimpleNamespace(wait=lambda **_kw:0,poll=lambda:0)
    monkeypatch.setattr(runtime.subprocess,"Popen",launch)
    job=JobRepository(c.db).get(op.generation_job_id)
    job=JobStore(c.db).claim("normal-initial",timedelta(minutes=5),max_attempts=1,job_id=job.id)
    handler=MaintainedEvaluationTalkingHandler(c.db,c.root,options=options,policy=c.selection.policy)
    from app.jobs.lease_fence import owned_job_lease
    with owned_job_lease(JobStore(c.db),job,"normal-initial"):
        output=handler(job)
    assert len(calls) == 1
    if c.spec.schema_version == 10:
        from app.providers import maintained_talking_dual as dual
        monkeypatch.setattr(dual,"application_bundle",lambda:b"current edited application")
        monkeypatch.setattr(dual,"engine_bundle",lambda:b"current edited owned bootstrap")
        # Fresh admission still requires current source; completed output uses
        # the two authenticated retained inventories instead of another call.
        execution_receipt=c.service.admissions.get(c.project,c.selection.execution_admission_id)
        with pytest.raises(UseAdmissionError,match="child_evidence_invalid"):
            c.service.admissions._report(execution_receipt.review_reference,execution_receipt.review_sha256)
    # Crash recovery authenticates saved output without preparation/inference.
    monkeypatch.setattr(runtime.subprocess,"Popen",lambda *_a,**_kw:pytest.fail("recovery must not launch"))
    assert handler(job) == output
    # Simulate an interrupted completion after the durable output exists.
    c.db.connection.execute("UPDATE jobs SET lease_owner='synthetic-crashed',lease_expires_at='2000-01-01T00:00:00.000000Z' WHERE id=?",(str(job.id),))
    def runner(active):
        return JobRunner(JobStore(c.db),{active_type:active},worker_id="synthetic-worker",
            lease_duration=timedelta(minutes=5),max_attempts=3)
    active_type=job.type
    recovered=runner(handler).run_once()
    assert recovered.id == job.id and recovered.status == JobStatus.COMPLETED
    qa=c.service.create_qa(op)
    active_type=qa.type
    completed=runner(EvaluationTalkingQaHandler(c.db,c.root,probe=Probe(),policy=c.selection.policy)).run_once()
    assert completed.id == qa.id and completed.status == JobStatus.COMPLETED
    verified=AssetRepository(c.db).get(output.id)
    assert verified.metadata["talking_generation"]["qa_state"] == "verified"
    assert c.service.require_retained_operation(op)["output_scope"].fingerprint == op.output_scope_fingerprint
    assert len(c.service.scopes.get("asset",output.id).bindings) == 3
    assert len(calls) == 1
    preview_job=c.service.create_preview(op)
    mux_calls=[]
    def mux(argv,**kw):
        assert not c.db.connection.in_transaction
        mux_calls.append(argv)
        Path(argv[-1]).write_bytes(b"synthetic normal exact preview")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(planned_preview.subprocess,"run",mux)
    preview_handler=EvaluationTalkingPreviewHandler(c.db,c.root,ffmpeg_command="synthetic-never-launched",probe=Probe(),policy=c.selection.policy)
    active_type=preview_job.type
    completed=runner(preview_handler).run_once()
    assert completed.id == preview_job.id and completed.status == JobStatus.COMPLETED
    preview=AssetRepository(c.db).get(c.service.records.result(op.id,"preview")["asset_id"])
    assert len(mux_calls) == 1
    base=f"/projects/{c.project}/evaluation-talking/operations/{op.id}"
    before=c.db.connection.total_changes
    full=http(c,"GET",base+"/preview/media?purpose=internal_evaluation")
    assert full.status_code == 200,full.text
    part=http(c,"GET",base+"/preview/media?purpose=internal_evaluation",headers={"Range":"bytes=0-9"})
    assert part.status_code == 206 and part.content == full.content[:10]
    for suffix in ("","?purpose=commercial_production","?purpose=unknown"):
        assert http(c,"GET",base+"/preview/media"+suffix).status_code == 403
    assert http(c,"GET",base).json()["state"] == "awaiting_review"
    assert c.db.connection.total_changes == before
    series=TalkingSliceSeriesRepository(c.db).get(op.series_id)
    admission=EvaluationTalkingAdmissionService(c.db,c.root)
    assert admission.review_status(series)["continuity_review_state"] == "not_submitted"
    admission.record_review(series,approved=True,review_policy_version=2,preview_asset_id=preview.id,
        preview_sha256=preview.content_hash,evidence_reference="synthetic-only:six-pass",findings=[],
        dimensions=TalkingReviewDimensions(**dict.fromkeys(
            ("visible_sync","identity","artifacts","source_performance","continuity","publishability"),"pass")))
    admitted=admission.admit(series)
    assert admission.admit(series) == admitted
    assert c.service.scopes.get("talking_run",admitted.id).fingerprint == op.output_scope_fingerprint
    concern=admission.register_concern(series,preview_asset_id=preview.id,preview_sha256=preview.content_hash,
        finding=TalkingReviewFinding(dimension="artifacts",reason="synthetic frame inspection"),
        evidence_reference="synthetic-only:concern",idempotency_key="normal-concern")
    with pytest.raises(ValueError): admission.admit(series)
    admission.answer_concerns(series,preview_asset_id=preview.id,preview_sha256=preview.content_hash,
        answers=[TalkingReviewConcernAnswer(concern_id=concern.id,approved=True,reason="synthetic cleared",
            evidence_reference="synthetic-only:answer")])
    assert admission.admit(series) == admitted
    assert len(calls) == 1 and len(mux_calls) == 1


@pytest.mark.parametrize("variant",["callback","changed_factory","foreign_db"])
def test_normal_owned_job_cannot_use_generic_or_foreign_handlers(normal_flow,variant):
    from app.jobs.evaluation_talking_handler import MaintainedEvaluationTalkingHandler
    from app.jobs.runner import JobRunner
    from app.jobs.store import JobStore
    c=normal_flow
    state=c.service.preflight(c.project,c.selection)
    op=c.service.create(c.project,TalkingStart(**c.selection.model_dump(),idempotency_key="normal-denied-handler",
        expected_fingerprint=state["fingerprint"]))
    class ChangedFactory(MaintainedEvaluationTalkingHandler):
        def __call__(self,job): pytest.fail("changed factory must not execute")
    handler=(lambda _job:pytest.fail("generic callback must not execute")) if variant == "callback" else (
        ChangedFactory(c.db,c.root,options={}) if variant == "changed_factory" else
        MaintainedEvaluationTalkingHandler(object(),c.root,options={}))
    job=JobRepository(c.db).get(op.generation_job_id)
    result=JobRunner(JobStore(c.db),{job.type:handler},worker_id="synthetic-denied",
        lease_duration=timedelta(minutes=5),max_attempts=3).run_once()
    assert result.id == job.id and result.status == JobStatus.FAILED
    assert c.service.records.result(op.id,"generation") is None
    assert ProviderCallRepository(c.db).get_by_idempotency_key(f"evaluation-talking:{c.project}:{op.inference_sha256}:generation") is None


def test_normal_second_connection_source_revocation_rolls_back_enqueue(normal_flow,monkeypatch):
    from app.db import Database
    from app.talking.source_admission import TalkingSourceAdmissionRepository
    c=normal_flow
    state=c.service.preflight(c.project,c.selection)
    before_calls=ProviderCallRepository(c.db).list_all()
    estimate=c.service._budget_estimate
    def revoke(context):
        result=estimate(context)
        with Database(c.db.path) as other:
            TalkingSourceAdmissionRepository(other,c.root).revoke(c.receipt.id,
                actor_id="fixture-operator",reason="synthetic concurrent source withdrawal")
        return result
    monkeypatch.setattr(c.service,"_budget_estimate",revoke)
    with pytest.raises(UseAdmissionError,match="authority_changed"):
        c.service.create(c.project,TalkingStart(**c.selection.model_dump(),idempotency_key="normal-revocation-race",
            expected_fingerprint=state["fingerprint"]))
    assert c.db.connection.execute("SELECT COUNT(*) FROM evaluation_talking_operations").fetchone()[0] == 0
    assert ProviderCallRepository(c.db).list_all() == before_calls
