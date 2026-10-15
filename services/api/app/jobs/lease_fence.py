"""Internal claimed-lease context for owned, long-preparing Talking work."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from app.execution_admission import UseAdmissionError

_current = ContextVar("owned_job_lease", default=None)


class OwnedJobLease:
    def __init__(self, store, job, worker, heartbeat=None, resource_key=None):
        self.db, self.job_id, self.attempt = store.db, job.id, job.attempt
        self.worker, self.heartbeat, self.resource_key = worker, heartbeat, resource_key

    def require(self):
        if self.heartbeat is not None:
            try:
                self.heartbeat.ensure_healthy()
            except Exception:
                raise UseAdmissionError("evaluation_talking_lease_lost") from None
        row = self.db.connection.execute(
            "SELECT status,attempt,lease_owner,lease_expires_at FROM jobs WHERE id=?", (str(self.job_id),)).fetchone()
        now = datetime.now(timezone.utc)
        if (row is None or row[0] != "running" or row[1] != self.attempt or row[2] != self.worker
            or row[3] is None or datetime.fromisoformat(row[3].replace("Z", "+00:00")) <= now):
            raise UseAdmissionError("evaluation_talking_lease_lost")
        if self.resource_key is not None:
            resource = self.db.connection.execute(
                "SELECT job_id,worker_id,lease_expires_at FROM local_resource_leases WHERE resource_key=?",
                (self.resource_key,)).fetchone()
            if (resource is None or resource[0] != str(self.job_id) or resource[1] != self.worker
                or datetime.fromisoformat(resource[2].replace("Z", "+00:00")) <= now):
                raise UseAdmissionError("evaluation_talking_lease_lost")


@contextmanager
def owned_job_lease(store, job, worker, heartbeat=None, resource_key=None):
    lease = OwnedJobLease(store, job, worker, heartbeat, resource_key)
    token = _current.set(lease)
    try:
        yield lease
    finally:
        _current.reset(token)


def require_owned_lease(db, job):
    lease = _current.get()
    if (type(lease) is not OwnedJobLease or lease.db is not db
        or lease.job_id != job.id or lease.attempt != job.attempt):
        raise UseAdmissionError("evaluation_talking_owned_lease_required")
    lease.require()
    return lease
