"""同一基线的多项装配修复；不调用生成模型或真实生产数据。"""
import hashlib
from datetime import timedelta
from threading import Event
from types import SimpleNamespace
from uuid import UUID,uuid4

import pytest
from fastapi.testclient import TestClient

from app.db import AssetRepository,AudioAssetRepository,Database,JobRepository,ProjectRepository,ProviderCallRepository
from app.domain.models import ContentPlanJobPayload,Job,JobStatus,JobType,VideoSpec
from app.jobs import JobRunner,JobStore
from app.jobs.handlers import RenderVideoJobHandler
from app.main import create_app
from app.presentation_repair import PresentationObservationRequest,PresentationRepairRequest,PresentationRepairService
from app.video_production import VideoProductionService
from app.worker_cli import _run_product_queue
from test_production_runs import _ready_fixture,_admit_master,_fixture_reviewable_planned_preview
from test_presentation_repair import synthetic_video


def _two_scenes(tmp_path):
    path=tmp_path/'joint.sqlite'
    with TestClient(create_app(path)) as client:
        pid,_,_=_ready_fixture(client,path,with_master=False)
        draft=client.get(f'/projects/{pid}/draft').json()
        first={**draft['scenes'][0],'duration_target_ms':1500,'caption_emphasis':['A fresh point']}
        second={**first,'id':str(uuid4()),'scene_id':'closing','order':1,'voice_text':'A clear closing thought.',
            'caption_emphasis':['A clear closing']}
        copy=first['voice_text']+' '+second['voice_text']
        saved=client.put(f'/projects/{pid}/draft',json={'script':copy,'topic':'New','scenes':[first,second]})
        assert saved.status_code==200,saved.text
        _admit_master(path,pid,copy)
        plan=client.post(f'/projects/{pid}/production-preflight',json={}).json()
        started=client.post(f'/projects/{pid}/production-runs',json={'idempotency_key':'joint-fixture','expected_fingerprint':plan['fingerprint']})
        assert started.status_code==201,started.text
        run=started.json()
    db=Database(path);db.data_root=tmp_path
    old=JobRepository(db).get(UUID(run['render_job_id']))
    raw=old.payload.video_spec.model_dump(mode='json')
    for scene,entry in zip(raw['scenes'],raw['edit_plan']['scenes']):
        text=''.join(caption['text'] for caption in scene['captions']) or scene['caption']
        scene['graphic_text']=entry['graphic_text']=text
    old=JobRepository(db).update(old.model_copy(update={'status':JobStatus.COMPLETED,
        'payload':old.payload.model_copy(update={'video_spec':VideoSpec.model_validate(raw)})}))
    parent=JobRepository(db).create(Job(project_id=UUID(pid),type=JobType.PLAN_CONTENT,status=JobStatus.COMPLETED,
        idempotency_key='joint-start',created_at=old.created_at,updated_at=old.updated_at,
        payload=ContentPlanJobPayload(project_id=UUID(pid),topic='joint fixture')))
    db.connection.execute('UPDATE production_runs SET automatic=1,start_job_id=? WHERE id=?',(str(parent.id),run['id']))
    output=tmp_path/'renders'/pid/f'{old.payload.render_id}.mp4'
    output.parent.mkdir(parents=True,exist_ok=True);output.write_bytes(b'fixture historical joint output')
    service=PresentationRepairService(db,tmp_path,tmp_path/'renders')
    observed=[service.observe(UUID(pid),UUID(run['id']),PresentationObservationRequest(
        idempotency_key=f'joint-{scene.scene_id}',render_sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
        scene_id=scene.scene_id,finding='duplicate_text',reason='fixture independent duplicate',evidence_reference='fixture:baseline'))
        for scene in old.payload.video_spec.scenes]
    return db,UUID(pid),UUID(run['id']),old,output,observed


