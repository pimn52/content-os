"""Named exact-input assembly using existing EditPlan and timing contracts."""
from app.assembly.edit_plan import resolve_edit_plan
from app.assembly.video_spec import _audio_captions_for_interval, _master_offset_to_frames as timeline_frame
from app.domain.models import (CandidateAsset, CostCategory, MasterNarration, PortraitPresentation,
    SourceKind, UsageCost, VideoScene, VideoSpec, VideoVisual)


def assemble_retained_composition(project, audio, intervals, inputs, style):
    cost = UsageCost(category=CostCategory.RENDER, amount="0", currency="USD", note="local composition; compute cost unknown")
    choices = {scene.id: CandidateAsset(scene_plan_id=scene.id, source_kind=SourceKind.AI_VIDEO,
        asset_id=inputs[scene.id]["asset"].id, clip_id=inputs[scene.id]["clip"].id,
        match_score=1, why=["Explicit exact approved TalkingRun"], recommended=False, estimated_cost=cost)
        for scene, _, _ in intervals}
    ordered = [s for s, _, _ in intervals]
    clips = {v["clip"].id: (v["clip"].start_ms,v["clip"].end_ms) for v in inputs.values()}
    plan = resolve_edit_plan(project, ordered, choices, clip_intervals=clips, supplied=None)
    entries = [e.model_copy(update={"portrait_presentation": PortraitPresentation.PORTRAIT_PANEL})
        if style.name == "content-os-subject-first" else e for e in plan.scenes]
    plan = plan.model_copy(update={"scenes": entries, "style_tokens": style})
    plan = resolve_edit_plan(project, ordered, choices, clip_intervals=clips, supplied=plan)
    scenes = []
    cursor_ms = 0
    for (scene, start, end), entry in zip(intervals, plan.scenes):
        if start != cursor_ms:
            raise ValueError("composition_speech_gap_requires_explicit_plan")
        item = inputs[scene.id]
        asset, clip = item["asset"], item["clip"]
        first, last = timeline_frame(start,project.fps), timeline_frame(end,project.fps)
        if clip.end_ms*project.fps.numerator//(1000*project.fps.denominator) <= timeline_frame(clip.start_ms,project.fps):
            raise ValueError("composition_source_has_no_complete_frame")
        scenes.append(VideoScene(scene_id=scene.scene_id, start_frame=first, duration_frames=last-first,
            visual=VideoVisual(source_kind=SourceKind.AI_VIDEO, authorization_reference=asset.authorization_reference,
                asset_id=asset.id, clip_id=clip.id, clip_start_ms=clip.start_ms,
                clip_end_ms=clip.end_ms, source_duration_ms=asset.duration_ms),
            narration_asset_id=audio.id, narration_start_ms=start, narration_end_ms=end,
            caption=scene.voice_text, captions=_audio_captions_for_interval(audio,start,end),
            visual_role=entry.visual_role, framing_policy=entry.framing_policy,
            portrait_presentation=entry.portrait_presentation, subtitle_treatment=entry.subtitle_treatment,
            style_tokens=style))
        cursor_ms = end
    if cursor_ms < audio.duration_ms:
        first, last = timeline_frame(cursor_ms,project.fps), timeline_frame(audio.duration_ms,project.fps)
        if last <= first:
            raise ValueError("composition_tail_not_frame_representable")
        scenes.append(VideoScene(scene_id="composition-neutral-tail",start_frame=first,duration_frames=last-first,
            visual=VideoVisual(source_kind=SourceKind.TYPOGRAPHY,authorization_reference="non-speaking-neutral-tail"),
            narration_asset_id=audio.id,narration_start_ms=cursor_ms,narration_end_ms=audio.duration_ms,
            subtitle_treatment="none",graphic_treatment="none",style_tokens=style))
    return VideoSpec(project_id=project.id,format=project.format,width=1080,height=1920,fps=project.fps,
        scenes=scenes,master_narration=MasterNarration(audio_asset_id=audio.id,start_ms=0,end_ms=audio.duration_ms,
            transcript_source=audio.transcript_source,transcript_segments=audio.transcript_segments),
        edit_plan=plan,estimated_cost=cost)
