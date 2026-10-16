"""Retained-only production: synthetic local media/consent; no model or render."""
import hashlib
import json
from datetime import datetime, timezone, timedelta
from uuid import UUID, uuid4

import pytest

from app.db import (AssetRepository, AudioAssetRepository, ClipRepository, Database, JobRepository,
    ProjectDraftRepository, ProjectRepository, TalkingProfileRepository, VoiceProfileRepository)
from app.domain.models import (AudioAsset, Clip, ConsentRecord, Job, JobStatus, JobType, ProjectDraft,
    SourceKind, TalkingGenerationJobPayload, TalkingProfile, TranscriptSegment, VoiceHumanReview, VoiceProfile, VisualStyleTokens)
from app.domain.evaluation_composition import CompositionSelection
from app.evaluation_composition import EvaluationCompositionService
from app.execution_admission import UseAdmissionError
from app.execution_scope import ExecutionScopeService, require_evaluation_lane_closed
from app.production_preflight import build_production_preflight
from app.production_runs import ProductionRunService
from test_video_spec_assembly import _asset, _scene


@pytest.fixture
def legacy(tmp_path, admit_talking_run, monkeypatch):
    now=datetime.now(timezone.utc);db=Database(tmp_path/'legacy.sqlite3');db.data_root=tmp_path
    pid=uuid4();source=tmp_path/'synthetic.media';source.write_bytes(b'SYNTHETIC not a real approved video')
    asset=_asset(1,source_kind='ai_video').model_copy(update=dict(source_file=str(source),duration_ms=5020,
        content_hash=hashlib.sha256(source.read_bytes()).hexdigest(),authorization_reference='synthetic-consent'))
    AssetRepository(db).create(asset)
    clip=ClipRepository(db).create(Clip(asset_id=asset.id,start_ms=0,end_ms=5020,asset_duration_ms=5020))
    consent=ConsentRecord(subject_name='Synthetic fixture',basis='self',confirmed=True,confirmed_at=now,authorization_reference='synthetic-consent')
    voice=VoiceProfile(name='Synthetic',provider='fixture',provider_profile_id='test',reference_clip_ids=[clip.id],consent=consent,created_at=now)
    talking=TalkingProfile(name='Synthetic',provider='fixture',reference_clip_ids=[clip.id],consent=consent,created_at=now)
    VoiceProfileRepository(db).create(voice);TalkingProfileRepository(db).create(talking)
    asset=AssetRepository(db).update(asset.model_copy(update={"metadata":{"talking_generation":{
        "talking_profile_id":str(talking.id),"provider":"fixture","qa_state":"verified","human_review_state":"approved"}}}))
    review=VoiceHumanReview(approved=True,evidence_reference='fixture-only',findings=['Synthetic approval, no real quality claim'],
        likeness='pass',naturalness='pass',emphasis='pass',pace='pass',pauses='pass',rhythm='pass',reviewed_at=now)
    audiofile=tmp_path/'synthetic.wav';audiofile.write_bytes(b'SYNTHETIC not real audio')
    audio=AudioAsset(source_file=str(audiofile),content_hash=hashlib.sha256(audiofile.read_bytes()).hexdigest(),
        duration_ms=5300,sample_rate=24000,channels=1,authorization_reference='synthetic-consent',imported_at=now,
        transcript_source='synthetic',transcript_segments=[TranscriptSegment(start_ms=i*1000,end_ms=(i+1)*1000,text=f'word{i}') for i in range(5)],
        metadata={'voice_generation':dict(project_id=str(pid),provider='fixture',model='fixture',voice_profile_id=str(voice.id),
            qa_state='verified',human_review_state='approved',human_review=review.model_dump(mode='json'))})
    AudioAssetRepository(db).create(audio)
    copy=' '.join(s.text for s in audio.transcript_segments)
    asset,run,_=admit_talking_run(db,pid,asset,clip,copy,audio=audio)
    run=run.model_copy(update={"authorized_reference_clip_id":clip.id})
    db.connection.execute("UPDATE talking_runs SET payload=? WHERE id=?",(run.model_dump_json(),str(run.id)))
    # Failed/old execution status is deliberately not present consumption QA.
    JobRepository(db).create(Job(id=run.child_evidence[0].job_id,project_id=pid,type=JobType.GENERATE_TALKING,
        status=JobStatus.FAILED,idempotency_key='historical',created_at=now,updated_at=now,
        payload=TalkingGenerationJobPayload(project_id=pid,talking_profile_id=talking.id,reference_clip_id=clip.id,
            narration_audio_id=audio.id,authorization_reference='synthetic-consent')))
    project=ProjectRepository(db).get(pid)
    scenes=[_scene(project,i,1000).model_copy(update=dict(voice_text=f'word{i}',preferred_sources=[SourceKind.AI_VIDEO],
        visual_requirement='creator_speaking',visual_requirement_reason='Synthetic explicit speaking requirement')) for i in range(5)]
    draft=ProjectDraft(project_id=pid,version=1,script_revision=1,script=copy,scenes=scenes,updated_at=now)
    ProjectDraftRepository(db).save(draft)
    key='synthetic-operator-key-000000000000000000000000000000'
    monkeypatch.setenv('CONTENT_OS_EXECUTION_ADMISSION_KEY_HASHES',json.dumps({'fixture':hashlib.sha256(key.encode()).hexdigest()}))
    yield db,pid,asset,audio,talking,draft,key
    db.close()


