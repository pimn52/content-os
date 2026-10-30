"""Shared local replacement and queue; perception/render drawing remain fixtures."""
import hashlib
import json
import subprocess
from datetime import timedelta
from threading import Event
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.assembly.edit_plan import _default_edit_plan
from app.db import (AssetRepository,AudioAssetRepository,ClipRepository,JobRepository,
    ProjectDraftRepository,ProjectRepository,ProviderCallRepository)
from app.domain.models import Asset,CandidateAsset,Clip,CostCategory,JobStatus,JobType,SourceKind,UsageCost,VideoSpec,VerticalReframeMode
from app.jobs import JobRunner,JobStore
from app.jobs.handlers import RenderVideoJobHandler
from app.media.visual_facts import scene_visual_key
from app.presentation_material import replacement_options
from app.presentation_repair import PresentationRepairService,PresentationRepairRequest,PresentationObservationRequest
from app.presentation_semantic import PresentationSemanticDiagnosis
from app.production_runs import ProductionRunConflict,ProductionRunNotReady
from app.video_production import VideoProductionService
from app.worker_cli import _run_product_queue
from app.runtime import resolve_local_executable
from test_presentation_semantic import prepared,FixtureProvider
from test_semantic_production import _automatic
from test_presentation_repair import synthetic_video


@pytest.fixture
def material(prepared,monkeypatch):
    c=prepared
    draft=ProjectDraftRepository(c.db).get(c.project)
    project=ProjectRepository(c.db).get(c.project)
    spec=c.job.payload.video_spec
    c.source_clip=ClipRepository(c.db).get(spec.scenes[0].visual.clip_id)
    choices={scene.id:CandidateAsset(scene_plan_id=scene.id,source_kind=SourceKind.USER_ASSET,
        asset_id=c.asset.id,clip_id=c.source_clip.id,match_score=.9,why=['fixture old choice'],
        estimated_cost=UsageCost(category=CostCategory.USER_ASSET)) for scene in draft.scenes}
    spec=spec.model_copy(update={'edit_plan':_default_edit_plan(project,draft.scenes,choices)})
    c.job=JobRepository(c.db).update(c.job.model_copy(update={'payload':c.job.payload.model_copy(update={'video_spec':spec})}))
    base=PresentationRepairService._validate
    def validate(service,row,current):
        base(service,row,current)
        snapshots=[]
        for scene in current.scenes:
            asset=AssetRepository(c.db).get(scene.visual.asset_id)
            if asset is not None:
                if hashlib.sha256((c.root/asset.source_file).read_bytes()).hexdigest()!=asset.content_hash:
                    raise ProductionRunNotReady(('fixture_source_bytes_changed',))
                snapshots.extend([asset.model_dump(mode='json'),ClipRepository(c.db).get(scene.visual.clip_id).model_dump(mode='json')])
        snapshots.append(AudioAssetRepository(c.db).get(c.master.id).model_dump(mode='json'))
        return snapshots
    monkeypatch.setattr(PresentationRepairService,'_validate',validate)
    c.scene=draft.scenes[1]
    c.service=PresentationRepairService(c.db,c.root,c.root/'renders')
    return c


def add_footage(c,*,name='replacement',width=1080,height=1920,start=0,end=4000,source_bytes=None):
    path=c.root/(name+'.mp4');path.write_bytes(source_bytes or ('fixture full interval '+name).encode())
    clip=Clip(asset_id=uuid4(),start_ms=start,end_ms=end,asset_duration_ms=6000,quality_score=.9)
    facts={'source_content_hash':hashlib.sha256(path.read_bytes()).hexdigest(),'clip_interval':{'start_ms':start,'end_ms':end},
        'method':'provider_video_interval_analysis','coverage':'video_interval_analysis','content_consistent':True,
        'analysis_video_sha256':'a'*64,'covered_start_ms':start,'covered_end_ms':end,'analysis_duration_ms':end-start,
        'evidence_reference':'fixture:already-verified:'+name,'comparison_layout':'none','subtitle_regions':[],
        'protected_regions':[],'source_subtitle_clearance_verified':True,
        'scene_matches':{scene_visual_key(c.scene):{'supported':True,'score':.9,'quality_adequate':True,'fit':'illustrative','subject_verified':True}}}
    asset=Asset(id=clip.asset_id,source_file=path.name,content_hash=facts['source_content_hash'],duration_ms=6000,
        width=width,height=height,fps=c.asset.fps,has_audio=False,authorization_reference='registered source rights',
        imported_at=c.asset.imported_at,metadata={'visual_analysis':{str(clip.id):facts}})
    AssetRepository(c.db).create(asset);ClipRepository(c.db).create(clip)
    return asset,clip,path


