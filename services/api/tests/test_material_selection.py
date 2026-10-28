"""三类来源与整片选材的行为回归；全部使用明确标注的本地替身。"""
from datetime import datetime, timezone
import hashlib
import json
from uuid import uuid4

import pytest

from app.assembly.video_spec import VideoSpecAssembler, InvalidCandidateSelection
from app.db import AssetRepository, ClipRepository
from app.domain.models import Asset, Clip, MaterialRequest, SourceKind
from app.media.planning import MaterialResolutionError,_distinct_observed_windows
from app.media.visual_facts import scene_visual_key, full_interval_geometry, matches_scene
from app.providers.vision import VideoIntervalAnalysis, VideoIntervalBatch, SceneVisualMatch
from app.routing import AssetRouter
from app.search import ClipTextSearchService
from test_material_resolution import material_library


def add_source(db, project, root, kind, name):
    path=root/(name+'.mp4');path.write_bytes(('fixture-'+name).encode())
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    metadata={}
    if kind is SourceKind.AI_BROLL:
        metadata['visual_generation']={'provider':'fixture-generator','model':'fixture',
            'source_content_hash':digest,'license_evidence_reference':'fixture-model-use',
            'commercial_status':'commercial_safe'}
    asset=AssetRepository(db).create(Asset(source_kind=kind,source_file=str(path),content_hash=digest,
        duration_ms=6000,width=1080,height=1920,fps=project.fps,has_audio=False,
        authorization_reference='fixture intake',imported_at=datetime.now(timezone.utc),metadata=metadata))
    clip=ClipRepository(db).create(Clip(asset_id=asset.id,start_ms=0,end_ms=6000,
        asset_duration_ms=6000,visual_description='明确的测试画面',quality_score=.8))
    return asset,clip


def observed_pool(resolver, project, sources, scenes, match):
    class Observations:
        supports_video_input=True
        def analyze_intervals(self,videos,requests):
            return VideoIntervalBatch(clips=[VideoIntervalAnalysis(clip_id=v['clip_id'],description='本地替身画面',
                content_consistent=True,comparison_layout='none',subtitle_regions=[],protected_regions=[],reason='fixture',
                matches=[SceneVisualMatch(scene_key=scene_visual_key(s),supported=match(v,s)[0],
                    score=match(v,s)[1],fit=match(v,s)[2],quality_adequate=True,subject_verified=True,reason='替身表达任务核验') for s in scenes]) for v in videos])
    resolver.provider=Observations()
    windows={str(c.id):(a,c) for a,c in sources}
    resolver._analyze(project,windows,[{'scene_key':scene_visual_key(s)} for s in scenes],uuid4())
    return windows


def test_direct_external_explanation_beats_related_creator_equipment(tmp_path):
    db,p,_,_,scene,_,resolver=material_library(tmp_path)
    try:
        own=add_source(db,p,tmp_path,SourceKind.USER_ASSET,'equipment')
        stock=add_source(db,p,tmp_path,SourceKind.STOCK,'actual-subject')
        scene=scene.model_copy(update={'preferred_clip_id':None,'viewer_takeaway':'理解压缩如何影响运动细节'})
        windows=observed_pool(resolver,p,[own,stock],[scene],
            lambda v,s:(True,.98,'context') if v['clip_id']==str(own[1].id) else (True,.9,'direct'))
        result=resolver._select(p,[scene],windows,None)[0]
        assert result.preferred_clip_id==stock[1].id
        assert SourceKind.STOCK in result.fallback_sources
        facts=full_interval_geometry(AssetRepository(db).get(stock[0].id),stock[1])
        assert facts['editorial_selections'][scene_visual_key(scene)]['viewer_takeaway']==scene.viewer_takeaway
    finally:db.close()


