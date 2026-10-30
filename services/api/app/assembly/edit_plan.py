"""Provider-neutral EditPlan resolution and preflight before VideoSpec assembly."""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from uuid import UUID

from app.media.visual_facts import _requested_source_interval, footage_geometry, full_interval_geometry, scene_visual_key, subtitle_keep_height

from app.domain.models import (
    CandidateAsset,
    EditPlan,
    EditPlanFallback,
    EditPlanScene,
    EditSequenceScene,
    EditSourceInterval,
    EditWholeVideo,
    EditVisualRole,
    GraphicTreatment,
    Project,
    ScenePlan,
    SourceKind,
    SubtitleTreatment,
    TalkingFramingPolicy,
    PortraitPresentation,
    ProjectFormat,
)


class EditPlanPreflightError(ValueError):
    """A visual plan is unsafe or contradictory before renderer work begins."""


def source_frame_interval_ms(start_ms, duration_frames, fps, clip_end_ms=None):
    """Return the frame-ceiled source window used by a scene, in integer ms."""
    if (isinstance(start_ms, bool) or not isinstance(start_ms, int) or start_ms < 0
        or isinstance(duration_frames, bool) or not isinstance(duration_frames, int) or duration_frames <= 0
        or not hasattr(fps, "numerator") or not hasattr(fps, "denominator")
        or fps.numerator <= 0 or fps.denominator <= 0):
        raise ValueError("invalid_source_playback_interval")
    denominator = 1_000 * fps.denominator
    first_frame = (start_ms * fps.numerator + denominator - 1) // denominator
    last_frame_exclusive = first_frame + duration_frames
    interval_start_ms = (first_frame * denominator) // fps.numerator
    end_numerator = last_frame_exclusive * denominator
    interval_end_ms = (end_numerator + fps.numerator - 1) // fps.numerator
    if clip_end_ms is not None:
        interval_end_ms = min(interval_end_ms, clip_end_ms)
    if interval_end_ms <= interval_start_ms:
        raise ValueError("invalid_source_playback_interval")
    return interval_start_ms, interval_end_ms


def source_subtitle_updates(asset, clip, playback_interval_ms=None):
    """规划与装配共用原字幕处理，返回实际呈现字段，不创建图文替代。"""
    facts = full_interval_geometry(asset, clip, playback_interval_ms)
    if not facts:
        return {}
    if facts.get("method") == "human_full_interval_review" and facts.get("source_subtitle_clearance_verified") is True:
        ratio = 1 - facts["crop_window"]["height"] / asset.height
    else:
        regions = facts.get("subtitle_regions")
        if regions is None:
            return {}
        from app.providers.vision import validate_visual_geometry
        try:
            validate_visual_geometry(facts.get("comparison_layout", "unknown"), regions)
        except ValueError as exc:
            raise EditPlanPreflightError("source_subtitle_geometry_invalid") from exc
        if not regions:
            return {"burned_in_subtitles": "absent", "subtitle_treatment": SubtitleTreatment.TIMED_CAPTIONS}
        if (facts.get("source_subtitle_clearance_verified") is not True
            and (facts.get("coverage") == "video_interval_analysis" or facts.get("comparison_layout") != "side_by_side_equal_halves")):
            raise EditPlanPreflightError("source_subtitle_clearance_required")
        top = min(box[1] for box in regions)
        if top < .75:
            raise EditPlanPreflightError("source_subtitles_overlap_required_picture")
        ratio = 1 - subtitle_keep_height(asset.height,regions,machine=facts.get("coverage")=="video_interval_analysis") / asset.height
    return {
        "source_bottom_crop_ratio": ratio,
        "source_subtitle_evidence_reference": facts["evidence_reference"] if ratio else None,
        "burned_in_subtitles": "present" if ratio else "absent",
        "subtitle_treatment": SubtitleTreatment.TIMED_CAPTIONS,
    }


