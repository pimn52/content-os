"""Synthetic owned execution plus completed-review gates; no real model calls."""
import json
from pathlib import Path
from uuid import uuid4
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient
from app.db import JobRepository, ProviderCallRepository, AudioAssetRepository, VoiceProfileRepository, ProviderMachineCapabilityProfileRepository
from app.domain.models import JobStatus
from app.main import VoiceGenerationJobRequest, create_app
from app.voice_draft import VoiceDraftService
from app.media.audio_access import AudioAccess
from app.execution_admission import ProviderUseAdmissionService, UseAdmissionError
from app.providers import maintained_voice_execution
from app.voice_calibration import REVIEW_POLICY
from test_maintained_voice_job import bound_job, execute, setup, child_case, candidate
from test_voice_calibration import calibrate, body


def complete(c):
    calibrate(c)
    job=VoiceDraftService(c.db,c.root).enqueue(c.job.project_id,VoiceGenerationJobRequest.model_validate(body(c)))
    job=job.model_copy(update={"status":JobStatus.RUNNING,"attempt":1})
    JobRepository(c.db).update(job); c.job=job
    c.options['inputs']=c.options['inputs'].model_copy(update={'output_path':str(c.outputs/'voice'/f'{job.id}-attempt-1.wav')})
    audio=execute(c)
    c.job=job.model_copy(update={'status':JobStatus.COMPLETED}); JobRepository(c.db).update(c.job)
    return audio


def test_completed_exact_review_survives_app_drift_but_generation_does_not(bound_job,monkeypatch):
    c=bound_job;audio=complete(c)
    original=maintained_voice_execution.bundle_bytes
    monkeypatch.setattr(maintained_voice_execution,'bundle_bytes',lambda:original()+b'changed application')
    with pytest.raises(UseAdmissionError,match='evidence_not_current'):
        VoiceDraftService(c.db,c.root).require_job(c.job)
    admission=ProviderUseAdmissionService(c.db,c.root).get(c.job.project_id,c.receipt)
    assert not ProviderUseAdmissionService(c.db,c.root).current_evidence(admission)
    with pytest.raises(UseAdmissionError):
        ProviderUseAdmissionService(c.db,c.root).adopt(c.job.project_id,idempotency_key='no-upgrade',
            review_reference=admission.review_reference,review_sha256=admission.review_sha256,operator_key=c.options['operator_key'])
    with TestClient(create_app(c.db.path)) as client:
        context=f'?project_id={c.job.project_id}&purpose=internal_evaluation'
        access=client.get(f'/audio-assets/{audio.id}/access'+context).json()
        assert access['state']=='review_available' and access['review_policy']==REVIEW_POLICY
        media=client.get(f'/audio-assets/{audio.id}/media'+context)
        assert media.status_code==200 and media.headers['cache-control']=='no-store'
        partial=client.get(f'/audio-assets/{audio.id}/media'+context,headers={'Range':'bytes=0-127'})
        assert partial.status_code==206 and partial.content==media.content[:128]
        assert client.get(f'/audio-assets/{audio.id}/media').status_code==403
        status=client.get(f'/projects/{c.job.project_id}/voice-draft-jobs/{c.job.id}').json()
        assert status['state']=='completed' and status['reasons']==[]
        assert status['generation_eligibility']['state']=='blocked'
        assert status['quality_authorized'] is False and status['automatic_retry_authorized'] is False
        qa=status['outputs'][0]['qa_consumption']
        assert qa['state']=='blocked' and qa['purpose']=='commercial_production'
        assert qa['reasons'] and qa['execution_configuration']=='not_checked'
        assert qa['dispatch_authorized'] is False
        for project,purpose in ((uuid4(),'internal_evaluation'),(c.job.project_id,'commercial_production')):
            denied,_=AudioAccess(c.db,c.root).inspect(audio,project_id=project,purpose=purpose)
            assert denied['state']=='blocked'
        for operation in ('derivation','download','generation'):
            denied,_=AudioAccess(c.db,c.root).inspect(audio,project_id=c.job.project_id,purpose='internal_evaluation',operation=operation)
            assert denied['state']=='blocked'
        # Every fresh full/range/conditional read rechecks current consent.
        profile=VoiceProfileRepository(c.db).get(c.job.payload.voice_profile_id)
        withdrawn=profile.model_copy(update={'consent':profile.consent.model_copy(update={'confirmed':False})})
        c.db.connection.execute('UPDATE voice_profiles SET payload=? WHERE id=?',(withdrawn.model_dump_json(),str(profile.id)))
        for headers in ({},{'Range':'bytes=0-127'},{'If-None-Match':'*'}):
            assert client.get(f'/audio-assets/{audio.id}/media'+context,headers=headers).status_code==403
    assert c.calls==[1]


