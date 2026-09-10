from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.costs import CatalogProviderCostEstimator, estimate_selected_candidates, reduce_candidate_cost
from app.db import Database, IPProfileRepository, ProjectRepository
from app.domain.models import CandidateAsset, CostCategory, DraftRoute, IPProfile, Project, RationalFps, ScenePlan, SourceKind, UsageCost, VisualIntent
from app.main import create_app


def _candidate(scene: ScenePlan, cost: UsageCost) -> CandidateAsset:
    return CandidateAsset(
        scene_plan_id=scene.id,
        source_kind=SourceKind.USER_ASSET if cost.category == CostCategory.USER_ASSET else SourceKind.CAPTURE,
        asset_id=uuid4() if cost.category == CostCategory.USER_ASSET else None,
        clip_id=uuid4() if cost.category == CostCategory.USER_ASSET else None,
        match_score=0.8,
        why=["test candidate"],
        recommended=True,
        requires_capture=cost.category == CostCategory.CAPTURE,
        estimated_cost=cost,
    )


def test_catalog_provider_cost_estimator_keeps_missing_prices_unknown() -> None:
    known = UsageCost(category=CostCategory.LLM, amount=Decimal("0.0123"), currency="USD", provider="kimi")
    estimator = CatalogProviderCostEstimator({("scene_planning", "kimi", "k3"): known})

    result = estimator.estimate(
        operation="scene_planning", provider="kimi", model="k3", input_source="project:test", category=CostCategory.LLM,
    )
    missing = estimator.estimate(
        operation="asr", provider="kimi", model="k3", input_source="asset:test", category=CostCategory.ASR,
    )

    assert result.amount == Decimal("0.0123")
    assert result.provider == "kimi"
    assert missing.amount is None and missing.currency is None


def test_selected_candidate_cost_summary_is_scene_complete_and_explicit() -> None:
    scene = ScenePlan(
        project_id=uuid4(), scene_id="scene-1", order=0, purpose="hook", voice_text="观点",
        duration_target_ms=3_000, visual_intent=VisualIntent(subject="creator"),
        preferred_sources=[SourceKind.USER_ASSET],
    )
    local = _candidate(scene, UsageCost(category=CostCategory.USER_ASSET, amount=Decimal("0"), currency="USD"))
    summary = estimate_selected_candidates([local], required_scene_count=1)

    assert summary.known_amount == Decimal("0")
    assert summary.unknown_cost_count == 0
    assert summary.selected_scene_count == summary.required_scene_count == 1
    assert [item.scope for item in summary.line_items] == ["scene", "render"]


def test_cost_reduction_prefers_known_cheaper_candidate_within_score_tolerance() -> None:
    scene_id = uuid4()
    expensive = CandidateAsset(
        scene_plan_id=scene_id, source_kind=SourceKind.AI_VIDEO, match_score=0.9, why=["expensive"],
        recommended=True, estimated_cost=UsageCost(category=CostCategory.AI_VIDEO, amount=Decimal("1.00"), currency="USD"),
    )
    cheaper = expensive.model_copy(update={
        "source_kind": SourceKind.TYPOGRAPHY, "match_score": 0.76, "recommended": False,
        "estimated_cost": UsageCost(category=CostCategory.TYPOGRAPHY, amount=Decimal("0.10"), currency="USD"),
    })
    unknown = expensive.model_copy(update={
        "source_kind": SourceKind.CAPTURE, "match_score": 0.89, "recommended": False,
        "requires_capture": True, "estimated_cost": UsageCost(category=CostCategory.CAPTURE),
    })

    decision = reduce_candidate_cost([expensive, cheaper, unknown])

    assert decision.changed is True
    assert decision.suggested.source_kind == SourceKind.TYPOGRAPHY
    assert "未把未知价格当作 0" in decision.reason


def test_project_cost_estimate_does_not_zero_unknown_capture_cost(tmp_path: Path) -> None:
    path = tmp_path / "cost-estimate.sqlite3"
    db = Database(path)
    profile = IPProfileRepository(db).create(IPProfile(creator_name="Creator"))
    project = Project(
        ip_profile_id=profile.id, title="Cost estimate", topic="Capture gap",
        fps=RationalFps(numerator=30, denominator=1), created_at=datetime.now(timezone.utc),
    )
    ProjectRepository(db).create(project)
    db.close()
    scene = ScenePlan(
        project_id=project.id, scene_id="scene-1", order=0, purpose="gap", voice_text="需要补拍",
        duration_target_ms=3_000, visual_intent=VisualIntent(subject="screen"),
        preferred_sources=[SourceKind.CAPTURE],
    )
    capture = _candidate(scene, UsageCost(category=CostCategory.CAPTURE, note="requires creator capture"))

    with TestClient(create_app(path)) as client:
        saved = client.put(
            f"/projects/{project.id}/draft",
            json={
                "scenes": [scene.model_dump(mode="json")],
                "routes": [DraftRoute(scene_plan_id=scene.id, candidates=(capture,)).model_dump(mode="json")],
                "confirmed": [capture.model_dump(mode="json")],
            },
        )
        estimate = client.get(f"/projects/{project.id}/cost-estimate")
        reduction = client.get(f"/projects/{project.id}/cost-reduction")

    assert saved.status_code == 200
    assert estimate.status_code == 200
    body = estimate.json()
    assert body["selected_scene_count"] == body["required_scene_count"] == 1
    assert body["known_amount"] == "0"
    assert body["unknown_cost_count"] == 1
    assert body["line_items"][0]["cost"]["amount"] is None
    assert reduction.status_code == 200
    assert reduction.json()["changed_scene_count"] == 0
    assert reduction.json()["suggestions"][0]["changed"] is False
