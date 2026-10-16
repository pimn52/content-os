"""Source identity/current observations only; synthetic installations, no child."""
import json
import hashlib
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from app.domain.maintained_runtime import MaintainedGenerationOrigin, MaintainedRuntimeOrigin
from app.domain.models import ExecutionSpecificationRecord, ExecutionSpecificationV4, ExecutionSpecificationV5, ExecutionSpecificationV6, ProviderUseLicenseReviewV2
from app.execution_admission import ProviderUseAdmissionService, UseAdmissionError
from app.execution_scope import WorkerExecutionUseSnapshotV2
from app.providers.maintained_runtime import observe_inventory
from app.providers.maintained_voice import MaintainedVoiceParameters
from app.providers.maintained_voice_prepared import (observe_voice_inventory, prepare_maintained_voice_identity,
    require_voice_origin, require_voice_specification)
from app.providers.prepared import ExecutionPreparationError
from app.runtime_inventory import RuntimeInventoryError, RuntimeInventoryStore
from test_maintained_runtime import candidate, reference
from test_maintained_voice_execution import child_case


@pytest.fixture
def generation(candidate):
    options = dict(candidate["options"])
    inventory = observe_voice_inventory(options["environment"], options["base"], max_bytes=options["max_bytes"])
    origin = {key: value for key, value in candidate["origin"].items() if key != "diagnostic_scope"}
    origin.update(origin_version=2, generation_scope="voice_clone_only", accepts_generation_runtime_trust=True,
        runtime_inventory_sha256=inventory.descriptor.sha256)
    path = options["data_root"] / "generation-origin.json"
    path.write_text(json.dumps(origin))
    options.update(origin_reference=reference(path, options["data_root"]), parameters=MaintainedVoiceParameters(seed=7).model_dump())
    return dict(candidate=candidate, options=options, origin=origin, inventory=inventory, path=path)


def prepare(case):
    if case.get("packaged_child"):
        from contextlib import nullcontext
        from types import SimpleNamespace
        from app.providers.maintained_voice_prepared import _observe
        options = case["options"]
        spec = _observe(dict(options, gpu_uuid=options.get("gpu_uuid")), packaged=True, timeout_seconds=options["timeout_seconds"])
        return nullcontext(SimpleNamespace(specification=spec))
    return prepare_maintained_voice_identity(**case["options"])


@pytest.fixture(params=["source", "child"])
def report_generation(request, generation, child_case):
    if request.param == "source": return generation
    return dict(candidate=child_case.candidate, options=child_case.options,
        origin=child_case.origin, inventory=child_case.inventory, path=child_case.origin_path, packaged_child=True)


def test_diagnostic_and_generation_origins_and_inventory_are_distinct(generation):
    c = generation; options = c["options"]
    old = c["candidate"]["origin"]
    assert MaintainedRuntimeOrigin.model_validate_json(json.dumps(old)).model_dump(mode="json") == {**old, "network_control": None}
    with pytest.raises(ValidationError): MaintainedGenerationOrigin.model_validate_json(json.dumps(old))
    with pytest.raises(ValidationError): MaintainedRuntimeOrigin.model_validate_json(json.dumps(c["origin"]))
    legacy = observe_inventory(options["environment"], options["base"], max_bytes=options["max_bytes"])
    assert legacy.descriptor == c["candidate"]["inventory"].descriptor
    assert legacy.descriptor != c["inventory"].descriptor
    assert "entry/voice_recipe.py" in {row.name for row in c["inventory"].files}
    assert "entry/probe.py" not in {row.name for row in c["inventory"].files}
    with pytest.raises((ExecutionPreparationError, RuntimeInventoryError)):
        require_voice_origin(options["data_root"], options["origin_reference"], legacy)


def test_detached_schema5_preparation_without_execute_or_probe(generation, monkeypatch):
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: pytest.fail("no child"))
    with prepare(generation) as prepared:
        spec = prepared.specification
        assert type(spec) is ExecutionSpecificationV5
        assert not hasattr(prepared, "execute") and not hasattr(prepared, "probe_import_pcm")
        assert not hasattr(prepared, "invocation")
        parsed = TypeAdapter(ExecutionSpecificationRecord).validate_json(spec.model_dump_json())
        assert parsed == spec and type(parsed) is ExecutionSpecificationV5
        with pytest.raises(ValidationError): ExecutionSpecificationV4.model_validate_json(spec.model_dump_json())
        raw = prepared.execution_use_snapshot()
        assert WorkerExecutionUseSnapshotV2.model_validate_json(json.dumps(raw)).execution_sha256 == spec.execution_sha256
        raw["execution_specification"]["parameters"]["seed"] = 999
        assert prepared.specification.parameters["seed"] == 7 and prepared.verify() == spec
    with pytest.raises(ExecutionPreparationError, match="consumed"): prepared.verify()


