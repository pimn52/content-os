"""Named exact-input assembly using existing EditPlan and timing contracts."""
import re
from app.assembly.edit_plan import resolve_edit_plan, presentation_for_style
from app.assembly.video_spec import _audio_captions_for_interval, _master_offset_to_frames as timeline_frame, _edit_scene_fields, _talking_visual_treatment
from app.domain.models import (CostCategory, MasterNarration,
    SourceKind, SubtitleTreatment, UsageCost, VideoCaption, VideoScene, VideoSpec, VideoVisual)


def _approved_scene_captions(audio, ordered, scene, start, end):
    """Display approved copy at verified word positions; never rewrite ASR."""
    from app.production_runs import _unique_scene_speech_span
    from app.voice_qa import comparison_tokens
    evidence = audio.metadata.get("voice_word_timing",{})
    if (audio.metadata.get("voice_generation",{}).get("qa_state") != "verified"
        or evidence.get("source_sha256") != audio.content_hash):
        original = _audio_captions_for_interval(audio,start,end)
        return original if "".join(c.text for c in original) == scene.voice_text else []
    if _unique_scene_speech_span(audio,ordered,scene.id) != (start,end):
        return []
    words = [w for w in evidence.get("words",[])
        if w["start_ms"] >= start and w["end_ms"] <= end]
    matches = list(re.finditer(r"[A-Za-z0-9]+|[\u4e00-\u9fff]",scene.voice_text))
    if (not words or tuple(t for m in matches for t in comparison_tokens(m.group())) != comparison_tokens(scene.voice_text)
        or sum(len(comparison_tokens(w["text"])) for w in words) != len(matches)):
        # Older exact-transcript media can still use their original timings;
        # uncertain display alignment must not invent synchronized captions.
        original = _audio_captions_for_interval(audio,start,end)
        return original if "".join(c.text for c in original) == scene.voice_text else []
    captions = [];cursor = group_start = 0;first = words[0]["start_ms"]
    for index,word in enumerate(words):
        cursor += len(comparison_tokens(word["text"]))
        left = 0 if group_start == 0 else matches[group_start].start()
        right = matches[cursor].start() if cursor < len(matches) else len(scene.voice_text)
        if (cursor-group_start < 18 and index < len(words)-1
            and not re.search(r"[，。；！？,;!?]$",scene.voice_text[left:right].strip())):
            continue
        captions.append(VideoCaption(start_ms=first-start,end_ms=word["end_ms"]-start,text=scene.voice_text[left:right].strip()))
        group_start = cursor
        if index+1 < len(words):first = words[index+1]["start_ms"]
    return captions


def assemble_retained_composition(project, audio, intervals, inputs, style):
    cost = UsageCost(category=CostCategory.RENDER, amount=None, currency=None, note="local composition; external API cash cost0; compute cost unknown")
    choices = {scene.id: inputs[scene.id]["candidate"] for scene, _, _ in intervals}
    ordered = [s for s, _, _ in intervals]
    # An explicit revised visual plan opts into approved display copy. Keep
    # already-admitted historical specs/results byte-for-byte reproducible.
    approved_display = any(s.graphic_plan and s.graphic_plan.kind == "diagram" for s in ordered)
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
    scene_by_id = {scene.id: scene for scene, _, _ in intervals}
    entries = [presentation_for_style(e.model_copy(update={
        "subtitle_treatment":SubtitleTreatment.TIMED_CAPTIONS if inputs[e.scene_plan_id]["asset"] is None or "source_derivation" in inputs[e.scene_plan_id]["asset"].metadata else SubtitleTreatment.NONE,
        "burned_in_subtitles":"absent" if inputs[e.scene_plan_id]["asset"] is None or "source_derivation" in inputs[e.scene_plan_id]["asset"].metadata else "unknown"}),style,
        inputs[e.scene_plan_id]["asset"],scene_plan=scene_by_id[e.scene_plan_id],clip=inputs[e.scene_plan_id]["clip"]) for e in plan.scenes]
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
        captions = _approved_scene_captions(audio,ordered,scene,start,end) if approved_display else _audio_captions_for_interval(audio,start,end)
        fields = _edit_scene_fields(entry,plan)
        treatment = ("contain",None,0) if asset is None else _talking_visual_treatment(asset,entry)
        if approved_display and not captions:
            fields["subtitle_treatment"] = SubtitleTreatment.NONE
        scenes.append(VideoScene(scene_id=scene.scene_id, start_frame=first, duration_frames=last-first,
            visual=VideoVisual(source_kind=item["candidate"].source_kind,
                authorization_reference="local-typography" if asset is None else asset.authorization_reference,
                asset_id=None if asset is None else asset.id, clip_id=None if clip is None else clip.id,
                clip_start_ms=None if clip is None else clip.start_ms,
                clip_end_ms=None if clip is None else clip.end_ms, source_duration_ms=None if asset is None else asset.duration_ms,
                vertical_reframe_mode=treatment[0],vertical_reframe_evidence_reference=treatment[1],source_bottom_crop_ratio=treatment[2]),
            narration_asset_id=audio.id, narration_start_ms=start, narration_end_ms=end,
            caption=scene.voice_text, captions=captions,
            **fields))
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
