"""Exact completed-output Run consumption. All provider children are synthetic."""
from datetime import datetime, timezone
import hashlib
import json
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from app.db import ProjectDraftRepository, ProviderMachineCapabilityProfileRepository, Database
from app.domain.models import ProjectDraft, SourceKind
from app.domain.evaluation_master import MasterSelection, MasterStart, MasterAdoption
from app.evaluation_master import EvaluationMasterService, POLICY
from app.voice_evaluation_review import EvaluationVoiceReviewService
from app.main import create_app
from app.execution_admission import UseAdmissionError, ProviderUseAdmissionService
from app.execution_scope import read_subject_scope
from app.production_runs import ProductionRunService, ProductionRunNotReady
from test_voice_evaluation_review import qa_case, bound_job, child_case, setup, candidate, run, body, typed, protected
from test_execution_admission import OPERATOR_KEY
from test_production_preflight import _scene


def prepare(q):
    audio, job, _ = run(q)
    reviews = EvaluationVoiceReviewService(q.c.db, q.c.root)
    reviews.record(job.project_id, audio.id, typed(body(reviews, job.project_id, audio.id)))
    scene = _scene(str(job.project_id), text=job.payload.target_text).model_copy(update={"preferred_sources": [SourceKind.TYPOGRAPHY]})
    old = ProjectDraftRepository(q.c.db).get(job.project_id)
    ProjectDraftRepository(q.c.db).save(ProjectDraft(project_id=job.project_id, version=old.version + 1, script_revision=old.script_revision + 1,
        script=job.payload.target_text, topic="same complete narration", scenes=[scene], updated_at=datetime.now(timezone.utc)))
    service = EvaluationMasterService(q.c.db, q.c.root)
    selection = MasterSelection(policy=POLICY, purpose="internal_evaluation", audio_id=audio.id)
    return service, selection, job.project_id


def admit(q, service, selection, project_id):
    preflight = service.preflight(project_id, selection)
    assert preflight["reasons"] == ["evaluation_master_use_admission_required"], preflight
    scopes = preflight["proof"]["origins"]
    terms = q.c.root / "master-terms.txt"
    terms.write_text("Synthetic permission for unchanged complete narration as a local evaluation Run Master", encoding="utf-8")
    report = dict(policy=POLICY, purpose="internal_evaluation", operation="bind_existing_voice_master", local_only=True,
        project_id=str(project_id), selection=selection.model_dump(mode="json"),
        master_use_request_sha256=preflight["master_use_request_sha256"], proof=preflight["proof"],
        intended_activity="Reference exact unchanged full audio in the saved internal evaluation Run plan",
        findings=["Synthetic operator assessment; no real rights issued"],
        license_evidence=[dict(admission_id=identity, source="synthetic", version="fixture-v1", terms_reference=terms.name,
            terms_sha256=hashlib.sha256(terms.read_bytes()).hexdigest(), applicability="All fixture components permit this exact use",
            permits_intended_activity=True) for identity in sorted({b["admission_id"] for s in scopes for b in s["bindings"]})])
    path = q.c.root / "master-use-review.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    payload = MasterAdoption(idempotency_key="adopt-master", review_reference=path.name, review_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    receipt = service.adopt(project_id, payload, OPERATOR_KEY)
    return selection.model_copy(update={"admission_id": receipt.id}), payload


def start(service, project, selection, key="master-run"):
    state = service.preflight(project, selection)
    assert state["master_stage_eligible"], state
    return MasterStart(**selection.model_dump(), idempotency_key=key, expected_fingerprint=state["fingerprint"])