@pytest.mark.parametrize("change", ["recipe_copy", "runtime", "model", "source", "origin", "new_member", "operator"])
def test_changed_current_inputs_consume_retained_preparation(generation, monkeypatch, change):
    c = generation
    with prepare(c) as prepared:
        options = c["options"]
        if change == "recipe_copy": (Path(prepared._temporary.name) / "voice_recipe.py").write_bytes(b"changed")
        elif change == "runtime": (options["environment"] / "Scripts/python.exe").write_bytes(b"changed")
        elif change == "model": (options["model_root"] / "model.safetensors").write_bytes(b"changed")
        elif change == "source": c["candidate"]["source"].write_text("changed")
        elif change == "origin": c["path"].write_text("{}")
        elif change == "new_member": (options["environment"] / "extra.py").write_text("# changed")
        else: monkeypatch.delenv("CONTENT_OS_EXECUTION_ADMISSION_KEY_HASHES")
        with pytest.raises(ExecutionPreparationError, match="identity_changed"): prepared.verify()
        with pytest.raises(ExecutionPreparationError, match="consumed"): prepared.execution_use_snapshot()


@pytest.mark.parametrize("field,value", [("origin_version", True), ("origin_version", "2"),
    ("generation_scope", "imports_synthetic_pcm_only"), ("accepts_generation_runtime_trust", 1),
    ("accepts_generation_runtime_trust", False)])
def test_generation_acceptance_is_explicit_strict_and_not_diagnostic(generation, field, value):
    raw = {**generation["origin"], field: value}
    with pytest.raises(ValidationError): MaintainedGenerationOrigin.model_validate_json(json.dumps(raw))


def test_original_diagnostic_origin_cannot_prepare_generation(generation):
    c = generation
    c["options"]["origin_reference"] = c["candidate"]["options"]["origin_reference"]
    with pytest.raises(ExecutionPreparationError): prepare(c)


def test_reader_requires_expanded_generation_parameters(generation):
    with prepare(generation) as prepared:
        spec = prepared.specification
        require_voice_specification(spec)
        incomplete = spec.model_copy(update={"parameters": {key: value for key, value in spec.parameters.items() if key != "seed"}})
        with pytest.raises(ExecutionPreparationError, match="not_expanded"): require_voice_specification(incomplete)
        with pytest.raises(ExecutionPreparationError): require_voice_specification(object())


@pytest.mark.parametrize("change", ["source", "origin", "recipe", "parameters"])
def test_generation_report_reader_rechecks_generation_evidence(report_generation, monkeypatch, change):
    c = report_generation
    with prepare(c) as prepared:
        spec = prepared.specification
        data = c["options"]["data_root"]
        RuntimeInventoryStore(data).install_v2(c["inventory"])
        terms = data / "terms.txt"
        terms.write_text("Synthetic test terms only")
        def write_report(current):
            report = ProviderUseLicenseReviewV2(policy_version=2, evidence_class="operator_verified", project_id=uuid4(),
                purpose="internal_evaluation", capability_profile_id=uuid4(), configuration_sha256="a" * 64,
                identity=current.identity, intended_activity="Synthetic permission test", license_source="test",
                license_version="test", license_terms_reference="terms.txt", license_terms_sha256=hashlib.sha256(terms.read_bytes()).hexdigest(),
                permits_intended_activity=True, permitted_operations=["generation"], findings=["synthetic only"],
                execution_specification=current, execution_sha256=current.execution_sha256)
            path = data / "review.json"
            path.write_text(report.model_dump_json())
            return report, hashlib.sha256(path.read_bytes()).hexdigest()
        report, digest = write_report(spec)
        service = ProviderUseAdmissionService(None, data)
        assert service._report("review.json", digest) == report
        if change == "source": c["candidate"]["source"].write_text("changed")
        elif change == "origin": c["path"].write_bytes(c["candidate"]["origin_path"].read_bytes())
        elif change == "recipe":
            if c.get("packaged_child"):
                # A code release is no longer a change to retained model-use permission.
                monkeypatch.setattr("app.providers.maintained_voice_execution.bundle_bytes", lambda: b"changed recipe")
                assert service._report("review.json", digest) == report
                return
            owned_source = "app.providers.maintained_voice_execution.bundle_bytes" if c.get("packaged_child") else "app.providers.maintained_voice_prepared.recipe_bytes"
            monkeypatch.setattr(owned_source, lambda: b"changed recipe")
        else:
            incomplete = spec.model_copy(update={"parameters": {key: value for key, value in spec.parameters.items() if key != "seed"}})
            _, digest = write_report(incomplete)
        with pytest.raises(UseAdmissionError, match="maintained_voice_(child_)?evidence_invalid"):
            service._report("review.json", digest)