def source_subtitle_presentation(entry, asset, clip, playback_interval_ms=None):
    """Handle existing subtitles before laying a new narration over footage."""
    return entry.model_copy(update=source_subtitle_updates(asset,clip,playback_interval_ms))


def comparison_clearance(asset, clip, playback_interval_ms=None):
    requested = _requested_source_interval(clip, playback_interval_ms)
    if requested is None:
        raise EditPlanPreflightError("comparison_full_interval_clearance_required")
    requested_start_ms, requested_end_ms = requested
    facts=footage_geometry(asset,clip,playback_interval_ms)
    if facts.get("comparison_layout")!="side_by_side_equal_halves":
        raise EditPlanPreflightError("comparison_source_layout_or_subtitle_geometry_unknown")
    if not full_interval_geometry(asset, clip, playback_interval_ms):
        raise EditPlanPreflightError("comparison_full_interval_clearance_required")
    if facts.get("coverage") == "video_interval_analysis" and facts.get("source_subtitle_clearance_verified") is not True:
        raise EditPlanPreflightError("comparison_source_subtitle_clearance_required")
    if facts.get("method") == "human_full_interval_review":
        crop = facts["crop_window"]
        return 1-crop["height"]/asset.height, facts["evidence_reference"]
    if facts.get("subtitle_regions") is None:
        raise EditPlanPreflightError("comparison_source_layout_or_subtitle_geometry_unknown")
    regions=facts["subtitle_regions"]
    if not regions:
        return 0,None
    top=min(box[1] for box in regions)
    if top<.75:
        raise EditPlanPreflightError("comparison_subtitles_overlap_required_picture")
    keep=subtitle_keep_height(asset.height,regions,machine=facts.get("coverage")=="video_interval_analysis")
    return 1-keep/asset.height,facts["evidence_reference"]


def presentation_for_style(entry, style, asset=None, scene_plan=None, clip=None, playback_interval_ms=None, *, portrait_output=True):
    """Select existing layouts without inventing a crop or editorial content."""
    if entry.visual_role is EditVisualRole.FOOTAGE:
        entry = source_subtitle_presentation(entry, asset, clip, playback_interval_ms)
    if not portrait_output:
        return entry.model_copy(update={"portrait_presentation": PortraitPresentation.FULL_CANVAS})
    comparison_style = style.name == "content-os-portrait-comparison"
    facts = footage_geometry(asset,clip,playback_interval_ms)
    comparison_intent = False
    if scene_plan is not None:
        intent_text = " ".join(str(value or "") for value in (
            scene_plan.purpose,
            scene_plan.visual_intent.subject,
            scene_plan.visual_intent.action,
            scene_plan.visual_intent.description,
        )).casefold()
        comparison_intent = any(token in intent_text for token in ("对比", "比較", "比较", "comparison", "contrast"))
    if (comparison_intent and entry.visual_role is EditVisualRole.FOOTAGE
        and entry.selected_source_kind in {SourceKind.USER_ASSET,SourceKind.HISTORICAL_ASSET,SourceKind.STOCK}
        and asset is not None and asset.width>asset.height
        and facts.get("comparison_layout","unknown")=="unknown"):
        raise EditPlanPreflightError("comparison_source_layout_or_subtitle_geometry_unknown")
    comparison_candidate = (
        (comparison_style or facts.get("comparison_layout")=="side_by_side_equal_halves")
        and comparison_intent
        and entry.selected_source_kind in {SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET, SourceKind.STOCK}
        and entry.visual_role is EditVisualRole.FOOTAGE
    )
    if comparison_candidate and asset is not None and asset.width > asset.height and asset.width * 9 != asset.height * 16:
        raise EditPlanPreflightError("portrait_comparison_requires_16_9_source")
    comparison_source = (
        comparison_candidate
        and asset is not None
        and asset.width * 9 == asset.height * 16
        and entry.framing_policy is TalkingFramingPolicy.FACE_SAFE_CONTAIN
    )
    if comparison_source:
        ratio,reference = comparison_clearance(asset,clip,playback_interval_ms)
        updates = {}
        if ratio:
            updates = dict(source_bottom_crop_ratio=ratio,
                source_subtitle_evidence_reference=reference,burned_in_subtitles="present")
        return entry.model_copy(update={
            "portrait_presentation": PortraitPresentation.STACKED_COMPARISON,
            "subtitle_treatment": SubtitleTreatment.TIMED_CAPTIONS,
            **updates,
        })

    panel_style = style.name in {
        "content-os-subject-first", "content-os-editorial", "content-os-portrait-comparison",
    }
    talking_panel = (
        entry.selected_source_kind is SourceKind.AI_VIDEO
        and entry.visual_role is EditVisualRole.TALKING
    )
    subject_first_footage_panel = (
        style.name in {"content-os-subject-first", "content-os-portrait-comparison"}
        and entry.selected_source_kind in {SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET, SourceKind.STOCK}
        and entry.visual_role is EditVisualRole.FOOTAGE
    )
    if (panel_style and (talking_panel or subject_first_footage_panel)
        and entry.framing_policy is TalkingFramingPolicy.FACE_SAFE_CONTAIN):
        if subject_first_footage_panel and not entry.graphic_text:
            # No useful editorial content and no evidenced crop: preserve the
            # largest complete frame, centered, rather than an empty top panel.
            return entry.model_copy(update={"portrait_presentation":PortraitPresentation.FULL_CANVAS})
        if style.name == "content-os-editorial" and not entry.graphic_text:
            raise EditPlanPreflightError("editorial_semantic_plan_required")
        if talking_panel and entry.graphic_text and (len(entry.graphic_text)>32 or len(entry.graphic_text.splitlines())>2):
            raise EditPlanPreflightError("talking_panel_graphics_exceed_header")
        if asset is not None and asset.width < asset.height:
            if talking_panel and entry.graphic_text:
                raise EditPlanPreflightError("portrait_graphic_margin_unavailable")
            if not subject_first_footage_panel:
                return entry
        return entry.model_copy(update={"portrait_presentation":PortraitPresentation.PORTRAIT_PANEL})
    return entry


