"""Normal evaluation QA with complete synthetic installation and fake child only."""
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError, TypeAdapter
from app.db import (AudioAssetRepository, JobRepository, ProviderCallRepository,
    ProviderMachineCapabilityProfileRepository, VoiceProfileRepository)
from app.domain.models import (JobStatus, ProviderUseLicenseReviewV4, ExecutionSpecificationRecord,
    ProviderUseLicenseReviewV3, ExecutionSpecificationV6)
from app.domain.asr_child import ASRChildRequest, ASRChildResult
from app.domain.asr_recipe import ASR_MODEL_FILES
from app.execution_admission import ProviderUseAdmissionService, UseAdmissionError, configuration_digest
from app.execution_scope import require_evaluation_lane_closed
from app.main import VoiceQaJobRequest, create_app
from app.media.audio_access import AudioAccess, file_digest
from app.providers.maintained_asr_execution import (observe_asr_inventory, prepare_maintained_asr_child)
from app.runtime_inventory import RuntimeInventoryStore
from app.voice_evaluation_qa import (VoiceEvaluationQAService, _original_proof, retained_claim,
    execute_evaluation_qa, require_completed_qa, REVIEW_POLICY, qa_call_digest)
from app.jobs.voice_evaluation_qa_handler import MaintainedVoiceEvaluationQAHandler
from app.jobs.runner import JobRunner
from app.jobs.store import JobStore
from test_execution_admission import setup, OPERATOR_KEY
from test_maintained_runtime import candidate, reference
from test_maintained_voice_execution import child_case
from test_maintained_voice_job import bound_job
from test_completed_voice_review import complete
from test_provider_execution import Cost

