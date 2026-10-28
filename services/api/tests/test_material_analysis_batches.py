"""分批动态核验与明确恢复；全部为本地契约夹具，无真实模型调用。"""
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app.budget import BudgetLimitError
from app.db import AssetRepository, BudgetPolicyRepository, ProviderCallRepository
from app.domain.models import BudgetPolicy, Clip
from app.media.planning import MaterialResolutionError, VisualMaterialResolver
from app.media.visual_facts import scene_visual_key
from app.provider_execution import ProviderExecutionRecordedFailure
from app.providers.vision import VisionTimeout
from test_material_resolution import material_library


def scope(resolver, asset, base, *, count=8, duration=3000, step=3000, scene_count=1):
    scenes=tuple(base.model_copy(update={"id":uuid4(),"scene_id":f"part-{index}","order":index,
        "purpose":f"演示第{index}项", "voice_text":f"这是第{index}个完整表达。", "duration_target_ms":2000,
        "preferred_clip_id":None}) for index in range(scene_count))
    windows={str(identity):(asset,Clip(id=identity,asset_id=asset.id,start_ms=index*step,
        end_ms=index*step+duration,asset_duration_ms=asset.duration_ms))
        for index,identity in enumerate(uuid4() for _ in range(count))}
    requests=[{"scene_key":scene_visual_key(scene),"voice_text":scene.voice_text,"purpose":scene.purpose,
        "visual_intent":scene.visual_intent.model_dump(mode="json"),"visual_requirement":scene.visual_requirement,
        "visual_requirement_reason":scene.visual_requirement_reason} for scene in scenes]
    resolver._windows=lambda *a,**k:(windows,requests)
    return scenes,windows


def test_first_complete_batch_stops_without_extracting_or_analyzing_unused_candidates(tmp_path):
    db,project,asset,_,base,provider,resolver=material_library(tmp_path,metered=True)
    try:
        scenes,windows=scope(resolver,asset,base)
        provider.accept=lambda video:True
        chosen=resolver.resolve(project,scenes,uuid4())
        assert chosen[0].preferred_clip_id in {clip.id for _,clip in list(windows.values())[:3]}
        assert len(provider.calls)==1 and len(provider.calls[0][0])==3
        assert len(resolver.extractor.calls)==3
    finally:db.close()


@pytest.mark.parametrize("count,duration,step,file_bytes,expected", [
    (8,3000,3000,40,[3,3,2]), (3,10000,1000,40,[2,1]), (3,3000,3000,7*1024*1024,[1,1,1]),
])
def test_batches_bound_count_full_duration_and_binary_bytes(tmp_path,count,duration,step,file_bytes,expected):
    db,_,asset,_,base,_,resolver=material_library(tmp_path)
    try:
        _,windows=scope(resolver,asset,base,count=count,duration=duration,step=step)
        extract=resolver.extractor.extract_video_interval
        def sized(*args):
            path=extract(*args)
            with path.open("r+b") as stream:stream.truncate(file_bytes)
            return path
        resolver.extractor.extract_video_interval=sized
        batches=list(resolver._batches(windows))
        assert [len(videos) for _,videos in batches]==expected
        passed=[]
        for _,videos in batches:
            assert sum(video["proxy_duration_ms"] for video in videos)<=24000
            assert sum(Path(video["path"]).stat().st_size for video in videos)<=12*1024*1024
            assert all(video["end_ms"]-video["start_ms"]==duration for video in videos)
            passed.extend(video["clip_id"] for video in videos)
        assert passed==list(windows)  # 原区间完整上传，未改成抽帧或短截断。
    finally:db.close()