_TEXT_TOKEN = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]", re.IGNORECASE)


def resolve_edit_plan(
    project: Project,
    scenes: Sequence[ScenePlan],
    selections: Mapping[UUID, CandidateAsset],
    *,
    clip_intervals: Mapping[UUID, tuple[int, int]],
    supplied: EditPlan | None,
) -> EditPlan:
    """Resolve an explicit plan, or a deterministic fail-closed product default."""

    plan = supplied or _default_edit_plan(project, scenes, selections)
    _preflight(plan, project, scenes, selections, clip_intervals)
    return review_edit_plan(plan, scenes, selections, clip_intervals=clip_intervals)


def _default_edit_plan(
    project: Project,
    scenes: Sequence[ScenePlan],
    selections: Mapping[UUID, CandidateAsset],
) -> EditPlan:
    """Keep legacy callers safe while making the resolved plan inspectable in VideoSpec."""

    entries: list[EditPlanScene] = []
    for scene in scenes:
        candidate = selections[scene.id]
        role = _role(candidate.source_kind)
        treatment = _treatment(scene)
        short_text = _short_text(scene)
        # The provisional entry is also used for exact-use comparisons. Only
        # resolve_edit_plan's preflight grants a renderable plan; unsupported
        # declarations must reach the normal blocked-result reporting path.
        if scene.graphic_plan is not None and scene.graphic_plan.kind in {"text_card", "diagram"}:
            treatment = GraphicTreatment(scene.graphic_plan.treatment)
            short_text = "\n".join(scene.graphic_plan.points)
        graphic = role is EditVisualRole.GRAPHIC or role is EditVisualRole.TALKING and scene.graphic_plan is not None and scene.graphic_plan.kind == "text_card"
        entries.append(EditPlanScene(
            scene_plan_id=scene.id,
            scene_id=scene.scene_id,
            visual_role=role,
            selected_source_kind=candidate.source_kind,
            selected_asset_id=candidate.asset_id,
            selected_clip_id=candidate.clip_id,
            # Unknown or absent crop evidence always resolves to the full-frame
            # designed treatment; it never inherits old fixed crop metadata.
            framing_policy=TalkingFramingPolicy.FACE_SAFE_CONTAIN,
            graphic_treatment=treatment if graphic else GraphicTreatment.NONE,
            graphic_text=short_text if graphic else None,
            fallback=EditPlanFallback(graphic_treatment=treatment, text=short_text),
        ))
    return EditPlan(project_id=project.id, scenes=entries)


