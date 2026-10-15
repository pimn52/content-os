"""Normal HTTP read/delivery projection; retained input authority STUBBED.

Same-thread ASGI transport uses real temp SQLite/files and fake media children.
No TestClient worker thread, native tools, network, or real app activation.
"""
import asyncio
from uuid import uuid4
import httpx
import pytest
from fastapi import FastAPI
from app.evaluation_talking_access import EvaluationTalkingAccess
from app import evaluation_talking_api
from app.execution_admission import UseAdmissionError
from app.execution_scope import _make_scope
from app.db import ProjectRepository
from app.domain.models import TalkingReviewFinding
from app.talking.planned_preview import _source_path
from test_evaluation_talking_admission import (
    admission_owned, preview_owned, qa_owned, call_case, ownership, qa_case,
    bound_job, child_case, setup, candidate, talking_case, review,
)


def request(c,monkeypatch,method,path,**kwargs):
    app = FastAPI()
    app.include_router(evaluation_talking_api.router)
    monkeypatch.setattr(evaluation_talking_api,"service",lambda *_a:c.service)
    async def send():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://fixture") as client:
            return await client.request(method,path,**kwargs)
    return asyncio.run(send())


def url(c):
    return f"/projects/{c.project}/evaluation-talking/operations/{c.operation.id}"


def test_scoped_http_preview_full_range_purpose_and_read_only_reload(admission_owned,monkeypatch):
    c = admission_owned
    before = c.db.connection.total_changes
    base = url(c)
    full = request(c,monkeypatch,"GET",base+"/preview/media?purpose=internal_evaluation")
    assert full.status_code == 200,full.text
    assert full.headers["cache-control"] == "no-store"
    part = request(c,monkeypatch,"GET",base+"/preview/media?purpose=internal_evaluation",headers={"Range":"bytes=0-9"})
    assert part.status_code == 206 and part.content == full.content[:10]
    for purpose in ("","?purpose=commercial_production","?purpose=unknown"):
        assert request(c,monkeypatch,"GET",base+"/preview/media"+purpose).status_code == 403
    for _ in range(2):
        status = request(c,monkeypatch,"GET",base).json()
        assert status["state"] == "awaiting_review" and status["automatic_retry"] is False
        assert "proof" not in status and "request" not in status
        listing = request(c,monkeypatch,"GET",base.rsplit("/",1)[0]).json()
        assert listing[0]["id"] == str(c.operation.id)
    assert c.db.connection.total_changes == before
    assert len(c.calls) == len(c.mux_calls) == 1


def test_http_cancel_preserves_history_denies_bytes_and_other_project(admission_owned,monkeypatch):
    c = admission_owned
    foreign = url(c).replace(str(c.project),str(uuid4()))
    assert request(c,monkeypatch,"GET",foreign).status_code == 409
    assert request(c,monkeypatch,"GET",foreign+"/preview/media?purpose=internal_evaluation").status_code == 409
    assert request(c,monkeypatch,"POST",url(c)+"/cancel").status_code == 200
    status = request(c,monkeypatch,"GET",url(c)).json()
    assert status["state"] == "cancelled" and not status["current_eligible"]
    assert status["stages"] == dict.fromkeys(("generation","qa","preview"),"completed")
    assert request(c,monkeypatch,"GET",url(c)+"/preview/media?purpose=internal_evaluation").status_code == 403
    assert c.service.records.result(c.operation.id,"preview") is not None
    assert len(c.calls) == len(c.mux_calls) == 1


@pytest.mark.parametrize("failure",["rights","bytes","origin","alias","negative"])
def test_retained_preview_fails_closed_without_fresh_dispatch(admission_owned,monkeypatch,failure):
    c = admission_owned
    if failure == "rights":
        def revoked(_): raise UseAdmissionError("evaluation_talking_input_authority_changed")
        monkeypatch.setattr(c.service,"require_retained_operation",revoked)
    elif failure == "bytes":
        _source_path(c.preview.source_file,c.service.root).write_bytes(b"changed fixture")
    elif failure == "origin":
        c.db.connection.execute("DELETE FROM execution_use_content_origins WHERE subject_kind='asset' AND subject_id=?",(str(c.preview.id),))
    elif failure == "alias":
        projects = ProjectRepository(c.db)
        project = projects.create(projects.get(c.project).model_copy(update={"id":uuid4()}))
        scope = _make_scope(project.id,"internal_evaluation",c.context["output_scope"].bindings)
        identity = uuid4()
        c.db.connection.execute("INSERT INTO execution_use_scopes(subject_kind,subject_id,project_id,fingerprint,payload) VALUES ('asset',?,?,?,?)",
            (str(identity),str(scope.project_id),scope.fingerprint,scope.model_dump_json()))
        c.db.connection.execute("INSERT INTO execution_use_content_origins(content_hash,subject_kind,subject_id) VALUES (?,'asset',?)",(c.preview.content_hash,str(identity)))
    else:
        review(c,approved=False,scoped_findings=[TalkingReviewFinding(dimension="artifacts",reason="fixture defect")])
    access = EvaluationTalkingAccess(c.service)
    status,path = access.preview(c.project,c.operation.id,purpose="internal_evaluation")
    assert path is None and status["state"] == "blocked"
    if failure == "alias":
        assert status["reasons"] == ["evaluation_talking_preview_origin_changed"]
    assert len(c.calls) == len(c.mux_calls) == 1