@pytest.fixture(params=["isolated-zip-asr-v1", "isolated-zip-asr-v2"])
def qa_case(bound_job, monkeypatch, request):
    c = bound_job
    audio = complete(c)
    model = c.root/"asr-model"
    model.mkdir()
    for name in ASR_MODEL_FILES:
        (model/name).write_bytes(b"synthetic model; not executed" if name == "model.bin" else b"{}")
    options = {key:value for key,value in c.options.items() if key in
        ("environment", "base", "data_root", "staging_parent", "operator_key", "installation_id", "runtime_label", "machine_id", "max_bytes")}
    options["invocation_policy"] = request.param
    for directory in (options["base"]/"Lib", options["base"]/"DLLs"):
        directory.mkdir(exist_ok=True)
    inventory = observe_asr_inventory(options["environment"], options["base"], max_bytes=options["max_bytes"], invocation_policy=request.param)
    old = json.loads((c.root/c.options["origin_reference"].reference).read_text())
    old.pop("generation_scope"); old.pop("accepts_generation_runtime_trust")
    old.update(origin_version=3, transcription_scope="voice_evaluation_qa_only", accepts_transcription_runtime_trust=True,
        runtime_inventory_sha256=inventory.descriptor.sha256)
    origin = c.root/"asr-origin.json"; origin.write_text(json.dumps(old))
    options.update(model_root=model, origin_reference=reference(origin,c.root))
    request = ASRChildRequest(model_root=str(model), audio_path=str(Path(audio.source_file)),
        audio_sha256=audio.content_hash, language="zh")
    from app.providers.maintained_asr_execution import observe_asr
    spec = observe_asr(dict(options, timeout_seconds=600))
    RuntimeInventoryStore(c.root).install_v2(inventory)
    profile = ProviderMachineCapabilityProfileRepository(c.db).get(c.job.payload.execution_capability_profile_id)
    cap = profile.model_copy(update=dict(id=uuid4(), capability="asr", provider=spec.provider, model=spec.model,
        runtime=spec.runtime, machine_id=spec.machine_id, readiness="configured", quality_status="unknown"))
    ProviderMachineCapabilityProfileRepository(c.db).save(cap)
    original_receipt = ProviderUseAdmissionService(c.db,c.root).get(c.job.project_id,c.receipt)
    fields = original_receipt.review.model_dump(mode="json")
    fields.pop("request_sha256"); fields.update(policy_version=4, admission_policy="voice-evaluation-qa-v1",
        capability_profile_id=str(cap.id), configuration_sha256=configuration_digest(cap),
        identity=spec.identity.model_dump(mode="json"), execution_specification=spec.model_dump(mode="json"),
        execution_sha256=spec.execution_sha256, input_binding=_original_proof(c.db,c.root,audio,project_id=c.job.project_id)[0].model_dump(mode="json"))
    report = ProviderUseLicenseReviewV4.model_validate_json(json.dumps(fields))
    reportfile = c.root/"asr-review.json"; reportfile.write_text(report.model_dump_json(),encoding="utf-8")
    receipt = ProviderUseAdmissionService(c.db,c.root).adopt(c.job.project_id, idempotency_key="asr-adopt",
        review_reference=reportfile.name, review_sha256=hashlib.sha256(reportfile.read_bytes()).hexdigest(), operator_key=OPERATOR_KEY)
    body = dict(idempotency_key="one-qa", narration_audio_id=audio.id, target_text=c.job.payload.text,
        qa_policy="voice-evaluation-qa-v1", purpose="internal_evaluation", use_admission_id=receipt.id)
    calls = []
    result = ASRChildResult.model_validate_json(json.dumps(dict(text=c.job.payload.text, language="zh",
        segments=[dict(start_ms=0,end_ms=1,text=c.job.payload.text)], words=[])))
    class Child:
        def __init__(self, args, **kw): self.cwd=Path(kw["cwd"])
        def wait(self, timeout):
            assert not c.db.connection.in_transaction
            rows = ProviderCallRepository(c.db).list_for_project(c.job.project_id)
            assert len(rows)==2 and sum(row.status=="running" for row in rows)==1
            child_request = ASRChildRequest.model_validate_json((self.cwd/"request.json").read_bytes())
            assert "target_text" not in child_request.model_dump() and "prompt" not in child_request.model_dump()
            calls.append(1)
            (self.cwd/"result.json").write_text(result.model_dump_json(),encoding="utf-8")
            return 0
        def poll(self): return 0
    monkeypatch.setattr(subprocess,"Popen",Child)
    return SimpleNamespace(c=c,audio=audio,options=options,spec=spec,receipt=receipt,body=body,calls=calls,
        set_result=lambda value:setattr(Child,"wait",lambda self,timeout:write_result(c,calls,self.cwd,value)))

def write_result(c,calls,cwd,value):
    calls.append(1); (cwd/"result.json").write_text(value.model_dump_json(),encoding="utf-8"); return 0

def enqueue(q):
    return VoiceEvaluationQAService(q.c.db,q.c.root).enqueue(q.c.job.project_id,VoiceQaJobRequest(**q.body))

def run(q):
    job=enqueue(q); job=job.model_copy(update=dict(status=JobStatus.RUNNING,attempt=1)); JobRepository(q.c.db).update(job)
    handler=MaintainedVoiceEvaluationQAHandler(q.c.db,q.c.root,options={k:v for k,v in q.options.items() if k not in ("data_root","staging_parent")})
    audio=handler(job)
    job=job.model_copy(update=dict(status=JobStatus.COMPLETED)); JobRepository(q.c.db).update(job)
    return audio,job,handler

