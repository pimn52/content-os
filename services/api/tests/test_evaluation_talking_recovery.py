"""Normal operator/rights/unique-slot recovery with synthetic runtime only."""
import json
import hashlib
from datetime import datetime, timedelta, timezone
from uuid import UUID
from types import SimpleNamespace
import pytest
from app.db import Database, JobRepository, ProviderCallRepository
from app.domain.models import execution_canonical_sha256 as digest, ProviderCallRecord
from app.domain.talking_recovery import TalkingRecoveryReview, TalkingRecoveryStart
from app.domain.evaluation_talking import TalkingCalibrationStart
from app.evaluation_composition import EvaluationCompositionService
from app.evaluation_talking_recovery import TalkingRecoveryService
from app.evaluation_talking_execution import call_key
from app.execution_admission import UseAdmissionError
from app.jobs.store import JobStore
from app.runtime_inventory import RuntimeInventoryStore
from app.providers import maintained_talking_dual as dual
from test_evaluation_talking_calibration import calibration_flow, start
from test_evaluation_talking_normal_inputs import normal_inputs, qa_case, bound_job, child_case, setup
from test_evaluation_talking_dual_normal import talking_case, original_talking_case, candidate
from test_evaluation_talking_normal_flow import save, http
from test_evaluation_composition import batch
from test_execution_admission import OPERATOR_KEY


