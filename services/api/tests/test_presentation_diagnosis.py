"""Pure scene comparison; fixture observations never establish model quality."""
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.assembly.video_spec import _master_narration_intervals, NarrationTimelineError
from app.domain.models import (Asset, AudioAsset, Clip, MasterNarration, RationalFps, ScenePlan,
    SourceKind, TranscriptSegment, VideoScene, VideoSpec, VideoVisual, VisualIntent)
from app.presentation_diagnosis import build_semantic_comparison_scope, classify_semantic_comparison
from app.providers.vision import VideoIntervalBatch
from app.voice_boundary_alignment import scene_narration_segment_spans


INTRO_VOICE_TEXT = "先明确使用场景。"


def comparison_scene_plans(spec, detail_plan):
    """The fixture's complete ordered copy, with a separate introductory scene."""
    intro = detail_plan.model_copy(update={"id": uuid4(), "scene_id": "intro", "order": 0,
        "voice_text": INTRO_VOICE_TEXT})
    return [intro, detail_plan]


def verified_comparison_interval(spec, detail_plan):
    """Reuse product mapping from actual persisted segments; never estimate time."""
    master = spec.master_narration
    actual_copy = "\n\n".join(segment.text for segment in master.transcript_segments)
    audio = AudioAsset(id=master.audio_asset_id, source_file="fixture-master.wav", content_hash="b"*64,
        duration_ms=master.end_ms, sample_rate=16000, channels=1, authorization_reference="fixture-rights",
        imported_at=datetime.now(timezone.utc), transcript_source=master.transcript_source,
        transcript_segments=master.transcript_segments,
        metadata={"voice_generation":{"qa_state":"verified", "target_text":actual_copy,
            "qa":{"missing_token_count":0,"duplicate_token_count":0,"substitution_token_count":0}}})
    try:
        scenes = comparison_scene_plans(spec, detail_plan)
        if scene_narration_segment_spans(audio, scenes) is None:
            return None
        mapped = _master_narration_intervals(audio, scenes, end_ms=master.end_ms)
    except NarrationTimelineError:
        return None
    return next((start,end) for scene,start,end in mapped if scene.id == detail_plan.id)


def comparison_fixture(*, fps=None):
    fps = fps or RationalFps(numerator=30, denominator=1)
    project, master_id = uuid4(), uuid4()
    asset = Asset(source_file="assets/originals/source.mp4", content_hash="a"*64, duration_ms=6000,
        width=1920, height=1080, fps=fps, authorization_reference="registered-rights",
        imported_at=datetime.now(timezone.utc))
    clip = Clip(asset_id=asset.id, start_ms=1000, end_ms=5500, asset_duration_ms=6000)
    plan = ScenePlan(project_id=project, scene_id="detail", order=1, purpose="解释实际使用情境",
        voice_text="完整旁白：按实际需求选择参数，不承诺实测提升。", duration_target_ms=1000,
        visual_intent=VisualIntent(subject="设置界面", action="展示日常使用", description="承接建议"),
        preferred_sources=[SourceKind.USER_ASSET], visual_requirement="explanatory",
        visual_requirement_reason="应景的使用画面，不增加实测证明要求。",
        viewer_takeaway="按需求选择参数", caption_emphasis=["按需求选择"])
    visual = VideoVisual(source_kind=asset.source_kind, authorization_reference=asset.authorization_reference,
        asset_id=asset.id, clip_id=clip.id, clip_start_ms=1000, clip_end_ms=5500, source_duration_ms=6000)
    seconds_ms = 30*1000*fps.denominator//fps.numerator
    scenes = [VideoScene(scene_id="intro" if i==0 else "detail", start_frame=i*30, duration_frames=30,
        visual=visual, narration_asset_id=master_id, narration_start_ms=i*seconds_ms,
        narration_end_ms=(i+1)*seconds_ms) for i in range(2)]
    spec = VideoSpec(project_id=project, format="vertical", width=1080, height=1920, fps=fps, scenes=scenes,
        master_narration=MasterNarration(audio_asset_id=master_id, end_ms=seconds_ms*2,
            transcript_source="fixture:independent-asr", transcript_segments=[
                TranscriptSegment(start_ms=100, end_ms=seconds_ms, text=INTRO_VOICE_TEXT),
                TranscriptSegment(start_ms=seconds_ms+100, end_ms=seconds_ms*2, text=plan.voice_text)]))
    return spec, plan, asset, clip


