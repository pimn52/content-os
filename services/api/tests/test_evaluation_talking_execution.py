"""Ledger/output integration with STUBBED admission/frozen resolver.

Real temporary SQLite and file identity, static probe and fake Popen. This is
not the W30 normal-input admission matrix or real LatentSync runtime evidence.
"""
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from pathlib import Path
import pytest
from app.db import JobRepository, AssetRepository, BudgetPolicyRepository, ProviderCallRepository
from app.domain.models import BudgetPolicy, JobStatus
from app.costs import UnknownProviderCostEstimator
from app.execution_scope import WorkerExecutionUseSnapshotV2, register_generated_import_scope
from app.execution_admission import UseAdmissionError
from app.provider_execution import ProviderExecutionAccountingFailure
from app.budget import BudgetLimitError
from app.talking.slices import plan_talking_audio_slice
from app.providers import maintained_talking_execution as runtime
from app import evaluation_talking_execution as execution
from test_evaluation_talking_ownership import ownership, qa_case, bound_job, child_case, setup, candidate, talking_case
from test_media_import import _StaticProbe
from app.jobs import evaluation_talking_handler as handler_module
from app.media.audio_access import require_ordinary_media
from app.media.importer import MediaImporter


@pytest.fixture
def call_case(ownership,talking_case,monkeypatch):
    c = ownership
    operation = c.service.create(c.project,c.payload)
    job = JobRepository(c.db).get(operation.generation_job_id)
    from app.jobs.store import JobStore
    job = JobStore(c.db).claim("execution-fixture",timedelta(minutes=5),max_attempts=1,job_id=job.id)
    audio = talking_case.request.narration.model_copy(update={"metadata":{"voice_generation":{"qa_state":"verified"}}})
    plan = plan_talking_audio_slice(audio,start_segment_index=0,end_segment_index=1,max_duration_ms=2000)
    request = talking_case.request.model_copy(update={"narration":audio,"slice_plan":plan})
    context = c.context
    context.update(audio=audio,profile=request.profile,source=SimpleNamespace(source_file=request.reference_path,content_hash=request.reference_sha256),
        clip=SimpleNamespace(id=request.reference_clip_id,start_ms=request.reference_start_ms,end_ms=request.reference_end_ms),slice_plan=plan,
        execution=c.service.admissions.get(c.project,operation.selection.execution_admission_id))
    monkeypatch.setattr(execution,"EvaluationTalkingService",lambda *_a:c.service)
    monkeypatch.setattr(c.service,"require_current_operation",lambda *_a,**_kw:context)
    monkeypatch.setattr(execution,"FFProbeAdapter",lambda *_a:_StaticProbe())
    calls = []
    def frozen(_scopes,_job,_provider,prepared,**_kw):
        snapshot = WorkerExecutionUseSnapshotV2.model_validate_json(__import__("json").dumps(prepared.execution_use_snapshot()))
        def reserve(): assert c.db.connection.in_transaction
        return SimpleNamespace(snapshot=snapshot,reservation_guard=reserve,verify_before_launch=prepared.verify)
    monkeypatch.setattr(execution,"freeze_worker_identity",frozen)
    monkeypatch.setattr(runtime,"_system_root",lambda:talking_case.root)
    def launch(argv,**kw):
        calls.append(argv)
        (Path(kw["cwd"])/"output.mp4").write_bytes(b"fake owned Talking output")
        return SimpleNamespace(wait=lambda **_kw:0,poll=lambda:0)
    monkeypatch.setattr(runtime.subprocess,"Popen",launch)
    BudgetPolicyRepository(c.db).save(BudgetPolicy(project_id=c.project,allow_unknown_cost=True,updated_at=datetime.now(timezone.utc)))
    return SimpleNamespace(**vars(c),operation=operation,job=job,request=request,options=talking_case.options,calls=calls,launch=launch)


def run(c):
    from app.jobs.lease_fence import owned_job_lease
    from app.jobs.store import JobStore
    with owned_job_lease(JobStore(c.db),c.job,"execution-fixture"), runtime.prepare_maintained_talking_child(request=c.request,**c.options) as prepared:
        return execution.execute_evaluation_talking(db=c.db,root=c.service.root,job=c.job,prepared=prepared,cost_estimator=UnknownProviderCostEstimator())


def test_once_only_ledger_full_output_scope_and_authenticated_replay(call_case):
    c = call_case
    asset,result = run(c)
    assert len(c.calls) == 1
    assert c.service.scopes.get("asset",asset.id) == c.context["output_scope"]
    assert c.service.scopes.scopes_for_hash(asset.content_hash) == (c.context["output_scope"],)
    assert asset.metadata["talking_generation"]["qa_state"] == "pending"
    call = ProviderCallRepository(c.db).get_by_idempotency_key(execution.call_key(c.operation))
    assert call.status == "completed" and call.actual_cost is None
    assert run(c) == (asset,result)
    assert len(c.calls) == 1
    # The generic importer must never replace the named role-aware writer.
    with pytest.raises(UseAdmissionError,match="owned_output_writer"):
        with c.db.transaction(immediate=True): register_generated_import_scope(c.db,"asset",asset,c.job)