@pytest.fixture
def recovery_case(calibration_flow,monkeypatch):
    c=calibration_flow
    from app.db import BudgetPolicyRepository
    budgets=BudgetPolicyRepository(c.db)
    for scope in (None,c.project):
        policy=budgets.get_by_project(scope)
        budgets.save(policy.model_copy(update={"max_calls":10}))
    c.composition=EvaluationCompositionService(c.db,c.root)
    batch(c,[c.selection.scene_plan_id])
    c.predecessor=c.service.create(c.project,start(c,"old-carrier"))
    claimed=JobStore(c.db).claim("interrupted",timedelta(minutes=5),max_attempts=1,job_id=c.predecessor.generation_job_id)
    c.db.connection.execute("UPDATE jobs SET lease_expires_at='2000-01-01T00:00:00Z' WHERE id=?",(str(claimed.id),))
    c.service.cancel(c.project,c.predecessor.id)
    c.recovery=TalkingRecoveryService(c.service)
    c.original_root=c.recovery._root(c.project,c.predecessor.id)[2]
    # A real owned application bundle difference; installed files/recipe,
    # capability and all creative inputs remain identical.
    bundle=dual.application_bundle()
    monkeypatch.setattr(dual,"application_bundle",lambda:bundle+b"synthetic tested application correction")
    app=dual.observe_application_inventory(c.talking_options["environment"],c.talking_options["base"],max_bytes=c.talking_options["max_bytes"])
    RuntimeInventoryStore(c.root).install_v2(app)
    origin=json.loads((c.root/c.spec.runtime_origin.reference).read_text())
    for part in origin["components"]:
        if part["role"]=="application":part["inventory_sha256"]=app.descriptor.sha256
    path=c.root/"corrected-origin.json";path.write_text(json.dumps(origin))
    from app.domain.maintained_runtime import MaintainedEvidenceReference
    c.talking_options["origin_reference"]=MaintainedEvidenceReference(reference=path.name,sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    c.spec=dual.observe_dual(c.talking_options)
    from app.domain.evaluation_talking import TalkingSelection
    c.selection=TalkingSelection.model_validate_json(c.selection.model_dump_json(exclude={"policy","trial_duration_limit_ms","trial_processing_limit_ms"}))
    import test_evaluation_talking_calibration as calibration
    with monkeypatch.context() as scoped:
        scoped.setattr(calibration,"save",lambda root,name,value:save(root,"corrected-"+name,value))
        calibration.calibration_flow.__wrapped__(c)
    state,_,_=c.recovery.preflight(c.project,c.predecessor.id,c.selection)
    closure=c.root/"closure.json"
    closure.write_text(json.dumps(dict(predecessor_id=str(c.predecessor.id),processes_absent=True,
        accepted_local_history=True,method="Synthetic process-free fixture, no actual runtime evidence")))
    review=TalkingRecoveryReview(policy="evaluation-talking-no-dispatch-continuation-v1",project_id=c.project,
        batch_id=UUID(state["batch_id"]),predecessor_id=c.predecessor.id,predecessor_sha256=state["predecessor_sha256"],
        selection=c.selection,fingerprint=state["fingerprint"],closure_evidence=MaintainedEvidenceReference(
            reference=closure.name,sha256=hashlib.sha256(closure.read_bytes()).hexdigest()),findings=("Synthetic closure only",))
    adopted=save(c.root,"recovery-review.json",review.model_dump(mode="json"))
    c.recovery_payload=TalkingRecoveryStart(start=start(c,"successor"),review_reference=adopted.review_reference,review_sha256=adopted.review_sha256)
    return c


@pytest.mark.parametrize("normal_inputs",["calibration"],indirect=True)
def test_normal_linked_successor_reload_downstream_and_original_immutability(recovery_case,monkeypatch):
    c=recovery_case
    with pytest.raises(UseAdmissionError,match="operator_required"):
        c.recovery.create(c.project,c.predecessor.id,c.recovery_payload,None)
    base=f"/projects/{c.project}/evaluation-talking/operations/{c.predecessor.id}/recovery"
    response=http(c,"POST",base,json=c.recovery_payload.model_dump(mode="json"),headers={"x-content-os-execution-admission-key":OPERATOR_KEY})
    assert response.status_code==201,response.text
    successor=c.service.records.operation(response.json()["id"])
    assert successor!=c.predecessor
    assert c.recovery.create(c.project,c.predecessor.id,c.recovery_payload,OPERATOR_KEY)==successor
    assert http(c,"GET",base).json()["successor_id"]==str(successor.id)
    assert c.recovery._root(c.project,c.predecessor.id)[2]==c.original_root
    with pytest.raises(UseAdmissionError,match="cancelled"):c.service.require_current_operation(c.predecessor)
    c.service.require_job_identity(JobRepository(c.db).get(successor.generation_job_id))
    # Actual owned handler -> QA -> scoped preview -> six-dimensional review
    # -> retained admission, all synthetic; no authority resolver is mocked.
    import test_evaluation_talking_normal_flow as normal
    monkeypatch.setattr(normal,"TalkingStart",TalkingCalibrationStart)
    normal.test_normal_owned_handler_through_preview_review_admission_with_real_authority(c,monkeypatch)
    assert c.recovery._root(c.project,c.predecessor.id)[2]==c.original_root
    row=c.db.connection.execute("SELECT * FROM evaluation_talking_recoveries").fetchone()
    c.db.connection.execute("DELETE FROM evaluation_talking_recoveries")
    with pytest.raises(UseAdmissionError):c.service.require_job_identity(JobRepository(c.db).get(successor.generation_job_id))
    c.db.connection.execute("INSERT INTO evaluation_talking_recoveries VALUES (?,?,?,?,?,?,?)",tuple(row))
    c.recovery.require_successor(successor)


@pytest.mark.parametrize("normal_inputs",["calibration"],indirect=True)
@pytest.mark.parametrize("status",["reserved","running","completed","failed","cancelled"])
def test_any_predecessor_reservation_denies_without_new_jobs(recovery_case,status):
    c=recovery_case
    before=c.db.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    estimate=c.service._budget_estimate(c.service._context(c.project,c.selection))
    call=ProviderCallRecord(project_id=c.project,idempotency_key=call_key(c.predecessor),operation="talking",mode="runtime",
        provider=c.cap.provider,model=c.cap.model,input_source="synthetic",estimated_cost=estimate,status=status,created_at=datetime.now(timezone.utc))
    ProviderCallRepository(c.db).create(call)
    with pytest.raises(UseAdmissionError,match="already_reserved"):
        c.recovery.create(c.project,c.predecessor.id,c.recovery_payload,OPERATOR_KEY)
    assert c.db.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]==before
    assert c.db.connection.execute("SELECT COUNT(*) FROM evaluation_talking_recoveries").fetchone()[0]==0