def test_promoted_preview_still_reviewable_concern_blocks_run_use(admission_owned):
    c = admission_owned
    review(c)
    run = c.admission.admit(c.series)
    access = EvaluationTalkingAccess(c.service)
    assert access.status(c.project,c.operation.id)["talking_run_id"] == str(run.id)
    c.admission.register_concern(c.series,preview_asset_id=c.preview.id,preview_sha256=c.preview.content_hash,
        finding=TalkingReviewFinding(dimension="artifacts",reason="inspect frame"),
        evidence_reference="fixture-only:concern",idempotency_key="inspect")
    status = access.status(c.project,c.operation.id)
    assert status["state"] == "blocked" and status["use_blockers"] and not status["current_eligible"]
    info,path = access.preview(c.project,c.operation.id,purpose="internal_evaluation")
    assert info["state"] == "review_allowed" and path is not None


def test_http_evidence_errors_do_not_expose_private_paths(admission_owned,monkeypatch):
    c = admission_owned
    def invalid(_): raise ValueError("private C:/operator/key")
    monkeypatch.setattr(c.service,"require_retained_operation",invalid)
    response = request(c,monkeypatch,"GET",url(c)+"/preview/access?purpose=internal_evaluation")
    assert response.json()["reasons"] == ["evaluation_talking_evidence_invalid"]
    assert "operator" not in response.text


def test_http_exact_review_admission_and_concern_answer_recovery(admission_owned,monkeypatch):
    c = admission_owned
    base = url(c)
    payload = dict(approved=True,review_policy_version=2,preview_asset_id=str(c.preview.id),
        preview_sha256=c.preview.content_hash,evidence_reference="fixture-only:explicit review",
        dimensions=dict.fromkeys(("visible_sync","identity","artifacts","source_performance","continuity","publishability"),"pass"))
    for invalid in (dict(payload,review_policy_version=1),{k:v for k,v in payload.items() if k != "review_policy_version"}):
        assert request(c,monkeypatch,"POST",base+"/review",json=invalid).status_code == 422
    assert request(c,monkeypatch,"POST",base+"/admit").status_code == 409
    result = request(c,monkeypatch,"POST",base+"/review",json=payload)
    assert result.status_code == 200,result.text
    run = request(c,monkeypatch,"POST",base+"/admit")
    assert run.status_code == 200,run.text
    assert request(c,monkeypatch,"GET",base).json()["state"] == "admitted"
    concern = dict(preview_asset_id=str(c.preview.id),preview_sha256=c.preview.content_hash,
        finding=dict(dimension="artifacts",reason="inspect frame"),idempotency_key="http-concern",
        evidence_reference="fixture-only:concern")
    registered = request(c,monkeypatch,"POST",base+"/concerns",json=concern)
    assert registered.status_code == 200,registered.text
    assert request(c,monkeypatch,"POST",base+"/admit").status_code == 409
    answers = dict(preview_asset_id=str(c.preview.id),preview_sha256=c.preview.content_hash,
        answers=[dict(concern_id=registered.json()["id"],approved=True,reason="inspected",evidence_reference="fixture-only:clear")])
    answered = request(c,monkeypatch,"POST",base+"/concern-answers",json=answers)
    assert answered.status_code == 200,answered.text
    assert request(c,monkeypatch,"POST",base+"/admit").json()["id"] == run.json()["id"]
    assert len(c.calls) == len(c.mux_calls) == 1


def test_router_is_registered_in_normal_application():
    from app.main import create_app
    paths = create_app().openapi()["paths"]
    assert "/projects/{project_id}/evaluation-talking/operations/{identity}/preview/media" in paths
    assert "/projects/{project_id}/evaluation-talking/operations/{identity}/review" in paths
