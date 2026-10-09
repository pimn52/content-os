"""M1 canonical evidence only: synthetic inventories/temp DB, no native load."""
import hashlib
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from app.db import Database, JobRepository, ProviderCallRepository
from app.domain.execution_runtime import (RuntimeInventory, RuntimeInventoryFile,
    MAX_RUNTIME_FILE_BYTES, MAX_RUNTIME_FILES, MAX_RUNTIME_PATH_DEPTH)
from app.domain.models import ExecutionSpecificationRecord, ExecutionSpecificationV2
from app.execution_admission import UseAdmissionError, ProviderUseAdmissionService
from app.execution_scope import (ExecutionScopeService, ExecutionUseBindingV2,
    job_use_identity, require_evaluation_lane_closed)
from app.runtime_inventory import RuntimeInventoryError, RuntimeInventoryStore
from test_execution_admission import setup, headers
from test_execution_attestation import specification, adopt_v2
from test_execution_scope import _job


def inventory(count=1000):
    return RuntimeInventory(inventory_version=1, directories=("Lib", "Lib/pkg"), files=(
        RuntimeInventoryFile(role="interpreter", name="python.exe", size_bytes=10, sha256="a"*64),
        RuntimeInventoryFile(role="entrypoint", name="Lib/pkg/entry.py", size_bytes=10, sha256="b"*64),
        RuntimeInventoryFile(role="dependency", name="Lib/pkg/__init__.py", size_bytes=0,
            sha256=hashlib.sha256(b"").hexdigest()),
        *(RuntimeInventoryFile(role="dependency", name=f"Lib/pkg/module{i}.py", size_bytes=10,
                               sha256="c"*64) for i in range(count))))


def spec2(case, descriptor):
    raw = specification(case[3]).model_dump(mode="json")
    del raw["runtime_artifacts"]
    raw.update(schema_version=2, runtime_inventory=descriptor.model_dump(mode="json"),
        host_runtime=dict(observation_recipe="synthetic-windows-host", observation_version=1,
            trust_recipe="windows-os-selected-driver-v1", os_family="windows", architecture="amd64",
            build=26100, revision=1, device={"kind": "cpu"}))
    return ExecutionSpecificationV2.model_validate_json(json.dumps(raw))


def test_large_manifest_is_compact_stable_and_supports_empty_packages(tmp_path):
    value = inventory()
    reordered = value.model_copy(update={"files": tuple(reversed(value.files)),
                                         "directories": tuple(reversed(value.directories))})
    assert reordered.descriptor == value.descriptor
    assert value.descriptor.file_count > 512
    assert len(value.descriptor.model_dump_json()) < 500
    for name in ("one", "two"):
        root = tmp_path / name
        root.mkdir()
        store = RuntimeInventoryStore(root)
        assert store.install(reordered) == value.descriptor
        assert store.read(value.descriptor).canonical_bytes() == value.canonical_bytes()
        assert list((root / "execution/runtime-manifests/sha256").iterdir()) == [
            root / "execution/runtime-manifests/sha256" / (value.descriptor.sha256 + ".json")]


@pytest.mark.parametrize("name", ["../file", "/absolute", "C:/file", "a\\b", "a:stream", "CON", "con.txt",
    "COM1.py", "lpt¹.py", "A.", "A ", " A", "a//b", "a/./b", "a/../b", "a?b", "a\0b",
    "a\u202eb", "e\u0301.py", "/".join(["x"]*(MAX_RUNTIME_PATH_DEPTH+1))])
def test_windows_unsafe_path_rejected(name):
    with pytest.raises(ValidationError):
        RuntimeInventoryFile(role="dependency", name=name, size_bytes=1, sha256="a"*64)


@pytest.mark.parametrize("problem", ["case", "duplicate", "file_directory", "parent_missing", "parent_case",
    "interpreter_missing", "entrypoint_missing", "second_interpreter", "total_size", "wrong_empty_hash", "empty_interpreter"])
def test_membership_roles_and_totals_are_verified(problem):
    raw = inventory(1).model_dump(mode="json")
    if problem == "case": raw["files"][3]["name"] = "Lib/pkg/ENTRY.py"
    elif problem == "duplicate": raw["files"].append(raw["files"][2])
    elif problem == "file_directory": raw["directories"].append("python.exe")
    elif problem == "parent_missing": raw["directories"] = ["Lib"]
    elif problem == "parent_case": raw["directories"] = ["Lib", "Lib/Pkg"]
    elif problem == "interpreter_missing": raw["files"][0]["role"] = "dependency"
    elif problem == "entrypoint_missing": raw["files"][1]["role"] = "dependency"
    elif problem == "second_interpreter": raw["files"][1]["role"] = "interpreter"
    elif problem == "total_size": raw["files"][0]["size_bytes"] = MAX_RUNTIME_FILE_BYTES
    elif problem == "wrong_empty_hash": raw["files"][2]["sha256"] = "a"*64
    else: raw["files"][0]["size_bytes"] = 0
    with pytest.raises(ValidationError):
        RuntimeInventory.model_validate_json(json.dumps(raw))


