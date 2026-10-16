"""Bound human judgments; synthetic QA child only, no model execution."""
from datetime import datetime, timezone
import json
import sqlite3
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from app.db import AudioAssetRepository, Database, JobRepository, ProviderCallRepository, VoiceProfileRepository
from app.db.voice_evaluation_review import EvaluationVoiceReviewRepository
from app.domain.models import JobStatus, execution_canonical_sha256
from app.domain.voice_evaluation_review import EvaluationVoiceReviewInput
from app.execution_admission import ProviderUseAdmissionService, UseAdmissionError
from app.main import create_app
from app.media.audio_access import AudioAccess
from app.voice_evaluation_review import EvaluationVoiceReviewService
from app.voice_evaluation_qa import require_completed_qa, retained_claim
from test_voice_evaluation_qa import qa_case, bound_job, child_case, setup, candidate, run
from test_execution_admission import OPERATOR_KEY

DIMENSIONS = ("likeness", "naturalness", "emphasis", "pace", "pauses", "rhythm")

def test_new_qa_master_enters_normal_production_without_evaluation_master_run(qa_case,monkeypatch):
    from app.db import ProjectDraftRepository
    from app.domain.models import ProjectDraft, GraphicCardPlan, SourceKind, VisualStyleTokens
    from app.domain.asr_child import ASRChildResult
    from app.evaluation_composition import EvaluationCompositionService
    from app.production_runs import ProductionRunService
    from test_production_preflight import _scene
    from test_retained_production import use_and_batch
    q=qa_case;c=q.c;pid=c.job.project_id
    q.set_result(ASRChildResult.model_validate_json(json.dumps(dict(text=c.job.payload.text,language='zh',
        segments=[dict(start_ms=0,end_ms=q.audio.duration_ms,text=c.job.payload.text)],words=[]))))
    audio,job,_=run(q)  # Existing fake child + real QA persistence; never model inference.
    old=ProjectDraftRepository(c.db).get(pid)
    scene=_scene(str(pid),text=c.job.payload.text).model_copy(update=dict(duration_target_ms=audio.duration_ms,
        preferred_sources=[SourceKind.TYPOGRAPHY],fallback_sources=[],visual_requirement='explanatory',
        visual_requirement_reason='Synthetic conceptual explanation',
        graphic_plan=GraphicCardPlan(kind='text_card',treatment='key_point',points=['Synthetic concept'])))
    draft=ProjectDraft(project_id=pid,version=old.version+1,script_revision=old.script_revision+1,
        script=c.job.payload.text,scenes=[scene],updated_at=datetime.now(timezone.utc))
    ProjectDraftRepository(c.db).save(draft)
    consumer=EvaluationCompositionService(c.db,c.root);style=VisualStyleTokens()
    with pytest.raises(UseAdmissionError,match='composition_approved_master_required'):
        consumer.retained_selection(pid,style)
    review=EvaluationVoiceReviewService(c.db,c.root)
    review.record(pid,audio.id,typed(body(review,pid,audio.id)))
    from app.db import BudgetPolicyRepository
    budgets=BudgetPolicyRepository(c.db);policy=budgets.get_by_project(pid)
    budgets.save(policy.model_copy(update={'max_calls':3}))  # Fixture grants one subsequent local render.
    context=consumer._context(pid,consumer.retained_selection(pid,style))
    assert context['spec'].master_narration.audio_asset_id==audio.id
    use_and_batch(consumer,pid,context,OPERATOR_KEY)
    calls_before=len(ProviderCallRepository(c.db).list_for_project(pid))
    monkeypatch.setenv('CONTENT_OS_DATA_ROOT',str(c.root))
    with TestClient(create_app(c.db.path)) as client:
        response=client.post(f'/projects/{pid}/production-preflight',json={'purpose':'internal_evaluation','repair_allowance':0})
        assert response.status_code==200,response.text
        plan=response.json();assert plan['status']=='ready_for_authorized_dispatch',plan['stop_reasons']
        response=client.post(f'/projects/{pid}/production-runs',json=dict(purpose='internal_evaluation',
            repair_allowance=0,idempotency_key='new-master',expected_fingerprint=plan['fingerprint']))
        assert response.status_code==201,response.text
        run_id=response.json()['id']
    assert c.db.connection.execute('SELECT count(*) FROM evaluation_run_master_bindings').fetchone()[0]==0
    assert len(ProviderCallRepository(c.db).list_for_project(pid))==calls_before
    consumer.require_current_job(JobRepository(c.db).get(ProductionRunService(c.db,c.root).get(pid,run_id).render_job_id))