def use_and_batch(service,pid,context,key):
    root=service.root;terms=root/'synthetic-terms.txt';terms.write_text('Synthetic fixture permission, never runtime license evidence')
    evidence=dict(source='synthetic',version='fixture',terms_reference=terms.name,terms_sha256=hashlib.sha256(terms.read_bytes()).hexdigest(),
        applicability='synthetic exact test use',permits_intended_activity=True)
    def save(name,value):
        file=root/name;file.write_text(json.dumps(value),encoding='utf-8')
        from app.domain.evaluation_master import MasterAdoption
        return MasterAdoption(review_reference=name,review_sha256=hashlib.sha256(file.read_bytes()).hexdigest(),idempotency_key=name)
    report=dict(policy='evaluation-composition-v1',purpose='internal_evaluation',operation='compose_retained_media_and_review',local_only=True,
        project_id=str(pid),selection=context['selection'].model_dump(mode='json'),request_sha256=context['request_sha256'],proof=context['proof'],
        intended_activity='Synthetic fixture only',license_evidence=[],findings=['No real rights or quality claims'],
        dependency_findings=[dict(evidence,dependency=d) for d in ['retained_media_composition','local_renderer_and_tools',*context['proof']['dependencies']]])
    use=service.adopt(pid,save('use.json',report),key)
    service.adopt_batch(pid,save('batch.json',dict(policy='evaluation-composition-v1',project_id=str(pid),master_audio_id=str(context['audio'].id),
        authorization_reference=terms.name,authorization_sha256=evidence['terms_sha256'],scene_ids=[],max_renders=1,findings=['Synthetic one-render grant'])),key)
    return use