def compose_qa_master(q, tmp_path):
    """Build a composed-source asset through the existing composer contract; no FFmpeg or model child."""
    import wave
    from app.domain.models import AudioAsset
    from app.master_narration import MasterNarrationComposer
    from app.voice_takes import ProvisionalMasterNarration

    source = AudioAssetRepository(q.c.db).get(q.audio.id)
    assert source is not None and source.metadata["voice_generation"]["qa_state"] == "verified"

    class Composer:
        def compose(self, takes, output_path):
            assert [take.audio.id for take in takes] == [source.id]
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(output_path), "wb") as output:
                output.setparams((1, 2, 24_000, 0, "NONE", "not compressed"))
                output.writeframes(b"\x01\x00" * 24)
            return ProvisionalMasterNarration(output_path, source.duration_ms,
                tuple(source.transcript_segments), (str(source.id),))

    class Importer:
        def import_path(self, path, authorization_reference, *, source_kind):
            destination = tmp_path / "composed-master.wav"
            destination.write_bytes(path.read_bytes())
            content = destination.read_bytes()
            audio = AudioAsset(source_file=str(destination), content_hash=hashlib.sha256(content).hexdigest(),
                duration_ms=source.duration_ms, sample_rate=24_000, channels=1,
                authorization_reference=authorization_reference, imported_at=datetime.now(timezone.utc))
            return AudioAssetRepository(q.c.db).create(audio)

    master = MasterNarrationComposer(audios=AudioAssetRepository(q.c.db), jobs=JobRepository(q.c.db),
        importer=Importer(), composer=Composer(), output_root=tmp_path / "master-work").compose(
            q.c.job.project_id, [source.id])
    return source, master

def test_normal_submission_owned_child_atomic_result_and_scoped_review(qa_case, monkeypatch):
    q=qa_case; c=q.c
    def forbidden(*a, **kw): pytest.fail("full environment/model observation in QA hot path")
    monkeypatch.setattr("app.providers.maintained_asr_execution.observe_asr", forbidden)
    monkeypatch.setattr("app.providers.maintained_asr_execution.observe_asr_inventory", forbidden)
    assert VoiceEvaluationQAService(c.db,c.root).status(c.job.project_id,q.audio.id)["state"]=="ready"
    audio,job,handler=run(q)
    assert audio.id==q.audio.id and audio.content_hash==q.audio.content_hash and q.calls==[1]
    assert audio.metadata["voice_generation"]["qa_state"]=="verified"
    assert require_completed_qa(c.db,c.root,audio,project_id=c.job.project_id,purpose="internal_evaluation")==REVIEW_POLICY
    access,source=AudioAccess(c.db,c.root).inspect(audio,project_id=c.job.project_id,purpose="internal_evaluation")
    assert access["state"]=="review_available" and access["review_policy"]==REVIEW_POLICY and source
    assert VoiceEvaluationQAService(c.db,c.root).status(c.job.project_id,audio.id)["technical_ready"] is True
    cap=ProviderMachineCapabilityProfileRepository(c.db).get(q.receipt.review.capability_profile_id)
    assert cap.quality_status=="unknown" and cap.readiness=="configured"
    assert c.calls==[1] and len(ProviderCallRepository(c.db).list_for_project(job.project_id))==2

def test_no_second_claim_or_reset_and_same_key_recovers(qa_case):
    q=qa_case; original=enqueue(q)
    assert enqueue(q).id==original.id
    q.body["idempotency_key"]="different"
    with pytest.raises(UseAdmissionError,match="already_claimed"): enqueue(q)
    assert q.calls==[] and q.c.calls==[1]
    with pytest.raises(UseAdmissionError,match="not_integrated"): require_evaluation_lane_closed(q.c.db,original)