def scope_fixture():
    spec, plan, asset, clip = comparison_fixture()
    return build_semantic_comparison_scope(spec, plan.scene_id, plan, asset, clip, master_verified=True,
        verified_scene_interval=verified_comparison_interval(spec,plan))


def batch(scope, *, source=True, render=False, fit="context"):
    rows=[]
    for role, supported in (("render",render),("source",source)):
        rows.append({"clip_id":scope[role]["clip_id"], "description":"完整上传区间中的可见对象与动作",
            "content_consistent":True, "comparison_layout":"none", "subtitle_regions":[],
            "protected_regions":[], "matches":[{"scene_key":scope["scene_key"], "supported":supported,
                "score":.9 if supported else .99, "reason":"fixture显式判断", "fit":fit,
                "quality_adequate":True, "subject_verified":supported}], "reason":"fixture观察"})
    return VideoIntervalBatch.model_validate({"clips":rows})


def test_scope_uses_exact_render_frames_and_only_source_frames_shown_without_shortening_semantic_request():
    spec, plan, asset, clip = comparison_fixture(fps=RationalFps(numerator=30000,denominator=1001))
    before = [value.model_dump(mode="json") for value in (spec,plan,asset,clip)]
    scope=build_semantic_comparison_scope(spec,plan.scene_id,plan,asset,clip,master_verified=True,
        verified_scene_interval=verified_comparison_interval(spec,plan))
    assert scope["state"]=="ready"
    assert scope["render"]["start_frame"]==30 and scope["render"]["end_frame"]==60
    assert (scope["render"]["start_ms"],scope["render"]["end_ms"])==(1001,2002)
    assert (scope["source"]["start_ms"],scope["source"]["end_ms"])==(1001,2002)
    assert scope["source"]["end_ms"] < clip.end_ms
    assert scope["source"]["source_content_hash"]==asset.content_hash
    request=scope["scene_request"]
    assert request["voice_text"]==plan.voice_text
    assert request["visual_intent"]==plan.visual_intent.model_dump(mode="json")
    assert request["visual_requirement_reason"]==plan.visual_requirement_reason
    assert request["viewer_takeaway"]==plan.viewer_takeaway
    assert request["caption_emphasis"]==plan.caption_emphasis
    assert request["comparison_task"]=="explicit_render_source_semantic_comparison"
    assert [value.model_dump(mode="json") for value in (spec,plan,asset,clip)]==before


def test_long_valid_scene_id_stays_within_provider_clip_identity_limit():
    spec,plan,asset,clip=comparison_fixture()
    plan=plan.model_copy(update={"scene_id":"长"*100})
    spec=spec.model_copy(update={"scenes":[spec.scenes[0],spec.scenes[1].model_copy(update={"scene_id":plan.scene_id})]})
    scope=build_semantic_comparison_scope(spec,plan.scene_id,plan,asset,clip,master_verified=True,
        verified_scene_interval=verified_comparison_interval(spec,plan))
    assert scope["state"]=="ready"
    assert max(len(scope[role]["clip_id"]) for role in ("source","render"))<=100
    assert scope["source"]["clip_id"]!=scope["render"]["clip_id"]
    assert classify_semantic_comparison(scope,batch(scope))["responsible_stage"]=="assembly"


def test_positive_match_must_also_meet_the_existing_material_admission_floor():
    scope=scope_fixture();result=batch(scope)
    result.clips[1].matches[0].score=.01
    assert classify_semantic_comparison(scope,result)['state']=='unknown'


def test_technical_master_pass_without_a_current_copy_to_time_mapping_cannot_prepare_a_call():
    spec,plan,asset,clip=comparison_fixture()
    scope=build_semantic_comparison_scope(spec,plan.scene_id,plan,asset,clip,master_verified=True)
    assert scope["state"]=="unknown"
    assert scope["stop_reasons"]==["semantic_scene_narration_copy_interval_unverified"]
    assert "scene_request" not in scope and "render" not in scope