def test_ordinary_retained_preflight_start_no_generation_history_or_new_calls(legacy):
    db,pid,asset,audio,_,draft,key=legacy;service=EvaluationCompositionService(db,db.data_root);style=VisualStyleTokens()
    # An obsolete generation payload cannot become a media consumption dependency.
    db.connection.execute("UPDATE jobs SET payload=json_set(payload,'$.payload.planned_context',json('{\"obsolete\":true}'))")
    selection=service.retained_selection(pid,style);context=service._context(pid,selection)
    assert selection.master_run_id is None and selection.master_audio_id==audio.id
    assert len(selection.talking_runs)==5 and len(set(selection.talking_runs.values()))==1
    spec=context['spec'];assert len(spec.scenes)==6
    assert [(s.visual.clip_start_ms,s.visual.clip_end_ms) for s in spec.scenes[:5]]==[(i*1000,(i+1)*1000) for i in range(5)]
    assert spec.master_narration.end_ms==5300 and spec.scenes[-1].narration_start_ms==5000
    assert not spec.scenes[-1].caption and not spec.scenes[-1].captions and spec.scenes[-1].visual.asset_id is None
    plan=build_production_preflight(db,ProjectRepository(db).get(pid),draft,style_tokens=style,purpose='internal_evaluation',data_root=db.data_root)
    assert plan.stop_reasons==('composition_use_admission_required',)
    assert not any(a.required and a.kind in {'voice','talking','analysis'} for a in plan.actions)
    with pytest.raises(UseAdmissionError):service.current_use(pid,context)
    use=use_and_batch(service,pid,context,key)
    plan=build_production_preflight(db,ProjectRepository(db).get(pid),draft,style_tokens=style,purpose='internal_evaluation',data_root=db.data_root)
    assert plan.status=='ready_for_authorized_dispatch'
    runs=ProductionRunService(db,db.data_root)
    run=runs.start(pid,idempotency_key='normal',expected_fingerprint=plan.fingerprint,style_tokens=style,purpose='internal_evaluation')
    assert runs.start(pid,idempotency_key='different-key',expected_fingerprint=plan.fingerprint,style_tokens=style,purpose='internal_evaluation').id==run.id
    job=JobRepository(db).get(run.render_job_id)
    assert job.type==JobType.RENDER and job.payload.video_spec==spec
    assert run.talking_review_policy_version==1  # No policy2 history is manufactured.
    assert db.connection.execute('SELECT count(*) FROM jobs').fetchone()[0]==2
    assert db.connection.execute('SELECT count(*) FROM provider_call_records').fetchone()[0]==0
    with pytest.raises(UseAdmissionError):require_evaluation_lane_closed(db,job)
    with pytest.raises(UseAdmissionError):ExecutionScopeService(db,db.data_root).prepare(pid,purpose='internal_evaluation')
    service.revoke(pid,use.id,'Synthetic permission withdrawn',key)
    with pytest.raises(UseAdmissionError):service.require_current_job(job)


@pytest.mark.parametrize('change',['hash','copy','consent','project','review'])
def test_current_media_safety_fails_without_fallback_generation(legacy,change):
    db,pid,asset,audio,talking,draft,_=legacy;service=EvaluationCompositionService(db,db.data_root)
    selection=service.retained_selection(pid,VisualStyleTokens())
    if change=='hash':
        from pathlib import Path
        Path(asset.source_file).write_bytes(b'changed')
    elif change=='copy':
        db.connection.execute("UPDATE project_drafts SET payload=json_set(payload,'$.script','changed') WHERE project_id=?",(str(pid),))
    elif change=='consent':
        db.connection.execute("UPDATE talking_profiles SET payload=json_set(payload,'$.consent.confirmed',json('false')) WHERE id=?",(str(talking.id),))
    elif change=='project':
        pid=uuid4()
    else:
        db.connection.execute("UPDATE talking_slice_series_continuity_reviews SET payload=json_set(payload,'$.approved',json('false'))")
    with pytest.raises((ValueError,OSError)):service._context(pid,selection)
    assert db.connection.execute('SELECT count(*) FROM jobs').fetchone()[0]==1
    assert db.connection.execute('SELECT count(*) FROM provider_call_records').fetchone()[0]==0