@pytest.mark.parametrize("outcome",["verified","failed"])
def test_saved_outcome_survives_application_drift_reconciles_without_inference_and_denies_tampering(qa_case,monkeypatch,outcome):
    q=qa_case;c=q.c
    if outcome=="failed": q.set_result(ASRChildResult(text="",language="zh",segments=(),words=()))
    audio,job,handler=run(q)
    assert audio.metadata["voice_generation"]["qa_state"]==outcome
    from app.providers import maintained_asr_execution, maintained_voice_execution
    before=maintained_asr_execution.asr_bundle_bytes
    monkeypatch.setattr(maintained_asr_execution,"asr_bundle_bytes",lambda:before()+b"app drift")
    vb=maintained_voice_execution.bundle_bytes
    monkeypatch.setattr(maintained_voice_execution,"bundle_bytes",lambda:vb()+b"voice drift")
    job=job.model_copy(update=dict(status=JobStatus.RUNNING,attempt=2)); JobRepository(c.db).update(job)
    assert handler(job).id==audio.id and q.calls==[1]
    state=VoiceEvaluationQAService(c.db,c.root).status(c.job.project_id,audio.id)
    assert state["state"]=="reconciliation_required" and state["technical_ready"] is False
    assert AudioAccess(c.db,c.root).inspect(audio,project_id=c.job.project_id,purpose="internal_evaluation")[0]["state"]=="review_available"
    tampered=audio.model_copy(update={"metadata":q.audio.metadata,"transcript_segments":q.audio.transcript_segments,"transcript_source":q.audio.transcript_source})
    c.db.connection.execute("UPDATE audio_assets SET payload=? WHERE id=?",(tampered.model_dump_json(),str(audio.id)))
    assert AudioAccess(c.db,c.root).inspect(tampered,project_id=c.job.project_id,purpose="internal_evaluation")[0]["state"]=="blocked"
    assert q.calls==[1]

@pytest.mark.parametrize("change",["target","project","tokenizer","audio","consent","revoked"])
def test_changed_authority_denies_before_asr(qa_case,change):
    q=qa_case;c=q.c
    if change=="target":q.body["target_text"]+="changed"
    elif change=="project":q.body["use_admission_id"]=uuid4()
    elif change=="tokenizer":(q.options["model_root"]/"tokenizer.json").unlink()
    elif change=="audio":Path(q.audio.source_file).write_bytes(b"changed")
    elif change=="consent":
        profile=VoiceProfileRepository(c.db).get(c.job.payload.voice_profile_id)
        profile=profile.model_copy(update={"consent":profile.consent.model_copy(update={"confirmed":False})})
        c.db.connection.execute("UPDATE voice_profiles SET payload=? WHERE id=?",(profile.model_dump_json(),str(profile.id)))
    else:ProviderUseAdmissionService(c.db,c.root).revoke(c.job.project_id,q.receipt.id,reason="test",operator_key=OPERATOR_KEY)
    if change=="tokenizer":
        job=enqueue(q).model_copy(update=dict(status=JobStatus.RUNNING,attempt=1)); JobRepository(c.db).update(job)
        from app.jobs.runner import JobExecutionError
        with pytest.raises(JobExecutionError):MaintainedVoiceEvaluationQAHandler(c.db,c.root,options={k:v for k,v in q.options.items() if k not in ("data_root","staging_parent")})(job)
    else:
        with pytest.raises(UseAdmissionError):enqueue(q)
    assert q.calls==[]

def test_new_schema_and_report_do_not_satisfy_old_voice_contracts(qa_case):
    q=qa_case
    assert TypeAdapter(ExecutionSpecificationRecord).validate_json(q.spec.model_dump_json())==q.spec
    with pytest.raises(ValidationError):ExecutionSpecificationV6.model_validate_json(q.spec.model_dump_json())
    with pytest.raises(ValidationError):ProviderUseLicenseReviewV3.model_validate_json(q.receipt.review.model_dump_json())
    raw=q.receipt.review.model_dump(mode="json");raw["max_provider_calls"]=True
    with pytest.raises(ValidationError):ProviderUseLicenseReviewV4.model_validate_json(json.dumps(raw))


