from types import SimpleNamespace
from unittest.mock import Mock
import json

import pytest

from app.domain.models import JobStatus, JobType
from app.video_production import VideoProductionService


def _row(**changes):
    row = dict(waiting_stop_reasons='[]', voice_qa_auto_stop_reasons='[]',
               start_job_id=None, stage='voice', automatic=1, repair_allowance=1,voice_audio_id=None)
    row.update(changes)
    return row


def _job(kind, status, error=None):
    return SimpleNamespace(type=kind, status=status, error_code=error)


def test_decision_keeps_known_qa_failure_in_diagnosis_and_stops_unknown_or_exhausted():
    service = VideoProductionService(Mock(), '.')
    handlers = {JobType.GENERATE_VOICE, JobType.VERIFY_VOICE}
    failed = _job(JobType.VERIFY_VOICE, JobStatus.FAILED, 'voice_qa_failed')
    result = service._queue_decision(_row(), [failed], handlers)
    assert result['status'] == 'diagnosing_qa'
    assert result['next_action'] == 'diagnose_qa' and result['responsible_stage'] == 'voice'
    assert service._queue_decision(_row(repair_allowance=0), [failed], handlers)['next_action'] == 'stopped'
    unknown = _job(JobType.VERIFY_VOICE, JobStatus.FAILED, 'tool_error')
    assert service._queue_decision(_row(), [unknown], handlers)['next_action'] == 'stopped'
    assert service._queue_decision(_row(), [failed,unknown], handlers)['next_action'] == 'stopped'
    assert service._queue_decision(_row(stage='cancelled'), [], handlers)['next_action'] == 'stopped'
    assert service._queue_decision(_row(waiting_stop_reasons='["automatic_stop:unknown"]'), [], handlers)['next_action'] == 'stopped'


def test_completed_reconciles_before_automatic_advance_and_propagates_worker_capability():
    service = VideoProductionService(Mock(), '.')
    service.runs = Mock()
    service.db.connection.execute.return_value.fetchone.return_value = None
    job = SimpleNamespace(type=JobType.GENERATE_VOICE, id='voice')
    service.completed(job, set())
    service.runs.reconcile_completed_voice_job.assert_called_once_with('voice', qa_worker_ready=False)


@pytest.mark.parametrize('reason,owner',[
    ('automatic_stop:material_scene_unresolved:comparison','material'),
    ('automatic_stop:talking_motion_capacity_insufficient','material'),
    ('automatic_stop:landscape_fallback_dominates_video:comparison','material'),
    ('automatic_stop:draft_talking_speech_would_be_cut','timing'),
    ('automatic_stop:talking_qa_narration_requires_voice_repair','voice'),
    ('automatic_stop:visual_presentation_unresolved:caption','assembly'),
    ('automatic_stop:scene_planner_response_invalid','planning'),
    ('automatic_stop:visual_revision_changed_content','planning'),
])
def test_known_failure_returns_to_its_owner_instead_of_the_runs_current_stage(reason,owner):
    service=VideoProductionService(Mock(),'.')
    decision=service._queue_decision(_row(stage='voice',waiting_stop_reasons=json.dumps([reason])),[],set(JobType))
    assert decision['next_action']=='stopped' and decision['responsible_stage']==owner
    assert decision['next_job_id'] is None