def _preflight(
    plan: EditPlan,
    project: Project,
    scenes: Sequence[ScenePlan],
    selections: Mapping[UUID, CandidateAsset],
    clip_intervals: Mapping[UUID, tuple[int, int]],
) -> None:
    if plan.project_id != project.id:
        raise EditPlanPreflightError("EditPlan must belong to the requested Project")
    by_scene = {entry.scene_plan_id: entry for entry in plan.scenes}
    scene_ids = {scene.id for scene in scenes}
    if set(by_scene) != scene_ids:
        raise EditPlanPreflightError("EditPlan must contain exactly one visual decision per ScenePlan")

    for scene in scenes:
        entry = by_scene[scene.id]
        candidate = selections[scene.id]
        if entry.visual_task_key is not None and entry.visual_task_key != scene_visual_key(scene):
            raise EditPlanPreflightError("edit_plan_visual_task_changed")
        if scene.graphic_plan is not None:
            if scene.graphic_plan.kind == "unsupported":
                raise EditPlanPreflightError("graphic_realization_unsupported")
            if scene.graphic_plan.kind == "none":
                if candidate.source_kind is SourceKind.TYPOGRAPHY:
                    raise EditPlanPreflightError("no_graphics_requires_media")
                if entry.graphic_treatment is not GraphicTreatment.NONE or entry.graphic_text is not None:
                    raise EditPlanPreflightError("no_graphics_presentation_changed")
            elif (entry.fallback.text != "\n".join(scene.graphic_plan.points)
                    or entry.fallback.graphic_treatment.value != scene.graphic_plan.treatment):
                raise EditPlanPreflightError("graphic_card_points_or_treatment_changed")
            if scene.graphic_plan.kind in {"text_card", "diagram"} and candidate.source_kind is SourceKind.TYPOGRAPHY and (
                entry.graphic_text != "\n".join(scene.graphic_plan.points)
                or entry.graphic_treatment.value != scene.graphic_plan.treatment
            ):
                raise EditPlanPreflightError("graphic_card_points_or_treatment_changed")
        if candidate.source_kind is SourceKind.TYPOGRAPHY and scene.visual_requirement in {"creator_speaking", "action_evidence"}:
            raise EditPlanPreflightError("typography cannot replace required creator-speaking or action evidence")
        if entry.scene_id != scene.scene_id:
            raise EditPlanPreflightError(f"EditPlan scene identity does not match {scene.scene_id!r}")
        if (
            entry.selected_source_kind is not candidate.source_kind
            or entry.selected_asset_id != candidate.asset_id
            or entry.selected_clip_id != candidate.clip_id
        ):
            raise EditPlanPreflightError(f"EditPlan selected visual does not match routed candidate for {scene.scene_id!r}")
        if entry.visual_role is not _role(candidate.source_kind):
            raise EditPlanPreflightError(f"EditPlan visual role does not match selected source for {scene.scene_id!r}")
        if entry.framing_policy is TalkingFramingPolicy.VERIFIED_STATIC_CROP:
            if candidate.source_kind is not SourceKind.AI_VIDEO or candidate.clip_id is None:
                raise EditPlanPreflightError("verified_static_crop is only available for a selected Talking Clip")
            interval = clip_intervals.get(candidate.clip_id)
            if interval is None:
                raise EditPlanPreflightError("verified_static_crop selected Clip is unavailable")
            assert entry.crop_safety_start_ms is not None and entry.crop_safety_end_ms is not None
            if entry.crop_safety_start_ms > interval[0] or entry.crop_safety_end_ms < interval[1]:
                raise EditPlanPreflightError(
                    f"verified_static_crop evidence does not cover the full selected interval for {scene.scene_id!r}"
                )
        if entry.burned_in_subtitles == "present" and entry.subtitle_treatment is SubtitleTreatment.TIMED_CAPTIONS and not entry.source_bottom_crop_ratio:
            raise EditPlanPreflightError(f"known burned-in subtitles conflict with timed captions for {scene.scene_id!r}")
        if (
            entry.subtitle_treatment is SubtitleTreatment.TIMED_CAPTIONS
            and entry.graphic_text is not None
            and _normalized(entry.graphic_text) == _normalized(scene.voice_text)
        ):
            raise EditPlanPreflightError(f"graphic text duplicates the full timed-caption paragraph for {scene.scene_id!r}")