def test_retained_qa_digest_uses_the_original_persisted_claim_shape():
    from app.budget import provider_call_input_digest

    stored_claim = {"policy":"voice-evaluation-qa-v1", "input_binding":{"audio_id":"legacy-audio"}}
    expanded_claim = {"policy":"voice-evaluation-qa-v1", "input_binding":{
        "audio_id":"legacy-audio", "composition_source_take_audio_ids":[]}}
    execution = SimpleNamespace(model_dump=lambda **_kwargs: {"runtime":"local"})
    receipt = SimpleNamespace(review=SimpleNamespace(
        identity=SimpleNamespace(provider="faster-whisper", model="medium"),
        execution_specification=execution, configuration_sha256="c"*64), review_sha256="d"*64)
    claim = SimpleNamespace(project_id=uuid4(), execution_sha256="e"*64,
        model_dump=lambda **_kwargs: expanded_claim)

    expected = provider_call_input_digest(dict(project_id=str(claim.project_id), operation="asr",
        provider="faster-whisper", model="medium", input_source="voice-evaluation-qa-job",
        input=dict(claim=stored_claim, execution_specification={"runtime":"local"},
            execution_sha256=claim.execution_sha256, configuration_sha256="c"*64, review_sha256="d"*64)))
    assert qa_call_digest(claim, receipt, claim_payload=stored_claim) == expected
    assert qa_call_digest(claim, receipt) != expected


def test_composed_master_enters_existing_qa_claim_with_bound_source_and_authority(qa_case, tmp_path):
    q=qa_case;source,_,_=run(q)
    source,master=compose_qa_master(q,tmp_path)
    binding,source_path=_original_proof(q.c.db,q.c.root,master,project_id=q.c.job.project_id)
    composition=master.metadata["voice_generation"]["composition"]
    assert binding.producer_job_id is None
    assert binding.composition_source_take_audio_ids==(source.id,)
    assert binding.audio_sha256==master.content_hash
    assert binding.original_scope_fingerprint and binding.original_admissions
    assert binding.target_text_sha256==hashlib.sha256(master.metadata["voice_generation"]["target_text"].encode()).hexdigest()
    assert source_path==Path(master.source_file) and file_digest(source_path)==master.content_hash
    assert composition["source_take_content_hashes"]==[source.content_hash]
    assert composition["copy_adjudicated_source_take_audio_ids"]==[]
    assert composition.get("measured_seam_compaction") is None

    # Existing QA admission and Job path accepts the exact composition binding; it does not launch ASR here.
    fields=q.receipt.review.model_dump(mode="json")
    fields["input_binding"]=binding.model_dump(mode="json")
    report=ProviderUseLicenseReviewV4.model_validate_json(json.dumps(fields))
    report_path=q.c.root/"composed-asr-review.json"
    report_path.write_text(report.model_dump_json(),encoding="utf-8")
    receipt=ProviderUseAdmissionService(q.c.db,q.c.root).adopt(q.c.job.project_id,
        idempotency_key="composed-master-asr",review_reference=report_path.name,
        review_sha256=hashlib.sha256(report_path.read_bytes()).hexdigest(),operator_key=OPERATOR_KEY)
    from app.db import BudgetPolicyRepository
    from app.domain.models import BudgetPolicy
    BudgetPolicyRepository(q.c.db).save(BudgetPolicy(project_id=q.c.job.project_id,max_calls=4,
        allow_unknown_cost=True,updated_at=datetime.now(timezone.utc)))
    q.receipt=receipt
    q.body.update(idempotency_key="composed-master-qa",narration_audio_id=master.id,
        target_text=master.metadata["voice_generation"]["target_text"],use_admission_id=str(receipt.id))
    service=VoiceEvaluationQAService(q.c.db,q.c.root)
    ready=service.status(q.c.job.project_id,master.id)
    assert ready["state"]=="ready" and ready["choices"][0]["admission_id"]==str(receipt.id)
    job=service.enqueue(q.c.job.project_id,VoiceQaJobRequest(**q.body))
    claim=retained_claim(q.c.db,master.id)
    assert job.status==JobStatus.PENDING and claim.input_binding==binding
    assert q.calls==[1]  # only the source take's mocked QA ran; Master QA was queued, not dispatched.


