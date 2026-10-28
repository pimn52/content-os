"""真实产品编排的契约夹具；替身核验不代表真实素材质量。"""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4
import hashlib

import pytest

from app.costs import UnknownProviderCostEstimator
from app.db import (AssetRepository, ClipRepository, Database, IPProfileRepository, ProjectRepository,
    BudgetPolicyRepository, ProviderCallRepository, ProjectDraftRepository)
from app.domain.models import Asset, Clip, IPProfile, Project, RationalFps, ScenePlan, VisualIntent, BudgetPolicy
from app.media.planning import VisualMaterialResolver, MaterialResolutionError, split_footage_clauses, subtitle_clearance
from app.media.visual_facts import full_interval_geometry, scene_visual_key, matches_scene
from app.providers.vision import VideoIntervalBatch, VideoIntervalAnalysis, SceneVisualMatch


class FixtureVideoExtractor:
    def __init__(self, root): self.root = root; self.calls = []
    def extract_video_interval(self, asset, start, end):
        self.calls.append((asset.id,start,end))
        path = self.root/asset.content_hash/f'{start}-{end}.mp4'
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(b'\x00\x00\x00\x18ftypisom'+b'fixture proxy')
        return path


class FixtureVideoProbe:
    def probe(self, path):
        start,end = map(int,Path(path).stem.split('-'))
        return SimpleNamespace(duration_ms=end-start,width=1080,height=1920,has_audio=False)


class FixtureVideoVision:
    supports_video_input = True
    def __init__(self, accept=None, steps=None, description='狗在草地接飞盘的码率对比'):
        self.calls=[]; self.accept=accept or (lambda item:item['start_ms'] >= 20000); self.steps=steps; self.description=description
    def analyze_intervals(self, videos, scenes):
        self.calls.append((videos,scenes))
        if self.steps is not None: self.steps.append('material' if scenes else 'catalogue')
        return VideoIntervalBatch(clips=[VideoIntervalAnalysis(clip_id=v['clip_id'],
            description=self.description if self.accept(v) else '另一场景的手机画面',
            content_consistent=True,comparison_layout='none',subtitle_regions=[],protected_regions=[],
            matches=[SceneVisualMatch(scene_key=s['scene_key'],supported=self.accept(v),score=.95 if self.accept(v) else .1,
                fit='direct',quality_adequate=True,subject_verified=True,reason='测试替身结论，非真实素材判断') for s in scenes],reason='契约夹具') for v in videos])


def material_library(tmp_path, *, metered=False):
    db=Database(tmp_path/'material.sqlite3'); db.data_root=tmp_path
    now=datetime.now(timezone.utc)
    profile=IPProfileRepository(db).create(IPProfile(creator_name='创作者'))
    project=ProjectRepository(db).create(Project(ip_profile_id=profile.id,title='画质科普',topic='狗接飞盘码率',
        created_at=now,fps=RationalFps(numerator=30,denominator=1)))
    source=tmp_path/'source.mp4';source.write_bytes(b'immutable fixture source')
    asset=AssetRepository(db).create(Asset(source_file=str(source),content_hash=hashlib.sha256(source.read_bytes()).hexdigest(),
        duration_ms=30000,width=1080,height=1920,fps=project.fps,authorization_reference='入库授权',imported_at=now))
    coarse=ClipRepository(db).create(Clip(asset_id=asset.id,start_ms=0,end_ms=30000,asset_duration_ms=30000,
        visual_description='狗接飞盘码率对比'))
    ClipRepository(db).create(Clip(asset_id=asset.id,start_ms=10000,end_ms=20000,asset_duration_ms=30000))
    ClipRepository(db).create(Clip(asset_id=asset.id,start_ms=20000,end_ms=30000,asset_duration_ms=30000,
        visual_description='狗接飞盘码率对比实拍'))
    asset=AssetRepository(db).update(asset.model_copy(update={'metadata':{'visual_analysis':{str(coarse.id):dict(
        source_content_hash=asset.content_hash,clip_interval=dict(start_ms=0,end_ms=30000),coverage='sampled_keyframe',
        observations=[dict(timestamp_ms=19500,description='采样画面描述')])}}}))
    scene=ScenePlan(project_id=project.id,scene_id='demo',order=0,purpose='解释画质',voice_text='狗接飞盘时的画面细节。',
        duration_target_ms=3000,visual_intent=VisualIntent(subject='狗接飞盘码率',action='展示细节'),
        preferred_sources=['user_asset'],fallback_sources=['capture'],preferred_clip_id=coarse.id)
    provider=FixtureVideoVision()
    if metered:
        provider.provider_name='contract-fixture';provider.model='fixture-video'
        BudgetPolicyRepository(db).save(BudgetPolicy(project_id=project.id,max_calls=12,allow_unknown_cost=True,updated_at=now))
    resolver=VisualMaterialResolver(db,tmp_path,provider,FixtureVideoExtractor(tmp_path/'proxies'),FixtureVideoProbe(),UnknownProviderCostEstimator())
    return db,project,asset,coarse,scene,provider,resolver


