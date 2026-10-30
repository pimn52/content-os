"""Pure classification and bounded corrections for exact presentation defects.

These pure functions neither open media nor call a provider. The caller owns
current-version, file-hash, proxy and provider-accounting checks. A machine
observation here is never a Voice/Talking or publication quality approval.
"""
from __future__ import annotations

import hashlib

from app.assembly.edit_plan import source_frame_interval_ms, _normalized
from app.domain.models import Asset, Clip, EditVisualRole, ScenePlan, SourceKind, VideoSpec
from app.media.visual_facts import MIN_SCENE_MATCH_SCORE, scene_visual_key
from app.providers.vision import VideoIntervalBatch


_FOOTAGE = {SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET, SourceKind.STOCK}
_LIMITATIONS = ["audio_not_observed", "naturalness_and_visible_sync_not_assessed",
                "human_quality_review_required", "diagnosis_does_not_authorize_repair"]
DETERMINISTIC_PRESENTATION_FINDINGS = ("duplicate_text", "known_subtitle_conflict")
PRESENTATION_CORRECTIONS = frozenset((*DETERMINISTIC_PRESENTATION_FINDINGS, "text_overlay"))


def _entry_document(entry):
    values = entry.model_dump(mode='json')
    # Serialized defaults are omitted, but a replacement must explicitly clear old source proof.
    values.update(source_bottom_crop_ratio=entry.source_bottom_crop_ratio,
        source_subtitle_evidence_reference=entry.source_subtitle_evidence_reference)
    return values


def diagnosis_for_finding(finding):
    """Route an observation; a named symptom alone does not locate a safe fix."""
    stage, capabilities = {
        "duplicate_text": ("assembly", ["exact_render_text_duplication_diagnosis"]),
        "known_subtitle_conflict": ("material", ["full_interval_source_subtitle_analysis"]),
        "text_overlay": ("assembly", ["exact_scene_text_face_layout_diagnosis"]),
        "face_out_of_frame": ("unknown", ["source_vs_render_framing_diagnosis"]),
        "source_change": ("material", ["scene_source_interval_selection_and_qa", "scoped_material_replacement"]),
        "unknown_subtitles": ("material", ["full_interval_source_subtitle_analysis"]),
        "voice_naturalness": ("voice", ["exact_audio_naturalness_diagnosis", "consumed_voice_dependency_repair"]),
        "talking_naturalness": ("talking", ["exact_talking_naturalness_diagnosis", "render_stage_talking_repair"]),
        "visible_sync": ("unknown", ["render_vs_talking_sync_diagnosis"]),
        "semantic_mismatch": ("unknown", ["exact_scene_narration_visual_semantics_diagnosis"]),
        "narrative_rhythm": ("timing", ["exact_scene_delivery_vs_edit_timing_diagnosis", "scoped_timing_reassembly"]),
    }.get(finding, ("unknown", ["exact_scene_defect_diagnosis"]))
    return {"responsible_stage": stage, "assessment_state": "unknown", "required_capability": capabilities}