def test_partial_success_survives_failure_and_only_explicit_attempt_reissues_failed_batch(tmp_path):
    db,project,asset,_,base,provider,resolver=material_library(tmp_path,metered=True)
    try:
        scenes,windows=scope(resolver,asset,base,scene_count=4)
        keys=[scene_visual_key(scene) for scene in scenes]
        original=provider.analyze_intervals
        provider.accept=lambda video:True
        attempts=[]
        fail=[True]
        def analyze(videos,requests):
            attempts.append([video["clip_id"] for video in videos])
            if videos[0]["start_ms"]==9000 and fail[0]:
                raise VisionTimeout("second bounded request timed out")
            result=original(videos,requests)
            for observed,video in zip(result.clips,videos):
                index=video["start_ms"]//3000
                observed.matches=[match for match in observed.matches if index<len(keys) and match.scene_key==keys[index]]
            return result
        provider.analyze_intervals=analyze
        job_id=uuid4()
        with pytest.raises(VisionTimeout):resolver.resolve(project,scenes,job_id)
        records=ProviderCallRepository(db).list_for_project(project.id)
        assert [record.status for record in records]==["completed","failed"]
        successful=set(list(windows)[:3])
        facts=AssetRepository(db).get(asset.id).metadata["visual_analysis"]
        assert successful<=set(facts)
        with pytest.raises(ProviderExecutionRecordedFailure):resolver.resolve(project,scenes,job_id)
        assert len(attempts)==2
        fail[0]=False
        # 当前召回已变化，显式恢复仍使用原稿保存的候选和成功事实。
        resolver._windows=lambda *a,**k:({},[{
            "scene_key":scene_visual_key(scene),"voice_text":scene.voice_text,"purpose":scene.purpose,
            "visual_intent":scene.visual_intent.model_dump(mode="json"),"visual_requirement":scene.visual_requirement,
            "visual_requirement_reason":scene.visual_requirement_reason} for scene in scenes])
        chosen=resolver.resolve(project,scenes,job_id,attempt_id=uuid4())
        assert len(chosen)==4 and len(attempts)==3
        assert attempts[2]==attempts[1] and not successful.intersection(attempts[2])
        records=ProviderCallRepository(db).list_for_project(project.id)
        assert [record.status for record in records]==["completed","failed","completed"]
        assert len({record.idempotency_key for record in records})==3
        assert len(list((tmp_path/"render-diagnostics"/str(job_id)).glob("material-source-analysis-*.json")))>=3
    finally:db.close()


@pytest.mark.parametrize("style,tail", [("auto",3),("creator_led",4)])
@pytest.mark.parametrize("global_policy", [False,True])
def test_real_provider_reservation_preserves_production_tail(tmp_path,style,tail,global_policy):
    db,project,asset,_,base,provider,resolver=material_library(tmp_path,metered=True)
    try:
        project=project.model_copy(update={"presentation_style":style})
        scenes,_=scope(resolver,asset,base)
        provider.accept=lambda video:True
        policy=BudgetPolicy(project_id=None if global_policy else project.id,max_calls=tail,
            allow_unknown_cost=True,updated_at=datetime.now(timezone.utc))
        BudgetPolicyRepository(db).save(policy)
        with pytest.raises(BudgetLimitError) as error:
            resolver.resolve(project,scenes,uuid4())
        assert error.value.code=="material_analysis_tail_budget" and provider.calls==[]
        assert ProviderCallRepository(db).list_for_project(project.id)==[]
        BudgetPolicyRepository(db).save(policy.model_copy(update={"max_calls":tail+1}))
        assert resolver.resolve(project,scenes,uuid4())
        assert len(ProviderCallRepository(db).list_for_project(project.id))==1
    finally:db.close()


def test_three_batch_limit_returns_specific_gap_and_keeps_all_completed_evidence(tmp_path):
    db,project,asset,_,base,provider,resolver=material_library(tmp_path,metered=True)
    try:
        scenes,_=scope(resolver,asset,base,duration=10000,step=2000)
        provider.accept=lambda video:video["start_ms"]==14000
        job_id=uuid4()
        with pytest.raises(MaterialResolutionError) as error:
            resolver.resolve(project,scenes,job_id)
        assert "material_analysis_batch_limit" in error.value.reasons
        assert any(reason.startswith("material_scene_unresolved:part-0") for reason in error.value.reasons)
        assert len(provider.calls)==3 and all(len(videos)==2 for videos,_ in provider.calls)
        assert len(ProviderCallRepository(db).list_for_project(project.id))==3
        with pytest.raises(MaterialResolutionError,match="material_analysis_batch_limit"):
            resolver.resolve(project,scenes,job_id)
        assert len(provider.calls)==3  # 恢复调用不能暗中突破同一尝试的三批上限。
    finally:db.close()