def test_failed_selection_reports_subtitle_and_purpose_rejections_without_changing_admission(tmp_path):
    db,p,_,_,scene,_,resolver=material_library(tmp_path)
    try:
        blocked=add_source(db,p,tmp_path,SourceKind.USER_ASSET,'subtitle-overlap')
        unrelated=add_source(db,p,tmp_path,SourceKind.STOCK,'unrelated')
        windows=observed_pool(resolver,p,[blocked,unrelated],[scene],
            lambda v,s:(v['clip_id']==str(blocked[1].id),.95,'direct'))
        current=AssetRepository(db).get(blocked[0].id)
        metadata=json.loads(json.dumps(current.metadata))
        metadata['visual_analysis'][str(blocked[1].id)]['source_subtitle_clearance_verified']=False
        AssetRepository(db).update(current.model_copy(update={'metadata':metadata}))
        with pytest.raises(MaterialResolutionError) as failure:
            resolver._select(p,[scene],windows,None)
        reasons=failure.value.candidate_rejections[scene.scene_id]
        assert reasons[str(blocked[1].id)]==['subtitle_clearance_unverified']
        assert reasons[str(unrelated[1].id)]==['visual_purpose_mismatch']
        from types import SimpleNamespace
        from app.jobs.handlers import _write_render_failure_diagnostic
        job=SimpleNamespace(id=uuid4(),attempt=1)
        assert _write_render_failure_diagnostic(job,tmp_path,'content_planning',failure.value)
        report=json.loads((tmp_path/'render-diagnostics'/str(job.id)/'attempt-1.json').read_text(encoding='utf-8'))
        assert report['material_candidate_rejections']==failure.value.candidate_rejections
    finally:db.close()


@pytest.mark.parametrize('known,same_start,expected',[(True,True,1),(False,True,2),(True,False,2)])
def test_qa_slots_share_only_verified_same_start_prefixes_without_moving_actions(tmp_path,known,same_start,expected):
    db,p,_,_,scene,_,resolver=material_library(tmp_path)
    try:
        asset,long=add_source(db,p,tmp_path,SourceKind.STOCK,'same-source')
        short=ClipRepository(db).create(long.model_copy(update={'id':uuid4(),'start_ms':0 if same_start else 1000,'end_ms':4000}))
        windows={str(c.id):(asset,c) for c in [short,long]}
        if known:
            observed_pool(resolver,p,[(asset,short),(asset,long)],[scene],lambda v,s:(True,.95,'direct'))
            current=AssetRepository(db).get(asset.id)
            windows={key:(current,clip) for key,(_,clip) in windows.items()}
        distinct=_distinct_observed_windows(p,windows)
        assert len(distinct)==expected
        if expected==1:assert list(distinct)==[str(long.id)]
        else:assert set(distinct)=={str(short.id),str(long.id)}
    finally:db.close()


@pytest.mark.parametrize('kind',[SourceKind.USER_ASSET,SourceKind.STOCK])
@pytest.mark.parametrize('verified',[True,False,None])
def test_action_evidence_requires_identifiable_subject_across_selection_routing_and_assembly(tmp_path,kind,verified):
    from app.routing.asset_router import RoutingInputError
    db,p,_,_,base,_,resolver=material_library(tmp_path)
    try:
        source=add_source(db,p,tmp_path,kind,'cup-motion')
        scene=base.model_copy(update={'preferred_clip_id':source[1].id,'preferred_sources':[kind],
            'visual_requirement':'action_evidence','visual_requirement_reason':'须看清咖啡制作对象与操作'})
        windows=observed_pool(resolver,p,[source],[scene],lambda v,s:(True,.95,'direct'))
        selected=resolver._select(p,[scene],windows,None)[0]
        route=AssetRouter(ClipTextSearchService(db),AssetRepository(db)).route(selected)
        candidate=next(c for c in route.candidates if c.recommended)
        current=AssetRepository(db).get(source[0].id)
        metadata=json.loads(json.dumps(current.metadata))
        match=metadata['visual_analysis'][str(source[1].id)]['scene_matches'][scene_visual_key(scene)]
        match['subject_verified']=verified
        AssetRepository(db).update(current.model_copy(update={'metadata':metadata}))
        facts=full_interval_geometry(AssetRepository(db).get(source[0].id),source[1])
        if verified:
            assert matches_scene(facts,scene)
            assert resolver._select(p,[scene],windows,None)[0].preferred_clip_id==source[1].id
            assert AssetRouter(ClipTextSearchService(db),AssetRepository(db)).explicit_footage(scene,[candidate])==candidate
            assert VideoSpecAssembler(AssetRepository(db),ClipRepository(db)).assemble(p,[scene],{scene.id:candidate})
        else:
            assert not matches_scene(facts,scene)
            with pytest.raises(MaterialResolutionError):resolver._select(p,[scene],windows,None)
            with pytest.raises(RoutingInputError):AssetRouter(ClipTextSearchService(db),AssetRepository(db)).explicit_footage(scene,[candidate])
            with pytest.raises(InvalidCandidateSelection):VideoSpecAssembler(AssetRepository(db),ClipRepository(db)).assemble(p,[scene],{scene.id:candidate})
    finally:db.close()