@pytest.mark.parametrize("normal_inputs",["calibration"],indirect=True)
def test_racing_reservation_rolls_back_successor_and_alias_does_not_reset(recovery_case,monkeypatch):
    c=recovery_case
    original=c.service._budget_estimate
    def reserve_between_observation_and_transaction(context):
        estimate=original(context)
        with Database(c.db.path) as other:
            ProviderCallRepository(other).create(ProviderCallRecord(project_id=c.project,idempotency_key=call_key(c.predecessor),
                operation="talking",mode="runtime",provider=c.cap.provider,model=c.cap.model,input_source="synthetic-race",estimated_cost=estimate,
                status="running",created_at=datetime.now(timezone.utc)))
        return estimate
    monkeypatch.setattr(c.service,"_budget_estimate",reserve_between_observation_and_transaction)
    before=c.db.connection.execute("SELECT COUNT(*) FROM evaluation_talking_operations").fetchone()[0]
    with pytest.raises(UseAdmissionError,match="already_reserved"):
        c.recovery.create(c.project,c.predecessor.id,c.recovery_payload,OPERATOR_KEY)
    assert c.db.connection.execute("SELECT COUNT(*) FROM evaluation_talking_operations").fetchone()[0]==before
    assert c.db.connection.execute("SELECT COUNT(*) FROM evaluation_talking_recoveries").fetchone()[0]==0


@pytest.mark.parametrize("normal_inputs",["calibration"],indirect=True)
def test_duplicate_second_connection_and_changed_request_are_bounded(recovery_case):
    c=recovery_case
    successor=c.recovery.create(c.project,c.predecessor.id,c.recovery_payload,OPERATOR_KEY)
    from app.evaluation_talking import EvaluationTalkingService
    with Database(c.db.path) as other:
        other.data_root=c.root
        recovery=TalkingRecoveryService(EvaluationTalkingService(other,c.root))
        assert recovery.create(c.project,c.predecessor.id,c.recovery_payload,OPERATOR_KEY)==successor
    alias=c.recovery_payload.model_copy(update={"start":c.recovery_payload.start.model_copy(update={"idempotency_key":"alias"})})
    with pytest.raises(UseAdmissionError,match="idempotency_conflict"):
        c.recovery.create(c.project,c.predecessor.id,alias,OPERATOR_KEY)
    with pytest.raises(UseAdmissionError,match="root_slot_required|chain_forbidden"):
        c.recovery._root(c.project,successor.id)
    assert c.db.connection.execute("SELECT COUNT(*) FROM evaluation_composition_slots").fetchone()[0]==1


@pytest.mark.parametrize("normal_inputs",["calibration"],indirect=True)
def test_missing_cancellation_quarantine_and_uncertain_closure_deny(recovery_case):
    c=recovery_case
    row=tuple(c.db.connection.execute("SELECT * FROM evaluation_talking_results WHERE operation_id=? AND role='cancellation'",(str(c.predecessor.id),)).fetchone())
    c.db.connection.execute("DELETE FROM evaluation_talking_results WHERE operation_id=? AND role='cancellation'",(str(c.predecessor.id),))
    with pytest.raises(UseAdmissionError,match="cancelled_root_required"):
        c.recovery.create(c.project,c.predecessor.id,c.recovery_payload,OPERATOR_KEY)
    c.db.connection.execute("INSERT INTO evaluation_talking_results VALUES (?,?,?,?)",row)
    c.service.records.save_result(c.predecessor.id,"quarantine",{"synthetic":"unverified output"})
    with pytest.raises(UseAdmissionError,match="existing_output"):
        c.recovery.create(c.project,c.predecessor.id,c.recovery_payload,OPERATOR_KEY)
    c.db.connection.execute("DELETE FROM evaluation_talking_results WHERE operation_id=? AND role='quarantine'",(str(c.predecessor.id),))
    closure=c.root/"closure.json";closure.write_text(json.dumps(dict(predecessor_id=str(c.predecessor.id),processes_absent=False,accepted_local_history=True,method="unknown")))
    with pytest.raises(UseAdmissionError,match="quiescence_required"):
        c.recovery.create(c.project,c.predecessor.id,c.recovery_payload,OPERATOR_KEY)
    assert c.db.connection.execute("SELECT COUNT(*) FROM evaluation_talking_recoveries").fetchone()[0]==0