def test_video_timeout_keeps_failed_call_without_saving_qa_or_retry(tmp_path):
    from app.providers.vision import VisionTimeout
    db,project,asset,coarse,scene,provider,resolver=material_library(tmp_path,metered=True)
    attempted=[]
    def timeout(videos,scenes):
        attempted.append(videos)
        raise VisionTimeout('vision request timed out')
    provider.analyze_intervals=timeout
    try:
        with pytest.raises(VisionTimeout):resolver.resolve(project,[scene],uuid4())
        calls=ProviderCallRepository(db).list_for_project(project.id)
        assert len(attempted)==len(calls)==1
        assert calls[0].status=='failed' and calls[0].error_code=='material_video_analysis_timeout'
        assert all(fact.get('coverage')!='video_interval_analysis'
            for fact in AssetRepository(db).get(asset.id).metadata['visual_analysis'].values())
    finally:db.close()


def test_broll_cannot_consume_motion_reserved_by_the_same_measured_plan(tmp_path):
    from app.source_usage import SourceUsageService
    db,project,asset,_,scene,provider,resolver=material_library(tmp_path)
    try:
        reserved=[{**use,'content_changed':True,'repeatable':False} for use in
            SourceUsageService(db).resolve(asset.id,0,asset.duration_ms,'person')]
        with pytest.raises(MaterialResolutionError) as failure:
            resolver.resolve(project,[scene],uuid4(),reserved_source_uses=reserved)
        assert failure.value.reasons==('material_scene_unresolved:demo',)
        assert any('talking_motion_reserved' in reasons for reasons in failure.value.candidate_rejections['demo'].values())
    finally:db.close()


def test_actual_video_windows_replace_wrong_coarse_interval_and_keep_qa(tmp_path):
    db,project,asset,coarse,scene,provider,resolver=material_library(tmp_path,metered=True)
    try:
        job_id=uuid4()
        selected=resolver.resolve(project,[scene],job_id)[0]
        clip=ClipRepository(db).get(selected.preferred_clip_id)
        assert clip.id != coarse.id and clip.start_ms == 20000
        assert len(provider.calls) == 1 and len(provider.calls[0][0]) >= 2
        facts=full_interval_geometry(AssetRepository(db).get(asset.id),clip)
        assert facts['coverage'] == 'video_interval_analysis'
        assert facts['content_consistent'] is True and matches_scene(facts,scene)
        assert not matches_scene(facts,scene.model_copy(update={'voice_text':'没有拍摄过的水下实验。'}))
        assert not matches_scene(facts,scene.model_copy(update={'caption_emphasis':['实测画质提升十倍']}))
        assert matches_scene(facts,scene.model_copy(update={'duration_target_ms':3200}))
        assert ClipRepository(db).get(coarse.id) == coarse
        assert len(ProviderCallRepository(db).list_for_project(project.id)) == 1
        # 同一任务恢复复用实际核验结果，不再次收费，也不使用另一套候选。
        replay=resolver.resolve(project,[scene],job_id)
        assert replay[0].preferred_clip_id == selected.preferred_clip_id and len(provider.calls) == 1
        assert len(ProviderCallRepository(db).list_for_project(project.id)) == 1
    finally: db.close()


