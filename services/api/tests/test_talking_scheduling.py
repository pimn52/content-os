"""真实旁白调度；只替换原稿明确允许的画面，不制造质量结论。"""
from uuid import uuid4
from types import SimpleNamespace
from datetime import datetime, timezone
from itertools import combinations

import pytest
from pydantic import ValidationError

from app.db import Database
from app.domain.models import (Asset, Clip, ConsentRecord, ProviderMachineCapabilityProfile,
    RationalFps, ScenePlan, TalkingProfile, VisualIntent)
from app.providers.maintained_talking import MaintainedLatentSyncProvider
from app.talking.pool import (TalkingPool, TalkingPoolEntry, allocate_talking_windows,
    build_talking_pool, TalkingPoolCapacityError, required_talking_reference_duration_ms)
from app.talking.scheduling import schedule_talking_scenes
from test_talking_pool import pool_fixture


def scene(project,index,*,alternative=False,duration=20000):
    return ScenePlan(project_id=project.id,scene_id=f"person-{index}",order=index,purpose="本人解释码率",
        voice_text=f"第{index}个完整观点，不能因为素材不足而删掉。",duration_target_ms=duration,
        visual_intent=VisualIntent(subject="创作者本人"),preferred_sources=["ai_video"],
        fallback_sources=["user_asset","stock"] if alternative else [],
        visual_requirement="creator_speaking",visual_requirement_reason="本人讲解",
        supporting_visual_intent=VisualIntent(subject="编码设置与视频画质对比",action="查看码率设置") if alternative else None)


def test_capacity_rearranges_only_planned_cutaways_keeps_copy_and_creator(tmp_path):
    with Database(tmp_path/'scheduling.sqlite') as db:
        project,*_=pool_fixture(db)
        pool=build_talking_pool(db,project.id,occupied=[])
        original=[scene(project,0),scene(project,1,alternative=True),scene(project,2,alternative=True)]
        revised,allocations=schedule_talking_scenes(pool,original)
        assert [s.visual_requirement for s in revised]==['creator_speaking','explanatory','explanatory']
        assert [s.voice_text for s in revised]==[s.voice_text for s in original]
        assert [s.duration_target_ms for s in revised]==[20000]*3
        assert all(s.graphic_plan is None for s in revised)
        assert revised[1].preferred_sources==original[1].fallback_sources
        assert revised[1].visual_intent==original[1].supporting_visual_intent
        assert all(s.id==before.id and s.viewer_takeaway==before.viewer_takeaway and s.evidence_refs==before.evidence_refs
            for s,before in zip(revised,original))
        assert len(allocations)==1 and allocations[0].narration_duration_ms==20000


def test_short_reference_keeps_the_feasible_complete_sentence_without_clipping_opening(tmp_path):
    from dataclasses import replace
    from app.talking.pool import TalkingPool
    with Database(tmp_path/'short-reference.sqlite') as db:
        project,*_=pool_fixture(db)
        pool=build_talking_pool(db,project.id,occupied=[])
        entry=pool.entries[0]
        short=TalkingPool((replace(entry,end_ms=entry.start_ms+3200,source_end_ms=entry.source_start_ms+3200),))
        original=[scene(project,0,alternative=True,duration=3480),
            scene(project,1,alternative=True,duration=2360),scene(project,2,alternative=True,duration=3540)]
        revised,allocations=schedule_talking_scenes(short,original)
        assert [s.visual_requirement for s in revised]==['explanatory','creator_speaking','explanatory']
        assert [s.voice_text for s in revised]==[s.voice_text for s in original]
        assert [s.duration_target_ms for s in revised]==[3480,2360,3540]
        assert len(allocations)==1 and allocations[0].narration_duration_ms==2360


def short_latentsync_pool(duration=3200):
    """已合格竖屏池的纯调度夹具；不创建数据库或媒体，不初始化模型。"""
    now=datetime.now(timezone.utc)
    asset=Asset(source_file='fixture:portrait-motion',content_hash='a'*64,duration_ms=duration,
        width=1080,height=1920,fps=RationalFps(numerator=25,denominator=1),
        authorization_reference='fixture:ingestion',imported_at=now)
    clip=Clip(asset_id=asset.id,start_ms=0,end_ms=duration,asset_duration_ms=duration)
    profile=TalkingProfile(name='本人动作',provider=MaintainedLatentSyncProvider.provider_name,
        reference_clip_ids=[clip.id],consent=ConsentRecord(subject_name='本人',basis='self',
            confirmed=True,confirmed_at=now),created_at=now)
    capability=ProviderMachineCapabilityProfile(capability='talking',mode='local',
        provider=MaintainedLatentSyncProvider.provider_name,model=MaintainedLatentSyncProvider.model,
        runtime='fixture',machine_id='fixture',readiness='configured',provenance_source='fixture',updated_at=now)
    return TalkingPool((TalkingPoolEntry(profile,clip,asset,capability,0,duration,asset.content_hash,0,duration),))


