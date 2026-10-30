"""Persisted visual-task identity and dispatch seams; no model or media execution."""
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.assembly.edit_plan import review_edit_plan, source_frame_interval_ms
from app.assembly.video_spec import InvalidCandidateSelection, VideoSpecAssembler
from app.db import AssetRepository, ClipRepository, Database, JobRepository, ProjectRepository, ProviderCallRepository
from app.domain.models import EditPlanScene, Job, JobStatus, JobType, RationalFps, RenderVideoJobPayload, VideoSpec
from app.jobs.handlers import RenderVideoJobHandler
from app.jobs.runner import JobExecutionError
from app.media.visual_facts import scene_visual_key
from app.renderer import RemotionRenderer
from app.source_usage import SourceUsageService, overlap
from test_render_api import _seed


@pytest.fixture
def local_scope(tmp_path):
    path=tmp_path/'semantic-use.sqlite'
    project,plan,candidate=_seed(path,tmp_path)
    db=Database(path);db.data_root=tmp_path
    try:
        yield SimpleNamespace(db=db,root=tmp_path,project=project,plan=plan,candidate=candidate,
            assembler=VideoSpecAssembler(AssetRepository(db),ClipRepository(db)))
    finally:
        db.close()


@pytest.fixture
def guarded_use(monkeypatch):
    """A shared guard substitute to verify every seam sees the actual frame use.

    Negative-evidence persistence and its currentness are independently owned
    by semantic_rejections tests. This fixture adds no source-wide ban.
    """
    from app.media import semantic_rejections
    rejected=[];seen=[]
    def guard(db,spec):
        seen.append(spec)
        entries={entry.scene_id:entry for entry in spec.edit_plan.scenes} if spec.edit_plan else {}
        hits=[]
        for scene in spec.scenes:
            entry=entries.get(scene.scene_id)
            if entry is None or entry.visual_task_key is None or scene.visual.clip_start_ms is None:
                continue
            start,end=source_frame_interval_ms(scene.visual.clip_start_ms,scene.duration_frames,
                spec.fps,scene.visual.clip_end_ms)
            uses=SourceUsageService(db).resolve(scene.visual.asset_id,start,end,scene.scene_id)
            hits.extend(item for item in rejected if item['task_key']==entry.visual_task_key
                and any(overlap(item,use) for use in uses))
        if hits:
            raise semantic_rejections.SemanticUseRejected(hits)
    monkeypatch.setattr(semantic_rejections,'require_spec_semantics',guard)
    return SimpleNamespace(rejected=rejected,seen=seen)


def assemble(value,**kwargs):
    return value.assembler.assemble(value.project,[value.plan],{value.plan.id:value.candidate},**kwargs)


def reject(value,guard,*,start=400,end=500,task_key=None):
    asset=AssetRepository(value.db).get(value.candidate.asset_id)
    guard.rejected.append({'task_key':task_key or scene_visual_key(value.plan),
        'source_hash':asset.content_hash,'start_ms':start,'end_ms':end,'evidence_reference':'fixture:known-negative'})


def test_real_assembly_persists_actual_visual_task_key_without_changing_legacy_serialization(local_scope,guarded_use):
    value=local_scope;spec=assemble(value)
    entry=spec.edit_plan.scenes[0]
    assert entry.visual_task_key==scene_visual_key(value.plan)
    assert guarded_use.seen[-1].edit_plan.scenes[0].visual_task_key==scene_visual_key(value.plan)
    assert VideoSpec.model_validate_json(spec.model_dump_json()).edit_plan.scenes[0].visual_task_key==entry.visual_task_key
    legacy=entry.model_dump(mode='json');legacy.pop('visual_task_key')
    old=EditPlanScene.model_validate(legacy)
    assert old.visual_task_key is None and 'visual_task_key' not in old.model_dump(mode='json')
    reviewed=review_edit_plan(spec.edit_plan.model_copy(update={'scenes':[old]}),[value.plan],
        {value.plan.id:value.candidate},clip_intervals={value.candidate.clip_id:(0,1000)})
    assert reviewed.scenes[0].visual_task_key==scene_visual_key(value.plan)
    assert old.visual_task_key is None


@pytest.mark.parametrize('bad_key',['a'*63,'A'*64,'z'*64,'a'*65])
def test_visual_task_key_rejects_invalid_serialized_identity(local_scope,guarded_use,bad_key):
    entry=assemble(local_scope).edit_plan.scenes[0].model_dump(mode='json')
    with pytest.raises(ValueError):EditPlanScene.model_validate({**entry,'visual_task_key':bad_key})