def diagnose_material(c):
    provider=FixtureProvider(source=False,render=False)
    center=_automatic(c,provider)
    center.queue_jobs({JobType.RENDER},run_id=c.run)
    return center,provider


def body(c,plan,key='one-local-replacement'):
    return PresentationRepairRequest(observation_id=UUID(c.observation['id']),expected_fingerprint=plan['fingerprint'],
        idempotency_key=key,confirmed_action='render_revision',reason='fixture material correction')


def test_material_plan_changes_only_target_visual_and_refreshes_whole_video_without_writes(material):
    c=material;asset,clip,_=add_footage(c)
    center,provider=diagnose_material(c)
    assert center.status(c.project)['next_action']=='diagnose_quality'
    before=c.db.connection.total_changes
    options=replacement_options(c.db,c.root,c.job.payload.video_spec,c.scene,style=c.job.payload.video_spec.edit_plan.style_tokens,run_id=c.run)
    assert options
    proposed=c.job.payload.video_spec.model_copy(update={'scenes':[c.job.payload.video_spec.scenes[0],options[0]['scene']],
        'edit_plan':c.job.payload.video_spec.edit_plan.model_copy(update={'scenes':[c.job.payload.video_spec.edit_plan.scenes[0],options[0]['entry']]})})
    row=c.db.connection.execute('SELECT * FROM production_runs WHERE id=?',(str(c.run),)).fetchone()
    c.service._review_material_spec(row,proposed,ProjectDraftRepository(c.db).get(c.project))
    plan=c.service.plan(c.project,c.run,UUID(c.observation['id']))
    assert c.db.connection.total_changes==before
    assert plan['action']=='render_revision',plan['stop_reasons']
    assert plan['responsible_stage']=='material' and plan['required_capability']==[]
    new=VideoSpec.model_validate(plan['candidate_spec']);old=c.job.payload.video_spec
    assert new.master_narration==old.master_narration and new.scenes[0]==old.scenes[0]
    ignored={'visual','portrait_presentation','framing_policy','subtitle_treatment','graphic_treatment','graphic_text','visual_role'}
    assert {k:v for k,v in new.scenes[1].model_dump().items() if k not in ignored}=={k:v for k,v in old.scenes[1].model_dump().items() if k not in ignored}
    assert new.scenes[1].visual.asset_id==asset.id and new.scenes[1].visual.clip_id==clip.id
    assert new.edit_plan.whole_video.timing_basis=='rendered_timeline'
    assert new.edit_plan.whole_video.scenes[1].source_keys==[asset.content_hash]
    assert plan['voice_calls']==plan['talking_calls']==0 and provider.calls==1


@pytest.mark.parametrize('change',['missing_qa','wrong_task','unknown_fit','short','subtitle_unknown','wrong_hash','same_source_alias'])
def test_unqualified_or_rejected_original_source_cannot_be_used_as_replacement(material,change):
    c=material
    asset,clip,path=add_footage(c,start=1000 if change=='same_source_alias' else 0,end=500 if change=='short' else 4000)
    metadata=asset.model_dump(mode='json')['metadata'];facts=metadata['visual_analysis'][str(clip.id)]
    if change=='missing_qa':facts['coverage']='single_frame'
    if change=='wrong_task':facts['scene_matches']={}
    if change=='unknown_fit':facts['scene_matches'][scene_visual_key(c.scene)]['fit']='unknown'
    if change=='subtitle_unknown':facts['source_subtitle_clearance_verified']=False
    if change=='wrong_hash':path.write_bytes(b'changed bytes')
    if change=='same_source_alias':
        metadata['source_derivation']={'source_asset_id':str(c.asset.id),'source_content_hash':c.asset.content_hash,
            'source_interval':{'start_ms':0,'end_ms':6000},
            'crop_window':{'schema_version':'0.1','x':0,'y':0,'width':c.asset.width,'height':c.asset.height}}
    AssetRepository(c.db).update(asset.model_copy(update={'metadata':metadata}))
    center,provider=diagnose_material(c)
    plan=c.service.plan(c.project,c.run,UUID(c.observation['id']))
    assert plan['action']=='scoped_fix_required' and plan['candidate_spec'] is None
    assert 'presentation_material_replacement_unavailable' in plan['stop_reasons']
    before=JobRepository(c.db).list();center.queue_jobs({JobType.RENDER},run_id=c.run)
    assert center.status(c.project)['next_action']=='stopped' and JobRepository(c.db).list()==before and provider.calls==1
    assert center.status(c.project)['responsible_stage']=='material'
    assert center.status(c.project)['quality_diagnosis']['assessment_state']=='known'