def test_normal_parent_reuses_exact_source_without_reparenting_or_calls(qa_case):
    q = qa_case
    service, selection, project = prepare(q)
    before = protected(q.c.db)
    selection, adoption = admit(q, service, selection, project)
    assert service.adopt(project, adoption, OPERATOR_KEY).id == selection.admission_id
    request = start(service, project, selection)
    value = service.create(project, request)
    assert value.status == "evaluation_master_bound", value
    assert value.evaluation_master["current_eligible"] is True
    assert value.evaluation_master["whole_run_ready"] is False
    assert value.voice_job_id is None and value.voice_qa_job_id is None and value.render_job_id is None
    assert service.create(project, request).id == value.id
    assert service.create(project, request.model_copy(update={"idempotency_key": "different-key"})).id == value.id
    assert service.list_runs(project)[0].id == value.id
    assert protected(q.c.db) == before and q.calls == [1] and q.c.calls == [1]
    assert ProviderMachineCapabilityProfileRepository(q.c.db).get(q.receipt.review.capability_profile_id).quality_status == "unknown"
    with TestClient(create_app(q.c.db.path)) as client:
        result = client.get(f"/projects/{project}/production-runs/{value.id}")
        assert result.status_code == 200, result.text
        assert result.json()["evaluation_master"]["current_eligible"] is True
        for operation in ("resume", "voice-dispatch", "voice-qa-advance", "talking-dispatch", "talking-source-bind", "talking-run-admit", "voice-repair", "presentation-repair"):
            response = client.post(f"/projects/{project}/production-runs/{value.id}/{operation}", json={})
            assert response.status_code == 409, (operation, response.text)
        cancelled = client.post(f"/projects/{project}/production-runs/{value.id}/cancel")
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled", cancelled.text
    assert protected(q.c.db) == before


def test_unrelated_downstream_configuration_keeps_completed_master_current(qa_case):
    """Adding Talking configuration must not reopen an unchanged U-Voice Master."""
    from app.domain.models import ProviderMachineCapabilityProfile
    q = qa_case
    service, selection, project = prepare(q)
    selection, _ = admit(q, service, selection, project)
    request = start(service, project, selection)
    value = service.create(project, request)
    binding = service.records.binding(value.id)
    ProviderMachineCapabilityProfileRepository(q.c.db).save(ProviderMachineCapabilityProfile(
        capability="talking", mode="local", provider="latentsync", model="LatentSync-1.5",
        runtime="new-downstream-configured-runtime", machine_id="fixture-machine",
        readiness="configured", quality_status="unknown", continuity_status="unknown",
        commercial_status="non_commercial_only", provenance_source="Synthetic unrelated downstream configuration",
        evidence_reference="fixture-only", updated_at=datetime.now(timezone.utc)))
    protected_before = protected(q.c.db)
    current = service.get(project, value.id)
    assert current.evaluation_master["current_eligible"], current.evaluation_master["reasons"]
    assert service.records.binding(value.id) == binding
    assert service.preflight(project, selection)["fingerprint"] == request.expected_fingerprint
    assert service.create(project, request).id == value.id
    assert protected(q.c.db) == protected_before and q.calls == [1] and q.c.calls == [1]


def test_legacy_parent_fingerprint_is_not_silently_upgraded(qa_case):
    from app.domain.models import ProviderMachineCapabilityProfile, execution_canonical_sha256
    q = qa_case
    service, selection, project = prepare(q)
    selection, _ = admit(q, service, selection, project)
    value = service.create(project, start(service, project, selection))
    context = service._context(project, selection)
    receipt = service.records.admission(selection.admission_id)
    legacy = execution_canonical_sha256(dict(request=context["request_sha256"],
        receipt=receipt.model_dump(mode="json"), plan=context["plan"].fingerprint))
    q.c.db.connection.execute("UPDATE production_runs SET preflight_fingerprint=? WHERE id=?", (legacy, str(value.id)))
    assert service.get(project, value.id).evaluation_master["fingerprint_policy"] == "legacy-full-preflight-v1"
    ProviderMachineCapabilityProfileRepository(q.c.db).save(ProviderMachineCapabilityProfile(
        capability="talking", mode="local", provider="latentsync", model="LatentSync-1.5",
        runtime="new-downstream-configured-runtime", machine_id="fixture-machine",
        readiness="configured", provenance_source="Synthetic configuration",
        updated_at=datetime.now(timezone.utc)))
    assert not service.get(project, value.id).evaluation_master["current_eligible"]
    assert q.c.db.connection.execute("SELECT preflight_fingerprint FROM production_runs WHERE id=?", (str(value.id),)).fetchone()[0] == legacy


