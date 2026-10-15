"""Saved-plan UI request validation with existing synthetic Voice authority."""
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient
from app.db import ProjectDraftRepository, JobRepository
from app.domain.models import ProjectDraft
from app.main import create_app, VoiceGenerationJobRequest
from app.voice_draft import VoiceDraftService
from app.execution_admission import UseAdmissionError
from test_voice_draft import bound_job, setup, child_case, candidate, body, budget
from test_production_preflight import _scene


def save_plan(c, *, text=None, scenes=True):
    old=ProjectDraftRepository(c.db).get(c.job.project_id)
    text=c.job.payload.text if text is None else text
    draft=ProjectDraft(project_id=c.job.project_id,version=old.version+1,script_revision=old.script_revision+1,
        script=text,scenes=[_scene(str(c.job.project_id),text=text)] if scenes else [],updated_at=datetime.now(timezone.utc))
    ProjectDraftRepository(c.db).save(draft)
    return draft


def request(c,draft):
    return {**body(c),'saved_plan_source':{'version':draft.version,'script_revision':draft.script_revision}}


def test_normal_saved_full_copy_replays_and_recovers_without_new_call(bound_job):
    c=bound_job;budget(c);draft=save_plan(c)
    with TestClient(create_app(c.db.path)) as client:
        url=f'/projects/{c.job.project_id}/voice-jobs';payload=request(c,draft)
        result=client.post(url,json=payload);assert result.status_code==201,result.text
        job_id=result.json()['id'];assert client.post(url,json=payload).json()['id']==job_id
        state=client.get(f'/projects/{c.job.project_id}/voice-draft-jobs/{job_id}').json()
        assert state['target_text']==draft.script and state['production_authorized'] is False
        assert JobRepository(c.db).get(job_id).payload.text==draft.script
        assert client.get(f'/projects/{c.job.project_id}/voice-draft-jobs').json()[0]['job_id']==job_id
    assert c.calls==[]


@pytest.mark.parametrize('change',['text','version','revision','scenes','purpose','boolean','extra','transform'])
def test_saved_input_mismatch_and_invalid_source_create_nothing(bound_job,change):
    c=bound_job;budget(c);draft=save_plan(c,scenes=change!='scenes');payload=request(c,draft)
    if change=='text':payload['text']='Different full copy'
    if change=='version':payload['saved_plan_source']['version']+=1
    if change=='revision':payload['saved_plan_source']['script_revision']+=1
    if change=='transform':payload['delivery_text']=payload['text']
    if change=='purpose':payload['purpose']='commercial_production'
    if change=='boolean':payload['saved_plan_source']['version']=True
    if change=='extra':payload['saved_plan_source']['authority']='approved'
    before=c.db.connection.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
    with TestClient(create_app(c.db.path)) as client:
        response=client.post(f'/projects/{c.job.project_id}/voice-jobs',json=payload)
        assert response.status_code in (409,422),response.text
    assert c.db.connection.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==before and c.calls==[]


def test_saved_plan_changes_during_preparation_are_fenced_before_job_insert(bound_job,monkeypatch):
    c=bound_job;budget(c);draft=save_plan(c)
    service=VoiceDraftService(c.db,c.root);original=service._inputs
    def changed(payload,scope):
        value=original(payload,scope);save_plan(c,text='Concurrent new complete copy');return value
    monkeypatch.setattr(service,'_inputs',changed)
    before=c.db.connection.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
    with pytest.raises(UseAdmissionError,match='voice_saved_plan_changed'):
        service.enqueue(c.job.project_id,VoiceGenerationJobRequest(**request(c,draft)))
    assert c.db.connection.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==before and c.calls==[]


def test_scene_copy_order_and_whitespace_are_exact_not_lossy(bound_job):
    from app.voice_plan import require_saved_voice_plan,SavedVoicePlanSource
    c=bound_job;old=ProjectDraftRepository(c.db).get(c.job.project_id)
    second=_scene(str(c.job.project_id),text='Second').model_copy(update={'order':2})
    first=_scene(str(c.job.project_id),text='  First  ').model_copy(update={'order':1})
    draft=old.model_copy(update={'version':old.version+1,'script_revision':old.script_revision+1,'script':None,'scenes':[second,first]})
    ProjectDraftRepository(c.db).save(draft)
    source=SavedVoicePlanSource(version=draft.version,script_revision=draft.script_revision)
    assert require_saved_voice_plan(c.db,c.job.project_id,source,'First\n\nSecond')==ProjectDraftRepository(c.db).get(c.job.project_id).model_dump_json()
    with pytest.raises(UseAdmissionError,match='voice_saved_plan_changed'):
        require_saved_voice_plan(c.db,c.job.project_id,source,'First Second')
    assert c.calls==[]