@pytest.mark.parametrize('corrupt',[False,True])
def test_one_ordinary_queue_revision_fixes_two_scenes_and_keeps_all_media(tmp_path,corrupt):
    db,pid,run,old,output,observed=_two_scenes(tmp_path)
    with db:
        service=PresentationRepairService(db,tmp_path,tmp_path/'renders')
        plans=[service.plan(pid,run,UUID(item['id'])) for item in observed]
        assert plans[0]['candidate_spec']==plans[1]['candidate_spec']
        plan=plans[0]
        assert plan['action']=='render_revision' and plan['assessment_state']=='known'
        assert len(plan['changes'])==len(plan['diagnoses'])==2 and plan['required_capability']==[]
        assert {item['scene_id'] for item in plan['changes']}=={'hook','closing'}
        assert all(item['assessment_state']=='known' for item in plan['diagnoses'])
        assert [scene['graphic_text'] for scene in plan['candidate_spec']['scenes']]==['A fresh point','A clear closing']
        master=AudioAssetRepository(db).get(old.payload.video_spec.master_narration.audio_asset_id)
        before=JobRepository(db).list()
        center=VideoProductionService(db,tmp_path)
        diagnosis=center.decision_for_run(run,{JobType.RENDER})['quality_diagnosis']
        assert diagnosis['assessment_state']==plan['assessment_state']=='known'
        assert diagnosis['responsible_stage']==plan['responsible_stage']=='assembly'
        assert [item['assessment_state'] for item in diagnosis['findings']]==[item['assessment_state'] for item in plan['diagnoses']]
        class Renderer:
            data_root=tmp_path
            def render(self,spec,target,**kwargs):
                if corrupt:
                    target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(b'invalid joint output')
                else:synthetic_video(target,spec)
        runner=JobRunner(JobStore(db),{JobType.RENDER:RenderVideoJobHandler(ProjectRepository(db),Renderer(),tmp_path/'renders')},
            worker_id='joint-worker',lease_duration=timedelta(minutes=1),max_attempts=1,on_completed=center.completed)
        runner.production_service=center
        assert _run_product_queue(SimpleNamespace(once=True,poll_seconds=.01),db,runner,Event(),run_id=run)==(1 if corrupt else 0)
        current=center.status(pid)
        assert current['next_action']==('stopped' if corrupt else 'review')
        assert len(JobRepository(db).list())==len(before)+1
        assert AudioAssetRepository(db).get(master.id)==master
        assert JobRepository(db).get(old.id)==old and output.read_bytes()==b'fixture historical joint output'
        history=service.history(pid,run)
        assert len(history)==1 and len(history[0]['plan']['observations'])==2
        if not corrupt:assert history[0]['technical_qa']['decode']=='pass'
        calls=ProviderCallRepository(db).list_for_project(pid)
        assert len(calls)==1 and calls[0].operation=='render'
        assert center.queue_jobs({JobType.RENDER},run_id=run)==[]
        assert len(JobRepository(db).list())==len(before)+1


def test_one_unlocated_finding_prevents_partial_joint_output_and_preserves_every_observation(tmp_path):
    db,pid,run,old,_,observed=_two_scenes(tmp_path)
    with db:
        service=PresentationRepairService(db,tmp_path,tmp_path/'renders')
        extra=service.observe(pid,run,PresentationObservationRequest(**{**observed[0]['request'],
            'idempotency_key':'unlocated-voice','finding':'voice_naturalness'}))
        before=JobRepository(db).list()
        for selected in [*observed,extra]:
            plan=service.plan(pid,run,UUID(selected['id']))
            assert plan['action']=='scoped_fix_required' and plan['assessment_state']=='unknown'
            assert len(plan['diagnoses'])==3 and len(plan['observations'])==3
            assert plan['candidate_spec'] is None and plan['changes']==[]
        assert VideoProductionService(db,tmp_path).queue_jobs({JobType.RENDER},run_id=run)==[]
        assert JobRepository(db).list()==before and ProviderCallRepository(db).list_for_project(pid)==[]


def test_new_finding_after_joint_plan_invalidates_the_whole_candidate(tmp_path):
    db,pid,run,old,_,observed=_two_scenes(tmp_path)
    with db:
        service=PresentationRepairService(db,tmp_path,tmp_path/'renders')
        plan=service.plan(pid,run,UUID(observed[0]['id']))
        service.observe(pid,run,PresentationObservationRequest(**{**observed[0]['request'],
            'idempotency_key':'late-voice','finding':'voice_naturalness'}))
        with pytest.raises(ValueError,match='stale'):
            service.apply(pid,run,PresentationRepairRequest(observation_id=UUID(observed[0]['id']),expected_fingerprint=plan['fingerprint'],
                idempotency_key='stale-joint',confirmed_action='render_revision',reason='joint correction'))
        assert db.connection.execute('SELECT render_job_id FROM production_runs WHERE id=?',(str(run),)).fetchone()[0]==str(old.id)
        assert db.connection.execute('SELECT COUNT(*) FROM presentation_repairs').fetchone()[0]==0