def test_composed_master_rejects_source_or_qa_proof_tampering(qa_case,tmp_path):
    q=qa_case;run(q)
    source,master=compose_qa_master(q,tmp_path)
    generation=master.metadata["voice_generation"]
    composition=generation["composition"]

    def changed(*,generation_changes=None,composition_changes=None,authorization=None):
        saved=dict(master.metadata["voice_generation"])
        saved.update(generation_changes or {})
        saved_composition=dict(composition)
        saved_composition.update(composition_changes or {})
        saved["composition"]=saved_composition
        metadata={**master.metadata,"voice_generation":saved}
        return master.model_copy(update={"metadata":metadata,
            **({"authorization_reference":authorization} if authorization is not None else {})})

    invalid=[
        changed(composition_changes={"source_take_content_hashes":["0"*64]}),
        changed(generation_changes={"target_text":generation["target_text"]+" changed"}),
        changed(generation_changes={"voice_profile_id":str(uuid4())}),
        changed(authorization="different-authorization"),
        changed(composition_changes={"copy_adjudicated_source_take_audio_ids":[str(source.id)]}),
        changed(composition_changes={"measured_seam_compaction":{"method":"unknown","cuts":[]}}),
    ]
    for candidate in invalid:
        with pytest.raises(UseAdmissionError):
            _original_proof(q.c.db,q.c.root,candidate,project_id=q.c.job.project_id)
    with pytest.raises(UseAdmissionError):
        _original_proof(q.c.db,q.c.root,changed(),project_id=uuid4())

    # The take's persisted ASR result is part of its completed QA claim.
    source_qa_claim=retained_claim(q.c.db,source.id)
    call=ProviderCallRepository(q.c.db).get_by_idempotency_key(f"voice-evaluation-qa:{source_qa_claim.job_id}")
    payload=json.loads(call.model_dump_json())
    payload["result_payload"]["result"]["transcription"]["text"]="changed QA transcript"
    q.c.db.connection.execute("UPDATE provider_call_records SET payload=? WHERE id=?",
        (json.dumps(payload),str(call.id)))
    with pytest.raises(UseAdmissionError):
        _original_proof(q.c.db,q.c.root,master,project_id=q.c.job.project_id)


def test_normal_http_entry_and_scoped_full_range_current_revocation(qa_case):
    q=qa_case;c=q.c
    with TestClient(create_app(c.db.path)) as client:
        endpoint=f"/projects/{c.job.project_id}/voice-qa-jobs"
        body=VoiceQaJobRequest(**q.body).model_dump(mode="json")
        first=client.post(endpoint,json=body)
        assert first.status_code==201,first.text
        assert client.post(endpoint,json=body).json()["id"]==first.json()["id"]
        job=JobRepository(c.db).get(first.json()["id"]).model_copy(update=dict(status=JobStatus.RUNNING,attempt=1))
        JobRepository(c.db).update(job)
        handler=MaintainedVoiceEvaluationQAHandler(c.db,c.root,options={k:v for k,v in q.options.items() if k not in ("data_root","staging_parent")})
        audio=handler(job)
        JobRepository(c.db).update(job.model_copy(update=dict(status=JobStatus.COMPLETED)))
        context=f"?project_id={c.job.project_id}&purpose=internal_evaluation"
        url=f"/audio-assets/{audio.id}/media"+context
        full=client.get(url); assert full.status_code==200
        assert client.get(url,headers={"Range":"bytes=0-15"}).content==full.content[:16]
        assert client.get(f"/audio-assets/{audio.id}/media").status_code==403
        status=client.get(f"/projects/{c.job.project_id}/audio-assets/{audio.id}/evaluation-qa").json()
        assert status["technical_ready"] is True
        other=client.get(f"/projects/{uuid4()}/audio-assets/{audio.id}/evaluation-qa")
        assert other.status_code==404
        ProviderUseAdmissionService(c.db,c.root).revoke(c.job.project_id,q.receipt.id,reason="test",operator_key=OPERATOR_KEY)
        for headers in ({},{"Range":"bytes=0-15"},{"If-None-Match":"*"}):
            assert client.get(url,headers=headers).status_code==403
    assert q.calls==[1]


