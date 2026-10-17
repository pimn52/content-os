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
    from app.db import BudgetPolicyRepository
    from app.domain.models import BudgetPolicy
    BudgetPolicyRepository(db).save(BudgetPolicy(project_id=pid,currency='CNY',allow_unknown_cost=True,max_calls=1,updated_at=now))
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
        intended_activity='Synthetic fixture only',
        license_evidence=[dict(evidence,admission_id=str(b.admission_id)) for b in context['scope'].bindings],
        findings=['No real rights or quality claims'],
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


@pytest.mark.parametrize("footage", [False, True])
def test_new_narration_consumes_graphics_and_authorized_footage_in_same_worker(legacy,footage):
    from app.domain.models import GraphicCardPlan, VisualIntent
    db,pid,_,audio,_,draft,key=legacy
    scenes=[s.model_copy(update=dict(preferred_sources=[SourceKind.TYPOGRAPHY],fallback_sources=[],
        visual_requirement="explanatory",visual_requirement_reason="Synthetic concept explanation",
        graphic_plan=GraphicCardPlan(kind="text_card",treatment="key_point",points=[f"Point {i}"])))
        for i,s in enumerate(draft.scenes)]
    source=None
    if footage:
        path=db.data_root/'original.media';path.write_bytes(b'SYNTHETIC authorized full-frame footage')
        source=_asset(2,source_kind='user_asset').model_copy(update=dict(source_file=str(path),duration_ms=5000,
            content_hash=hashlib.sha256(path.read_bytes()).hexdigest(),metadata={'r1_usage':'production','project_id':str(pid)}))
        AssetRepository(db).create(source)
        clip=ClipRepository(db).create(Clip(asset_id=source.id,start_ms=0,end_ms=5000,asset_duration_ms=5000,
            transcript='word2',visual_description='word2',quality_score=1))
        scenes[2]=scenes[2].model_copy(update=dict(preferred_sources=[SourceKind.USER_ASSET],
            visual_requirement='action_evidence',visual_requirement_reason='Synthetic exact source demonstration',
            visual_intent=VisualIntent(subject='word2',action='word2',description='word2')))
    draft=draft.model_copy(update={'scenes':scenes,'version':draft.version+1});ProjectDraftRepository(db).save(draft)
    service=EvaluationCompositionService(db,db.data_root);style=VisualStyleTokens()
    selection=service.retained_selection(pid,style);assert selection.talking_runs=={}
    context=service._context(pid,selection);assert context['spec'] is not None and not context['missing']
    assert all(v['run'] is None for v in context['inputs'].values())
    if source:
        assert context['inputs'][scenes[2].id]['asset'].id==source.id
        assert 'source:'+str(source.id) in context['proof']['dependencies']
        assert context['spec'].scenes[2].visual.vertical_reframe_mode=='contain'
    plan=build_production_preflight(db,ProjectRepository(db).get(pid),draft,style_tokens=style,purpose='internal_evaluation',data_root=db.data_root)
    assert plan.status=='blocked' and 'composition_use_admission_required' in plan.stop_reasons
    use=use_and_batch(service,pid,context,key)
    plan=build_production_preflight(db,ProjectRepository(db).get(pid),draft,style_tokens=style,purpose='internal_evaluation',data_root=db.data_root)
    assert plan.status=='ready_for_authorized_dispatch' and plan.video_spec==context['spec']
    runs=ProductionRunService(db,db.data_root)
    result=runs.start(pid,idempotency_key='new-topic',expected_fingerprint=plan.fingerprint,style_tokens=style,purpose='internal_evaluation')
    job=JobRepository(db).get(result.render_job_id)
    assert job.payload.video_spec==plan.video_spec and service.require_current_job(job)['spec']==plan.video_spec
    assert job.type is JobType.RENDER and result.voice_job_id is None
    assert db.connection.execute('SELECT count(*) FROM jobs WHERE project_id=?',(str(pid),)).fetchone()[0]==2
    from app.renderer import RemotionRenderer
    from test_remotion_renderer import _renderer_project
    renderer=RemotionRenderer(AssetRepository(db),ClipRepository(db),audios=AudioAssetRepository(db),
        renderer_dir=_renderer_project(db.data_root/'new-topic-props'))
    for scene in plan.video_spec.scenes:
        renderer._validate_scene(scene,plan.video_spec.fps,project_id=pid,master_audio_id=audio.id,
            validate_scene_narration=False,composition_job_id=job.id)
    if source:
        repo=AssetRepository(db)
        repo.update(source.model_copy(update={'metadata':{**source.metadata,'project_id':str(uuid4())}}))
        with pytest.raises(UseAdmissionError,match='composition_source_not_authorized'):service.require_current_job(job)
        repo.update(source.model_copy(update={'metadata':{**source.metadata,'source_derivation':{}}}))
        with pytest.raises(UseAdmissionError,match='composition_source_derivation_changed'):service.require_current_job(job)
        repo.update(source)
        path.write_bytes(b'Changed source')
        with pytest.raises(UseAdmissionError,match='composition_source_bytes_changed'):service.require_current_job(job)
    else:
        service.revoke(pid,use.id,'Synthetic revocation',key)
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
    if calls:
        assert calls[0].status==('completed' if outcome=='completed' else 'failed')
        assert calls[0].estimated_cost == job.payload.video_spec.estimated_cost
        if outcome=='completed':
            assert calls[0].actual_cost == job.payload.video_spec.estimated_cost
            assert calls[0].actual_cost.amount is None and calls[0].actual_cost.currency is None
        else:
            with pytest.raises(UseAdmissionError):runs.resume(pid,run.id)
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
    budgets.save(BudgetPolicy(project_id=pid,max_calls=0,allow_unknown_cost=True,updated_at=datetime.now(timezone.utc)))
    with pytest.raises(ProductionRunNotReady):
        ProductionRunService(db,db.data_root).start(pid,idempotency_key='blocked',expected_fingerprint=context['request_sha256'],style_tokens=style,purpose='internal_evaluation')
    assert db.connection.execute('SELECT count(*) FROM jobs').fetchone()[0]==1