def body(service, project, audio):
    state = service.status(project, audio)
    assert state["state"] == "eligible", state
    return dict(policy="voice-evaluation-human-review-v1", purpose="internal_evaluation",
        expected_qa_fingerprint=state["expected_qa_fingerprint"], approved=True,
        evidence_reference="synthetic-user-feedback", findings=["explicit exact six-dimension judgment"],
        **{key:"pass" for key in DIMENSIONS})

def typed(value):
    return EvaluationVoiceReviewInput.model_validate_json(json.dumps(value))

def protected(db):
    return tuple((table, tuple(tuple(r) for r in db.connection.execute(f"SELECT * FROM {table} ORDER BY rowid")))
        for table in ("audio_assets", "voice_evaluation_qa_claims", "voice_calibration_claims", "jobs", "provider_call_records", "provider_machine_capability_profiles"))

def test_http_record_replay_rejection_legacy_guard_and_media_remain_separate(qa_case):
    q=qa_case; audio,job,_=run(q); c=q.c
    before=protected(c.db)
    service=EvaluationVoiceReviewService(c.db,c.root)
    value=body(service,job.project_id,audio.id)
    url=f"/projects/{job.project_id}/voice-assets/{audio.id}/evaluation-human-review"
    with TestClient(create_app(c.db.path)) as client:
        assert client.get(url).status_code==422
        assert client.get(url+"?purpose=commercial_production").status_code==422
        assert client.post(url,json={**value,"approved":"true"}).status_code==422
        for field in DIMENSIONS:
            assert client.post(url,json={**value,field:"unknown"}).status_code==422
        first=client.post(url,json=value);assert first.status_code==200,first.text
        assert client.post(url,json=value).json()==first.json()
        assert client.post(url,json={**value,"findings":["different"]}).status_code==409
        assert client.post(url,json={**value,"approved":False,"rhythm":"needs_revision"}).status_code==409
        state=client.get(url+"?purpose=internal_evaluation").json()
        assert state["state"]=="recorded" and state["record"]["review"]["approved"]
        assert not state["quality_authorized"] and not state["production_authorized"]
        legacy={k:v for k,v in value.items() if k not in ("policy","purpose","expected_qa_fingerprint")}
        old=url.replace("evaluation-human-review","human-review")
        assert client.post(old,json=legacy).status_code==422
        media=f"/audio-assets/{audio.id}/media?project_id={job.project_id}&purpose=internal_evaluation"
        full=client.get(media);assert full.status_code==200
        assert client.get(media,headers={"Range":"bytes=0-15"}).status_code==206
        # Existing FileResponse serves 200 for this conditional request; preserve
        # its current permission check rather than inventing a cache feature.
        assert client.get(media,headers={"If-None-Match":full.headers["etag"]}).status_code==200
        assert client.get(media.replace("internal_evaluation","commercial_production")).status_code==403
    assert protected(c.db)==before
    assert q.calls==[1] and c.calls==[1]
    assert AudioAssetRepository(c.db).get(audio.id)==audio
    assert require_completed_qa(c.db,c.root,audio,project_id=job.project_id,purpose="internal_evaluation")

def test_negative_judgment_is_immutable_and_not_promoted(qa_case):
    q=qa_case;audio,job,_=run(q);s=EvaluationVoiceReviewService(q.c.db,q.c.root)
    value=body(s,job.project_id,audio.id);value.update(approved=False,naturalness="needs_revision")
    record=s.record(job.project_id,audio.id,typed(value))
    assert not record.review.approved
    assert s.record(job.project_id,audio.id,typed(value))==record
    value.update(approved=True,naturalness="pass")
    with pytest.raises(UseAdmissionError,match="immutable_conflict"):s.record(job.project_id,audio.id,typed(value))
    assert audio.metadata["voice_generation"]["human_review_state"]=="pending"