@pytest.mark.parametrize('field,value',[('voice_text','Different complete narration.'),('purpose','Different visual purpose')])
def test_preflight_does_not_relabel_a_supplied_plan_for_different_words_or_purpose(local_scope,guarded_use,field,value):
    scope=local_scope;spec=assemble(scope);before=len(guarded_use.seen)
    changed=scope.plan.model_copy(update={field:value})
    with pytest.raises(InvalidCandidateSelection,match='edit_plan_visual_task_changed'):
        scope.assembler.assemble(scope.project,[changed],{changed.id:scope.candidate},edit_plan=spec.edit_plan)
    assert len(guarded_use.seen)==before
    assert spec.edit_plan.scenes[0].visual_task_key==scene_visual_key(scope.plan)


def test_assembly_checks_actual_ntsc_source_frames_instead_of_banning_the_whole_clip(local_scope,guarded_use):
    from app.media.semantic_rejections import SemanticUseRejected
    scope=local_scope
    scope.project=scope.project.model_copy(update={'fps':RationalFps(numerator=30000,denominator=1001)})
    scope.candidate=scope.candidate.model_copy(update={'playback_start_ms':1,'playback_end_ms':1000})
    reject(scope,guarded_use,start=900,end=950)
    reject(scope,guarded_use,start=10,end=20)  # Before the renderer's frame-ceiled source start.
    spec=assemble(scope)
    assert spec.scenes[0].duration_frames==24
    assert source_frame_interval_ms(1,24,spec.fps,1000)==(33,835)
    reject(scope,guarded_use,start=820,end=830)
    with pytest.raises(SemanticUseRejected) as caught:assemble(scope)
    assert caught.value.reasons==('source_semantic_use_rejected',)
    assert len(caught.value.hits)==1


def test_new_negative_evidence_after_assembly_blocks_handler_before_any_renderer_or_call(local_scope,guarded_use):
    scope=local_scope;spec=assemble(scope);reject(scope,guarded_use)
    calls=[]
    renderer=SimpleNamespace(data_root=scope.root,render=lambda *args,**kwargs:calls.append(args))
    now=datetime.now(timezone.utc)
    job=JobRepository(scope.db).create(Job(project_id=scope.project.id,type=JobType.RENDER,status=JobStatus.RUNNING,
        idempotency_key='fixture-render',created_at=now,updated_at=now,
        payload=RenderVideoJobPayload(project_id=scope.project.id,render_id=uuid4(),video_spec=spec)))
    with pytest.raises(JobExecutionError) as caught:
        RenderVideoJobHandler(ProjectRepository(scope.db),renderer,scope.root/'renders')(job)
    assert caught.value.code=='source_semantic_use_rejected' and caught.value.retryable is False
    assert len(caught.value.semantic_hits)==1 and calls==[]
    assert ProviderCallRepository(scope.db).list()==[]
    assert [item.type for item in JobRepository(scope.db).list()]==[JobType.RENDER]
    assert not (scope.root/'renders'/str(scope.project.id)/f'{job.payload.render_id}.mp4').exists()


def test_new_negative_evidence_blocks_direct_render_before_media_staging_or_command(local_scope,guarded_use):
    from app.media.semantic_rejections import SemanticUseRejected
    scope=local_scope;spec=assemble(scope);reject(scope,guarded_use)
    calls=[]
    renderer_root=scope.root/'renderer-not-created'
    renderer=RemotionRenderer(AssetRepository(scope.db),ClipRepository(scope.db),renderer_dir=renderer_root,
        runner=SimpleNamespace(run=lambda *args,**kwargs:calls.append(args)))
    with pytest.raises(SemanticUseRejected) as caught:renderer.render(spec,scope.root/'output.mp4')
    assert caught.value.render_stage=='input_validation' and len(caught.value.hits)==1
    assert calls==[] and not renderer_root.exists() and not (scope.root/'output.mp4').exists()


def test_legacy_missing_task_key_stays_unknown_instead_of_borrowing_current_scene_purpose(local_scope,guarded_use):
    scope=local_scope;spec=assemble(scope);reject(scope,guarded_use)
    old_entry=spec.edit_plan.scenes[0].model_copy(update={'visual_task_key':None})
    legacy=spec.model_copy(update={'edit_plan':spec.edit_plan.model_copy(update={'scenes':[old_entry]})})
    from app.media.semantic_rejections import require_spec_semantics
    assert require_spec_semantics(scope.db,legacy) is None
    assert legacy.edit_plan.scenes[0].visual_task_key is None
