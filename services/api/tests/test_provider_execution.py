from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from threading import Barrier

import pytest

from app.budget import ProviderCallLedger, provider_call_input_digest
from app.costs import UnknownProviderCostEstimator
from app.db import BudgetPolicyRepository, Database, IPProfileRepository, ProjectRepository
from app.domain.models import BudgetPolicy, CostCategory, IPProfile, Project, RationalFps, UsageCost
from app.provider_execution import ProviderExecutionInProgress, ProviderExecutionService, RuntimeProviderIdentity


def _project(db: Database) -> Project:
    profile = IPProfile(creator_name="Execution creator")
    IPProfileRepository(db).create(profile)
    project = Project(
        ip_profile_id=profile.id,
        title="Execution boundary",
        topic="retry safety",
        fps=RationalFps(numerator=30, denominator=1),
        created_at=datetime.now(timezone.utc),
    )
    ProjectRepository(db).create(project)
    return project


def test_crashed_provider_execution_is_not_blindly_repeated(tmp_path: Path) -> None:
    db = Database(tmp_path / "crash-recovery.sqlite")
    try:
        project = _project(db)
        with db.transaction():
            BudgetPolicyRepository(db).save(
                BudgetPolicy(project_id=project.id, allow_unknown_cost=True, updated_at=datetime.now(timezone.utc))
            )
        service = ProviderExecutionService(db, UnknownProviderCostEstimator())
        identity = RuntimeProviderIdentity("test-runtime", "test-model")

        def crash() -> dict[str, str]:
            raise KeyboardInterrupt("simulated process stop")

        with pytest.raises(KeyboardInterrupt):
            service.execute(
                project_id=project.id,
                idempotency_key="crash-key",
                operation="scene_planning",
                provider=identity,
                input_source=f"project:{project.id}:scene-plan",
                category=CostCategory.LLM,
                input_document={"topic": "one"},
                action=crash,
                encode_result=lambda value: value,
                decode_result=lambda value: dict(value),
                error_code=lambda _: "scene_planner_failed",
            )

        calls: list[str] = []
        with pytest.raises(ProviderExecutionInProgress):
            service.execute(
                project_id=project.id,
                idempotency_key="crash-key",
                operation="scene_planning",
                provider=identity,
                input_source=f"project:{project.id}:scene-plan",
                category=CostCategory.LLM,
                input_document={"topic": "one"},
                action=lambda: calls.append("must-not-run") or {"result": "new"},
                encode_result=lambda value: value,
                decode_result=lambda value: dict(value),
                error_code=lambda _: "scene_planner_failed",
            )
        assert calls == []
        # The durable row is visible through the regular project ledger; it is
        # deliberately still running until an explicit recovery decision.
        from app.db import ProviderCallRepository

        assert ProviderCallRepository(db).list_for_project(project.id)[0].status == "running"
    finally:
        db.close()


def test_concurrent_execution_claims_have_one_owner(tmp_path: Path) -> None:
    path = tmp_path / "concurrent-claim.sqlite"
    db = Database(path)
    try:
        project = _project(db)
    finally:
        db.close()

    barrier = Barrier(2)
    digest = provider_call_input_digest({"topic": "same input"})

    def claim() -> bool:
        worker_db = Database(path)
        try:
            barrier.wait(timeout=5)
            reservation = ProviderCallLedger(worker_db).reserve_execution(
                project_id=project.id,
                idempotency_key="same-concurrent-key",
                operation="scene_planning",
                mode="runtime",
                provider="test-runtime",
                model="test-model",
                input_source=f"project:{project.id}:scene-plan",
                input_digest=digest,
                estimated_cost=UsageCost(category=CostCategory.LLM, amount=Decimal("0"), currency="USD"),
            )
            return reservation.owner
        finally:
            worker_db.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        owners = list(executor.map(lambda _: claim(), range(2)))
    assert owners.count(True) == 1
    assert owners.count(False) == 1