@pytest.mark.parametrize('style,tail',[('auto',3),('creator_led',4)])
@pytest.mark.parametrize('revision_calls',[2,1+VisualMaterialResolver.max_analysis_batches])
def test_completed_negative_batch_preserves_an_affordable_visual_revision_instead_of_more_qa(tmp_path,style,tail,revision_calls):
    db,project,asset,_,base,provider,resolver=material_library(tmp_path,metered=True)
    try:
        project=project.model_copy(update={'presentation_style':style})
        scenes,_=scope(resolver,asset,base)
        provider.accept=lambda video:False
        BudgetPolicyRepository(db).save(BudgetPolicy(project_id=project.id,max_calls=tail+revision_calls+1,
            allow_unknown_cost=True,updated_at=datetime.now(timezone.utc)))
        with pytest.raises(MaterialResolutionError) as error:
            resolver.resolve(project,scenes,uuid4(),revision_reserve_calls=revision_calls)
        assert 'material_analysis_revision_due' in error.value.reasons
        assert error.value.candidate_rejections
        assert len(provider.calls)==1
        records=ProviderCallRepository(db).list_for_project(project.id)
        assert len(records)==1 and records[0].status=='completed'
        assert tail+revision_calls+1-len(records)==tail+revision_calls
    finally:db.close()


def test_first_qa_may_finish_without_requiring_an_unused_revision_budget(tmp_path):
    db,project,asset,_,base,provider,resolver=material_library(tmp_path,metered=True)
    try:
        scenes,_=scope(resolver,asset,base)
        provider.accept=lambda video:True
        BudgetPolicyRepository(db).save(BudgetPolicy(project_id=project.id,max_calls=4,
            allow_unknown_cost=True,updated_at=datetime.now(timezone.utc)))
        assert resolver.resolve(project,scenes,uuid4(),revision_reserve_calls=1+resolver.max_analysis_batches)
        assert len(provider.calls)==1
    finally:db.close()


@pytest.mark.parametrize('style,tail',[('auto',3),('creator_led',4)])
@pytest.mark.parametrize('global_policy',[False,True])
def test_revision_with_two_analysis_batches_preserves_the_entire_production_tail(tmp_path,monkeypatch,style,tail,global_policy):
    from types import SimpleNamespace
    from app.costs import UnknownProviderCostEstimator
    from app.db import ProjectDraftRepository,ProjectRepository
    from app.domain.models import ProjectDraft,CostCategory,VisualIntent
    from app.provider_execution import ProviderExecutionService,RuntimeProviderIdentity
    from app.video_production import VideoProductionService

    db,project,asset,_,base,provider,resolver=material_library(tmp_path,metered=True)
    try:
        project=ProjectRepository(db).update(project.model_copy(update={'presentation_style':style}))
        scenes,windows=scope(resolver,asset,base,count=6)
        original=scenes[0]
        replacement=original.model_copy(update={'visual_intent':VisualIntent(subject='按实际候选调整的狗接飞盘画面')})
        job_id=uuid4()
        draft=ProjectDraftRepository(db).save(ProjectDraft(project_id=project.id,version=1,script_revision=1,
            topic=project.topic,script=original.voice_text,scenes=[original],updated_at=datetime.now(timezone.utc)))
        # 真实批次限制与账目边界，视频判断及规划使用明确替身。
        def revised_windows(project,scenes,acquired_assets=None):
            return windows,[{'scene_key':scene_visual_key(scene),'voice_text':scene.voice_text,'purpose':scene.purpose,
                'visual_intent':scene.visual_intent.model_dump(mode='json'),'visual_requirement':scene.visual_requirement,
                'visual_requirement_reason':scene.visual_requirement_reason} for scene in scenes]
        resolver._windows=revised_windows
        resolver.revision_candidates=lambda *args:tuple(clip.id for _,clip in windows.values())
        provider.accept=lambda video:video['start_ms']>=9000
        policy=BudgetPolicy(project_id=None if global_policy else project.id,max_calls=tail+1+resolver.max_analysis_batches,
            allow_unknown_cost=True,updated_at=datetime.now(timezone.utc))
        BudgetPolicyRepository(db).save(policy)
        service=VideoProductionService(db,tmp_path,material_resolver=resolver)
        def planning(db,project,payload,planner,cost,key,**kwargs):
            result=SimpleNamespace(scenes=(replacement,))
            return ProviderExecutionService(db,UnknownProviderCostEstimator()).execute(project_id=project.id,
                idempotency_key=key,operation='scene_planning',provider=RuntimeProviderIdentity('contract-fixture','fixture-planner'),
                category=CostCategory.LLM,input_source='fixture:visual-revision',input_document={'scene_id':original.scene_id},
                action=lambda:result,encode_result=lambda value:{'fixture':True},decode_result=lambda value:result,
                error_code=lambda exc:'fixture_planning_failed',reservation_guard=kwargs['reservation_guard'])
        monkeypatch.setattr('app.video_production.generate_scene_plan',planning)
        job=SimpleNamespace(id=job_id,payload=SimpleNamespace(topic=project.topic))
        failure=MaterialResolutionError(f'material_scene_unresolved:{original.scene_id}')
        result=service._revise_visuals(project,draft,job,job_id,None,object(),object(),failure)
        records=ProviderCallRepository(db).list_for_project(project.id)
        assert len(result)==1 and len(provider.calls)==2
        assert [len(videos) for videos,_ in provider.calls]==[3,3]
        assert [record.operation for record in records]==['scene_planning','vision','vision']
        assert all(record.status=='completed' for record in records)
        assert policy.max_calls-len(records)==tail+1  # 未用的第三批也未收费，原制作尾额完整保留。
        assert ProjectDraftRepository(db).get(project.id)==draft
    finally:db.close()