def test_new_candidate_bytes_invalidate_plan_before_dispatch_and_unknown_mixed_finding_does_not_partial_repair(material):
    c=material;_,_,path=add_footage(c);diagnose_material(c)
    plan=c.service.plan(c.project,c.run,UUID(c.observation['id']))
    path.write_bytes(b'changed')
    with pytest.raises(ProductionRunConflict):c.service.apply(c.project,c.run,body(c,plan))
    c.service.observe(c.project,c.run,PresentationObservationRequest(**{**c.observation['request'],
        'idempotency_key':'also-voice','finding':'voice_naturalness'}))
    current=c.service.plan(c.project,c.run,UUID(c.observation['id']))
    assert current['candidate_spec'] is None and len(current['observations'])==2


def test_apply_updates_usage_snapshot_and_new_observation_fences_already_queued_render(material):
    c=material;asset,_,_=add_footage(c)
    superseded={**c.observation,'id':str(uuid4()),'request':{**c.observation['request'],'idempotency_key':'superseded'}}
    c.db.connection.execute('INSERT INTO presentation_observations(rowid,id,project_id,run_id,render_job_id,idempotency_key,payload) VALUES (0,?,?,?,?,?,?)',
        (superseded['id'],str(c.project),str(c.run),str(c.job.id),'superseded',json.dumps(superseded)))
    diagnose_material(c)
    plan=c.service.plan(c.project,c.run,UUID(c.observation['id']))
    repair=c.service.apply(c.project,c.run,body(c,plan))
    assert c.service.apply(c.project,c.run,body(c,plan))==repair
    job=JobRepository(c.db).get(UUID(repair['replacement_job_id']))
    assert c.db.connection.execute('SELECT 1 FROM render_use_constraint_snapshots WHERE job_id=?',(str(job.id),)).fetchone()
    uses=json.loads(c.db.connection.execute('SELECT visual_source_uses FROM production_runs WHERE id=?',(str(c.run),)).fetchone()[0])
    assert any(use['source_hash']==asset.content_hash for use in uses)
    assert c.service.require_current_repair(job)['id']==repair['id']
    # A separately retained observation about the old version cannot be lost by the new output.
    saved={**c.observation,'id':str(uuid4()),'request':{**c.observation['request'],'idempotency_key':'late','finding':'voice_naturalness'}}
    c.db.connection.execute('INSERT INTO presentation_observations VALUES (?,?,?,?,?,?)',
        (saved['id'],str(c.project),str(c.run),str(c.job.id),'late',json.dumps(saved)))
    with pytest.raises(ProductionRunNotReady,match='presentation_material_observations_changed'):
        c.service.require_current_repair(job)