def test_generic_action_words_do_not_displace_subject_candidates_from_new_supply(tmp_path):
    db,p,asset,coarse,base,_,resolver=material_library(tmp_path)
    try:
        ClipRepository(db).upsert(coarse.model_copy(update={'visual_description':'制作时双手操作器具，观看操作细节'}))
        stock=add_source(db,p,tmp_path,SourceKind.STOCK,'external-coffee')
        own=add_source(db,p,tmp_path,SourceKind.USER_ASSET,'own-coffee')
        ClipRepository(db).upsert(own[1].model_copy(update={'visual_description':'咖啡制作，双手操作器具'}))
        scene=base.model_copy(update={'preferred_clip_id':None,'voice_text':'画面要让人看清双手怎样操作器具。',
            'visual_intent':base.visual_intent.model_copy(update={'subject':'咖啡制作','action':'双手实际操作器具'}),
            'material_request':MaterialRequest(source_kind='stock',query='coffee brewing hands',reason='需要具体对象'),
            'preferred_sources':[SourceKind.USER_ASSET,SourceKind.STOCK]})
        windows,_=resolver._windows(p,[scene],{'coffee brewing hands':[stock[0].id]})
        ids={a.id for a,c in windows.values()}
        assert asset.id not in ids
        assert stock[0].id in ids and own[0].id in ids
    finally:db.close()


def test_equally_effective_creator_source_keeps_priority_without_source_quotas(tmp_path):
    db,p,_,_,scene,_,resolver=material_library(tmp_path)
    try:
        sources=[add_source(db,p,tmp_path,k,k.value) for k in (SourceKind.USER_ASSET,SourceKind.STOCK,SourceKind.AI_BROLL)]
        windows=observed_pool(resolver,p,sources,[scene],lambda v,s:(True,.95,'direct'))
        assert resolver._select(p,[scene],windows,None)[0].preferred_clip_id==sources[0][1].id
    finally:db.close()


def test_uploaded_external_material_does_not_gain_creator_preference(tmp_path):
    db,p,_,_,scene,_,resolver=material_library(tmp_path)
    try:
        borrowed=add_source(db,p,tmp_path,SourceKind.USER_ASSET,'uploaded-documentary')
        own=add_source(db,p,tmp_path,SourceKind.USER_ASSET,'creator-work')
        AssetRepository(db).update(borrowed[0].model_copy(update={'metadata':{'external_sources':[{'source':'fixture-public-source'}]}}))
        borrowed=(AssetRepository(db).get(borrowed[0].id),borrowed[1])
        windows=observed_pool(resolver,p,[borrowed,own],[scene],
            lambda v,s:(True,.98 if v['clip_id']==str(borrowed[1].id) else .94,'direct'))
        assert resolver._select(p,[scene],windows,None)[0].preferred_clip_id==own[1].id
    finally:db.close()


