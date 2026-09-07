"""Synchronous orchestration for lease-based local jobs.

Handlers deliberately run after ``JobStore.claim`` has committed, so media or
provider work never holds a SQLite write transaction.  This module does not
poll or start a background thread; an application chooses when to call it.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Callable, Mapping, Protocol

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
    ) -> None:
        if not isinstance(store, JobStore):
            raise TypeError("store must be a JobStore")
        if not isinstance(worker_id, str) or not worker_id.strip() or len(worker_id) > 500:
            raise ValueError("worker_id must be a non-empty string of at most 500 characters")
        if not isinstance(lease_duration, timedelta) or lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be a positive timedelta")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 100:
            raise ValueError("max_attempts must be an integer from 1 to 100")
        self._store = store
        self._handlers = dict(handlers)
        self._worker_id = worker_id.strip()
        self._lease_duration = lease_duration
        self._max_attempts = max_attempts

    def run_once(self) -> Job | None:
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
        )
        if job is None:
            return None
        handler = self._handlers.get(job.type)
        if handler is None:
            return self._record_failure(job, NoHandler(job.type))
        try:
            handler(job)
        except JobExecutionError as error:
            return self._record_failure(job, error)
        except Exception:
            # Do not persist an arbitrary exception's text: provider clients
            # and OS errors can include credentials, paths, or response data.
            return self._record_failure(
                job,
                JobExecutionError(
                    "unexpected_handler_error",
                    "job handler failed unexpectedly",
                    retryable=True,
                ),
            )
        completed = self._store.complete(job.id, self._worker_id)
        if completed is None:
            raise LeaseLost("job lease was lost before completion could be recorded")
        return completed

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