def test_explicit_resume_replaces_only_one_unreserved_failure_preserving_history(legacy):
    db,pid,_,_,_,_,key=legacy;service=EvaluationCompositionService(db,db.data_root);style=VisualStyleTokens()
    context=service._context(pid,service.retained_selection(pid,style));use_and_batch(service,pid,context,key)
    runs=ProductionRunService(db,db.data_root)
    run=runs.start(pid,idempotency_key='recover',expected_fingerprint=context['request_sha256'],style_tokens=style,purpose='internal_evaluation')
    jobs=JobRepository(db);old=jobs.get(run.render_job_id)
    jobs.update(old.model_copy(update=dict(status=JobStatus.FAILED,attempt=1,error_code='composition_render_failed')))
    history=dict(db.connection.execute('SELECT * FROM jobs WHERE id=?',(str(old.id),)).fetchone())
    resumed=runs.resume(pid,run.id);new=jobs.get(resumed.render_job_id)
    assert new.id!=old.id and new.status==JobStatus.PENDING and new.attempt==0 and new.payload==old.payload
    assert runs.resume(pid,run.id).render_job_id==new.id
    service.require_current_job(new)
    assert dict(db.connection.execute('SELECT * FROM jobs WHERE id=?',(str(old.id),)).fetchone())==history
    assert db.connection.execute('SELECT count(*) FROM provider_call_records').fetchone()[0]==0
    jobs.update(new.model_copy(update=dict(status=JobStatus.FAILED,attempt=1)))
    with pytest.raises(UseAdmissionError):runs.resume(pid,run.id)


def test_known_cny_cost_still_obeys_amount_limit(legacy):
    from decimal import Decimal
    from app.budget import ProviderCallLedger, BudgetLimitError
    from app.db import BudgetPolicyRepository
    from app.domain.models import BudgetPolicy, UsageCost, CostCategory
    db,pid,*_=legacy
    BudgetPolicyRepository(db).save(BudgetPolicy(project_id=pid,currency='CNY',max_amount=Decimal('2'),max_calls=1,
        allow_unknown_cost=True,updated_at=datetime.now(timezone.utc)))
    ledger=ProviderCallLedger(db)
    ledger.check_reservation(project_id=pid,estimated_cost=UsageCost(category=CostCategory.RENDER,amount=Decimal('1'),currency='CNY'))
    with pytest.raises(BudgetLimitError):
        ledger.check_reservation(project_id=pid,estimated_cost=UsageCost(category=CostCategory.RENDER,amount=Decimal('3'),currency='CNY'))


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


