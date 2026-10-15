"""Owned post-generation gates tolerate only a forward heartbeat timestamp."""
from datetime import timedelta
import pytest
from app.db import JobRepository
from app.domain.models import JobStatus
from app.execution_admission import UseAdmissionError
from test_evaluation_talking_preview import (
    preview_owned, qa_owned, call_case, ownership, qa_case, bound_job, child_case, setup, candidate, talking_case,
)


@pytest.mark.parametrize('qa_case',['isolated-zip-asr-v2'],indirect=True)
@pytest.mark.parametrize('role',['qa','preview'])
@pytest.mark.parametrize('mutation',['heartbeat','backward_time','status','attempt'])
def test_owned_post_generation_job_observation(preview_owned,role,mutation):
    c=preview_owned
    job=c.qa if role=='qa' else c.preview_job
    handler=c.handler if role=='qa' else c.preview_handler
    JobRepository(c.db).update(job)
    updates={'updated_at':job.updated_at+timedelta(seconds=30)}
    if mutation=='backward_time':updates['updated_at']=job.updated_at-timedelta(seconds=1)
    elif mutation=='status':updates['status']=JobStatus.CANCELLED
    elif mutation=='attempt':updates['attempt']=job.attempt+1
    JobRepository(c.db).update(job.model_copy(update=updates))
    calls=len(c.calls)
    if mutation=='heartbeat':
        assert handler.require_dispatch(job)[1]==c.operation
    else:
        with pytest.raises(UseAdmissionError,match='job_not_running'):handler.require_dispatch(job)
    assert len(c.calls)==calls
