"""配时交接的真实隔离事务；媒体、用途核验和准入结论是替身。"""
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.db import AudioAssetRepository, JobRepository, ProjectDraftRepository, ProviderCallRepository
from app.domain.models import AudioAsset, ContentPlanJobPayload, JobStatus, JobType, ProjectDraft, ScenePlan, TranscriptSegment, VisualIntent
from app.production_runs import ProductionRunNotReady
from app.project_drafts import save_project_draft
from app.video_production import VideoProductionService
from test_production_run_worker_cli import _fixture, _job, _production


@pytest.fixture
def handoff(tmp_path,monkeypatch):
    db,project,now=_fixture(tmp_path/'handoff.sqlite')
    db.data_root=tmp_path
    parent=JobRepository(db).update(_job(db,project,now,'input',JobType.PLAN_CONTENT).model_copy(update={
        'status':JobStatus.COMPLETED,'payload':ContentPlanJobPayload(project_id=project.id,topic=project.topic)}))
    voice=JobRepository(db).update(_job(db,project,now,'voice',JobType.GENERATE_VOICE).model_copy(update={'status':JobStatus.COMPLETED}))
    qa=JobRepository(db).update(_job(db,project,now,'qa',JobType.VERIFY_VOICE).model_copy(update={'status':JobStatus.COMPLETED}))
    scene=ScenePlan(project_id=project.id,scene_id='explain',order=0,purpose='完整解释',voice_text='保留完整旁白。',
        duration_target_ms=1000,visual_intent=VisualIntent(subject='相关实拍'),preferred_sources=['user_asset'],
        visual_requirement='explanatory',visual_requirement_reason='相关录像承接讲述')
    draft=ProjectDraftRepository(db).save(ProjectDraft(project_id=project.id,version=1,script_revision=1,
        script=scene.voice_text,topic=project.topic,scenes=[scene],updated_at=now))
    path=tmp_path/'master.wav';path.write_bytes(b'fixture existing narration, not a quality verdict')
    master=AudioAssetRepository(db).create(AudioAsset(source_file=str(path),content_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
        duration_ms=1000,sample_rate=16000,channels=1,authorization_reference='intake',imported_at=now,
        transcript_source='fixture independent copy',transcript_segments=[TranscriptSegment(start_ms=0,end_ms=1000,text=scene.voice_text)],
        metadata={'voice_generation':{'qa_state':'verified'}}))
    run=_production(db,project,now,[],stage='voice')
    db.connection.execute('UPDATE production_runs SET automatic=1,start_job_id=?,voice_job_id=?,voice_qa_job_id=?,voice_audio_id=? WHERE id=?',
        (str(parent.id),str(voice.id),str(qa.id),str(master.id),str(run)))
    service=VideoProductionService(db,tmp_path,material_resolver=object())
    service.runs._require_bound_voice_master=Mock()
    service.runs.reconcile_completed_voice_job=Mock()
    monkeypatch.setattr('app.video_production.build_production_preflight',lambda *a,**kw:SimpleNamespace(scenes=[]))
    value=SimpleNamespace(db=db,project=project,now=now,draft=draft,scene=scene,master=master,path=path,qa=qa,run=run,service=service)
    yield value
    db.close()