@pytest.mark.parametrize('quality', [True, False, None, 'missing'])
def test_machine_quality_requires_explicit_acceptance_across_consumers_without_reanalysis(tmp_path, quality):
    from copy import deepcopy
    from app.assembly.video_spec import VideoSpecAssembler, InvalidCandidateSelection
    from app.routing import AssetRouter
    from app.routing.asset_router import RoutingInputError
    from app.search import ClipTextSearchService
    db,project,asset,_,scene,provider,resolver=material_library(tmp_path,metered=True)
    try:
        job_id=uuid4()
        selected=resolver.resolve(project,[scene],job_id)[0]
        router=AssetRouter(ClipTextSearchService(db),AssetRepository(db))
        candidate=next(c for c in router.route(selected).candidates if c.recommended)
        current=AssetRepository(db).get(asset.id)
        metadata=deepcopy(current.metadata)
        key=scene_visual_key(scene)
        for facts in metadata['visual_analysis'].values():
            match=facts.get('scene_matches',{}).get(key)
            if match is None:continue
            if quality=='missing':match.pop('quality_adequate',None)
            else:match['quality_adequate']=quality
        AssetRepository(db).update(current.model_copy(update={'metadata':metadata}))
        for _ in range(2):
            if quality is True:
                assert resolver.resolve(project,[scene],job_id)[0].preferred_clip_id==selected.preferred_clip_id
                assert router.explicit_footage(selected,[candidate])==candidate
                assert VideoSpecAssembler(AssetRepository(db),ClipRepository(db)).assemble(project,[selected],{selected.id:candidate})
            else:
                with pytest.raises(MaterialResolutionError):resolver.resolve(project,[scene],job_id)
                with pytest.raises(RoutingInputError):router.explicit_footage(selected,[candidate])
                with pytest.raises(InvalidCandidateSelection):
                    VideoSpecAssembler(AssetRepository(db),ClipRepository(db)).assemble(project,[selected],{selected.id:candidate})
        # 未知保持原事实，不能改写为明确失败，也不能为恢复任务重复调用同一次核验。
        kept=AssetRepository(db).get(asset.id).metadata['visual_analysis'][str(candidate.clip_id)]['scene_matches'][key]
        if quality=='missing':assert 'quality_adequate' not in kept
        else:assert kept['quality_adequate'] is quality
        assert len(provider.calls)==1 and len(ProviderCallRepository(db).list_for_project(project.id))==1
    finally:db.close()


def test_subtitle_conflict_switches_to_safe_candidate_and_unknown_never_passes(tmp_path):
    db,project,asset,coarse,scene,provider,resolver=material_library(tmp_path)
    try:
        original=provider.analyze_intervals
        def captioned(videos,scenes):
            result=original(videos,scenes)
            for row,video in zip(result.clips,videos):
                row.matches=[SceneVisualMatch(scene_key=s['scene_key'],supported=True,score=.95,quality_adequate=True,reason='fixture') for s in scenes]
                row.subtitle_regions=[[0,.5,1,.1]] if video['start_ms'] < 20000 else [[0,.9,1,.08]]
                row.protected_regions=[[.1,.1,.5,.5]]
            return result
        provider.analyze_intervals=captioned
        chosen=resolver.resolve(project,[scene],uuid4())[0]
        clip=ClipRepository(db).get(chosen.preferred_clip_id)
        assert clip.start_ms >= 20000
        facts=full_interval_geometry(AssetRepository(db).get(asset.id),clip)
        assert facts['source_subtitle_clearance_verified'] is True
        assert not subtitle_clearance(None,[],720)
        assert not subtitle_clearance([[0,.9,1,.08]],None,720)
        assert not subtitle_clearance([[0,.9,1,.08]],[[0,.7,.5,.25]],720)
    finally: db.close()


