"""Real lease rows and fake children; no vendor/model execution."""
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
import pytest
from app.db import Database
from app.domain.models import Job, JobType
from app.execution_admission import UseAdmissionError
from app.jobs.store import JobStore
from app.jobs.lease_fence import OwnedJobLease, owned_job_lease, require_owned_lease
from test_maintained_talking_dual import dual_case, talking_case, candidate, prepare


def lease_case(db):
    now=datetime.now(timezone.utc)
    store=JobStore(db)
    job=store.enqueue(Job(type=JobType.ANALYZE_ASSET,idempotency_key="synthetic-lease",created_at=now,updated_at=now))
    job=store.claim("owned",timedelta(minutes=5),max_attempts=1,job_id=job.id)
    return store,job,OwnedJobLease(store,job,"owned")


@pytest.mark.parametrize("change",["expired","wrong_owner","missing","heartbeat_failed","attempt"])
def test_exact_owned_lease_denies_loss_without_adopting_new_owner(tmp_path,change):
    with Database(tmp_path/"lease.sqlite3") as db:
        store,job,lease=lease_case(db)
        lease.require()
        if change=="expired":db.connection.execute("UPDATE jobs SET lease_expires_at='2000-01-01T00:00:00Z'")
        elif change=="wrong_owner":db.connection.execute("UPDATE jobs SET lease_owner='another'")
        elif change=="missing":db.connection.execute("DELETE FROM jobs")
        elif change=="attempt":db.connection.execute("UPDATE jobs SET attempt=attempt+1")
        else:
            def fail():raise RuntimeError("synthetic heartbeat failure")
            lease.heartbeat=SimpleNamespace(ensure_healthy=fail)
        with pytest.raises(UseAdmissionError,match="lease_lost"):lease.require()


def test_direct_owned_dispatch_requires_context_and_renewal_is_accepted(tmp_path):
    with Database(tmp_path/"renew.sqlite3") as db:
        store,job,_=lease_case(db)
        with pytest.raises(UseAdmissionError,match="owned_lease_required"):require_owned_lease(db,job)
        with owned_job_lease(store,job,"owned") as lease:
            assert require_owned_lease(db,job) is lease
            assert store.heartbeat(job.id,"owned",timedelta(minutes=10)) is not None
            lease.require()
        with pytest.raises(UseAdmissionError):require_owned_lease(db,job)


def test_lease_loss_during_last_prepared_verify_launches_nothing(dual_case,tmp_path,monkeypatch):
    with Database(tmp_path/"last-verify.sqlite3") as db:
        _,_,lease=lease_case(db)
        with prepare(dual_case) as child:
            verify=child.verify
            def lose_lease():
                value=verify()
                db.connection.execute("UPDATE jobs SET lease_expires_at='2000-01-01T00:00:00Z'")
                return value
            monkeypatch.setattr(child,"verify",lose_lease)
            monkeypatch.setattr("app.providers.maintained_talking_dual.subprocess.Popen",lambda *_a,**_kw:pytest.fail("expired carrier must not launch"))
            with pytest.raises(UseAdmissionError,match="lease_lost"):child.execute_owned(lease)
