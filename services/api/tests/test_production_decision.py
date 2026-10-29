from types import SimpleNamespace
from unittest.mock import Mock
import json

import pytest

from app.domain.models import JobStatus, JobType
from app.video_production import VideoProductionService


def _row(**changes):
    row = dict(waiting_stop_reasons='[]', voice_qa_auto_stop_reasons='[]',
               start_job_id=None, stage='voice', automatic=1, repair_allowance=1)
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
