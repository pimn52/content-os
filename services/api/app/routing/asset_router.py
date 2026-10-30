"""Deterministic local routing from a ScenePlan to continuous Clip candidates.

The router deliberately receives a query-capable search service rather than an
embedding or LLM provider.  ``ClipEmbeddingIndexer`` is one compatible
implementation, but the policy and score calculation remain provider-neutral.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence
from uuid import UUID

from app.db import AssetRepository, ClipRepository, ImageAssetRepository, ProjectRepository, TalkingRunRepository
from app.domain.models import Asset, CandidateAsset, Clip, CostCategory, ProjectFormat, ScenePlan, SourceKind, UsageCost
from app.media.crop_assessment import CropAssessmentRepository, CropProposal
from app.search import ClipSearchHit
from app.talking.admission import talking_visual_blocker

WEAK_LEXICAL_ROUTE_REASON = "Weak lexical retrieval leaves the visual route unresolved; capture is an optional proposal, not proven necessary."
COMPARABLE_RELEVANCE_MARGIN = 0.05
CREATOR_PREFERENCE_MARGIN = 0.05

def typography_preserves_declared_intent(scene: ScenePlan) -> bool:
    """Only an intended primary graphic can lead; media failure cannot promote one."""
    if SourceKind.AI_VIDEO in scene.preferred_sources or scene.visual_requirement in {"creator_speaking", "action_evidence"}:
        return False
    return bool(scene.preferred_sources and scene.preferred_sources[0] is SourceKind.TYPOGRAPHY)


class RoutingConfigurationError(ValueError):
    pass


class RoutingInputError(ValueError):
    pass


class SceneClipSearcher(Protocol):
    """Natural-language Clip retrieval boundary implemented by local search."""

    def search(
        self,
        query: str,
        *,
        top_k: int = 10,
        asset_id: UUID | None = None,
        orientation: ProjectFormat | str | None = None,
        talking_candidate: bool | None = None,
    ) -> list[ClipSearchHit]: ...


@dataclass(frozen=True)
class RoutingWeights:
    """Weights for normalized [0, 1] ranking features."""

    semantic_match: float = 0.55
    visual_quality: float = 0.15
    shot_suitability: float = 0.10
    occupancy_penalty: float = 0.15

    def __post_init__(self) -> None:
        for name, value in self.__dict__.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or not 0 <= float(value) <= 1:
                raise RoutingConfigurationError(f"{name} must be a finite number from 0 to 1")
        if not any(float(getattr(self, name)) for name in ("semantic_match", "visual_quality", "shot_suitability")):
            raise RoutingConfigurationError("at least one positive match weight is required")


@dataclass(frozen=True)
class RoutingResult:
    """Stable candidates for exactly one ScenePlan, with no persistence side effects."""

    scene_plan_id: UUID
    candidates: tuple[CandidateAsset, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.candidates, tuple) or not self.candidates:
            raise ValueError("routing candidates must be a non-empty immutable tuple")
        if any(candidate.scene_plan_id != self.scene_plan_id for candidate in self.candidates):
            raise ValueError("routing candidates must belong to one ScenePlan")
        if sum(candidate.recommended for candidate in self.candidates) != 1:
            raise ValueError("routing must have exactly one recommended candidate")


class AssetRouter:
    """Score eligible continuous local Clips and expose a capture gap when needed."""

    def __init__(
        self,
        searcher: SceneClipSearcher,
        assets: AssetRepository,
        *,
        weights: RoutingWeights | None = None,
        candidate_pool_size: int = 20,
        max_candidates: int = 3,
        capture_gap_threshold: float = 0.45,
        allowed_source_kinds: Sequence[SourceKind] = (SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET, SourceKind.STOCK, SourceKind.AI_VIDEO, SourceKind.AI_BROLL),
        images: ImageAssetRepository | None = None,
        crop_proposals: Mapping[UUID, CropProposal] | None = None,
        now: Callable[[], datetime] | None = None,
        source_usage_run_id: UUID | None = None,
        source_usage_work_id: UUID | None = None,
    ) -> None:
        if not isinstance(candidate_pool_size, int) or isinstance(candidate_pool_size, bool) or not 1 <= candidate_pool_size <= 1_000:
            raise RoutingConfigurationError("candidate_pool_size must be an integer from 1 to 1000")
        if not isinstance(max_candidates, int) or isinstance(max_candidates, bool) or not 1 <= max_candidates <= candidate_pool_size:
            raise RoutingConfigurationError("max_candidates must be an integer from 1 through candidate_pool_size")
        if isinstance(capture_gap_threshold, bool) or not isinstance(capture_gap_threshold, (int, float)) or not math.isfinite(float(capture_gap_threshold)) or not 0 <= float(capture_gap_threshold) <= 1:
            raise RoutingConfigurationError("capture_gap_threshold must be a finite number from 0 to 1")
        if isinstance(allowed_source_kinds, (str, bytes)):
            raise RoutingConfigurationError("allowed_source_kinds must be non-empty SourceKind values")
        try:
            normalized_sources = tuple(allowed_source_kinds)
        except TypeError as exc:
            raise RoutingConfigurationError("allowed_source_kinds must be non-empty SourceKind values") from exc
        if not normalized_sources or any(not isinstance(source, SourceKind) for source in normalized_sources):
            raise RoutingConfigurationError("allowed_source_kinds must be non-empty SourceKind values")
        self.searcher = searcher
        self.assets = assets
        self.weights = weights or RoutingWeights()
        if not isinstance(self.weights, RoutingWeights):
            raise RoutingConfigurationError("weights must be RoutingWeights")
        self.candidate_pool_size = candidate_pool_size
        self.max_candidates = max_candidates
        self.capture_gap_threshold = float(capture_gap_threshold)
        self.allowed_source_kinds = frozenset(normalized_sources)
        self.images = images
        self.crop_proposals = dict(crop_proposals or {})
        self.crop_assessments = CropAssessmentRepository(assets.db)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.source_usage_run_id = source_usage_run_id
        self.source_usage_work_id = source_usage_work_id

    def _known_bad_crop(self, scene: ScenePlan, asset: Asset, clip: Clip) -> bool:
        proposal = self.crop_proposals.get(scene.id)
        return bool(
            proposal is not None
            and proposal.scene_plan_id == scene.id
            and proposal.source_asset_id == asset.id
            and proposal.source_clip_id == clip.id
            and proposal.end_ms - proposal.start_ms >= scene.duration_target_ms
            and self.crop_assessments.decision(proposal) == "unusable"
        )

    def route(self, scene: ScenePlan) -> RoutingResult:
        if not isinstance(scene, ScenePlan):
            raise RoutingInputError("routing requires a ScenePlan contract")
        if scene.preferred_clip_id is not None:
            return self._route_from_hits(scene,[])
        query = _scene_query(scene)
        # Retrieval errors retain the provider/indexer's own typed semantics.
        hits = self.searcher.search(query, top_k=self.candidate_pool_size)
        return self._route_from_hits(scene, hits)

    def _route_from_hits(self, scene: ScenePlan, hits: list[ClipSearchHit]) -> RoutingResult:
        if not isinstance(hits, list) or any(not isinstance(hit, ClipSearchHit) for hit in hits):
            raise RoutingInputError("scene searcher returned invalid Clip hits")
        if scene.preferred_clip_id is not None:
            clip=ClipRepository(self.assets.db).get(scene.preferred_clip_id)
            hits=[ClipSearchHit(clip,1.0,"planner_selection")] if clip is not None else []
        candidates = self._score_hits(scene, hits)
        relevance = {hit.clip.id: hit.score for hit in hits}
        asset_relevance = {item.asset_id: max((hit.score for hit in hits if hit.clip.asset_id == item.asset_id), default=0)
                           for _, item in candidates}
        for score, reviewed in self._reviewed_comparison_candidates(
            scene, candidates,
        ):
            candidates = [(value, item) for value, item in candidates if item.clip_id != reviewed.clip_id]
            candidates.append((score, reviewed))
            relevance[reviewed.clip_id] = asset_relevance.get(reviewed.asset_id, 0)
        # TalkingRun admission is persisted independently of the optional
        # search index. Discover it locally without another embedding call.
        seen_clips = {candidate.clip_id for _, candidate in candidates}
        for score, candidate in self._admitted_run_candidates(scene):
            if candidate.clip_id not in seen_clips:
                candidates.append((score, candidate))
                relevance[candidate.clip_id] = 1.0  # Exact reviewed speech, independently checked above.
        from app.media.semantic_rejections import negative_source_evidence,candidate_rejections
        from app.media.visual_facts import scene_visual_key
        project = ProjectRepository(self.assets.db).get(scene.project_id)
        negative = negative_source_evidence(self.assets.db,scene_visual_key(scene))
        if project is not None and negative:
            candidates = [(score,candidate) for score,candidate in candidates
                if not candidate_rejections(self.assets.db,scene,candidate,fps=project.fps,evidence=negative)]
        candidates.sort(key=lambda value: (
            -relevance.get(value[1].clip_id, 0),
            -value[0],
            str(value[1].asset_id), str(value[1].clip_id),
        ))
        if candidates and candidates[0][1].source_kind is not SourceKind.AI_VIDEO:
            # Retrieval remains the content-fit boundary. Within that narrow
            # neighbourhood, use quality/reuse and then a bounded creator
            # preference; ownership never increases the suitability score.
            best_relevance = relevance.get(candidates[0][1].clip_id, 0)
            comparable = [value for value in candidates
                if best_relevance - relevance.get(value[1].clip_id, 0) <= COMPARABLE_RELEVANCE_MARGIN + 1e-9
                and value[0] >= self.capture_gap_threshold]
            if comparable:
                best = max(comparable, key=lambda value: value[0])
                if best[1].source_kind is not SourceKind.AI_VIDEO:
                    creator = [value for value in comparable
                        if value[1].source_kind in {SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET}
                        and best[0] - value[0] <= CREATOR_PREFERENCE_MARGIN + 1e-9]
                    if creator:
                        best = max(creator, key=lambda value: value[0])
                        score, candidate = best
                        best = (score, candidate.model_copy(update={"why": [*candidate.why,
                            "内容匹配和综合适配相近，优先使用创作者素材。"]}))
                candidates = [best, *(value for value in candidates if value[1].clip_id != best[1].clip_id)]
        selected = candidates[: self.max_candidates]
        needs_capture = not selected or selected[0][0] < self.capture_gap_threshold
        requested_sources = set(scene.preferred_sources) | set(scene.fallback_sources)
        values = [candidate for _, candidate in selected]
        if (scene.visual_requirement == "explanatory" and scene.preferred_sources[0] is SourceKind.TYPOGRAPHY
                and scene.graphic_plan is not None and scene.graphic_plan.kind in {"text_card", "diagram"}):
            values.insert(0, _typography_fallback(scene, recommended=True))
            values = [item.model_copy(update={"recommended": index == 0}) for index, item in enumerate(values)]
            return RoutingResult(scene_plan_id=scene.id, candidates=tuple(values))
        if needs_capture:
            values.extend(_static_fallbacks(scene, requested_sources, self.images, recommended=not values))
            if SourceKind.TYPOGRAPHY in requested_sources:
                values.append(_typography_fallback(scene, recommended=not values and typography_preserves_declared_intent(scene)))
            values.append(_capture_gap(scene, recommended=not values))
        if not values and SourceKind.TYPOGRAPHY in requested_sources:
            values.append(_typography_fallback(scene, recommended=True))
        if values and not needs_capture:
            values[0] = CandidateAsset.model_validate({**values[0].model_dump(mode="python"), "recommended": True})
        elif values:
            # An explicitly preferred graphic route is established planning
            # intent. Merely allowing typography as fallback is not proof
            # that it can replace creator/footage intent.
            graphic = next((value for value in values if value.source_kind is SourceKind.TYPOGRAPHY), None)
            if graphic is not None and typography_preserves_declared_intent(scene):
                values = [value.model_copy(update={
                    "recommended": value is graphic,
                    "why": [*value.why, "Explicit primary typography preserves the planned graphic route."] if value is graphic else value.why,
                }) for value in values]
            elif not any(value.recommended for value in values):
                values[-1] = values[-1].model_copy(update={"recommended": True})
            if selected and all(hit.score_basis == "lexical_overlap" for hit in hits):
                values = [value.model_copy(update={"why": [*value.why, WEAK_LEXICAL_ROUTE_REASON]})
                    if value.source_kind is SourceKind.CAPTURE else value for value in values]
        return RoutingResult(scene_plan_id=scene.id, candidates=tuple(values))

    def route_all(self, scenes: Sequence[ScenePlan]) -> tuple[RoutingResult, ...]:
        if isinstance(scenes, (str, bytes)) or not scenes:
            raise RoutingInputError("routing requires one or more ScenePlans")
        values = tuple(scenes)
        if any(not isinstance(scene, ScenePlan) for scene in values):
            raise RoutingInputError("routing requires ScenePlan contracts")
        ordered = sorted(values, key=lambda scene: scene.order)
        if len({scene.id for scene in ordered}) != len(ordered):
            raise RoutingInputError("ScenePlan IDs must be unique")
        if [scene.order for scene in ordered] != list(range(len(ordered))):
            raise RoutingInputError("ScenePlan order must be contiguous and start at zero")
        search_many = getattr(self.searcher, "search_many", None)
        if callable(search_many) and all(scene.preferred_clip_id is None for scene in ordered):
            queries = tuple(_scene_query(scene) for scene in ordered)
            batches = search_many(queries, top_k=self.candidate_pool_size)
            if not isinstance(batches, list) or len(batches) != len(ordered):
                raise RoutingInputError("scene searcher returned invalid batched Clip hits")
            results = tuple(
                self._route_from_hits(scene, hits)
                for scene, hits in zip(ordered, batches, strict=True)
            )
        else:
            results = tuple(self.route(scene) for scene in ordered)
        project = ProjectRepository(self.assets.db).get(ordered[0].project_id)
        total_duration = sum(scene.duration_target_ms for scene in ordered)
        vertical = project is not None and project.format is ProjectFormat.VERTICAL
        landscape_uses: dict[UUID, tuple[int, int]] = {}
        used_clips: set[UUID] = set()
        from app.source_usage import SourceUsageService, SourceUsageConflict
        usage = SourceUsageService(self.assets.db)
        visible_uses = []
        balanced = []
        for scene, result in zip(ordered, results, strict=True):
            recommended = next(item for item in result.candidates if item.recommended)
            repeat_unavailable = False
            if recommended.clip_id is not None:
                try:
                    available = usage.allocate_candidate(recommended,scene.scene_id,scene.duration_target_ms,visible_uses,
                        fixed=recommended.playback_start_ms is not None or _comparison_intent(scene),
                        prefer_unused=not _comparison_intent(scene))
                    from app.media.semantic_rejections import candidate_rejections
                    if available is not None and project is not None and candidate_rejections(self.assets.db,scene,available,fps=project.fps):
                        available = None
                    if available is not None:
                        if available != recommended:
                            result = RoutingResult(scene_plan_id=scene.id,candidates=tuple(
                                available if item is recommended else item for item in result.candidates))
                            recommended = available
                    else:
                        repeat_unavailable = any(not use.get("repeatable",False) for use in usage.candidate_uses(recommended,scene.scene_id,scene.duration_target_ms))
                except (SourceUsageConflict,KeyError,ValueError):
                    repeat_unavailable = True
            asset = self.assets.get(recommended.asset_id) if recommended.asset_id else None
            count, duration = landscape_uses.get(asset.id, (0, 0)) if asset is not None else (0, 0)
            # A reviewed comparison may deliberately revisit the same interval
            # to establish a controlled contrast. It still counts towards the
            # landscape-footage limit and all ordinary reuse checks.
            comparison_replay = ((_comparison_intent(scene) or scene.reuse_intent == "reference")
                and recommended.playback_start_ms is not None and recommended.playback_end_ms is not None)
            from app.source_usage import overlap
            try:
                repeated_interval=any(overlap(use,previous) for use in usage.candidate_uses(
                    recommended,scene.scene_id,scene.duration_target_ms) for previous in visible_uses)
            except SourceUsageConflict:
                repeated_interval=True
            repeated_clip = (repeat_unavailable or repeated_interval) and not comparison_replay
            overused_landscape = bool(
                vertical and asset is not None and asset.width > asset.height
                and (duration + scene.duration_target_ms) * 2 > total_duration
            )
            if repeated_clip or overused_landscape:
                alternative = next((item for item in result.candidates
                    if item.source_kind in {SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET, SourceKind.STOCK}
                    and item.clip_id is not None and item.clip_id not in used_clips
                    and item.clip_id != recommended.clip_id
                    and item.match_score >= self.capture_gap_threshold
                    and (other := self.assets.get(item.asset_id)) is not None
                    and (not overused_landscape or other.width <= other.height)), None)
                if alternative is None and not repeat_unavailable and recommended.clip_id is not None:
                    # Preserve the content while output framing is unresolved.
                    # A portrait layout problem cannot authorize a graphic substitute.
                    alternative = recommended.model_copy(update={"why": [*recommended.why,
                        "原样画面保留供用途和整体画幅判断；优先选择应景且适合竖屏的镜头与布局。"]})
                if alternative is None:
                    alternative = _capture_gap(scene, recommended=True).model_copy(update={
                        "why": ["Avoided repeating the same source clip or overusing landscape footage.",
                                "需要其他合适画面或竖屏布局；素材缺口不能靠解释图文或大字卡填补。"]
                    })
                candidates = [item for item in result.candidates if item is not recommended or alternative.clip_id != recommended.clip_id]
                if alternative not in candidates:
                    candidates.append(alternative)
                result = RoutingResult(scene_plan_id=scene.id, candidates=tuple(
                    item.model_copy(update={"recommended": item is alternative}) for item in candidates))
                recommended = alternative
                asset = self.assets.get(recommended.asset_id) if recommended.asset_id else None
            if recommended.clip_id is not None:
                used_clips.add(recommended.clip_id)
                try:
                    visible_uses.extend(usage.candidate_uses(recommended,scene.scene_id,scene.duration_target_ms))
                except SourceUsageConflict:
                    pass  # Ordinary preflight reports unresolved provenance.
            if vertical and asset is not None and asset.width > asset.height:
                count, duration = landscape_uses.get(asset.id, (0, 0))
                landscape_uses[asset.id] = (count + 1, duration + scene.duration_target_ms)
            balanced.append(result)
        return tuple(balanced)

    def explicit_footage(
        self, scene: ScenePlan, selections: Sequence[CandidateAsset],
    ) -> CandidateAsset | None:
        """Reopen a saved footage choice; selection is not suitability approval."""
        choice = next((c for c in selections if c.scene_plan_id == scene.id), None)
        if choice is None or choice.source_kind not in {SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET, SourceKind.STOCK, SourceKind.AI_BROLL}:
            return None
        asset = self.assets.get(choice.asset_id) if choice.asset_id else None
        clip = ClipRepository(self.assets.db).get(choice.clip_id) if choice.clip_id else None
        if choice.source_kind is SourceKind.AI_BROLL:
            from app.media.visual_facts import generated_footage_admitted
            if (asset is None or not generated_footage_admitted(asset)
                or scene.visual_requirement in {"creator_speaking","action_evidence"}):
                raise RoutingInputError("generated_footage_not_admitted_for_task")
        partial_interval_invalid = False
        if choice.playback_start_ms is not None and choice.playback_end_ms is not None:
            interval_duration = choice.playback_end_ms - choice.playback_start_ms
            if interval_duration < scene.duration_target_ms:
                partial_interval_invalid = True
        if (asset is None or clip is None or clip.asset_id != asset.id
            or asset.source_kind != choice.source_kind or clip.asset_duration_ms != asset.duration_ms
            or clip.end_ms > asset.duration_ms or clip.end_ms - clip.start_ms < scene.duration_target_ms
            or choice.playback_start_ms is not None and (
                choice.playback_start_ms < clip.start_ms or choice.playback_end_ms > clip.end_ms
                or partial_interval_invalid)
            or asset.metadata.get("project_id") not in (None, str(scene.project_id))
            or choice.source_kind not in (*scene.preferred_sources, *scene.fallback_sources)
            or scene.visual_requirement == "creator_speaking" or self._known_bad_crop(scene, asset, clip)):
            raise RoutingInputError("explicit_footage_selection_changed_or_not_authorized")
        from app.media.visual_facts import full_interval_geometry, matches_scene
        interval = (choice.playback_start_ms, choice.playback_end_ms) if choice.playback_start_ms is not None else None
        facts = full_interval_geometry(asset, clip, interval)
        if facts and not matches_scene(facts, scene):
            raise RoutingInputError("explicit_footage_source_scene_mismatch")
        from app.media.semantic_rejections import candidate_rejections
        project = ProjectRepository(self.assets.db).get(scene.project_id)
        if project is not None and candidate_rejections(self.assets.db,scene,choice,fps=project.fps):
            raise RoutingInputError('source_semantic_use_rejected')
        return choice

    def _score_hits(self, scene: ScenePlan, hits: Sequence[ClipSearchHit]) -> list[tuple[float, CandidateAsset]]:
        seen: set[UUID] = set()
        ranked: list[tuple[float, float, str, int, int, str, CandidateAsset]] = []
        now = _aware_now(self._now())
        requested_sources = set(scene.preferred_sources) | set(scene.fallback_sources)
        from app.source_usage import SourceUsageService, SourceUsageConflict
        usage = SourceUsageService(self.assets.db)
        occupied = usage.occupied(scene.project_id, run_id=self.source_usage_run_id, work_id=self.source_usage_work_id)
        for hit in hits:
            clip = hit.clip
            if clip.id in seen:
                continue
            seen.add(clip.id)
            asset = self.assets.get(clip.asset_id)
            if (
                asset is None
                or asset.source_kind is SourceKind.AI_VIDEO  # One reviewed-Run discovery path below.
                or asset.source_kind not in self.allowed_source_kinds
                or asset.source_kind not in requested_sources
                or asset.source_kind is SourceKind.STOCK and scene.visual_requirement == "creator_speaking"
                or asset.metadata.get("project_id") not in (None, str(scene.project_id))
                or clip.end_ms - clip.start_ms < scene.duration_target_ms
            ):
                continue
            if self._known_bad_crop(scene, asset, clip):
                continue
            if asset.source_kind is SourceKind.AI_BROLL:
                from app.media.visual_facts import generated_footage_admitted
                if scene.visual_requirement in {"creator_speaking","action_evidence"} or not generated_footage_admitted(asset):
                    continue
            try:
                degree = usage.clip_occupancy_degree(clip, occupied)
            except SourceUsageConflict:
                continue
            score, why = _score(scene, clip, asset, hit.score, self.weights, now, score_basis=hit.score_basis, occupancy=degree)
            from app.media.visual_facts import full_interval_geometry, matches_scene, scene_visual_key
            analysis = asset.metadata.get("visual_analysis", {})
            facts = analysis.get(str(clip.id), {}) if isinstance(analysis, dict) else {}
            interval = (facts.get("covered_start_ms"), facts.get("covered_end_ms")) if isinstance(facts, dict) else (None, None)
            known_interval = full_interval_geometry(asset, clip, interval)
            if asset.source_kind is SourceKind.AI_BROLL and not known_interval:
                continue
            if known_interval and not matches_scene(known_interval, scene):
                continue
            if known_interval and interval[1] - interval[0] < scene.duration_target_ms:
                continue
            choices=known_interval.get("editorial_selections",{})
            choice=choices.get(f"{scene_visual_key(scene)}:{scene.id}",{})
            if choice.get("scene_plan_id") != str(scene.id):
                choice=choices.get(scene_visual_key(scene),{})
                if choice.get("scene_id") != scene.scene_id or (choice.get("scene_plan_id") is not None
                    and choice.get("scene_plan_id") != str(scene.id)):
                    choice={}
            if choice.get("reason"):
                why.append(choice["reason"])
            chosen_start,chosen_end=choice.get("playback_start_ms"),choice.get("playback_end_ms")
            if (known_interval and type(chosen_start) is int and type(chosen_end) is int
                and interval[0] <= chosen_start < chosen_end <= interval[1]
                and chosen_end-chosen_start >= scene.duration_target_ms):
                interval=(chosen_start,chosen_end)
            candidate = CandidateAsset(
                scene_plan_id=scene.id,
                source_kind=asset.source_kind,
                asset_id=asset.id,
                clip_id=clip.id,
                playback_start_ms=interval[0] if known_interval else None,
                playback_end_ms=interval[1] if known_interval else None,
                match_score=score,
                why=why,
                reuse_count=clip.used_count,
                estimated_cost=_local_cost(asset.source_kind),
            )
            ranked.append((hit.score, score, str(asset.id), clip.start_ms, clip.end_ms, str(clip.id), candidate))
        ranked.sort(key=lambda value: (-value[0], -value[1], value[2], value[3], value[4], value[5]))
        return [(value[1], value[-1]) for value in ranked]

    def _admitted_run_candidates(self, scene: ScenePlan) -> list[tuple[float, CandidateAsset]]:
        if SourceKind.AI_VIDEO not in self.allowed_source_kinds or SourceKind.AI_VIDEO not in (
            set(scene.preferred_sources) | set(scene.fallback_sources)
        ):
            return []
        result = []
        for c in self.reviewed_talking_candidates(scene):
            asset = self.assets.get(c.asset_id)
            result.append((c.match_score,c))
        return result

    def _reviewed_comparison_candidates(
        self, scene: ScenePlan, ranked: list[tuple[float, CandidateAsset]],
    ) -> list[tuple[float, CandidateAsset]]:
        if not _comparison_intent(scene):
            return []
        from app.assembly.edit_plan import EditPlanPreflightError, comparison_clearance
        clips = ClipRepository(self.assets.db)
        best_by_asset: dict[UUID, float] = {}
        for score, candidate in ranked:
            if candidate.asset_id is not None:
                best_by_asset[candidate.asset_id] = max(best_by_asset.get(candidate.asset_id, 0), score)
        reviewed = []
        for asset_id, base_score in best_by_asset.items():
            asset = self.assets.get(asset_id)
            if asset is None or asset.width <= asset.height or asset.source_kind not in {SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET, SourceKind.STOCK}:
                continue
            records = asset.metadata.get("source_presentation_reviews")
            if not isinstance(records, dict):
                continue
            for clip_id, record in records.items():
                if not isinstance(record, dict) or not isinstance(record.get("clip_interval"), dict):
                    continue
                try:
                    clip = clips.get(UUID(clip_id))
                except ValueError:
                    continue
                interval = record["clip_interval"]
                start, end = interval.get("start_ms"), interval.get("end_ms")
                interval_duration = end - start if type(start) is int and type(end) is int else 0
                covers_scene = interval_duration >= scene.duration_target_ms
                if (clip is None or clip.asset_id != asset.id or type(start) is not int or type(end) is not int
                    or start < clip.start_ms or end > clip.end_ms or not covers_scene):
                    continue
                try:
                    comparison_clearance(asset, clip, (start, end))
                except (EditPlanPreflightError, ValueError):
                    continue
                score = min(1.0, base_score + 0.12)
                reviewed.append((score, CandidateAsset(
                    scene_plan_id=scene.id, source_kind=asset.source_kind, asset_id=asset.id, clip_id=clip.id,
                    playback_start_ms=start, playback_end_ms=end, match_score=score,
                    why=["same-source retrieval", "approved full comparison interval", "verified subtitle crop"],
                    reuse_count=clip.used_count, estimated_cost=_local_cost(asset.source_kind),
                )))
        return reviewed

    def reviewed_talking_candidates(self, scene: ScenePlan, *, master_audio_id=None, master_interval=None, run_id=None):
        """Shared media suitability, separate from current-purpose permission."""
        from app.media.vertical_derivation import VerticalSourceDerivationService
        from app.media.importer import MediaImporter
        from app.media.ffprobe import FFProbeAdapter
        db = self.assets.db
        root = getattr(db,"data_root",Path(db.path).parent)
        derivation = VerticalSourceDerivationService(db,root,MediaImporter(db,root,FFProbeAdapter()))
        clips = ClipRepository(db)
        candidates: list[CandidateAsset] = []
        for run in TalkingRunRepository(db).list_for_project(scene.project_id):
            if run_id is not None and run.id != run_id:
                continue
            visuals = [a for a in self.assets.list() if a.id==run.assembled_asset_id or
                isinstance(a.metadata.get("source_derivation"),dict) and a.metadata["source_derivation"].get("source_asset_id")==str(run.assembled_asset_id)]
            for asset in visuals:
                available = [clips.get(run.assembled_clip_id)] if asset.id==run.assembled_asset_id else clips.list_by_asset(asset.id)
                for clip in available:
                    if clip is None:
                        continue
                    if asset.id!=run.assembled_asset_id:
                        try:
                            derivation.verify_existing(asset,clip)
                        except (ValueError,RuntimeError,KeyError,TypeError,OSError):
                            continue
                    if master_interval is None and clip.end_ms-clip.start_ms<scene.duration_target_ms:
                        continue
                    if talking_visual_blocker(asset,clip,self.assets,project_id=scene.project_id,copy=scene.voice_text,
                        master_audio_id=master_audio_id,master_interval=master_interval,
                        selected_duration_ms=scene.duration_target_ms if master_interval is None else None) is not None or self._known_bad_crop(scene,asset,clip):
                        continue
                    candidate = CandidateAsset(
                        scene_plan_id=scene.id, source_kind=SourceKind.AI_VIDEO, asset_id=asset.id, clip_id=clip.id,
                        match_score=0.7+0.05*min(1,asset.height/asset.width/(16/9)),
                        why=["admitted TalkingRun", "same project and exact reviewed speech",
                            "verified existing portrait derivative" if asset.id!=run.assembled_asset_id else "reviewed complete frame; no new crop"],
                        reuse_count=clip.used_count, estimated_cost=_local_cost(SourceKind.AI_VIDEO))
                    candidates.append(candidate)
        return sorted(candidates,key=lambda c:(-c.match_score,str(c.asset_id),str(c.clip_id)))


def _scene_query(scene: ScenePlan) -> str:
    intent = scene.visual_intent
    values = (intent.subject, intent.action, intent.description)
    query = " ".join(value.strip() for value in values if isinstance(value, str) and value.strip())
    if not query:
        query = scene.voice_text.strip()
    if not query:
        raise RoutingInputError("ScenePlan has no searchable voice or visual intent")
    return query


def _comparison_intent(scene: ScenePlan) -> bool:
    intent = " ".join(str(value or "") for value in (
        scene.purpose, scene.visual_intent.subject, scene.visual_intent.action, scene.visual_intent.description)).casefold()
    return scene.visual_requirement == "action_evidence" and any(
        token in intent for token in ("对比", "比較", "比较", "comparison", "contrast")
    )


def _score(
    scene: ScenePlan,
    clip: Clip,
    asset: Asset,
    raw_semantic: object,
    weights: RoutingWeights,
    now: datetime,
    *,
    score_basis: str = "embedding_similarity",
    occupancy: float = 0.0,
) -> tuple[float, list[str]]:
    if isinstance(raw_semantic, bool) or not isinstance(raw_semantic, (int, float)) or not math.isfinite(float(raw_semantic)):
        raise RoutingInputError("Clip search hit score must be finite")
    if score_basis not in {"embedding_similarity", "lexical_overlap", "planner_selection"}:
        raise RoutingInputError("Clip search hit has an unsupported score basis")
    # Cosine similarity or lexical overlap at or below zero does not constitute
    # a useful retrieval match.
    semantic = max(0.0, min(1.0, float(raw_semantic)))
    quality = 0.5 if clip.quality_score is None else float(clip.quality_score)
    suitability = _shot_suitability(scene, clip)
    total = (
        semantic * weights.semantic_match
        + quality * weights.visual_quality
        + suitability * weights.shot_suitability
        - occupancy * weights.occupancy_penalty
    )
    score = max(0.0, min(1.0, total))
    source_label = (
        "user-provided media" if asset.source_kind == SourceKind.USER_ASSET else
        "historical creator media" if asset.source_kind == SourceKind.HISTORICAL_ASSET else
        "licensed external media" if asset.source_kind == SourceKind.STOCK else
        "admitted generated TalkingRun"
    )
    retrieval_label = {"embedding_similarity":"semantic match","lexical_overlap":"lexical text overlap",
        "planner_selection":"explicit planner source choice (not visual QA)"}[score_basis]
    why = [
        f"{retrieval_label} {semantic:.2f}", f"visual quality {quality:.2f}",
        f"shot suitability {suitability:.2f}", f"source occupancy {occupancy:.2f}", source_label,
    ]
    return score, why


def _shot_suitability(scene: ScenePlan, clip: Clip) -> float:
    intent = scene.visual_intent
    scene_terms = _terms(" ".join(value for value in (intent.subject, intent.action, intent.description) if value))
    clip_terms = _terms(" ".join(value for value in (clip.visual_description, clip.action, clip.shot_type, " ".join(clip.objects)) if value))
    overlap = len(scene_terms & clip_terms) / len(scene_terms) if scene_terms else 0.5
    score = 0.35 + 0.35 * overlap
    if intent.framing:
        framing = intent.framing.lower().strip()
        score += 0.25 if framing in (clip.shot_type or "").lower() else 0.0
    else:
        score += 0.10
    if clip.talking_candidate and any(term in {"creator", "person", "speaker", "talking"} for term in scene_terms):
        score += 0.10
    return max(0.0, min(1.0, score))


def _terms(value: str) -> set[str]:
    return {token for token in re.findall(r"[\w\u4e00-\u9fff]+", value.lower()) if len(token) >= 2}


def _aware_now(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise RoutingInputError("routing clock must return timezone-aware timestamps")
    return value.astimezone(timezone.utc)


def _local_cost(source_kind: SourceKind) -> UsageCost:
    category = (
        CostCategory.USER_ASSET if source_kind == SourceKind.USER_ASSET else
        CostCategory.HISTORICAL_ASSET if source_kind == SourceKind.HISTORICAL_ASSET else
        CostCategory.STOCK if source_kind == SourceKind.STOCK else
        CostCategory.AI_VIDEO if source_kind == SourceKind.AI_BROLL else
        CostCategory.TALKING
    )
    return UsageCost(category=category, amount=Decimal("0"), currency="USD", note="Local continuous Clip reuse; no provider cost.")


def _capture_gap(scene: ScenePlan, *, recommended: bool) -> CandidateAsset:
    intent = scene.visual_intent
    shoot_instruction = "Capture"
    if intent.subject:
        shoot_instruction += f" {intent.subject}"
    if intent.action:
        shoot_instruction += f" {intent.action}"
    if intent.framing:
        shoot_instruction += f" in a {intent.framing} framing"
    shoot_instruction += f" for at least {scene.duration_target_ms / 1000:g} seconds."
    return CandidateAsset(
        scene_plan_id=scene.id,
        source_kind=SourceKind.CAPTURE,
        match_score=0.0,
        why=["No local continuous Clip met the configured match threshold.", shoot_instruction, "Capture gap only; no media is generated."],
        recommended=recommended,
        requires_capture=True,
        estimated_cost=UsageCost(category=CostCategory.CAPTURE, note="No provider cost; requires creator capture."),
    )


def _typography_fallback(scene: ScenePlan, *, recommended: bool = False) -> CandidateAsset:
    intent = scene.visual_intent
    detail = intent.description or intent.action or intent.subject or scene.purpose
    return CandidateAsset(
        scene_plan_id=scene.id,
        source_kind=SourceKind.TYPOGRAPHY,
        match_score=0.0,
        why=[
            "No eligible continuous Clip met the configured match threshold.",
            f"Use editable typography to explain: {detail}.",
            "Local fallback only; no external image or video is generated.",
        ],
        recommended=recommended,
        requires_capture=False,
        estimated_cost=UsageCost(
            category=CostCategory.TYPOGRAPHY,
            amount=Decimal("0"),
            currency="USD",
            note="Local editable typography; no provider cost.",
        ),
    )


def _static_fallbacks(
    scene: ScenePlan,
    requested_sources: set[SourceKind],
    images: ImageAssetRepository | None,
    *,
    recommended: bool,
) -> list[CandidateAsset]:
    if images is None:
        return []
    values: list[CandidateAsset] = []
    for image in images.list():
        if image.source_kind not in requested_sources:
            continue
        category = CostCategory.SCREENSHOT if image.source_kind == SourceKind.SCREENSHOT else CostCategory.CHART
        values.append(CandidateAsset(
            scene_plan_id=scene.id,
            source_kind=image.source_kind,
            asset_id=image.id,
            match_score=0.0,
            why=[
                "No eligible continuous Clip met the configured match threshold.",
                "Use a user-imported static visual as an explicit fallback.",
                "Static visual is not a semantic match score; review before export.",
            ],
            recommended=recommended and not values,
            estimated_cost=UsageCost(category=category, amount=Decimal("0"), currency="USD", note="Local static visual; no provider cost."),
        ))
        if len(values) >= 3:
            break
    return values
