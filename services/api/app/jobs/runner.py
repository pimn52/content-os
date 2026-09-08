"""Synchronous orchestration for lease-based local jobs.

Handlers deliberately run after ``JobStore.claim`` has committed, so media or
provider work never holds a SQLite write transaction.  This module does not
poll or start a background thread; an application chooses when to call it.
"""
from __future__ import annotations

from datetime import timedelta
from threading import Event, Thread
from typing import Callable, Collection, Mapping, Protocol
from uuid import UUID

from app.db.database import Database
from app.domain.models import Job, JobType

from .store import JobStore


class JobHandler(Protocol):
    """A synchronous, side-effecting handler for one claimed job."""

    def __call__(self, job: Job) -> object: ...


class JobExecutionError(Exception):
    """A handler's deliberately safe, classified failure.

    ``message`` is persisted for the user, so a handler must supply a
    credential-free explanation rather than a raw provider exception.
    """

    def __init__(self, code: str, message: str, *, retryable: bool = True) -> None:
        if not isinstance(code, str) or not code.strip() or len(code) > 100:
            raise ValueError("error code must be a non-empty string of at most 100 characters")
        if not isinstance(message, str) or not message.strip() or len(message) > 2_000:
            raise ValueError("error message must be a non-empty string of at most 2000 characters")
        if not isinstance(retryable, bool):
            raise ValueError("retryable must be a boolean")
        self.code = code.strip()
        self.message = message.strip()
        self.retryable = retryable
        super().__init__(self.message)


class NoHandler(JobExecutionError):
    """The runner has no registered implementation for a claimed job type."""

    def __init__(self, job_type: JobType) -> None:
        super().__init__("no_handler", f"no handler is registered for job type {job_type.value}", retryable=False)


class LeaseLost(RuntimeError):
    """A handler finished after its lease was transitioned or expired."""


HeartbeatStoreFactory = Callable[[], JobStore]


class _LeaseHeartbeat:
    """Renew a lease from a dedicated thread-owned SQLite connection."""

    def __init__(
        self,
        job_id: UUID,
        worker_id: str,
        lease_duration: timedelta,
        interval: timedelta,
        store_factory: HeartbeatStoreFactory,
        main_connection: object,
    ) -> None:
        self._job_id = job_id
        self._worker_id = worker_id
        self._lease_duration = lease_duration
        self._interval_seconds = interval.total_seconds()
        self._store_factory = store_factory
        self._main_connection = main_connection
        self._stop = Event()
        self._lost = Event()
        self._failed = Event()
        self._thread = Thread(target=self._run, name=f"content-os-job-heartbeat-{job_id}", daemon=False)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()

    def ensure_healthy(self) -> None:
        if self._lost.is_set():
            raise LeaseLost("job lease was lost during handler execution")
        if self._failed.is_set():
            raise LeaseLost("job heartbeat failed during handler execution")

    def _run(self) -> None:
        store: JobStore | None = None
        try:
            store = self._store_factory()
            if not isinstance(store, JobStore) or store.db.connection is self._main_connection:
                self._failed.set()
                return
            while not self._stop.wait(self._interval_seconds):
                renewed = store.heartbeat(self._job_id, self._worker_id, self._lease_duration)
                if renewed is None:
                    self._lost.set()
                    return
        except BaseException:
            # This thread cannot safely propagate details (which could include
            # provider/database paths) into persisted job state.  The runner
            # will only surface the fixed LeaseLost diagnostic after joining.
            self._failed.set()
        finally:
            if store is not None:
                try:
                    store.db.close()
                except Exception:
                    self._failed.set()