@pytest.mark.parametrize("change", ["add", "remove", "bytes", "directory"])
def test_dependency_changes_affect_inventory_identity(change):
    value = inventory(2)
    raw = value.model_dump(mode="json")
    if change == "add": raw["files"].append({**raw["files"][-1], "name": "Lib/pkg/new.py"})
    elif change == "remove": raw["files"].pop()
    elif change == "bytes": raw["files"][-1]["sha256"] = "d"*64
    else: raw["directories"].append("empty_directory")
    assert RuntimeInventory.model_validate_json(json.dumps(raw)).descriptor != value.descriptor


def test_read_is_pure_and_missing_or_conflicting_bytes_never_repaired(tmp_path, monkeypatch):
    store = RuntimeInventoryStore(tmp_path)
    value = inventory(1)
    before = list(tmp_path.iterdir())
    with pytest.raises(RuntimeInventoryError, match="unavailable"):
        store.read(value.descriptor)
    assert list(tmp_path.iterdir()) == before
    descriptor = store.install(value)
    monkeypatch.chdir(tmp_path.parent)
    assert store.read(descriptor).descriptor == descriptor
    path = tmp_path / "execution/runtime-manifests/sha256" / (descriptor.sha256 + ".json")
    path.write_bytes(b"conflict")
    with pytest.raises(RuntimeInventoryError, match="changed"):
        store.install(value)
    assert path.read_bytes() == b"conflict"
    assert not list(path.parent.glob(".runtime-inventory-*"))


