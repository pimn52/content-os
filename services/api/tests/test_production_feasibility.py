"""估算与真实时轴共用人物调度；纯内存媒体/仓储夹具，不调用模型。"""
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from app.assembly.video_spec import _master_narration_intervals
from app.domain.models import AudioAsset, Project, ProjectDraft, RationalFps, ScenePlan, TranscriptSegment, VisualIntent
from app.production_runs import ProductionRunNotReady
from app.providers.maintained_talking import MaintainedLatentSyncProvider
from app.talking.pool import TalkingPool
from app.video_production import VideoProductionService
from test_talking_scheduling import short_latentsync_pool


def _case(*, alternatives=True):
    now=datetime.now(timezone.utc)
    project=Project(ip_profile_id=uuid4(),title='码率与清晰度',topic='码率与清晰度',
        presentation_style='creator_led',fps=RationalFps(numerator=25,denominator=1),created_at=now)
    texts=['码率并非决定清晰度的唯一因素。','同一段视频，','复杂运动画面需要更多码率；',
        '静态画面用很低码率就能很清晰。','编码器好坏也差很多。','应结合片源与编码设置判断。']
    durations=[11540,1400,2100,2820,1500,6960]
    scenes=[]
    for index,(text,duration) in enumerate(zip(texts,durations)):
        speaking=0<index<5
        scenes.append(ScenePlan(project_id=project.id,scene_id=f'encoding-{index}',order=index,
            purpose='解释影响清晰度的因素',voice_text=text,duration_target_ms=duration,
            visual_intent=VisualIntent(subject='创作者本人' if speaking else '编码设置'),
            preferred_sources=['ai_video'] if speaking else ['user_asset'],
            fallback_sources=['user_asset','stock'] if speaking and alternatives else [],
            supporting_visual_intent=VisualIntent(subject='视频编码设置',action='比较运动与静态画面')
                if speaking and alternatives else None,
            visual_requirement='creator_speaking' if speaking else 'explanatory',
            visual_requirement_reason='本人说明观点' if speaking else '切题录像辅助说明'))
    draft=ProjectDraft(project_id=project.id,version=1,script_revision=1,script=''.join(texts),
        topic=project.topic,scenes=scenes,updated_at=now)
    return project,draft


def _master(scenes,*,durations=None):
    """Typed transcript timing only; no claim of real synthesis or human quality."""
    segments=[]
    cursor=0
    for scene,duration in zip(scenes,durations or [s.duration_target_ms for s in scenes]):
        segments.append(TranscriptSegment(start_ms=cursor,end_ms=cursor+duration,text=scene.voice_text))
        cursor+=duration
    return AudioAsset(source_file='fixture:master-not-rendered.wav',content_hash='b'*64,
        duration_ms=cursor,sample_rate=24000,channels=1,authorization_reference='fixture:profile-consent',
        imported_at=datetime.now(timezone.utc),transcript_source='fixture:typed-admitted-segments',
        transcript_segments=segments)


def _service(tmp_path,monkeypatch,project,draft,master=None,*,resolver=None):
    db=Mock()
    db.connection.execute.return_value.fetchone.return_value=None
    db.transaction.side_effect=lambda **_:nullcontext()
    service=VideoProductionService(db,tmp_path,voice_provider=Mock(),talking_provider=MaintainedLatentSyncProvider,
        material_resolver=resolver)
    service.runs=Mock()
    pool=short_latentsync_pool()
    monkeypatch.setattr('app.talking.pool.build_talking_pool',lambda *a,**kw:pool)
    for name,value in (('ProjectRepository',project),('ProjectDraftRepository',draft),('AudioAssetRepository',master)):
        monkeypatch.setattr(f'app.video_production.{name}',lambda db,value=value:SimpleNamespace(get=lambda _:value))
    return service,pool


def _job(project):
    return SimpleNamespace(id=uuid4(),project_id=project.id,payload=SimpleNamespace(topic=project.topic,
        resume_from_job_id=None,candidate_of_run_id=None))