@pytest.mark.parametrize('outcome',['completed','renderer_failure','generic_handler'])
def test_one_normal_worker_preserves_private_result_accounting_and_once_only(legacy,monkeypatch,outcome):
    """Synthetic subprocess/QA only: no media render or provider call."""
    db,pid,_,_,_,_,key=legacy;service=EvaluationCompositionService(db,db.data_root);style=VisualStyleTokens()
    context=service._context(pid,service.retained_selection(pid,style));use=use_and_batch(service,pid,context,key)
    runs=ProductionRunService(db,db.data_root)
    def start(key):
        return runs.start(pid,idempotency_key=key,expected_fingerprint=context['request_sha256'],style_tokens=style,purpose='internal_evaluation')
    run=start('worker');job=JobRepository(db).get(run.render_job_id)
    from app.renderer import RemotionRenderer
    from app.jobs.handlers import RenderVideoJobHandler
    from app.jobs.runner import JobRunner
    from app.jobs.store import JobStore
    from app.db import ProviderCallRepository
    from test_remotion_renderer import _Runner,_renderer_project
    from app import presentation_repair
    fake=_Runner(returncode=1 if outcome=='renderer_failure' else 0)
    renderer=RemotionRenderer(AssetRepository(db),ClipRepository(db),audios=AudioAssetRepository(db),
        renderer_dir=_renderer_project(db.data_root/'renderer'),runner=fake)
    handler=RenderVideoJobHandler(ProjectRepository(db),renderer,db.data_root/'renders')
    monkeypatch.setattr(presentation_repair,'measure_render_output',lambda path,spec:dict(
        state='verified',sha256=hashlib.sha256(path.read_bytes()).hexdigest(),scope='synthetic_technical_only'))
    selected=handler if outcome!='generic_handler' else lambda _:pytest.fail('untrusted callback consumed restricted media')
    runner=JobRunner(JobStore(db),{JobType.RENDER:selected},worker_id='normal-render',lease_duration=timedelta(minutes=5),max_attempts=1)
    finished=runner.run_once(job_id=job.id)
    assert finished.status==(JobStatus.COMPLETED if outcome=='completed' else JobStatus.FAILED)
    assert runner.run_once(job_id=job.id) is None
    assert start('another-key').id==run.id
    calls=ProviderCallRepository(db).list_all()
    assert len(calls)==(0 if outcome=='generic_handler' else 1)
    if calls:assert calls[0].status==('completed' if outcome=='completed' else 'failed')
    assert db.connection.execute('SELECT count(*) FROM jobs').fetchone()[0]==2
    if outcome!='completed':
        assert runs.get(pid,run.id).status=='render_failed'
        with pytest.raises(UseAdmissionError):service.media(pid,run.id,'internal_evaluation')
        return
    path=service.media(pid,run.id,'internal_evaluation')
    assert path.read_bytes()==b'minimal-muxed-mp4'
    with pytest.raises(UseAdmissionError):service.media(uuid4(),run.id,'internal_evaluation')
    with pytest.raises(UseAdmissionError):service.media(pid,run.id,'commercial_production')
    from app.media.audio_access import require_ordinary_media
    with pytest.raises(UseAdmissionError):require_ordinary_media(db,db.data_root,path,kind='render',subject_id=job.payload.render_id)
    JobRepository(db).update(finished.model_copy(update=dict(status=JobStatus.FAILED)))
    assert runs.get(pid,run.id).status=='render_failed'
    assert runs.resume(pid,run.id).status=='render_completed_awaiting_review'
    assert len(fake.calls)==2
    service.revoke(pid,use.id,'Fixture revoked',key)
    assert runs.get(pid,run.id).status=='render_failed'
    with pytest.raises(UseAdmissionError):service.media(pid,run.id,'internal_evaluation')


def test_retained_budget_and_immutable_selection_reject_before_job_creation(legacy):
    from app.db import BudgetPolicyRepository
    from app.production_runs import ProductionRunNotReady
    db,pid,_,_,_,_,key=legacy;service=EvaluationCompositionService(db,db.data_root);style=VisualStyleTokens()
    context=service._context(pid,service.retained_selection(pid,style));use_and_batch(service,pid,context,key)
    budgets=BudgetPolicyRepository(db)
    from app.domain.models import BudgetPolicy
    budgets.save(BudgetPolicy(project_id=pid,max_calls=0,updated_at=datetime.now(timezone.utc)))
    with pytest.raises(ProductionRunNotReady):
        ProductionRunService(db,db.data_root).start(pid,idempotency_key='blocked',expected_fingerprint=context['request_sha256'],style_tokens=style,purpose='internal_evaluation')
    assert db.connection.execute('SELECT count(*) FROM jobs').fetchone()[0]==1


