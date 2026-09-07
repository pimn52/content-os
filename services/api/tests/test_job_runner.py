from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.db import Database
from app.domain.models import Job, JobStatus, JobType
from app.jobs import JobExecutionError, JobRunner, JobStore, LeaseLost


def make_job(key: str) -> Job:
    now = datetime.now(timezone.utc)
    return Job(type=JobType.ANALYZE_ASSET, idempotency_key=key, created_at=now, updated_at=now)


def make_runner(store: JobStore, handlers: dict[JobType, object], *, max_attempts: int = 3) -> JobRunner:
    return JobRunner(
        store,
        handlers,  # type: ignore[arg-type]
        worker_id="runner-worker",
        lease_duration=timedelta(minutes=1),
        max_attempts=max_attempts,
    )


def test_runner_completes_one_job_and_returns_none_when_empty(tmp_path: Path) -> None:
    db = Database(tmp_path / "jobs.sqlite")
    store = JobStore(db)
    job = store.enqueue(make_job("complete"))
    seen: list[Job] = []
    runner = make_runner(store, {JobType.ANALYZE_ASSET: seen.append})

    completed = runner.run_once()

    assert completed is not None
    assert completed.id == job.id
    assert completed.status is JobStatus.COMPLETED
    assert len(seen) == 1
    assert seen[0].id == job.id
    assert seen[0].status is JobStatus.RUNNING
    assert runner.run_once() is None
    db.close()


def test_runner_handler_executes_outside_claim_write_transaction(tmp_path: Path) -> None:
    db = Database(tmp_path / "jobs.sqlite")
    store = JobStore(db)
    store.enqueue(make_job("outer-job"))

    def handler(_: Job) -> None:
        # This opens its own SQLite write transaction.  It would fail with
        # "cannot start a transaction within a transaction" if run_once kept
        # claim's BEGIN IMMEDIATE open across handler execution.
        store.enqueue(make_job("created-by-handler"))

    completed = make_runner(store, {JobType.ANALYZE_ASSET: handler}).run_once()
    assert completed is not None and completed.status is JobStatus.COMPLETED
    assert len(store.jobs.list()) == 2
    db.close()


def test_runner_retryable_error_retries_then_completes(tmp_path: Path) -> None:
    db = Database(tmp_path / "jobs.sqlite")
    store = JobStore(db)
    job = store.enqueue(make_job("retry"))
    calls = 0

    def handler(_: Job) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise JobExecutionError("temporary_provider", "provider is temporarily unavailable", retryable=True)

    runner = make_runner(store, {JobType.ANALYZE_ASSET: handler}, max_attempts=2)
    first = runner.run_once()
    assert first is not None
    assert first.status is JobStatus.PENDING
    assert first.error_code == "temporary_provider"

    completed = runner.run_once()
    assert completed is not None
    assert completed.status is JobStatus.COMPLETED
    assert completed.attempt == 2
    assert store.get(job.id).status is JobStatus.COMPLETED  # type: ignore[union-attr]
    db.close()


def test_runner_non_retryable_and_missing_handler_terminally_fail(tmp_path: Path) -> None:
    db = Database(tmp_path / "jobs.sqlite")
    store = JobStore(db)
    terminal = store.enqueue(make_job("terminal"))

    def permanent(_: Job) -> None:
        raise JobExecutionError("invalid_input", "input cannot be processed", retryable=False)

    first = make_runner(store, {JobType.ANALYZE_ASSET: permanent}, max_attempts=3).run_once()
    assert first is not None
    assert first.status is JobStatus.FAILED
    assert first.attempt == 1
    assert first.error_code == "invalid_input"

    missing = store.enqueue(make_job("missing-handler"))
    second = make_runner(store, {}).run_once()
    assert second is not None
    assert second.id == missing.id
    assert second.status is JobStatus.FAILED
    assert second.error_code == "no_handler"
    assert "analyze_asset" in second.error_message  # type: ignore[operator]
    assert store.get(terminal.id).status is JobStatus.FAILED  # type: ignore[union-attr]
    db.close()


def test_runner_unknown_error_persists_only_generic_safe_message(tmp_path: Path) -> None:
    db = Database(tmp_path / "jobs.sqlite")
    store = JobStore(db)
    job = store.enqueue(make_job("unknown-error"))
    secret = "provider-key-that-must-not-persist"

    def handler(_: Job) -> None:
        raise RuntimeError(secret)

    result = make_runner(store, {JobType.ANALYZE_ASSET: handler}, max_attempts=2).run_once()
    assert result is not None
    assert result.status is JobStatus.PENDING
    assert result.error_code == "unexpected_handler_error"
    assert result.error_message == "job handler failed unexpectedly"
    assert secret not in result.error_message
    persisted = store.get(job.id)
    assert persisted is not None
    assert secret not in (persisted.error_message or "")
    db.close()


def test_runner_raises_lease_lost_when_handler_transition_wins(tmp_path: Path) -> None:
    db = Database(tmp_path / "jobs.sqlite")
    store = JobStore(db)
    job = store.enqueue(make_job("lease-lost"))

    def handler(claimed: Job) -> None:
        assert store.complete(claimed.id, "runner-worker") is not None

    with pytest.raises(LeaseLost, match="lease was lost"):
        make_runner(store, {JobType.ANALYZE_ASSET: handler}).run_once()
    assert store.get(job.id).status is JobStatus.COMPLETED  # type: ignore[union-attr]
    db.close()


def test_runner_does_not_catch_process_interrupt_and_expired_lease_recovers(tmp_path: Path) -> None:
    path = tmp_path / "jobs.sqlite"
    db = Database(path)
    store = JobStore(db)
    job = store.enqueue(make_job("interrupted"))

    def interrupted(_: Job) -> None:
        raise KeyboardInterrupt()

    runner = JobRunner(
        store,
        {JobType.ANALYZE_ASSET: interrupted},
        worker_id="runner-worker",
        lease_duration=timedelta(milliseconds=1),
        max_attempts=2,
    )
    with pytest.raises(KeyboardInterrupt):
        runner.run_once()
    assert store.get(job.id).status is JobStatus.RUNNING  # type: ignore[union-attr]
    db.close()

    reopened = Database(path)
    try:
        recovered = JobStore(reopened).recover_expired(
            max_attempts=2,
            now=datetime.now(timezone.utc) + timedelta(seconds=1),
        )
        assert [(item.id, item.status) for item in recovered] == [(job.id, JobStatus.PENDING)]
    finally:
        reopened.close()