@pytest.mark.parametrize('stop',['parent_failed','cancelled','unknown'])
def test_queue_status_and_completion_obey_the_same_stop_without_dispatch(tmp_path,stop):
    from app.db import JobRepository
    from app.domain.models import ContentPlanJobPayload
    from test_production_run_worker_cli import _fixture,_job as stored_job,_production
    db,project,now=_fixture(tmp_path/'decisions.sqlite')
    with db:
        parent=stored_job(db,project,now,'start',JobType.PLAN_CONTENT)
        parent=JobRepository(db).update(parent.model_copy(update={
            'payload':ContentPlanJobPayload(project_id=project.id,topic=project.topic),
            'status':JobStatus.FAILED if stop=='parent_failed' else JobStatus.COMPLETED,
            'error_code':'planning_failed' if stop=='parent_failed' else None}))
        voice=stored_job(db,project,now,'voice',JobType.GENERATE_VOICE)
        run_id=_production(db,project,now,[],stage='voice')
        db.connection.execute('UPDATE production_runs SET automatic=1,start_job_id=?,voice_job_id=?,stage=?,waiting_stop_reasons=? WHERE id=?',
            (str(parent.id),str(voice.id),'cancelled' if stop=='cancelled' else 'voice',
                json.dumps(['automatic_stop:unclassified'] if stop=='unknown' else []),str(run_id)))
        service=VideoProductionService(db,tmp_path)
        service.runs.reconcile_completed_voice_job=Mock()
        service.advance=Mock()
        decision=service.decision_for_run(run_id,set(JobType))
        assert decision['next_action']=='stopped' and decision['next_job_id'] is None
        assert service.status(project.id)['next_action']=='stopped'
        assert service.queue_jobs(set(JobType),run_id=run_id)==[]
        assert service.job_allowed(voice,set(JobType)) is False
        service.completed(voice.model_copy(update={'status':JobStatus.COMPLETED}))
        service.runs.reconcile_completed_voice_job.assert_not_called()
        service.advance.assert_not_called()
        assert JobRepository(db).get(voice.id).attempt==0


def test_queue_completion_replay_does_not_apply_an_unrelated_worker_capability(tmp_path):
    from app.db import JobRepository
    from test_production_run_worker_cli import _fixture,_job as stored_job,_production
    db,project,now=_fixture(tmp_path/'worker-group.sqlite')
    with db:
        voice=stored_job(db,project,now,'voice',JobType.GENERATE_VOICE)
        JobRepository(db).update(voice.model_copy(update={'status':JobStatus.COMPLETED}))
        run_id=_production(db,project,now,[],stage='voice')
        db.connection.execute('UPDATE production_runs SET automatic=1,voice_job_id=? WHERE id=?',(str(voice.id),str(run_id)))
        service=VideoProductionService(db,tmp_path)
        service.completed=Mock(side_effect=AssertionError('unrelated Voice completion must wait for the Voice worker'))
        assert service.queue_jobs({JobType.GENERATE_TALKING,JobType.VERIFY_TALKING})==[]
        assert db.connection.execute('SELECT voice_qa_auto_stop_reasons FROM production_runs WHERE id=?',(str(run_id),)).fetchone()[0]=='[]'