@pytest.mark.parametrize('corrupt',[False,True])
@pytest.mark.parametrize('currency',['USD','CNY'])
def test_ordinary_queue_replaces_once_preserves_other_work_and_rechecks_whole_output(material,corrupt,currency):
    from app.db import BudgetPolicyRepository
    c=material;asset,_,_=add_footage(c);center,provider=diagnose_material(c)
    provider.source=provider.render=True  # The replacement is independently observed after the old pair failed.
    policy=BudgetPolicyRepository(c.db).get_by_project(c.project)
    BudgetPolicyRepository(c.db).save(policy.model_copy(update={'currency':currency}))
    before=JobRepository(c.db).list();master=AudioAssetRepository(c.db).get(c.master.id)
    class Renderer:
        data_root=c.root
        def render(self,spec,target,**kwargs):
            if corrupt:target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(b'bad local fixture output')
            else:synthetic_video(target,spec)
    runner=JobRunner(JobStore(c.db),{JobType.RENDER:RenderVideoJobHandler(ProjectRepository(c.db),Renderer(),c.root/'renders',
        semantic_diagnoser=center.semantic_diagnoser)},
        worker_id='material-local',lease_duration=timedelta(minutes=1),max_attempts=1,on_completed=center.completed)
    runner.production_service=center
    assert _run_product_queue(SimpleNamespace(once=False,poll_seconds=.01),c.db,runner,Event(),run_id=c.run)==(1 if corrupt else 0)
    history=c.service.history(c.project,c.run)
    assert len(history)==1 and len(JobRepository(c.db).list())==len(before)+1
    job=JobRepository(c.db).get(UUID(history[0]['replacement_job_id']))
    assert job.payload.video_spec.scenes[1].visual.asset_id==asset.id
    assert job.payload.video_spec.scenes[0]==c.job.payload.video_spec.scenes[0]
    assert job.payload.video_spec.master_narration==c.job.payload.video_spec.master_narration
    assert AudioAssetRepository(c.db).get(master.id)==master and JobRepository(c.db).get(c.job.id)==c.job
    assert c.output.read_bytes()==b'fixture existing rendered output' and provider.calls==(1 if corrupt else 2)
    assert center.status(c.project)['next_action']==('stopped' if corrupt else 'review')
    if not corrupt:
        assert history[0]['technical_qa']['decode']=='pass'
        assert history[0]['semantic_recheck']['state']=='pass'
    center.queue_jobs({JobType.RENDER},run_id=c.run)
    assert len(JobRepository(c.db).list())==len(before)+1
    calls=ProviderCallRepository(c.db).list_for_project(c.project)
    assert [call.operation for call in calls].count('render')==1


def test_full_repair_budget_is_checked_before_any_new_render_is_created(material):
    from app.db import BudgetPolicyRepository
    c=material;add_footage(c);center,provider=diagnose_material(c)
    policy=BudgetPolicyRepository(c.db).get_by_project(c.project)
    BudgetPolicyRepository(c.db).save(policy.model_copy(update={'max_calls':2}))  # One already used; Render alone would fit.
    before=JobRepository(c.db).list();center.queue_jobs({JobType.RENDER},run_id=c.run)
    assert JobRepository(c.db).list()==before and provider.calls==1
    assert center.status(c.project)['next_action']=='stopped'


def test_missing_recheck_configuration_blocks_manual_render_before_ledger_or_renderer(material):
    from app.jobs.runner import JobExecutionError
    c=material;add_footage(c);diagnose_material(c)
    plan=c.service.plan(c.project,c.run,UUID(c.observation['id']))
    assert plan['semantic_recheck_calls']==1 and plan['external_charge_ceiling']=='unknown'
    record=c.service.apply(c.project,c.run,body(c,plan))
    job=JobRepository(c.db).get(UUID(record['replacement_job_id']))
    job=JobRepository(c.db).update(job.model_copy(update={'status':JobStatus.RUNNING}))
    class NeverRender:
        data_root=c.root
        def render(self,*_args,**_kwargs):pytest.fail('render began before required recheck capability was ready')
    before=ProviderCallRepository(c.db).list()
    with pytest.raises(JobExecutionError) as failure:
        RenderVideoJobHandler(ProjectRepository(c.db),NeverRender(),c.root/'renders')(job)
    assert failure.value.code=='material_semantic_recheck_not_configured' and not failure.value.retryable
    assert ProviderCallRepository(c.db).list()==before


def test_replacement_does_not_inherit_prior_assets_intentional_reuse_exception(material):
    c=material;asset,_,_=add_footage(c)
    old=AssetRepository(c.db).get(c.asset.id)
    AssetRepository(c.db).update(old.model_copy(update={'metadata':{'reuse_role':'meme'}}))
    diagnose_material(c)
    plan=c.service.plan(c.project,c.run,UUID(c.observation['id']))
    whole=VideoSpec.model_validate(plan['candidate_spec']).edit_plan.whole_video
    assert whole.scenes[0].intentional_reuse and not whole.scenes[1].intentional_reuse


def test_current_source_policy_context_fences_pending_material_plan(material,monkeypatch):
    from app.source_use_constraints import SourceUseConstraintService
    c=material;add_footage(c);diagnose_material(c)
    state=[]
    monkeypatch.setattr(SourceUseConstraintService,'context',lambda *_:state[:])
    plan=c.service.plan(c.project,c.run,UUID(c.observation['id']))
    state.append({'fixture':'policy changed'})
    with pytest.raises(ProductionRunConflict):c.service.apply(c.project,c.run,body(c,plan))