def test_estimate_and_typed_master_use_same_183c_batch_capacity_and_preserve_whole_timeline(tmp_path,monkeypatch):
    project,draft=_case()
    master=_master(draft.scenes)
    service,pool=_service(tmp_path,monkeypatch,project,draft,master)
    before=master.model_dump()
    estimated=service._visual_feasibility(project,draft.scenes,script=draft.script,pool=pool)
    measured=service._visual_feasibility(project,draft.scenes,script=draft.script,master=master,pool=pool)
    assert estimated.basis=='estimated_scene_duration' and measured.basis=='measured_master'
    assert estimated.reason is measured.reason is None
    assert estimated.scheduled==measured.scheduled
    for result in (estimated,measured):
        summary=result.summary(MaintainedLatentSyncProvider)
        assert summary['usable_total_ms']==3200 and summary['alignment_state']=='known'
        assert summary['requested_talking_ms']==7820 and summary['allocated_reference_ms']==1920
        assert summary['talking_scene_ids']==['encoding-4']
        assert summary['supporting_footage_scene_ids']==['encoding-1','encoding-2','encoding-3']
        assert len(result.allocations)==1
        assert [s.voice_text for s in result.scheduled]==[s.voice_text for s in draft.scenes]
        assert [s.duration_target_ms for s in result.scheduled]==[11540,1400,2100,2820,1500,6960]
        assert all(s.graphic_plan is None for s in result.scheduled)
    intervals=_master_narration_intervals(master,measured.scheduled)
    assert [(start,end) for _,start,end in intervals]==[
        (0,11540),(11540,12940),(12940,15040),(15040,17860),(17860,19360),(19360,26320)]
    assert ''.join(s.voice_text for s in measured.scheduled)==draft.script
    assert (project.resolution_width,project.resolution_height)==(1080,1920)
    assert master.model_dump()==before and service.voice_provider.mock_calls==[]


def test_initial_plan_blocks_impossible_required_speech_before_voice_with_estimate_evidence(tmp_path,monkeypatch):
    project,draft=_case(alternatives=False)
    service,_=_service(tmp_path,monkeypatch,project,draft)
    job=_job(project)
    draft.evidence_refs.append(f'content_plan_job:{job.id}')
    planner=Mock()
    preflight=Mock(side_effect=AssertionError('must stop before run and Voice'))
    monkeypatch.setattr('app.video_production.build_production_preflight',preflight)
    with pytest.raises(ProductionRunNotReady) as error:
        service.plan(job,planner,Mock())
    assert error.value.reasons==('talking_motion_capacity_insufficient',)
    evidence=json.loads((tmp_path/'render-diagnostics'/str(job.id)/'visual-feasibility.json').read_text(encoding='utf-8'))
    assert evidence['basis']=='estimated_scene_duration' and evidence['schedulable'] is False
    assert evidence['requires_measured_partition'] is False
    assert evidence['requested_talking_ms']==7820 and evidence['allocated_reference_ms']==0
    assert evidence['stop_reasons']==['talking_motion_capacity_insufficient']
    assert 'master_audio_id' not in evidence and 'voice_qa_state' not in evidence
    assert service.voice_provider.mock_calls==[] and service.runs.mock_calls==[]
    assert planner.mock_calls==[] and preflight.mock_calls==[]
    assert all(call.args[0].lstrip().upper().startswith('SELECT') for call in service.db.connection.execute.call_args_list)