@pytest.mark.parametrize('other',['known_subtitle_conflict','text_overlay'])
def test_same_scene_joint_correction_keeps_talking_master_and_source_bindings(tmp_path,monkeypatch,other):
    path=tmp_path/'talking-joint.sqlite'
    with TestClient(create_app(path)) as client:
        pid,waiting,bound,preview=_fixture_reviewable_planned_preview(client,path,monkeypatch)
        series=f'/projects/{pid}/talking-slice-series/{bound["talking_series_id"]}'
        assert client.post(series+'/continuity-review',json={'approved':True,'evidence_reference':'fixture:joint',
            'findings':[],'preview_asset_id':str(preview.id),'preview_sha256':preview.content_hash}).status_code==201
        assert client.post(series+'/talking-run').status_code==201
        run_id=UUID(waiting['id'])
        resumed=client.post(f'/projects/{pid}/production-runs/{run_id}/resume')
        assert resumed.status_code==200,resumed.text
    with Database(path) as db:
        db.data_root=tmp_path
        old=JobRepository(db).get(UUID(resumed.json()['render_job_id']))
        raw=old.payload.video_spec.model_dump(mode='json')
        scene,entry=raw['scenes'][0],raw['edit_plan']['scenes'][0]
        scene['graphic_text']=entry['graphic_text']=''.join(value['text'] for value in scene['captions']) or scene['caption']
        scene['graphic_treatment']=entry['graphic_treatment']='headline'
        scene['subtitle_treatment']=entry['subtitle_treatment']='timed_captions'
        entry['burned_in_subtitles']='present' if other=='known_subtitle_conflict' else 'absent'
        old=JobRepository(db).update(old.model_copy(update={'status':JobStatus.COMPLETED,
            'payload':old.payload.model_copy(update={'video_spec':VideoSpec.model_validate(raw)})}))
        output=tmp_path/'renders'/pid/f'{old.payload.render_id}.mp4'
        output.parent.mkdir(parents=True,exist_ok=True);output.write_bytes(b'fixture same scene old render')
        service=PresentationRepairService(db,tmp_path,tmp_path/'renders')
        observations=[service.observe(UUID(pid),run_id,PresentationObservationRequest(idempotency_key=finding,
            render_sha256=hashlib.sha256(output.read_bytes()).hexdigest(),scene_id='hook',finding=finding,
            reason='fixture compound defect',evidence_reference='fixture:exact-source'))
            for finding in ('duplicate_text',other)]
        master=AudioAssetRepository(db).get(UUID(bound['master_audio_id']))
        talking=AssetRepository(db).get(UUID(bound['talking_output_asset_id']))
        bindings=db.connection.execute('SELECT talking_source_bindings FROM production_runs WHERE id=?',(str(run_id),)).fetchone()[0]
        plan=service.plan(UUID(pid),run_id,UUID(observations[0]['id']))
        assert plan['action']=='render_revision',plan['stop_reasons']
        assert len(plan['diagnoses'])==2 and len(plan['changes'])==1
        assert plan['scope']['scene_id']=='hook'
        candidate=plan['candidate_spec']['scenes'][0]
        assert candidate['graphic_text'] is None
        assert candidate['subtitle_treatment']==('none' if other=='known_subtitle_conflict' else 'timed_captions')
        assert candidate['portrait_presentation']==scene['portrait_presentation']
        assert candidate['visual']==scene['visual']
        service.apply(UUID(pid),run_id,PresentationRepairRequest(observation_id=UUID(observations[0]['id']),
            expected_fingerprint=plan['fingerprint'],idempotency_key='talking-joint-once',confirmed_action='render_revision',reason='joint assembly only'))
        assert AudioAssetRepository(db).get(master.id)==master and AssetRepository(db).get(talking.id)==talking
        assert db.connection.execute('SELECT talking_source_bindings FROM production_runs WHERE id=?',(str(run_id),)).fetchone()[0]==bindings
        assert ProviderCallRepository(db).list_for_project(UUID(pid))==[]