def test_atomic_projection_rolls_back_with_failed_ledger_write_and_never_runs_again(qa_case,monkeypatch):
    q=qa_case;c=q.c
    original=ProviderCallRepository.update
    def fail(repo,call):
        if call.operation=="asr" and call.status=="completed":raise RuntimeError("synthetic commit failure")
        return original(repo,call)
    monkeypatch.setattr(ProviderCallRepository,"update",fail)
    from app.jobs.runner import JobExecutionError
    with pytest.raises(JobExecutionError):run(q)
    assert AudioAssetRepository(c.db).get(q.audio.id)==q.audio
    call=ProviderCallRepository(c.db).get_by_idempotency_key(f"voice-evaluation-qa:{enqueue(q).id}")
    assert call.status=="running" and call.result_payload is None
    job=JobRepository(c.db).get(enqueue(q).id)
    handler=MaintainedVoiceEvaluationQAHandler(c.db,c.root,options={k:v for k,v in q.options.items() if k not in ("data_root","staging_parent")})
    with pytest.raises(JobExecutionError,match="已有 QA"):handler(job)
    assert q.calls==[1]

@pytest.mark.parametrize("ledger_state",["failed","cancelled","running","reserved"])
def test_existing_terminal_or_unknown_call_never_reexecutes(qa_case,ledger_state):
    q=qa_case;c=q.c;audio,job,handler=run(q)
    call=ProviderCallRepository(c.db).get_by_idempotency_key(f"voice-evaluation-qa:{job.id}")
    changed=call.model_copy(update=dict(status=ledger_state,result_payload=None))
    c.db.connection.execute("UPDATE provider_call_records SET payload=? WHERE id=?",(changed.model_dump_json(),str(call.id)))
    c.db.connection.execute("UPDATE audio_assets SET payload=? WHERE id=?",(q.audio.model_dump_json(),str(audio.id)))
    job=job.model_copy(update=dict(status=JobStatus.RUNNING,attempt=2));JobRepository(c.db).update(job)
    from app.jobs.runner import JobExecutionError
    with pytest.raises(JobExecutionError):handler(job)
    assert q.calls==[1]

@pytest.mark.parametrize("tamper",["claim_deleted","result_changed","qa_stripped","original_metadata","timing"])
def test_completed_proof_tampering_never_falls_back(qa_case,tamper):
    q=qa_case;c=q.c;audio,job,_=run(q)
    if tamper=="claim_deleted":c.db.connection.execute("DELETE FROM voice_evaluation_qa_claims WHERE job_id=?",(str(job.id),))
    elif tamper in ("result_changed","timing"):
        call=ProviderCallRepository(c.db).get_by_idempotency_key(f"voice-evaluation-qa:{job.id}")
        value=json.loads(call.model_dump_json())
        if tamper=="result_changed":value["result_payload"]["result"]["audio_id"]=str(uuid4())
        else:value["result_payload"]["result"]["transcription"]["segments"][0]["end_ms"]=9999
        c.db.connection.execute("UPDATE provider_call_records SET payload=? WHERE id=?",(json.dumps(value),str(call.id)))
    else:
        metadata=dict(audio.metadata)
        if tamper=="qa_stripped":metadata.pop("voice_evaluation_qa")
        else:metadata["voice_generation"]={**metadata["voice_generation"],"target_text":"changed"}
        audio=audio.model_copy(update=dict(metadata=metadata))
        c.db.connection.execute("UPDATE audio_assets SET payload=? WHERE id=?",(audio.model_dump_json(),str(audio.id)))
    assert AudioAccess(c.db,c.root).inspect(audio,project_id=c.job.project_id,purpose="internal_evaluation")[0]["state"]=="blocked"
    assert q.calls==[1]