@pytest.mark.parametrize('change',['draft','cancel','replacement','master'])
def test_resolution_cannot_overwrite_new_inputs_or_poison_a_changed_run(handoff,change):
    c=handoff;replacement=[]
    def resolve(project,scenes,*args,**kwargs):
        if change=='draft':
            edited=c.scene.model_copy(update={'voice_text':'这是用户刚修改的稿件。'})
            save_project_draft(c.db,c.project,script=edited.voice_text,topic=c.draft.topic,scenes=[edited],
                routes=(),confirmed=(),video_spec=None,accept_scene_plan_with_new_script=True)
        elif change=='cancel':c.db.connection.execute("UPDATE production_runs SET stage='cancelled' WHERE id=?",(str(c.run),))
        elif change=='replacement':
            newer=_job(c.db,c.project,c.now,'new-voice',JobType.GENERATE_VOICE)
            replacement.append(newer)
            c.db.connection.execute('UPDATE production_runs SET voice_job_id=?,voice_audio_id=NULL,voice_qa_job_id=NULL WHERE id=?',(str(newer.id),str(c.run)))
        else:
            AudioAssetRepository(c.db).update(c.master.model_copy(update={'metadata':{'voice_generation':{'qa_state':'failed'}}}))
        return tuple(scenes)
    c.service._resolve_with_supplement=Mock(side_effect=resolve)
    c.service.runs.resume=Mock(side_effect=AssertionError('stale results must not reach downstream resume'))
    before_calls=ProviderCallRepository(c.db).list()
    c.service.completed(c.qa)
    fresh=ProjectDraftRepository(c.db).get(c.project.id)
    row=c.db.connection.execute('SELECT * FROM production_runs WHERE id=?',(str(c.run),)).fetchone()
    if change=='draft':
        assert fresh.script=='这是用户刚修改的稿件。' and fresh.version==2
        assert row['draft_version']==1 and row['script_revision']==1
        assert json.loads(row['waiting_stop_reasons'])==['automatic_stop:automatic_draft_changed']
    elif change=='cancel':
        assert fresh==c.draft and row['stage']=='cancelled' and json.loads(row['waiting_stop_reasons'])==[]
    elif change=='replacement':
        assert fresh==c.draft and row['voice_job_id']==str(replacement[0].id)
        assert json.loads(row['waiting_stop_reasons'])==[]
        assert c.service.status(c.project.id)['next_action']=='continue'
    else:
        assert fresh==c.draft and json.loads(row['waiting_stop_reasons'])==['automatic_stop:automatic_master_changed']
    c.service.runs.resume.assert_not_called();c.service.runs._require_bound_voice_master.assert_not_called()
    assert c.path.read_bytes()==b'fixture existing narration, not a quality verdict'
    assert ProviderCallRepository(c.db).list()==before_calls
    assert all(job.type is not JobType.GENERATE_TALKING and job.type is not JobType.RENDER for job in JobRepository(c.db).list())


def test_post_commit_resume_failure_is_recorded_against_its_new_snapshot_without_repeating_materials(handoff):
    c=handoff
    c.service._resolve_with_supplement=Mock(side_effect=lambda project,scenes,*a,**kw:tuple(scenes))
    c.service.runs.resume=Mock(side_effect=ProductionRunNotReady(('master_scene_timing_unresolved',)))
    c.service.completed(c.qa)
    row=c.db.connection.execute('SELECT * FROM production_runs WHERE id=?',(str(c.run),)).fetchone()
    fresh=ProjectDraftRepository(c.db).get(c.project.id)
    assert fresh.version==row['draft_version']==2 and fresh.script==c.draft.script
    assert json.loads(row['waiting_stop_reasons'])==['automatic_stop:master_scene_timing_unresolved']
    assert c.service.status(c.project.id)['next_action']=='stopped'
    assert c.service.status(c.project.id)['responsible_stage']=='timing'
    c.service.completed(c.qa)
    c.service._resolve_with_supplement.assert_called_once()
    assert AudioAssetRepository(c.db).get(c.master.id)==c.master


def test_talking_dispatch_failure_after_resume_stops_the_new_phase_once(handoff):
    c=handoff
    c.service._resolve_with_supplement=Mock(side_effect=lambda project,scenes,*a,**kw:tuple(scenes))
    def resumed(*args):
        dependency={'scene_plan_id':str(c.scene.id),'scene_id':c.scene.scene_id,'visual_dependency_fingerprint':'e'*64}
        c.db.connection.execute("UPDATE production_runs SET stage='awaiting_talking',talking_master_audio_id=?,talking_dependencies=? WHERE id=?",
            (str(c.master.id),json.dumps([dependency]),str(c.run)))
        return c.service.runs.get(c.project.id,c.run)
    c.service.runs.resume=Mock(side_effect=resumed)
    c.service.runs.dispatch_all_talking=Mock(side_effect=ProductionRunNotReady(('creator_talking_reference_not_configured',)))
    c.service.completed(c.qa)
    row=c.db.connection.execute('SELECT * FROM production_runs WHERE id=?',(str(c.run),)).fetchone()
    assert row['stage']=='awaiting_talking' and row['draft_version']==2
    assert json.loads(row['waiting_stop_reasons'])==['automatic_stop:creator_talking_reference_not_configured']
    assert c.service.status(c.project.id)['responsible_stage']=='material'
    c.service.completed(c.qa)
    c.service.runs.dispatch_all_talking.assert_called_once()
    assert c.service._resolve_with_supplement.call_count==1
