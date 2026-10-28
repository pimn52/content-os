"""真实旁白调度；只替换原稿明确允许的画面，不制造质量结论。"""
from uuid import uuid4
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.db import Database
from app.domain.models import ScenePlan, VisualIntent
from app.talking.pool import build_talking_pool, TalkingPoolCapacityError
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
