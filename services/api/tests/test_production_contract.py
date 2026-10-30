"""隔离工序事实：创建、调度及状态消费同一决策，不调用真实提供商。"""
import hashlib
import json
from unittest.mock import Mock
from uuid import UUID

import pytest

from app.db import AudioAssetRepository, BudgetPolicyRepository, JobRepository
from app.domain.models import AudioAsset, BudgetPolicy, ContentPlanJobPayload, JobStatus, JobType, VoiceHumanReview, VideoSpec
from app.presentation_repair import PresentationObservationRequest, PresentationRepairService
from app.production_runs import ProductionRunNotReady
from app.video_production import VideoProductionService
from app.voice_qa import apply_voice_human_review
from test_material_repair import material, prepared
from test_material_semantic_recheck import repaired, verifier, check
from test_production_run_worker_cli import _fixture, _job, _production
from test_semantic_production import _automatic


def reject(audio, now):
    generation={**audio.metadata.get('voice_generation',{}),'qa_state':'verified'}
    return apply_voice_human_review(audio.model_copy(update={'metadata':{**audio.metadata,'voice_generation':generation}}),
        VoiceHumanReview(approved=False,evidence_reference='fixture:exact-rejected-audio',findings=['fixture unnatural delivery'],
            likeness='pass',naturalness='needs_revision',emphasis='pass',pace='pass',pauses='pass',rhythm='pass',reviewed_at=now))


@pytest.mark.parametrize('rejected',[False,True])
def test_voice_quality_decision_is_shared_by_status_queue_dispatch_and_completion(tmp_path,rejected):
    db,project,now=_fixture(tmp_path/'contract.sqlite')
    with db:
        parent=_job(db,project,now,'input',JobType.PLAN_CONTENT)
        JobRepository(db).update(parent.model_copy(update={'status':JobStatus.COMPLETED,
            'payload':ContentPlanJobPayload(project_id=project.id,topic=project.topic)}))
        voice=JobRepository(db).update(_job(db,project,now,'voice',JobType.GENERATE_VOICE).model_copy(update={'status':JobStatus.COMPLETED}))
        qa=JobRepository(db).update(_job(db,project,now,'qa',JobType.VERIFY_VOICE).model_copy(update={'status':JobStatus.COMPLETED}))
        source=tmp_path/'master.wav';source.write_bytes(b'fixture existing master, no quality claim')
        audio=AudioAsset(source_file=str(source),content_hash=hashlib.sha256(source.read_bytes()).hexdigest(),
            duration_ms=1000,sample_rate=16000,channels=1,authorization_reference='intake',imported_at=now,
            metadata={'voice_generation':{'qa_state':'verified'}})
        audio=AudioAssetRepository(db).create(reject(audio,now) if rejected else audio)
        run_id=_production(db,project,now,[],stage='voice')
        db.connection.execute('UPDATE production_runs SET automatic=1,start_job_id=?,voice_job_id=?,voice_qa_job_id=?,voice_audio_id=? WHERE id=?',
            (str(parent.id),str(voice.id),str(qa.id),str(audio.id),str(run_id)))
        service=VideoProductionService(db,tmp_path)
        service.runs.reconcile_completed_voice_job=Mock();service.advance=Mock()
        before=JobRepository(db).list()
        decision=service.decision_for_run(run_id,set(JobType))
        status=service.status(project.id)
        assert decision['next_action']==status['next_action']==('stopped' if rejected else 'continue')
        assert decision['responsible_stage']==status['responsible_stage']=='voice'
        assert service.job_allowed(voice,set(JobType)) is not rejected
        assert service.queue_jobs(set(JobType),run_id=run_id)==[]
        service.completed(voice)
        if rejected:
            assert decision['stop_reasons']==['voice_quality_review_rejected']
            assert status['run']['status']=='u_voice_rejected'
            service.advance.assert_not_called();service.runs.reconcile_completed_voice_job.assert_not_called()
        else:
            assert status['run']['status']=='voice_qa_ready_for_draft'
            assert service.advance.called
        assert JobRepository(db).list()==before and AudioAssetRepository(db).get(audio.id)==audio


def test_consumed_voice_rejection_returns_to_voice_without_reactivating_render(material):
    c=material;center=_automatic(c,None)
    audio=AudioAssetRepository(c.db).update(reject(c.master,c.master.imported_at))
    c.db.connection.execute('UPDATE production_runs SET voice_audio_id=? WHERE id=?',(str(audio.id),str(c.run)))
    jobs=JobRepository(c.db).list();master_bytes=(c.root/audio.source_file).read_bytes()
    status=center.status(c.project)
    assert status['next_action']=='stopped' and status['responsible_stage']=='voice'
    assert center.queue_jobs(set(JobType),run_id=c.run)==[]
    assert JobRepository(c.db).list()==jobs and (c.root/audio.source_file).read_bytes()==master_bytes