@pytest.mark.parametrize("change",["original_cross_scene_segment","wrong_scene_words","partial_scene_words",
    "extra_undeclared_words","boundary_in_silence",
    "boundary_inside_speech","wrong_end_boundary"])
def test_ambiguous_copy_or_wrong_audio_interval_never_prepares_video_comparison(change):
    spec,plan,asset,clip=comparison_fixture()
    if change=="original_cross_scene_segment":
        # The original unsafe fixture: detail text exists only in a segment
        # spanning both scenes, so its actual subscene timing is unavailable.
        master=spec.master_narration.model_copy(update={"transcript_segments":[
            TranscriptSegment(start_ms=0,end_ms=2000,text=plan.voice_text)]})
        spec=spec.model_copy(update={"master_narration":master})
    if change=="wrong_scene_words":
        plan=plan.model_copy(update={"voice_text":"错误文案：这些参数一定提高实测结果。"})
    if change=="partial_scene_words":
        plan=plan.model_copy(update={"voice_text":"按实际需求选择参数"})
    if change=="extra_undeclared_words":
        segments=spec.master_narration.transcript_segments
        master=spec.master_narration.model_copy(update={"transcript_segments":[segments[0],
            segments[1].model_copy(update={"text":segments[1].text+"另有未声明的内容。"})]})
        spec=spec.model_copy(update={"master_narration":master})
    if change in {"boundary_in_silence","boundary_inside_speech","wrong_end_boundary"}:
        altered=spec.scenes[1].model_copy(update={
            "narration_start_ms":1001 if change=="boundary_in_silence" else 1150 if change=="boundary_inside_speech" else 1000,
            "narration_end_ms":1999 if change=="wrong_end_boundary" else 2000})
        spec=spec.model_copy(update={"scenes":[spec.scenes[0],altered]})
    interval=verified_comparison_interval(spec,plan)
    if change in {"original_cross_scene_segment","wrong_scene_words","partial_scene_words","extra_undeclared_words"}:assert interval is None
    else:assert interval==(1000,2000)
    scope=build_semantic_comparison_scope(spec,plan.scene_id,plan,asset,clip,master_verified=True,
        verified_scene_interval=interval)
    assert scope["state"]=="unknown"
    assert any("semantic_scene_narration_copy_interval" in reason for reason in scope["stop_reasons"])
    assert "scene_request" not in scope and "render" not in scope


@pytest.mark.parametrize("change,reason",[
    ("no_master_qa","verified_master"),("no_source","source_missing"),("wrong_source","identity_mismatch"),
    ("wrong_clip","identity_mismatch"),("creator_speaking","talking_modality"),
    ("talking_role","talking_modality"),("generated","real_footage"),
    ("graphic","real_footage"),("unknown_task","visual_task_unknown"),
    ("missing_interval","interval_mismatch"),("short_source","cover_rendered_frames"),
    ("wrong_plan","scene_plan_mismatch"),("too_long","exceeds_video_analysis_scope"),
])
def test_unsupported_or_unbound_scope_stays_unknown(change,reason):
    spec,plan,asset,clip=comparison_fixture()
    scene=spec.scenes[1]
    if change=="no_source":asset=None
    if change=="wrong_source":asset=asset.model_copy(update={"id":uuid4()})
    if change=="wrong_clip":clip=clip.model_copy(update={"id":uuid4()})
    if change=="creator_speaking":plan=plan.model_copy(update={"visual_requirement":"creator_speaking"})
    if change=="talking_role":scene=scene.model_copy(update={"visual_role":"talking"})
    if change=="generated":scene=scene.model_copy(update={"visual":scene.visual.model_copy(update={"source_kind":SourceKind.AI_BROLL})})
    if change=="graphic":scene=scene.model_copy(update={"visual_role":"graphic"})
    if change=="unknown_task":plan=plan.model_copy(update={"visual_requirement":"unknown"})
    if change=="missing_interval":scene=scene.model_copy(update={"visual":scene.visual.model_copy(update={"clip_start_ms":None})})
    if change=="short_source":scene=scene.model_copy(update={"visual":scene.visual.model_copy(update={"clip_end_ms":1500})})
    if change=="wrong_plan":plan=plan.model_copy(update={"project_id":uuid4()})
    if change=="too_long":scene=scene.model_copy(update={"duration_frames":721})
    # Validate enum values for copy-updated scene fields, without cross-scene fixture timing.
    if change in {"talking_role","graphic"}:
        scene=scene.model_copy(update={"visual_role":type(spec.scenes[1].visual_role)(scene.visual_role)})
    spec=spec.model_copy(update={"scenes":[spec.scenes[0],scene]})
    scope=build_semantic_comparison_scope(spec,plan.scene_id,plan,asset,clip,master_verified=change!="no_master_qa",
        verified_scene_interval=verified_comparison_interval(spec,plan))
    assert scope["state"]=="unknown" and scope["responsible_stage"]=="unknown"
    assert any(reason in item for item in scope["stop_reasons"])
    assert "scene_request" not in scope