@pytest.mark.parametrize('alternatives',[False,True])
def test_long_estimate_defers_to_real_clauses_without_cutaway_revision(tmp_path,monkeypatch,alternatives):
    project,draft=_case(alternatives=alternatives)
    clauses=['先看复杂运动画面。','再看静态画面。']
    whole=draft.scenes[1].model_copy(update={'order':0,'voice_text':''.join(clauses),'duration_target_ms':4000})
    draft=draft.model_copy(update={'script':whole.voice_text,'scenes':[whole]})
    clause_scenes=[whole.model_copy(update={'voice_text':text,'duration_target_ms':2000}) for text in clauses]
    master=_master(clause_scenes)
    service,first_pool=_service(tmp_path,monkeypatch,project,draft,master,resolver=object())
    second=short_latentsync_pool().entries[0]
    second=replace(second,asset=second.asset.model_copy(update={'content_hash':'c'*64}),source_hash='c'*64)
    pool=TalkingPool((first_pool.entries[0],second))
    monkeypatch.setattr('app.talking.pool.build_talking_pool',lambda *a,**kw:pool)
    service._motion_revision=Mock(side_effect=AssertionError('wait for actual clauses before changing visuals'))
    estimated=service._visual_feasibility(project,draft.scenes,script=draft.script,pool=pool)
    assert estimated.reason is None and estimated.requires_measured_partition is True
    assert estimated.summary(MaintainedLatentSyncProvider)['schedulable'] is None
    assert estimated.scenes==tuple(draft.scenes) and estimated.allocations==()
    job=_job(project)
    assert service._prepare_initial_visuals(project,draft,job,Mock(),Mock()) is draft
    assert service._motion_revision.mock_calls==[]
    evidence=json.loads((tmp_path/'render-diagnostics'/str(job.id)/'visual-feasibility.json').read_text(encoding='utf-8'))
    assert evidence['basis']=='estimated_scene_duration' and evidence['requires_measured_partition'] is True
    assert evidence['schedulable'] is None and evidence['stop_reasons']==[]
    assert evidence['usable_total_ms']==6400 and evidence['requested_talking_ms']==4000
    measured=service._visual_feasibility(project,draft.scenes,script=draft.script,master=master,pool=pool)
    assert measured.reason is None and measured.requires_measured_partition is False
    assert measured.summary(MaintainedLatentSyncProvider)['schedulable'] is True
    assert [s.voice_text for s in measured.scheduled]==clauses
    assert [s.duration_target_ms for s in measured.scheduled]==[2000,2000]
    assert len(measured.allocations)==2 and len({a.entry.source_hash for a in measured.allocations})==2
    assert [a.needed_reference_ms for a in measured.allocations]==[2560,2560]
    assert all(s.visual_requirement=='creator_speaking' for s in measured.scheduled)
    assert [(start,end) for _,start,end in _master_narration_intervals(master,measured.scheduled)]==[(0,2000),(2000,4000)]
    assert ''.join(s.voice_text for s in measured.scheduled)==draft.script==whole.voice_text
    assert service.voice_provider.mock_calls==[] and service.runs.mock_calls==[]


def test_initial_existing_cutaways_do_not_request_or_rewrite_motion_revision(tmp_path,monkeypatch):
    project,draft=_case()
    service,_=_service(tmp_path,monkeypatch,project,draft,resolver=object())
    job=_job(project)
    service._motion_revision=Mock(side_effect=AssertionError('existing alternatives already fit'))
    before=draft.model_dump()
    assert service._prepare_initial_visuals(project,draft,job,Mock(),Mock()) is draft
    assert draft.model_dump()==before and service._motion_revision.mock_calls==[]
    assert service.voice_provider.mock_calls==[] and service.runs.mock_calls==[]
    evidence=json.loads((tmp_path/'render-diagnostics'/str(job.id)/'visual-feasibility.json').read_text(encoding='utf-8'))
    assert evidence['basis']=='estimated_scene_duration' and evidence['schedulable'] is True
    assert evidence['talking_scene_ids']==['encoding-4']


def test_required_cutaways_are_verified_in_the_normal_material_path_before_voice(tmp_path,monkeypatch):
    from app.media.planning import MaterialResolutionError
    project,draft=_case()
    seen=[]
    def resolve(project,scenes,*args,**kwargs):
        seen.extend(scenes)
        raise MaterialResolutionError('material_scene_unresolved:encoding-1')
    resolver=SimpleNamespace(check_configuration=lambda *a:None,resolve=resolve)
    service,_=_service(tmp_path,monkeypatch,project,draft,resolver=resolver)
    service._reserved_talking_uses=Mock(return_value=[])
    job=_job(project)
    draft.evidence_refs.append(f'content_plan_job:{job.id}')
    with pytest.raises(MaterialResolutionError):
        service.plan(job,Mock(),Mock())
    assert [s.scene_id for s in seen if s.visual_requirement=='creator_speaking']==['encoding-4']
    assert all(s.visual_requirement=='explanatory' for s in seen[1:4])
    assert [s.voice_text for s in seen]==[s.voice_text for s in draft.scenes]
    assert all(s.visual_requirement=='creator_speaking' for s in draft.scenes[1:5])
    assert service.runs.mock_calls==[] and service.voice_provider.mock_calls==[]