def test_late_cancellation_quarantines_bytes_and_keeps_consumed_call(call_case,monkeypatch):
    c = call_case
    def launch(*args,**kwargs):
        value = c.launch(*args,**kwargs)
        c.service.cancel(c.project,c.operation.id)
        return value
    monkeypatch.setattr(runtime.subprocess,"Popen",launch)
    with pytest.raises(UseAdmissionError,match="not_running|lease_lost"): run(c)
    assert len(c.calls) == 1
    call = ProviderCallRepository(c.db).get_by_idempotency_key(execution.call_key(c.operation))
    assert call.status == "failed" and call.actual_cost is None
    assert c.service.records.result(c.operation.id,"generation") is None
    quarantine = c.service.records.result(c.operation.id,"quarantine")
    assert (c.service.root/quarantine["path"]).read_bytes() == b"fake owned Talking output"
    assert AssetRepository(c.db).get_by_content_hash(quarantine["content_hash"]) is None
    assert c.service.scopes.scopes_for_hash(quarantine["content_hash"]) == (c.context["output_scope"],)
    alias = MediaImporter(c.db,c.service.root,_StaticProbe()).import_path(c.service.root/quarantine["path"],"ordinary reimport cannot remove restriction")
    with pytest.raises(UseAdmissionError):
        require_ordinary_media(c.db,c.service.root,Path(alias.source_file),kind="asset",subject_id=alias.id,content_hash=alias.content_hash)
    assert c.service.cancel(c.project,c.operation.id) == c.operation
    with pytest.raises(UseAdmissionError): run(c)
    assert len(c.calls) == 1


def test_terminal_commit_failure_rolls_back_asset_scope_but_retains_unknown_call(call_case,monkeypatch):
    c = call_case
    save = c.service.records.save_result
    def rejected(identity,role,payload):
        if role == "generation": raise ValueError("injected terminal sidecar failure")
        return save(identity,role,payload)
    monkeypatch.setattr(c.service.records,"save_result",rejected)
    with pytest.raises((ProviderExecutionAccountingFailure,ValueError),match="sidecar|reconciled"): run(c)
    assert len(c.calls) == 1
    call = ProviderCallRepository(c.db).get_by_idempotency_key(execution.call_key(c.operation))
    assert call.status in ("reserved","running")
    quarantine = c.service.records.result(c.operation.id,"quarantine")
    assert AssetRepository(c.db).get_by_content_hash(quarantine["content_hash"]) is None
    assert c.service.scopes.scopes_for_hash(quarantine["content_hash"]) == (c.context["output_scope"],)
    with pytest.raises(Exception): run(c)
    assert len(c.calls) == 1


def test_unknown_cost_budget_blocks_before_child_launch(call_case):
    c = call_case
    BudgetPolicyRepository(c.db).save(BudgetPolicy(project_id=c.project,allow_unknown_cost=False,updated_at=datetime.now(timezone.utc)))
    with pytest.raises(BudgetLimitError): run(c)
    assert c.calls == []
    assert ProviderCallRepository(c.db).get_by_idempotency_key(execution.call_key(c.operation)) is None


def test_owned_handler_completed_recovery_does_not_prepare_or_launch(call_case,monkeypatch):
    c = call_case
    asset,_ = run(c)
    monkeypatch.setattr(handler_module,"EvaluationTalkingService",lambda *_a:c.service)
    monkeypatch.setattr(c.service,"require_retained_operation",lambda *_a:c.context)
    monkeypatch.setattr(handler_module,"prepare_maintained_talking_child",lambda **_kw:pytest.fail("completed recovery cannot prepare"))
    handler = handler_module.MaintainedEvaluationTalkingHandler(c.db,c.service.root,options={})
    assert handler(c.job) == asset
    assert len(c.calls) == 1


def test_retained_carrier_report_survives_bundle_update_but_terms_do_not(call_case,monkeypatch):
    import hashlib
    c = call_case
    receipt = c.context["execution"]
    path = c.service.root/"retained-talking-report.json"
    path.write_text(receipt.review.model_dump_json())
    receipt = receipt.model_copy(update={"review_reference":path.name,"review_sha256":hashlib.sha256(path.read_bytes()).hexdigest()})
    from app.runtime_inventory import RuntimeInventoryStore
    inventory = runtime.observe_talking_inventory(c.options["environment"],c.options["base"],max_bytes=c.options["max_bytes"])
    assert RuntimeInventoryStore(c.service.root).install_v2(inventory) == receipt.review.execution_specification.runtime_inventory
    # The structural fixture's carrier has its own nested data root. Install
    # its exact synthetic origin at the report's project evidence boundary.
    origin_reference = receipt.review.execution_specification.runtime_origin.reference
    (c.service.root/origin_reference).write_bytes((c.options["data_root"]/origin_reference).read_bytes())
    monkeypatch.setattr(runtime,"talking_bundle_bytes",lambda:b"updated application bundle")
    assert execution.retained_talking_report(c.service.admissions,receipt) == receipt.review
    with pytest.raises(ValueError): c.service.admissions._report(path.name,receipt.review_sha256)
    terms = c.service.root/receipt.review.license_terms_reference
    terms.write_bytes(b"changed terms")
    with pytest.raises(UseAdmissionError,match="terms_hash_changed"):
        execution.retained_talking_report(c.service.admissions,receipt)


def test_invalid_media_probe_still_quarantines_produced_bytes(call_case,monkeypatch):
    c = call_case
    def invalid(_self,_path): raise ValueError("synthetic invalid probe")
    monkeypatch.setattr(_StaticProbe,"probe",invalid)
    with pytest.raises(ValueError,match="invalid probe"): run(c)
    quarantine = c.service.records.result(c.operation.id,"quarantine")
    assert (c.service.root/quarantine["path"]).read_bytes() == b"fake owned Talking output"
    assert AssetRepository(c.db).get_by_content_hash(quarantine["content_hash"]) is None
    assert c.service.scopes.scopes_for_hash(quarantine["content_hash"]) == (c.context["output_scope"],)
    assert len(c.calls) == 1
