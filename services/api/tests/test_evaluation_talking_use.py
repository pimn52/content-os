"""Exact-use evidence parsing/matching only; no runtime or source-quality pass."""
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4
import pytest
from app.db import Database
from app.domain.evaluation_talking import TalkingSelection, EvaluationTalkingUseReview
from app.domain.models import TalkingPerformanceBrief
from app.evaluation_talking import EvaluationTalkingService
from app.execution_admission import UseAdmissionError


def review(root):
    project,origin = uuid4(),uuid4()
    terms = root/"terms.txt"; terms.write_bytes(b"synthetic terms, not actual permission")
    selection = TalkingSelection(master_run_id=uuid4(),scene_plan_id=uuid4(),talking_profile_id=uuid4(),reference_clip_id=uuid4(),
        source_admission_id=uuid4(),capability_profile_id=uuid4(),execution_admission_id=uuid4(),authorization_reference="synthetic authority",
        brief=TalkingPerformanceBrief())
    evidence = dict(source="synthetic",version="fixture",terms_reference=terms.name,terms_sha256=hashlib.sha256(terms.read_bytes()).hexdigest(),
        applicability="synthetic exact input/output use",permits_intended_activity=True)
    value = dict(policy="evaluation-talking-run-v1",purpose="internal_evaluation",operation="transform_master_to_talking",local_only=True,
        project_id=str(project),selection=selection.model_dump(mode="json"),request_sha256="a"*64,proof={"exact":"fixture"},intended_activity="synthetic local review",
        license_evidence=[dict(evidence,admission_id=str(origin))],dependency_findings=[dict(evidence,dependency=x) for x in
            ("narration_input_use","source_person_video","talking_output_processing_review")],findings=["no actual rights issued"])
    return project,origin,value


@pytest.mark.parametrize("mutation",["old_policy","old_operation","personal_flag","integer_acceptance","foreign_receipt"])
def test_new_authority_is_not_old_receipt_or_client_flag(tmp_path,mutation):
    _,_,value = review(tmp_path)
    if mutation == "old_policy": value["policy"] = "evaluation-run-voice-master-v1"
    elif mutation == "old_operation": value["operation"] = "bind_existing_voice_master"
    elif mutation == "personal_flag": value["personal_use_approved"] = True
    elif mutation == "integer_acceptance": value["local_only"] = 1
    else: value["selection"]["use_admission_id"] = str(uuid4())
    with pytest.raises(ValueError): EvaluationTalkingUseReview.model_validate_json(json.dumps(value))


def test_report_terms_all_origins_and_each_role_are_required(tmp_path):
    project,origin,value = review(tmp_path)
    report = EvaluationTalkingUseReview.model_validate_json(json.dumps(value))
    with Database(tmp_path/"test.sqlite") as db:
        service = EvaluationTalkingService(db,tmp_path)
        context = dict(request_sha256="a"*64,proof={"exact":"fixture"},output_scope=SimpleNamespace(bindings=(SimpleNamespace(admission_id=origin),)))
        service._match(project,report,context)
        with pytest.raises(UseAdmissionError): service._match(uuid4(),report,context)
        with pytest.raises(UseAdmissionError): service._match(project,report,{**context,"proof":{"exact":"changed"}})
        with pytest.raises(UseAdmissionError,match="all_origin"):
            service._match(project,report,{**context,"output_scope":SimpleNamespace(bindings=(SimpleNamespace(admission_id=uuid4()),))})
        with pytest.raises(UseAdmissionError,match="dependency_findings"):
            service._match(project,report.model_copy(update={"dependency_findings":report.dependency_findings[:-1]}),context)
        path = tmp_path/"review.json"; path.write_bytes(report.model_dump_json().encode())
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        assert service._report(path.name,sha) == report
        (tmp_path/"terms.txt").write_bytes(b"changed")
        with pytest.raises(UseAdmissionError,match="terms_changed"): service._report(path.name,sha)