def test_previous_single_large_request_failure_requires_explicit_new_attempt(tmp_path):
    from app.domain.models import CostCategory
    from app.provider_execution import ProviderExecutionService,runtime_provider_identity
    db,project,asset,_,base,provider,resolver=material_library(tmp_path,metered=True)
    try:
        scenes,_=scope(resolver,asset,base)
        provider.accept=lambda video:True
        job_id=uuid4()
        def timeout():raise VisionTimeout("old large request failed")
        with pytest.raises(VisionTimeout):
            ProviderExecutionService(db,resolver.cost_estimator).execute(project_id=project.id,
                idempotency_key=f"video-production:{job_id}:source-analysis",operation="vision",
                provider=runtime_provider_identity(provider),input_source=f"project:{project.id}:source-intervals",
                category=CostCategory.LLM,input_document={"legacy":"all candidates in one request"},action=timeout,
                encode_result=lambda value:value,decode_result=lambda value:value,error_code=lambda error:"old_timeout")
        with pytest.raises(ProviderExecutionRecordedFailure):resolver.resolve(project,scenes,job_id)
        assert not provider.calls
        assert resolver.resolve(project,scenes,job_id,attempt_id=uuid4())
        assert len(provider.calls)==1 and len(ProviderCallRepository(db).list_for_project(project.id))==2
    finally:db.close()


def test_catalogue_preparation_uses_bounded_batches_and_restores_partial_success(tmp_path,monkeypatch):
    import app.media.planning as planning
    db,project,asset,_,base,provider,resolver=material_library(tmp_path,metered=True)
    try:
        _,windows=scope(resolver,asset,base)
        monkeypatch.setattr(planning,"_catalogue_candidates",lambda *a:((),windows))
        original=provider.analyze_intervals
        fail=[True];attempts=[]
        def analyze(videos,requests):
            attempts.append([video["clip_id"] for video in videos])
            if videos[0]["start_ms"]==9000 and fail[0]:raise VisionTimeout("catalogue second batch failed")
            return original(videos,requests)
        provider.analyze_intervals=analyze
        job_id=uuid4()
        with pytest.raises(VisionTimeout):resolver.prepare(project,project.topic,job_id)
        with pytest.raises(ProviderExecutionRecordedFailure):resolver.prepare(project,project.topic,job_id)
        assert len(attempts)==2
        fail[0]=False
        prepared=resolver.prepare(project,project.topic,job_id,attempt_id=uuid4())
        assert len(attempts)==4 and attempts[2]==attempts[1] and len(attempts[3])==2
        assert len(prepared)==8 and all(not requests for _,requests in provider.calls)
        assert resolver.prepare(project,project.topic,job_id)==prepared and len(attempts)==4
    finally:db.close()