def test_snapshot_failure_rolls_back_new_job_usage_and_repair_record(material,monkeypatch):
    from app.source_use_constraints import SourceUseConstraintService
    c=material;add_footage(c);diagnose_material(c)
    plan=c.service.plan(c.project,c.run,UUID(c.observation['id']))
    before=JobRepository(c.db).list()
    row=dict(c.db.connection.execute('SELECT * FROM production_runs WHERE id=?',(str(c.run),)).fetchone())
    def fail(*_):raise RuntimeError('fixture snapshot commit failed')
    monkeypatch.setattr(SourceUseConstraintService,'snapshot_render',fail)
    with pytest.raises(RuntimeError,match='snapshot commit failed'):c.service.apply(c.project,c.run,body(c,plan))
    assert JobRepository(c.db).list()==before and c.service.history(c.project,c.run)==[]
    assert dict(c.db.connection.execute('SELECT * FROM production_runs WHERE id=?',(str(c.run),)).fetchone())==row


def test_pending_replacement_rechecks_new_source_bytes_before_render(material):
    c=material;_,_,path=add_footage(c);diagnose_material(c)
    plan=c.service.plan(c.project,c.run,UUID(c.observation['id']))
    repair=c.service.apply(c.project,c.run,body(c,plan))
    path.write_bytes(b'mutated after enqueue')
    with pytest.raises(ProductionRunNotReady,match='fixture_source_bytes_changed'):
        c.service.require_current_repair(JobRepository(c.db).get(UUID(repair['replacement_job_id'])))


@pytest.mark.parametrize('new_subtitles',[False,True])
def test_new_source_replaces_or_clears_old_subtitle_crop_evidence(material,new_subtitles):
    c=material;asset,clip,_=add_footage(c)
    if new_subtitles:
        metadata=asset.model_dump(mode='json')['metadata']
        metadata['visual_analysis'][str(clip.id)]['subtitle_regions']=[[0,.8,1,.15]]
        AssetRepository(c.db).update(asset.model_copy(update={'metadata':metadata}))
    old=c.job.payload.video_spec
    old_entry=old.edit_plan.scenes[1].model_copy(update={'burned_in_subtitles':'present','source_bottom_crop_ratio':.1,
        'source_subtitle_evidence_reference':'fixture:old-crop-proof'})
    old_scene=old.scenes[1].model_copy(update={'visual':old.scenes[1].visual.model_copy(update={
        'vertical_reframe_mode':VerticalReframeMode.BOTTOM_CROP_CONTAIN,'source_bottom_crop_ratio':.1,
        'vertical_reframe_evidence_reference':'fixture:old-crop-proof'})})
    old=old.model_copy(update={'scenes':[old.scenes[0],old_scene],
        'edit_plan':old.edit_plan.model_copy(update={'scenes':[old.edit_plan.scenes[0],old_entry]})})
    c.job=JobRepository(c.db).update(c.job.model_copy(update={'payload':c.job.payload.model_copy(update={'video_spec':old})}))
    diagnose_material(c)
    plan=c.service.plan(c.project,c.run,UUID(c.observation['id']))
    assert plan['action']=='render_revision',plan['stop_reasons']
    new=VideoSpec.model_validate(plan['candidate_spec']);entry=new.edit_plan.scenes[1]
    assert entry.source_bottom_crop_ratio==new.scenes[1].visual.source_bottom_crop_ratio
    if new_subtitles:
        assert entry.source_bottom_crop_ratio>0 and entry.source_subtitle_evidence_reference=='fixture:already-verified:replacement'
    else:
        assert entry.source_bottom_crop_ratio==0 and entry.source_subtitle_evidence_reference is None
        assert new.scenes[1].visual.vertical_reframe_evidence_reference is None


def test_replacement_uses_the_current_ntsc_frame_window_without_shortening_narration(material):
    from app.domain.models import RationalFps
    c=material;add_footage(c,start=1000,end=5000)
    spec=c.job.payload.video_spec.model_copy(update={'fps':RationalFps(numerator=30000,denominator=1001)})
    options=replacement_options(c.db,c.root,spec,c.scene,style=spec.edit_plan.style_tokens,run_id=c.run)
    assert len(options)==1 and options[0]['evidence']['used_interval_ms']==[1001,2002]
    assert options[0]['scene'].duration_frames==spec.scenes[1].duration_frames
    assert options[0]['scene'].narration_start_ms==spec.scenes[1].narration_start_ms
    assert options[0]['scene'].narration_end_ms==spec.scenes[1].narration_end_ms


