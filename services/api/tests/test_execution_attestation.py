"""Versioned attestation seam, synthetic manifests only; no native runtime."""
import hashlib
import json
from uuid import UUID, uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from app.db import Database, JobRepository, ProviderCallRepository, ProviderMachineCapabilityProfileRepository
from app.domain.models import (ExecutionSpecification, ProviderUseAdmission,
    ProviderUseLicenseReview, ProviderUseLicenseReviewV2)
from app.execution_admission import UseAdmissionError, configuration_digest
from app.execution_scope import (ExecutionScopeService, ExecutionUseScopeV2,
    WorkerExecutionUseSnapshotV2, job_use_identity, require_evaluation_lane_closed)
from test_execution_admission import setup, headers
from test_execution_scope import _job


def specification(profile):
    def artifact(role, name, digest):
        return {"role": role, "name": name, "size_bytes": 100, "sha256": digest * 64}
    return ExecutionSpecification.model_validate_json(json.dumps({
        "schema_version": 1,
        **{key: getattr(profile, key) for key in ("capability", "mode", "provider", "model", "runtime", "machine_id")},
        "observation_recipe": "synthetic-selected-artifacts", "observation_version": 1,
        "model_artifacts": [artifact("weights", "model/weights.bin", "a"), artifact("tokenizer", "model/tokenizer.json", "b")],
        "runtime_artifacts": [artifact("interpreter", "python.exe", "c"), artifact("entrypoint", "infer.py", "d"), artifact("dependency", "runtime/module.py", "e")],
        "machine": {"installation_id": str(uuid4()), "host_sha256": "f" * 64, "device_sha256": "0" * 64},
        "parameters": {"steps": 20, "speed": 1.0, "device": "cpu", "offline": True}}))


def report_v2(case, spec):
    report = case[5].model_dump(mode="json")
    report.update(policy_version=2, identity=spec.identity.model_dump(mode="json"),
        execution_specification=spec.model_dump(mode="json"), execution_sha256=spec.execution_sha256,
        permitted_operations=["generation", "derivation", "review", "download"])
    return ProviderUseLicenseReviewV2.model_validate_json(json.dumps(report))


def adopt_v2(case, spec, key="adopt-v2"):
    client, path, project, _, _, _, _ = case
    report = report_v2(case, spec)
    evidence = path.parent / f"{key}.json"
    evidence.write_text(report.model_dump_json(), encoding="utf-8")
    payload = {"idempotency_key": key, "review_reference": evidence.name,
        "review_sha256": hashlib.sha256(evidence.read_bytes()).hexdigest()}
    response = client.post(f"/projects/{project}/provider-use-admissions", json=payload, headers=headers())
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"]), report, payload


@pytest.fixture
def v2(setup):
    spec = specification(setup[3])
    receipt, report, payload = adopt_v2(setup, spec)
    return setup, spec, receipt, report, payload


def test_manifest_order_is_canonical_and_evidence_changes_remain_independent(setup):
    profile = setup[3]
    spec = specification(profile)
    raw = spec.model_dump(mode="json")
    raw["model_artifacts"].reverse()
    raw["runtime_artifacts"].reverse()
    reordered = ExecutionSpecification.model_validate_json(json.dumps(raw))
    assert reordered.execution_sha256 == spec.execution_sha256
    assert reordered.model_artifact_sha256 == spec.model_artifact_sha256
    for changes in ({"id": uuid4()}, {"quality_status": "observed"}, {"license_evidence_reference": "different-terms"}):
        changed = profile.model_copy(update=changes)
        assert configuration_digest(profile) != configuration_digest(changed)
        assert spec.execution_sha256 == reordered.execution_sha256
    assert TypeAdapter(ProviderUseLicenseReview | ProviderUseLicenseReviewV2).validate_json(setup[5].model_dump_json()).model_dump_json() == setup[5].model_dump_json()


