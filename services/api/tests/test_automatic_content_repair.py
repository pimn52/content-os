"""普通中心消费确切成片观察；只修装配，未知质量不触发全文或媒体重生。"""
from datetime import timedelta
from threading import Event
from types import SimpleNamespace
from uuid import UUID
from uuid import uuid4
import json

import pytest
from fastapi.testclient import TestClient

from app.db import AudioAssetRepository,Database,JobRepository,ProjectRepository,ProviderCallRepository
from app.domain.models import ContentPlanJobPayload,Job,JobStatus,JobType
from app.jobs import JobRunner,JobStore
from app.jobs.handlers import RenderVideoJobHandler
from app.main import create_app
from app.presentation_repair import PresentationRepairService
from app.presentation_repair import PresentationObservationRequest,PresentationRepairRequest
from app.video_production import VideoProductionService
from app.worker_cli import _run_product_queue
import test_presentation_repair as fixture


def _completed(tmp_path,monkeypatch,finding='duplicate_text'):
    path=tmp_path/'ordinary-repair.sqlite'
    def historical(path,spec):
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(b'fixture historical rendered output')
    monkeypatch.setattr(fixture,'synthetic_video',historical)
    with TestClient(create_app(path)) as client:
        pid,run,_,job,output,observation=fixture.setup(client,path,finding)
    db=Database(path);db.data_root=tmp_path
    parent=JobRepository(db).create(Job(project_id=UUID(pid),type=JobType.PLAN_CONTENT,status=JobStatus.COMPLETED,
        payload=ContentPlanJobPayload(project_id=UUID(pid),topic='隔离历史装配缺陷'),idempotency_key='fixture-start',
        created_at=job.created_at,updated_at=job.updated_at))
    db.connection.execute('UPDATE production_runs SET automatic=1,start_job_id=? WHERE id=?',(str(parent.id),run['id']))
    return db,UUID(pid),UUID(run['id']),job,output,observation


@pytest.mark.parametrize('corrupt',[False,True])
def test_ordinary_queue_repairs_only_assembly_then_rechecks_the_whole_output(tmp_path,monkeypatch,corrupt):
    real_video=fixture.synthetic_video
    db,pid,run,old,output,observation=_completed(tmp_path,monkeypatch)
    with db:
        center=VideoProductionService(db,tmp_path)
        master_id=old.payload.video_spec.master_narration.audio_asset_id
        master=AudioAssetRepository(db).get(master_id)
        before_jobs=JobRepository(db).list()
        decision=center.decision_for_run(run,{JobType.RENDER})
        assert decision['next_action']=='diagnose_quality'
        assert decision['quality_diagnosis']['observation_id']==observation['id']
        assert center.status(pid)['responsible_stage']=='assembly'
        class Renderer:
            data_root=tmp_path
            def render(self,spec,target,**kwargs):
                if corrupt:
                    target.parent.mkdir(parents=True,exist_ok=True)
                    target.write_bytes(b'invalid replacement output')
                else:real_video(target,spec)
        runner=JobRunner(JobStore(db),{JobType.RENDER:RenderVideoJobHandler(ProjectRepository(db),Renderer(),tmp_path/'renders')},
            worker_id='ordinary-quality',lease_duration=timedelta(minutes=1),max_attempts=1,on_completed=center.completed)
        runner.production_service=center
        assert _run_product_queue(SimpleNamespace(once=True,poll_seconds=.01),db,runner,Event(),run_id=run)==(1 if corrupt else 0)
        status=center.status(pid)
        assert status['next_action']==('stopped' if corrupt else 'review') and status['quality_diagnosis'] is None
        current=JobRepository(db).get(UUID(status['run']['render_job_id']))
        assert current.id!=old.id and current.status is (JobStatus.FAILED if corrupt else JobStatus.COMPLETED)
        assert current.payload.video_spec.master_narration==old.payload.video_spec.master_narration
        assert AudioAssetRepository(db).get(master_id)==master
        jobs=JobRepository(db).list()
        assert len(jobs)==len(before_jobs)+1
        assert all(job.type is JobType.RENDER for job in jobs if job.id not in {item.id for item in before_jobs})
        assert JobRepository(db).get(old.id)==old and output.read_bytes()==b'fixture historical rendered output'
        history=PresentationRepairService(db,tmp_path,tmp_path/'renders').history(pid,run)
        if not corrupt:assert history[0]['technical_qa']['decode']=='pass'
        assert history[0]['final_review_state']=='not_submitted'
        calls=ProviderCallRepository(db).list_for_project(pid)
        assert len(calls)==1 and calls[0].operation=='render'
        assert center.queue_jobs({JobType.RENDER},run_id=run)==[]
        assert len(JobRepository(db).list())==len(jobs)