@pytest.mark.parametrize('stage', ['voice', 'talking'])
@pytest.mark.parametrize('change', ['none', 'run', 'qa'])
@pytest.mark.parametrize('failure_kind', ['not_ready', 'conflict', 'missing'])
def test_qa_repair_failure_stops_once_and_does_not_overwrite_newer_state(tmp_path, monkeypatch, stage, change, failure_kind):
    from uuid import uuid4
    from app.db import JobRepository
    from app.domain.models import ContentPlanJobPayload, Job, TalkingQaJobPayload, VoiceQaJobPayload
    from app.production_runs import ProductionRunNotReady, ProductionRunConflict
    from test_production_run_worker_cli import _fixture, _job as stored_job, _production

    db,project,now=_fixture(tmp_path/'repair-error.sqlite')
    with db:
        parent=stored_job(db,project,now,'start',JobType.PLAN_CONTENT)
        parent=JobRepository(db).update(parent.model_copy(update={'status':JobStatus.COMPLETED,
            'payload':ContentPlanJobPayload(project_id=project.id,topic=project.topic)}))
        run_id=_production(db,project,now,[],stage='voice' if stage=='voice' else 'awaiting_talking')
        payload=(VoiceQaJobPayload(project_id=project.id,narration_audio_id=uuid4(),target_text='测试')
            if stage=='voice' else TalkingQaJobPayload(project_id=project.id,run_id=run_id,scene_plan_id=uuid4(),
                generation_job_id=uuid4(),output_asset_id=uuid4(),output_sha256='a'*64,
                master_audio_id=uuid4(),master_sha256='b'*64,master_start_ms=0,master_end_ms=1000))
        kind=JobType.VERIFY_VOICE if stage=='voice' else JobType.VERIFY_TALKING
        qa=JobRepository(db).create(Job(project_id=project.id,type=kind,idempotency_key='qa',created_at=now,
            updated_at=now,status=JobStatus.FAILED,error_code=f'{stage}_qa_failed',payload=payload))
        column='voice_qa_job_id' if stage=='voice' else 'talking_source_bindings'
        binding=str(qa.id) if stage=='voice' else json.dumps([{'talking_qa_job_id':str(qa.id)}])
        db.connection.execute(f'UPDATE production_runs SET automatic=1,start_job_id=?,repair_allowance=1,{column}=? WHERE id=?',
            (str(parent.id),binding,str(run_id)))
        path=('app.voice_repair.VoiceRepairService.reconcile_automatic_qa_failure' if stage=='voice'
            else 'app.talking.repair.TalkingRepairService.reconcile_automatic_qa_failure')
        called=[]
        def fail(*args):
            called.append(1)
            if change=='run':
                db.connection.execute('UPDATE production_runs SET stage=? WHERE id=?',('cancelled',str(run_id)))
            if change=='qa':
                JobRepository(db).update(qa.model_copy(update={'status':JobStatus.COMPLETED,'error_code':None}))
            if failure_kind=='conflict':raise ProductionRunConflict('talking_repair_plan_stale')
            if failure_kind=='missing':raise LookupError('production run not found')
            raise ProductionRunNotReady(('talking_reference_bytes_changed',))
        monkeypatch.setattr(path,fail)
        service=VideoProductionService(db,tmp_path)
        fake_run=Mock(id=run_id,status=f'{stage}_qa_failed')
        fake_run.model_dump.return_value={}
        service.runs.get=Mock(return_value=fake_run)
        handlers={JobType.GENERATE_VOICE,JobType.VERIFY_VOICE,JobType.GENERATE_TALKING,JobType.VERIFY_TALKING}
        assert service.queue_jobs(handlers,run_id=run_id)==[]
        row=db.connection.execute('SELECT * FROM production_runs WHERE id=?',(str(run_id),)).fetchone()
        reasons=json.loads(row['waiting_stop_reasons'])
        if change=='none':
            expected={'not_ready':'talking_reference_bytes_changed','conflict':'talking_repair_plan_stale',
                      'missing':'qa_repair_dependency_missing'}[failure_kind]
            assert reasons==['automatic_stop:'+expected]
            status=service.status(project.id)
            assert status['error_code']==f'{stage}_qa_failed'
            assert status['stop_reasons']==['automatic_stop:'+expected]
            assert JobRepository(db).get(qa.id)==qa
            assert service.queue_jobs(handlers,run_id=run_id)==[]
            assert len(called)==1
        else:
            assert reasons==[]
            if change=='run':assert row['stage']=='cancelled'