def presentation_correction(old, item, draft):
    """每项针对同一基线诊断；仅修改已有装配字段。"""
    target = next((scene for scene in old.scenes if scene.scene_id == item["scene_id"]), None)
    if target is None:
        raise ValueError("presentation_observation_scene_mismatch")
    before = target.model_dump(mode="json")
    scene = dict(before)
    entry_model = next((entry for entry in old.edit_plan.scenes if entry.scene_id == item["scene_id"]), None) if old.edit_plan else None
    entry_before = None if entry_model is None else _entry_document(entry_model)
    entry = None if entry_before is None else dict(entry_before)
    diagnosis = diagnosis_for_finding(item["finding"])
    reasons = []
    observed = False
    if entry is None:
        reasons.append("presentation_scene_mapping_required")
    elif item["finding"] == "duplicate_text":
        captions = "".join(c["text"] for c in scene["captions"]) or scene["caption"] or ""
        observed = bool(scene["subtitle_treatment"] == "timed_captions" and captions and scene["graphic_text"]
            and _normalized(scene["graphic_text"]) == _normalized(captions))
        if not observed:
            reasons.append("presentation_no_supported_duplicate_candidate")
        elif scene["visual_role"] == "graphic":
            target_plan = next((value for value in draft.scenes if value.scene_id == item["scene_id"]), None) if draft else None
            short = None if target_plan is None or not target_plan.caption_emphasis else target_plan.caption_emphasis[0]
            if not short or _normalized(short) == _normalized(captions):
                reasons.append("presentation_semantic_short_text_required")
                diagnosis["required_capability"] = ["scene_semantic_short_emphasis_selection"]
            else:
                scene["graphic_text"] = entry["graphic_text"] = short
        else:
            scene["graphic_text"] = entry["graphic_text"] = None
            scene["graphic_treatment"] = entry["graphic_treatment"] = "none"
    elif item["finding"] == "known_subtitle_conflict":
        if (entry["burned_in_subtitles"] != "present" or not entry["selected_clip_id"]
            or scene["subtitle_treatment"] != "timed_captions" or entry["source_bottom_crop_ratio"]
            or scene['visual']['source_bottom_crop_ratio']):
            reasons.append("presentation_known_source_subtitles_required")
        else:
            observed = True
            scene["subtitle_treatment"] = entry["subtitle_treatment"] = "none"
    elif item["finding"] == "text_overlay":
        if (entry["visual_role"] != "talking" or entry["selected_source_kind"] != "ai_video"
            or old.width != 1080 or old.height != 1920
            or entry["framing_policy"] != "face_safe_contain" or entry["burned_in_subtitles"] == "unknown"
            or scene["portrait_presentation"] != "full_canvas" or not scene["graphic_text"]
            or scene["visual"]["vertical_reframe_mode"] != "contain" or scene["visual"]["source_bottom_crop_ratio"]):
            reasons.append("presentation_qualified_source_preserving_panel_required")
        else:
            scene["portrait_presentation"] = entry["portrait_presentation"] = "portrait_panel"
    else:
        reasons.append("presentation_" + item["finding"] + "_diagnosis_required")
    if not reasons and scene != before:
        diagnosis.update(responsible_stage="assembly", assessment_state="known", required_capability=[])
    return dict(scene_id=target.scene_id, before=before, after=scene, entry_before=entry_before,
        entry_after=entry, reasons=reasons, diagnosis=diagnosis, observed=observed)


def summarize_diagnoses(diagnoses):
    """同一版本的分类与定位，不包含预算、文件读取或执行资格。"""
    stages = {value["responsible_stage"] for value in diagnoses}
    return dict(responsible_stage=next(iter(stages)) if len(stages)==1 else "unknown",
        assessment_state="known" if diagnoses and all(value["assessment_state"]=="known" for value in diagnoses) else "unknown",
        required_capability=list(dict.fromkeys(capability for value in diagnoses for capability in value["required_capability"])))


def _unknown(reason, **values):
    return {"state": "unknown", "assessment_state": "unknown", "responsible_stage": "unknown",
            "stop_reasons": [reason], **values}