@pytest.fixture
def portrait(legacy):
    from app.media import MediaImporter,ProbeMetadata,VerticalSourceDerivationService
    from app.domain.models import VerifiedVerticalDerivationRequest,SourceCropWindow,VerifiedVerticalDerivationEvidence,RationalFps
    from pathlib import Path
    db,pid,asset,_,_,_,_=legacy
    asset=AssetRepository(db).update(asset.model_copy(update={'width':1280,'height':720}))
    class Probe:
        def probe(self,path):return ProbeMetadata(duration_ms=5000,width=1080,height=1080,fps=RationalFps(numerator=30,denominator=1),has_audio=False,metadata={})
    crop=SourceCropWindow(x=0,y=0,width=500,height=500)
    clip=ClipRepository(db).list_by_asset(asset.id)[0]
    evidence=VerifiedVerticalDerivationEvidence(source_asset_id=asset.id,source_clip_id=clip.id,source_content_hash=asset.content_hash,
        covered_start_ms=0,covered_end_ms=5000,crop_window=crop,face_head_clearance_verified=True,crop_stability_verified=True,
        source_subtitle_clearance_verified=True,method='human_full_interval_review',method_version='synthetic-v1',
        coverage='full_interval_continuous',confidence='reviewed',evidence_reference='Synthetic fixture, no actual crop-safety claim')
    request=VerifiedVerticalDerivationRequest(idempotency_key='synthetic-square',source_asset_id=asset.id,source_clip_id=clip.id,
        start_ms=0,end_ms=5000,crop_window=crop,output_height=1080,source_subtitle_state='not_observed',evidence=evidence)
    service=VerticalSourceDerivationService(db,db.data_root,MediaImporter(db,db.data_root,Probe()),runner=lambda argv:Path(argv[-1]).write_bytes(b'synthetic derived media'))
    return service.derive(request)


@pytest.mark.parametrize('style_name',['content-os-subject-first','content-os-editorial'])
def test_normal_visual_route_edit_plan_spec_and_renderer_share_verified_portrait(legacy,portrait,style_name):
    from app.domain.models import GraphicCardPlan
    from app.renderer import RemotionRenderer
    from test_remotion_renderer import _renderer_project
    db,pid,_,_,_,draft,key=legacy
    draft=draft.model_copy(update={'version':draft.version+1,'scenes':[s.model_copy(update={'graphic_plan':GraphicCardPlan(kind='text_card',treatment='headline',points=['word0 / word1'])}) for s in draft.scenes]})
    ProjectDraftRepository(db).save(draft)
    service=EvaluationCompositionService(db,db.data_root);style=VisualStyleTokens(name=style_name)
    context=service._context(pid,service.retained_selection(pid,style));use_and_batch(service,pid,context,key)
    plan=build_production_preflight(db,ProjectRepository(db).get(pid),draft,style_tokens=style,purpose='internal_evaluation',data_root=db.data_root)
    assert plan.status=='ready_for_authorized_dispatch' and all(d.selected_candidate.asset_id==portrait.asset.id for d in plan.scenes)
    run=ProductionRunService(db,db.data_root).start(pid,idempotency_key='portrait',expected_fingerprint=plan.fingerprint,style_tokens=style,purpose='internal_evaluation')
    job=JobRepository(db).get(run.render_job_id);spec=job.payload.video_spec
    assert all(s.visual.asset_id==portrait.asset.id and s.portrait_presentation.value=='portrait_panel' for s in spec.scenes[:5])
    assert all(s.graphic_text=='word0 / word1' and s.subtitle_treatment.value=='timed_captions' for s in spec.scenes[:5])
    assert spec.edit_plan.scenes[0].graphic_text==spec.scenes[0].graphic_text
    renderer=RemotionRenderer(AssetRepository(db),ClipRepository(db),audios=AudioAssetRepository(db),renderer_dir=_renderer_project(db.data_root/'props-renderer'))
    prepared={s.scene_id:renderer._validate_scene(s,spec.fps,project_id=pid,master_audio_id=context['audio'].id,
        validate_scene_narration=False,composition_job_id=job.id) for s in spec.scenes}
    stage=db.data_root/'props-stage';stage.mkdir()
    props=renderer._stage_props(spec,prepared,stage,'synthetic',None)
    source=props['sceneSources'][spec.scenes[0].scene_id]
    assert source['styleTokens']['name']==style_name and source['graphicText']=='word0 / word1'
    assert source['verticalReframeMode']=='contain' and source['sourceBottomCropRatio']==0


