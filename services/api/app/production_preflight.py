"""Read-only, provider-neutral production preview for the current ProjectDraft.

This is a planning seam, not a dispatcher or an asset-suitability oracle. It
uses the normal local Router and exposes every unproven condition before work.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.assembly.edit_plan import EditPlanPreflightError, resolve_edit_plan, _default_edit_plan
from app.budget import snapshot
from app.costs import estimate_whole_production
from app.db import (
    AssetRepository, AudioAssetRepository, BudgetPolicyRepository, ClipRepository, Database, JobRepository,
    ImageAssetRepository, ProviderCallRepository, ProviderMachineCapabilityProfileRepository,
    ProviderMachineSettingRepository,
)
from app.domain.models import (
    Asset, AudioAsset, CandidateAsset, CostCategory, CostEstimate, EditPlan, EditVisualRole, ImageAsset,
    GraphicTreatment, Project, ProjectDraft, ScenePlan, SourceKind,
    SubtitleTreatment, UsageCost, VisualStyleTokens,
)
from app.media.crop_assessment import CropAssessmentRepository, CropProposal
from app.routing import AssetRouter
from app.routing.asset_router import WEAK_LEXICAL_ROUTE_REASON
from app.search import ClipTextSearchService
from app.source_use_constraints import SourceUseConstraintService, UseConstraintDecision, presentation


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
    suitability: Literal["known_deterministic", "reviewed_native_planned_run", "unknown"]
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
    evidence_level: Literal["deterministic_metadata_only", "metadata_and_persisted_negative_assessment", "metadata_and_reviewed_planned_run", "metadata_and_adopted_use_constraint"] = "deterministic_metadata_only"
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
    purpose: str = "commercial_production",
    use_admission_ids: tuple[UUID, ...] = (),
    data_root=None,
) -> ProductionPreflight:
    if purpose not in ("commercial_production", "internal_evaluation"):
        raise PreflightInputError("execution_purpose_unsupported")
    if purpose == "commercial_production" and use_admission_ids:
        raise PreflightInputError("evaluation_scope_not_commercial")
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
    reusable_master = _approved_master(db, project.id, voice_copy)
    routing_scenes = scenes
    if reusable_master is not None:
        from app.assembly.video_spec import _master_narration_intervals, NarrationTimelineError
        try:
            intervals = _master_narration_intervals(reusable_master, scenes)
        except NarrationTimelineError as exc:
            if bound_talking_clips:
                raise PreflightInputError("approved Master timing does not resolve current scenes") from exc
            # Preliminary plans may have whole-copy timing but no scene cuts.
            # They remain estimates; bound production resume cannot use them.
        else:
            routing_scenes = [scene.model_copy(update={"duration_target_ms": end - start})
                              for scene, start, end in intervals]
    assessments = CropAssessmentRepository(db)
    crop_outcomes = {str(scene_id): assessments.decision(proposal) for scene_id, proposal in proposal_map.items()}
    router = AssetRouter(ClipTextSearchService(db), assets, images=images, crop_proposals=proposal_map)
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
    stop: list[str] = []
    authorization: list[str] = ["provider_license_and_privacy_scope"]
    if reusable_master is None:
        authorization.append("voice_consent")
    need_analysis = False

    for scene in scenes:
        candidate, need = _choose_candidate(scene, route_by_scene[scene.id].candidates, clips, assets)
        if bound_talking_clips is not None and scene.id in bound_talking_clips:
            candidate = next((item for item in route_by_scene[scene.id].candidates
                              if item.clip_id == bound_talking_clips[scene.id]
                              and item.source_kind is SourceKind.AI_VIDEO), None)
            if candidate is None:
                raise PreflightInputError("bound planned TalkingRun is no longer an eligible candidate")
            need = "none"
        excluded = []
        blocked_candidates = set()
        for item in route_by_scene[scene.id].candidates:
            item_source = assets.get(item.asset_id) if item.asset_id is not None else None
            burned_state, _ = _subtitle_state(item_source)
            entry = _default_edit_plan(project, [scene], {scene.id: item}).scenes[0].model_copy(update={
                "burned_in_subtitles": burned_state,
                "subtitle_treatment": SubtitleTreatment.NONE if burned_state == "present" else SubtitleTreatment.TIMED_CAPTIONS,
            })
            matches = use_service.evaluate(item, presentation(entry, width=project.resolution_width,
                height=project.resolution_height, style=style_tokens), use_records)
            if matches:
                excluded.extend(matches)
                blocked_candidates.add((item.asset_id, item.clip_id, item.source_kind))
        if candidate is not None and (candidate.asset_id, candidate.clip_id, candidate.source_kind) in blocked_candidates:
            if bound_talking_clips is not None and scene.id in bound_talking_clips:
                raise PreflightInputError("bound_source_use_excluded_or_unresolved_replan")
            requires_talking = SourceKind.AI_VIDEO in scene.preferred_sources or candidate.source_kind is SourceKind.AI_VIDEO
            # No new source-admission authority: only an already supported,
            # explicitly allowed deterministic alternative qualifies here.
            alternative = next((item for item in route_by_scene[scene.id].candidates
                if (item.asset_id, item.clip_id, item.source_kind) not in blocked_candidates and item.source_kind is SourceKind.TYPOGRAPHY
                and not requires_talking and scene.visual_requirement not in {"creator_speaking", "action_evidence"}
                and SourceKind.TYPOGRAPHY in (*scene.preferred_sources, *scene.fallback_sources)), None)
            if alternative is None and not requires_talking and scene.visual_requirement not in {"creator_speaking", "action_evidence"} and SourceKind.TYPOGRAPHY in (*scene.preferred_sources, *scene.fallback_sources):
                from app.routing.asset_router import _typography_fallback
                alternative = _typography_fallback(scene, recommended=True)
            if alternative is None and SourceKind.AI_VIDEO in (*scene.preferred_sources, *scene.fallback_sources):
                alternative = next((item for item in route_by_scene[scene.id].candidates
                    if item.source_kind is SourceKind.AI_VIDEO
                    and (item.asset_id, item.clip_id, item.source_kind) not in blocked_candidates
                    and _reviewed_native_planned(db, project, scene, assets.get(item.asset_id), clips.get(item.clip_id), reusable_master)), None)
            candidate, need = (alternative, "none") if alternative is not None else (None, "capture")
            if candidate is None:
                stop.append(f"source_use_replan_required:{scene.scene_id}")
        kind = candidate.source_kind if candidate is not None else SourceKind.AI_VIDEO if need == "new_talking" else SourceKind.CAPTURE
        role = EditVisualRole.TALKING if kind is SourceKind.AI_VIDEO else EditVisualRole.GRAPHIC if kind is SourceKind.TYPOGRAPHY else EditVisualRole.FOOTAGE
        asset = assets.get(candidate.asset_id) if candidate is not None and candidate.asset_id is not None else None
        image = images.get(candidate.asset_id) if candidate is not None and candidate.source_kind in {SourceKind.SCREENSHOT, SourceKind.CHART} and candidate.asset_id is not None else None
        source = asset or image
        clip = clips.get(candidate.clip_id) if candidate is not None and candidate.clip_id is not None else None
        burned, subtitle_reason = _subtitle_state(source)
        subtitle = SubtitleTreatment.NONE if burned == "present" else SubtitleTreatment.TIMED_CAPTIONS
        reviewed_native = kind is SourceKind.AI_VIDEO and _reviewed_native_planned(db, project, scene, asset, clip, reusable_master)
        if reviewed_native and burned == "unknown":
            subtitle = SubtitleTreatment.NONE
        reasons: list[str] = []
        if crop_outcomes.get(str(scene.id)) == "unusable":
            reasons.append("proposed interval/crop has persisted face-boundary failure; use another visual route")
        if subtitle_reason:
            reasons.append(subtitle_reason)
        if need == "new_talking":
            reasons.append("no admitted same-project TalkingRun for this exact speech")
            authorization.append("talking_reference_consent")
            actions.append(_action(
                "talking", scene.id, True, 1,
                "generate only this required creator-speaking interval after Master admission",
                CostCategory.TALKING, None,
                _digest({"scene": scene.model_dump(mode="json"), "voice": voice_identity, "capability": capability_identity}),
            ))
            stop.append(f"talking_source_not_admitted:{scene.scene_id}")
        elif need == "capture":
            reasons.append("creator capture or an explicit alternative is required")
            capture = next((item for item in route_by_scene[scene.id].candidates if item.source_kind is SourceKind.CAPTURE), None)
            if capture is not None and WEAK_LEXICAL_ROUTE_REASON in capture.why:
                reasons.append("weak lexical retrieval does not establish source unusability or capture necessity; resolve visual intent before dispatch")
                stop.append(f"visual_route_unresolved:{scene.scene_id}")
            stop.append(f"capture_required:{scene.scene_id}")
        elif candidate is not None:
            selected.append(candidate)
        if source is not None and not reviewed_native:
            reasons.append("full-interval visual and portrait suitability have not been independently verified")
            authorization.append(f"source_rights_scope:{scene.scene_id}")
            need_analysis = True
            stop.append(f"source_suitability_unknown:{scene.scene_id}")
            if asset is not None and asset.width >= asset.height:
                actions.append(_action(
                    "transform", scene.id, False, 1,
                    "portrait transform is conditional on interval-bound face/subtitle evidence",
                    CostCategory.USER_ASSET, None,
                    _digest({"asset": asset.id, "hash": asset.content_hash, "rights": asset.authorization_reference, "clip": None if clip is None else clip.model_dump(mode="json"), "style": style_tokens.model_dump(mode="json")}),
                ))
        if burned == "present":
            reasons.append("new timed captions disabled to avoid overlapping source subtitles")
        if burned == "unknown" and source is not None:
            reasons.append("source subtitles are unknown; absence has not been verified")
        if reviewed_native:
            reasons.append("exact approved native-portrait planned Run; preserve full frame without a new crop")
            if burned == "unknown":
                reasons.append("subtitle state remains unknown; no new caption overlay is authorized")
        unsupported_graphic = scene.graphic_plan is not None and scene.graphic_plan.kind == "unsupported"
        if unsupported_graphic:
            stop.append(f"graphic_realization_unsupported:{scene.scene_id}")
            reasons.append("declared graphic requires unsupported realization; typography cannot silently replace it")
        elif kind is SourceKind.TYPOGRAPHY and scene.graphic_plan is None:
            reasons.append("legacy graphic realization is undeclared; intended effect coverage remains unverified")
        suitability = "unknown" if unsupported_graphic else "known_deterministic" if kind is SourceKind.TYPOGRAPHY else "reviewed_native_planned_run" if reviewed_native else "unknown"
        visual_identity_payload = {
            "source_kind": kind.value, "asset_id": None if source is None else str(source.id),
            "asset_hash": None if source is None else source.content_hash,
            "authorization": None if source is None else source.authorization_reference,
            "clip": None if clip is None else clip.model_dump(mode="json"),
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
            graphic_treatment=GraphicTreatment.HEADLINE if role is EditVisualRole.GRAPHIC and scene.order == 0 else GraphicTreatment.KEY_POINT if role is EditVisualRole.GRAPHIC else GraphicTreatment.NONE,
            reasons=tuple(reasons), excluded_uses=tuple(excluded), visual_dependency_fingerprint=visual_identity,
        ))

    actions.insert(0, _action(
        "voice", None, reusable_master is None, 1 if reusable_master is None else 0,
        "produce and admit a new exact-copy MasterNarration" if reusable_master is None else "reuse exact-copy approved MasterNarration without another human review",
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
            candidate.clip_id: (clip.start_ms, clip.end_ms)
            for candidate in selected
            if candidate.clip_id is not None and (clip := clips.get(candidate.clip_id)) is not None
        }
        try:
            base_plan = resolve_edit_plan(project, scenes, selections, clip_intervals=intervals, supplied=None)
            by_scene = {decision.scene_plan_id: decision for decision in decisions}
            preliminary_edit_plan = EditPlan(
                project_id=project.id, style_tokens=style_tokens,
                scenes=[entry.model_copy(update={
                    "subtitle_treatment": by_scene[entry.scene_plan_id].subtitle_treatment,
                    "burned_in_subtitles": by_scene[entry.scene_plan_id].burned_in_subtitles,
                }) for entry in base_plan.scenes],
            )
            resolve_edit_plan(project, scenes, selections, clip_intervals=intervals, supplied=preliminary_edit_plan)
        except (EditPlanPreflightError, ValueError):
            preliminary_edit_plan = None
            stop.append("preliminary_edit_plan_conflict")
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
    if any(action.required and action.kind in {"voice", "talking"} for action in actions):
        stop.append("provider_license_scope_unverified")
    for capability in ("voice", "talking"):
        if capability == "voice" and reusable_master is not None:
            continue
        if capability == "talking" and not any(action.kind == "talking" for action in actions):
            continue
        verified = any(
            profile.capability == capability and profile.readiness == "verified"
            and profile.quality_status == "verified" and profile.evidence_reference
            for profile in profiles
        )
        if not verified:
            stop.append(f"{capability}_capability_not_verified")
    identity = _digest({
        "project_id": str(project.id), "script_revision": draft.script_revision,
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
    if purpose == "internal_evaluation":
        from app.execution_scope import ExecutionScopeService
        from app.execution_admission import UseAdmissionError
        scope_fingerprint = None
        if data_root is None:
            stop.append("execution_scope_data_root_required")
        else:
            try:
                scope = ExecutionScopeService(db, data_root).prepare(project.id,
                    purpose=purpose, admission_ids=use_admission_ids)
                scope_fingerprint = scope.fingerprint
            except UseAdmissionError as exc:
                stop.append(str(exc))
        # No gate removal until Worker and whole-lane lineage are closed.
        stop.append("evaluation_execution_not_integrated")
        identity = _digest({"policy_version": 1, "commercial_plan": identity, "purpose": purpose,
            "use_admission_ids": sorted(str(value) for value in use_admission_ids), "scope": scope_fingerprint})
    if expected_fingerprint is not None and expected_fingerprint != identity:
        raise StaleProductionPreflight("stale_production_preflight")
    return ProductionPreflight(
        project_id=project.id, draft_version=draft.version, script_revision=draft.script_revision,
        reusable_master_audio_id=None if reusable_master is None else reusable_master.id,
        evidence_level="metadata_and_adopted_use_constraint" if any(item.excluded_uses for item in decisions) else "metadata_and_persisted_negative_assessment" if "unusable" in crop_outcomes.values() else "metadata_and_reviewed_planned_run" if any(item.suitability == "reviewed_native_planned_run" for item in decisions) else "deterministic_metadata_only",
        fingerprint=identity, status="blocked" if stop else "ready_for_authorized_dispatch",
        scenes=tuple(decisions), preliminary_edit_plan=preliminary_edit_plan,
        actions=tuple(actions), cost_estimate=cost,
        stop_reasons=tuple(dict.fromkeys(stop)), authorization_needs=tuple(dict.fromkeys(authorization)),
    )


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


def _choose_candidate(scene: ScenePlan, candidates: tuple[CandidateAsset, ...], clips: ClipRepository, assets: AssetRepository) -> tuple[CandidateAsset | None, Literal["none", "new_talking", "capture"]]:
    if SourceKind.AI_VIDEO in scene.preferred_sources or scene.visual_requirement == "creator_speaking":
        from app.voice_qa import comparison_tokens

        target = "".join(comparison_tokens(scene.voice_text))
        for candidate in candidates:
            if candidate.source_kind not in {SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET} or candidate.clip_id is None:
                continue
            clip = clips.get(candidate.clip_id)
            asset = assets.get(candidate.asset_id) if candidate.asset_id is not None else None
            if clip is not None and asset is not None and asset.has_audio and clip.talking_candidate and clip.transcript and "".join(comparison_tokens(clip.transcript)) == target:
                return candidate, "none"
        admitted = next((candidate for candidate in candidates if candidate.source_kind is SourceKind.AI_VIDEO), None)
        return (admitted, "none") if admitted is not None else (None, "new_talking" if SourceKind.AI_VIDEO in scene.preferred_sources else "capture")
    recommended = next((candidate for candidate in candidates if candidate.recommended), candidates[0])
    if recommended.requires_capture or recommended.source_kind is SourceKind.CAPTURE:
        return None, "capture"
    return recommended, "none"


def _subtitle_state(asset: Asset | ImageAsset | None) -> tuple[Literal["unknown", "present", "absent"], str | None]:
    if asset is None:
        return "absent", None
    state = asset.metadata.get("burned_in_subtitles")
    evidence = asset.metadata.get("subtitle_evidence_reference")
    if state == "present" and isinstance(evidence, str) and evidence.strip():
        return state, None
    return "unknown", "burned-in subtitle state lacks bound evidence"


def _approved_master(db: Database, project_id: UUID, copy: str) -> AudioAsset | None:
    from app.voice_boundary_alignment import voice_asset_project_id
    from app.voice_qa import comparison_tokens, voice_human_review_status

    target = "".join(comparison_tokens(copy))
    for audio in AudioAssetRepository(db).list():
        if (
            not audio.authorization_reference.strip()
            or voice_asset_project_id(audio, JobRepository(db)) != project_id
            or voice_human_review_status(audio) != "approved"
        ):
            continue
        spoken = "".join(comparison_tokens(" ".join(segment.text for segment in audio.transcript_segments)))
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