def test_stale_wrong_context_nonterminal_and_inconsistent_decision_are_write_free(qa_case):
    q=qa_case;audio,job,_=run(q);s=EvaluationVoiceReviewService(q.c.db,q.c.root)
    value=body(s,job.project_id,audio.id)
    for changed in ({**value,"expected_qa_fingerprint":"0"*64},{**value,"approved":False}):
        with pytest.raises(ValueError):s.record(job.project_id,audio.id,typed(changed))
    with pytest.raises(ValueError):s.record(uuid4(),audio.id,typed(value))
    JobRepository(q.c.db).update(job.model_copy(update={"status":JobStatus.RUNNING}))
    with pytest.raises(UseAdmissionError,match="terminal_qa_required"):s.record(job.project_id,audio.id,typed(value))
    assert s.records.get_by_audio(audio.id) is None and q.calls==[1]

def test_pending_and_failed_qa_cannot_record(qa_case):
    q=qa_case;s=EvaluationVoiceReviewService(q.c.db,q.c.root)
    assert not s.status(q.c.job.project_id,q.audio.id)["current_eligible"]
    from app.domain.asr_child import ASRChildResult
    q.set_result(ASRChildResult(text="",language="zh",segments=(),words=()))
    audio,job,_=run(q)
    assert not s.status(job.project_id,audio.id)["current_eligible"]
    assert AudioAccess(q.c.db,q.c.root).inspect(audio,project_id=job.project_id,purpose="internal_evaluation")[0]["state"]=="review_available"

def test_record_payload_columns_and_each_binding_tamper_fail_closed(qa_case):
    q=qa_case;audio,job,_=run(q);s=EvaluationVoiceReviewService(q.c.db,q.c.root)
    record=s.record(job.project_id,audio.id,typed(body(s,job.project_id,audio.id)))
    original=q.c.db.connection.execute("SELECT * FROM voice_evaluation_human_reviews").fetchone()
    for key in ("project_id","audio_id","qa_job_id","qa_call_id","audio_sha256","qa_claim_sha256","qa_result_sha256","projection_sha256","submission_sha256"):
        data=record.model_dump(mode="json");data[key]=str(uuid4()) if key.endswith("_id") else "0"*64
        q.c.db.connection.execute("UPDATE voice_evaluation_human_reviews SET payload=?",(json.dumps(data),))
        state=s.status(job.project_id,audio.id)
        assert not state["current_eligible"] and state["record"] is None,(key,state)
        q.c.db.connection.execute("UPDATE voice_evaluation_human_reviews SET payload=?",(original["payload"],))
    q.c.db.connection.execute("UPDATE voice_evaluation_human_reviews SET qa_job_id=?",(str(q.c.job.id),))
    assert not s.status(job.project_id,audio.id)["current_eligible"]
    q.c.db.connection.execute("UPDATE voice_evaluation_human_reviews SET qa_job_id=?",(original["qa_job_id"],))
    # Even a recomputed row digest cannot make an unrelated technical binding current.
    data=record.model_dump(mode="json");data["qa_result_sha256"]="0"*64
    from app.domain.voice_evaluation_review import EvaluationVoiceReviewRecord
    forged=EvaluationVoiceReviewRecord.model_validate_json(json.dumps(data))
    request={**record.review.model_dump(mode="json",exclude={"schema_version","reviewed_at"}),
        "policy":record.policy,"purpose":"internal_evaluation","expected_qa_fingerprint":execution_canonical_sha256(forged.binding())}
    data["submission_sha256"]=typed(request).submission_digest(job.project_id,audio.id)
    q.c.db.connection.execute("UPDATE voice_evaluation_human_reviews SET payload=?,payload_sha256=?",(json.dumps(data),execution_canonical_sha256(data)))
    assert "binding_changed" in s.status(job.project_id,audio.id)["reasons"][0]
    assert AudioAccess(q.c.db,q.c.root).inspect(audio,project_id=job.project_id,purpose="internal_evaluation")[0]["state"]=="review_available"

def test_revocation_blocks_replay_and_preserves_historical_record(qa_case):
    q=qa_case;audio,job,_=run(q);s=EvaluationVoiceReviewService(q.c.db,q.c.root)
    value=typed(body(s,job.project_id,audio.id));record=s.record(job.project_id,audio.id,value)
    ProviderUseAdmissionService(q.c.db,q.c.root).revoke(job.project_id,q.receipt.id,reason="synthetic",operator_key=OPERATOR_KEY)
    state=s.status(job.project_id,audio.id)
    assert not state["current_eligible"] and state["record"]["id"]==str(record.id)
    with pytest.raises(ValueError):s.record(job.project_id,audio.id,value)
    assert q.calls==[1]

