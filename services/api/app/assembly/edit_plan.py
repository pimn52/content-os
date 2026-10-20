"""Provider-neutral EditPlan resolution and preflight before VideoSpec assembly."""
from __future__ import annotations

import re
import math
from collections.abc import Mapping, Sequence
from uuid import UUID

from app.domain.models import (
    CandidateAsset,
    EditPlan,
    EditPlanFallback,
    EditPlanScene,
    EditVisualRole,
    GraphicTreatment,
    Project,
    ScenePlan,
    SourceKind,
    SubtitleTreatment,
    TalkingFramingPolicy,
    PortraitPresentation,
)


class EditPlanPreflightError(ValueError):
    """A visual plan is unsafe or contradictory before renderer work begins."""


def footage_geometry(asset, clip):
    """Read existing source-analysis facts; stale or unrelated intervals are unknown."""
    if asset is None or clip is None:
        return {}
    analysis=asset.metadata.get("visual_analysis",{})
    facts=analysis.get(str(clip.id),{}) if isinstance(analysis,dict) else {}
    if not isinstance(facts,dict):return {}
    interval = facts.get("clip_interval",facts)
    if (facts.get("source_content_hash") != asset.content_hash
        or interval.get("start_ms",clip.start_ms+1)>clip.start_ms or interval.get("end_ms",0)<clip.end_ms):
        return {}
    return facts


def comparison_clearance(asset, clip):
    facts=footage_geometry(asset,clip)
    if facts.get("comparison_layout")!="side_by_side_equal_halves" or facts.get("subtitle_regions") is None:
        raise EditPlanPreflightError("comparison_source_layout_or_subtitle_geometry_unknown")
    if (facts.get("coverage") != "full_interval_continuous"
        or facts.get("covered_start_ms",clip.end_ms) > clip.start_ms
        or facts.get("covered_end_ms",clip.start_ms) < clip.end_ms):
        raise EditPlanPreflightError("comparison_full_interval_clearance_required")
    regions=facts["subtitle_regions"]
    if not regions:
        return 0,None
    top=min(box[1] for box in regions)
    if top<.75:
        raise EditPlanPreflightError("comparison_subtitles_overlap_required_picture")
    keep=max(1,math.floor(top*asset.height)-2)
    return 1-keep/asset.height,facts["evidence_reference"]


def presentation_for_style(entry, style, asset=None, scene_plan=None, clip=None):
    """Select existing layouts without inventing a crop or editorial content."""
    comparison_style = style.name == "content-os-portrait-comparison"
    facts = footage_geometry(asset,clip)
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
        and entry.selected_source_kind in {SourceKind.USER_ASSET,SourceKind.HISTORICAL_ASSET}
        and asset is not None and asset.width>asset.height
        and facts.get("comparison_layout","unknown")=="unknown"):
        raise EditPlanPreflightError("comparison_source_layout_or_subtitle_geometry_unknown")
    comparison_candidate = (
        (comparison_style or facts.get("comparison_layout")=="side_by_side_equal_halves")
        and comparison_intent
        and entry.selected_source_kind in {SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET}
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
        ratio,reference = comparison_clearance(asset,clip)
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
        and entry.selected_source_kind in {SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET}
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
    return plan


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