@pytest.mark.parametrize("source,render,stage",[(True,False,"assembly"),(False,False,"material"),
    (True,True,"unknown"),(False,True,"unknown")])
@pytest.mark.parametrize("fit",["context","illustrative","direct"])
def test_explicit_same_task_observations_locate_only_supported_root_causes(source,render,stage,fit):
    scope=scope_fixture(); result=batch(scope,source=source,render=render,fit=fit)
    before=deepcopy(scope),result.model_dump(mode="json")
    diagnosis=classify_semantic_comparison(scope,result)
    assert diagnosis["responsible_stage"]==stage
    assert diagnosis["state"]==("unknown" if stage=="unknown" else "known")
    assert diagnosis["source"]=="machine_semantic_comparison"
    assert diagnosis["observations"]["source"]["matches"][0]["supported"] is source
    assert "naturalness_and_visible_sync_not_assessed" in diagnosis["limitations"]
    assert "diagnosis_does_not_authorize_repair" in diagnosis["limitations"]
    assert "approved" not in diagnosis and "score" not in diagnosis
    assert (scope,result.model_dump(mode="json"))==before


@pytest.mark.parametrize("change",["missing_negative","wrong_scene","wrong_clip","duplicate_clip", "unknown_fit",
    "unknown_quality","bad_quality","inconsistent", "negative_subject_unknown", "negative_subject_matches",
    "action_context", "action_subject"])
def test_uncertain_or_incomplete_comparison_never_becomes_known_diagnosis(change):
    scope=scope_fixture(); result=batch(scope)
    if change=="missing_negative":result.clips[0].matches=[]
    if change=="wrong_scene":result.clips[0].matches[0].scene_key="unrelated"
    if change=="wrong_clip":result.clips[0].clip_id="unrelated"
    if change=="duplicate_clip":result.clips[0].clip_id=result.clips[1].clip_id
    if change=="unknown_fit":result.clips[0].matches[0].fit="unknown"
    if change=="unknown_quality":result.clips[0].matches[0].quality_adequate=None
    if change=="bad_quality":result.clips[0].matches[0].quality_adequate=False
    if change=="inconsistent":result.clips[0].content_consistent=False
    if change=="negative_subject_unknown":result.clips[0].matches[0].subject_verified=None
    if change=="negative_subject_matches":result.clips[0].matches[0].subject_verified=True
    if change in {"action_context","action_subject"}:
        scope["scene_request"]["visual_requirement"]="action_evidence"
        if change=="action_subject":
            result.clips[1].matches[0].fit="direct";result.clips[1].matches[0].subject_verified=None
    diagnosis=classify_semantic_comparison(scope,result)
    assert diagnosis["state"]=="unknown" and diagnosis["responsible_stage"]=="unknown"
    assert diagnosis["stop_reasons"]


def test_unknown_scope_cannot_be_promoted_by_positive_or_negative_provider_result():
    scope=scope_fixture(); result=batch(scope)
    scope["state"]="unknown"
    assert classify_semantic_comparison(scope,result)["state"]=="unknown"