@pytest.mark.parametrize("problem", ["file_count", "directory_count", "total_file_bytes", "noncanonical", "duplicate_json_key"])
def test_false_descriptor_or_noncanonical_json_blocks(tmp_path, problem):
    value = inventory(1)
    descriptor = value.descriptor
    raw = value.canonical_bytes()
    if problem == "noncanonical": raw += b"\n"
    elif problem == "duplicate_json_key": raw = raw[:-1] + b',"inventory_version":1}'
    else: descriptor = descriptor.model_copy(update={problem: getattr(descriptor, problem) + 1})
    if problem in ("noncanonical", "duplicate_json_key"):
        descriptor = descriptor.model_copy(update={"size_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
    path = tmp_path / "execution/runtime-manifests/sha256" / (descriptor.sha256 + ".json")
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    with pytest.raises(RuntimeInventoryError, match="invalid"):
        RuntimeInventoryStore(tmp_path).read(descriptor)


def test_forged_model_copy_descriptor_cannot_escape_root(tmp_path):
    bad = inventory(1).descriptor.model_copy(update={"sha256": "../../outside"})
    with pytest.raises(RuntimeInventoryError, match="invalid"):
        RuntimeInventoryStore(tmp_path).read(bad)
    with pytest.raises(RuntimeInventoryError, match="path_invalid"):
        RuntimeInventoryStore(Path("relative"))


def test_aliased_store_directory_rejected(tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    try:
        (root / "execution").symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("Windows symlink privilege unavailable")
    with pytest.raises(RuntimeInventoryError, match="path_invalid"):
        RuntimeInventoryStore(root).install(inventory(1))
    assert list(external.iterdir()) == []


def test_failed_atomic_publication_cleans_only_own_temporary_file(tmp_path, monkeypatch):
    def fail(*_args):
        raise OSError("private path")
    monkeypatch.setattr(os, "link", fail)
    with pytest.raises(RuntimeInventoryError) as error:
        RuntimeInventoryStore(tmp_path).install(inventory(1))
    assert str(error.value) == "execution_runtime_inventory_write_failed"
    assert not list(tmp_path.rglob("*.json"))
    assert not list(tmp_path.rglob(".runtime-inventory-*"))


@pytest.mark.parametrize("problem", ["version_bool", "version_float", "missing_version", "mixed_fields", "device",
    "unknown_os", "unknown_trust", "missing_host_fact", "size_bool", "count_limit"])
def test_schema2_requires_explicit_version_and_complete_host(setup, problem):
    value = spec2(setup, inventory(1).descriptor)
    raw = value.model_dump(mode="json")
    if problem == "version_bool": raw["schema_version"] = True
    elif problem == "version_float": raw["schema_version"] = 2.0
    elif problem == "missing_version": del raw["schema_version"]
    elif problem == "mixed_fields": raw["runtime_artifacts"] = []
    elif problem == "device": raw["parameters"]["device"] = "cuda:0"
    elif problem == "unknown_os": raw["host_runtime"]["os_family"] = "unknown"
    elif problem == "unknown_trust": raw["host_runtime"]["trust_recipe"] = "trust-any-system-path"
    elif problem == "missing_host_fact": del raw["host_runtime"]["revision"]
    elif problem == "size_bool": raw["runtime_inventory"]["size_bytes"] = True
    else: raw["runtime_inventory"]["file_count"] = MAX_RUNTIME_FILES + 1
    with pytest.raises(ValidationError):
        TypeAdapter(ExecutionSpecificationRecord).validate_json(json.dumps(raw))


def test_schema2_adoption_restart_worker_compare_revocation_and_closed_lane(setup):
    client, path, project, profile, _, _, _ = setup
    value = inventory()
    descriptor = RuntimeInventoryStore(path.parent).install(value)
    spec = spec2(setup, descriptor)
    old_model_identity = specification(profile).model_artifact_sha256
    assert spec.model_artifact_sha256 == old_model_identity
    receipt, report, payload = adopt_v2(setup, spec)
    assert len(report.model_dump_json().encode()) < 100_000
    assert client.post(f"/projects/{project}/provider-use-admissions", json=payload, headers=headers()).status_code == 201
    with Database(path) as db:
        service = ExecutionScopeService(db, path.parent)
        scope = service.prepare(project, purpose="internal_evaluation", admission_ids=(receipt,))
        job = JobRepository(db).create(_job(project))
        service.pin("job", job.id, scope, operation="generation", subject_sha256=job_use_identity(job))
    class Adapter:
        provider_name, model = profile.provider, profile.model
        current_spec = spec
        def execution_use_snapshot(self):
            return {"snapshot_version": 2, "identity": self.current_spec.identity.model_dump(mode="json"),
                "execution_specification": self.current_spec.model_dump(mode="json"),
                "execution_sha256": self.current_spec.execution_sha256}
    with Database(path) as db:
        service = ExecutionScopeService(db, path.parent)
        assert service.get("job", job.id).model_dump_json() == scope.model_dump_json()
        adapter = Adapter()
        service.require_worker_identity(job, adapter, capability_profile_id=profile.id, execution_admission_id=receipt)
        changed = spec.model_dump(mode="json")
        changed["host_runtime"]["revision"] += 1
        adapter.current_spec = ExecutionSpecificationV2.model_validate_json(json.dumps(changed))
        with pytest.raises(UseAdmissionError, match="execution_runtime_identity_changed"):
            service.require_worker_identity(job, adapter, capability_profile_id=profile.id, execution_admission_id=receipt)
        with pytest.raises(UseAdmissionError, match="evaluation_execution_not_integrated"):
            require_evaluation_lane_closed(db, job)
        assert ProviderCallRepository(db).list_for_project(project) == []
    assert client.post(f"/projects/{project}/provider-use-admissions/{receipt}/revoke",
        json={"reason": "synthetic revoke"}, headers=headers()).status_code == 200
    with Database(path) as db:
        with pytest.raises(UseAdmissionError, match="provider_use_admission_revoked"):
            ExecutionScopeService(db, path.parent).require_current(scope, project_id=project,
                purpose=scope.purpose, operation="generation")


def test_adoption_requires_current_inventory_and_changes_hold_receipt(setup):
    client, path, project, _, _, _, _ = setup
    value = inventory(1)
    spec = spec2(setup, value.descriptor)
    from test_execution_attestation import report_v2
    report = report_v2(setup, spec)
    source = path.parent / "schema2-review.json"
    source.write_text(report.model_dump_json())
    payload = {"idempotency_key": "schema2", "review_reference": source.name,
               "review_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
    response = client.post(f"/projects/{project}/provider-use-admissions", json=payload, headers=headers())
    assert response.status_code == 409 and "inventory_unavailable" in response.text
    descriptor = RuntimeInventoryStore(path.parent).install(value)
    receipt, _, _ = adopt_v2(setup, spec)
    manifest = path.parent / "execution/runtime-manifests/sha256" / (descriptor.sha256 + ".json")
    manifest.write_bytes(b"changed")
    with Database(path) as db:
        service = ProviderUseAdmissionService(db, path.parent)
        assert not service.current_evidence(service.get(project, receipt))
        with pytest.raises(UseAdmissionError, match="provider_use_evidence_not_current"):
            ExecutionScopeService(db, path.parent).prepare(project, purpose="internal_evaluation", admission_ids=(receipt,))


def test_gpu_identity_requires_driver_fields_and_library_order_is_canonical(setup):
    raw = spec2(setup, inventory(1).descriptor).model_dump(mode="json")
    raw["parameters"]["device"] = "cuda:0"
    raw["host_runtime"]["device"] = dict(kind="cuda", uuid="GPU-00000000-0000-0000-0000-000000000001",
        pci_address="00000000:01:00.0", driver_version="999.1", libraries=[
            dict(role="driver", slot="driver/nvcuda.dll", size_bytes=10, sha256="a"*64),
            dict(role="probe", slot="probe/nvml.dll", size_bytes=10, sha256="b"*64)])
    first = ExecutionSpecificationV2.model_validate_json(json.dumps(raw))
    raw["host_runtime"]["device"]["libraries"].reverse()
    assert ExecutionSpecificationV2.model_validate_json(json.dumps(raw)).execution_sha256 == first.execution_sha256
    del raw["host_runtime"]["device"]["driver_version"]
    with pytest.raises(ValidationError):
        ExecutionSpecificationV2.model_validate_json(json.dumps(raw))


def test_schema2_normal_start_still_creates_no_run_job_or_call(setup):
    client, path, project, _, _, _, _ = setup
    descriptor = RuntimeInventoryStore(path.parent).install(inventory(1))
    receipt, _, _ = adopt_v2(setup, spec2(setup, descriptor))
    request = {"purpose": "internal_evaluation", "use_admission_ids": [str(receipt)]}
    preview = client.post(f"/projects/{project}/production-preflight", json=request)
    assert preview.status_code == 200, preview.text
    assert "evaluation_execution_not_integrated" in preview.json()["stop_reasons"]
    response = client.post(f"/projects/{project}/production-runs", json={**request,
        "idempotency_key": "schema2-normal", "expected_fingerprint": preview.json()["fingerprint"]})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["reasons"] == ["evaluation_execution_not_integrated"]
    with Database(path) as db:
        for table in ("production_runs", "jobs", "execution_use_scopes"):
            assert db.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert ProviderCallRepository(db).list_for_project(project) == []


def test_mixed_nested_versions_and_v1_ancestor_keep_bytes_and_revocation(setup):
    client, path, project, _, _, old_report, old_payload = setup
    legacy = client.post(f"/projects/{project}/provider-use-admissions", json=old_payload, headers=headers())
    assert legacy.status_code == 201
    old_id = UUID(legacy.json()["id"])
    schema1 = specification(setup[3])
    one_id, _, _ = adopt_v2(setup, schema1, key="schema1-parent")
    descriptor = RuntimeInventoryStore(path.parent).install(inventory(1))
    two_id, _, _ = adopt_v2(setup, spec2(setup, descriptor), key="schema2-producer")
    with Database(path) as db:
        old_bytes = db.connection.execute("SELECT payload FROM provider_use_admissions WHERE id=?", (str(old_id),)).fetchone()[0]
        service = ExecutionScopeService(db, path.parent)
        scope = service.prepare(project, purpose="internal_evaluation", admission_ids=(old_id, one_id, two_id))
        job = JobRepository(db).create(_job(project))
        service.pin("job", job.id, scope, operation="generation", subject_sha256=job_use_identity(job))
        service.inherit("asset", uuid4(), project_id=project, purpose=scope.purpose,
            parents=(("job", job.id),), operation="generation", content_hash="8" * 64)
    with Database(path) as db:
        service = ExecutionScopeService(db, path.parent)
        restored = service.get("job", job.id)
        assert restored.model_dump_json() == scope.model_dump_json()
        assert {binding.execution_specification.schema_version for binding in restored.bindings
                if isinstance(binding, ExecutionUseBindingV2)} == {1, 2}
        assert db.connection.execute("SELECT payload FROM provider_use_admissions WHERE id=?", (str(old_id),)).fetchone()[0] == old_bytes
        assert ProviderUseAdmissionService(db, path.parent).get(project, old_id).review.model_dump_json() == old_report.model_dump_json()
    assert client.post(f"/projects/{project}/provider-use-admissions/{old_id}/revoke",
        json={"reason": "synthetic ancestor revoked"}, headers=headers()).status_code == 200
    with Database(path) as db:
        with pytest.raises(UseAdmissionError, match="provider_use_admission_revoked"):
            ExecutionScopeService(db, path.parent).require_content("8" * 64, project_id=project, purpose="internal_evaluation")