def test_all_candidates_rejected_stops_before_run_voice_and_lip_sync(tmp_path):
    from app.video_production import VideoProductionService
    from app.providers.scene_planner import ScenePlanResult
    db,project,asset,coarse,scene,provider,resolver=material_library(tmp_path)
    try:
        provider.accept=lambda v:False
        class Planner:
            def plan(self, p, **kwargs):
                revision=kwargs['context'].get('visual_revision')
                if revision:
                    # 合法的无变化调整仍返回原场景失败；夹具不改写已冻结的表达要求。
                    return ScenePlanResult(p.id,tuple(ScenePlan.model_validate(value) for value in revision['scenes']))
                selected=UUID(kwargs['context']['materials'][0]['clip_id'])
                return ScenePlanResult(p.id,(scene.model_copy(update={'preferred_clip_id':selected}),))
        service=VideoProductionService(db,tmp_path,material_resolver=resolver)
        job=SimpleNamespace(project_id=project.id,id=uuid4(),payload=SimpleNamespace(topic=project.topic,candidate_of_run_id=None))
        with pytest.raises(MaterialResolutionError,match='material_scene_unresolved'):
            service.plan(job,Planner(),UnknownProviderCostEstimator())
        assert db.connection.execute('SELECT COUNT(*) FROM production_runs').fetchone()[0] == 0
        assert db.connection.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0
        assert f'content_plan_job:{job.id}' in ProjectDraftRepository(db).get(project.id).evidence_refs
        # 修复或恢复不重跑原脚本规划；原任务保留确切负面结果。
        with pytest.raises(MaterialResolutionError): service.plan(job,object(),UnknownProviderCostEstimator())
        assert len(provider.calls) == 2
    finally: db.close()


def test_long_scene_splits_only_at_existing_clauses_without_losing_copy(tmp_path):
    db,project,asset,coarse,scene,provider,resolver=material_library(tmp_path)
    try:
        original='先看狗接飞盘，观察草地细节。再比较高速运动，不要只看文件大小。'
        scene=scene.model_copy(update={'voice_text':original,'duration_target_ms':15000})
        parts=split_footage_clauses([scene])
        assert len(parts)>1 and ''.join(p.voice_text for p in parts) == original
        assert sum(p.duration_target_ms for p in parts) == 15000
        assert all(p.preferred_clip_id == coarse.id for p in parts)
        assert [p.order for p in parts] == list(range(len(parts)))
        assert split_footage_clauses([scene.model_copy(update={'voice_text':'一个没有标点的完整长句'})])[0].voice_text == '一个没有标点的完整长句'
    finally: db.close()


def test_candidate_window_stays_inside_short_known_shot_even_with_timing_margin(tmp_path):
    from app.media.vision import planning_visual_evidence
    db,project,asset,coarse,scene,provider,resolver=material_library(tmp_path)
    try:
        clips=ClipRepository(db)
        middle=next(c for c in clips.list_by_asset(asset.id) if c.start_ms == 10000)
        clips.upsert(middle.model_copy(update={'start_ms':17000}))
        windows,_=resolver._windows(project,[scene])
        intervals=[(c.start_ms,c.end_ms) for _,c in windows.values()]
        assert (17000,20000) in intervals  # 够用的短镜头保留，语速余量不得跨镜头。
        assert all(any(start <= a < b <= end for start,end in [(17000,20000),(20000,30000)]) for a,b in intervals)
        evidence=planning_visual_evidence(db,asset,coarse)
        assert evidence['local_interval_hints'] == [dict(timestamp_ms=19500,start_ms=17000,end_ms=20000,duration_ms=3000)]
        assert evidence['coverage'] == 'sampled_keyframe'
        # 几何切点不等于语义切点；长候选保留供视频核验，但不能凭时长直接通过。
        longer=scene.model_copy(update={'duration_target_ms':3500})
        remaining,_=resolver._windows(project,[longer])
        assert any(c.start_ms == 17000 and c.end_ms > 20000 for _,c in remaining.values())
        selected=resolver.resolve(project,[longer],uuid4())[0]
        assert clips.get(selected.preferred_clip_id).start_ms >= 20000
        assert len(provider.calls) == 1
    finally: db.close()


def test_analysis_request_binds_caption_text_to_semantic_cache(tmp_path):
    db,project,asset,coarse,scene,provider,resolver=material_library(tmp_path)
    try:
        scene=scene.model_copy(update={'caption_emphasis':['观察草地细节']})
        selected=resolver.resolve(project,[scene],uuid4())[0]
        requests=provider.calls[0][1]
        assert requests[0]['caption_emphasis'] == ['观察草地细节']
        assert requests[0]['visual_requirement_reason'] == scene.visual_requirement_reason
        clip=ClipRepository(db).get(selected.preferred_clip_id)
        facts=full_interval_geometry(AssetRepository(db).get(asset.id),clip)
        assert matches_scene(facts,scene)
        assert not matches_scene(facts,scene.model_copy(update={'caption_emphasis':['实测清晰度提升十倍']}))
    finally: db.close()


