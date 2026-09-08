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
from typing import Callable, Protocol, Sequence
from uuid import UUID

from app.db import AssetRepository
from app.domain.models import Asset, CandidateAsset, Clip, CostCategory, ProjectFormat, ScenePlan, SourceKind, UsageCost
from app.search import ClipSearchHit


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
    freshness: float = 0.10
    reuse_penalty: float = 0.15
    user_asset_bonus: float = 0.05
    historical_asset_bonus: float = 0.02

    def __post_init__(self) -> None:
        for name, value in self.__dict__.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or not 0 <= float(value) <= 1:
                raise RoutingConfigurationError(f"{name} must be a finite number from 0 to 1")
        if not any(float(getattr(self, name)) for name in ("semantic_match", "visual_quality", "shot_suitability", "freshness")):
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
    """Score real, continuous local Clips and expose a capture-only gap when needed."""

    def __init__(
        self,
        searcher: SceneClipSearcher,
        assets: AssetRepository,
        *,
        weights: RoutingWeights | None = None,
        candidate_pool_size: int = 20,
        max_candidates: int = 3,
        capture_gap_threshold: float = 0.45,
        allowed_source_kinds: Sequence[SourceKind] = (SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET),
        now: Callable[[], datetime] | None = None,
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
        self._now = now or (lambda: datetime.now(timezone.utc))

    def route(self, scene: ScenePlan) -> RoutingResult:
        if not isinstance(scene, ScenePlan):
            raise RoutingInputError("routing requires a ScenePlan contract")
        query = _scene_query(scene)
        # Retrieval errors retain the provider/indexer's own typed semantics.
        hits = self.searcher.search(query, top_k=self.candidate_pool_size)
        if not isinstance(hits, list) or any(not isinstance(hit, ClipSearchHit) for hit in hits):
            raise RoutingInputError("scene searcher returned invalid Clip hits")
        candidates = self._score_hits(scene, hits)
        selected = candidates[: self.max_candidates]
        needs_capture = not selected or selected[0][0] < self.capture_gap_threshold
        values = [candidate for _, candidate in selected]
        if needs_capture:
            values.append(_capture_gap(scene, recommended=not values))
        if values and not needs_capture:
            values[0] = CandidateAsset.model_validate({**values[0].model_dump(mode="python"), "recommended": True})
        elif values and not any(value.recommended for value in values):
            values[-1] = CandidateAsset.model_validate({**values[-1].model_dump(mode="python"), "recommended": True})
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
        return tuple(self.route(scene) for scene in ordered)

    def _score_hits(self, scene: ScenePlan, hits: Sequence[ClipSearchHit]) -> list[tuple[float, CandidateAsset]]:
        seen: set[UUID] = set()
        ranked: list[tuple[float, int, str, int, int, str, CandidateAsset]] = []
        now = _aware_now(self._now())
        requested_sources = set(scene.preferred_sources) | set(scene.fallback_sources)
        for hit in hits:
            clip = hit.clip
            if clip.id in seen:
                continue
            seen.add(clip.id)
            asset = self.assets.get(clip.asset_id)
            if (
                asset is None
                or asset.source_kind not in self.allowed_source_kinds
                or asset.source_kind not in requested_sources
                or clip.end_ms - clip.start_ms < scene.duration_target_ms
            ):
                continue
            score, why = _score(scene, clip, asset, hit.score, self.weights, now)
            candidate = CandidateAsset(
                scene_plan_id=scene.id,
                source_kind=asset.source_kind,
                asset_id=asset.id,
                clip_id=clip.id,
                match_score=score,
                why=why,
                reuse_count=clip.used_count,
                estimated_cost=_local_cost(asset.source_kind),
            )
            source_rank = 0 if asset.source_kind == SourceKind.USER_ASSET else 1
            ranked.append((score, source_rank, str(asset.id), clip.start_ms, clip.end_ms, str(clip.id), candidate))
        ranked.sort(key=lambda value: (-value[0], value[1], value[2], value[3], value[4], value[5]))
        return [(value[0], value[-1]) for value in ranked]


def _scene_query(scene: ScenePlan) -> str:
    intent = scene.visual_intent
    values = (scene.voice_text, intent.subject, intent.action, intent.framing, intent.description, *scene.caption_emphasis)
    query = " ".join(value.strip() for value in values if isinstance(value, str) and value.strip())
    if not query:
        raise RoutingInputError("ScenePlan has no searchable voice or visual intent")
    return query


def _score(scene: ScenePlan, clip: Clip, asset: Asset, raw_semantic: object, weights: RoutingWeights, now: datetime) -> tuple[float, list[str]]:
    if isinstance(raw_semantic, bool) or not isinstance(raw_semantic, (int, float)) or not math.isfinite(float(raw_semantic)):
        raise RoutingInputError("Clip search hit score must be finite")
    # Cosine similarity at or below zero does not constitute a semantic match.
    semantic = max(0.0, min(1.0, float(raw_semantic)))
    quality = 0.5 if clip.quality_score is None else float(clip.quality_score)
    suitability = _shot_suitability(scene, clip)
    freshness = _freshness(clip.last_used_at, now)
    reuse = min(1.0, clip.used_count / 5.0)
    bonus = weights.user_asset_bonus if asset.source_kind == SourceKind.USER_ASSET else weights.historical_asset_bonus
    total = (
        semantic * weights.semantic_match
        + quality * weights.visual_quality
        + suitability * weights.shot_suitability
        + freshness * weights.freshness
        - reuse * weights.reuse_penalty
        + bonus
    )
    score = max(0.0, min(1.0, total))
    source_label = "user-provided media" if asset.source_kind == SourceKind.USER_ASSET else "historical creator media"
    why = [
        f"semantic match {semantic:.2f}", f"visual quality {quality:.2f}",
        f"shot suitability {suitability:.2f}", f"freshness {freshness:.2f}",
        f"reuse penalty {reuse:.2f}", source_label,
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


def _freshness(last_used_at: datetime | None, now: datetime) -> float:
    if last_used_at is None:
        return 1.0
    previous = _aware_now(last_used_at)
    days = max(0.0, (now - previous).total_seconds() / 86_400)
    return min(1.0, days / 30.0)


def _aware_now(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise RoutingInputError("routing clock must return timezone-aware timestamps")
    return value.astimezone(timezone.utc)


def _local_cost(source_kind: SourceKind) -> UsageCost:
    category = CostCategory.USER_ASSET if source_kind == SourceKind.USER_ASSET else CostCategory.HISTORICAL_ASSET
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