def test_verified_estimate_keeps_original_person_intent_for_measured_rescheduling(tmp_path,monkeypatch):
    project,draft=_case()
    selection=uuid4()
    events=[]
    def resolve(project,scenes,*args,**kwargs):
        events.append('materials')
        return tuple(scene.model_copy(update={'preferred_clip_id':selection})
            if scene.visual_requirement!='creator_speaking' else scene for scene in scenes)
    service,_=_service(tmp_path,monkeypatch,project,draft,resolver=SimpleNamespace(
        check_configuration=lambda *a:None,resolve=resolve))
    service._reserved_talking_uses=Mock(return_value=[])
    job=_job(project)
    draft.evidence_refs.append(f'content_plan_job:{job.id}')
    saved=[]
    def save(db,project,**values):
        saved.append(values)
        return draft.model_copy(update={'scenes':list(values['scenes']),'version':2})
    monkeypatch.setattr('app.video_production.save_project_draft',save)
    monkeypatch.setattr('app.video_production.build_production_preflight',lambda *a,**kw:SimpleNamespace(fingerprint='fixture',scenes=[]))
    def start(*a,**kw):
        events.append('start')
        return SimpleNamespace(project_id=project.id,id=uuid4())
    service.runs.start=start
    service.advance=Mock()
    service.plan(job,Mock(),Mock())
    assert events==['materials','start']
    assert saved[0]['script']==draft.script
    assert [s for s in saved[0]['scenes'] if s.visual_requirement=='creator_speaking']==draft.scenes[1:5]
    assert saved[0]['scenes'][0].preferred_clip_id==selection
    evidence=json.loads((tmp_path/'render-diagnostics'/str(job.id)/'visual-feasibility.json').read_text(encoding='utf-8'))
    assert evidence['material_verification']=='estimated_substitutes_verified'
    assert evidence['basis']=='estimated_scene_duration'
    assert service.voice_provider.mock_calls==[]


def test_failed_initial_revision_retains_original_estimate_evidence(tmp_path,monkeypatch):
    project,draft=_case(alternatives=False)
    service,_=_service(tmp_path,monkeypatch,project,draft,resolver=object())
    job=_job(project)
    service._motion_revision=Mock(side_effect=ProductionRunNotReady(('talking_motion_capacity_insufficient',)))
    with pytest.raises(ProductionRunNotReady):
        service._prepare_initial_visuals(project,draft,job,Mock(),Mock())
    service._motion_revision.assert_called_once()
    evidence=json.loads((tmp_path/'render-diagnostics'/str(job.id)/'visual-feasibility.json').read_text(encoding='utf-8'))
    assert evidence['basis']=='estimated_scene_duration' and evidence['schedulable'] is False
    assert evidence['stop_reasons']==['talking_motion_capacity_insufficient']
    assert service.voice_provider.mock_calls==[]