def test_review_needs_no_new_budget_or_dispatch_secret(bound_job, monkeypatch):
    c=bound_job;audio=complete(c)
    from app.db import BudgetPolicyRepository
    from app.domain.models import BudgetPolicy
    BudgetPolicyRepository(c.db).save(BudgetPolicy(project_id=c.job.project_id,max_calls=0,
        allow_unknown_cost=True,updated_at=datetime.now(timezone.utc)))
    monkeypatch.delenv('CONTENT_OS_EXECUTION_ADMISSION_KEY_HASHES')
    access,source=AudioAccess(c.db,c.root).inspect(audio,project_id=c.job.project_id,purpose='internal_evaluation')
    assert access['state']=='review_available' and source is not None and c.calls==[1]


def test_qa_state_transition_is_not_implicitly_admitted(bound_job):
    c=bound_job;audio=complete(c)
    transitioned=audio.model_copy(update={'metadata':{**audio.metadata,
        'voice_generation':{**audio.metadata['voice_generation'],'qa_state':'verified'}}})
    access,source=AudioAccess(c.db,c.root).inspect(transitioned,
        project_id=c.job.project_id,purpose='internal_evaluation')
    assert access['state']=='blocked' and source is None
    assert access['reasons']==['voice_calibration_output_unbound']
    assert c.calls==[1]  # synthetic generation only; no ASR invoked


@pytest.mark.parametrize('mutation',[
    'job_pending','job_failed','call_running','call_failed','call_missing','claim','producer','observation',
    'execution','attempt','result','terms','report','origin','inventory','consent','source','configuration','revoked','reimport'])
def test_completed_review_denies_changed_proof_or_authority(bound_job,mutation):
    c=bound_job;audio=complete(c)
    calls=ProviderCallRepository(c.db);call=calls.get_by_idempotency_key(f'maintained-voice:{c.job.id}')
    receipt=ProviderUseAdmissionService(c.db,c.root).get(c.job.project_id,c.receipt)
    if mutation.startswith('job_'):
        c.job=c.job.model_copy(update={'status':JobStatus(mutation[4:])});JobRepository(c.db).update(c.job)
    elif mutation.startswith('call_'):
        if mutation=='call_missing':c.db.connection.execute('DELETE FROM provider_call_records WHERE id=?',(str(call.id),))
        else:calls.update(call.model_copy(update={'status':mutation[5:]}))
    elif mutation=='claim':c.db.connection.execute('DELETE FROM voice_calibration_claims')
    elif mutation in ('producer','observation','execution','attempt'):
        generation=dict(audio.metadata['voice_generation'])
        key={'producer':'job_id','observation':'configuration_observation','execution':'execution_sha256','attempt':'attempt'}[mutation]
        if mutation=='producer':generation.pop(key)
        else:generation[key]=0 if mutation=='attempt' else 'changed'
        audio=audio.model_copy(update={'metadata':{**audio.metadata,'voice_generation':generation}})
    elif mutation=='result':calls.update(call.model_copy(update={'result_payload':{'result':{}}}))
    elif mutation in ('terms','report','origin'):
        path={'terms':receipt.review.license_terms_reference,'report':receipt.review_reference,'origin':receipt.review.execution_specification.runtime_origin.reference}[mutation]
        (c.root/path).write_bytes(b'changed evidence')
    elif mutation=='inventory':
        # Tamper retained detached CAS bytes, never observe a new installation.
        descriptor=receipt.review.execution_specification.runtime_inventory
        from app.runtime_inventory import RuntimeInventoryStore
        store=RuntimeInventoryStore(c.root)
        candidates=list(c.root.rglob(descriptor.sha256+'*'))
        assert candidates
        candidates[0].write_bytes(b'changed inventory')
    elif mutation=='consent':
        repo=VoiceProfileRepository(c.db);profile=repo.get(c.job.payload.voice_profile_id)
        value=profile.model_copy(update={'consent':profile.consent.model_copy(update={'confirmed':False})})
        c.db.connection.execute('UPDATE voice_profiles SET payload=? WHERE id=?',(value.model_dump_json(),str(value.id)))
    elif mutation=='source':Path(c.asset.source_file).write_bytes(b'changed reference')
    elif mutation=='configuration':
        repo=ProviderMachineCapabilityProfileRepository(c.db);profile=repo.get(c.job.payload.execution_capability_profile_id)
        value=profile.model_copy(update={'runtime':'changed-runtime'})
        c.db.connection.execute('UPDATE provider_machine_capability_profiles SET payload=? WHERE id=?',(value.model_dump_json(),str(value.id)))
    elif mutation=='revoked':
        value=receipt.model_copy(update={'revoked_at':datetime.now(timezone.utc),'revoked_by':'fixture','revocation_reason':'test'})
        c.db.connection.execute('UPDATE provider_use_admissions SET payload=? WHERE id=?',(value.model_dump_json(),str(receipt.id)))
    elif mutation=='reimport':audio=audio.model_copy(update={'id':uuid4()})
    access,source=AudioAccess(c.db,c.root).inspect(audio,project_id=c.job.project_id,purpose='internal_evaluation')
    assert access['state']=='blocked' and source is None and access['reasons'], access
    assert c.calls==[1]