def malformed_candidate(provider, identity, *, repair_bottom=.5):
    """真实解析器的字段隔离，返回控制开关；没有网络请求。"""
    import json
    from app.providers.vision import _parse_video_interval_output
    original=provider.analyze_intervals
    repaired=[False]
    provider.accept=lambda video:False
    def analyze(videos,requests):
        document=original(videos,requests).model_dump(mode="json")
        for row in document["clips"]:
            if row["clip_id"]!=identity:continue
            row["matches"]=[{**match,"supported":True,"score":.95} for match in row["matches"]]
            row["subtitle_regions"]=[{"left":0.,"top":.86,"right":1.,"bottom":1.}]
            row["protected_regions"]=[{"left":.32,"top":.18,"right":.62,
                "bottom" if repaired[0] else "top_bottom":repair_bottom if repaired[0] else .95}]
        result,issues=_parse_video_interval_output(json.dumps(document),[v["clip_id"] for v in videos],
            {r["scene_key"] for r in requests})
        provider.last_validation_issues=issues
        return result
    provider.analyze_intervals=analyze
    return repaired


@pytest.mark.parametrize("legacy",[False,True])
@pytest.mark.parametrize("repair_bottom",[.5,.95])
def test_malformed_is_unknown_same_attempt_never_reissues_and_new_attempt_only_repairs_invalid(tmp_path,legacy,repair_bottom):
    from copy import deepcopy
    db,project,asset,_,base,provider,resolver=material_library(tmp_path,metered=True)
    try:
        scenes,windows=scope(resolver,asset,base,count=3)
        first=next(iter(windows));job_id=uuid4();attempt=uuid4()
        repair=malformed_candidate(provider,first,repair_bottom=repair_bottom)
        with pytest.raises(MaterialResolutionError):resolver.resolve(project,scenes,job_id,attempt_id=attempt)
        repository=AssetRepository(db)
        current=repository.get(asset.id)
        initial=deepcopy(current.metadata["visual_analysis"])
        assert initial[first]["analysis_valid"] is False and initial[first]["scene_matches"]=={}
        assert all(initial[key]["analysis_valid"] is True for key in list(windows)[1:])
        assert all(initial[key]["scene_matches"][scene_visual_key(scenes[0])]["supported"] is False
            for key in list(windows)[1:])
        if legacy:
            metadata=deepcopy(current.metadata);old=metadata["visual_analysis"][first]
            for key in ("analysis_valid","analysis_attempt_key","analysis_call_id","analysis_invalid_fields"):old.pop(key)
            old["coverage"]="video_interval_analysis"
            old["scene_matches"]={scene_visual_key(scenes[0]):{"scene_key":scene_visual_key(scenes[0]),
                "supported":False,"score":0.,"reason":"本次核验未认可该用途"}}
            repository.update(current.model_copy(update={"metadata":metadata}))
        for same_attempt in (attempt,None):
            with pytest.raises(MaterialResolutionError):resolver.resolve(project,scenes,job_id,attempt_id=same_attempt)
        assert len(provider.calls)==1 and len(ProviderCallRepository(db).list_for_project(project.id))==1
        restored=repository.get(asset.id).metadata["visual_analysis"][first]
        assert restored["coverage"]=="invalid_video_interval_analysis" and restored["scene_matches"]=={}
        assert restored["analysis_call_id"]==str(ProviderCallRepository(db).list_for_project(project.id)[0].id)
        repair[0]=True
        if repair_bottom==.5:
            selected=resolver.resolve(project,scenes,job_id,attempt_id=uuid4())
            assert str(selected[0].preferred_clip_id)==first
        else:
            # 明确坐标虽然合法，但关键内容压在字幕下方，不能因修复格式就通过。
            with pytest.raises(MaterialResolutionError):resolver.resolve(project,scenes,job_id,attempt_id=uuid4())
        assert len(provider.calls)==2 and [video["clip_id"] for video in provider.calls[1][0]]==[first]
        saved=repository.get(asset.id).metadata["visual_analysis"]
        assert saved[first]["analysis_valid"] is True and saved[first]["coverage"]=="video_interval_analysis"
        assert saved[first]["source_subtitle_clearance_verified"] is (repair_bottom==.5)
        assert all(saved[key]==initial[key] for key in list(windows)[1:])
        if repair_bottom==.95:
            with pytest.raises(MaterialResolutionError):resolver.resolve(project,scenes,job_id,attempt_id=uuid4())
            assert len(provider.calls)==2  # 合法但不安全的事实复用，不再为同一区间付费。
    finally:db.close()


