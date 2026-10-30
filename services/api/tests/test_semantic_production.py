"""语义观察进入同一普通生产中心；不把诊断升级为声音或人物修复。"""
from uuid import UUID

import pytest

from app.db import JobRepository,ProjectDraftRepository,ProviderCallRepository
from app.domain.models import ContentPlanJobPayload,Job,JobStatus,JobType
from app.presentation_repair import PresentationRepairService
from app.presentation_semantic import PresentationSemanticDiagnosis
from app.video_production import VideoProductionService
from test_presentation_semantic import prepared,FixtureProvider


def _automatic(c,provider):
    parent=JobRepository(c.db).create(Job(project_id=c.project,type=JobType.PLAN_CONTENT,status=JobStatus.COMPLETED,
        idempotency_key='fixture-automatic-start',created_at=c.job.created_at,updated_at=c.job.updated_at,
        payload=ContentPlanJobPayload(project_id=c.project,topic='fixture semantic comparison')))
    c.db.connection.execute('UPDATE production_runs SET automatic=1,start_job_id=? WHERE id=?',(str(parent.id),str(c.run)))
    return VideoProductionService(c.db,c.root,semantic_diagnoser=PresentationSemanticDiagnosis(
        c.db,c.root,provider,c.media,c.media))


@pytest.mark.parametrize('source,render,owner',[(True,False,'assembly'),(False,False,'material'),(True,True,'unknown')])
def test_ordinary_queue_records_one_comparison_and_returns_the_actual_scope_owner(prepared,source,render,owner):
    c=prepared;provider=FixtureProvider(source=source,render=render);center=_automatic(c,provider)
    jobs=JobRepository(c.db).list()
    assert center.status(c.project)['next_action']=='diagnose_semantic'
    assert center.queue_jobs({JobType.RENDER},run_id=c.run)==[]
    status=center.status(c.project)
    assert status['next_action']==('diagnose_quality' if owner=='material' else 'stopped') and status['responsible_stage']==owner
    assert status['quality_diagnosis']['assessment_state']==('known' if owner!='unknown' else 'unknown')
    assert status['quality_diagnosis']['machine_diagnosis']['call_id']
    service=PresentationRepairService(c.db,c.root,c.root/'renders')
    plan=service.plan(c.project,c.run,UUID(c.observation['id']))
    assert plan['action']=='scoped_fix_required' and plan['responsible_stage']==owner
    assert plan['candidate_spec'] is None and plan['changes']==[]
    assert plan['voice_calls']==plan['talking_calls']==0
    assert plan['diagnoses'][0]['machine_diagnosis']['limitations']
    assert plan['preserves']['master_audio_id']==str(c.master.id)
    for _ in range(3):
        center.status(c.project);assert center.queue_jobs({JobType.RENDER},run_id=c.run)==[]
    assert provider.calls==1 and len(ProviderCallRepository(c.db).list_for_project(c.project))==1
    assert JobRepository(c.db).list()==jobs


def test_missing_video_configuration_is_explicit_unknown_without_a_provider_call(prepared):
    c=prepared;center=_automatic(c,None)
    assert center.queue_jobs({JobType.RENDER},run_id=c.run)==[]
    diagnosis=center.status(c.project)['quality_diagnosis']
    assert diagnosis['assessment_state']=='unknown'
    assert 'configured_video_comparison_provider' in diagnosis['required_capability']
    assert ProviderCallRepository(c.db).list_for_project(c.project)==[]


def test_changed_input_invalidates_saved_semantic_owner_without_generating_or_calling_again(prepared):
    c=prepared;provider=FixtureProvider();center=_automatic(c,provider)
    center.queue_jobs({JobType.RENDER},run_id=c.run)
    draft=ProjectDraftRepository(c.db).get(c.project)
    ProjectDraftRepository(c.db).save(draft.model_copy(update={'version':draft.version+1}))
    diagnosis=center.status(c.project)['quality_diagnosis']
    assert diagnosis['assessment_state']=='unknown' and not diagnosis['evidence_current']
    assert 'current_render_semantic_diagnosis' in diagnosis['required_capability']
    center.queue_jobs({JobType.RENDER},run_id=c.run)
    assert provider.calls==1


def test_non_render_worker_does_not_consume_or_permanently_fail_pending_semantic_diagnosis(prepared):
    c=prepared;provider=FixtureProvider();center=_automatic(c,provider)
    assert center.queue_jobs({JobType.GENERATE_VOICE},run_id=c.run)==[]
    assert provider.calls==0 and center.status(c.project)['next_action']=='diagnose_semantic'


def test_concrete_semantic_plan_rechecks_source_bytes_without_retrying_model(prepared):
    c=prepared;provider=FixtureProvider();center=_automatic(c,provider)
    center.queue_jobs({JobType.RENDER},run_id=c.run)
    c.source.write_bytes(b'changed source bytes without metadata update')
    plan=PresentationRepairService(c.db,c.root,c.root/'renders').plan(c.project,c.run,UUID(c.observation['id']))
    assert plan['assessment_state']=='unknown' and plan['responsible_stage']=='unknown'
    assert plan['required_capability']==['current_render_semantic_diagnosis']
    assert plan['diagnoses'][0]['evidence_current'] is False
    assert plan['candidate_spec'] is None and plan['voice_calls']==plan['talking_calls']==0
    assert provider.calls==1