@pytest.mark.parametrize('invalid_first_hint',[False,True])
@pytest.mark.parametrize('keep_preferred',[False,True])
def test_external_files_each_get_valid_window_before_same_file_extras_and_recover_without_new_call(
        tmp_path,invalid_first_hint,keep_preferred):
    from app.domain.models import MaterialRequest, SourceKind
    db,project,base,coarse,scene,provider,resolver=material_library(tmp_path,metered=True)
    try:
        assets=[]
        for name in ('external-a','external-b'):
            path=tmp_path/(name+'.mp4');path.write_bytes(name.encode())
            assets.append(AssetRepository(db).create(base.model_copy(update={'id':uuid4(),'source_file':str(path),
                'content_hash':hashlib.sha256(path.read_bytes()).hexdigest(),'duration_ms':9000,
                'source_kind':SourceKind.STOCK,'metadata':{}})))
        intervals=[(0,1000),(1000,4000),(4000,7000)] if invalid_first_hint else [(0,3000),(3000,6000),(6000,9000)]
        for start,end in intervals:
            ClipRepository(db).create(Clip(asset_id=assets[0].id,start_ms=start,end_ms=end,asset_duration_ms=9000))
        ClipRepository(db).create(Clip(asset_id=assets[1].id,start_ms=0,end_ms=9000,asset_duration_ms=9000))
        query='coffee grinder demonstration'
        scene=scene.model_copy(update={'duration_target_ms':2000,'preferred_clip_id':coarse.id if keep_preferred else None,
            'visual_intent':VisualIntent(subject='咖啡磨豆萃取',action='磨豆'),
            'material_request':MaterialRequest(source_kind='stock',query=query,reason='明确的外取画面需求')})
        acquired={query:[asset.id for asset in assets]}
        windows,_=resolver._windows(project,[scene],acquired)
        assert {asset.id for asset in assets}<={asset.id for asset,_ in windows.values()}
        assert len(windows)<=3 and all(clip.end_ms-clip.start_ms>=2000 for _,clip in windows.values())
        if keep_preferred:assert base.id in {asset.id for asset,_ in windows.values()}
        if invalid_first_hint:
            assert all(clip.start_ms>=1000 for asset,clip in windows.values() if asset.id==assets[0].id)
        provider.accept=lambda item:True
        job_id=uuid4()
        selected=resolver.resolve(project,[scene],job_id,acquired_assets=acquired)
        assert {str(asset.id) for asset in assets}<={video['source_asset_id'] for video in provider.calls[0][0]}
        # 恢复沿已保存的候选集合，不能因外取池重排而换片、再次核验或重复记账。
        assert resolver.resolve(project,[scene],job_id,acquired_assets={query:list(reversed(acquired[query]))})==selected
        assert len(provider.calls)==1 and len(ProviderCallRepository(db).list_for_project(project.id))==1
    finally:db.close()


def test_only_unverified_windows_are_billed_and_full_selection_survives_recovery(tmp_path):
    db,project,asset,_,scene,provider,resolver=material_library(tmp_path)
    try:
        selected=resolver.resolve(project,[scene],uuid4())[0]
        windows,_=resolver._windows(project,[scene])
        unknown=next(identity for identity in windows if identity!=str(selected.preferred_clip_id))
        stored=AssetRepository(db).get(asset.id)
        metadata=dict(stored.metadata);facts=dict(metadata["visual_analysis"])
        facts[unknown]={**facts[unknown],"scene_matches":{}}
        metadata["visual_analysis"]=facts
        AssetRepository(db).update(stored.model_copy(update={"metadata":metadata}))
        provider.calls.clear();job_id=uuid4()
        current=resolver.resolve(project,[scene],job_id)[0]
        assert current.preferred_clip_id==selected.preferred_clip_id
        assert len(provider.calls)==0  # 已有合格整片方案时，不为未选备选重复付费。
        replay=resolver.resolve(project,[scene],job_id)[0]
        assert replay.preferred_clip_id==selected.preferred_clip_id
        assert len(provider.calls)==0
    finally:db.close()