def test_joint_selection_saves_later_scenes_only_effective_clip(tmp_path):
    db,p,_,_,base,_,resolver=material_library(tmp_path)
    try:
        shared=add_source(db,p,tmp_path,SourceKind.USER_ASSET,'shared')
        alternative=add_source(db,p,tmp_path,SourceKind.STOCK,'first-only')
        scenes=[base.model_copy(update={'id':uuid4(),'scene_id':name,'order':i,'voice_text':name,
            'duration_target_ms':4000}) for i,name in enumerate(('first','last'))]
        def match(v,s):
            ok=s.order==0 or v['clip_id']==str(shared[1].id)
            return ok,.95 if ok else .1,'direct'
        windows=observed_pool(resolver,p,[shared,alternative],scenes,match)
        selected=resolver._select(p,scenes,windows,None)
        assert [s.preferred_clip_id for s in selected]==[alternative[1].id,shared[1].id]
    finally:db.close()


def test_intentional_reference_can_return_to_same_unaltered_footage(tmp_path):
    db,p,_,_,base,_,resolver=material_library(tmp_path)
    try:
        own=add_source(db,p,tmp_path,SourceKind.USER_ASSET,'reference')
        stock=add_source(db,p,tmp_path,SourceKind.STOCK,'alternative')
        scenes=[base.model_copy(update={'id':uuid4(),'scene_id':str(i),'order':i,'voice_text':str(i),
            'reuse_intent':'reference' if i else 'auto'}) for i in range(2)]
        windows=observed_pool(resolver,p,[own,stock],scenes,lambda v,s:(True,.95,'direct'))
        assert [s.preferred_clip_id for s in resolver._select(p,scenes,windows,None)]==[own[1].id,own[1].id]
    finally:db.close()


def test_auto_scenes_use_distinct_verified_intervals_and_router_keeps_them(tmp_path):
    db,p,_,_,base,_,resolver=material_library(tmp_path)
    try:
        source=add_source(db,p,tmp_path,SourceKind.STOCK,'continuous')
        scenes=[base.model_copy(update={'id':uuid4(),'scene_id':str(i),'order':i,'voice_text':str(i),
            'duration_target_ms':2000,'preferred_clip_id':None}) for i in range(3)]
        windows=observed_pool(resolver,p,[source],scenes,lambda v,s:(True,.95,'direct'))
        selected=resolver._select(p,scenes,windows,None)
        router=AssetRouter(ClipTextSearchService(db),AssetRepository(db))
        starts=[next(c for c in route.candidates if c.recommended).playback_start_ms for route in router.route_all(selected)]
        assert starts==[0,2000,4000]
        facts=full_interval_geometry(AssetRepository(db).get(source[0].id),source[1])
        assert facts['editorial_selections'][scene_visual_key(scenes[-1])]['whole_video_unintentional_repeat_ms']==0
    finally:db.close()


def test_identical_scene_content_keeps_each_scene_playback_interval(tmp_path):
    db,p,_,_,base,_,resolver=material_library(tmp_path)
    try:
        source=add_source(db,p,tmp_path,SourceKind.STOCK,'identical-scenes')
        scenes=[base.model_copy(update={'id':uuid4(),'scene_id':str(i),'order':i,
            'duration_target_ms':2000,'preferred_clip_id':None}) for i in range(2)]
        assert scene_visual_key(scenes[0])==scene_visual_key(scenes[1])
        windows=observed_pool(resolver,p,[source],scenes,lambda v,s:(True,.95,'direct'))
        selected=resolver._select(p,scenes,windows,None)
        router=AssetRouter(ClipTextSearchService(db),AssetRepository(db))
        starts=[next(c for c in route.candidates if c.recommended).playback_start_ms for route in router.route_all(selected)]
        assert starts==[0,2000]
        facts=full_interval_geometry(AssetRepository(db).get(source[0].id),source[1])
        choices=facts['editorial_selections']
        assert [choices[f'{scene_visual_key(s)}:{s.id}']['scene_plan_id'] for s in scenes]==[str(s.id) for s in scenes]
    finally:db.close()


