"""Named exact-input assembly using existing EditPlan and timing contracts."""
from app.assembly.edit_plan import resolve_edit_plan, presentation_for_style
from app.assembly.video_spec import _audio_captions_for_interval, _master_offset_to_frames as timeline_frame, _edit_scene_fields
from app.domain.models import (CostCategory, MasterNarration,
    SourceKind, SubtitleTreatment, UsageCost, VideoScene, VideoSpec, VideoVisual)


def assemble_retained_composition(project, audio, intervals, inputs, style):
    cost = UsageCost(category=CostCategory.RENDER, amount=None, currency=None, note="local composition; external API cash cost0; compute cost unknown")
    choices = {scene.id: inputs[scene.id]["candidate"] for scene, _, _ in intervals}
    ordered = [s for s, _, _ in intervals]
    clips = {}
    for value in inputs.values():
        clip = value["clip"]
        if clip is None:
            continue
        first,last = clips.get(clip.id,(clip.start_ms,clip.end_ms))
        clips[clip.id] = (min(first,clip.start_ms),max(last,clip.end_ms))
    plan = resolve_edit_plan(project, ordered, choices, clip_intervals=clips, supplied=None)
    # Retain the reviewed complete frame. Unknown source subtitles never
    # authorize another overlay, even when a legacy source is landscape.
    entries = [presentation_for_style(e.model_copy(update={
        "subtitle_treatment":SubtitleTreatment.TIMED_CAPTIONS if inputs[e.scene_plan_id]["asset"] is None or "source_derivation" in inputs[e.scene_plan_id]["asset"].metadata else SubtitleTreatment.NONE,
        "burned_in_subtitles":"absent" if inputs[e.scene_plan_id]["asset"] is None or "source_derivation" in inputs[e.scene_plan_id]["asset"].metadata else "unknown"}),style,
        inputs[e.scene_plan_id]["asset"]) for e in plan.scenes]
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
        if clip is not None and clip.end_ms*project.fps.numerator//(1000*project.fps.denominator) <= timeline_frame(clip.start_ms,project.fps):
            raise ValueError("composition_source_has_no_complete_frame")
        scenes.append(VideoScene(scene_id=scene.scene_id, start_frame=first, duration_frames=last-first,
            visual=VideoVisual(source_kind=item["candidate"].source_kind,
                authorization_reference="local-typography" if asset is None else asset.authorization_reference,
                asset_id=None if asset is None else asset.id, clip_id=None if clip is None else clip.id,
                clip_start_ms=None if clip is None else clip.start_ms,
                clip_end_ms=None if clip is None else clip.end_ms, source_duration_ms=None if asset is None else asset.duration_ms),
            narration_asset_id=audio.id, narration_start_ms=start, narration_end_ms=end,
            caption=scene.voice_text, captions=_audio_captions_for_interval(audio,start,end),
            **_edit_scene_fields(entry,plan)))
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