def test_longer_real_master_reallocates_entire_batch_and_retains_voice(tmp_path,monkeypatch):
    project,draft=_case()
    master=_master(draft.scenes,durations=[11540,1400,2100,2820,3500,6960])
    service,pool=_service(tmp_path,monkeypatch,project,draft,master,resolver=object())
    estimated=service._visual_feasibility(project,draft.scenes,script=draft.script,pool=pool)
    assert estimated.summary(MaintainedLatentSyncProvider)['talking_scene_ids']==['encoding-4']
    service._motion_revision=Mock(side_effect=AssertionError('use existing alternatives with retained Master'))
    service._resolve_with_supplement=Mock(side_effect=lambda project,scenes,*args,**kwargs:tuple(scenes))
    monkeypatch.setattr('app.source_usage.SourceUsageService',lambda db:SimpleNamespace(resolve=
        lambda asset_id,start,end,scene_id:[dict(source_hash='a'*64,start_ms=start,end_ms=end)]))
    saved=[]
    def save(db,project,**values):
        saved.append(values)
        return draft.model_copy(update={'version':2,'scenes':list(values['scenes'])})
    monkeypatch.setattr('app.video_production.save_project_draft',save)
    monkeypatch.setattr('app.video_production.build_production_preflight',lambda db,project,saved_draft,**kw:
        SimpleNamespace(scenes=[SimpleNamespace(scene_plan_id=scene.id,scene_id=scene.scene_id,
            visual_dependency_fingerprint='e'*64,production_need='new_talking')
            for scene in saved_draft.scenes if scene.visual_requirement=='creator_speaking']))
    run_id=uuid4()
    row=dict(project_id=str(project.id),id=str(run_id),draft_version=1,script_revision=1,
        voice_audio_id=str(master.id),start_job_id=None,work_id=None,repair_allowance=1,stage='voice')
    service.db.connection.execute.return_value.fetchone.return_value=row
    before=master.model_dump()
    service._prepare_measured_plan(row)
    assert len(saved)==1 and saved[0]['script']==draft.script
    scheduled=saved[0]['scenes']
    assert [s.scene_id for s in scheduled if s.visual_requirement=='creator_speaking']==['encoding-2']
    assert [s.duration_target_ms for s in scheduled]==[11540,1400,2100,2820,3500,6960]
    assert ''.join(s.voice_text for s in scheduled)==draft.script
    intervals=_master_narration_intervals(master,scheduled)
    assert intervals[0][1]==0 and intervals[-1][2]==master.duration_ms==28320
    resolver_call=service._resolve_with_supplement.call_args
    assert resolver_call.args[6]['after_master'] is True and resolver_call.args[6]['remaining_talking_calls']==1
    assert resolver_call.kwargs['master_audio_id']==master.id
    evidence=json.loads((tmp_path/'render-diagnostics'/str(run_id)/'motion-schedule.json').read_text(encoding='utf-8'))
    assert evidence['master_audio_id']==str(master.id) and evidence['master_content_hash']==master.content_hash
    assert evidence['feasibility']['basis']=='measured_master'
    assert evidence['feasibility']['talking_scene_ids']==['encoding-2']
    assert evidence['feasibility']['requested_talking_ms']==9820
    assert evidence['feasibility']['allocated_reference_ms']==2560
    assert master.model_dump()==before and service.voice_provider.mock_calls==[]
    service.runs._require_bound_voice_master.assert_called_once_with(row,master.id)
    assert service._motion_revision.mock_calls==[]


@pytest.mark.parametrize('known_alignment',[True,False])
def test_real_clause_boundaries_fit_fragmented_pool_without_unnecessary_planning_revision(tmp_path,monkeypatch,known_alignment):
    project,draft=_case(alternatives=False)
    texts=['先观察复杂运动。','再看细节。','然后比较静态画面。','最后总结。']
    whole=draft.scenes[1].model_copy(update={'order':0,'voice_text':''.join(texts),'duration_target_ms':9600})
    draft=draft.model_copy(update={'script':whole.voice_text,'scenes':[whole]})
    master=_master([whole.model_copy(update={'voice_text':text}) for text in texts],durations=[3000,1800,3000,1800])
    service,_=_service(tmp_path,monkeypatch,project,draft,master)
    if not known_alignment:service.talking_provider=None
    first=short_latentsync_pool(6400).entries[0]
    second=short_latentsync_pool(4480).entries[0]
    second=replace(second,asset=second.asset.model_copy(update={'content_hash':'c'*64}),source_hash='c'*64)
    pool=TalkingPool((first,second))
    before=master.model_dump(),draft.model_dump()
    result=service._visual_feasibility(project,draft.scenes,script=draft.script,master=master,pool=pool)
    if not known_alignment:
        assert result.reason=='talking_motion_capacity_insufficient' and result.allocations==()
        assert result.summary()['alignment_state']=='unknown'
        assert (master.model_dump(),draft.model_dump())==before
        return
    assert result.reason is None and [s.duration_target_ms for s in result.scheduled]==[3000,1800,3000,1800]
    assert [s.voice_text for s in result.scheduled]==texts
    assert [a.needed_reference_ms for a in result.allocations]==[3200,1920,3200,1920]
    assert sum(a.needed_reference_ms for a in result.allocations)==10240
    assert all(scene.visual_requirement=='creator_speaking' for scene in result.scheduled)
    assert [(start,end) for _,start,end in _master_narration_intervals(master,result.scheduled)]==[(0,3000),(3000,4800),(4800,7800),(7800,9600)]
    assert (master.model_dump(),draft.model_dump())==before
    assert service.voice_provider.mock_calls==[] and service.runs.mock_calls==[]