def test_an_old_presentation_observation_cannot_relabel_an_explicit_planning_stop(material):
    c=material;center=_automatic(c,None)
    c.db.connection.execute('UPDATE production_runs SET waiting_stop_reasons=? WHERE id=?',
        (json.dumps(['automatic_stop:scene_planner_response_invalid']),str(c.run)))
    status=center.status(c.project)
    assert status['next_action']=='stopped' and status['responsible_stage']=='planning'
    assert status['quality_diagnosis']['finding']=='semantic_mismatch'
    assert center.queue_jobs(set(JobType),run_id=c.run)==[]


@pytest.mark.parametrize('crop',[False,True,'visual_only'])
def test_subtitle_classification_and_fix_plan_use_the_same_exact_source_conditions(material,crop):
    c=material
    raw=c.job.payload.video_spec.model_dump(mode='json')
    raw['edit_plan']['scenes'][1].update(burned_in_subtitles='present',subtitle_treatment='timed_captions',
        source_bottom_crop_ratio=.1 if crop is True else 0,source_subtitle_evidence_reference='fixture:source-video' if crop is True else None)
    raw['scenes'][1]['subtitle_treatment']='timed_captions'
    raw['scenes'][1]['visual'].update(source_bottom_crop_ratio=.1 if crop else 0,
        vertical_reframe_mode='center_crop' if crop else 'contain',
        vertical_reframe_evidence_reference='fixture:source-video' if crop else None)
    spec=VideoSpec.model_validate(raw)
    c.job=JobRepository(c.db).update(c.job.model_copy(update={'payload':c.job.payload.model_copy(update={'video_spec':spec})}))
    c.db.connection.execute('DELETE FROM presentation_observations')
    center=_automatic(c,None)
    row=c.db.connection.execute('SELECT * FROM production_runs WHERE id=?',(str(c.run),)).fetchone()
    center._observe_render_spec(c.job,row)
    saved=c.db.connection.execute('SELECT payload FROM presentation_observations').fetchall()
    assert bool(saved) is not crop
    observation=c.service.observe(c.project,c.run,PresentationObservationRequest(idempotency_key='exact-source-note',
        render_sha256=hashlib.sha256(c.output.read_bytes()).hexdigest(),scene_id=c.scene.scene_id,
        finding='known_subtitle_conflict',reason='fixture source caption overlap',evidence_reference='fixture:current-output'))
    plan=c.service.plan(c.project,c.run,UUID(observation['id']))
    decision=center.decision_for_run(c.run,{JobType.RENDER})
    diagnosis=decision['quality_diagnosis']
    assert diagnosis['responsible_stage']==plan['responsible_stage']==('material' if crop else 'assembly')
    assert diagnosis['assessment_state']==plan['assessment_state']==('unknown' if crop else 'known')
    if crop:
        assert plan['candidate_spec'] is None and decision['next_action']=='stopped'
    else:
        assert plan['candidate_spec']['scenes'][1]['subtitle_treatment']=='none'
        assert plan['candidate_spec']['master_narration']==spec.master_narration.model_dump(mode='json')
        assert decision['next_action']=='diagnose_quality'


@pytest.mark.parametrize('phase',['pending','unknown','pass'])
def test_new_input_admission_consumes_current_decision_not_just_completed_render(repaired,phase):
    c=repaired;diagnoser,_=verifier(c)
    if phase!='pending':
        if phase=='unknown':diagnoser.provider.last_validation_issues=[{'code':'fixture uncertain'}]
        check(c,diagnoser)
    BudgetPolicyRepository(c.db).save(BudgetPolicy(project_id=c.project,max_calls=100,allow_unknown_cost=True,updated_at=c.job.updated_at))
    center=VideoProductionService(c.db,c.root,semantic_diagnoser=diagnoser)
    action=center.decision_for_run(c.run,set(JobType))['next_action']
    before=JobRepository(c.db).list()
    if phase=='pending':
        assert action=='verify_material'
        with pytest.raises(ProductionRunNotReady,match='video_production_already_running'):
            center.enqueue(c.project,'下一条完整新主题','next-input')
        assert JobRepository(c.db).list()==before
    else:
        assert action==('stopped' if phase=='unknown' else 'review')
        assert center.enqueue(c.project,'下一条完整新主题','next-input').type is JobType.PLAN_CONTENT