@pytest.mark.parametrize('stage', ['voice', 'talking'])
@pytest.mark.parametrize('change', ['none', 'run', 'qa'])
def test_qa_repair_stop_diagnosis_does_not_overwrite_newer_run_or_qa(tmp_path, stage, change):
    from uuid import uuid4
    from app.db import JobRepository
    from app.domain.models import ContentPlanJobPayload
    from app.voice_repair import VoiceRepairService
    from app.talking.repair import TalkingRepairService
    from test_production_run_worker_cli import _fixture, _job as stored_job, _production

    db,project,now=_fixture(tmp_path/'repair-stop.sqlite')
    with db:
        parent=stored_job(db,project,now,'start',JobType.PLAN_CONTENT)
        parent=JobRepository(db).update(parent.model_copy(update={'status':JobStatus.COMPLETED,
            'payload':ContentPlanJobPayload(project_id=project.id,topic=project.topic)}))
        qa=stored_job(db,project,now,'qa',JobType.VERIFY_VOICE if stage=='voice' else JobType.VERIFY_TALKING)
        qa=JobRepository(db).update(qa.model_copy(update={'status':JobStatus.FAILED,'error_code':f'{stage}_qa_failed'}))
        run_id=_production(db,project,now,[],stage='voice' if stage=='voice' else 'awaiting_talking')
        db.connection.execute('UPDATE production_runs SET automatic=1,start_job_id=?,voice_qa_job_id=?,talking_source_bindings=? WHERE id=?',
            (str(parent.id),str(qa.id) if stage=='voice' else None,
             json.dumps([{'talking_qa_job_id':str(qa.id)}] if stage=='talking' else []),str(run_id)))
        repair=VoiceRepairService(db,tmp_path) if stage=='voice' else TalkingRepairService(db,tmp_path)
        if stage=='talking':
            repair.production.get=Mock(return_value=SimpleNamespace(automatic=True,waiting_stop_reasons=[],
                talking_source_bindings=[SimpleNamespace(talking_qa_job_id=qa.id,scene_plan_id=uuid4())]))
        def diagnose(*args):
            if change=='run':
                db.connection.execute('UPDATE production_runs SET stage=? WHERE id=?',('cancelled',str(run_id)))
            if change=='qa':
                JobRepository(db).update(qa.model_copy(update={'status':JobStatus.COMPLETED,'error_code':None}))
            return SimpleNamespace(action='stop',stop_reasons=('exact_repair_diagnosis',))
        repair.plan=diagnose
        assert repair.reconcile_automatic_qa_failure(project.id,run_id) is None
        row=db.connection.execute('SELECT * FROM production_runs WHERE id=?',(str(run_id),)).fetchone()
        target=json.loads(row['voice_qa_auto_stop_reasons' if stage=='voice' else 'waiting_stop_reasons'])
        assert target==((['exact_repair_diagnosis'] if stage=='voice' else ['automatic_stop:exact_repair_diagnosis'])
            if change=='none' else [])
        if change=='none':
            service=VideoProductionService(db,tmp_path)
            fake_run=Mock(id=run_id,status=f'{stage}_qa_failed')
            fake_run.model_dump.return_value={}
            service.runs.get=Mock(return_value=fake_run)
            status=service.status(project.id)
            assert status['error_code']==f'{stage}_qa_failed'
            assert status['stop_reasons']==target
        if change=='run':assert row['stage']=='cancelled'