def _role(source_kind: SourceKind) -> EditVisualRole:
    if source_kind is SourceKind.AI_VIDEO:
        return EditVisualRole.TALKING
    if source_kind is SourceKind.TYPOGRAPHY:
        return EditVisualRole.GRAPHIC
    return EditVisualRole.FOOTAGE


def _treatment(scene: ScenePlan) -> GraphicTreatment:
    purpose = scene.purpose.casefold()
    if scene.order == 0 or "hook" in purpose:
        return GraphicTreatment.HEADLINE
    if "contrast" in purpose or "versus" in purpose or "对比" in scene.purpose:
        return GraphicTreatment.CONTRAST
    return GraphicTreatment.KEY_POINT


def _short_text(scene: ScenePlan) -> str:
    if scene.caption_emphasis:
        return scene.caption_emphasis[0]
    return scene.purpose[:120].strip() or "Key point"


def _normalized(value: str) -> tuple[str, ...]:
    return tuple(_TEXT_TOKEN.findall(value.casefold()))


def information_intent(scene):
    """Only compare declared intent; different wording is not proof of new information."""
    value = scene.viewer_takeaway or scene.purpose
    return value.strip() if value and value.strip() else None


def intentional_reuse(scene, asset=None):
    from app.routing.asset_router import _comparison_intent
    return (scene.reuse_intent == "reference" or _comparison_intent(scene)
        or asset is not None and asset.metadata.get("reuse_role") in {"meme", "brand_marker"})


def source_overlap_ms(current, previous):
    """Measure repeated content against the union of prior source intervals."""
    total = 0
    for use in current:
        intervals = sorted((max(use["start_ms"], other["start_ms"]), min(use["end_ms"], other["end_ms"]))
            for other in previous if use["source_hash"] == other["source_hash"]
            and max(use["start_ms"], other["start_ms"]) < min(use["end_ms"], other["end_ms"]))
        end = -1
        for start, stop in intervals:
            total += max(0, stop - max(start, end))
            end = max(end, stop)
    return total


def same_source_information_change(scene, current, previous_scene, previous, *, intentional=False, previous_intentional=False):
    """A soft alternative preference, never evidence that a shot is inadequate."""
    if previous_scene is None or scene.order != previous_scene.order + 1 or intentional or previous_intentional:
        return False
    before, after = information_intent(previous_scene), information_intent(scene)
    return bool(before and after and _normalized(before) != _normalized(after)
        and {use["source_hash"] for use in current}.intersection(use["source_hash"] for use in previous))


def dominant_landscape_fallback(scenes, assets):
    """Share the existing portrait-output guard; no new source or person quota."""
    footage, total = {}, 0
    for scene_id, duration, asset_id, role, presentation in scenes:
        total += duration
        if role is not EditVisualRole.FOOTAGE or asset_id is None or presentation is not PortraitPresentation.FULL_CANVAS:
            continue
        asset = assets.get(asset_id)
        if asset is not None and asset.width > asset.height:
            footage.setdefault(asset_id, []).append((scene_id, duration))
    return tuple("landscape_fallback_dominates_video:" + ",".join(scene_id for scene_id, _ in uses)
        for uses in footage.values() if sum(duration for _, duration in uses) * 2 > total)