def test_authority_drift_between_validation_and_commit_is_write_free(qa_case,monkeypatch):
    q=qa_case;audio,job,_=run(q);s=EvaluationVoiceReviewService(q.c.db,q.c.root)
    value=typed(body(s,job.project_id,audio.id));before=s._binding
    def drifting(*args):
        result=before(*args)
        # Use the existing valid revocation action. A raw unconfirmed ConsentRecord
        # is malformed and is rejected by model validation before fact comparison.
        ProviderUseAdmissionService(q.c.db,q.c.root).revoke(job.project_id,q.receipt.id,reason="concurrent synthetic revocation",operator_key=OPERATOR_KEY)
        return result
    monkeypatch.setattr(s,"_binding",drifting)
    with pytest.raises(UseAdmissionError,match="authority_changed"):s.record(job.project_id,audio.id,value)
    assert s.records.get_by_audio(audio.id) is None

def test_sqlite_rollback_and_second_connection_preserve_exact_record(qa_case):
    q=qa_case;audio,job,_=run(q);s=EvaluationVoiceReviewService(q.c.db,q.c.root)
    value=typed(body(s,job.project_id,audio.id));binding=s._binding(job.project_id,audio,retained_claim(q.c.db,audio.id))
    from app.domain.voice_evaluation_review import EvaluationVoiceReviewRecord
    pending=EvaluationVoiceReviewRecord(**binding,submission_sha256=value.submission_digest(job.project_id,audio.id),review=value.review(datetime.now(timezone.utc)))
    with pytest.raises(RuntimeError):
        with q.c.db.transaction(immediate=True):
            s.records.create(pending);raise RuntimeError("simulated crash")
    assert s.records.get_by_audio(audio.id) is None
    record=s.record(job.project_id,audio.id,value)
    with Database(q.c.db.path) as other:
        other.data_root=q.c.root
        assert EvaluationVoiceReviewService(other,q.c.root).record(job.project_id,audio.id,value)==record
        with pytest.raises(sqlite3.IntegrityError):EvaluationVoiceReviewRepository(other).create(pending)

@pytest.mark.parametrize("same", [True, False], ids=["equal", "different"])
def test_two_connections_racing_submissions_return_one_record(qa_case, same):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    q=qa_case;audio,job,_=run(q);s=EvaluationVoiceReviewService(q.c.db,q.c.root)
    value=typed(body(s,job.project_id,audio.id));barrier=Barrier(2)
    changed = value if same else typed({**value.model_dump(mode="json"), "approved":False, "naturalness":"needs_revision"})
    def submit(payload):
        with Database(q.c.db.path) as db:
            db.data_root=q.c.root
            service=EvaluationVoiceReviewService(db,q.c.root);original=service._binding
            def synchronized(*args):
                result=original(*args);barrier.wait(timeout=60);return result
            service._binding=synchronized
            try:
                return service.record(job.project_id,audio.id,payload)
            except UseAdmissionError as exc:
                return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(submit,value);second=pool.submit(submit,changed)
        results=[first.result(timeout=90),second.result(timeout=90)]
        if same:
            assert results[0]==results[1] and not isinstance(results[0],str)
        else:
            assert sum(value=="voice_human_review_immutable_conflict" for value in results)==1
            assert sum(not isinstance(value,str) for value in results)==1
    assert q.c.db.connection.execute("SELECT COUNT(*) FROM voice_evaluation_human_reviews").fetchone()[0]==1

def test_legacy_guard_still_blocks_after_qa_markers_are_stripped(qa_case):
    q=qa_case;audio,job,_=run(q)
    from app.voice_evaluation_review import require_legacy_human_review
    q.c.db.connection.execute("DELETE FROM voice_evaluation_qa_claims")
    q.c.db.connection.execute("DELETE FROM provider_call_records WHERE idempotency_key=?",(f"voice-evaluation-qa:{job.id}",))
    metadata=dict(audio.metadata);metadata.pop("voice_evaluation_qa")
    stripped=audio.model_copy(update={"metadata":metadata})
    with pytest.raises(UseAdmissionError,match="action_required"):require_legacy_human_review(q.c.db,q.c.root,stripped)