def test_legacy_choice_for_another_scene_does_not_change_playback(tmp_path):
    db,p,_,_,base,_,resolver=material_library(tmp_path)
    try:
        source=add_source(db,p,tmp_path,SourceKind.STOCK,'legacy-scene')
        scene=base.model_copy(update={'duration_target_ms':2000,'preferred_clip_id':None})
        windows=observed_pool(resolver,p,[source],[scene],lambda v,s:(True,.95,'direct'))
        selected=resolver._select(p,[scene],windows,None)[0]
        asset=AssetRepository(db).get(source[0].id)
        metadata=json.loads(json.dumps(asset.metadata))
        choices=metadata['visual_analysis'][str(source[1].id)]['editorial_selections']
        choices.pop(f'{scene_visual_key(scene)}:{scene.id}')
        choices[scene_visual_key(scene)].update({'scene_id':'another-scene','playback_start_ms':2000,'playback_end_ms':6000})
        AssetRepository(db).update(asset.model_copy(update={'metadata':metadata}))
        candidate=next(c for c in AssetRouter(ClipTextSearchService(db),AssetRepository(db)).route(selected).candidates if c.recommended)
        assert candidate.playback_start_ms==0
    finally:db.close()


def test_unavoidable_auto_overlap_remains_visible_in_selection_evidence(tmp_path):
    db,p,_,_,base,_,resolver=material_library(tmp_path)
    try:
        source=add_source(db,p,tmp_path,SourceKind.STOCK,'short')
        scenes=[base.model_copy(update={'id':uuid4(),'scene_id':str(i),'order':i,'voice_text':str(i),
            'duration_target_ms':4000,'preferred_clip_id':None}) for i in range(2)]
        windows=observed_pool(resolver,p,[source],scenes,lambda v,s:(True,.95,'direct'))
        resolver._select(p,scenes,windows,None)
        facts=full_interval_geometry(AssetRepository(db).get(source[0].id),source[1])
        assert facts['editorial_selections'][scene_visual_key(scenes[-1])]['whole_video_unintentional_repeat_ms']==4000
    finally:db.close()


def test_ai_illustration_routes_as_footage_and_assembles_without_talking_admission(tmp_path):
    db,p,_,_,scene,_,resolver=material_library(tmp_path)
    try:
        generated=add_source(db,p,tmp_path,SourceKind.AI_BROLL,'concept')
        scene=scene.model_copy(update={'preferred_clip_id':None,'preferred_sources':[SourceKind.AI_BROLL],
            'viewer_takeaway':'理解信息丢失的示意，不是实测',
            'material_request':MaterialRequest(source_kind='ai_broll',query='信息逐渐减少的粒子流',reason='解释抽象机制')})
        windows=observed_pool(resolver,p,[generated],[scene],lambda v,s:(True,.95,'direct'))
        selected=resolver._select(p,[scene],windows,None)[0]
        assert selected.material_request is None
        route=AssetRouter(ClipTextSearchService(db),AssetRepository(db)).route(selected)
        candidate=next(c for c in route.candidates if c.recommended)
        assert candidate.source_kind is SourceKind.AI_BROLL
        spec=VideoSpecAssembler(AssetRepository(db),ClipRepository(db)).assemble(p,[selected],{selected.id:candidate})
        assert spec.scenes[0].visual.source_kind is SourceKind.AI_BROLL
        assert spec.scenes[0].visual_role.value=='footage'
        from app.renderer import RemotionRenderer, UnauthorizedVisualError
        renderer=RemotionRenderer(AssetRepository(db),ClipRepository(db),renderer_dir=tmp_path)
        assert renderer._validate_scene(spec.scenes[0],p.fps,project_id=p.id,master_audio_id=None).kind=='video'
        assert any('联合比较' in reason for reason in candidate.why)
        changed=selected.model_copy(update={'visual_requirement':'action_evidence','visual_requirement_reason':'要求真实测量'})
        with pytest.raises(InvalidCandidateSelection):
            VideoSpecAssembler(AssetRepository(db),ClipRepository(db)).assemble(p,[changed],{changed.id:candidate})
        current=AssetRepository(db).get(generated[0].id)
        AssetRepository(db).update(current.model_copy(update={'metadata':{}}))
        from app.routing.asset_router import RoutingInputError
        with pytest.raises(RoutingInputError,match='not_admitted'):
            AssetRouter(ClipTextSearchService(db),AssetRepository(db)).explicit_footage(selected,[candidate])
        with pytest.raises(UnauthorizedVisualError,match='model admission'):
            renderer._validate_scene(spec.scenes[0],p.fps,project_id=p.id,master_audio_id=None)
    finally:db.close()


