"""Synthetic first-validation admission: no model/tool/network execution."""
from datetime import datetime, timezone
import hashlib
import json
from uuid import UUID
import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient
from app.db import JobRepository, ProviderMachineCapabilityProfileRepository
from app.domain.models import ProviderUseAdmission, ProviderUseLicenseReviewV3
from app.execution_admission import ProviderUseAdmissionService, configuration_digest
from app.voice_calibration import request_digest
from app.voice_draft import VoiceDraftService
from app.main import create_app
from test_maintained_voice_job import bound_job, setup, child_case, candidate
import test_voice_draft as draft


def calibrate(c):
    repo=ProviderMachineCapabilityProfileRepository(c.db)
    profile=repo.get(c.job.payload.execution_capability_profile_id).model_copy(update={
        "readiness":"configured","quality_status":"unknown","verified_parameters":{}})
    repo.save(profile)
    old=ProviderUseAdmission.model_validate_json(c.db.connection.execute(
        "SELECT payload FROM provider_use_admissions WHERE id=?",(str(c.receipt),)).fetchone()[0])
    document=old.review.model_dump(mode="json")
    document.update(policy_version=3,admission_policy="voice-calibration-v1",max_provider_calls=1,
        request_sha256=request_digest(c.job.payload),configuration_sha256=configuration_digest(profile),
        permitted_operations=["generation","review"])
    report=ProviderUseLicenseReviewV3.model_validate_json(json.dumps(document))
    path=c.root/"calibration-review.json";path.write_text(report.model_dump_json(),encoding="utf-8")
    receipt=ProviderUseAdmissionService(c.db,c.root).adopt(c.job.project_id,idempotency_key="calibration",
        review_reference=path.name,review_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        operator_key=c.options["operator_key"])
    c.receipt=receipt.id
    draft.budget(c)
    return profile,receipt


def body(c):
    return {**draft.body(c),"draft_policy":"voice-calibration-v1"}


def test_claim_is_atomic_and_once_only_even_different_idempotency_key(bound_job):
    c=bound_job;profile,_=calibrate(c)
    with TestClient(create_app(c.db.path)) as client:
        url=f"/projects/{c.job.project_id}/voice-jobs"
        first=client.post(url,json=body(c));assert first.status_code==201,first.text
        assert client.post(url,json=body(c)).json()["id"]==first.json()["id"]
        count=len(JobRepository(c.db).list())
        for change in ({"idempotency_key":"another-key"},{"text":"different text"},{"draft_policy":"voice-draft-v1"}):
            response=client.post(url,json={**body(c),**change});assert response.status_code==409,response.text
            assert len(JobRepository(c.db).list())==count
        assert c.db.connection.execute("SELECT COUNT(*) FROM voice_calibration_claims").fetchone()[0]==1
        status=client.get(f"/projects/{c.job.project_id}/voice-draft-jobs/{first.json()['id']}").json()
        assert status["policy"]=="voice-calibration-v1" and status["state"]=="pending"
        assert status["quality_authorized"] is False and c.calls==[]
        assert ProviderMachineCapabilityProfileRepository(c.db).get(profile.id).quality_status=="unknown"


@pytest.mark.parametrize("field,value",[("max_provider_calls",True),("max_provider_calls",2),("policy_version",True),
    ("permitted_operations",["generation","review","derivation"]),("admission_policy","unknown")])
def test_new_report_is_strict(bound_job,field,value):
    _,receipt=calibrate(bound_job)
    document=receipt.review.model_dump(mode="json");document[field]=value
    with pytest.raises(ValidationError):ProviderUseLicenseReviewV3.model_validate_json(json.dumps(document))


@pytest.mark.parametrize("mutation",["claim","marker","receipt","source","scope"])
def test_current_status_denies_changed_authority(bound_job,mutation):
    c=bound_job;calibrate(c)
    with TestClient(create_app(c.db.path)) as client:
        response=client.post(f"/projects/{c.job.project_id}/voice-jobs",json=body(c));assert response.status_code==201,response.text
        identity=response.json()["id"]
        if mutation=="scope":c.db.connection.execute("DELETE FROM execution_use_scopes WHERE subject_kind='job' AND subject_id=?",(identity,))
        elif mutation=="claim":c.db.connection.execute("DELETE FROM voice_calibration_claims")
        elif mutation=="marker":c.db.connection.execute("UPDATE voice_draft_jobs SET policy='voice-draft-v1'")
        elif mutation=="source":__import__('pathlib').Path(c.asset.source_file).write_bytes(b"changed")
        else:
            row=c.db.connection.execute("SELECT payload FROM provider_use_admissions WHERE id=?",(str(c.receipt),)).fetchone()
            receipt=json.loads(row[0]);receipt.update(revoked_at=datetime.now(timezone.utc).isoformat(),revoked_by="fixture",revocation_reason="test")
            c.db.connection.execute("UPDATE provider_use_admissions SET payload=? WHERE id=?",(json.dumps(receipt),str(c.receipt)))
        status=client.get(f"/projects/{c.job.project_id}/voice-draft-jobs/{identity}").json()
        assert status["state"]=="blocked" and status["reasons"] and c.calls==[]


