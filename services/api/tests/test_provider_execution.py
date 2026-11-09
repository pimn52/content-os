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


def test_workspace_vision_accounting_obeys_global_budget_and_idempotency(tmp_path):
    from app.budget import BudgetLimitError
    from app.db import ProviderCallRepository
    db = Database(tmp_path / "workspace-calls.sqlite")
    try:
        project = _project(db)
        BudgetPolicyRepository(db).save(BudgetPolicy(max_calls=2, allow_unknown_cost=True,
            updated_at=datetime.now(timezone.utc)))
        ledger = ProviderCallLedger(db)
        def reserve(project_id, key):
            return ledger.reserve_execution(project_id=project_id, idempotency_key=key,
                operation="vision", mode="runtime", provider="test", model="test",
                input_source=key, input_digest=provider_call_input_digest(key),
                estimated_cost=UsageCost(category=CostCategory.LLM))
        first = reserve(None, "workspace-frame")
        assert first.owner and first.record.project_id is None
        assert not reserve(None, "workspace-frame").owner
        ledger.finish(project_id=None, call_id=first.record.id, status="completed")
        assert reserve(project.id, "project-frame").owner
        with pytest.raises(BudgetLimitError):
            reserve(None, "over-limit")
        assert len(ProviderCallRepository(db).list_for_project(None)) == 1
        assert len(ProviderCallRepository(db).list_for_project(project.id)) == 1
    finally:
        db.close()


def test_workspace_accounting_migration_preserves_referenced_calls(tmp_path, monkeypatch):
    import app.db.migrations as migrations
    from app.db import ProviderCallRepository
    path = tmp_path / "upgrade.sqlite"
    complete = migrations._MIGRATIONS
    monkeypatch.setattr(migrations, "_MIGRATIONS", tuple(item for item in complete if item[0] <= 60))
    db = Database(path)
    project = _project(db)
    call = ProviderCallLedger(db).reserve(project_id=project.id, idempotency_key="old-call",
        operation="web_search", mode="runtime", provider="test", model="test", input_source="test",
        estimated_cost=UsageCost(category=CostCategory.LLM, amount=Decimal("0"), currency="USD"))
    db.connection.execute("INSERT INTO external_search_replays(idempotency_key,project_id,input_digest,call_id,created_at,cache_hit) VALUES (?,?,?,?,?,0)",
        ("old-replay", str(project.id), "a" * 64, str(call.id), datetime.now(timezone.utc).isoformat()))
    db.close()
    monkeypatch.setattr(migrations, "_MIGRATIONS", complete)
    db = Database(path)
    try:
        assert ProviderCallRepository(db).get(call.id) == call
        assert db.connection.execute("SELECT call_id FROM external_search_replays").fetchone()[0] == str(call.id)
        assert db.connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        db.close()


def test_workspace_vision_job_is_metered_and_settled(tmp_path):
    from uuid import uuid4
    from app.db import ProviderCallRepository
    from app.domain.models import Job, JobStatus, JobType
    from app.jobs.handlers import _MeteredVisionProvider
    from app.jobs.runner import JobExecutionError
    db = Database(tmp_path / 'workspace-job.sqlite')
    try:
        BudgetPolicyRepository(db).save(BudgetPolicy(max_calls=1, allow_unknown_cost=True,
            updated_at=datetime.now(timezone.utc)))
        job = Job(type=JobType.INDEX_CLIPS, status=JobStatus.RUNNING, idempotency_key='intake',
            created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc))
        class Provider:
            def analyze(self, path):
                return 'observed frame'
        frame = tmp_path / 'frame.jpg'; frame.write_bytes(b'frame')
        second = tmp_path / 'second.jpg'; second.write_bytes(b'second')
        vision = _MeteredVisionProvider(Provider(), ProviderCallLedger(db), job, uuid4(), 'test', 'test', 'a' * 64)
        assert vision.analyze(frame) == 'observed frame'
        records = ProviderCallRepository(db).list_for_project(None)
        assert len(records) == 1 and records[0].status == 'completed'
        with pytest.raises(JobExecutionError, match='budget'):
            vision.analyze(second)
        assert len(ProviderCallRepository(db).list_all()) == 1
    finally:
        db.close()


class Cost:
    change = None
    def estimate(self, **options):
        if self.change: self.change()
        return UsageCost(category=options['category'], amount=Decimal('0'), currency='USD')


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