@pytest.mark.parametrize('finding,owner',[('voice_naturalness','voice'),('talking_naturalness','talking'),
    ('visible_sync','unknown'),('semantic_mismatch','unknown'),('narrative_rhythm','timing')])
def test_unlocated_content_quality_keeps_results_without_any_generation(tmp_path,monkeypatch,finding,owner):
    db,pid,run,old,_,observation=_completed(tmp_path,monkeypatch,finding)
    with db:
        center=VideoProductionService(db,tmp_path)
        before=JobRepository(db).list()
        assert center.queue_jobs(set(JobType),run_id=run)==[]
        status=center.status(pid)
        assert status['next_action']=='stopped' and status['responsible_stage']==owner
        assert status['quality_diagnosis']['assessment_state']=='unknown'
        assert status['quality_diagnosis']['scope']['scene_id']=='hook'
        assert status['quality_diagnosis']['required_capability']
        assert JobRepository(db).list()==before and JobRepository(db).get(old.id)==old
        assert ProviderCallRepository(db).list_for_project(pid)==[]


def test_stale_render_hash_stops_local_fix_without_replanning_or_regenerating(tmp_path,monkeypatch):
    db,pid,run,old,output,_=_completed(tmp_path,monkeypatch)
    with db:
        output.write_bytes(b'changed after observation')
        center=VideoProductionService(db,tmp_path)
        before=JobRepository(db).list()
        assert center.queue_jobs({JobType.RENDER},run_id=run)==[]
        assert center.status(pid)['next_action']=='stopped'
        assert center.queue_jobs({JobType.RENDER},run_id=run)==[]
        assert JobRepository(db).list()==before
        assert ProviderCallRepository(db).list_for_project(pid)==[]


def test_discarded_output_is_not_reactivated_by_a_previous_quality_observation(tmp_path,monkeypatch):
    db,pid,run,old,_,_=_completed(tmp_path,monkeypatch)
    with db:
        db.connection.execute('INSERT INTO render_choices VALUES(?,?,?,?)',
            (str(pid),str(old.payload.render_id),'discarded',old.updated_at.isoformat()))
        center=VideoProductionService(db,tmp_path)
        before=JobRepository(db).list()
        assert center.queue_jobs({JobType.RENDER},run_id=run)==[]
        assert center.status(pid)['quality_diagnosis'] is None
        assert JobRepository(db).list()==before and ProviderCallRepository(db).list_for_project(pid)==[]


def test_interrupted_completion_records_deterministic_scope_without_claiming_subjective_qa(tmp_path,monkeypatch):
    db,pid,run,old,_,observation=_completed(tmp_path,monkeypatch)
    with db:
        db.connection.execute('DELETE FROM presentation_observations WHERE id=?',(observation['id'],))
        center=VideoProductionService(db,tmp_path)
        center.completed(old)
        saved=db.connection.execute('SELECT payload FROM presentation_observations WHERE run_id=?',(str(run),)).fetchone()
        import json
        record=json.loads(saved[0])
        assert record['source']=='deterministic_spec_qa' and record['request']['finding']=='duplicate_text'
        center.completed(old)
        assert db.connection.execute('SELECT COUNT(*) FROM presentation_observations WHERE run_id=?',(str(run),)).fetchone()[0]==1
        assert ProviderCallRepository(db).list_for_project(pid)==[]


def test_actual_worker_recovery_reconciles_a_missing_render_completion_callback(tmp_path,monkeypatch):
    db,pid,run,old,_,observation=_completed(tmp_path,monkeypatch)
    with db:
        db.connection.execute('DELETE FROM presentation_observations WHERE id=?',(observation['id'],))
        center=VideoProductionService(db,tmp_path)
        center.recover_completed({JobType.RENDER})
        queued=center.queue_jobs({JobType.RENDER},run_id=run)
        assert len(queued)==1 and queued[0].id!=old.id
        assert queued[0].payload.video_spec.master_narration==old.payload.video_spec.master_narration
        assert ProviderCallRepository(db).list_for_project(pid)==[]


def _feedback(db,pid,job,accepted):
    value={'id':str(uuid4()),'project_id':str(pid),'output_version':f'render:{job.payload.render_id}',
        'accepted':accepted,'created_at':job.updated_at.isoformat()}
    db.connection.execute('INSERT INTO content_feedback VALUES(?,?,?,?)',
        (value['id'],str(pid),value['output_version'],json.dumps(value)))