def test_planned_observed_interval_is_not_resliced_when_scene_duration_is_shorter(tmp_path):
    db,project,_,_,base,provider,resolver=material_library(tmp_path)
    try:
        provider.accept=lambda video:True
        prepared=resolver.prepare(project,project.topic,uuid4())
        clip=ClipRepository(db).get(prepared[0])
        scene=base.model_copy(update={'preferred_clip_id':clip.id,'duration_target_ms':2000})
        windows,_=resolver._windows(project,[scene])
        assert str(clip.id) in windows
        observed=windows[str(clip.id)][1]
        assert (observed.start_ms,observed.end_ms)==(clip.start_ms,clip.end_ms)
        assert observed.end_ms-observed.start_ms>scene.duration_target_ms
        chosen=resolver.resolve(project,[scene],uuid4())[0]
        assert chosen.preferred_clip_id in {window.id for _,window in windows.values()}
        # 后续同一用途直接读原事实，不能因重新截取而再核一次。
        calls=len(provider.calls)
        assert resolver.resolve(project,[scene],uuid4())[0].preferred_clip_id==chosen.preferred_clip_id
        assert len(provider.calls)==calls
    finally:db.close()


def test_interval_resolution_enforces_amount_before_model_and_records_observed_usage(tmp_path):
    from datetime import date
    from decimal import Decimal
    from app.budget import BudgetLimitError
    from app.costs import CatalogProviderCostEstimator, ProviderUsage
    from app.domain.models import CostCategory, UsageCost
    db,project,_,_,scene,provider,resolver=material_library(tmp_path,metered=True)
    try:
        BudgetPolicyRepository(db).save(BudgetPolicy(project_id=project.id,currency="CNY",max_amount=Decimal("0.10"),
            max_calls=8,updated_at=datetime.now(timezone.utc)))
        def estimator(amount):
            return CatalogProviderCostEstimator({("vision",provider.provider_name,provider.model):
                UsageCost(category=CostCategory.LLM,amount=Decimal(amount),currency="CNY")})
        resolver.cost_estimator=estimator("0.11")
        with pytest.raises(BudgetLimitError):resolver.resolve(project,[scene],uuid4())
        assert not provider.calls and not ProviderCallRepository(db).list_for_project(project.id)
        observed=[]
        def usage(error=None):
            observed.append(error)
            return ProviderUsage(actual_cost=UsageCost(category=CostCategory.LLM,amount=Decimal("0.02"),currency="CNY"),
                input_tokens=1000,output_tokens=100,cache_tokens=200,usage_observable=True,price_date=date(2026,10,25))
        provider.usage_accounting=usage;resolver.cost_estimator=estimator("0.08")
        job_id=uuid4()
        selected=resolver.resolve(project,[scene],job_id)
        record=ProviderCallRepository(db).list_for_project(project.id)[0]
        assert record.actual_cost.amount==Decimal("0.02")
        assert (record.input_tokens,record.output_tokens,record.cache_tokens)==(1000,100,200)
        assert resolver.resolve(project,[scene],job_id)==selected
        assert len(provider.calls)==len(observed)==1
    finally:db.close()


def test_one_malformed_candidate_keeps_good_selection_and_persists_failure_evidence_without_retry(tmp_path):
    import json
    from app.providers.vision import OpenAICompatibleVisionProvider,VisionHTTPResponse
    db,project,asset,coarse,scene,fixture,resolver=material_library(tmp_path,metered=True)
    class Transport:
        calls=0
        def post(self,url,headers,body,timeout_seconds):
            self.calls+=1
            videos=[]
            for item in json.loads(body)['messages'][0]['content']:
                if item['type']!='text':continue
                try: label=json.loads(item['text'])
                except ValueError: continue
                if isinstance(label,dict) and 'clip_id' in label:videos.append(label)
            batch=fixture.analyze_intervals(videos,[{'scene_key':scene_visual_key(scene)}]).model_dump(mode='json')
            bad=batch['clips'][0]
            bad['matches'][0].update(supported=True,score=1.0)
            bad['protected_regions']=[{'left':.65,'top':.05,'right':.8,'0.120':.12}]
            self.bad_id=bad['clip_id']
            response={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(batch)}}],
                'usage':{'prompt_tokens':100,'completion_tokens':30,'total_tokens':130}}
            return VisionHTTPResponse(200,json.dumps(response).encode(),{})
    transport=Transport()
    resolver.provider=OpenAICompatibleVisionProvider('fixture-secret',model='fixture-video',protocol='chat_completions',
        video_input=True,transport=transport)
    try:
        job_id=uuid4()
        selected=resolver.resolve(project,[scene],job_id)[0]
        assert ClipRepository(db).get(selected.preferred_clip_id).start_ms==20000 and transport.calls==1
        facts=AssetRepository(db).get(asset.id).metadata['visual_analysis'][transport.bad_id]
        assert not facts['content_consistent'] and not facts['source_subtitle_clearance_verified']
        assert facts['analysis_valid'] is False and facts['coverage']=='invalid_video_interval_analysis'
        assert facts['scene_matches']=={} and facts['analysis_invalid_fields']==['protected_regions']
        assert not any(match['supported'] for match in facts['scene_matches'].values())
        call=ProviderCallRepository(db).list_for_project(project.id)[0]
        recorded=call.result_payload['result']
        assert call.status=='completed' and recorded['provider_usage']['total_tokens']==130
        assert recorded['validation_issues'][0]['clip_id']==transport.bad_id
        assert facts['analysis_call_id']==str(call.id)
        assert recorded['validation_issues'][0]['raw_output']['protected_regions'][0].get('bottom') is None
        assert resolver.resolve(project,[scene],job_id)[0].preferred_clip_id==selected.preferred_clip_id
        assert transport.calls==1 and len(ProviderCallRepository(db).list_for_project(project.id))==1
    finally:db.close()


