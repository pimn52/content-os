"""Provider-neutral cost estimation and project-level cost summaries."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Mapping, Protocol, Sequence
from uuid import UUID

from app.domain.models import CandidateAsset, CostEstimate, CostLineItem, CostCategory, UsageCost


CostOperation = Literal["scene_planning", "asr", "vision", "embedding", "tts", "talking", "render"]


class ProviderCostEstimator(Protocol):
    """Provider boundary for a price estimate; it never receives credentials."""

    def estimate(
        self,
        *,
        operation: CostOperation,
        provider: str,
        model: str,
        input_source: str,
        category: CostCategory,
    ) -> UsageCost: ...


@dataclass(frozen=True)
class UnknownProviderCostEstimator:
    """Safe default until a runtime provider supplies an explicit price."""

    note: str = "runtime provider price is not observable; set an explicit estimate before execution"

    def estimate(
        self,
        *,
        operation: CostOperation,
        provider: str,
        model: str,
        input_source: str,
        category: CostCategory,
    ) -> UsageCost:
        del operation, model, input_source
        return UsageCost(category=category, provider=provider, note=self.note)


@dataclass(frozen=True)
class CatalogProviderCostEstimator:
    """Explicit in-process price catalog for a configured provider/model pair.

    Missing entries intentionally fall back to an unknown amount. This keeps
    budget enforcement conservative and lets a real runtime adapter provide a
    catalog without coupling the core to one vendor or currency.
    """

    prices: Mapping[tuple[CostOperation, str, str], UsageCost]
    fallback: ProviderCostEstimator = UnknownProviderCostEstimator()

    def estimate(
        self,
        *,
        operation: CostOperation,
        provider: str,
        model: str,
        input_source: str,
        category: CostCategory,
    ) -> UsageCost:
        value = self.prices.get((operation, provider, model))
        if value is None:
            return self.fallback.estimate(
                operation=operation,
                provider=provider,
                model=model,
                input_source=input_source,
                category=category,
            )
        if value.category != category:
            raise ValueError("provider price catalog category does not match the requested operation")
        return value.model_copy(update={"provider": provider})


def estimate_selected_candidates(
    selections: Sequence[CandidateAsset],
    *,
    required_scene_count: int,
    currency: str = "USD",
    include_local_render: bool = True,
    scene_labels: Mapping[UUID, str] | None = None,
) -> CostEstimate:
    """Aggregate selected scene costs without treating unknown prices as zero."""

    if isinstance(required_scene_count, bool) or not isinstance(required_scene_count, int) or required_scene_count < 0:
        raise ValueError("required_scene_count must be a non-negative integer")
    items: list[CostLineItem] = []
    scene_ids: set[object] = set()
    for candidate in selections:
        if candidate.scene_plan_id in scene_ids:
            raise ValueError("cost estimate selections must be unique per scene")
        scene_ids.add(candidate.scene_plan_id)
        items.append(CostLineItem(
            scope="scene",
            scene_plan_id=candidate.scene_plan_id,
            label=f"{(scene_labels or {}).get(candidate.scene_plan_id, f'scene {candidate.scene_plan_id}')} · {candidate.source_kind.value}",
            cost=candidate.estimated_cost,
        ))
    if include_local_render and len(scene_ids) == required_scene_count and required_scene_count > 0:
        items.append(CostLineItem(
            scope="render",
            label="local render",
            cost=UsageCost(
                category=CostCategory.RENDER,
                amount=Decimal("0"),
                currency=currency,
                note="Local renderer; no provider charge.",
            ),
        ))
    known = Decimal("0")
    unknown = 0
    for item in items:
        if item.cost.amount is None or item.cost.currency != currency:
            unknown += 1
        else:
            known += item.cost.amount
    return CostEstimate(
        currency=currency,
        known_amount=known,
        unknown_cost_count=unknown,
        selected_scene_count=len(scene_ids),
        required_scene_count=required_scene_count,
        line_items=tuple(items),
    )


@dataclass(frozen=True)
class CostReductionDecision:
    current: CandidateAsset
    suggested: CandidateAsset
    changed: bool
    reason: str


def reduce_candidate_cost(
    candidates: Sequence[CandidateAsset],
    *,
    current: CandidateAsset | None = None,
    max_score_drop: float = 0.15,
) -> CostReductionDecision:
    """Prefer a known cheaper candidate without assuming unknown is cheap."""

    if not candidates:
        raise ValueError("cost reduction requires one or more candidates")
    if isinstance(max_score_drop, bool) or not isinstance(max_score_drop, (int, float)) or not 0 <= max_score_drop <= 1:
        raise ValueError("max_score_drop must be a number from 0 to 1")
    selected = current or next((candidate for candidate in candidates if candidate.recommended), candidates[0])
    if selected not in candidates:
        raise ValueError("current candidate must belong to candidates")
    baseline_amount = selected.estimated_cost.amount
    baseline_currency = selected.estimated_cost.currency
    alternatives: list[CandidateAsset] = []
    for candidate in candidates:
        if candidate == selected:
            continue
        amount = candidate.estimated_cost.amount
        currency = candidate.estimated_cost.currency
        if amount is None or currency is None:
            # Unknown is not a cost reduction claim.
            continue
        if candidate.match_score < selected.match_score - float(max_score_drop):
            continue
        if baseline_amount is not None:
            if baseline_currency != currency or amount >= baseline_amount:
                continue
        alternatives.append(candidate)
    if not alternatives:
        return CostReductionDecision(
            current=selected,
            suggested=selected,
            changed=False,
            reason="没有满足匹配度阈值且价格已知更低的候选；未知价格未被视为更便宜。",
        )
    suggested = min(
        alternatives,
        key=lambda candidate: (
            candidate.estimated_cost.amount,
            -candidate.match_score,
            candidate.source_kind.value,
            str(candidate.asset_id or ""),
            str(candidate.clip_id or ""),
        ),
    )
    return CostReductionDecision(
        current=selected,
        suggested=suggested,
        changed=True,
        reason=f"已知价格更低，且匹配度下降不超过 {float(max_score_drop):.0%}；未把未知价格当作 0。",
    )