@pytest.mark.parametrize('kind',[SourceKind.AI_BROLL,SourceKind.STOCK])
def test_unresolved_acquisition_stays_a_request_and_preserves_the_claim(tmp_path,kind):
    db,p,_,_,scene,_,resolver=material_library(tmp_path)
    try:
        scene=scene.model_copy(update={'preferred_clip_id':None,'preferred_sources':[kind],
            'material_request':MaterialRequest(source_kind=kind.value,query='所需画面',reason='已有素材不能完成表达')})
        with pytest.raises(MaterialResolutionError,match='material_source_unavailable'):
            resolver._select(p,[scene],{},None)
        assert scene.voice_text=='狗接飞盘时的画面细节。' and scene.material_request is not None
        assert db.connection.execute('SELECT COUNT(*) FROM production_runs').fetchone()[0]==0
    finally:db.close()


def test_contextual_visual_cannot_prove_action_and_task_changes_invalidate_matching(tmp_path):
    db,p,_,_,base,_,resolver=material_library(tmp_path)
    try:
        source=add_source(db,p,tmp_path,SourceKind.STOCK,'context')
        scene=base.model_copy(update={'visual_requirement':'action_evidence','visual_requirement_reason':'展示真实对比',
            'viewer_takeaway':'看清实测差异'})
        windows=observed_pool(resolver,p,[source],[scene],lambda v,s:(True,.98,'context'))
        facts=full_interval_geometry(AssetRepository(db).get(source[0].id),source[1])
        assert not matches_scene(facts,scene)
        with pytest.raises(MaterialResolutionError):resolver._select(p,[scene],windows,None)
        assert scene_visual_key(scene)!=scene_visual_key(scene.model_copy(update={'viewer_takeaway':'另一项结论'}))
    finally:db.close()


def test_generated_illustration_requires_original_model_admission_and_matching_qa(tmp_path):
    db,p,_,_,scene,_,resolver=material_library(tmp_path)
    try:
        generated=add_source(db,p,tmp_path,SourceKind.AI_BROLL,'unknown-model')
        scene=scene.model_copy(update={'preferred_clip_id':generated[1].id,'preferred_sources':[SourceKind.AI_BROLL]})
        windows=observed_pool(resolver,p,[generated],[scene],lambda v,s:(True,.95,'direct'))
        AssetRepository(db).update(AssetRepository(db).get(generated[0].id).model_copy(update={'metadata':{}}))
        with pytest.raises(MaterialResolutionError):resolver._select(p,[scene],windows,None)
        route=AssetRouter(ClipTextSearchService(db),AssetRepository(db)).route(scene)
        assert all(c.source_kind is not SourceKind.AI_BROLL for c in route.candidates)
    finally:db.close()