@pytest.mark.parametrize('invalid',['bytes','evidence','interval'])
def test_invalid_portrait_falls_back_to_reviewed_original_without_new_media(legacy,portrait,invalid):
    from pathlib import Path
    db,pid,original,_,_,_,_=legacy
    if invalid=='bytes':Path(portrait.asset.source_file).write_bytes(b'changed')
    else:
        p=portrait.asset.metadata['source_derivation']
        if invalid=='evidence':p['evidence']['face_head_clearance_verified']=False
        else:p['source_interval']['end_ms']=4999
        AssetRepository(db).update(portrait.asset)
    service=EvaluationCompositionService(db,db.data_root)
    context=service._context(pid,service.retained_selection(pid,VisualStyleTokens(name='content-os-subject-first')))
    assert all(v['asset'].id==original.id for v in context['inputs'].values())
    assert all(s.subtitle_treatment.value=='none' for s in context['spec'].scenes[:5])
    assert db.connection.execute('SELECT count(*) FROM provider_call_records').fetchone()[0]==0


@pytest.mark.parametrize("fps",[(30,1),(30000,1001)])
def test_absolute_timing_full_master_and_neutral_tail(fps,tmp_path):
    from app.assembly.evaluation_composition import assemble_retained_composition
    from app.domain.models import AudioAsset, RationalFps, VisualStyleTokens, TranscriptSegment, Clip, CandidateAsset
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
    for scene in scenes:
        item=inputs[scene.id]
        item['candidate']=CandidateAsset(scene_plan_id=scene.id,source_kind='ai_video',asset_id=item['asset'].id,clip_id=item['clip'].id,
            match_score=1,why=['synthetic'],estimated_cost={'category':'render','amount':'0','currency':'USD'})
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


@pytest.mark.parametrize("change", [None, "stale", "missing", "inside_word", "unverified", "overlap"])
def test_verified_position_preserving_words_map_scenes_without_rewriting_asr(change):
    from types import SimpleNamespace
    from app.production_runs import _unique_scene_speech_span
    from app.domain.models import AudioAsset, TranscriptSegment
    tokens = [f"word{i}" for i in range(80)]
    copy = " ".join(tokens)
    observed = list(tokens); observed[10] = "recognized"
    words = [dict(start_ms=i*100, end_ms=(i+1)*100, text=t) for i,t in enumerate(observed)]
    metadata = dict(voice_generation=dict(target_text=copy, qa_state="verified"),
        voice_word_timing=dict(source_sha256="a"*64, words=words))
    scenes = [SimpleNamespace(id=uuid4(), voice_text=" ".join(tokens[:40])),
        SimpleNamespace(id=uuid4(), voice_text=" ".join(tokens[40:]))]
    if change == "stale": metadata["voice_word_timing"]["source_sha256"] = "b"*64
    elif change == "missing": words.pop(10)
    elif change == "inside_word": words[39:41] = [dict(start_ms=3900,end_ms=4100,text=" ".join(observed[39:41]))]
    elif change == "unverified": metadata["voice_generation"]["qa_state"] = "pending"
    elif change == "overlap": words[2]["start_ms"] = 150
    audio = AudioAsset(source_file="fixture.wav",content_hash="a"*64,duration_ms=8200,
        sample_rate=24000,channels=1,authorization_reference="synthetic",imported_at=datetime.now(timezone.utc),
        transcript_source="fixture-asr",transcript_segments=[TranscriptSegment(start_ms=0,end_ms=8000,text=" ".join(observed))],metadata=metadata)
    before = audio.model_dump_json()
    assert _unique_scene_speech_span(audio,scenes,scenes[0].id) == ((0,4000) if change is None else None)
    if change is None: assert _unique_scene_speech_span(audio,scenes,scenes[1].id) == (4000,8000)
    assert audio.model_dump_json() == before