class JobRunner:
    """Claim and execute at most one persisted job per :meth:`run_once`."""

    def __init__(
        self,
        store: JobStore,
        handlers: Mapping[JobType, JobHandler | Callable[[Job], object]],
        *,
        worker_id: str,
        lease_duration: timedelta,
        max_attempts: int,
        heartbeat_interval: timedelta | None = None,
        heartbeat_store_factory: HeartbeatStoreFactory | None = None,
    ) -> None:
        if not isinstance(store, JobStore):
            raise TypeError("store must be a JobStore")
        if not isinstance(worker_id, str) or not worker_id.strip() or len(worker_id) > 500:
            raise ValueError("worker_id must be a non-empty string of at most 500 characters")
        if not isinstance(lease_duration, timedelta) or lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be a positive timedelta")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 100:
            raise ValueError("max_attempts must be an integer from 1 to 100")
        if heartbeat_interval is not None:
            if not isinstance(heartbeat_interval, timedelta) or heartbeat_interval <= timedelta(0):
                raise ValueError("heartbeat_interval must be a positive timedelta")
            if heartbeat_interval >= lease_duration:
                raise ValueError("heartbeat_interval must be shorter than lease_duration")
        if heartbeat_store_factory is not None and heartbeat_interval is None:
            raise ValueError("heartbeat_store_factory requires heartbeat_interval")
        if heartbeat_interval is not None and heartbeat_store_factory is None and store.db.path in {":memory:", ""}:
            raise ValueError("heartbeat on :memory: requires an explicit thread-local store factory")
        self._store = store
        self._handlers = dict(handlers)
        self._worker_id = worker_id.strip()
        self._lease_duration = lease_duration
        self._max_attempts = max_attempts
        self._heartbeat_interval = heartbeat_interval
        self._heartbeat_store_factory = heartbeat_store_factory or self._default_heartbeat_store_factory(store)

    @property
    def handler_types(self) -> frozenset[JobType]:
        """The job types this runner can execute without ``NoHandler``."""
        return frozenset(self._handlers)

    def recover_expired(self) -> list[Job]:
        """Make expired leases visible before a polling worker starts."""
        return self._store.recover_expired(max_attempts=self._max_attempts)

    def run_once(self, *, allowed_types: Collection[JobType] | None = None) -> Job | None:
        """Execute one claimed job, or return ``None`` when no work is eligible.

        An ownership failure after the handler returns raises :class:`LeaseLost`
        rather than claiming a completion/failure that was not persisted.
        ``BaseException`` subclasses intentionally propagate unchanged; a
        process interruption leaves its running lease for crash recovery.
        """
        job = self._store.claim(
            self._worker_id,
            self._lease_duration,
            max_attempts=self._max_attempts,
            allowed_types=allowed_types,
        )
        if job is None:
            return None
        handler = self._handlers.get(job.type)
        if handler is None:
            return self._record_failure(job, NoHandler(job.type))
        heartbeat = self._start_heartbeat(job)
        try:
            try:
                handler(job)
            except JobExecutionError as error:
                handler_error: JobExecutionError | None = error
            except Exception:
                # Do not persist an arbitrary exception's text: provider clients
                # and OS errors can include credentials, paths, or response data.
                handler_error = JobExecutionError(
                    "unexpected_handler_error",
                    "job handler failed unexpectedly",
                    retryable=True,
                )
            else:
                handler_error = None
        finally:
            if heartbeat is not None:
                heartbeat.stop()
        if heartbeat is not None:
            heartbeat.ensure_healthy()
        if handler_error is not None:
            return self._record_failure(job, handler_error)
        completed = self._store.complete(job.id, self._worker_id)
        if completed is None:
            raise LeaseLost("job lease was lost before completion could be recorded")
        return completed

    def _start_heartbeat(self, job: Job) -> _LeaseHeartbeat | None:
        if self._heartbeat_interval is None:
            return None
        if self._heartbeat_store_factory is None:
            raise LeaseLost("job heartbeat could not acquire a thread-local store")
        heartbeat = _LeaseHeartbeat(
            job.id,
            self._worker_id,
            self._lease_duration,
            self._heartbeat_interval,
            self._heartbeat_store_factory,
            self._store.db.connection,
        )
        try:
            heartbeat.start()
        except Exception:
            raise LeaseLost("job heartbeat could not start") from None
        return heartbeat

    @staticmethod
    def _default_heartbeat_store_factory(store: JobStore) -> HeartbeatStoreFactory | None:
        if store.db.path in {":memory:", ""}:
            return None
        path = store.db.path
        return lambda: JobStore(Database(path))

    def _record_failure(self, job: Job, error: JobExecutionError) -> Job:
        if error.retryable:
            transitioned = self._store.fail(
                job.id,
                self._worker_id,
                error.code,
                error.message,
                max_attempts=self._max_attempts,
            )
        else:
            transitioned = self._store.fail_terminal(
                job.id,
                self._worker_id,
                error.code,
                error.message,
            )
        if transitioned is None:
            raise LeaseLost("job lease was lost before failure could be recorded")
        return transitioned