def test_unknown_quality_normal_owned_execution_and_audition(bound_job,monkeypatch):
    c=bound_job;profile,_=calibrate(c)
    original=draft.body
    monkeypatch.setattr(draft,"body",lambda value:{**original(value),"draft_policy":"voice-calibration-v1"})
    draft.test_normal_factory_runner_output_pending_audition_and_failure_recovery(c,monkeypatch)
    assert c.calls==[1]
    assert ProviderMachineCapabilityProfileRepository(c.db).get(profile.id).quality_status=="unknown"
    job=JobRepository(c.db).list()[-1]
    status=VoiceDraftService(c.db,c.root).status(c.job.project_id,job)
    assert status["outputs"][0]["access"]["state"]=="blocked"


def test_claim_transaction_rolls_back_and_cannot_dispatch_without_scope(bound_job,monkeypatch):
    c=bound_job;calibrate(c)
    from app.execution_admission import UseAdmissionError
    from app.execution_scope import ExecutionScopeService
    count=len(JobRepository(c.db).list())
    monkeypatch.setattr(ExecutionScopeService,"pin",lambda *a,**kw: (_ for _ in ()).throw(UseAdmissionError("synthetic-pin-failed")))
    with TestClient(create_app(c.db.path)) as client:
        assert client.post(f"/projects/{c.job.project_id}/voice-jobs",json=body(c)).status_code==409
    assert len(JobRepository(c.db).list())==count
    assert c.db.connection.execute("SELECT COUNT(*) FROM voice_calibration_claims").fetchone()[0]==0
    assert c.db.connection.execute("SELECT COUNT(*) FROM voice_draft_jobs").fetchone()[0]==0
    assert c.calls==[]


def test_concurrent_requests_cannot_claim_twice(bound_job):
    c=bound_job;calibrate(c)
    from concurrent.futures import ThreadPoolExecutor
    from app.db import Database
    from app.main import VoiceGenerationJobRequest
    from app.execution_admission import UseAdmissionError
    def attempt(index):
        with Database(c.db.path) as db:
            db.data_root=c.root
            request=VoiceGenerationJobRequest.model_validate({**body(c),"idempotency_key":f"concurrent-{index}"})
            try:return str(VoiceDraftService(db,c.root).enqueue(c.job.project_id,request).id)
            except UseAdmissionError as error:return str(error)
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(attempt,[1,2]))
    assert results.count("voice_calibration_already_claimed")==1
    assert c.db.connection.execute("SELECT COUNT(*) FROM voice_calibration_claims").fetchone()[0]==1
    assert c.calls==[]


def test_calibration_replay_and_failed_accounting_do_not_invoke_twice(bound_job):
    c=bound_job;calibrate(c)
    from app.main import VoiceGenerationJobRequest
    from app.domain.models import JobStatus
    from app.db import ProviderCallRepository,AudioAssetRepository
    from app.provider_execution import ProviderExecutionRecordedFailure
    from app.execution_scope import ExecutionScopeService
    from app.media.audio_access import AudioAccess
    from test_maintained_voice_job import execute
    request=VoiceGenerationJobRequest.model_validate(body(c))
    job=VoiceDraftService(c.db,c.root).enqueue(c.job.project_id,request).model_copy(update={"status":JobStatus.RUNNING,"attempt":1})
    JobRepository(c.db).update(job);c.job=job
    c.options["inputs"]=c.options["inputs"].model_copy(update={"output_path":str(c.outputs/"voice"/f"{job.id}-attempt-1.wav")})
    audio=execute(c)
    assert execute(c,replay=True).id==audio.id and c.calls==[1]
    access,_=AudioAccess(c.db,c.root).inspect(audio,project_id=job.project_id,purpose="internal_evaluation")
    assert access["state"]=="blocked"
    JobRepository(c.db).update(job.model_copy(update={"status":JobStatus.COMPLETED}))
    access,_=AudioAccess(c.db,c.root).inspect(audio,project_id=job.project_id,purpose="internal_evaluation")
    assert access["state"]=="review_available",access
    for operation in ("derivation","download"):
        with pytest.raises(ValueError,match="operation_not_permitted"):
            ExecutionScopeService(c.db,c.root).require_content(audio.content_hash,project_id=job.project_id,purpose="internal_evaluation",operation=operation)
    repo=ProviderCallRepository(c.db);record=repo.list_for_project(job.project_id)[0]
    repo.update(record.model_copy(update={"status":"failed","result_payload":None}))
    JobRepository(c.db).update(job)
    with pytest.raises(ProviderExecutionRecordedFailure):execute(c,replay=True)
    assert c.calls==[1]
