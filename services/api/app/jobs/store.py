"""SQLite-backed, lease-based state transitions for local jobs."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Collection, Iterator
from uuid import UUID

from app.db.database import Database
from app.db.repositories import JobRepository, _job_payload, _model, _utc_timestamp
from app.domain.models import Job, JobStatus, JobType


class JobStore:
    """Persist and claim jobs safely across independent SQLite connections.

    ``BEGIN IMMEDIATE`` serializes local writers.  SQLite's UNIQUE constraint
    remains the source of truth for enqueue idempotency, while lease columns
    are kept outside the unchanged ``Job`` contract.
    """

    def __init__(self, db: Database) -> None:
        self.db = db
        self.jobs = JobRepository(db)
        # This setting is per connection, including connections that only use
        # claim/transition methods rather than JobRepository.create().
        self.db.connection.execute("PRAGMA busy_timeout = 5000")

    def enqueue(self, job: Job) -> Job:
        """Create ``job`` once, or return the existing idempotency match."""
        return self.jobs.create(job)

    def get(self, job_id: UUID) -> Job | None:
        return self.jobs.get(job_id)

    def claim(
        self,
        worker_id: str,
        lease_duration: timedelta,
        *,
        max_attempts: int,
        allowed_types: Collection[JobType] | None = None,
        now: datetime | None = None,
    ) -> Job | None:
        """Atomically claim one pending or expired-running job.

        The transaction prevents two processes from observing and claiming the
        same row.  An expired lease is intentionally eligible for direct
        reclaim, which is the crash-recovery path after reopening a database.
        """
        worker = _valid_worker(worker_id)
        maximum = _valid_max_attempts(max_attempts)
        allowed = _valid_allowed_types(allowed_types)
        if allowed is not None and not allowed:
            return None
        timestamp, expiry = _lease_times(now, lease_duration)
        timestamp_text, expiry_text = _utc_timestamp(timestamp), _utc_timestamp(expiry)
        with self._write_transaction():
            while True:
                type_clause = "" if allowed is None else f" AND json_extract(payload, '$.type') IN ({','.join('?' for _ in allowed)})"
                row = self.db.connection.execute(
                    f"""SELECT * FROM jobs
                       WHERE (status = ?
                          OR (status = ? AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?)){type_clause}
                       ORDER BY CASE status WHEN ? THEN 0 ELSE 1 END, created_at, id
                       LIMIT 1""",
                    (
                        JobStatus.PENDING.value,
                        JobStatus.RUNNING.value,
                        timestamp_text,
                        *(item.value for item in allowed or ()),
                        JobStatus.PENDING.value,
                    ),
                ).fetchone()
                if row is None:
                    return None
                current = _model(row, Job)
                if current.attempt >= maximum:
                    self._fail_exhausted(current, maximum, timestamp, timestamp_text)
                    continue
                claimed = current.model_copy(update={
                    "status": JobStatus.RUNNING,
                    "attempt": current.attempt + 1,
                    "updated_at": timestamp,
                    "error_code": None,
                    "error_message": None,
                })
                cursor = self.db.connection.execute(
                    """UPDATE jobs
                       SET status = ?, attempt = ?, updated_at = ?, error_code = NULL,
                           error_message = NULL, lease_owner = ?, lease_expires_at = ?, payload = ?
                       WHERE id = ?
                         AND (status = ? OR (status = ? AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?))""",
                    (
                        JobStatus.RUNNING.value, claimed.attempt, timestamp_text, worker, expiry_text, _job_payload(claimed),
                        str(claimed.id), JobStatus.PENDING.value, JobStatus.RUNNING.value, timestamp_text,
                    ),
                )
                if cursor.rowcount != 1:
                    # This should only be possible if this connection is used
                    # outside JobStore's transaction discipline.
                    return None
                return claimed

    def claim_next(
        self,
        worker_id: str,
        lease_duration: timedelta,
        *,
        max_attempts: int,
        allowed_types: Collection[JobType] | None = None,
        now: datetime | None = None,
    ) -> Job | None:
        """Compatibility-friendly name for a runner's next-job operation."""
        return self.claim(worker_id, lease_duration, max_attempts=max_attempts, allowed_types=allowed_types, now=now)

    def heartbeat(
        self,
        job_id: UUID,
        worker_id: str,
        lease_duration: timedelta,
        *,
        now: datetime | None = None,
    ) -> Job | None:
        """Extend an unexpired lease held by ``worker_id``."""
        worker = _valid_worker(worker_id)
        timestamp, expiry = _lease_times(now, lease_duration)
        timestamp_text, expiry_text = _utc_timestamp(timestamp), _utc_timestamp(expiry)
        with self._write_transaction():
            current = self._owned_running_job(job_id, worker, timestamp_text)
            if current is None:
                return None
            renewed = current.model_copy(update={"updated_at": timestamp})
            cursor = self.db.connection.execute(
                """UPDATE jobs SET updated_at = ?, lease_expires_at = ?, payload = ?
                   WHERE id = ? AND status = ? AND lease_owner = ? AND lease_expires_at > ?""",
                (timestamp_text, expiry_text, _job_payload(renewed), str(job_id), JobStatus.RUNNING.value, worker, timestamp_text),
            )
            return renewed if cursor.rowcount == 1 else None

    def renew_lease(self, job_id: UUID, worker_id: str, lease_duration: timedelta, *, now: datetime | None = None) -> Job | None:
        """Alias for :meth:`heartbeat` for runners that use lease terminology."""
        return self.heartbeat(job_id, worker_id, lease_duration, now=now)

    def complete(self, job_id: UUID, worker_id: str, *, now: datetime | None = None) -> Job | None:
        """Mark an owned, unexpired running job complete."""
        worker = _valid_worker(worker_id)
        timestamp = _validated_now(now)
        timestamp_text = _utc_timestamp(timestamp)
        with self._write_transaction():
            current = self._owned_running_job(job_id, worker, timestamp_text)
            if current is None:
                return None
            completed = current.model_copy(update={
                "status": JobStatus.COMPLETED,
                "updated_at": timestamp,
                "error_code": None,
                "error_message": None,
            })
            cursor = self.db.connection.execute(
                """UPDATE jobs
                   SET status = ?, updated_at = ?, error_code = NULL, error_message = NULL,
                       lease_owner = NULL, lease_expires_at = NULL, payload = ?
                   WHERE id = ? AND status = ? AND lease_owner = ? AND lease_expires_at > ?""",
                (
                    JobStatus.COMPLETED.value, timestamp_text, _job_payload(completed), str(job_id),
                    JobStatus.RUNNING.value, worker, timestamp_text,
                ),
            )
            return completed if cursor.rowcount == 1 else None

    def fail(
        self,
        job_id: UUID,
        worker_id: str,
        error_code: str,
        error_message: str,
        *,
        max_attempts: int,
        now: datetime | None = None,
    ) -> Job | None:
        """Record a failure and return it to pending while its budget remains."""
        worker = _valid_worker(worker_id)
        code, message = _valid_error(error_code, error_message)
        maximum = _valid_max_attempts(max_attempts)
        timestamp = _validated_now(now)
        timestamp_text = _utc_timestamp(timestamp)
        with self._write_transaction():
            current = self._owned_running_job(job_id, worker, timestamp_text)
            if current is None:
                return None
            next_status = JobStatus.PENDING if current.attempt < maximum else JobStatus.FAILED
            transitioned = current.model_copy(update={
                "status": next_status,
                "updated_at": timestamp,
                "error_code": code,
                "error_message": message,
            })
            cursor = self.db.connection.execute(
                """UPDATE jobs
                   SET status = ?, updated_at = ?, error_code = ?, error_message = ?,
                       lease_owner = NULL, lease_expires_at = NULL, payload = ?
                   WHERE id = ? AND status = ? AND lease_owner = ? AND lease_expires_at > ?""",
                (
                    next_status.value, timestamp_text, code, message, _job_payload(transitioned), str(job_id),
                    JobStatus.RUNNING.value, worker, timestamp_text,
                ),
            )
            return transitioned if cursor.rowcount == 1 else None

    def fail_terminal(
        self,
        job_id: UUID,
        worker_id: str,
        error_code: str,
        error_message: str,
        *,
        now: datetime | None = None,
    ) -> Job | None:
        """Terminally fail an owned, unexpired running job.

        This is intentionally separate from :meth:`fail`: callers that know
        an error cannot succeed on retry must not consume the remaining retry
        budget merely to reach the terminal state.
        """
        worker = _valid_worker(worker_id)
        code, message = _valid_error(error_code, error_message)
        timestamp = _validated_now(now)
        timestamp_text = _utc_timestamp(timestamp)
        with self._write_transaction():
            current = self._owned_running_job(job_id, worker, timestamp_text)
            if current is None:
                return None
            failed = current.model_copy(update={
                "status": JobStatus.FAILED,
                "updated_at": timestamp,
                "error_code": code,
                "error_message": message,
            })
            cursor = self.db.connection.execute(
                """UPDATE jobs
                   SET status = ?, updated_at = ?, error_code = ?, error_message = ?,
                       lease_owner = NULL, lease_expires_at = NULL, payload = ?
                   WHERE id = ? AND status = ? AND lease_owner = ? AND lease_expires_at > ?""",
                (
                    JobStatus.FAILED.value, timestamp_text, code, message, _job_payload(failed), str(job_id),
                    JobStatus.RUNNING.value, worker, timestamp_text,
                ),
            )
            return failed if cursor.rowcount == 1 else None

    def recover_expired(self, *, max_attempts: int, now: datetime | None = None) -> list[Job]:
        """Recover expired running jobs and terminalize exhausted pending jobs.

        Direct ``claim`` also recovers expired jobs, but this explicit operation
        is useful to a future runner during startup and makes recovery visible.
        Retryable leases return to pending without changing attempt count.
        """
        maximum = _valid_max_attempts(max_attempts)
        timestamp = _validated_now(now)
        timestamp_text = _utc_timestamp(timestamp)
        with self._write_transaction():
            rows = self.db.connection.execute(
                """SELECT * FROM jobs
                   WHERE (status = ? AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?)
                      OR (status = ? AND attempt >= ?)
                   ORDER BY created_at, id""",
                (JobStatus.RUNNING.value, timestamp_text, JobStatus.PENDING.value, maximum),
            ).fetchall()
            recovered: list[Job] = []
            for row in rows:
                current = _model(row, Job)
                if current.attempt >= maximum:
                    recovered.append(self._fail_exhausted(current, maximum, timestamp, timestamp_text))
                    continue
                pending = current.model_copy(update={"status": JobStatus.PENDING, "updated_at": timestamp})
                cursor = self.db.connection.execute(
                    """UPDATE jobs
                       SET status = ?, updated_at = ?, lease_owner = NULL, lease_expires_at = NULL, payload = ?
                       WHERE id = ? AND status = ? AND lease_expires_at <= ?""",
                    (
                        JobStatus.PENDING.value, timestamp_text, _job_payload(pending), str(current.id),
                        JobStatus.RUNNING.value, timestamp_text,
                    ),
                )
                if cursor.rowcount == 1:
                    recovered.append(pending)
            return recovered

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        self.db.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.connection.rollback()
            raise
        else:
            self.db.connection.commit()

    def _owned_running_job(self, job_id: UUID, worker: str, timestamp_text: str) -> Job | None:
        row = self.db.connection.execute(
            """SELECT * FROM jobs
               WHERE id = ? AND status = ? AND lease_owner = ? AND lease_expires_at > ?""",
            (str(job_id), JobStatus.RUNNING.value, worker, timestamp_text),
        ).fetchone()
        return None if row is None else _model(row, Job)

    def _fail_exhausted(self, current: Job, maximum: int, timestamp: datetime, timestamp_text: str) -> Job:
        """Terminalize an exhausted pending or expired-running job in this transaction."""
        failed = current.model_copy(update={
            "status": JobStatus.FAILED,
            "updated_at": timestamp,
            "error_code": "attempt_budget_exhausted",
            "error_message": _attempt_budget_message(maximum),
        })
        cursor = self.db.connection.execute(
            """UPDATE jobs
               SET status = ?, updated_at = ?, error_code = ?, error_message = ?,
                   lease_owner = NULL, lease_expires_at = NULL, payload = ?
               WHERE id = ?
                 AND (status = ? OR (status = ? AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?))""",
            (
                JobStatus.FAILED.value, timestamp_text, failed.error_code, failed.error_message, _job_payload(failed),
                str(current.id), JobStatus.PENDING.value, JobStatus.RUNNING.value, timestamp_text,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("exhausted job state transition lost its eligibility guard")
        return failed


def _validated_now(value: datetime | None) -> datetime:
    timestamp = datetime.now(timezone.utc) if value is None else value
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return timestamp.astimezone(timezone.utc)


def _lease_times(value: datetime | None, lease_duration: timedelta) -> tuple[datetime, datetime]:
    if not isinstance(lease_duration, timedelta) or lease_duration <= timedelta(0):
        raise ValueError("lease_duration must be a positive timedelta")
    timestamp = _validated_now(value)
    try:
        return timestamp, timestamp + lease_duration
    except OverflowError as exc:
        raise ValueError("lease_duration is outside the supported timestamp range") from exc


def _valid_worker(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise ValueError("worker_id must be a non-empty string of at most 500 characters")
    return value.strip()


def _valid_max_attempts(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > 100:
        raise ValueError("max_attempts must be an integer from 1 to 100")
    return value


def _valid_allowed_types(value: Collection[JobType] | None) -> tuple[JobType, ...] | None:
    if value is None:
        return None
    try:
        values = tuple(value)
    except TypeError as exc:
        raise ValueError("allowed_types must be a collection of JobType values") from exc
    if any(not isinstance(item, JobType) for item in values):
        raise ValueError("allowed_types must contain only JobType values")
    return tuple(dict.fromkeys(values))


def _valid_error(code: str, message: str) -> tuple[str, str]:
    if not isinstance(code, str) or not code.strip() or len(code) > 100:
        raise ValueError("error_code must be a non-empty string of at most 100 characters")
    if not isinstance(message, str) or not message.strip() or len(message) > 2_000:
        raise ValueError("error_message must be a non-empty string of at most 2000 characters")
    return code.strip(), message.strip()


def _attempt_budget_message(max_attempts: int) -> str:
    return f"job exhausted its maximum of {max_attempts} attempts"