@pytest.mark.parametrize("problem", ["absolute", "traversal", "duplicate", "missing_weights", "missing_dependency", "size_string", "size_bool", "parameter_list", "parameter_null", "nan", "version", "version_bool", "missing_version"])
def test_specification_rejects_ambiguous_or_incomplete_input(setup, problem):
    raw = specification(setup[3]).model_dump(mode="json")
    if problem in ("absolute", "traversal"):
        raw["model_artifacts"][0]["name"] = "/weights" if problem == "absolute" else "../weights"
    elif problem == "duplicate":
        raw["model_artifacts"].append(raw["model_artifacts"][0])
    elif problem == "missing_weights":
        raw["model_artifacts"] = raw["model_artifacts"][1:]
    elif problem == "missing_dependency":
        raw["runtime_artifacts"] = raw["runtime_artifacts"][:2]
    elif problem.startswith("size_"):
        raw["model_artifacts"][0]["size_bytes"] = "100" if problem == "size_string" else True
    elif problem in ("parameter_list", "parameter_null", "nan"):
        raw["parameters"]["steps"] = [20] if problem == "parameter_list" else None if problem == "parameter_null" else float("nan")
    elif problem == "version":
        raw["schema_version"] = 3
    elif problem == "version_bool":
        raw["schema_version"] = True
    else:
        del raw["schema_version"]
    with pytest.raises(ValidationError):
        ExecutionSpecification.model_validate_json(json.dumps(raw))


@pytest.mark.parametrize("problem", ["missing_version", "version_one", "version_float", "missing_digest", "bad_digest", "wrong_identity"])
def test_report_versions_cannot_silently_upgrade_or_forge_execution(setup, problem):
    raw = report_v2(setup, specification(setup[3])).model_dump(mode="json")
    if problem == "missing_version": del raw["policy_version"]
    elif problem == "version_one": raw["policy_version"] = 1
    elif problem == "version_float": raw["policy_version"] = 2.0
    elif problem == "missing_digest": del raw["execution_sha256"]
    elif problem == "bad_digest": raw["execution_sha256"] = "0" * 64
    else: raw["identity"]["model_artifact_sha256"] = "0" * 64
    with pytest.raises(ValidationError):
        TypeAdapter(ProviderUseLicenseReview | ProviderUseLicenseReviewV2).validate_json(json.dumps(raw))


def test_v2_roundtrip_replay_restart_scope_and_output_remain_closed(v2):
    case, spec, receipt, report, payload = v2
    client, path, project, profile, _, _, _ = case
    assert client.post(f"/projects/{project}/provider-use-admissions", json=payload, headers=headers()).status_code == 201
    stored = client.get(f"/projects/{project}/provider-use-admissions/{receipt}").json()
    assert ProviderUseAdmission.model_validate_json(json.dumps(stored)).review == report
    with Database(path) as db:
        service = ExecutionScopeService(db, path.parent)
        scope = service.prepare(project, purpose="internal_evaluation", admission_ids=(receipt,))
        assert isinstance(scope, ExecutionUseScopeV2)
        job = JobRepository(db).create(_job(project))
        service.pin("job", job.id, scope, operation="generation", subject_sha256=job_use_identity(job))
        service.inherit("audio", uuid4(), project_id=project, purpose=scope.purpose,
            parents=(("job", job.id),), operation="generation", content_hash="7" * 64)
    with Database(path) as db:
        service = ExecutionScopeService(db, path.parent)
        assert service.get("job", job.id) == scope
        assert service.scopes_for_hash("7" * 64)[0] == scope
        class Adapter:
            provider_name, model = profile.provider, profile.model
            def execution_use_snapshot(self):
                return WorkerExecutionUseSnapshotV2(snapshot_version=2, identity=spec.identity,
                    execution_specification=spec, execution_sha256=spec.execution_sha256)
        service.require_worker_identity(job, Adapter(), capability_profile_id=profile.id, execution_admission_id=receipt)
        with pytest.raises(UseAdmissionError, match="execution_admission_binding_required"):
            service.require_worker_identity(job, Adapter(), capability_profile_id=profile.id)
        with pytest.raises(UseAdmissionError, match="evaluation_execution_not_integrated"):
            require_evaluation_lane_closed(db, job)
        assert ProviderCallRepository(db).list_for_project(project) == []