def test_current_receipt_revoke_denies_run_bytes_but_preserves_source_review_and_history(qa_case):
    q = qa_case; service, selection, project = prepare(q)
    selection, _ = admit(q, service, selection, project)
    request = start(service, project, selection)
    value = service.create(project, request)
    before = protected(q.c.db)
    with TestClient(create_app(q.c.db.path)) as client:
        url = f"/projects/{project}/evaluation-master/runs/{value.id}/audio"
        query = "?purpose=internal_evaluation"
        full = client.get(url + "/media" + query)
        assert full.status_code == 200 and full.headers["cache-control"] == "no-store"
        partial = client.get(url + "/media" + query, headers={"Range": "bytes=0-9"})
        assert partial.status_code == 206 and partial.content == full.content[:10]
        assert client.get(url + "/media").status_code == 403
        service.revoke(project, selection.admission_id, "explicit fixture revocation", OPERATOR_KEY)
        state = client.get(f"/projects/{project}/production-runs/{value.id}").json()
        assert state["status"] == "evaluation_master_blocked" and state["evaluation_master"]["historical_binding"]
        assert client.get(url + "/media" + query).status_code == 403
        source = client.get(f"/audio-assets/{selection.audio_id}/media?project_id={project}&purpose=internal_evaluation")
        assert source.status_code == 200  # Original review permission was not revoked.
    with pytest.raises(UseAdmissionError): service.create(project, request)
    assert protected(q.c.db) == before


def test_tamper_all_proof_roles_and_stripped_markers_never_fall_back_to_legacy(qa_case):
    q = qa_case; service, selection, project = prepare(q)
    selection, _ = admit(q, service, selection, project)
    value = service.create(project, start(service, project, selection))
    connection = q.c.db.connection
    mutations = [
        ("UPDATE production_runs SET preflight_fingerprint=? WHERE id=?", ("0" * 64, str(value.id))),
        ("UPDATE production_runs SET stage='render' WHERE id=?", (str(value.id),)),
        ("DELETE FROM evaluation_run_master_bindings WHERE run_id=?", (str(value.id),)),
        ("UPDATE evaluation_run_master_bindings SET payload_sha256=? WHERE run_id=?", ("0" * 64, str(value.id))),
        ("DELETE FROM execution_use_scopes WHERE subject_kind='run' AND subject_id=?", (str(value.id),)),
        ("DELETE FROM voice_evaluation_human_reviews WHERE audio_id=?", (str(selection.audio_id),)),
        ("DELETE FROM voice_evaluation_qa_claims WHERE audio_id=?", (str(selection.audio_id),)),
        ("UPDATE evaluation_master_use_admissions SET payload_sha256=? WHERE id=?", ("0" * 64, str(selection.admission_id))),
        ("UPDATE project_drafts SET payload=json_set(payload,'$.script','different topic') WHERE project_id=?", (str(project),)),
        ("UPDATE execution_use_scopes SET fingerprint=? WHERE subject_kind='audio' AND subject_id=?", ("0" * 64, str(selection.audio_id))),
    ]
    for statement, args in mutations:
        connection.execute("SAVEPOINT negative")
        try:
            connection.execute(statement, args)
            result = ProductionRunService(q.c.db, q.c.root).get(project, value.id)
            assert result.status == "evaluation_master_blocked", (statement, result)
            assert not result.evaluation_master["current_eligible"]
            with pytest.raises(ProductionRunNotReady): ProductionRunService(q.c.db, q.c.root).resume(project, value.id)
        finally:
            connection.execute("ROLLBACK TO negative"); connection.execute("RELEASE negative")
        assert service.get(project, value.id).evaluation_master["current_eligible"]