def _whole_video(rows, *, timing_basis, portrait_output, assets, source_identity_basis="selected_asset"):
    entries, previous_uses, previous_keys, previous_intent = [], [], set(), None
    talking_ids, talking_duration, first_talking, last_talking = [], 0, None, None
    without_talking, longest_without = 0, 0
    unintentional, intentional, unchanged_source, unchanged_ids = 0, 0, 0, []
    for index, row in enumerate(rows):
        intent = row["information_intent"]
        change = ("opening" if index == 0 else "unknown" if not intent or not previous_intent else
            "declared_repeated" if _normalized(intent) == _normalized(previous_intent) else "declared_changed")
        uses, keys = row["uses"], row["source_keys"]
        repeated = source_overlap_ms(uses, previous_uses)
        if row["intentional_reuse"]:
            intentional += repeated
        else:
            unintentional += repeated
        if (change == "declared_changed" and set(keys).intersection(previous_keys)
            and not row["intentional_reuse"] and not (entries and entries[-1].intentional_reuse)):
            unchanged_source += row["duration_ms"]
            unchanged_ids.append(row["scene_id"])
        entries.append(EditSequenceScene(scene_id=row["scene_id"], start_ms=row["start_ms"],
            duration_ms=row["duration_ms"], visual_role=row["visual_role"], information_intent=intent,
            information_change=change, source_keys=keys, intentional_reuse=row["intentional_reuse"],
            source_intervals=[EditSourceInterval(source_key=use["source_hash"], start_ms=use["start_ms"],
                end_ms=use["end_ms"]) for use in uses], repeated_source_ms=repeated))
        if row["visual_role"] is EditVisualRole.TALKING:
            talking_ids.append(row["scene_id"])
            talking_duration += row["duration_ms"]
            first_talking = row["start_ms"] if first_talking is None else first_talking
            last_talking = row["start_ms"] + row["duration_ms"]
            longest_without = max(longest_without, without_talking)
            without_talking = 0
        else:
            without_talking += row["duration_ms"]
        previous_uses.extend(uses)
        previous_keys, previous_intent = set(keys), intent
    longest_without = max(longest_without, without_talking)
    stops = dominant_landscape_fallback(((row["scene_id"], row["duration_ms"], row["asset_id"],
        row["visual_role"], row["portrait_presentation"]) for row in rows), assets) if portrait_output else ()
    return EditWholeVideo(timing_basis=timing_basis, source_identity_basis=source_identity_basis,
        duration_ms=max(row["start_ms"] + row["duration_ms"] for row in rows),
        scenes=entries, talking_scene_ids=talking_ids, talking_duration_ms=talking_duration,
        first_talking_start_ms=first_talking, last_talking_end_ms=last_talking,
        longest_without_talking_ms=longest_without, unintentional_repeat_ms=unintentional,
        intentional_repeat_ms=intentional, same_source_on_information_change_ms=unchanged_source,
        same_source_on_information_change_scene_ids=unchanged_ids, stop_reasons=list(stops))