def test_full_admission_with_real_media_and_voice_fixture_uses_same_plan_apply_path(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.db import Database
    from app.domain.models import ScenePlan
    from test_production_runs import _ready_fixture
    from test_presentation_semantic import FixtureMedia
    path=tmp_path/'full-material.sqlite'
    with TestClient(create_app(path)) as client:
        pid,_,_=_ready_fixture(client,path)
        raw=client.get(f'/projects/{pid}/draft').json()
        scene=ScenePlan.model_validate({**raw['scenes'][0],'preferred_sources':['user_asset'],
            'visual_requirement':'explanatory','visual_requirement_reason':'fixture原意图不变'})
        with Database(path) as db:
            template=Asset(source_file='unused.mp4',content_hash='e'*64,duration_ms=6000,width=320,height=568,
                fps=ProjectRepository(db).get(UUID(pid)).fps,authorization_reference='fixture rights',
                imported_at=ProjectRepository(db).get(UUID(pid)).created_at)
            c=SimpleNamespace(root=tmp_path,db=db,asset=template,scene=scene)
            for color,name in [('blue','old-real'),('red','new-real')]:
                video=tmp_path/(name+'-media.mp4')
                subprocess.run([resolve_local_executable('ffmpeg'),'-y','-v','error','-f','lavfi','-i',
                    f'color=c={color}:s=320x568:r=30:d=6','-an','-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p',str(video)],
                    capture_output=True,check=True,timeout=30)
                asset,clip,_=add_footage(c,name=name,width=320,height=568,source_bytes=video.read_bytes())
                if name=='old-real':old_clip=clip
                else:replacement=asset
        scene=scene.model_copy(update={'preferred_clip_id':old_clip.id})
        saved=client.put(f'/projects/{pid}/draft',json={'script':scene.voice_text,'topic':'New','scenes':[scene.model_dump(mode='json')]})
        assert saved.status_code==200,saved.text
        assert client.put(f'/projects/{pid}/budget',json={'currency':'USD','max_calls':4,'allow_unknown_cost':True}).status_code==200
        plan=client.post(f'/projects/{pid}/production-preflight',json={}).json()
        started=client.post(f'/projects/{pid}/production-runs',json={'idempotency_key':'full-material','expected_fingerprint':plan['fingerprint']})
        assert started.status_code==201,started.text
        run=UUID(started.json()['id'])
    with Database(path) as db:
        db.data_root=tmp_path
        row=db.connection.execute('SELECT * FROM production_runs WHERE id=?',(str(run),)).fetchone()
        old=JobRepository(db).get(UUID(row['render_job_id']))
        old=JobRepository(db).update(old.model_copy(update={'status':JobStatus.COMPLETED}))
        output=tmp_path/'renders'/pid/f'{old.payload.render_id}.mp4';synthetic_video(output,old.payload.video_spec)
        service=PresentationRepairService(db,tmp_path,tmp_path/'renders')
        observation=service.observe(UUID(pid),run,PresentationObservationRequest(idempotency_key='full-observe',
            render_sha256=hashlib.sha256(output.read_bytes()).hexdigest(),scene_id=scene.scene_id,
            finding='semantic_mismatch',reason='fixture visual perception mismatch',evidence_reference='fixture:full-version'))
        media=FixtureMedia(tmp_path)
        machine=PresentationSemanticDiagnosis(db,tmp_path,FixtureProvider(source=False,render=False),media,media).diagnose(UUID(pid),run,observation['id'])
        assert machine['responsible_stage']=='material',machine
        revised=service.plan(UUID(pid),run,UUID(observation['id']))
        assert revised['action']=='render_revision',revised['stop_reasons']
        record=service.apply(UUID(pid),run,PresentationRepairRequest(observation_id=UUID(observation['id']),
            expected_fingerprint=revised['fingerprint'],idempotency_key='full-apply',confirmed_action='render_revision',reason='fixture local replacement'))
        job=JobRepository(db).get(UUID(record['replacement_job_id']))
        assert service.require_current_repair(job)['id']==record['id']
        assert job.payload.video_spec.scenes[0].visual.asset_id==replacement.id
        assert job.payload.video_spec.master_narration==old.payload.video_spec.master_narration