@pytest.mark.parametrize("change", ["weights", "dependency", "device", "parameters", "recipe", "echo", "missing", "receipt"])
def test_independent_runtime_changes_and_expected_hash_echo_block(v2, change):
    case, spec, receipt, _, _ = v2
    _, path, project, profile, _, _, _ = case
    raw = spec.model_dump(mode="json")
    if change == "weights": raw["model_artifacts"][0]["sha256"] = "1" * 64
    elif change == "dependency": raw["runtime_artifacts"][2]["sha256"] = "1" * 64
    elif change == "device": raw["machine"]["device_sha256"] = "1" * 64
    elif change == "parameters": raw["parameters"]["steps"] = 21
    elif change == "recipe": raw["observation_version"] = 2
    changed = ExecutionSpecification.model_validate_json(json.dumps(raw))
    class Adapter:
        provider_name, model = profile.provider, profile.model
        def execution_use_snapshot(self):
            return {"snapshot_version": 2, "identity": changed.identity.model_dump(mode="json"),
                "execution_specification": changed.model_dump(mode="json"),
                "execution_sha256": spec.execution_sha256 if change == "echo" else changed.execution_sha256}
    if change == "echo":
        raw["parameters"]["steps"] = 21
        changed = ExecutionSpecification.model_validate_json(json.dumps(raw))
    if change == "missing": Adapter.execution_use_snapshot = None
    with Database(path) as db:
        service = ExecutionScopeService(db, path.parent)
        scope = service.prepare(project, purpose="internal_evaluation", admission_ids=(receipt,))
        job = JobRepository(db).create(_job(project))
        service.pin("job", job.id, scope, operation="generation", subject_sha256=job_use_identity(job))
        reason = "execution_admission_binding_mismatch" if change == "receipt" else "execution_runtime_identity_unavailable" if change in ("echo", "missing") else "execution_runtime_identity_changed"
        with pytest.raises(UseAdmissionError, match=reason):
            service.require_worker_identity(job, Adapter(), capability_profile_id=profile.id,
                execution_admission_id=uuid4() if change == "receipt" else receipt)
        assert ProviderCallRepository(db).list_for_project(project) == []


def test_evidence_freshness_is_checked_even_when_runtime_matches(v2):
    case, spec, receipt, _, _ = v2
    client, path, project, profile, _, _, _ = case
    with Database(path) as db:
        service = ExecutionScopeService(db, path.parent)
        scope = service.prepare(project, purpose="internal_evaluation", admission_ids=(receipt,))
        ProviderMachineCapabilityProfileRepository(db).save(profile.model_copy(update={"license_evidence_reference": "changed-license"}))
        with pytest.raises(UseAdmissionError, match="provider_use_identity_changed"):
            service.require_current(scope, project_id=project, purpose=scope.purpose, operation="generation")
        ProviderMachineCapabilityProfileRepository(db).save(profile)
    assert client.post(f"/projects/{project}/provider-use-admissions/{receipt}/revoke", json={"reason": "synthetic revoke"}, headers=headers()).status_code == 200
    with Database(path) as db:
        with pytest.raises(UseAdmissionError, match="provider_use_admission_revoked"):
            ExecutionScopeService(db, path.parent).require_current(scope, project_id=project, purpose=scope.purpose, operation="generation")


def test_multiple_same_capability_receipts_require_exact_producer(v2):
    case, spec, receipt, _, _ = v2
    _, path, project, profile, _, _, _ = case
    raw = spec.model_dump(mode="json")
    raw["parameters"]["steps"] = 21
    other_spec = ExecutionSpecification.model_validate_json(json.dumps(raw))
    other_receipt, _, _ = adopt_v2(case, other_spec, key="other-v2")
    class Adapter:
        provider_name, model = profile.provider, profile.model
        def execution_use_snapshot(self):
            return WorkerExecutionUseSnapshotV2(snapshot_version=2, identity=other_spec.identity,
                execution_specification=other_spec, execution_sha256=other_spec.execution_sha256)
    with Database(path) as db:
        service = ExecutionScopeService(db, path.parent)
        scope = service.prepare(project, purpose="internal_evaluation", admission_ids=(receipt, other_receipt))
        job = JobRepository(db).create(_job(project))
        service.pin("job", job.id, scope, operation="generation", subject_sha256=job_use_identity(job))
        service.require_worker_identity(job, Adapter(), capability_profile_id=profile.id, execution_admission_id=other_receipt)
        with pytest.raises(UseAdmissionError, match="execution_runtime_identity_changed"):
            service.require_worker_identity(job, Adapter(), capability_profile_id=profile.id, execution_admission_id=receipt)