def build_semantic_comparison_scope(
    spec: VideoSpec, scene_id: str, scene_plan: ScenePlan,
    source_asset: Asset | None, source_clip: Clip | None, *, master_verified: bool,
    verified_scene_interval: tuple[int, int] | None = None,
) -> dict:
    """Describe one exact rendered frame range and its used source interval.

    Frame boundaries are authoritative for the render. Integer milliseconds
    are an enclosing interval, not a replacement for rational frame timing.
    Source timing uses the renderer's existing composition-frame ceiling rule.
    The caller must first require ``scene_narration_segment_spans`` for the
    current complete draft and QA-bound Master, then derive this interval using
    ``_master_narration_intervals``. That helper's substring fallback alone does
    not establish a complete copy match. Technical QA alone cannot bind words.
    """
    identity = {"scene_id": scene_id, "scene_plan_id": str(scene_plan.id)}
    if scene_plan.project_id != spec.project_id or scene_plan.scene_id != scene_id:
        return _unknown("semantic_scene_plan_mismatch", **identity)
    scenes = [scene for scene in spec.scenes if scene.scene_id == scene_id]
    if len(scenes) != 1:
        return _unknown("semantic_render_scene_missing_or_ambiguous", **identity)
    scene = scenes[0]
    if scene.visual_role is EditVisualRole.TALKING or scene_plan.visual_requirement == "creator_speaking":
        return _unknown("semantic_talking_modality_unsupported", **identity)
    if scene.visual_role is not EditVisualRole.FOOTAGE or scene.visual.source_kind not in _FOOTAGE:
        return _unknown("semantic_real_footage_source_required", **identity)
    if scene_plan.visual_requirement == "unknown":
        return _unknown("semantic_planned_visual_task_unknown", **identity)
    master = spec.master_narration
    if (master_verified is not True or master is None or scene.narration_asset_id != master.audio_asset_id
            or scene.narration_start_ms is None or scene.narration_end_ms is None
            or not master.start_ms <= scene.narration_start_ms < scene.narration_end_ms <= master.end_ms):
        return _unknown("semantic_verified_master_interval_required", **identity)
    if (not isinstance(verified_scene_interval, tuple) or len(verified_scene_interval) != 2
            or any(type(value) is not int for value in verified_scene_interval)):
        return _unknown("semantic_scene_narration_copy_interval_unverified", **identity)
    if verified_scene_interval != (scene.narration_start_ms, scene.narration_end_ms):
        return _unknown("semantic_scene_narration_copy_interval_mismatch", **identity)
    if source_asset is None or source_clip is None:
        return _unknown("semantic_original_source_missing", **identity)
    visual = scene.visual
    if (visual.asset_id != source_asset.id or visual.clip_id != source_clip.id
            or source_clip.asset_id != source_asset.id or visual.source_kind != source_asset.source_kind
            or source_clip.asset_duration_ms != source_asset.duration_ms
            or visual.source_duration_ms != source_asset.duration_ms
            or visual.authorization_reference != source_asset.authorization_reference
            or source_clip.end_ms > source_asset.duration_ms):
        return _unknown("semantic_source_identity_mismatch", **identity)
    if (source_clip.reference_source_timebase is not None or source_clip.talking_candidate
            or source_clip.talking_reference_assessment is not None or source_asset.metadata.get("talking_generation")):
        return _unknown("semantic_talking_modality_unsupported", **identity)
    if (visual.clip_start_ms is None or visual.clip_end_ms is None
            or not source_clip.start_ms <= visual.clip_start_ms < visual.clip_end_ms <= source_clip.end_ms):
        return _unknown("semantic_source_interval_mismatch", **identity)
    frame_ms = 1000 * spec.fps.denominator
    if scene.duration_frames * frame_ms > 24000 * spec.fps.numerator:
        return _unknown("semantic_scene_exceeds_video_analysis_scope", **identity)
    source_first_frame = (visual.clip_start_ms * spec.fps.numerator + frame_ms - 1) // frame_ms
    if (source_first_frame + scene.duration_frames - 1) * frame_ms >= visual.clip_end_ms * spec.fps.numerator:
        return _unknown("semantic_source_does_not_cover_rendered_frames", **identity)
    source_start, source_end = source_frame_interval_ms(
        visual.clip_start_ms, scene.duration_frames, spec.fps, visual.clip_end_ms,
    )
    if source_end - source_start > 24000:
        return _unknown("semantic_source_exceeds_video_analysis_scope", **identity)
    request = {"scene_key": scene_visual_key(scene_plan), "voice_text": scene_plan.voice_text,
               "purpose": scene_plan.purpose, "visual_intent": scene_plan.visual_intent.model_dump(mode="json"),
               "visual_requirement": scene_plan.visual_requirement,
               "visual_requirement_reason": scene_plan.visual_requirement_reason}
    if scene_plan.viewer_takeaway:
        request["viewer_takeaway"] = scene_plan.viewer_takeaway
    if scene_plan.caption_emphasis:
        request["caption_emphasis"] = list(scene_plan.caption_emphasis)
    if scene_plan.graphic_plan is not None and scene_plan.graphic_plan.kind != "none":
        request["graphic_plan"] = scene_plan.graphic_plan.model_dump(mode="json")
    # The same request applies to both clips; omission is not a negative result.
    request["comparison_task"] = "explicit_render_source_semantic_comparison"
    end_frame = scene.start_frame + scene.duration_frames
    clip_key = hashlib.sha256(f"{spec.project_id}:{scene_id}:{source_clip.id}".encode("utf-8")).hexdigest()[:24]
    return {"state": "ready", "stop_reasons": [], **identity, "scene_key": request["scene_key"],
            "scene_request": request,
            "render": {"clip_id": f"render:{clip_key}", "start_frame": scene.start_frame,
                       "end_frame": end_frame, "fps": spec.fps.model_dump(mode="json"),
                       "start_ms": scene.start_frame * frame_ms // spec.fps.numerator,
                       "end_ms": (end_frame * frame_ms + spec.fps.numerator - 1) // spec.fps.numerator},
            "source": {"clip_id": f"source:{clip_key}",
                       "source_asset_id": str(source_asset.id), "source_clip_id": str(source_clip.id),
                       "source_content_hash": source_asset.content_hash,
                       "start_ms": source_start, "end_ms": source_end},
            "master": {"audio_asset_id": str(master.audio_asset_id),
                       "start_ms": scene.narration_start_ms, "end_ms": scene.narration_end_ms}}


def classify_semantic_comparison(scope: dict, result: VideoIntervalBatch) -> dict:
    """Locate only a supported source/render disagreement or joint rejection.

    No numeric score is promoted into diagnostic certainty. Explicit known
    fit and adequate visibility are required for both positive and negative
    observations; rejection additionally requires a visibly mismatched subject.
    Neither a missing match nor an unknown fit means rejection.
    """
    base = {"source": "machine_semantic_comparison", "method": "same_scene_render_source_video_comparison",
            "scene_id": scope.get("scene_id"), "scene_key": scope.get("scene_key"),
            "assessed_dimensions": ["visual_semantics_against_verified_narration_text"],
            "limitations": list(_LIMITATIONS)}
    if scope.get("state") != "ready":
        return _unknown("semantic_comparison_scope_unavailable", **base)
    expected = {scope[role]["clip_id"]: role for role in ("render", "source")}
    if len(result.clips) != 2 or len(expected) != 2 or {row.clip_id for row in result.clips} != set(expected):
        return _unknown("semantic_comparison_clip_identity_mismatch", **base)
    observations = {expected[row.clip_id]: row.model_dump(mode="json") for row in result.clips}
    base["observations"] = observations
    statuses = {}
    for row in result.clips:
        if len(row.matches) != 1 or row.matches[0].scene_key != scope["scene_key"]:
            return _unknown("semantic_comparison_explicit_scene_match_required", **base)
        match = row.matches[0]
        if not row.content_consistent or match.fit == "unknown" or match.quality_adequate is not True:
            return _unknown("semantic_comparison_observation_uncertain", **base)
        if not match.supported and match.subject_verified is not False:
            return _unknown("semantic_comparison_negative_subject_unverified", **base)
        if match.supported and match.score < MIN_SCENE_MATCH_SCORE:
            return _unknown("semantic_comparison_source_match_not_qualified", **base)
        if (match.supported and scope["scene_request"]["visual_requirement"] == "action_evidence"
                and (match.fit != "direct" or match.subject_verified is not True)):
            return _unknown("semantic_comparison_direct_evidence_unverified", **base)
        statuses[expected[row.clip_id]] = match.supported
    if statuses["source"] is True and statuses["render"] is False:
        stage = "assembly"
    elif statuses["source"] is False and statuses["render"] is False:
        stage = "material"
    else:
        return _unknown("semantic_comparison_does_not_locate_reported_mismatch", **base)
    return {"state": "known", "assessment_state": "known", "responsible_stage": stage,
            "stop_reasons": [], **base}


def classify_material_recheck(scope: dict, result: VideoIntervalBatch) -> dict:
    """Recheck only the replaced scene, retaining the existing observation bar.

    A positive pair establishes this bounded machine check, not whole-video
    quality or human approval. A negative/uncertain pair never authorizes a
    further replacement attempt.
    """
    verdict = classify_semantic_comparison(scope, result)
    verdict['limitations'] = [*verdict['limitations'],'only_replaced_scene_visual_semantics_rechecked']
    if verdict["state"] == "known":
        return {**verdict, "state": "failed", "required_capability": [
            "scoped_material_replacement" if verdict["responsible_stage"] == "material" else "semantic_layout_correction"]}
    if (verdict["stop_reasons"] == ["semantic_comparison_does_not_locate_reported_mismatch"]
            and all(row.matches[0].supported for row in result.clips)):
        return {**verdict, "state": "pass", "assessment_state": "known", "responsible_stage": "material",
                "stop_reasons": [], "required_capability": []}
    return {**verdict, "required_capability": ["exact_scene_narration_visual_semantics_diagnosis"]}