def test_183c_clauses_prefer_complete_closing_with_real_frame_alignment():
    pool=short_latentsync_pool()
    provider=MaintainedLatentSyncProvider  # 只调用真实静态对齐规则，不加载运行时。
    project=SimpleNamespace(id=uuid4())
    texts=['同一段视频，','复杂运动画面需要更多码率；','静态画面用很低码率就能很清晰。','编码器好坏也差很多。']
    durations=[1400,2100,2820,1500]
    original=[scene(project,index,alternative=True,duration=duration).model_copy(update={'voice_text':text})
        for index,(text,duration) in enumerate(zip(texts,durations))]
    assert [required_talking_reference_duration_ms(provider,duration,3200) for duration in durations]==[1920,2560,3200,1920]
    for pair in combinations(durations,2):
        with pytest.raises(TalkingPoolCapacityError):
            allocate_talking_windows(pool,pair,talking_provider=provider)
    revised,allocations=schedule_talking_scenes(pool,original,talking_provider=provider)
    assert [s.visual_requirement for s in revised]==['explanatory']*3+['creator_speaking']
    assert revised[-1]==original[-1]
    assert len(allocations)==1 and allocations[0].narration_duration_ms==1500
    assert (allocations[0].start_ms,allocations[0].end_ms)==(0,1920)
    assert [s.voice_text for s in revised]==texts
    assert [s.duration_target_ms for s in revised]==durations
    for before,after in zip(original[:3],revised[:3]):
        assert after.visual_intent==before.supporting_visual_intent
        assert after.preferred_sources==before.fallback_sources
        assert after.graphic_plan is None and after.material_request is None
        assert set(after.preferred_sources).issubset({'user_asset','historical_asset','stock','ai_broll'})
    assert (allocations[0].entry.asset.width,allocations[0].entry.asset.height)==(1080,1920)


@pytest.mark.parametrize('text',['同一段视频，','请看这个设置：','Consider this,','Look here:', '“请看这个设置：”'])
def test_setup_speech_is_still_eligible_when_complete_sentence_does_not_fit(text):
    project=SimpleNamespace(id=uuid4())
    original=[scene(project,0,alternative=True,duration=1400).model_copy(update={'voice_text':text}),
        scene(project,1,alternative=True,duration=3500)]
    revised,allocations=schedule_talking_scenes(short_latentsync_pool(),original,
        talking_provider=MaintainedLatentSyncProvider)
    assert revised[0]==original[0] and revised[1].visual_requirement=='explanatory'
    assert len(allocations)==1 and allocations[0].narration_duration_ms==1400


def test_sentence_preference_does_not_change_required_speech_or_missing_punctuation():
    project=SimpleNamespace(id=uuid4())
    pool=short_latentsync_pool()
    closing=scene(project,1,alternative=True,duration=1500)
    required=scene(project,0,duration=1400).model_copy(update={'voice_text':'同一段视频，'})
    revised,allocations=schedule_talking_scenes(pool,[required,closing],talking_provider=MaintainedLatentSyncProvider)
    assert revised[0]==required and revised[1].visual_requirement=='explanatory'
    assert len(allocations)==1 and allocations[0].narration_duration_ms==1400
    unpunctuated=scene(project,0,alternative=True,duration=1400).model_copy(update={'voice_text':'本人开场没有句末标点'})
    revised,_=schedule_talking_scenes(pool,[unpunctuated,closing],talking_provider=MaintainedLatentSyncProvider)
    assert revised[0]==unpunctuated and revised[1].visual_requirement=='explanatory'


def test_sentence_preference_keeps_all_speech_when_capacity_is_sufficient():
    project=SimpleNamespace(id=uuid4())
    original=[scene(project,0,alternative=True,duration=1400).model_copy(update={'voice_text':'同一段视频，'}),
        scene(project,1,alternative=True,duration=1500)]
    revised,allocations=schedule_talking_scenes(short_latentsync_pool(4000),original,
        talking_provider=MaintainedLatentSyncProvider)
    assert revised==tuple(original) and len(allocations)==2
    assert [(value.start_ms,value.end_ms) for value in allocations]==[(0,1920),(1920,3840)]


def test_indispensable_visible_performance_does_not_downgrade_or_remove_all_creator(tmp_path):
    with Database(tmp_path/'essential.sqlite') as db:
        project,*_=pool_fixture(db)
        pool=build_talking_pool(db,project.id,occupied=[])
        with pytest.raises(TalkingPoolCapacityError):schedule_talking_scenes(pool,[scene(project,0,duration=50000)])
        with pytest.raises(TalkingPoolCapacityError):schedule_talking_scenes(pool,[scene(project,0,alternative=True,duration=50000)])
        with pytest.raises(TalkingPoolCapacityError):
            schedule_talking_scenes(pool,[scene(project,0,duration=25000),scene(project,1,duration=25000),scene(project,2,alternative=True)])


def test_unknown_occupancy_is_not_a_reflow_trigger(tmp_path):
    with Database(tmp_path/'unknown.sqlite') as db:
        project,*_=pool_fixture(db)
        with pytest.raises(TalkingPoolCapacityError,match='occupancy_unresolved'):
            schedule_talking_scenes(SimpleNamespace(occupancy_state='unresolved'),[scene(project,0,alternative=True)])


def test_search_limit_never_silently_turns_creator_into_cutaway(tmp_path,monkeypatch):
    with Database(tmp_path/'limit.sqlite') as db:
        project,*_=pool_fixture(db)
        calls=[]
        def allocate(pool,durations,**kwargs):
            calls.append(tuple(durations))
            if len(calls)==1:raise TalkingPoolCapacityError()
            if not durations:return ()
            raise TalkingPoolCapacityError('talking_motion_allocation_search_limit')
        monkeypatch.setattr('app.talking.scheduling.allocate_talking_windows',allocate)
        with pytest.raises(TalkingPoolCapacityError,match='allocation_search_limit'):
            schedule_talking_scenes(object(),[scene(project,0,alternative=True)])


def test_cutaway_contract_requires_explicit_footage_permission():
    with pytest.raises(ValidationError):
        ScenePlan(project_id=uuid4(),scene_id='person',order=0,purpose='本人表达',voice_text='完整句子。',duration_target_ms=5000,
            visual_intent=VisualIntent(subject='本人'),preferred_sources=['ai_video'],fallback_sources=['typography'],
            visual_requirement='creator_speaking',visual_requirement_reason='本人讲述',
            supporting_visual_intent=VisualIntent(subject='字卡'))
