"""Read-only, provider-neutral production preview for the current ProjectDraft.

This is a planning seam, not a dispatcher or an asset-suitability oracle. It
uses the normal local Router and exposes every unproven condition before work.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.assembly.edit_plan import (EditPlanPreflightError, resolve_edit_plan, _default_edit_plan,
    presentation_for_style, review_edit_plan, dominant_landscape_fallback)
from app.media.visual_facts import full_interval_geometry, footage_geometry, matches_scene
from app.budget import snapshot
from app.costs import estimate_whole_production
from app.db import (
    AssetRepository, AudioAssetRepository, BudgetPolicyRepository, ClipRepository, Database, JobRepository,
    ImageAssetRepository, ProviderCallRepository, ProviderMachineCapabilityProfileRepository,
    ProviderMachineSettingRepository,
)
from app.domain.models import (
    Asset, AudioAsset, CandidateAsset, CostCategory, CostEstimate, EditPlan, EditPlanScene, EditVisualRole, ImageAsset,
    GraphicTreatment, Project, ProjectDraft, ProjectFormat, ScenePlan, SourceKind,
    SubtitleTreatment, TalkingFramingPolicy, UsageCost, VisualStyleTokens, VideoSpec,
)
from app.media.crop_assessment import CropAssessmentRepository, CropProposal
from app.routing import AssetRouter
from app.routing.asset_router import WEAK_LEXICAL_ROUTE_REASON
from app.search import ClipTextSearchService
from app.source_use_constraints import SourceUseConstraintService, UseConstraintDecision, presentation
from app.source_usage import SourceUsageService, SourceUsageConflict
from app.planning_input import presentation_plan_gaps
from app.providers.local_generation import local_generation_ready, local_generation_licensed


class PreflightInputError(ValueError):
    pass


class StaleProductionPreflight(ValueError):
    pass


class PreflightScene(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene_plan_id: UUID
    scene_id: str
    visual_role: EditVisualRole
    planned_source_kind: SourceKind
    selected_candidate: CandidateAsset | None = None
    source_authorization_reference: str | None = None
    production_need: Literal["none", "new_talking", "capture"] = "none"
    suitability: Literal["known_deterministic", "preserved_full_frame", "reviewed_source_interval", "analyzed_source_interval", "reviewed_native_planned_run", "qa_verified_planned_output", "reviewed_talking_run", "reviewed_retained_media", "authorized_retained_media", "unknown"]
    subtitle_treatment: SubtitleTreatment
    burned_in_subtitles: Literal["unknown", "present", "absent"]
    graphic_treatment: GraphicTreatment
    reasons: tuple[str, ...] = ()
    excluded_uses: tuple[UseConstraintDecision, ...] = ()
    visual_dependency_fingerprint: str = Field(min_length=64, max_length=64)


class PreflightAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["voice", "talking", "analysis", "transform", "render", "repair_allowance"]
    scene_plan_id: UUID | None = None
    required: bool
    max_attempts: int = Field(ge=0, le=3)
    reason: str
    cost: UsageCost
    local_runtime_ms: int | None = None
    input_fingerprint: str = Field(min_length=64, max_length=64)


class ProductionPreflight(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: UUID
    draft_version: int
    script_revision: int
    reusable_master_audio_id: UUID | None = None
    fingerprint: str = Field(min_length=64, max_length=64)
    evidence_level: Literal["deterministic_metadata_only", "metadata_and_persisted_negative_assessment", "metadata_and_reviewed_planned_run", "metadata_and_reviewed_source_interval", "metadata_and_video_interval_analysis", "metadata_and_adopted_use_constraint"] = "deterministic_metadata_only"
    status: Literal["ready_for_authorized_dispatch", "blocked"]
    scenes: tuple[PreflightScene, ...]
    preliminary_edit_plan: EditPlan | None = None
    actions: tuple[PreflightAction, ...]
    cost_estimate: CostEstimate
    stop_reasons: tuple[str, ...]
    authorization_needs: tuple[str, ...]
    estimated_user_active_minutes: int | None = None
    estimated_local_runtime_ms: int | None = None
    dispatch_performed: Literal[False] = False
    video_spec: VideoSpec | None = None
    retained_media: dict | None = None





def build_production_preflight(
    db: Database,
    project: Project,
    draft: ProjectDraft | None,
    *,
    style_tokens: VisualStyleTokens,
    expected_fingerprint: str | None = None,
    repair_allowance: int = 1,
    crop_proposals: tuple[CropProposal, ...] = (),
    bound_talking_clips: dict[UUID, UUID] | None = None,
    production_run_id: UUID | None = None,
    source_usage_run_id: UUID | None = None,
    source_usage_work_id: UUID | None = None,
) -> ProductionPreflight:
    if draft is None or not draft.scenes:
        if expected_fingerprint is not None:
            raise StaleProductionPreflight("stale_production_preflight")
        raise PreflightInputError("current draft has no ScenePlan; create and save a scene plan first")
    scenes = sorted(draft.scenes, key=lambda scene: scene.order)
    if [scene.order for scene in scenes] != list(range(len(scenes))) or any(scene.project_id != project.id for scene in scenes):
        raise PreflightInputError("current draft ScenePlan order or project identity is invalid")
    if not 0 <= repair_allowance <= 2:
        raise PreflightInputError("repair allowance must be 0, 1, or 2")
    proposal_map = {proposal.scene_plan_id: proposal for proposal in crop_proposals}
    if None in proposal_map or len(proposal_map) != len(crop_proposals) or not set(proposal_map).issubset({scene.id for scene in scenes}):
        raise PreflightInputError("crop proposals require unique current ScenePlan identities")

    assets, clips, images = AssetRepository(db), ClipRepository(db), ImageAssetRepository(db)
    voice_copy = draft.script or "\n\n".join(scene.voice_text for scene in scenes)
    reusable_master = _approved_master(db, project.id, voice_copy, production_run_id=production_run_id or source_usage_run_id)
    routing_scenes = scenes
    master_timing_unaligned = False
    if reusable_master is not None:
        from app.assembly.video_spec import _master_narration_intervals, NarrationTimelineError
        try:
            intervals = _master_narration_intervals(reusable_master, scenes)
        except NarrationTimelineError as exc:
            if bound_talking_clips:
                raise PreflightInputError("approved Master timing does not resolve current scenes") from exc
            master_timing_unaligned = True
        else:
            routing_scenes = [scene.model_copy(update={"duration_target_ms": end - start})
                              for scene, start, end in intervals]
    assessments = CropAssessmentRepository(db)
    crop_outcomes = {str(scene_id): assessments.decision(proposal) for scene_id, proposal in proposal_map.items()}
    router = AssetRouter(ClipTextSearchService(db), assets, images=images, crop_proposals=proposal_map,
        source_usage_run_id=source_usage_run_id or production_run_id,
        source_usage_work_id=source_usage_work_id)
    route_by_scene = {result.scene_plan_id: result for result in router.route_all(routing_scenes)}
    use_service = SourceUseConstraintService(db)
    use_records = use_service.records(project.id)
    use_context = use_service.context_from_records(use_records)
    profiles = tuple(sorted(
        (value for value in ProviderMachineCapabilityProfileRepository(db).list()
         if value.capability in {"voice", "talking", "vision", "render"}),
        key=lambda value: value.scope_key,
    ))
    settings = tuple(sorted(
        (value for value in ProviderMachineSettingRepository(db).list()
         if value.capability in {"voice", "talking", "vision", "render"}),
        key=lambda value: value.scope_key,
    ))
    capability_identity = _digest({
        "profiles": [value.model_dump(mode="json") for value in profiles],
        "settings": [value.model_dump(mode="json") for value in settings],
    })
    voice_identity = _digest({
        "script": voice_copy,
        "reusable_master": None if reusable_master is None else {
            "id": str(reusable_master.id), "hash": reusable_master.content_hash,
            "review": reusable_master.metadata.get("voice_generation"),
        },
        "performance": None if draft.narration_performance_plan is None else draft.narration_performance_plan.model_dump(mode="json"),
        "ip_profile_version": draft.ip_profile_version,
        "voice_configuration": [value.model_dump(mode="json") for value in (*profiles, *settings) if value.capability == "voice"],
    })
    decisions: list[PreflightScene] = []
    actions: list[PreflightAction] = []
    selected: list[CandidateAsset] = []
    stop: list[str] = list(presentation_plan_gaps(project, scenes))
    if master_timing_unaligned:
        stop.append("approved_master_timing_unaligned")
    authorization: list[str] = []
    if reusable_master is None:
        from app.voice_plan import resolve_project_voice_profile
        try:
            resolve_project_voice_profile(db,project.id)
        except ValueError as exc:
            stop.append(str(exc))
    need_analysis = False
    usage = SourceUsageService(db)
    allocated_uses = []
    try:
        occupied_uses = usage.occupied(project.id, run_id=source_usage_run_id or production_run_id, work_id=source_usage_work_id)
    except (SourceUsageConflict, KeyError, ValueError):
        occupied_uses = []
        stop.append("visual_source_history_unresolved")
    routing_durations = {scene.id:scene.duration_target_ms for scene in routing_scenes}
    checked_source_bytes = {}

    for scene in scenes:
        if scene.material_request is not None:
            stop.append(f"material_source_unavailable:{scene.scene_id}:{scene.material_request.source_kind}")
        explicit = router.explicit_footage(
            scene, draft.confirmed,
        )
        candidates = (explicit,) if explicit is not None else route_by_scene[scene.id].candidates
        candidate, need = _choose_candidate(scene, candidates, clips, assets, portrait_output=project.format is ProjectFormat.VERTICAL)
        qa_verified_output = False
        if bound_talking_clips is not None and scene.id in bound_talking_clips:
            if production_run_id is not None:
                from app.talking.admission import talking_visual_blocker
                bound_clip = clips.get(bound_talking_clips[scene.id])
                bound_asset = None if bound_clip is None else assets.get(bound_clip.asset_id)
                if (bound_asset is None or bound_clip is None
                    or bound_asset.source_kind is not SourceKind.AI_VIDEO
                    or talking_visual_blocker(bound_asset, bound_clip, assets,
                        project_id=project.id, copy=scene.voice_text,
                        master_audio_id=None if reusable_master is None else reusable_master.id,
                        production_run_id=production_run_id) is not None):
                    raise PreflightInputError("bound planned Talking output failed current Run QA checks")
                candidate = CandidateAsset(scene_plan_id=scene.id, source_kind=SourceKind.AI_VIDEO,
                    asset_id=bound_asset.id, clip_id=bound_clip.id,
                    playback_start_ms=0, playback_end_ms=bound_asset.duration_ms,
                    match_score=1, recommended=True,
                    why=["same ProductionRun automated QA passed; final human review pending"],
                    estimated_cost=UsageCost(category=CostCategory.TALKING, amount=Decimal("0"), currency="USD"))
                candidates = (candidate,)
                qa_verified_output = isinstance(bound_asset.metadata.get("talking_generation"), dict)
            else:
                candidate = next((item for item in candidates
                                  if item.clip_id == bound_talking_clips[scene.id]
                                  and item.source_kind is SourceKind.AI_VIDEO), None)
            if candidate is None:
                raise PreflightInputError("bound planned TalkingRun is no longer an eligible candidate")
            need = "none"
        # Real visible intervals, including underlying Talking/crop sources.
        # An admitted copy/identity does not grant fresh motion capacity.
        if candidate is not None:
            duration = routing_durations[scene.id]
            try:
                available = usage.allocate_candidate(candidate, scene.scene_id, duration,
                    [*occupied_uses, *allocated_uses], fixed=explicit is not None or qa_verified_output)
                if available is None:
                    for alternative in candidates:
                        if alternative == candidate or alternative.requires_capture:
                            continue
                        selected_alternative, alternative_need = _choose_candidate(scene, (alternative,), clips, assets, portrait_output=project.format is ProjectFormat.VERTICAL)
                        if selected_alternative is None or alternative_need != "none":
                            continue
                        available = usage.allocate_candidate(selected_alternative, scene.scene_id, duration,
                            [*occupied_uses, *allocated_uses])
                        if available is not None:
                            break
                if available is None:
                    current_uses = usage.candidate_uses(candidate, scene.scene_id, duration)
                    try:
                        usage.require_available(project.id, [*allocated_uses, *current_uses], run_id=source_usage_run_id or production_run_id)
                    except SourceUsageConflict as exc:
                        stop.append(f"{exc.reason}:{scene.scene_id}")
                else:
                    candidate = available
            except SourceUsageConflict as exc:
                stop.append(f"{exc.reason}:{scene.scene_id}")
            except (KeyError, ValueError):
                stop.append(f"visual_source_lineage_unresolved:{scene.scene_id}")
        excluded = []
        blocked_candidates = set()
        presentation_failures = {}
        for item in candidates:
            item_source = assets.get(item.asset_id) if item.asset_id is not None else None
            item_clip = clips.get(item.clip_id) if item.clip_id else None
            item_interval = _playback_interval(item, item_clip, routing_durations[scene.id])
            burned_state, _ = _subtitle_state(item_source, item_clip, item_interval)
            entry = _default_edit_plan(project, [scene], {scene.id: item}).scenes[0].model_copy(update={
                "burned_in_subtitles": burned_state,
                "subtitle_treatment": SubtitleTreatment.NONE if burned_state == "present" else SubtitleTreatment.TIMED_CAPTIONS,
            })
            try:
                entry = presentation_for_style(entry, style_tokens, item_source, scene_plan=scene,
                    clip=item_clip, playback_interval_ms=item_interval,
                    portrait_output=project.format is ProjectFormat.VERTICAL)
            except EditPlanPreflightError as exc:
                presentation_failures[(item.asset_id,item.clip_id,item.source_kind)] = str(exc)
                continue
            matches = use_service.evaluate(item, presentation(entry, width=project.resolution_width,
                height=project.resolution_height, style=style_tokens), use_records)
            if matches:
                excluded.extend(matches)
                blocked_candidates.add((item.asset_id, item.clip_id, item.source_kind))
        if candidate is not None and (candidate.asset_id, candidate.clip_id, candidate.source_kind) in blocked_candidates:
            if bound_talking_clips is not None and scene.id in bound_talking_clips:
                raise PreflightInputError("bound_source_use_excluded_or_unresolved_replan")
            alternative = None
            if alternative is None and SourceKind.AI_VIDEO in (*scene.preferred_sources, *scene.fallback_sources):
                alternative = next((item for item in candidates
                    if item.source_kind is SourceKind.AI_VIDEO
                    and (item.asset_id, item.clip_id, item.source_kind) not in blocked_candidates
                    and _reviewed_native_planned(db, project, scene, assets.get(item.asset_id), clips.get(item.clip_id), reusable_master)), None)
            candidate, need = (alternative, "none") if alternative is not None else (None, "capture")
            if candidate is None:
                stop.append(f"source_use_replan_required:{scene.scene_id}")
        if candidate is not None:
            try:
                allocated_uses.extend(usage.candidate_uses(candidate, scene.scene_id, routing_durations[scene.id]))
            except (SourceUsageConflict, KeyError, ValueError):
                stop.append(f"visual_source_lineage_unresolved:{scene.scene_id}")
        kind = candidate.source_kind if candidate is not None else SourceKind.AI_VIDEO if need == "new_talking" else SourceKind.CAPTURE
        role = EditVisualRole.TALKING if kind is SourceKind.AI_VIDEO else EditVisualRole.GRAPHIC if kind is SourceKind.TYPOGRAPHY else EditVisualRole.FOOTAGE
        asset = assets.get(candidate.asset_id) if candidate is not None and candidate.asset_id is not None else None
        image = images.get(candidate.asset_id) if candidate is not None and candidate.source_kind in {SourceKind.SCREENSHOT, SourceKind.CHART} and candidate.asset_id is not None else None
        source = asset or image
        clip = clips.get(candidate.clip_id) if candidate is not None and candidate.clip_id is not None else None
        preserved_frame = False
        if asset is not None and role is EditVisualRole.FOOTAGE and clip is not None and getattr(db,"data_root",None) is not None:
            if asset.id not in checked_source_bytes:
                from app.media.local_paths import resolve_local_media_path
                try:
                    source_path = resolve_local_media_path(asset.source_file, Path(db.data_root))
                    with source_path.open("rb") as content:
                        checked_source_bytes[asset.id] = hashlib.file_digest(content,"sha256").hexdigest() == asset.content_hash
                except (ValueError, OSError):
                    checked_source_bytes[asset.id] = False
            preserved_frame = checked_source_bytes[asset.id] and style_tokens.name == "content-os-dark"
            if not checked_source_bytes[asset.id]:
                stop.append(f"source_content_hash_mismatch:{scene.scene_id}")
        playback_interval = _playback_interval(candidate, clip, routing_durations[scene.id])
        if candidate is not None and candidate.asset_id is not None and candidate.clip_id is not None:
            from app.media.semantic_rejections import candidate_rejections
            if candidate_rejections(db,scene,candidate,fps=project.fps,duration_ms=routing_durations[scene.id]):
                stop.append(f'source_semantic_use_rejected:{scene.scene_id}')
        source_facts = full_interval_geometry(asset, clip, playback_interval)
        if source_facts and not matches_scene(source_facts, scene):
            stop.append(f"source_scene_mismatch:{scene.scene_id}")
            source_facts = {}
        burned, subtitle_reason = _subtitle_state(source, clip, playback_interval)
        subtitle = SubtitleTreatment.NONE if burned == "present" else SubtitleTreatment.TIMED_CAPTIONS
        if preserved_frame and burned == "unknown":
            subtitle = SubtitleTreatment.NONE
        reviewed_native = kind is SourceKind.AI_VIDEO and _reviewed_native_planned(db, project, scene, asset, clip, reusable_master)
        reviewed_comparison = False
        if (asset is not None and clip is not None and candidate is not None
            and candidate.playback_start_ms is not None and scene.visual_requirement == "action_evidence"):
            from app.assembly.edit_plan import comparison_clearance
            try:
                comparison_clearance(asset, clip, (candidate.playback_start_ms, candidate.playback_end_ms))
                reviewed_comparison = True
            except (EditPlanPreflightError, ValueError):
                pass
        if (reviewed_native or qa_verified_output) and burned == "unknown":
            subtitle = SubtitleTreatment.NONE
        reasons: list[str] = []
        if candidate is not None and (candidate.asset_id,candidate.clip_id,candidate.source_kind) in presentation_failures:
            failure = presentation_failures[(candidate.asset_id,candidate.clip_id,candidate.source_kind)]
            stop.append(f"visual_presentation_unresolved:{scene.scene_id}:{failure}")
            reasons.append(failure)
        if asset is not None and role is EditVisualRole.FOOTAGE and asset.width > asset.height:
            reasons.append("prefer compatible portrait or verified same-interval derivative; without a safe alternative preserve full source centered, no empty top panel")
        if crop_outcomes.get(str(scene.id)) == "unusable":
            reasons.append("proposed interval/crop has persisted face-boundary failure; use another visual route")
        if subtitle_reason:
            reasons.append(subtitle_reason)
        if need == "new_talking":
            reasons.append("no admitted same-project TalkingRun for this exact speech")
            actions.append(_action(
                "talking", scene.id, True, 1,
                "generate only this required creator-speaking interval after Master admission",
                CostCategory.TALKING, None,
                _digest({"scene": scene.model_dump(mode="json"), "voice": voice_identity, "capability": capability_identity}),
            ))
            stop.append(f"talking_source_not_bound:{scene.scene_id}")
        elif need == "capture":
            reasons.append("画面方案尚未解决，需要调整选材、布局或整体画幅；当前缺口不证明必须补拍。")
            stop.append(f"visual_route_unresolved:{scene.scene_id}")
            capture = next((item for item in candidates if item.source_kind is SourceKind.CAPTURE), None)
            if capture is not None and WEAK_LEXICAL_ROUTE_REASON in capture.why:
                reasons.append("weak lexical retrieval does not establish source unusability or capture necessity; resolve visual intent before dispatch")
            stop.append(f"capture_required:{scene.scene_id}")
        elif candidate is not None:
            selected.append(candidate)
        if preserved_frame:
            reasons.append("实际文件哈希匹配，保持完整原画面；这不是整段内容、构图或人工质量通过。")
        if source is not None and not reviewed_native and not qa_verified_output and not reviewed_comparison and not source_facts:
            reasons.append("full-interval visual and portrait suitability have not been independently verified")
            need_analysis = True
            stop.append(f"source_suitability_unknown:{scene.scene_id}")
            if asset is not None and asset.width >= asset.height:
                actions.append(_action(
                    "transform", scene.id, False, 1,
                    "portrait transform is conditional on interval-bound face/subtitle evidence",
                    CostCategory.USER_ASSET, None,
                    _digest({"asset": asset.id, "hash": asset.content_hash, "rights": asset.authorization_reference, "clip": None if clip is None else clip.model_dump(mode="json"), "style": style_tokens.model_dump(mode="json")}),
                ))
        if role is EditVisualRole.FOOTAGE and source is not None and not reviewed_comparison:
            try:
                checked_entry = presentation_for_style(_default_edit_plan(project, [scene], {scene.id: candidate}).scenes[0],
                    style_tokens, asset, scene_plan=scene, clip=clip, playback_interval_ms=playback_interval,
                    portrait_output=project.format is ProjectFormat.VERTICAL)
                subtitles_resolved = checked_entry.burned_in_subtitles == "absent" or checked_entry.source_bottom_crop_ratio > 0
            except EditPlanPreflightError:
                subtitles_resolved = False
            if not subtitles_resolved:
                need_analysis = True
                stop.append(f"source_subtitle_handling_unresolved:{scene.scene_id}")
                reasons.append("新旁白需要消除原片字幕冲突；仅关闭新字幕不算解决，先分析确切选段或换素材。")
        if burned == "unknown" and source is not None and not reviewed_comparison:
            reasons.append("source subtitles are unknown; absence has not been verified")
        if reviewed_comparison:
            reasons.append("approved same-source comparison interval and subtitle crop; no new source analysis")
        if reviewed_native:
            reasons.append("exact approved native-portrait planned Run; preserve full frame without a new crop")
            if burned == "unknown":
                reasons.append("subtitle state remains unknown; no new caption overlay is authorized")
        if qa_verified_output:
            reasons.append("same ProductionRun output passed technical QA; human quality judgment remains pending on the final video")
        unsupported_graphic = scene.graphic_plan is not None and scene.graphic_plan.kind == "unsupported"
        no_graphics = scene.graphic_plan is not None and scene.graphic_plan.kind == "none"
        if no_graphics:
            reasons.append(f"no additional graphics requested: {scene.graphic_plan.reason}")
            if kind is SourceKind.TYPOGRAPHY:
                stop.append(f"no_graphics_requires_media:{scene.scene_id}")
        if unsupported_graphic:
            stop.append(f"graphic_realization_unsupported:{scene.scene_id}")
            reasons.append("declared graphic requires unsupported realization; typography cannot silently replace it")
        elif kind is SourceKind.TYPOGRAPHY and scene.graphic_plan is None:
            reasons.append("legacy graphic realization is undeclared; intended effect coverage remains unverified")
        if unsupported_graphic or no_graphics and kind is SourceKind.TYPOGRAPHY:
            suitability = "unknown"
        elif kind is SourceKind.TYPOGRAPHY:
            suitability = "known_deterministic"
        elif qa_verified_output:
            suitability = "qa_verified_planned_output"
        elif reviewed_native:
            suitability = "reviewed_native_planned_run"
        elif source_facts.get("coverage") == "video_interval_analysis":
            suitability = "analyzed_source_interval"
        elif reviewed_comparison or source_facts:
            suitability = "reviewed_source_interval"
        else:
            suitability = "preserved_full_frame" if preserved_frame else "unknown"
        visual_identity_payload = {
            "source_kind": kind.value, "asset_id": None if source is None else str(source.id),
            "asset_hash": None if source is None else source.content_hash,
            "authorization": None if source is None else source.authorization_reference,
            "clip": None if clip is None else clip.model_dump(mode="json"),
            "playback_interval": playback_interval,
            "source_visual_facts": source_facts,
            "sampled_visual_facts": footage_geometry(asset, clip, playback_interval),
            "style": style_tokens.model_dump(mode="json"), "subtitle": subtitle.value,
        }
        if use_context:
            visual_identity_payload["source_use_constraints"] = use_context
        visual_identity = _digest(visual_identity_payload)
        decisions.append(PreflightScene(
            scene_plan_id=scene.id, scene_id=scene.scene_id, visual_role=role,
            planned_source_kind=kind, selected_candidate=candidate,
            source_authorization_reference=None if source is None else source.authorization_reference,
            production_need=need, suitability=suitability,
            subtitle_treatment=subtitle, burned_in_subtitles=burned,
            graphic_treatment=GraphicTreatment.NONE if no_graphics or role is not EditVisualRole.GRAPHIC else GraphicTreatment(scene.graphic_plan.treatment) if scene.graphic_plan is not None and scene.graphic_plan.kind == "text_card" else GraphicTreatment.HEADLINE if scene.order == 0 else GraphicTreatment.KEY_POINT,
            reasons=tuple(reasons), excluded_uses=tuple(excluded), visual_dependency_fingerprint=visual_identity,
        ))

    actions.insert(0, _action(
        "voice", None, reusable_master is None, 1 if reusable_master is None else 0,
        "produce and admit a new exact-copy MasterNarration" if reusable_master is None else (
            "reuse exact-copy authorized source audio" if isinstance(reusable_master.metadata.get("source_audio"), dict)
            else "reuse exact-copy approved MasterNarration without another human review"
        ),
        CostCategory.VOICE, None if reusable_master is None else Decimal("0"), voice_identity,
    ))
    if need_analysis:
        actions.append(_action(
            "analysis", None, True, 1,
            "assess exact selected intervals, face/crop safety and burned-in subtitles before transform or assembly",
            CostCategory.LLM, None,
            _digest({"visuals": [item.visual_dependency_fingerprint for item in decisions], "capability": capability_identity}),
        ))
    actions.append(_action(
        "render", None, True, 1, "local final render after admitted media and resolved EditPlan",
        CostCategory.RENDER, Decimal("0"),
        _digest({"visuals": [item.visual_dependency_fingerprint for item in decisions], "voice": voice_identity, "style": style_tokens.model_dump(mode="json")}),
    ))
    if repair_allowance:
        actions.append(_action(
            "repair_allowance", None, False, repair_allowance,
            "bounded contingency only; no retry is authorized or dispatched by this preview",
            CostCategory.TALKING, None,
            _digest({"voice": voice_identity, "visuals": [item.visual_dependency_fingerprint for item in decisions], "allowance": repair_allowance}),
        ))

    preliminary_edit_plan: EditPlan | None = None
    if len(selected) == len(scenes):
        selections = {candidate.scene_plan_id: candidate for candidate in selected}
        intervals = {
            candidate.clip_id: (candidate.playback_start_ms if candidate.playback_start_ms is not None else clip.start_ms,
                                candidate.playback_end_ms if candidate.playback_end_ms is not None else clip.end_ms)
            for candidate in selected
            if candidate.clip_id is not None and (clip := clips.get(candidate.clip_id)) is not None
        }
        try:
            base_plan = resolve_edit_plan(project, scenes, selections, clip_intervals=intervals, supplied=None)
            by_scene = {decision.scene_plan_id: decision for decision in decisions}
            scene_by_id = {scene.id: scene for scene in scenes}
            preliminary_edit_plan = EditPlan(
                project_id=project.id, style_tokens=style_tokens,
                scenes=[presentation_for_style(entry.model_copy(update={
                    "subtitle_treatment": by_scene[entry.scene_plan_id].subtitle_treatment,
                    "burned_in_subtitles": by_scene[entry.scene_plan_id].burned_in_subtitles,
                }), style_tokens, assets.get(selections[entry.scene_plan_id].asset_id)
                    if selections[entry.scene_plan_id].asset_id is not None else None,
                    scene_plan=scene_by_id[entry.scene_plan_id],clip=clips.get(entry.selected_clip_id) if entry.selected_clip_id else None,
                    playback_interval_ms=_playback_interval(selections[entry.scene_plan_id], clips.get(entry.selected_clip_id),
                        routing_durations[entry.scene_plan_id]) if entry.selected_clip_id else None,
                    portrait_output=project.format is ProjectFormat.VERTICAL) for entry in base_plan.scenes],
            )
            preliminary_edit_plan = resolve_edit_plan(project, scenes, selections, clip_intervals=intervals, supplied=preliminary_edit_plan)
            preliminary_edit_plan = review_edit_plan(preliminary_edit_plan, scenes, selections,
                durations_ms=routing_durations, clip_intervals=intervals, assets=assets, source_uses=allocated_uses,
                timing_basis="measured_master" if reusable_master is not None and not master_timing_unaligned else "estimated_scene_duration",
                portrait_output=project.format is ProjectFormat.VERTICAL)
            planned={entry.scene_plan_id:entry for entry in preliminary_edit_plan.scenes}
            decisions=[decision.model_copy(update={
                "subtitle_treatment":planned[decision.scene_plan_id].subtitle_treatment,
                "burned_in_subtitles":planned[decision.scene_plan_id].burned_in_subtitles,
                "graphic_treatment":planned[decision.scene_plan_id].graphic_treatment,
            }) for decision in decisions]
        except (EditPlanPreflightError, ValueError) as exc:
            preliminary_edit_plan = None
            stop.extend(("preliminary_edit_plan_conflict",f"visual_presentation_unresolved:{exc}"))
    if preliminary_edit_plan is not None:
        stop.extend(preliminary_edit_plan.whole_video.stop_reasons)
    cost = estimate_whole_production(selected, required_scene_count=len(scenes), actions=actions)
    policy_repo = BudgetPolicyRepository(db)
    policies = [policy for policy in (policy_repo.get_by_project(None), policy_repo.get_by_project(project.id)) if policy is not None]
    if cost.unknown_cost_count and (not policies or any(not policy.allow_unknown_cost for policy in policies)):
        stop.append("unknown_cost_requires_budget_authorization")
    call_repo = ProviderCallRepository(db)
    for policy in policies:
        records = call_repo.list() if policy.project_id is None else call_repo.list_for_project(project.id)
        spent = snapshot(records, policy.currency)
        if policy.max_calls is not None and spent.calls >= policy.max_calls and any(
            action.required and action.kind in {"voice", "talking", "analysis"} for action in actions
        ):
            stop.append("budget_call_limit")
        if policy.max_amount is not None and cost.known_amount > 0 and spent.known_amount + cost.known_amount > policy.max_amount:
            stop.append("budget_amount_limit")
    for capability in ("voice", "talking"):
        if capability == "voice" and reusable_master is not None:
            continue
        if capability == "talking" and not any(action.kind == "talking" for action in actions):
            continue
        configured_profiles = [profile for profile in profiles
                               if profile.capability == capability and local_generation_ready(profile)]
        if not configured_profiles:
            stop.append(f"{capability}_capability_not_verified")
        if not any(local_generation_licensed(profile) for profile in configured_profiles):
            stop.append("provider_license_scope_unverified")
    identity = _digest({
        "project_id": str(project.id), "script_revision": draft.script_revision,
        "presentation_style": project.presentation_style,
        "delivery_intent": project.delivery_intent, "format_policy": project.format_policy,
        "format": project.format, "width": project.resolution_width, "height": project.resolution_height,
        "production_run_id": None if production_run_id is None else str(production_run_id),
        "topic": draft.topic or project.topic,
        "scenes": [scene.model_dump(mode="json") for scene in scenes],
        "decisions": [item.model_dump(mode="json", exclude={"excluded_uses"} if not item.excluded_uses else set()) for item in decisions],
        "preliminary_edit_plan": None if preliminary_edit_plan is None else preliminary_edit_plan.model_dump(mode="json"),
        "actions": [item.model_dump(mode="json") for item in actions],
        "policy": [value.model_dump(mode="json") for value in policies],
        "capability": capability_identity,
        "crop_proposals": [proposal.model_dump(mode="json") for proposal in crop_proposals],
        "crop_outcomes": crop_outcomes,
    })
    if expected_fingerprint is not None and expected_fingerprint != identity:
        raise StaleProductionPreflight("stale_production_preflight")
    return ProductionPreflight(
        project_id=project.id, draft_version=draft.version, script_revision=draft.script_revision,
        reusable_master_audio_id=None if reusable_master is None else reusable_master.id,
        evidence_level="metadata_and_adopted_use_constraint" if any(item.excluded_uses for item in decisions) else "metadata_and_persisted_negative_assessment" if "unusable" in crop_outcomes.values() else "metadata_and_video_interval_analysis" if any(item.suitability == "analyzed_source_interval" for item in decisions) else "metadata_and_reviewed_planned_run" if any(item.suitability == "reviewed_native_planned_run" for item in decisions) else "metadata_and_reviewed_source_interval" if any(item.suitability == "reviewed_source_interval" for item in decisions) else "deterministic_metadata_only",
        fingerprint=identity, status="blocked" if stop else "ready_for_authorized_dispatch",
        scenes=tuple(decisions), preliminary_edit_plan=preliminary_edit_plan,
        actions=tuple(actions), cost_estimate=cost,
        stop_reasons=tuple(dict.fromkeys(stop)), authorization_needs=tuple(dict.fromkeys(authorization)),
    )


def dominant_landscape_fallback_in_spec(spec: VideoSpec, assets: AssetRepository) -> tuple[str, ...]:
    """Catch repeated full-frame landscape fallback before a new render."""
    if spec.format is not ProjectFormat.VERTICAL:
        return ()
    entries = {entry.scene_id: entry for entry in spec.edit_plan.scenes} if spec.edit_plan else {}
    return _dominant_landscape_fallback(
        ((scene.scene_id, scene.duration_frames, scene.visual.asset_id
          if scene.visual.vertical_reframe_mode.value == "contain" else None,
          scene.visual_role, entries[scene.scene_id].portrait_presentation)
         for scene in spec.scenes if scene.scene_id in entries),
        assets,
    )


def _dominant_landscape_fallback(scenes, assets: AssetRepository) -> tuple[str, ...]:
    """Compatibility for old callers; EditPlan owns the shared whole-video rule."""
    return dominant_landscape_fallback(scenes, assets)


def _reviewed_native_planned(db, project, scene, asset, clip, master):
    if asset is None or clip is None or asset.width >= asset.height:
        return False
    from app.db import TalkingRunRepository
    from app.talking.admission import talking_visual_blocker
    metadata = asset.metadata.get("talking_run")
    run_id = metadata.get("run_id") if isinstance(metadata, dict) else None
    try:
        run = TalkingRunRepository(db).get(UUID(run_id)) if isinstance(run_id, str) else None
    except ValueError:
        return False
    return (run is not None and run.planned_origin_sha256 is not None
            and run.assembled_asset_id == asset.id and run.assembled_clip_id == clip.id
            and talking_visual_blocker(asset, clip, AssetRepository(db), project_id=project.id,
                copy=scene.voice_text, master_audio_id=None if master is None else master.id) is None)


def _choose_candidate(scene: ScenePlan, candidates: tuple[CandidateAsset, ...], clips: ClipRepository, assets: AssetRepository, *, portrait_output: bool = True) -> tuple[CandidateAsset | None, Literal["none", "new_talking", "capture"]]:
    talking_sources = {SourceKind.AI_VIDEO, SourceKind.TALKING_PROFILE}
    allows_new_talking = bool(talking_sources.intersection((*scene.preferred_sources, *scene.fallback_sources)))
    if talking_sources.intersection(scene.preferred_sources) or scene.visual_requirement == "creator_speaking":
        from app.voice_qa import comparison_tokens

        target = "".join(comparison_tokens(scene.voice_text))
        for candidate in candidates:
            if candidate.source_kind not in {SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET} or candidate.clip_id is None:
                continue
            clip = clips.get(candidate.clip_id)
            asset = assets.get(candidate.asset_id) if candidate.asset_id is not None else None
            if clip is not None and asset is not None and asset.has_audio and clip.talking_candidate and clip.transcript and "".join(comparison_tokens(clip.transcript)) == target:
                return candidate, "none"
        admitted = next((candidate for candidate in candidates if candidate.source_kind is SourceKind.AI_VIDEO), None) if allows_new_talking else None
        return (admitted, "none") if admitted is not None else (None, "new_talking" if allows_new_talking else "capture")
    if not portrait_output:
        # 横版自动选材不能重新优先选中之前为竖屏裁出的画面。
        candidates = tuple(item for item in candidates if not (
            (asset := assets.get(item.asset_id) if item.asset_id else None) is not None
            and asset.width <= asset.height and isinstance(asset.metadata.get("source_derivation"), dict)
            and asset.metadata["source_derivation"].get("crop_window")))
        if not candidates:
            return None, "capture"
    recommended = next((candidate for candidate in candidates if candidate.recommended), candidates[0])
    if recommended.requires_capture or recommended.source_kind is SourceKind.CAPTURE:
        return None, "capture"
    if recommended.source_kind in {SourceKind.USER_ASSET,SourceKind.HISTORICAL_ASSET,SourceKind.STOCK,SourceKind.AI_BROLL}:
        source = assets.get(recommended.asset_id)
        original = clips.get(recommended.clip_id)
        if portrait_output and source is not None and original is not None and source.width > source.height:
            # Reuse an existing exact-source portrait derivative before
            # proposing another crop or asking for different footage.
            for asset in assets.list():
                meta = asset.metadata.get("source_derivation",{})
                interval = meta.get("source_interval",{})
                if (asset.source_kind is not source.source_kind or asset.width>asset.height or meta.get("source_asset_id")!=str(source.id)
                    or meta.get("source_content_hash")!=source.content_hash
                    or interval.get("start_ms")!=original.start_ms
                    or interval.get("end_ms")!=original.end_ms):
                    continue
                # Cropping one half out of a comparison destroys its evidence.
                intent=" ".join((scene.purpose,scene.visual_intent.description or "",scene.visual_intent.action or "")).lower()
                if any(t in intent for t in ("对比","比较","comparison","contrast")) and meta.get("crop_window",{}).get("width")!=source.width:
                    continue
                from app.media import VerticalSourceDerivationService,MediaImporter,FFProbeAdapter
                for derived in clips.list_by_asset(asset.id):
                    try:
                        root=getattr(assets.db,"data_root",None)
                        if root is None:continue
                        VerticalSourceDerivationService(assets.db,root,MediaImporter(assets.db,root,FFProbeAdapter())).verify_existing(asset,derived)
                    except (ValueError,RuntimeError,OSError,KeyError):
                        continue
                    if derived.end_ms-derived.start_ms>=scene.duration_target_ms:
                        start = None if recommended.playback_start_ms is None else derived.start_ms + recommended.playback_start_ms - original.start_ms
                        end = None if recommended.playback_end_ms is None else derived.start_ms + recommended.playback_end_ms - original.start_ms
                        return recommended.model_copy(update=dict(asset_id=asset.id,clip_id=derived.id,source_kind=asset.source_kind,
                            playback_start_ms=start, playback_end_ms=end,
                            why=[*recommended.why,"verified same-source same-interval portrait derivative"])),"none"
            for item in candidates:
                asset=assets.get(item.asset_id) if item.asset_id else None
                if (item.source_kind in {SourceKind.USER_ASSET,SourceKind.HISTORICAL_ASSET,SourceKind.STOCK}
                    and item.match_score>=recommended.match_score and asset is not None and asset.width<=asset.height
                    and not asset.metadata.get("source_derivation")):
                    return item.model_copy(update={"why":[*item.why,"equally relevant native portrait footage preferred before rendering"]}),"none"
    return recommended, "none"


def _playback_interval(candidate, clip, duration_ms):
    if candidate is None or clip is None:
        return None
    start = candidate.playback_start_ms if candidate.playback_start_ms is not None else clip.start_ms
    end = candidate.playback_end_ms if candidate.playback_end_ms is not None else clip.end_ms
    return start, min(end, start + duration_ms)


def _subtitle_state(asset: Asset | ImageAsset | None, clip=None, playback_interval_ms=None) -> tuple[Literal["unknown", "present", "absent"], str | None]:
    if asset is None:
        return "absent", None
    facts = full_interval_geometry(asset, clip, playback_interval_ms)
    if facts.get("method") == "human_full_interval_review" and facts.get("source_subtitle_clearance_verified") is True:
        return ("present" if facts['crop_window']['height'] < asset.height else "absent"), None
    if facts and isinstance(facts.get("subtitle_regions"), list):
        return ("present" if facts["subtitle_regions"] else "absent"), None
    state = asset.metadata.get("burned_in_subtitles")
    evidence = asset.metadata.get("subtitle_evidence_reference")
    if state == "present" and isinstance(evidence, str) and evidence.strip():
        return state, None
    return "unknown", "burned-in subtitle state lacks bound evidence"


def _approved_master(db: Database, project_id: UUID, copy: str, *, production_run_id=None) -> AudioAsset | None:
    from app.voice_boundary_alignment import voice_asset_project_id
    from app.voice_qa import comparison_tokens, voice_human_review_status
    from app.media.source_audio import admitted_source_audio_binding

    target = "".join(comparison_tokens(copy))
    for audio in AudioAssetRepository(db).list():
        source_binding = admitted_source_audio_binding(db, audio, project_id)
        generation = audio.metadata.get("voice_generation")
        if source_binding is None:
            if voice_asset_project_id(audio, JobRepository(db)) != project_id or not isinstance(generation, dict):
                continue
            from app.assembly.video_spec import _require_generated_voice_qa
            try:
                _require_generated_voice_qa(audio,db,production_run_id=production_run_id)
                from app.voice_plan import require_project_voice_audio
                require_project_voice_audio(db,project_id,audio)
            except ValueError:
                continue
            spoken_text = generation.get("target_text") or " ".join(segment.text for segment in audio.transcript_segments)
        else:
            spoken_text = " ".join(segment.text for segment in audio.transcript_segments)
        spoken = "".join(comparison_tokens(spoken_text))
        if target and target == spoken:
            return audio
    return None


def _action(kind, scene_id, required, attempts, reason, category, amount, identity) -> PreflightAction:
    return PreflightAction(
        kind=kind, scene_plan_id=scene_id, required=required, max_attempts=attempts,
        reason=reason, cost=UsageCost(
            category=category, amount=amount, currency="USD" if amount is not None else None,
            note="provider price/selection unknown" if amount is None else "local provider cash cost only; compute time unknown",
        ), input_fingerprint=identity,
    )


def _digest(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