@pytest.mark.parametrize("normal_inputs",["calibration"],indirect=True)
def test_cancelled_predecessor_cannot_reserve_after_recovery_commits(recovery_case):
    c=recovery_case
    c.recovery.create(c.project,c.predecessor.id,c.recovery_payload,OPERATOR_KEY)
    estimate=c.service._budget_estimate(c.service._context(c.project,c.selection))
    from app.budget import ProviderCallLedger
    from app.evaluation_talking import EvaluationTalkingService
    with Database(c.db.path) as other:
        other.data_root=c.root;owner=EvaluationTalkingService(other,c.root)
        old_job=JobRepository(other).get(c.predecessor.generation_job_id)
        with pytest.raises(UseAdmissionError,match="not_running"):
            ProviderCallLedger(other).reserve_execution(project_id=c.project,idempotency_key=call_key(c.predecessor),
                operation="talking",mode="runtime",provider=c.cap.provider,model=c.cap.model,input_source="synthetic-old-late-worker",
                input_digest="1"*64,estimated_cost=estimate,
                reservation_guard=lambda:owner.generation_guard(c.predecessor,old_job,{"facts":()}))
        assert ProviderCallRepository(other).get_by_idempotency_key(call_key(c.predecessor)) is None


@pytest.mark.parametrize("normal_inputs",["calibration"],indirect=True)
def test_post_reservation_lease_loss_is_consumed_without_child_or_output(recovery_case,monkeypatch):
    c=recovery_case
    successor=c.recovery.create(c.project,c.predecessor.id,c.recovery_payload,OPERATOR_KEY)
    from app.jobs.evaluation_talking_handler import MaintainedEvaluationTalkingHandler
    from app.jobs.runner import JobRunner, LeaseLost
    original=dual.PreparedDualTalkingChild._execute
    def expire_inside_final_verification(child,lease=None):
        assert ProviderCallRepository(c.db).get_by_idempotency_key(call_key(successor)).status=="running"
        verify=child.verify
        def expensive_verify_then_expire():
            value=verify()
            c.db.connection.execute("UPDATE jobs SET lease_expires_at='2000-01-01T00:00:00Z' WHERE id=?",(str(successor.generation_job_id),))
            return value
        monkeypatch.setattr(child,"verify",expensive_verify_then_expire)
        return original(child,lease)
    monkeypatch.setattr(dual.PreparedDualTalkingChild,"_execute",expire_inside_final_verification)
    monkeypatch.setattr(dual.subprocess,"Popen",lambda *_a,**_kw:pytest.fail("lost lease must not launch"))
    options={k:v for k,v in c.talking_options.items() if k not in ("data_root","staging_parent")}
    handler=MaintainedEvaluationTalkingHandler(c.db,c.root,options=options,policy=c.selection.policy)
    job=JobRepository(c.db).get(successor.generation_job_id)
    with pytest.raises(LeaseLost):
        JobRunner(JobStore(c.db),{job.type:handler},worker_id="expired-successor",lease_duration=timedelta(minutes=5),max_attempts=1).run_once(job_id=job.id)
    assert ProviderCallRepository(c.db).get_by_idempotency_key(call_key(successor)).status=="failed"
    assert c.service.records.result(successor.id,"generation") is None
    assert c.db.connection.execute("SELECT COUNT(*) FROM evaluation_talking_recoveries").fetchone()[0]==1