def review_edit_plan(plan, scenes, selections, *, durations_ms=None, clip_intervals=None, assets=None,
                     source_uses=None, timing_basis="estimated_scene_duration", portrait_output=False):
    """Refresh the existing EditPlan from current choices and timing without model calls."""
    durations_ms, clip_intervals = durations_ms or {}, clip_intervals or {}
    assets = assets if assets is not None else {}
    task_keys = {scene.id: scene_visual_key(scene) for scene in scenes}
    entries = [entry.model_copy(update={"visual_task_key": task_keys[entry.scene_plan_id]}) for entry in plan.scenes]
    by_scene, rows, cursor = {entry.scene_plan_id: entry for entry in entries}, [], 0
    lineage_known = source_uses is not None
    for scene in sorted(scenes, key=lambda item: item.order):
        entry, candidate = by_scene[scene.id], selections[scene.id]
        duration = durations_ms.get(scene.id, scene.duration_target_ms)
        asset = assets.get(candidate.asset_id) if candidate.asset_id is not None else None
        uses = [use for use in source_uses or () if use["scene_id"] == scene.scene_id]
        if candidate.clip_id is not None and not uses:
            lineage_known = False
        source_key = getattr(asset, "content_hash", None) or (f"asset:{candidate.asset_id}" if candidate.asset_id else None)
        if source_uses is None and candidate.clip_id is not None:
            interval = clip_intervals.get(candidate.clip_id)
            start = candidate.playback_start_ms if candidate.playback_start_ms is not None else interval[0] if interval else None
            end = candidate.playback_end_ms if candidate.playback_end_ms is not None else interval[1] if interval else None
            if source_key and start is not None and end is not None and start < min(start + duration, end):
                uses = [dict(source_hash=source_key, start_ms=start, end_ms=min(start + duration, end))]
        keys = list(dict.fromkeys(use["source_hash"] for use in uses)) or ([source_key] if source_key else [])
        rows.append(dict(scene_id=scene.scene_id, start_ms=cursor, duration_ms=duration, visual_role=entry.visual_role,
            information_intent=information_intent(scene), intentional_reuse=intentional_reuse(scene, asset),
            uses=uses, source_keys=keys, asset_id=candidate.asset_id, portrait_presentation=entry.portrait_presentation))
        cursor += duration
    return plan.model_copy(update={"scenes": entries, "whole_video": _whole_video(rows, timing_basis=timing_basis,
        portrait_output=portrait_output, assets=assets,
        source_identity_basis="source_lineage" if lineage_known else "selected_asset")})


def review_video_spec(spec, assets, *, source_uses=None):
    """Consume actual output frames and current source lineage before ordinary rendering."""
    if spec.edit_plan is None:
        return spec
    prior = {row.scene_id: row for row in spec.edit_plan.whole_video.scenes} if spec.edit_plan.whole_video else {}
    rows = []
    lineage_known = source_uses is not None
    for scene in sorted(spec.scenes, key=lambda item: item.start_frame):
        original = prior.get(scene.scene_id)
        start = (scene.start_frame * 1000 * spec.fps.denominator + spec.fps.numerator - 1) // spec.fps.numerator
        end = ((scene.start_frame + scene.duration_frames) * 1000 * spec.fps.denominator + spec.fps.numerator - 1) // spec.fps.numerator
        asset = assets.get(scene.visual.asset_id) if scene.visual.asset_id else None
        uses = [use for use in source_uses or () if use["scene_id"] == scene.scene_id]
        if scene.visual.clip_start_ms is not None and not uses:
            lineage_known = False
        key = getattr(asset, "content_hash", None) or (f"asset:{scene.visual.asset_id}" if scene.visual.asset_id else None)
        if source_uses is None and key and scene.visual.clip_start_ms is not None:
            uses = [dict(source_hash=key, start_ms=scene.visual.clip_start_ms,
                end_ms=min(scene.visual.clip_end_ms, scene.visual.clip_start_ms + end - start))]
        rows.append(dict(scene_id=scene.scene_id, start_ms=start, duration_ms=end-start, visual_role=scene.visual_role,
            information_intent=original.information_intent if original else None,
            intentional_reuse=original.intentional_reuse if original else False, uses=uses,
            source_keys=list(dict.fromkeys(use["source_hash"] for use in uses)) or ([key] if key else []),
            asset_id=scene.visual.asset_id if scene.visual.vertical_reframe_mode.value == "contain" else None,
            portrait_presentation=scene.portrait_presentation))
    whole = _whole_video(rows, timing_basis="rendered_timeline", portrait_output=spec.format is ProjectFormat.VERTICAL,
        assets=assets, source_identity_basis="source_lineage" if lineage_known else "selected_asset")
    return spec.model_copy(update={"edit_plan": spec.edit_plan.model_copy(update={"whole_video": whole})})