def test_mixed_v1_v2_ancestry_keeps_old_bytes_and_revocation(v2):
    case, spec, receipt, _, payload = v2
    client, path, project, profile, _, _, legacy_payload = case
    legacy = client.post(f"/projects/{project}/provider-use-admissions", json=legacy_payload, headers=headers())
    assert legacy.status_code == 201
    old_receipt = UUID(legacy.json()["id"])
    with Database(path) as db:
        old_row = db.connection.execute("SELECT payload FROM provider_use_admissions WHERE id=?", (str(old_receipt),)).fetchone()[0]
        service = ExecutionScopeService(db, path.parent)
        scope = service.prepare(project, purpose="internal_evaluation", admission_ids=(old_receipt, receipt))
        assert len(scope.bindings) == 2 and scope.policy_version == 2
        job = JobRepository(db).create(_job(project))
        service.pin("job", job.id, scope, operation="generation", subject_sha256=job_use_identity(job))
        class Adapter:
            provider_name, model = profile.provider, profile.model
            def execution_use_snapshot(self):
                return WorkerExecutionUseSnapshotV2(snapshot_version=2, identity=spec.identity,
                    execution_specification=spec, execution_sha256=spec.execution_sha256)
        service.require_worker_identity(job, Adapter(), capability_profile_id=profile.id, execution_admission_id=receipt)
        with pytest.raises(UseAdmissionError, match="execution_attestation_upgrade_required"):
            service.require_worker_identity(job, Adapter(), capability_profile_id=profile.id, execution_admission_id=old_receipt)
        output_id = uuid4()
        inherited = service.inherit("asset", output_id, project_id=project, purpose=scope.purpose,
            parents=(("job", job.id),), operation="generation", content_hash="8" * 64)
        assert {binding.admission_id for binding in inherited.bindings} == {old_receipt, receipt}
        assert db.connection.execute("SELECT payload FROM provider_use_admissions WHERE id=?", (str(old_receipt),)).fetchone()[0] == old_row
    assert client.post(f"/projects/{project}/provider-use-admissions/{old_receipt}/revoke", json={"reason": "synthetic ancestor revoked"}, headers=headers()).status_code == 200
    with Database(path) as db:
        with pytest.raises(UseAdmissionError, match="provider_use_admission_revoked"):
            ExecutionScopeService(db, path.parent).require_content("8" * 64, project_id=project, purpose="internal_evaluation")
    replay = client.post(f"/projects/{project}/provider-use-admissions", json=legacy_payload, headers=headers())
    assert replay.status_code == 409 and replay.json()["detail"] == "provider_use_admission_revoked"


def test_v2_replay_changed_report_conflicts_and_cannot_revive_revocation(v2):
    case, spec, receipt, report, payload = v2
    client, path, project, _, _, _, _ = case
    raw = report.model_dump(mode="json")
    raw["license_version"] = "synthetic-v2-changed"
    changed = path.parent / "conflicting-report.json"
    changed.write_text(json.dumps(raw), encoding="utf-8")
    conflict = {**payload, "review_reference": changed.name,
        "review_sha256": hashlib.sha256(changed.read_bytes()).hexdigest()}
    response = client.post(f"/projects/{project}/provider-use-admissions", json=conflict, headers=headers())
    assert response.status_code == 409 and response.json()["detail"] == "provider_use_idempotency_conflict"
    assert client.post(f"/projects/{project}/provider-use-admissions/{receipt}/revoke", json={"reason": "synthetic revoked"}, headers=headers()).status_code == 200
    response = client.post(f"/projects/{project}/provider-use-admissions", json=payload, headers=headers())
    assert response.status_code == 409 and response.json()["detail"] == "provider_use_admission_revoked"


@pytest.mark.parametrize("change", ["scope_version", "binding_version", "digest", "specification"])
def test_stripped_or_tampered_v2_scope_never_becomes_legacy(v2, change):
    case, _, receipt, _, _ = v2
    _, path, project, _, _, _, _ = case
    with Database(path) as db:
        service = ExecutionScopeService(db, path.parent)
        scope = service.prepare(project, purpose="internal_evaluation", admission_ids=(receipt,))
        job = JobRepository(db).create(_job(project))
        service.pin("job", job.id, scope, operation="generation", subject_sha256=job_use_identity(job))
        raw = scope.model_dump(mode="json")
        if change == "scope_version": del raw["policy_version"]
        elif change == "binding_version": del raw["bindings"][0]["attestation_version"]
        elif change == "digest": raw["bindings"][0]["execution_sha256"] = "0" * 64
        else: del raw["bindings"][0]["execution_specification"]
        db.connection.execute("UPDATE execution_use_scopes SET payload=? WHERE subject_kind='job' AND subject_id=?", (json.dumps(raw), str(job.id)))
        with pytest.raises(UseAdmissionError, match="execution_use_scope_invalid"):
            require_evaluation_lane_closed(db, job)
        assert ProviderCallRepository(db).list_for_project(project) == []
