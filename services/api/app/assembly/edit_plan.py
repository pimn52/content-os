"""Provider-neutral EditPlan resolution and preflight before VideoSpec assembly."""
from __future__ import annotations

import re
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
)


class EditPlanPreflightError(ValueError):
    """A visual plan is unsafe or contradictory before renderer work begins."""


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
        if scene.graphic_plan is not None and scene.graphic_plan.kind == "text_card":
            treatment = GraphicTreatment(scene.graphic_plan.treatment)
            short_text = "\n".join(scene.graphic_plan.points)
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
            graphic_treatment=treatment if role is EditVisualRole.GRAPHIC else GraphicTreatment.NONE,
            graphic_text=short_text if role is EditVisualRole.GRAPHIC else None,
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
            if (entry.fallback.text != "\n".join(scene.graphic_plan.points)
                    or entry.fallback.graphic_treatment.value != scene.graphic_plan.treatment):
                raise EditPlanPreflightError("graphic_card_points_or_treatment_changed")
            if candidate.source_kind is SourceKind.TYPOGRAPHY and (
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
        if entry.burned_in_subtitles == "present" and entry.subtitle_treatment is SubtitleTreatment.TIMED_CAPTIONS:
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