def test_auto_task_keeps_unavailable_request_and_status_without_starting_production(tmp_path):
    from app.costs import UnknownProviderCostEstimator
    from app.db import ProjectDraftRepository
    from app.domain.models import Job, ContentPlanJobPayload, JobType
    from app.jobs.store import JobStore
    from app.video_production import VideoProductionService
    from app.providers.scene_planner import ScenePlanResult
    db,p,_,_,base,provider,resolver=material_library(tmp_path)
    try:
        now=datetime.now(timezone.utc)
        job=Job(project_id=p.id,type=JobType.PLAN_CONTENT,idempotency_key='missing-material',created_at=now,updated_at=now,
            payload=ContentPlanJobPayload(project_id=p.id,topic=p.topic))
        JobStore(db).enqueue(job)
        original=provider.analyze_intervals
        def analyze(videos,requests):
            result=original(videos,requests)
            for row in result.clips:
                row.content_consistent=True
                for match in row.matches:match.supported=False
            return result
        provider.analyze_intervals=analyze
        request=MaterialRequest(source_kind='stock',query='宇宙星云的实际观测画面',reason='展示对象，不用相机空镜替代')
        class Planner:
            def plan(self,project,**kwargs):
                return ScenePlanResult(project.id,(base.model_copy(update={'preferred_sources':[SourceKind.STOCK],
                    'preferred_clip_id':None,'voice_text':'星云的结构来自不同波段的观测。',
                    'viewer_takeaway':'看清真实观测对象','material_request':request}),))
        service=VideoProductionService(db,tmp_path,material_resolver=resolver)
        with pytest.raises(MaterialResolutionError,match='material_source_unavailable'):
            service.plan(job,Planner(),UnknownProviderCostEstimator())
        draft=ProjectDraftRepository(db).get(p.id)
        assert draft.scenes[0].material_request==request
        assert draft.script=='星云的结构来自不同波段的观测。'
        status=service.status(p.id)
        assert status['material_requests'][0]['query']==request.query
        assert status['material_requests'][0]['source_kind']=='stock'
        assert db.connection.execute('SELECT COUNT(*) FROM production_runs').fetchone()[0]==0
        assert db.connection.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==1
    finally:db.close()


@pytest.mark.parametrize('kind,requirement,accepted',[
    ('stock','action_evidence',True),('ai_broll','explanatory',True),('ai_broll','action_evidence',False)])
def test_planner_can_request_visuals_beyond_catalogue_without_inventing_available_clips(tmp_path,kind,requirement,accepted):
    import json
    from app.providers.scene_planner import (OpenAICompatibleScenePlanner,ScenePlannerHTTPResponse,
        ScenePlannerProviderResponseError,_SCENE_SCHEMA)
    db,p,_,_,base,_,_=material_library(tmp_path)
    try:
        fields=_SCENE_SCHEMA['properties']
        def output(scene):
            value={k:v for k,v in scene.model_dump(mode='json').items() if k in fields}
            value['visual_intent'].pop('schema_version',None)
            return value
        creator=output(base.model_copy(update={'scene_id':'creator','order':0,'voice_text':'先看星云',
            'duration_target_ms':1600,'preferred_sources':[SourceKind.AI_VIDEO],'fallback_sources':[],
            'preferred_clip_id':None,'visual_requirement':'creator_speaking','visual_requirement_reason':'已指定本人讲解'}))
        support=output(base.model_copy(update={'scene_id':'nebula','order':1,'preferred_sources':[SourceKind(kind)],
            'fallback_sources':[],'preferred_clip_id':None,'voice_text':'星云的结构来自不同波段的观测。',
            'viewer_takeaway':'看清观测结构','visual_requirement':requirement,'visual_requirement_reason':'展示对象',
            'material_request':MaterialRequest(source_kind=kind,query='所需的星云画面',reason='已有目录不含对应对象')}))
        calls=[]
        class Transport:
            def post(self,url,headers,body,timeout_seconds):
                calls.append(json.loads(body))
                return ScenePlannerHTTPResponse(200,json.dumps({'choices':[{'message':{'content':json.dumps(
                    {'creator_scene':creator,'scenes':[support]})}}]}).encode(),{})
        planner=OpenAICompatibleScenePlanner('fixture',protocol='chat_completions',chat_output_mode='json_schema',transport=Transport())
        project=p.model_copy(update={'presentation_style':'creator_led'})
        context={'materials':[],'continuous_production':{'talking_motion_budget':{
            'planned_visible_ms_max':2400,'approx_chinese_characters_max':7}}}
        if accepted:
            result=planner.plan(project,context=context)
            assert result.scenes[1].preferred_clip_id is None
            assert result.scenes[1].material_request.source_kind==kind
            assert result.scenes[1].voice_text==support['voice_text']
        else:
            with pytest.raises(ScenePlannerProviderResponseError):planner.plan(project,context=context)
        assert len(calls)==1
    finally:db.close()