def test_owned_runner_closes_generic_callback_and_completes_normal_qa(qa_case):
    q=qa_case;c=q.c;job=enqueue(q)
    from app.domain.models import JobType
    called=[]
    runner=JobRunner(JobStore(c.db),{JobType.VERIFY_VOICE:lambda _:called.append(1)},worker_id="generic",
        lease_duration=timedelta(minutes=10),max_attempts=2)
    denied=runner.run_once()
    assert denied.id==job.id and denied.status==JobStatus.FAILED and called==[] and q.calls==[]
    # Only the same retained unspent claim is restored for the fixture-owned runner.
    c.db.connection.execute("UPDATE jobs SET status='pending',lease_expires_at=NULL,lease_owner=NULL WHERE id=?",(str(job.id),))
    saved=JobRepository(c.db).get(job.id).model_copy(update=dict(status=JobStatus.PENDING,error_code=None,error_message=None))
    JobRepository(c.db).update(saved)
    handler=MaintainedVoiceEvaluationQAHandler(c.db,c.root,options={k:v for k,v in q.options.items() if k not in ("data_root","staging_parent")})
    runner=JobRunner(JobStore(c.db),{JobType.VERIFY_VOICE:handler},worker_id="owned",lease_duration=timedelta(minutes=10),max_attempts=3)
    completed=runner.run_once()
    assert completed.status==JobStatus.COMPLETED and q.calls==[1]
    assert VoiceEvaluationQAService(c.db,c.root).status(c.job.project_id,q.audio.id)["technical_ready"] is True


def test_terminal_preflight_failure_is_explicit_without_spending_asr(qa_case):
    q=qa_case;c=q.c;job=enqueue(q)
    JobRepository(c.db).update(job.model_copy(update=dict(status=JobStatus.FAILED,error_code="maintained_asr_local_assets_missing",error_message="blocked")))
    status=VoiceEvaluationQAService(c.db,c.root).status(job.project_id,q.audio.id)
    assert status["state"]=="blocked" and status["technical_ready"] is False
    assert status["recovery_action"]=="correct_preflight_and_reconcile_same_unspent_job"
    assert status["reasons"]==["maintained_asr_local_assets_missing"] and q.calls==[]
    assert len(ProviderCallRepository(c.db).list_all())==1


def test_cancel_during_reservation_never_launches_asr(qa_case):
    q=qa_case;c=q.c;job=enqueue(q).model_copy(update=dict(status=JobStatus.RUNNING,attempt=1));JobRepository(c.db).update(job)
    cost=Cost()
    def cancel():JobRepository(c.db).update(job.model_copy(update=dict(status=JobStatus.CANCELLED)))
    cost.change=cancel
    request=ASRChildRequest(model_root=q.spec.model,audio_path=q.audio.source_file,audio_sha256=q.audio.content_hash,language="zh")
    with prepare_maintained_asr_child(**q.options,request=request,deployment_specification=q.spec) as prepared:
        with pytest.raises(UseAdmissionError,match="not_running"):
            execute_evaluation_qa(db=c.db,root=c.root,job=job,prepared=prepared,cost_estimator=cost)
    assert q.calls==[] and len(ProviderCallRepository(c.db).list_all())==1


def test_initial_commercial_denial_retains_evaluation_manifest_for_explicit_audition(qa_case):
    q=qa_case;c=q.c;audio,job,_=run(q)
    denied,_=AudioAccess(c.db,c.root).inspect(audio,project_id=job.project_id,purpose="commercial_production")
    assert denied["state"]=="blocked" and denied["reasons"]==["evaluation_scope_not_commercial"]
    assert any(scope["purpose"]=="internal_evaluation" for scope in denied["manifest"]["scopes"])
    allowed,source=AudioAccess(c.db,c.root).inspect(audio,project_id=job.project_id,purpose="internal_evaluation")
    assert allowed["state"]=="review_available" and source is not None
    assert q.calls==[1]