def test_only_normal_run_and_media_routes_remain(legacy,monkeypatch):
    import asyncio,httpx
    from app.main import create_app
    db,pid,_,_,_,_,key=legacy;service=EvaluationCompositionService(db,db.data_root);style=VisualStyleTokens()
    context=service._context(pid,service.retained_selection(pid,style));use_and_batch(service,pid,context,key)
    monkeypatch.setenv('CONTENT_OS_DATA_ROOT',str(db.data_root))
    app=create_app(db.path);app.state.database=db
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture') as api:
            base=f'/projects/{pid}'
            plan=await api.post(base+'/production-preflight',json={'purpose':'internal_evaluation'})
            assert plan.status_code==200
            response=await api.post(base+'/production-runs',json=dict(purpose='internal_evaluation',idempotency_key='normal-api',expected_fingerprint=plan.json()['fingerprint']))
            assert response.status_code==201,response.text
            run=response.json();assert (await api.get(base+'/production-runs/'+run['id'])).status_code==200
            resumed=await api.post(base+'/production-runs/'+run['id']+'/resume')
            assert resumed.status_code==200 and resumed.json()['render_job_id']==run['render_job_id']
            for operation in ['voice-dispatch','talking-dispatch','presentation-repair']:
                assert (await api.post(base+'/production-runs/'+run['id']+'/'+operation,json={})).status_code==409
            history=await api.get(base+'/evaluation-composition/runs')
            assert history.status_code==200 and [r['id'] for r in history.json()]==[run['id']]
            for method,path in [('GET','selections'),('GET','runs/'+run['id']),('POST','runs/'+run['id']+'/reconcile'),('GET','runs/'+run['id']+'/media')]:
                assert (await api.request(method,base+'/evaluation-composition/'+path)).status_code==404
            assert (await api.get(base+'/renders/'+run['evaluation_composition']['render_id']+'?purpose=internal_evaluation')).status_code==403
            cancelled=await api.post(base+'/production-runs/'+run['id']+'/cancel')
            assert cancelled.status_code==200 and cancelled.json()['status']=='cancelled'
            assert JobRepository(db).get(UUID(run['render_job_id'])).status==JobStatus.CANCELLED
    asyncio.run(check())


@pytest.mark.parametrize('tamper',['binding','scope','slot','owner','terms'])
def test_normal_job_keeps_exact_identity_and_current_license_checks(legacy,tamper):
    db,pid,_,_,_,_,key=legacy;service=EvaluationCompositionService(db,db.data_root);style=VisualStyleTokens()
    c=service._context(pid,service.retained_selection(pid,style));use_and_batch(service,pid,c,key)
    run=ProductionRunService(db,db.data_root).start(pid,idempotency_key='identity',expected_fingerprint=c['request_sha256'],style_tokens=style,purpose='internal_evaluation')
    job=JobRepository(db).get(run.render_job_id)
    if tamper=='binding':
        binding=service.records.get('binding',run.id);binding['proof']['audio_sha256']='f'*64
        service.records.save('binding',run.id,binding,update=True)
    elif tamper=='scope':
        db.connection.execute("DELETE FROM execution_use_scopes WHERE subject_kind='render'")
    elif tamper=='slot':
        db.connection.execute("UPDATE evaluation_composition_slots SET operation_id=? WHERE slot='render'",(str(uuid4()),))
    elif tamper=='owner':
        db.connection.execute('DELETE FROM evaluation_composition_owners')
    else:
        (db.data_root/'synthetic-terms.txt').write_text('Changed after adoption')
    with pytest.raises(UseAdmissionError):service.require_current_job(job)
    assert db.connection.execute('SELECT count(*) FROM provider_call_records').fetchone()[0]==0