def test_malformed_cache_replay_reads_its_own_validation_issues_not_last_provider_state(tmp_path):
    db,project,asset,_,base,provider,resolver=material_library(tmp_path,metered=True)
    try:
        scenes,windows=scope(resolver,asset,base,count=3)
        first,second,*_=windows;job_id=uuid4();attempt=uuid4()
        malformed_candidate(provider,first)
        requests=resolver._windows(project,scenes)[1]
        resolver._analyze(project,windows,requests,job_id,attempt_id=attempt)
        provider.last_validation_issues=[{"clip_id":second,"code":"invalid_clip_fields","fields":["matches"]}]
        resolver._analyze(project,windows,requests,job_id,attempt_id=attempt)
        facts=AssetRepository(db).get(asset.id).metadata["visual_analysis"]
        assert facts[first]["analysis_valid"] is False and facts[first]["scene_matches"]=={}
        assert facts[second]["analysis_valid"] is True and facts[second]["coverage"]=="video_interval_analysis"
        assert len(provider.calls)==1 and len(ProviderCallRepository(db).list_for_project(project.id))==1
    finally:db.close()


def test_legacy_invalid_marker_requires_original_job_clip_and_proxy_evidence(tmp_path):
    from copy import deepcopy
    from app.media.planning import _legacy_invalid_analysis
    db,project,asset,_,base,provider,resolver=material_library(tmp_path,metered=True)
    try:
        scenes,windows=scope(resolver,asset,base,count=1)
        first=next(iter(windows));job_id=uuid4()
        malformed_candidate(provider,first)
        with pytest.raises(MaterialResolutionError):resolver.resolve(project,scenes,job_id)
        fact=deepcopy(AssetRepository(db).get(asset.id).metadata["visual_analysis"][first])
        fact.pop("analysis_valid");fact["coverage"]="video_interval_analysis"
        call=ProviderCallRepository(db).list_for_project(project.id)[0]
        derived=_legacy_invalid_analysis(fact,first,[call])
        assert derived["analysis_valid"] is False and "analysis_valid" not in fact
        assert _legacy_invalid_analysis(fact,str(uuid4()),[call]) is None
        assert _legacy_invalid_analysis({**fact,"analysis_video_sha256":"0"*64},first,[call]) is None
        other=call.model_copy(update={"idempotency_key":call.idempotency_key.replace(str(job_id),str(uuid4()))})
        assert _legacy_invalid_analysis(fact,first,[other]) is None
        payload=deepcopy(call.result_payload)
        payload["result"]["validation_issues"][0]["clip_id"]=str(uuid4())
        assert _legacy_invalid_analysis(fact,first,[call.model_copy(update={"result_payload":payload})]) is None
    finally:db.close()


def test_catalogue_malformed_candidate_only_rechecks_on_explicit_new_attempt(tmp_path,monkeypatch):
    import app.media.planning as planning
    db,project,asset,_,base,provider,resolver=material_library(tmp_path,metered=True)
    try:
        _,windows=scope(resolver,asset,base,count=3)
        monkeypatch.setattr(planning,"_catalogue_candidates",lambda *a:((),windows))
        first=next(iter(windows));job_id=uuid4();attempt=uuid4()
        repair=malformed_candidate(provider,first)
        prepared=resolver.prepare(project,project.topic,job_id,attempt_id=attempt)
        assert len(prepared)==2 and str(prepared[0])!=first and str(prepared[1])!=first
        assert resolver.prepare(project,project.topic,job_id,attempt_id=attempt)==prepared
        assert resolver.prepare(project,project.topic,job_id)==prepared and len(provider.calls)==1
        repair[0]=True
        assert len(resolver.prepare(project,project.topic,job_id,attempt_id=uuid4()))==3
        assert len(provider.calls)==2 and [video["clip_id"] for video in provider.calls[1][0]]==[first]
    finally:db.close()
