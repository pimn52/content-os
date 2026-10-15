"""Synthetic independent review/Run integration, STUBBED retained input rights."""
from pathlib import Path
import pytest
from app.db import JobRepository, TalkingSliceSeriesRepository, AssetRepository, TalkingRunRepository
from app.domain.models import JobStatus, TalkingReviewDimensions, TalkingReviewFinding, TalkingReviewConcernAnswer
from app.execution_admission import UseAdmissionError
from app.talking.evaluation_admission import EvaluationTalkingAdmissionService
from app.talking import evaluation_admission
from app.talking.review_policy import TalkingReviewPolicyError
from app.jobs.evaluation_talking_preview_handler import require_preview_result
from test_evaluation_talking_preview import (
    preview_owned, qa_owned, call_case, ownership, qa_case, bound_job, child_case, setup, candidate, talking_case,
)


@pytest.fixture
def admission_owned(preview_owned,monkeypatch):
    c = preview_owned
    c.preview = c.preview_handler(c.preview_job)
    JobRepository(c.db).update(c.preview_job.model_copy(update={"status":JobStatus.COMPLETED}))
    c.series = TalkingSliceSeriesRepository(c.db).get(c.operation.series_id)
    monkeypatch.setattr(evaluation_admission,"EvaluationTalkingService",lambda *_a:c.service)
    c.admission = EvaluationTalkingAdmissionService(c.db,c.service.root)
    return c


def review(c,**updates):
    options = dict(approved=True,evidence_reference="fixture-only:exact preview",findings=[],
        preview_asset_id=c.preview.id,preview_sha256=c.preview.content_hash,review_policy_version=2,
        dimensions=TalkingReviewDimensions(**dict.fromkeys(
            ("visible_sync","identity","artifacts","source_performance","continuity","publishability"),"pass")))
    options.update(updates)
    return c.admission.record_review(c.series,**options)


def test_exact_six_pass_run_is_atomic_recoverable_and_parent_immutable(admission_owned):
    c = admission_owned
    parent = tuple(tuple(r) for r in c.db.connection.execute("SELECT * FROM production_runs WHERE id=?",(str(c.parent.id),)))
    approved = review(c)
    assert review(c) == approved
    run = c.admission.admit(c.series)
    assert c.admission.admit(c.series) == run
    output = AssetRepository(c.db).get(c.asset.id)
    assert "human_review_state" not in output.metadata["talking_generation"]
    preview = AssetRepository(c.db).get(c.preview.id)
    assert preview.metadata["talking_run_preview"]["state"] == "promoted"
    assert require_preview_result(c.service,c.operation,c.service.records.result(c.operation.id,"preview"))[0] == preview
    assert c.service.scopes.get("asset",preview.id) == c.context["output_scope"]
    assert c.service.scopes.get("talking_run",run.id) == c.context["output_scope"]
    assert tuple(tuple(r) for r in c.db.connection.execute("SELECT * FROM production_runs WHERE id=?",(str(c.parent.id),))) == parent
    assert len(c.calls) == 1 and len(c.mux_calls) == 1
    assert c.admission.review_status(c.series)["continuity_review_state"] == "approved"
    c.db.connection.execute("DELETE FROM execution_use_content_origins WHERE subject_kind='talking_run' AND subject_id=?",(str(run.id),))
    c.db.connection.execute("DELETE FROM execution_use_scopes WHERE subject_kind='talking_run' AND subject_id=?",(str(run.id),))
    with pytest.raises(UseAdmissionError,match="run_scope_changed"): c.admission.admit(c.series)


def test_unknown_wrong_policy_and_negative_hash_alias_veto(admission_owned):
    c = admission_owned
    dimensions = TalkingReviewDimensions(**dict.fromkeys(
        ("visible_sync","identity","artifacts","source_performance","continuity","publishability"),"unknown"))
    with pytest.raises(TalkingReviewPolicyError): review(c,dimensions=dimensions)
    with pytest.raises(ValueError): review(c,review_policy_version=1)
    rejection = review(c,approved=False,dimensions=dimensions,
        scoped_findings=[TalkingReviewFinding(dimension="artifacts",reason="fixture defect")])
    assert rejection.approved is False
    with pytest.raises(ValueError): c.admission.admit(c.series)
    # Policy hashes, not new IDs, carry the negative decision.
    assert "talking_exact_preview_previously_rejected" in c.admission.policy.blockers(c.project,c.preview.content_hash)
    assert c.admission.review_status(c.series)["continuity_review_state"] == "rejected"


def test_pending_concern_can_be_answered_after_admission_but_blocks_use(admission_owned):
    c = admission_owned
    review(c)
    run = c.admission.admit(c.series)
    concern = c.admission.register_concern(c.series,preview_asset_id=c.preview.id,preview_sha256=c.preview.content_hash,
        finding=TalkingReviewFinding(dimension="artifacts",reason="check local frame",start_ms=0,end_ms=100),
        evidence_reference="fixture-only:concern",idempotency_key="local-check")
    with pytest.raises(TalkingReviewPolicyError): c.admission.admit(c.series)
    c.admission.answer_concerns(c.series,preview_asset_id=c.preview.id,preview_sha256=c.preview.content_hash,
        answers=[TalkingReviewConcernAnswer(concern_id=concern.id,approved=True,evidence_reference="fixture-only:clear",reason="checked")])
    assert c.admission.admit(c.series) == run


def test_admission_sidecar_rollback_and_unrecorded_promotion_denied(admission_owned,monkeypatch):
    c = admission_owned
    review(c)
    save = c.service.records.save_result
    def fail(identity,role,payload):
        if role == "admission": raise ValueError("injected admission sidecar failure")
        return save(identity,role,payload)
    monkeypatch.setattr(c.service.records,"save_result",fail)
    with pytest.raises(ValueError,match="sidecar"): c.admission.admit(c.series)
    assert TalkingRunRepository(c.db).get_by_series_id(c.series.id) is None
    assert AssetRepository(c.db).get(c.preview.id) == c.preview
    metadata = dict(c.preview.metadata)
    metadata["talking_run_preview"] = dict(metadata["talking_run_preview"],state="promoted")
    forged = c.preview.model_copy(update={"metadata":metadata})
    c.db.connection.execute("UPDATE assets SET payload=? WHERE id=?",(forged.model_dump_json(),str(forged.id)))
    with pytest.raises(UseAdmissionError,match="admission_result_required"):
        require_preview_result(c.service,c.operation,c.service.records.result(c.operation.id,"preview"))


@pytest.mark.parametrize("window",["after_observation","inside_preview_validation"])
def test_late_cancel_between_observation_and_review_is_rejected(admission_owned,monkeypatch,window):
    c = admission_owned
    if window == "after_observation":
        observe = c.admission._observe
        def cancel(series):
            result = observe(series)
            c.service.cancel(c.project,c.operation.id)
            return result
        monkeypatch.setattr(c.admission,"_observe",cancel)
    else:
        validate = evaluation_admission.require_preview_result
        def cancel(*args):
            result = validate(*args)
            c.service.cancel(c.project,c.operation.id)
            return result
        monkeypatch.setattr(evaluation_admission,"require_preview_result",cancel)
    with pytest.raises(UseAdmissionError,match="authority_changed"): review(c)
    assert c.admission.reviews.get_by_series_id(c.series.id) is None