@pytest.mark.parametrize('entry', ['queue', 'recover'])
@pytest.mark.parametrize('parent_status,automatic,stopped,should_dispatch', [
    (JobStatus.COMPLETED, True, False, True),
    (JobStatus.RUNNING, True, False, False),
    (JobStatus.FAILED, True, False, False),
    (JobStatus.CANCELLED, True, False, False),
    (JobStatus.COMPLETED, False, False, False),
    (JobStatus.COMPLETED, True, True, False),
])
def test_restart_recovers_only_completed_automatic_plan_before_voice(tmp_path, parent_status, automatic, stopped, should_dispatch, entry):
    from uuid import UUID
    from datetime import datetime, timezone
    from fastapi.testclient import TestClient
    from app.db import Database, JobRepository
    from app.domain.models import ContentPlanJobPayload, Job, VisualStyleTokens
    from app.main import create_app
    from test_production_runs import _ready_fixture, _voice_dispatch_evidence

    path = tmp_path / 'restart.sqlite'
    with TestClient(create_app(path)) as client:
        project_id, _, _ = _ready_fixture(client, path, with_master=False)
        _voice_dispatch_evidence(path)
        preview = client.post(f'/projects/{project_id}/production-preflight', json={}).json()
        with Database(path) as db:
            now = datetime.now(timezone.utc)
            parent = JobRepository(db).create(Job(project_id=UUID(project_id), type=JobType.PLAN_CONTENT,
                status=parent_status, idempotency_key='plan', created_at=now, updated_at=now,
                payload=ContentPlanJobPayload(project_id=UUID(project_id), topic='New')))
            service = VideoProductionService(db, tmp_path)
            run = service.runs.start(UUID(project_id), idempotency_key='run', expected_fingerprint=preview['fingerprint'],
                style_tokens=VisualStyleTokens(), automatic=automatic, start_job_id=parent.id)
            if stopped:
                db.connection.execute('UPDATE production_runs SET waiting_stop_reasons=? WHERE id=?',
                    (json.dumps(['automatic_stop:previous_failure']), str(run.id)))
            handlers = {JobType.GENERATE_VOICE, JobType.VERIFY_VOICE}
            if entry == 'recover':
                service.recover_completed(handlers)
                assert len([job for job in JobRepository(db).list() if job.type is JobType.GENERATE_VOICE]) == int(should_dispatch)
            if should_dispatch and entry == 'queue':
                from concurrent.futures import ThreadPoolExecutor
                from threading import Barrier
                gate = Barrier(2)
                def concurrent_poll():
                    with Database(path, startup_mode='existing') as independent:
                        gate.wait(timeout=10)
                        return VideoProductionService(independent, tmp_path).queue_jobs(handlers, run_id=run.id)
                with ThreadPoolExecutor(max_workers=2) as workers:
                    futures = [workers.submit(concurrent_poll) for _ in range(2)]
                    concurrent_results = [future.result(timeout=15) for future in futures]
                assert all(len(jobs) == 1 for jobs in concurrent_results)
                assert concurrent_results[0][0].id == concurrent_results[1][0].id
            first = service.queue_jobs(handlers, run_id=run.id)
            second = service.queue_jobs(handlers, run_id=run.id)
            voices = [job for job in JobRepository(db).list() if job.type is JobType.GENERATE_VOICE]
            assert len(voices) == (1 if should_dispatch else 0)
            if should_dispatch:
                assert [job.id for job in first] == [voices[0].id]
                assert [job.id for job in second] == [voices[0].id]
            else:
                assert first == second == []


def test_restart_transition_failure_stops_once_instead_of_looping(tmp_path):
    from app.db import JobRepository
    from app.domain.models import ContentPlanJobPayload
    from test_production_run_worker_cli import _fixture, _job as stored_job, _production
    db, project, now = _fixture(tmp_path / 'restart-failure.sqlite')
    with db:
        parent = stored_job(db, project, now, 'start-failure', JobType.PLAN_CONTENT)
        JobRepository(db).update(parent.model_copy(update={
            'payload': ContentPlanJobPayload(project_id=project.id, topic=project.topic),
            'status': JobStatus.COMPLETED}))
        run_id = _production(db, project, now, [], stage='awaiting_master')
        db.connection.execute('UPDATE production_runs SET automatic=1,start_job_id=? WHERE id=?',
                              (str(parent.id), str(run_id)))
        service = VideoProductionService(db, tmp_path)
        service.advance = Mock(side_effect=RuntimeError('inconsistent state'))
        handlers = {JobType.GENERATE_VOICE, JobType.VERIFY_VOICE}
        assert service.queue_jobs(handlers, run_id=run_id) == []
        assert service.queue_jobs(handlers, run_id=run_id) == []
        service.recover_completed(handlers)
        service.advance.assert_called_once()
        assert service.decision_for_run(run_id, handlers)['stop_reasons'] == ['automatic_stop:automatic_production_stopped']
