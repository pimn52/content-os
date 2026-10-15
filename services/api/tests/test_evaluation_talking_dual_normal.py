"""The same real authority and normal flow contracts, now with schema10."""
import pytest
from app.providers import maintained_talking_dual as dual
from test_maintained_talking_execution import talking_case as original_talking_case
from test_maintained_talking_dual import make_dual_case, candidate
from test_evaluation_talking_normal_inputs import normal_inputs, qa_case, bound_job, child_case, setup
from test_evaluation_talking_normal_flow import (normal_flow,
    test_normal_preflight_create_reload_retained_and_cancel_without_calls,
    test_normal_owned_handler_through_preview_review_admission_with_real_authority,
    test_normal_owned_job_cannot_use_generic_or_foreign_handlers,
    test_normal_second_connection_source_revocation_rolls_back_enqueue)


@pytest.fixture
def talking_case(original_talking_case,monkeypatch,request):
    value=make_dual_case(original_talking_case,monkeypatch)
    value.quality_binding=getattr(request,'param',True)
    monkeypatch.setattr(dual,"_system_root",lambda:value.root)
    return value


def test_dual_capability_cannot_borrow_historical_quality(normal_flow):
    from app.db import ProviderMachineCapabilityProfileRepository
    from app.execution_admission import UseAdmissionError
    c=normal_flow
    assert c.spec.schema_version==10
    repo=ProviderMachineCapabilityProfileRepository(c.db)
    repo.save(c.cap.model_copy(update={"verified_parameters":{"fresh_voice_talking_duration_ms":10000}}))
    state=c.service.preflight(c.project,c.selection)
    assert not state["eligible"]
    assert state["reasons"] in (["evaluation_talking_master_not_eligible"],["provider_use_configuration_changed"],["exact_talking_configuration_not_verified"])
    # A direct configuration hash mismatch also remains a normal admission failure.
    with pytest.raises(UseAdmissionError):c.service._context(c.project,c.selection)


@pytest.mark.parametrize('talking_case',[False],indirect=True)
def test_exact_recipe_evidence_required_even_with_valid_master(normal_inputs):
    from test_evaluation_talking_normal_flow import admit_normal_execution
    from app.execution_admission import UseAdmissionError
    c=normal_inputs
    execution,_,_=admit_normal_execution(c)
    selected=c.selection.model_copy(update={'execution_admission_id':execution.id})
    assert c.parent.evaluation_master['current_eligible']
    with pytest.raises(UseAdmissionError,match='exact_talking_configuration_not_verified'):
        c.service._context(c.project,selected)


def test_dual_late_source_revocation_keeps_call_and_denies_output(normal_flow,monkeypatch):
    from types import SimpleNamespace
    from pathlib import Path
    from datetime import timedelta
    from app.db import Database, JobRepository, ProviderCallRepository
    from app.domain.models import JobStatus
    from app.domain.evaluation_talking import TalkingStart
    from app.jobs.evaluation_talking_handler import MaintainedEvaluationTalkingHandler
    from app.jobs.runner import JobRunner
    from app.jobs.store import JobStore
    from app.talking.source_admission import TalkingSourceAdmissionRepository
    c=normal_flow
    state=c.service.preflight(c.project,c.selection)
    op=c.service.create(c.project,TalkingStart(**c.selection.model_dump(),idempotency_key='dual-late-revoke',expected_fingerprint=state['fingerprint']))
    calls=[]
    from app.media.ffprobe import ProbeMetadata
    context=c.service.require_current_operation(op)
    probe=SimpleNamespace(probe=lambda _path:ProbeMetadata(duration_ms=context['slice_plan'].duration_ms,
        width=1080,height=1920,fps=c.asset.fps,has_audio=True,metadata={'streams':[{'codec_type':'video'},{'codec_type':'audio'}]}))
    monkeypatch.setattr('app.evaluation_talking_execution.FFProbeAdapter',lambda *_a:probe)
    def launch(argv,**kw):
        assert not c.db.connection.in_transaction
        calls.append(argv);(Path(kw['cwd'])/'output.mp4').write_bytes(b'synthetic revoked output')
        with Database(c.db.path) as other:
            TalkingSourceAdmissionRepository(other,c.root).revoke(c.receipt.id,actor_id='fixture-operator',reason='late synthetic source withdrawal')
        return SimpleNamespace(wait=lambda **_kw:0,poll=lambda:0)
    monkeypatch.setattr(dual.subprocess,'Popen',launch)
    options=dict(c.talking_options);options.pop('data_root');options.pop('staging_parent')
    handler=MaintainedEvaluationTalkingHandler(c.db,c.root,options=options,policy=c.selection.policy)
    job=JobRepository(c.db).get(op.generation_job_id)
    result=JobRunner(JobStore(c.db),{job.type:handler},worker_id='dual-revoke',lease_duration=timedelta(minutes=5),max_attempts=3).run_once()
    assert result.status==JobStatus.FAILED and len(calls)==1
    assert c.service.records.result(op.id,'generation') is None
    assert c.service.records.result(op.id,'quarantine') is not None
    call=ProviderCallRepository(c.db).get_by_idempotency_key(f'evaluation-talking:{c.project}:{op.inference_sha256}:generation')
    assert call is not None and call.status in ('failed','running','reserved')
    assert JobRunner(JobStore(c.db),{job.type:handler},worker_id='no-retry',lease_duration=timedelta(minutes=5),max_attempts=3).run_once() is None
