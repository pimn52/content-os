"""来源画面事实的范围校验；无数据库写入、提供商或编辑布局依赖。"""
from __future__ import annotations

import re
import hashlib
import json
import math


def subtitle_keep_height(height, regions, *, machine=False):
    """机器定位留出边缘误差，连续人工复核保持其既有裁除精度。"""
    margin=max(2,math.ceil(height*.015)) if machine else 2
    return max(1,math.floor(min(box[1] for box in regions)*height)-margin)


def visual_review_task(requirement):
    """沿用规划的画面任务；解释性画面不自动背负实测证明义务。"""
    task = {"action_evidence": "verify_required_factual_evidence",
        "explanatory": "verify_planned_explanatory_use"}.get(requirement, "verify_explicit_visual_use")
    return {"task": task, "preserve_explicit_visual_requirements": True,
        "viewer_takeaway_is_additional_proof_requirement": False,
        "planned_context_may_support": requirement == "explanatory",
        "context_cannot_establish_measured_results": True,
        "unrelated_or_misleading_visuals": "reject"}


def scene_visual_key(scene):
    """语义缓存按实际文案和画面要求匹配，不受实测时长调整影响。"""
    request = {
        "voice_text":scene.voice_text, "purpose":scene.purpose,
        "visual_intent":scene.visual_intent.model_dump(mode="json"),
        "visual_requirement":scene.visual_requirement,
        "visual_requirement_reason":scene.visual_requirement_reason,
        "review_task":visual_review_task(scene.visual_requirement),
    }
    if scene.caption_emphasis:
        request["caption_emphasis"] = scene.caption_emphasis
    if scene.graphic_plan is not None and scene.graphic_plan.kind != "none":
        request["graphic_plan"] = scene.graphic_plan.model_dump(mode="json")
    if scene.viewer_takeaway:
        request["viewer_takeaway"] = scene.viewer_takeaway
    return hashlib.sha256(json.dumps(request, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def generated_footage_admitted(asset):
    """读取生成时保存的模型准入及来源，不把来源标签当作质量核验。"""
    facts=asset.metadata.get("visual_generation",{})
    return (asset.source_kind.value=="ai_broll" and isinstance(facts,dict)
        and facts.get("source_content_hash")==asset.content_hash
        and all(isinstance(facts.get(key),str) and facts[key].strip()
            for key in ("provider","model","license_evidence_reference"))
        and facts.get("commercial_status") in {"commercial_safe","non_commercial_only"})


def material_origin(asset):
    if asset.source_kind.value=="ai_broll" or asset.metadata.get("visual_generation"):
        return "generated"
    if asset.source_kind.value=="stock" or asset.metadata.get("external_sources"):
        return "external"
    return "creator_provided"


def _requested_source_interval(clip, playback_interval_ms=None):
    if clip is None:
        return None
    if playback_interval_ms is None:
        return clip.start_ms, clip.end_ms
    if (not isinstance(playback_interval_ms, tuple) or len(playback_interval_ms) != 2
        or any(isinstance(value, bool) or not isinstance(value, int) for value in playback_interval_ms)):
        return None
    start_ms, end_ms = playback_interval_ms
    if not clip.start_ms <= start_ms < end_ms <= clip.end_ms:
        return None
    return start_ms, end_ms


def footage_geometry(asset, clip, playback_interval_ms=None):
    """Read existing source-analysis facts; stale or unrelated intervals are unknown."""
    if asset is None or clip is None:
        return {}
    requested = _requested_source_interval(clip, playback_interval_ms)
    if requested is None:
        return {}
    requested_start_ms, requested_end_ms = requested
    reviews = asset.metadata.get("source_presentation_reviews", {})
    review = reviews.get(str(clip.id), {}) if isinstance(reviews, dict) else {}
    crop = review.get("crop_window") if isinstance(review, dict) else None
    review_interval = review.get("clip_interval") if isinstance(review, dict) else None
    evidence_sha256 = review.get("evidence_sha256") if isinstance(review, dict) else None
    if (isinstance(review, dict) and isinstance(crop, dict) and isinstance(review_interval, dict)
        and review.get("source_asset_id") == str(asset.id)
        and review.get("source_clip_id") == str(clip.id)
        and review.get("source_content_hash") == asset.content_hash
        and review.get("method") == "human_full_interval_review"
        and review.get("decision") == "approved"
        and review.get("coverage") == "full_interval_continuous"
        and review.get("comparison_layout") == "side_by_side_equal_halves"
        and review.get("source_subtitle_clearance_verified") is True
        and isinstance(review.get("reviewer_reference"), str) and review["reviewer_reference"].strip()
        and isinstance(review.get("evidence_reference"), str) and review["evidence_reference"].strip()
        and isinstance(evidence_sha256, str) and re.fullmatch(r"[0-9a-f]{64}", evidence_sha256)
        and all(type(crop.get(key)) is int for key in ("x", "y", "width", "height"))
        and crop["x"] == 0 and crop["y"] == 0 and crop["width"] == asset.width
        and 0 < crop["height"] <= asset.height
        and type(review_interval.get("start_ms")) is int
        and type(review_interval.get("end_ms")) is int
        and type(review.get("covered_start_ms")) is int
        and type(review.get("covered_end_ms")) is int
        and review_interval["start_ms"] <= requested_start_ms
        and review_interval["end_ms"] >= requested_end_ms
        and review.get("covered_start_ms", clip.end_ms) <= requested_start_ms
        and review.get("covered_end_ms", clip.start_ms) >= requested_end_ms):
        return review
    analysis=asset.metadata.get("visual_analysis",{})
    facts=analysis.get(str(clip.id),{}) if isinstance(analysis,dict) else {}
    if not isinstance(facts,dict):return {}
    interval = facts.get("clip_interval",facts)
    if (not isinstance(interval, dict) or facts.get("source_content_hash") != asset.content_hash
        or type(interval.get("start_ms")) is not int or type(interval.get("end_ms")) is not int
        or not clip.start_ms <= interval["start_ms"] <= requested_start_ms
        or not requested_end_ms <= interval["end_ms"] <= clip.end_ms):
        return {}
    return facts

def full_interval_geometry(asset, clip, playback_interval_ms=None):
    """消费连续复核或实际视频区间核验，单帧不能升级为区间核验。"""
    requested = _requested_source_interval(clip, playback_interval_ms)
    facts = footage_geometry(asset, clip, playback_interval_ms)
    video_analysis = (facts.get("coverage") == "video_interval_analysis"
        and facts.get("method") == "provider_video_interval_analysis"
        and facts.get("content_consistent") is True
        and isinstance(facts.get("analysis_video_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", facts["analysis_video_sha256"])
        and type(facts.get("covered_start_ms")) is int and type(facts.get("covered_end_ms")) is int
        and facts.get("analysis_duration_ms") == facts.get("covered_end_ms", 0) - facts.get("covered_start_ms", 0))
    if (requested is None or not (facts.get("coverage") == "full_interval_continuous" or video_analysis)
        or type(facts.get("covered_start_ms")) is not int or type(facts.get("covered_end_ms")) is not int
        or not clip.start_ms <= facts["covered_start_ms"] <= requested[0]
        or not requested[1] <= facts["covered_end_ms"] <= clip.end_ms
        or not isinstance(facts.get("evidence_reference"), str) or not facts["evidence_reference"].strip()):
        return {}
    return facts


def matches_scene(facts, scene):
    if facts.get("coverage") != "video_interval_analysis":
        return True
    match = facts.get("scene_matches", {}).get(scene_visual_key(scene), {})
    return (match.get("analysis_valid") is not False
        and match.get("supported") is True and match.get("score", 0) >= .65
        and match.get("quality_adequate") is True
        and (scene.visual_requirement!="action_evidence"
            or match.get("fit")=="direct" and match.get("subject_verified") is True))
