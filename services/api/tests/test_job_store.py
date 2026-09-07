from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

import pytest

from app.db import Database
from app.domain.models import Job, JobStatus, JobType
from app.jobs import JobStore


NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
MAX_ATTEMPTS = 2


def make_job(key: str = "job-key") -> Job:
    return Job(type=JobType.ANALYZE_ASSET, idempotency_key=key, created_at=NOW, updated_at=NOW)


def test_concurrent_enqueue_returns_one_persisted_job(tmp_path: Path) -> None:
    path = tmp_path / "jobs.sqlite"
    workers = 8
    barrier = Barrier(workers)
    Database(path).close()  # Apply migrations before the contending connections open.

    def enqueue_contender(_: int) -> str:
        db = Database(path)
        try:
            barrier.wait()
            return str(JobStore(db).enqueue(make_job("same-key")).id)
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        job_ids = list(pool.map(enqueue_contender, range(workers)))

    reopened = Database(path)
    try:
        assert len(set(job_ids)) == 1
        assert len(JobStore(reopened).jobs.list()) == 1
    finally:
        reopened.close()


def test_concurrent_workers_claim_only_once(tmp_path: Path) -> None:
    path = tmp_path / "jobs.sqlite"
    workers = 8
    barrier = Barrier(workers)
    db = Database(path)
    JobStore(db).enqueue(make_job())
    db.close()

    def claim_contender(index: int) -> str | None:
        contender_db = Database(path)
        try:
            barrier.wait()
            claimed = JobStore(contender_db).claim(
                f"worker-{index}", timedelta(minutes=1), max_attempts=MAX_ATTEMPTS, now=NOW,
            )
            return None if claimed is None else str(claimed.id)
        finally:
            contender_db.close()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        claims = list(pool.map(claim_contender, range(workers)))

    assert len([claim for claim in claims if claim is not None]) == 1


def test_live_lease_cannot_be_reclaimed(tmp_path: Path) -> None:
    db = Database(tmp_path / "jobs.sqlite")
    store = JobStore(db)
    store.enqueue(make_job())
    claimed = store.claim("worker-one", timedelta(minutes=1), max_attempts=MAX_ATTEMPTS, now=NOW)

    assert claimed is not None
    assert store.claim("worker-two", timedelta(minutes=1), max_attempts=MAX_ATTEMPTS, now=NOW + timedelta(seconds=1)) is None
    db.close()


def test_expired_running_job_recovers_after_reopen_and_reclaims(tmp_path: Path) -> None:
    path = tmp_path / "jobs.sqlite"
    db = Database(path)
    store = JobStore(db)
    original = store.enqueue(make_job())
    assert store.claim("crashed-worker", timedelta(seconds=5), max_attempts=MAX_ATTEMPTS, now=NOW) is not None
    db.close()

    reopened = Database(path)
    try:
        store = JobStore(reopened)
        # Claim performs safe crash recovery directly; a separate process does
        # not need in-memory state from the worker that lost its lease.
        reclaimed = store.claim("recovery-worker", timedelta(minutes=1), max_attempts=MAX_ATTEMPTS, now=NOW + timedelta(seconds=6))
        assert reclaimed is not None
        assert reclaimed.id == original.id
        assert reclaimed.attempt == 2
    finally:
        reopened.close()


def test_explicit_recovery_retries_or_terminalizes_by_attempt_budget(tmp_path: Path) -> None:
    path = tmp_path / "jobs.sqlite"
    db = Database(path)
    store = JobStore(db)
    retryable = store.enqueue(make_job("retryable-expired"))
    assert store.claim("retry-worker", timedelta(seconds=5), max_attempts=MAX_ATTEMPTS, now=NOW) is not None
    exhausted = store.enqueue(make_job("exhausted-expired").model_copy(update={"attempt": 1}))
    assert store.claim("exhaust-worker", timedelta(seconds=5), max_attempts=MAX_ATTEMPTS, now=NOW) is not None

    recovered = store.recover_expired(max_attempts=MAX_ATTEMPTS, now=NOW + timedelta(seconds=6))
    results = {job.id: job for job in recovered}
    assert results[retryable.id].status is JobStatus.PENDING
    assert results[retryable.id].attempt == 1
    assert results[exhausted.id].status is JobStatus.FAILED
    assert results[exhausted.id].attempt == MAX_ATTEMPTS
    assert results[exhausted.id].error_code == "attempt_budget_exhausted"
    db.close()

    reopened = Database(path)
    try:
        persisted = JobStore(reopened).get(exhausted.id)
        assert persisted is not None
        assert persisted.status is JobStatus.FAILED
        assert persisted.error_code == "attempt_budget_exhausted"
        assert "2 attempts" in persisted.error_message  # type: ignore[operator]
    finally:
        reopened.close()