@pytest.mark.parametrize("fps",[(30,1),(30000,1001)])
def test_absolute_timing_full_master_and_neutral_tail(fps,tmp_path):
    from app.assembly.evaluation_composition import assemble_retained_composition
    from app.domain.models import AudioAsset, RationalFps, VisualStyleTokens, TranscriptSegment, Clip
    from test_video_spec_assembly import _project,_scene,_asset
    from app.renderer.remotion import RemotionRenderer
    from app.db import Database, AssetRepository, ClipRepository
    from test_remotion_renderer import _renderer_project
    project=_project(RationalFps(numerator=fps[0],denominator=fps[1]))
    scenes=[_scene(project,i) for i in range(2)]
    audio=AudioAsset(source_file="synthetic.wav",content_hash="a"*64,duration_ms=2300,sample_rate=24000,channels=1,
        authorization_reference="synthetic",imported_at=project.created_at,transcript_source="synthetic-timing",
        transcript_segments=[TranscriptSegment(start_ms=0,end_ms=1001,text=scenes[0].voice_text),TranscriptSegment(start_ms=1001,end_ms=2100,text=scenes[1].voice_text)])
    inputs={s.id:dict(asset=_asset(i+1,source_kind="ai_video"),clip=Clip(asset_id=uuid4(),start_ms=0,end_ms=3000,asset_duration_ms=3000)) for i,s in enumerate(scenes)}
    spec=assemble_retained_composition(project,audio,[(scenes[0],0,1001),(scenes[1],1001,2100)],inputs,VisualStyleTokens())
    assert spec.master_narration.end_ms==2300
    assert sum(s.duration_frames for s in spec.scenes)==(2300*fps[0]+1000*fps[1]-1)//(1000*fps[1])
    tail=spec.scenes[-1]
    assert (tail.narration_start_ms,tail.narration_end_ms)==(2100,2300)
    assert not tail.caption and not tail.captions and tail.subtitle_treatment.value=="none"
    with Database(tmp_path/"tail.sqlite3") as db:
        renderer=RemotionRenderer(AssetRepository(db),ClipRepository(db),renderer_dir=_renderer_project(tmp_path/"renderer"))
        prepared=renderer._validate_scene(tail,spec.fps,project_id=project.id,master_audio_id=audio.id,validate_scene_narration=False)
        props=renderer._stage_props(spec.model_copy(update={"scenes":[tail]}),{tail.scene_id:prepared},tmp_path,"tail",None)
        assert props["sceneSources"][tail.scene_id]["text"]==""
        assert props["sceneSources"][tail.scene_id]["graphicTreatment"]=="none"


def test_db_fence_tolerates_only_heartbeat_renewal(tmp_path):
    from datetime import datetime,timezone
    from app.db import Database
    from app.domain.models import Job,JobType
    from app.jobs.store import JobStore
    with Database(tmp_path/"fence.sqlite3") as db:
        service=EvaluationCompositionService(db,tmp_path)
        now=datetime.now(timezone.utc)
        job=Job(type=JobType.RENDER,created_at=now,updated_at=now,idempotency_key="fence")
        JobRepository(db).create(job)
        store=JobStore(db)
        claimed=store.claim("fixture",timedelta(minutes=5),max_attempts=1,job_id=job.id)
        before=service._token()
        assert store.heartbeat(claimed.id,"fixture",timedelta(minutes=5))
        assert service._token()==before
        db.connection.execute("UPDATE jobs SET lease_owner='other' WHERE id=?",(str(job.id),))
        assert service._token()!=before


def test_worker_uses_one_render_handler_independent_of_legacy_policy(tmp_path,monkeypatch):
    from app.db import Database
    from app.worker_cli import parse_config,build_runner
    from app.domain.models import JobType
    from app.jobs.handlers import RenderVideoJobHandler
    cfg=parse_config(['--db',str(tmp_path/'worker.sqlite3'),'--data-root',str(tmp_path),'--once','--job-type','render','--max-attempts','1'])
    monkeypatch.setenv('CONTENT_OS_RENDER_EXECUTION_POLICY','evaluation-composition-v1')
    with Database(cfg.db_path) as db:
        runner=build_runner(cfg,db)
        assert type(runner._handlers[JobType.RENDER]) is RenderVideoJobHandler
        monkeypatch.setenv('CONTENT_OS_RENDER_EXECUTION_POLICY','unknown')
        assert type(build_runner(cfg,db)._handlers[JobType.RENDER]) is RenderVideoJobHandler