from test_execution_admission import setup


@pytest.mark.parametrize("lane", ["normal", "frozen"])
def test_generation_receipt_keeps_normal_dispatch_closed_and_reuses_frozen_ledger(generation, setup, monkeypatch, lane):
    from app.db import Database, ProviderMachineCapabilityProfileRepository, ProviderCallRepository
    from app.execution_admission import configuration_digest
    from test_execution_admission import OPERATOR_KEY
    from test_execution_attestation import adopt_v2
    from test_maintained_runtime import KEY
    monkeypatch.setenv("CONTENT_OS_EXECUTION_ADMISSION_KEY_HASHES", json.dumps({
        "operator": hashlib.sha256(OPERATOR_KEY.encode()).hexdigest(),
        "test-operator": hashlib.sha256(KEY.encode()).hexdigest()}))
    client, path, project, *_ = setup
    with prepare(generation) as prepared:
        spec = prepared.specification
        with Database(path) as db:
            repo = ProviderMachineCapabilityProfileRepository(db)
            profile = setup[3].model_copy(update={"id": uuid4(),
                **{key: getattr(spec, key) for key in ("provider", "model", "runtime", "machine_id")}})
            repo.save(profile)
        case = list(setup)
        case[3] = profile
        case[5] = setup[5].model_copy(update={"capability_profile_id": profile.id,
            "configuration_sha256": configuration_digest(profile)})
        for source in (generation["path"], generation["candidate"]["source"]):
            (path.parent / source.name).write_bytes(source.read_bytes())
        RuntimeInventoryStore(path.parent).install_v2(generation["inventory"])
        receipt, _, _ = adopt_v2(case, spec, key="schema5-test")
        if lane == "frozen":
            from types import SimpleNamespace
            from app.db import JobRepository
            from app.execution_scope import ExecutionScopeService, job_use_identity, require_evaluation_lane_closed
            from app.frozen_worker import freeze_worker_identity
            from test_execution_scope import _job
            from test_frozen_worker import reserve
            with Database(path) as db:
                service = ExecutionScopeService(db, path.parent)
                scope = service.prepare(project, purpose="internal_evaluation", admission_ids=(receipt,))
                job = JobRepository(db).create(_job(project))
                service.pin("job", job.id, scope, operation="generation", subject_sha256=job_use_identity(job))
                actual_observe = prepared.execution_use_snapshot
                observations = []
                def observe():
                    assert not db.connection.in_transaction
                    observations.append("observe")
                    return actual_observe()
                monkeypatch.setattr(prepared, "execution_use_snapshot", observe)
                actual_file = service.admissions._file
                def read_file(*args, **kwargs):
                    assert not db.connection.in_transaction
                    return actual_file(*args, **kwargs)
                monkeypatch.setattr(service.admissions, "_file", read_file)
                provider = SimpleNamespace(provider_name=profile.provider, model=profile.model)
                frozen = freeze_worker_identity(service, job, provider, prepared,
                    capability_profile_id=profile.id, execution_admission_id=receipt)
                assert frozen.prepared is prepared and observations == ["observe"]
                reservation = reserve(SimpleNamespace(db=db, job=job, provider=provider), frozen.reservation_guard)
                assert reservation.owner and observations == ["observe"]
                frozen.verify_before_launch()
                assert observations == ["observe", "observe"]
                with pytest.raises(UseAdmissionError, match="evaluation_execution_not_integrated"):
                    require_evaluation_lane_closed(db, job)
                assert not hasattr(frozen, "execute")
                assert len(ProviderCallRepository(db).list_for_project(project)) == 1
            return
        request = dict(purpose="internal_evaluation", use_admission_ids=[str(receipt)])
        preview = client.post(f"/projects/{project}/production-preflight", json=request)
        assert preview.status_code == 200 and "provider_license_scope_unverified" in preview.json()["stop_reasons"]
        response = client.post(f"/projects/{project}/production-runs", json=dict(request,
            idempotency_key="schema5-still-closed", expected_fingerprint=preview.json()["fingerprint"]))
        assert response.status_code == 409
        with Database(path) as db:
            assert db.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
            assert ProviderCallRepository(db).list_for_project(project) == []