def test_rejected_output_still_exposes_and_repairs_a_new_scoped_observation(tmp_path,monkeypatch):
    db,pid,run,old,_,_=_completed(tmp_path,monkeypatch)
    with db:
        _feedback(db,pid,old,False)
        center=VideoProductionService(db,tmp_path)
        assert center.decision_for_run(run,{JobType.RENDER})['next_action']=='diagnose_quality'
        assert len(center.queue_jobs({JobType.RENDER},run_id=run))==1
        assert ProviderCallRepository(db).list_for_project(pid)==[]


def test_old_diagnosis_cannot_stop_an_already_created_replacement(tmp_path,monkeypatch):
    db,pid,run,old,_,observation=_completed(tmp_path,monkeypatch)
    with db:
        center=VideoProductionService(db,tmp_path)
        snapshot=db.connection.execute('SELECT * FROM production_runs WHERE id=?',(str(run),)).fetchone()
        diagnosis=center.decision_for_run(run,{JobType.RENDER})['quality_diagnosis']
        service=PresentationRepairService(db,tmp_path,tmp_path/'renders')
        plan=service.plan(pid,run,UUID(observation['id']))
        replacement=service.apply(pid,run,PresentationRepairRequest(observation_id=UUID(observation['id']),
            expected_fingerprint=plan['fingerprint'],idempotency_key='other-worker',confirmed_action='render_revision',reason='same scoped fix'))
        center._repair_presentation(snapshot,diagnosis)
        current=db.connection.execute('SELECT * FROM production_runs WHERE id=?',(str(run),)).fetchone()
        assert current['render_job_id']==replacement['replacement_job_id'] and json.loads(current['waiting_stop_reasons'])==[]
        assert len(center.queue_jobs({JobType.RENDER},run_id=run))==1


def test_accepting_output_during_diagnosis_prevents_automatic_replacement(tmp_path,monkeypatch):
    db,pid,run,old,_,_=_completed(tmp_path,monkeypatch)
    with db:
        original=PresentationRepairService.plan
        first=[True]
        def plan(service,*args):
            result=original(service,*args)
            if first[0]:
                first[0]=False;_feedback(db,pid,old,True)
            return result
        monkeypatch.setattr(PresentationRepairService,'plan',plan)
        center=VideoProductionService(db,tmp_path)
        before=JobRepository(db).list()
        assert center.queue_jobs({JobType.RENDER},run_id=run)==[]
        assert JobRepository(db).list()==before
        assert center.status(pid)['next_action']=='review'
        assert ProviderCallRepository(db).list_for_project(pid)==[]


def test_multiple_independent_findings_are_not_hidden_by_the_latest_observation(tmp_path,monkeypatch):
    db,pid,run,old,_,observation=_completed(tmp_path,monkeypatch)
    with db:
        service=PresentationRepairService(db,tmp_path,tmp_path/'renders')
        service.observe(pid,run,PresentationObservationRequest(**{**observation['request'],
            'idempotency_key':'voice-finding','finding':'voice_naturalness'}))
        center=VideoProductionService(db,tmp_path)
        before=JobRepository(db).list()
        assert center.queue_jobs({JobType.RENDER},run_id=run)==[]
        status=center.status(pid)
        assert status['next_action']=='stopped'
        assert status['quality_diagnosis']['assessment_state']=='unknown'
        assert {item['finding'] for item in status['quality_diagnosis']['findings']}=={'duplicate_text','voice_naturalness'}
        assert JobRepository(db).list()==before and ProviderCallRepository(db).list_for_project(pid)==[]


def test_spec_qa_records_all_independent_findings_before_deciding_to_render_again(tmp_path,monkeypatch):
    db,pid,run,old,_,observation=_completed(tmp_path,monkeypatch)
    with db:
        db.connection.execute('DELETE FROM presentation_observations WHERE id=?',(observation['id'],))
        edits=list(old.payload.video_spec.edit_plan.scenes)
        edits[0]=edits[0].model_copy(update={'burned_in_subtitles':'present'})
        spec=old.payload.video_spec.model_copy(update={'edit_plan':old.payload.video_spec.edit_plan.model_copy(update={'scenes':edits})})
        old=JobRepository(db).update(old.model_copy(update={'payload':old.payload.model_copy(update={'video_spec':spec})}))
        center=VideoProductionService(db,tmp_path)
        center.recover_completed({JobType.RENDER})
        decision=center.decision_for_run(run,{JobType.RENDER})
        assert decision['next_action']=='diagnose_quality'
        assert decision['quality_diagnosis']['finding']=='duplicate_text'
        # 没有实际选中原片区间，不能只靠一个 present 标签伪造源字幕冲突。
        assert all(json.loads(item[0])['request']['finding']!='known_subtitle_conflict' for item in
            db.connection.execute('SELECT payload FROM presentation_observations'))
        assert ProviderCallRepository(db).list_for_project(pid)==[]