def test_adoption_operator_exact_license_evidence_and_no_old_receipt_shortcut(qa_case):
    q = qa_case; service, selection, project = prepare(q)
    assert service.preflight(project, selection.model_copy(update={"admission_id": q.receipt.id}))["master_stage_eligible"] is False
    selection, payload = admit(q, service, selection, project)
    for key in (None, "caller-actor", "not-configured-operator-key-0123456789"):
        with pytest.raises(UseAdmissionError, match="operator_required"): service.adopt(project, payload, key)
    path = q.c.root / payload.review_reference
    raw = path.read_bytes()
    report = json.loads(raw)
    for changed in ({**report, "operation": "generation"}, {**report, "project_id": str(uuid4())},
                    {**report, "license_evidence": report["license_evidence"][:-1]},
                    {**report, "proof": {**report["proof"], "audio_sha256": "0" * 64}}):
        path.write_text(json.dumps(changed), encoding="utf-8")
        invalid = payload.model_copy(update={"idempotency_key": str(uuid4()), "review_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        with pytest.raises(ValueError): service.adopt(project, invalid, OPERATOR_KEY)
    path.write_bytes(raw)
    terms = q.c.root / "master-terms.txt"; original = terms.read_bytes(); terms.write_bytes(b"changed terms")
    assert not service.preflight(project, selection)["master_stage_eligible"]
    terms.write_bytes(original)
    assert service.preflight(project, selection)["master_stage_eligible"]


def test_revocation_race_and_binding_failure_roll_back_entire_parent(qa_case, monkeypatch):
    q = qa_case; service, selection, project = prepare(q)
    selection, _ = admit(q, service, selection, project)
    request = start(service, project, selection)
    original = service._require_receipt
    def raced(*args):
        receipt = original(*args)
        with Database(q.c.db.path) as second:
            EvaluationMasterService(second, q.c.root).revoke(project, receipt.id, "second connection revocation", OPERATOR_KEY)
        return receipt
    monkeypatch.setattr(service, "_require_receipt", raced)
    with pytest.raises(UseAdmissionError, match="authority_changed"): service.create(project, request)
    assert q.c.db.connection.execute("SELECT COUNT(*) FROM evaluation_run_master_bindings").fetchone()[0] == 0
    assert q.c.db.connection.execute("SELECT COUNT(*) FROM production_runs").fetchone()[0] == 0


def test_equal_and_conflicting_two_connection_create_and_atomic_binding_failure(qa_case, monkeypatch):
    q = qa_case; service, selection, project = prepare(q)
    selection, _ = admit(q, service, selection, project)
    request = start(service, project, selection)
    bind = service.records.bind
    def failed(value):
        bind(value)
        raise ValueError("synthetic commit failure")
    monkeypatch.setattr(service.records, "bind", failed)
    with pytest.raises(ValueError, match="synthetic commit"): service.create(project, request)
    assert q.c.db.connection.execute("SELECT COUNT(*) FROM production_runs").fetchone()[0] == 0
    assert q.c.db.connection.execute("SELECT COUNT(*) FROM execution_use_scopes WHERE subject_kind='run'").fetchone()[0] == 0
    monkeypatch.setattr(service.records, "bind", bind)
    original = service._require_receipt; winner = []
    def competing(*args):
        receipt = original(*args)
        with Database(q.c.db.path) as second:
            winner.append(EvaluationMasterService(second, q.c.root).create(project, request.model_copy(update={"idempotency_key": "second-connection"})).id)
        return receipt
    monkeypatch.setattr(service, "_require_receipt", competing)
    assert service.create(project, request).id == winner[0]
    assert q.c.db.connection.execute("SELECT COUNT(*) FROM production_runs").fetchone()[0] == 1
    monkeypatch.setattr(service, "_require_receipt", original)
    with pytest.raises(UseAdmissionError): service.create(project, request.model_copy(update={"audio_id": uuid4()}))


def test_original_voice_and_asr_revocation_and_changed_use_receipt_cannot_override(qa_case):
    q = qa_case; service, selection, project = prepare(q)
    selection, adoption = admit(q, service, selection, project)
    value = service.create(project, start(service, project, selection))
    origins = read_subject_scope(q.c.db, "run", value.id).bindings
    assert len(origins) == 2
    for binding in origins:
        row = q.c.db.connection.execute("SELECT payload FROM provider_use_admissions WHERE id=?", (str(binding.admission_id),)).fetchone()
        ProviderUseAdmissionService(q.c.db, q.c.root).revoke(project, binding.admission_id,
            reason="synthetic original authority revocation", operator_key=OPERATOR_KEY)
        assert not service.get(project, value.id).evaluation_master["current_eligible"]
        assert not service.preflight(project, selection)["master_stage_eligible"]
        q.c.db.connection.execute("UPDATE provider_use_admissions SET payload=? WHERE id=?", (row[0], str(binding.admission_id)))
    new_receipt = service.adopt(project, adoption.model_copy(update={"idempotency_key": "another-adoption"}), OPERATOR_KEY)
    replacement = selection.model_copy(update={"admission_id": new_receipt.id})
    with pytest.raises(UseAdmissionError, match="run_conflict"): service.create(project, start(service, project, replacement))
    assert service.get(project, value.id).evaluation_master["current_eligible"]


def test_malformed_source_listing_and_tampered_preview_deny_without_server_error(qa_case):
    q = qa_case; service, selection, project = prepare(q)
    selection, _ = admit(q, service, selection, project)
    value = service.create(project, start(service, project, selection))
    connection = q.c.db.connection
    connection.execute("SAVEPOINT source_marker")
    try:
        connection.execute("UPDATE audio_assets SET payload=json_set(payload,'$.metadata.voice_generation','stripped') WHERE id=?", (str(selection.audio_id),))
        candidates = service.candidates(project)
        assert candidates[0]["text"] == "" and not candidates[0]["current_review_eligible"]
    finally:
        connection.execute("ROLLBACK TO source_marker"); connection.execute("RELEASE source_marker")
    binding = service.records.binding(value.id).model_dump(mode="json")
    binding["proof"].pop("audio_id")
    from app.domain.models import execution_canonical_sha256
    connection.execute("UPDATE evaluation_run_master_bindings SET payload=?,payload_sha256=? WHERE run_id=?",
        (json.dumps(binding), execution_canonical_sha256(binding), str(value.id)))
    with TestClient(create_app(q.c.db.path)) as client:
        assert client.get(f"/projects/{project}/evaluation-master/runs/{value.id}/audio/media?purpose=internal_evaluation").status_code == 403
    for update in ({"purpose": "commercial_production"}, {"policy": "unknown"}, {"talking_review_policy_version": True}):
        assert not service.preflight(project, selection.model_copy(update=update))["master_stage_eligible"]


def test_declared_content_requirements_are_preserved_not_replaced_with_typography(qa_case):
    q = qa_case; service, selection, project = prepare(q)
    # Synthetic narration is fixed; these are distinct visual-intent contracts,
    # not evidence that a spoken placeholder is a real tutorial or case study.
    for kind, preferred, action, expected in (
        ("opinion", SourceKind.TYPOGRAPHY, "state a point", None),
        ("operation", SourceKind.USER_ASSET, "demonstrate the actual workflow", "capture_required"),
        ("case", SourceKind.HISTORICAL_ASSET, "show the required actual evidence", "capture_required"),
    ):
        old = ProjectDraftRepository(q.c.db).get(project)
        scene = old.scenes[0].model_copy(update={"preferred_sources": [preferred], "fallback_sources": [],
            "visual_intent": old.scenes[0].visual_intent.model_copy(update={"action": action})})
        ProjectDraftRepository(q.c.db).save(old.model_copy(update={"version": old.version + 1,
            "topic": kind, "scenes": [scene], "updated_at": datetime.now(timezone.utc)}))
        result = service.preflight(project, selection)
        assert not result["whole_run_ready"] and not result["master_stage_eligible"]
        assert result["proof"]["audio_id"] == str(selection.audio_id)
        if expected:
            assert any(r.startswith(expected) for r in result["reasons"]), (kind, result)
        else:
            assert result["reasons"] == ["evaluation_master_use_admission_required"], result


def test_normal_http_selection_preflight_create_and_saved_list_not_only_direct_service(qa_case):
    q = qa_case; service, selection, project = prepare(q)
    selection, _ = admit(q, service, selection, project)
    before = protected(q.c.db)
    base = f"/projects/{project}/evaluation-master"
    with TestClient(create_app(q.c.db.path)) as client:
        assets = client.get(base + "/candidates")
        assert assets.status_code == 200 and assets.json()[0]["audio_id"] == str(selection.audio_id)
        assert client.get(base + "/admissions").json()[0]["id"] == str(selection.admission_id)
        assert client.post(base + "/preflight", json={**selection.model_dump(mode="json"), "purpose": "commercial_production"}).status_code == 422
        state = client.post(base + "/preflight", json=selection.model_dump(mode="json")).json()
        assert state["master_stage_eligible"] and not state["whole_run_ready"]
        request = dict(**selection.model_dump(mode="json"), expected_fingerprint=state["fingerprint"], idempotency_key="normal-http")
        result = client.post(base + "/runs", json=request)
        assert result.status_code == 201, result.text
        identity = result.json()["id"]
        assert client.post(base + "/runs", json=request).json()["id"] == identity
        saved = client.get(base + "/runs").json()
        assert saved[0]["id"] == identity and saved[0]["evaluation_master"]["current_eligible"]
        assert client.get(f"/projects/{uuid4()}/production-runs/{identity}").status_code == 404
    assert protected(q.c.db) == before and q.calls == [1] and q.c.calls == [1]
