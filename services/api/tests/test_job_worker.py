from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Thread
from typing import Callable

from app.db import Database
from app.domain.models import Job, JobStatus, JobType
from app.jobs import JobRunner, JobStore, JobWorker


def _job(kind: JobType, key: str, *, timestamp: datetime | None = None) -> Job:
    now = datetime.now(timezone.utc) if timestamp is None else timestamp
    return Job(type=kind, idempotency_key=key, created_at=now, updated_at=now)


def _run_worker_in_thread(
    path: Path,
    stop: Event,
    handlers: dict[JobType, Callable[[Job], object]],
    errors: list[BaseException],
    *,
    started: Event | None = None,
) -> Thread:
    def target() -> None:
        db = Database(path)
        try:
            runner = JobRunner(
                JobStore(db), handlers, worker_id="worker", lease_duration=timedelta(seconds=1), max_attempts=2
            )
            if started is not None:
                started.set()
            JobWorker(runner, idle_interval=timedelta(seconds=30)).run_forever(stop)
        except BaseException as exc:
            errors.append(exc)
        finally:
            db.close()

    thread = Thread(target=target, name="test-job-worker")
    thread.start()
    return thread


def test_worker_idle_wait_is_interruptible(tmp_path: Path) -> None:
    path = tmp_path / "idle.sqlite"
    Database(path).close()
    stop = Event()
    started = Event()
    errors: list[BaseException] = []
    thread = _run_worker_in_thread(path, stop, {}, errors, started=started)

    assert started.wait(timeout=2)
    stop.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert errors == []


def test_worker_processes_multiple_jobs(tmp_path: Path) -> None:
    path = tmp_path / "multiple.sqlite"
    seed = Database(path)
    try:
        store = JobStore(seed)
        jobs = [store.enqueue(_job(JobType.ANALYZE_ASSET, f"multiple-{index}")) for index in range(2)]
    finally:
        seed.close()
    stop = Event()
    errors: list[BaseException] = []
    seen: list[str] = []

    def handler(job: Job) -> None:
        seen.append(str(job.id))
        if len(seen) == 2:
            stop.set()

    thread = _run_worker_in_thread(path, stop, {JobType.ANALYZE_ASSET: handler}, errors)
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert errors == []
    assert set(seen) == {str(job.id) for job in jobs}
    check = Database(path)
    try:
        assert [JobStore(check).get(job.id).status for job in jobs] == [JobStatus.COMPLETED, JobStatus.COMPLETED]  # type: ignore[union-attr]
    finally:
        check.close()


def test_worker_recovers_expired_job_at_startup(tmp_path: Path) -> None:
    path = tmp_path / "recovery.sqlite"
    timestamp = datetime(2020, 1, 1, tzinfo=timezone.utc)
    seed = Database(path)
    try:
        store = JobStore(seed)
        job = store.enqueue(_job(JobType.ANALYZE_ASSET, "expired", timestamp=timestamp))
        assert store.claim("crashed", timedelta(seconds=1), max_attempts=2, now=timestamp) is not None
    finally:
        seed.close()
    stop = Event()
    errors: list[BaseException] = []

    def handler(_: Job) -> None:
        stop.set()

    thread = _run_worker_in_thread(path, stop, {JobType.ANALYZE_ASSET: handler}, errors)
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert errors == []
    check = Database(path)
    try:
        persisted = JobStore(check).get(job.id)
        assert persisted is not None
        assert persisted.status is JobStatus.COMPLETED
        assert persisted.attempt == 2
    finally:
        check.close()


def test_worker_only_claims_registered_handler_types(tmp_path: Path) -> None:
    path = tmp_path / "partial.sqlite"
    seed = Database(path)
    try:
        store = JobStore(seed)
        analysis = store.enqueue(_job(JobType.ANALYZE_ASSET, "analysis"))
        transcription = store.enqueue(_job(JobType.TRANSCRIBE_AUDIO, "transcription"))
    finally:
        seed.close()
    stop = Event()
    errors: list[BaseException] = []

    def handler(_: Job) -> None:
        stop.set()

    thread = _run_worker_in_thread(path, stop, {JobType.ANALYZE_ASSET: handler}, errors)
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert errors == []
    check = Database(path)
    try:
        store = JobStore(check)
        assert store.get(analysis.id).status is JobStatus.COMPLETED  # type: ignore[union-attr]
        untouched = store.get(transcription.id)
        assert untouched is not None
        assert untouched.status is JobStatus.PENDING
        assert untouched.attempt == 0
    finally:
        check.close()


def test_worker_stop_waits_for_running_handler_to_finish(tmp_path: Path) -> None:
    path = tmp_path / "graceful.sqlite"
    seed = Database(path)
    try:
        job = JobStore(seed).enqueue(_job(JobType.ANALYZE_ASSET, "long"))
    finally:
        seed.close()
    stop = Event()
    handler_started = Event()
    release_handler = Event()
    errors: list[BaseException] = []

    def handler(_: Job) -> None:
        handler_started.set()
        assert release_handler.wait(timeout=2)

    thread = _run_worker_in_thread(path, stop, {JobType.ANALYZE_ASSET: handler}, errors)
    assert handler_started.wait(timeout=2)
    stop.set()
    thread.join(timeout=0.1)
    assert thread.is_alive()
    release_handler.set()
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert errors == []
    check = Database(path)
    try:
        assert JobStore(check).get(job.id).status is JobStatus.COMPLETED  # type: ignore[union-attr]
    finally:
        check.close()