def test_pending_job_at_attempt_budget_terminalizes_without_increment(tmp_path: Path) -> None:
    db = Database(tmp_path / "jobs.sqlite")
    store = JobStore(db)
    job = store.enqueue(make_job("already-exhausted").model_copy(update={"attempt": MAX_ATTEMPTS}))

    assert store.claim("worker", timedelta(minutes=1), max_attempts=MAX_ATTEMPTS, now=NOW) is None
    terminal = store.get(job.id)
    assert terminal is not None
    assert terminal.status is JobStatus.FAILED
    assert terminal.attempt == MAX_ATTEMPTS
    assert terminal.error_code == "attempt_budget_exhausted"
    db.close()


def test_heartbeat_extends_ownership(tmp_path: Path) -> None:
    db = Database(tmp_path / "jobs.sqlite")
    store = JobStore(db)
    job = store.enqueue(make_job())
    assert store.claim("worker-one", timedelta(seconds=10), max_attempts=MAX_ATTEMPTS, now=NOW) is not None

    heartbeat = store.heartbeat(job.id, "worker-one", timedelta(seconds=10), now=NOW + timedelta(seconds=9))
    assert heartbeat is not None
    assert heartbeat.updated_at == NOW + timedelta(seconds=9)
    assert store.claim("worker-two", timedelta(seconds=10), max_attempts=MAX_ATTEMPTS, now=NOW + timedelta(seconds=11)) is None
    db.close()


def test_wrong_worker_cannot_complete_or_fail_another_workers_job(tmp_path: Path) -> None:
    db = Database(tmp_path / "jobs.sqlite")
    store = JobStore(db)
    job = store.enqueue(make_job())
    assert store.claim("owner", timedelta(minutes=1), max_attempts=MAX_ATTEMPTS, now=NOW) is not None

    assert store.complete(job.id, "other", now=NOW + timedelta(seconds=1)) is None
    assert store.fail(job.id, "other", "worker_error", "not owner", max_attempts=2, now=NOW + timedelta(seconds=1)) is None
    assert store.fail_terminal(job.id, "other", "worker_error", "not owner", now=NOW + timedelta(seconds=1)) is None
    assert store.complete(job.id, "owner", now=NOW + timedelta(seconds=1)) is not None
    assert store.get(job.id).status is JobStatus.COMPLETED  # type: ignore[union-attr]
    db.close()


def test_failure_retries_until_attempt_budget_then_persists_after_reopen(tmp_path: Path) -> None:
    path = tmp_path / "jobs.sqlite"
    db = Database(path)
    store = JobStore(db)
    job = store.enqueue(make_job())
    first = store.claim("worker", timedelta(minutes=1), max_attempts=MAX_ATTEMPTS, now=NOW)
    assert first is not None and first.attempt == 1
    retry = store.fail(job.id, "worker", "temporary", "try again", max_attempts=2, now=NOW + timedelta(seconds=1))
    assert retry is not None
    assert retry.status is JobStatus.PENDING
    assert retry.error_code == "temporary"
    second = store.claim("worker", timedelta(minutes=1), max_attempts=MAX_ATTEMPTS, now=NOW + timedelta(seconds=2))
    assert second is not None and second.attempt == 2
    terminal = store.fail(job.id, "worker", "permanent", "cannot continue", max_attempts=2, now=NOW + timedelta(seconds=3))
    assert terminal is not None
    assert terminal.status is JobStatus.FAILED
    db.close()

    reopened = Database(path)
    try:
        persisted = JobStore(reopened).get(job.id)
        assert persisted is not None
        assert persisted.status is JobStatus.FAILED
        assert persisted.attempt == 2
        assert persisted.error_code == "permanent"
        assert persisted.error_message == "cannot continue"
        row = reopened.connection.execute(
            "SELECT status, attempt, error_code, error_message, payload FROM jobs WHERE id = ?", (str(job.id),)
        ).fetchone()
        assert tuple(row[:4]) == ("failed", 2, "permanent", "cannot continue")
        assert '"updated_at":"2026-09-07T12:00:03.000000Z"' in row["payload"]
    finally:
        reopened.close()


def test_naive_timestamps_and_invalid_leases_are_rejected(tmp_path: Path) -> None:
    db = Database(tmp_path / "jobs.sqlite")
    store = JobStore(db)
    store.enqueue(make_job())
    with pytest.raises(ValueError, match="timezone-aware"):
        store.enqueue(make_job("naive-job").model_copy(update={"updated_at": datetime(2026, 9, 7, 12, 0)}))
    with pytest.raises(ValueError, match="timezone-aware"):
        store.claim("worker", timedelta(seconds=1), max_attempts=MAX_ATTEMPTS, now=datetime(2026, 9, 7, 12, 0))
    with pytest.raises(ValueError, match="positive timedelta"):
        store.claim("worker", timedelta(0), max_attempts=MAX_ATTEMPTS, now=NOW)
    db.close()