def test_catalogue_prepares_actual_short_intervals_before_writing_and_keeps_scope_on_resume(tmp_path):
    from app.media.planning import estimated_material_calls
    from app.scene_planning import _scene_planning_context
    db,project,asset,coarse,scene,provider,resolver=material_library(tmp_path,metered=True)
    try:
        assert estimated_material_calls(db,project,project.topic)==2
        job_id=uuid4()
        prepared=resolver.prepare(project,project.topic,job_id)
        assert prepared and len(provider.calls)==1 and provider.calls[0][1]==[]
        context,refs=_scene_planning_context(db,project,topic=project.topic,material_clip_ids=prepared)
        assert {item['clip_id'] for item in context['materials']}=={str(v) for v in prepared}
        assert str(coarse.id) not in {item['clip_id'] for item in context['materials']}
        for item in context['materials']:
            evidence=item['visual_evidence'];scope=evidence['usable_interval']
            assert scope['duration_ms']<=6000 and scope['duration_ms']==item['end_ms']-item['start_ms']
            assert item['summary']==evidence['visual_description']
            assert evidence['source_hash_bound'] and evidence['evidence_reference'] in refs
            facts=AssetRepository(db).get(asset.id).metadata['visual_analysis'][item['clip_id']]
            assert facts['scene_matches']=={}  # 看过原片不等于认可尚未生成的脚本。
        assert estimated_material_calls(db,project,project.topic)==1
        # 检索内容变化不能让同一任务恢复时换一批素材或重复收费。
        ClipRepository(db).create(Clip(asset_id=asset.id,start_ms=5000,end_ms=9000,asset_duration_ms=asset.duration_ms,
            visual_description='狗接飞盘码率的新检索线索'))
        assert resolver.prepare(project,project.topic,job_id)==prepared and len(provider.calls)==1
        assert ClipRepository(db).get(coarse.id)==coarse
        assert len(ProviderCallRepository(db).list_for_project(project.id))==1
    finally:db.close()


def test_unusable_catalogue_keeps_other_production_options_and_does_not_repay_on_resume(tmp_path):
    from app.scene_planning import _scene_planning_context
    db,project,asset,coarse,scene,provider,resolver=material_library(tmp_path,metered=True)
    original=provider.analyze_intervals
    def inconsistent(videos,requests):
        result=original(videos,requests)
        for item in result.clips:item.content_consistent=False
        return result
    provider.analyze_intervals=inconsistent
    try:
        job_id=uuid4()
        for _ in range(2):
            prepared=resolver.prepare(project,project.topic,job_id)
            assert prepared==()
            context,_=_scene_planning_context(db,project,topic=project.topic,material_clip_ids=prepared)
            assert context['materials']==[] and context['production_feasibility']
        assert len(provider.calls)==1 and len(ProviderCallRepository(db).list_for_project(project.id))==1
        assert ProjectDraftRepository(db).get(project.id) is None
        assert db.connection.execute('SELECT COUNT(*) FROM production_runs').fetchone()[0]==0
        assert db.connection.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==0
    finally:db.close()
