"""Synthetic structural/transaction tests; input resolver is explicitly stubbed.

These tests cover durable ownership only, not complete Talking admission or UI.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4
import pytest
from app.domain.evaluation_talking import TalkingSelection, TalkingStart, EvaluationTalkingUseAdmission
from app.domain.models import TalkingPerformanceBrief, ProviderUseAdmission, ProviderUseLicenseReviewV2, execution_canonical_sha256 as digest
from app.evaluation_talking import EvaluationTalkingService
from app.execution_scope import _make_scope, ExecutionUseBindingV2, read_job_scope, require_evaluation_lane_closed
from app.db import JobRepository
from app.execution_admission import UseAdmissionError
from test_evaluation_master import prepare as master_prepare, admit as master_admit, start as master_start
from test_voice_evaluation_review import qa_case, bound_job, child_case, setup, candidate
from test_maintained_talking_execution import talking_case
from app.providers.maintained_talking_execution import observe_talking


@pytest.fixture
def ownership(qa_case,talking_case,monkeypatch):
    q = qa_case
    master,selection,project = master_prepare(q)
    selection,_ = master_admit(q,master,selection,project)
    parent = master.create(project,master_start(master,project,selection))
    service = EvaluationTalkingService(q.c.db,q.c.root)
    capid,executionid,useid = uuid4(),uuid4(),uuid4()
    spec = observe_talking(talking_case.options)
    # Explicit synthetic price only; real local execution keeps unknown cost.
    from app.costs import CatalogProviderCostEstimator
    from app.domain.models import UsageCost, CostCategory
    service.cost_estimator = CatalogProviderCostEstimator({("talking",spec.provider,spec.model):
        UsageCost(category=CostCategory.TALKING,amount="0",currency="USD")})
    # Voice/ASR fixtures deliberately exhausted their earlier allowance. This
    # separate synthetic Talking fixture grants exactly one additional call;
    # it is not permission to reuse the real spent Voice/ASR grants.
    from app.db import BudgetPolicyRepository, ProviderCallRepository
    from app.domain.models import BudgetPolicy
    calls = ProviderCallRepository(q.c.db)
    for budget_project, count in ((None,len(calls.list_all())),(project,len(calls.list_for_project(project)))):
        BudgetPolicyRepository(q.c.db).save(BudgetPolicy(project_id=budget_project,max_calls=count+1,
            allow_unknown_cost=True,updated_at=datetime.now(timezone.utc)))
    # A normal V2 Talking receipt, never an ASR/Voice calibration receipt.
    report = {k:v for k,v in q.receipt.review.model_dump(mode="json").items() if k in ProviderUseLicenseReviewV2.model_fields}
    report.update(policy_version=2,capability_profile_id=str(capid),identity=spec.identity.model_dump(mode="json"),
        execution_specification=spec.model_dump(mode="json"),execution_sha256=spec.execution_sha256)
    import json
    reviewed = ProviderUseLicenseReviewV2.model_validate_json(json.dumps(report))
    execution_receipt = ProviderUseAdmission(id=executionid,project_id=project,idempotency_key="structural-talking-execution",
        review_reference="not-read-in-structural-test.json",review_sha256="a"*64,review=reviewed,actor_id="test-operator",created_at=datetime.now(timezone.utc))
    q.c.db.connection.execute("INSERT INTO provider_use_admissions(id,project_id,idempotency_key,payload) VALUES (?,?,?,?)",
        (str(executionid),str(project),execution_receipt.idempotency_key,execution_receipt.model_dump_json()))
    scope = _make_scope(project,"internal_evaluation",(ExecutionUseBindingV2(admission_id=executionid,capability_profile_id=capid,
        review_sha256="a"*64,configuration_sha256="b"*64,identity=spec.identity,attestation_version=2,execution_specification=spec,execution_sha256=spec.execution_sha256),))
    selected = TalkingSelection(master_run_id=parent.id,scene_plan_id=uuid4(),talking_profile_id=uuid4(),reference_clip_id=uuid4(),
        source_admission_id=uuid4(),capability_profile_id=capid,execution_admission_id=executionid,authorization_reference="synthetic rights",
        brief=TalkingPerformanceBrief(),use_admission_id=useid)
    request = {"fixture":"immutable request","draft":{}}
    context = dict(selection=selected,proof={"fixture":"stubbed input resolver only",
        "master_proof":{"human_record_sha256":"e"*64},"profile":{"consent":{"confirmed":True}},
        "source_asset":{"content_hash":talking_case.request.reference_sha256},
        "source_clip":{"start_ms":talking_case.request.reference_start_ms,"end_ms":talking_case.request.reference_end_ms}},
        request=request,request_sha256=digest(request),
        inference_sha256="c"*64,execution_scope=scope,output_scope=scope,facts="detached facts",
        cap=SimpleNamespace(id=capid,provider=spec.provider,model=spec.model,runtime=spec.runtime,machine_id=spec.machine_id,
            verified_parameters={"fresh_voice_talking_duration_ms":2000}),audio=SimpleNamespace(id=selection.audio_id),
        profile=SimpleNamespace(consent=SimpleNamespace(model_dump=lambda **_kw:{"confirmed":True})),
        clip=SimpleNamespace(start_ms=0,end_ms=3000),slice_plan=SimpleNamespace(start_segment_index=0,end_segment_index=1))
    receipt = SimpleNamespace(id=useid,review_sha256="d"*64,review_reference="stubbed-not-read.json",model_dump=lambda **_kw:{"fixture":"receipt"})
    monkeypatch.setattr(service,"_context",lambda *_a,**_kw:context)
    monkeypatch.setattr(service,"_receipt",lambda *_a:receipt)
    monkeypatch.setattr(service,"_facts",lambda *_a:"detached facts")
    # Retain a typed receipt row solely to satisfy the SQLite FK. Its authority
    # is not consulted by these explicitly structural tests.
    q.c.db.connection.execute("INSERT INTO evaluation_talking_use_admissions(id,project_id,idempotency_key,payload_sha256,payload) VALUES (?,?,?,?,?)",
        (str(useid),str(project),"stubbed-receipt","d"*64,"{}"))
    monkeypatch.setattr(service.records,"admission",lambda _id:receipt)
    payload = TalkingStart(**selected.model_dump(),idempotency_key="synthetic-talking",expected_fingerprint=digest(dict(request=context["request_sha256"],receipt=receipt.model_dump())))
    return SimpleNamespace(service=service,project=project,payload=payload,parent=parent,db=q.c.db,context=context)


def test_atomic_ownership_once_only_keys_and_execution_role(ownership):
    c = ownership
    before = tuple(tuple(r) for r in c.db.connection.execute("SELECT * FROM production_runs WHERE id=?",(str(c.parent.id),)))
    value = c.service.create(c.project,c.payload)
    assert c.service.create(c.project,c.payload.model_copy(update={"idempotency_key":"another-key"})) == value
    job = JobRepository(c.db).get(value.generation_job_id)
    assert read_job_scope(c.db,job) == c.context["execution_scope"]
    assert all(b.identity.capability == "talking" for b in read_job_scope(c.db,job).bindings)
    with pytest.raises(UseAdmissionError): require_evaluation_lane_closed(c.db,job)
    assert tuple(tuple(r) for r in c.db.connection.execute("SELECT * FROM production_runs WHERE id=?",(str(c.parent.id),))) == before


@pytest.mark.parametrize("mutation",["operation","marker","scope","job_identity","role","payload"])
def test_individual_stripped_owned_markers_fail_closed(ownership,mutation):
    c = ownership
    value = c.service.create(c.project,c.payload)
    job = JobRepository(c.db).get(value.generation_job_id)
    sql,args = {
        "operation":("DELETE FROM evaluation_talking_operations WHERE id=?",(str(value.id),)),
        "marker":("DELETE FROM evaluation_talking_owned_jobs WHERE job_id=?",(str(job.id),)),
        "scope":("DELETE FROM execution_use_scopes WHERE subject_kind='job' AND subject_id=?",(str(job.id),)),
        "job_identity":("UPDATE evaluation_talking_owned_jobs SET job_sha256=? WHERE job_id=?",("0"*64,str(job.id))),
        "role":("UPDATE evaluation_talking_owned_jobs SET role='qa' WHERE job_id=?",(str(job.id),)),
        "payload":("UPDATE evaluation_talking_operations SET payload_sha256=? WHERE id=?",("0"*64,str(value.id))),
    }[mutation]
    c.db.connection.execute(sql,args)
    with pytest.raises(ValueError): read_job_scope(c.db,job)
    with pytest.raises(ValueError): require_evaluation_lane_closed(c.db,job)


def test_concurrent_fact_change_and_insert_failure_roll_back(ownership,monkeypatch):
    c = ownership
    monkeypatch.setattr(c.service,"_facts",lambda *_a:"changed facts")
    with pytest.raises(UseAdmissionError,match="authority_changed"): c.service.create(c.project,c.payload)
    assert c.db.connection.execute("SELECT count(*) FROM evaluation_talking_operations").fetchone()[0] == 0
    monkeypatch.setattr(c.service,"_facts",lambda *_a:"detached facts")
    monkeypatch.setattr(c.service,"require_job_identity",lambda *_a:(_ for _ in ()).throw(ValueError("injected final pin failure")))
    with pytest.raises(ValueError,match="pin failure"): c.service.create(c.project,c.payload)
    assert c.db.connection.execute("SELECT count(*) FROM evaluation_talking_operations").fetchone()[0] == 0
    assert c.db.connection.execute("SELECT count(*) FROM evaluation_talking_owned_jobs").fetchone()[0] == 0
