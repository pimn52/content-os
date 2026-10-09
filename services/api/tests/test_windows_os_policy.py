"""Owned v2 policy, synthetic host/files/temp admissions; no native loads."""
from dataclasses import replace
import hashlib
import json

import pytest
from pydantic import ValidationError

from app.domain.execution_runtime import (HostRuntimeObservation, OS_POLICY_SLOT, RuntimeInventory,
    RuntimeInventoryFile)
from app.providers import host_runtime as host_module
from app.providers.prepared import NativeExecutionUnsupported
from app.providers.windows_os_policy import (owned_os_policy_bytes, parse_os_policy,
    read_prepared_os_policy, OSMember, UCRT_CONTRACTS, UCRT_CONTRACT_REFERENCE,
    PATH_CONTRACT_REFERENCE)
from app.runtime_inventory import RuntimeInventoryStore
from app.domain.models import ExecutionSpecificationV2
from app.db import Database, JobRepository
from app.execution_admission import UseAdmissionError
from app.execution_scope import ExecutionScopeService, job_use_identity
from test_execution_admission import setup
from test_execution_attestation import adopt_v2, report_v2
from test_execution_scope import _job
from test_host_runtime import host
from test_runtime_inventory import inventory, spec2
from test_python_startup import layout


ADDED_NAMES = (
    "bcrypt.dll", "bcryptprimitives.dll", "combase.dll", "crypt32.dll", "gdi32.dll",
    "dbghelp.dll", "gdi32full.dll", "imm32.dll", "iphlpapi.dll", "msvcp_win.dll", "msvcrt.dll", "ole32.dll", "psapi.dll",
    "oleaut32.dll", "rpcrt4.dll", "sechost.dll", "shlwapi.dll", "ucrtbase.dll", "user32.dll",
    "userenv.dll", "win32u.dll", "ws2_32.dll",
)
SERVICING_URL = (
    "https://support.microsoft.com/en-us/servicing/os/windows/safeos-du/2026/03/"
    "kb5083482-safe-os-dynamic-update-for-windows-11-versions-24h2-and-25h2-march-26-2026"
)
UCRT_URL = "https://learn.microsoft.com/en-us/cpp/windows/universal-crt-deployment"


def encoded(raw):
    return json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"


def policy_inventory(payload=None, *, role="dependency"):
    payload = owned_os_policy_bytes() if payload is None else payload
    base = inventory(1)
    directories = tuple(sorted(set(base.directories) | {
        "Lib/site-packages", "Lib/site-packages/app", "Lib/site-packages/app/providers"}))
    return RuntimeInventory(inventory_version=1, directories=directories, files=(*base.files,
        RuntimeInventoryFile(name=OS_POLICY_SLOT, role=role, size_bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest())))


@pytest.fixture
def prepared_policy(tmp_path):
    root = tmp_path / "private"
    path = root / OS_POLICY_SLOT
    path.parent.mkdir(parents=True)
    path.write_bytes(owned_os_policy_bytes())
    return root, policy_inventory()


@pytest.fixture
def v2_host(host):
    system = host["windows"].root / "System32"
    for name in ("advapi32.dll", "version.dll", *ADDED_NAMES):
        (system / name).write_bytes(b"synthetic inbox " + name.encode())
    host["windows"] = replace(host["windows"], system_directory=system)
    return host


def witness(prepared_policy):
    root, value = prepared_policy
    return host_module.prepare_host_runtime_v2(runtime_root=root, inventory=value, device="cpu")


def test_owned_profile_canonical_bounded_finite_and_api_contracts_are_virtual():
    payload = owned_os_policy_bytes()
    value = parse_os_policy(payload)
    assert value.canonical_bytes() == payload
    assert value.component_names == {"advapi32.dll", "kernel32.dll", "kernelbase.dll", "ntdll.dll", "version.dll", *ADDED_NAMES}
    assert value.contract_names == UCRT_CONTRACTS | {'api-ms-win-core-path-l1-1-0.dll'}
    assert not value.contract_names & value.component_names
    raw = json.loads(payload)
    raw["api_contracts"] = [{"name": "api-ms-win-core-test-l1-1-0.dll",
        "evidence_url": "https://learn.microsoft.com/en-us/windows/win32/apiindex/windows-apisets"}]
    parsed = parse_os_policy(encoded(raw))
    assert "api-ms-win-core-test-l1-1-0.dll" in parsed.contract_names
    assert "api-ms-win-core-test-l1-1-0.dll" not in parsed.component_names


@pytest.mark.parametrize('name', sorted(UCRT_CONTRACTS | {'api-ms-win-core-path-l1-1-0.dll'}))
def test_owned_contracts_have_exact_reviewed_reference_not_physical_permission(name):
    policy = parse_os_policy(owned_os_policy_bytes())
    member = next(row for row in policy.api_contracts if row.name == name)
    assert member.evidence_url == (PATH_CONTRACT_REFERENCE if '-core-' in name else UCRT_CONTRACT_REFERENCE)
    assert name not in policy.component_names


@pytest.mark.parametrize('name,url', [
    ('api-ms-win-crt-private-l1-2-0.dll', UCRT_CONTRACT_REFERENCE),
    ('api-ms-win-crt-multibyte-l1-1-0.dll', UCRT_CONTRACT_REFERENCE),
    ('api-ms-win-crt-heap-l1-2-0.dll', UCRT_CONTRACT_REFERENCE),
    ('vcruntime140.dll', UCRT_CONTRACT_REFERENCE),
    ('ucrtbase.dll', UCRT_CONTRACT_REFERENCE),
    ('api-ms-win-core-path-l1-2-0.dll', PATH_CONTRACT_REFERENCE),
    ('api-ms-win-core-path-l1-1-0.dll', UCRT_CONTRACT_REFERENCE),
    ('api-ms-win-crt-heap-l1-1-0.dll', PATH_CONTRACT_REFERENCE),
])
def test_reviewed_reference_exception_is_only_exact_member_url_pair(name, url):
    with pytest.raises(ValidationError): OSMember(name=name, evidence_url=url)


@pytest.mark.parametrize('name', sorted(UCRT_CONTRACTS | {'api-ms-win-core-path-l1-1-0.dll'}))
@pytest.mark.parametrize('location', ['system', 'private'])
def test_reviewed_virtual_contract_never_admits_a_same_named_loaded_file(v2_host, prepared_policy, name, location):
    root, value = prepared_policy
    current = witness(prepared_policy)
    path = (v2_host['windows'].system_directory if location == 'system' else root) / name
    path.write_bytes(b'virtual-name shadow')
    with pytest.raises(NativeExecutionUnsupported, match='loaded_origin_unsupported'):
        current.verify_loaded_origins(runtime_root=root, inventory=value, loaded_paths=(path,))


def test_contract_policy_addition_invalidates_old_empty_contract_identity(prepared_policy):
    root, _ = prepared_policy
    old = json.loads(owned_os_policy_bytes()); old['api_contracts'] = []
    payload = encoded(old)
    assert parse_os_policy(payload).contract_names == frozenset()
    assert hashlib.sha256(payload).digest() != hashlib.sha256(owned_os_policy_bytes()).digest()
    (root / OS_POLICY_SLOT).write_bytes(payload)
    with pytest.raises(NativeExecutionUnsupported, match='identity_changed'):
        read_prepared_os_policy(runtime_root=root, inventory=policy_inventory(payload))


def test_private_contract_addition_does_not_upgrade_previous_policy(prepared_policy):
    root, _ = prepared_policy
    old = json.loads(owned_os_policy_bytes())
    old['api_contracts'] = [row for row in old['api_contracts']
        if row['name'] != 'api-ms-win-crt-private-l1-1-0.dll']
    payload = encoded(old)
    assert 'api-ms-win-crt-private-l1-1-0.dll' not in parse_os_policy(payload).contract_names
    (root / OS_POLICY_SLOT).write_bytes(payload)
    with pytest.raises(NativeExecutionUnsupported, match='identity_changed'):
        read_prepared_os_policy(runtime_root=root, inventory=policy_inventory(payload))


@pytest.mark.parametrize("problem", ["unknown", "version", "bool", "architecture", "duplicate", "order",
    "path", "wildcard", "case", "untrusted_url", "contract_as_file", "file_as_contract", "components_limit",
    "contracts_limit", "too_big", "duplicate_json", "noncanonical"])
def test_policy_rejects_unknown_ambiguous_or_unbounded_data(problem):
    raw = json.loads(owned_os_policy_bytes())
    if problem == "unknown": raw["trust_everything"] = True
    elif problem == "version": raw["policy_version"] = 2
    elif problem == "bool": raw["policy_version"] = True
    elif problem == "architecture": raw["architecture"] = "ARM64"
    elif problem == "duplicate": raw["components"].append(raw["components"][0])
    elif problem == "order": raw["components"].reverse()
    elif problem in ("path", "wildcard", "case", "contract_as_file"):
        raw["components"][0]["name"] = {"path": "../evil.dll", "wildcard": "*.dll",
            "case": "ADVAPI32.dll", "contract_as_file": "api-ms-win-test-l1-1-0.dll"}[problem]
    elif problem == "untrusted_url": raw["components"][0]["evidence_url"] = "https://learn.microsoft.com.evil/windows"
    elif problem == "file_as_contract": raw["api_contracts"] = [raw["components"][0]]
    elif problem == "components_limit": raw["components"] = raw["components"] * 33
    elif problem == "contracts_limit": raw["api_contracts"] = [raw["components"][0]] * 257
    payload = encoded(raw)
    if problem == "too_big": payload = b" " * 65537
    elif problem == "duplicate_json": payload = payload.replace(b'"policy_version":1', b'"policy_version":1,"policy_version":1')
    elif problem == "noncanonical": payload += b"\n"
    with pytest.raises(NativeExecutionUnsupported, match="os_policy_invalid"):
        parse_os_policy(payload)


@pytest.mark.parametrize("name,url", [("ucrtbase.dll", UCRT_URL), *[
    (name, SERVICING_URL) for name in ("gdi32full.dll", "msvcp_win.dll", "sechost.dll", "win32u.dll")]])
def test_reviewed_references_are_exact_name_pairs(name, url):
    member = OSMember(name=name, evidence_url=url)
    assert member.evidence_url == url
    owned = {item.name: item.evidence_url for item in parse_os_policy(owned_os_policy_bytes()).components}
    assert owned[name] == url
    with pytest.raises(ValidationError, match="os_member_evidence_invalid"):
        OSMember(name="unreviewed.dll", evidence_url=url)


@pytest.mark.parametrize("name,url", [
    ("ucrtbase.dll", UCRT_URL + "-different"),
    ("ucrtbase.dll", SERVICING_URL), ("gdi32full.dll", UCRT_URL),
    ("win32u.dll", SERVICING_URL.replace("5083482", "5059442")),
    ("vcruntime140.dll", UCRT_URL), ("msvcp140.dll", SERVICING_URL),
    ("gdi32full.dll", "https://support.microsoft.com/en-us/topic/arbitrary"),
    ("ucrtbase.dll", "https://learn.microsoft.com/en-us/cpp/windows/arbitrary"),
])
def test_unreviewed_source_or_wrong_name_is_not_reference_exception(name, url):
    with pytest.raises(ValidationError, match="os_member_evidence_invalid"):
        OSMember(name=name, evidence_url=url)


@pytest.mark.parametrize("url", [
    "http://learn.microsoft.com/en-us/windows/win32/doc",
    "https://learn.microsoft.com.evil/en-us/windows/win32/doc",
    "https://user@learn.microsoft.com/en-us/windows/win32/doc",
    "https://learn.microsoft.com:443/en-us/windows/win32/doc",
    "https://learn.microsoft.com/en-us/windows/../answers/doc",
    "https://learn.microsoft.com/en-us/windows/./win32/doc",
    "https://learn.microsoft.com/en-us/windows//win32/doc",
    "https://learn.microsoft.com/en-us/windows/%2e%2e/answers/doc",
    "https://learn.microsoft.com/en-us/windows/answers/doc",
    "https://learn.microsoft.com/en-us/windows/win32/doc?",
    "https://learn.microsoft.com/en-us/windows/win32/doc#",
    "https://learn.microsoft.com/en-us/windows/win32/doc?view=windows-11",
    "https://learn.microsoft.com/en-us/windows/win32/doc#requirements",
    "https://learn.microsoft.com/en-us/windows/win32/doc\n",
    "https://learn.microsoft.com/en-us/windows/win32/do\tc",
    "https://learn.microsoft.com/en-us/windows/win32/doc\\other",
    "https://learn.microsoft.com/en-us/windows/win32/dóc",
    UCRT_URL + "?view=msvc-170", SERVICING_URL + "#files",
])
def test_reference_parser_rejects_deceptive_or_noncanonical_urls(url):
    with pytest.raises(ValidationError, match="os_member_evidence_invalid"):
        OSMember(name="ucrtbase.dll", evidence_url=url)


@pytest.mark.parametrize("name", ADDED_NAMES)
def test_missing_added_component_blocks_preparation(v2_host, prepared_policy, name):
    (v2_host["windows"].system_directory / name).unlink()
    with pytest.raises(NativeExecutionUnsupported):
        witness(prepared_policy)


@pytest.mark.parametrize("name", ["vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll",
    "msvcr120.dll", "cuda-user.dll", "audio-vendor.dll", "unknown.dll"])
@pytest.mark.parametrize("location", ["system", "driverstore", "path"])
def test_external_or_unlisted_files_never_gain_os_membership(v2_host, prepared_policy, name, location):
    root, value = prepared_policy
    current = witness(prepared_policy)
    directory = {"system": v2_host["windows"].system_directory,
        "driverstore": v2_host["windows"].system_directory / "DriverStore",
        "path": root.parent / "PATH"}[location]
    directory.mkdir(exist_ok=True)
    path = directory / name
    path.write_bytes(b"synthetic external dependency")
    with pytest.raises(NativeExecutionUnsupported, match="loaded_origin_unsupported"):
        current.verify_loaded_origins(runtime_root=root, inventory=value, loaded_paths=(path,))


@pytest.mark.parametrize("problem", ["recipe", "version", "bool", "unknown"])
def test_v2_requires_explicit_pair_without_changing_old_payload(problem):
    old = HostRuntimeObservation(observation_recipe="legacy-synthetic", observation_version=3,
        trust_recipe="windows-os-selected-driver-v1", os_family="windows", architecture="amd64",
        build=26100, revision=1, device={"kind": "cpu"})
    assert HostRuntimeObservation.model_validate_json(old.model_dump_json()).model_dump_json() == old.model_dump_json()
    raw = old.model_dump(mode="json")
    raw.update(observation_recipe=host_module.RECIPE, observation_version=2, trust_recipe="windows-os-selected-driver-v2")
    assert HostRuntimeObservation.model_validate_json(json.dumps(raw)).observation_version == 2
    if problem == "recipe": raw["observation_recipe"] = "user-recipe"
    elif problem == "version": raw["observation_version"] = 1
    elif problem == "bool": raw["observation_version"] = True
    else: raw["trust_recipe"] = "windows-os-selected-driver-v3"
    with pytest.raises(ValidationError): HostRuntimeObservation.model_validate_json(json.dumps(raw))


@pytest.mark.parametrize("name", ["advapi32.dll", "version.dll", *ADDED_NAMES])
def test_v2_exact_inbox_origin_accepted_while_v1_still_refuses_it(v2_host, prepared_policy, name):
    root, value = prepared_policy
    current = witness(prepared_policy)
    assert current.verify().trust_recipe == "windows-os-selected-driver-v2"
    path = v2_host["windows"].system_directory / name
    current.verify_loaded_origins(runtime_root=root, inventory=value, loaded_paths=(path,))
    old = host_module.prepare_host_runtime(device="cpu")
    assert old.observation.observation_version == 1
    with pytest.raises(NativeExecutionUnsupported, match="loaded_origin_unsupported"):
        old.verify_loaded_origins(runtime_root=root, inventory=value, loaded_paths=(path,))


@pytest.mark.parametrize("name", ["advapi32.dll", "version.dll", "vcruntime140.dll", "api-ms-win-test-l1-1-0.dll"])
def test_private_unknown_and_virtual_names_do_not_become_os_origins(v2_host, prepared_policy, name):
    root, value = prepared_policy
    current = witness(prepared_policy)
    path = root / name
    path.write_bytes(b"unknown same-name library")
    with pytest.raises(NativeExecutionUnsupported, match="loaded_origin_unsupported"):
        current.verify_loaded_origins(runtime_root=root, inventory=value, loaded_paths=(path,))


@pytest.mark.parametrize("problem", ["bytes", "owned_bytes", "missing_inbox", "missing_version", "system_location", "inventory", "other_root"])
def test_v2_changes_consume_witness_before_later_use(v2_host, prepared_policy, monkeypatch, problem):
    from app.providers import windows_os_policy as policy_module
    root, value = prepared_policy
    current = witness(prepared_policy)
    if problem == "bytes": (root / OS_POLICY_SLOT).write_bytes(b"changed")
    elif problem == "owned_bytes": monkeypatch.setattr(policy_module, "owned_os_policy_bytes", lambda: b"different owned policy")
    elif problem == "missing_inbox": (v2_host["windows"].system_directory / "advapi32.dll").unlink()
    elif problem == "missing_version": (v2_host["windows"].system_directory / "version.dll").unlink()
    elif problem == "system_location": v2_host["windows"] = replace(v2_host["windows"], system_directory=root)
    elif problem == "inventory":
        value = value.model_copy(update={"files": tuple(reversed(value.files[:-1]))})
    else: root = root.parent
    with pytest.raises(NativeExecutionUnsupported):
        current.verify_loaded_origins(runtime_root=root, inventory=value,
            loaded_paths=(v2_host["windows"].root / "System32/kernel32.dll",))
    with pytest.raises(NativeExecutionUnsupported, match="witness_consumed"):
        current.verify()


def test_inventory_hash_is_not_permission_to_select_a_foreign_policy(prepared_policy):
    root, _value = prepared_policy
    raw = json.loads(owned_os_policy_bytes())
    raw["components"] = raw["components"][1:]
    payload = encoded(raw)
    (root / OS_POLICY_SLOT).write_bytes(payload)
    with pytest.raises(NativeExecutionUnsupported, match="identity_changed"):
        read_prepared_os_policy(runtime_root=root, inventory=policy_inventory(payload))


@pytest.mark.parametrize("name", ["advapi32.dll", "version.dll", *ADDED_NAMES])
def test_inventoried_same_name_os_shadow_still_rejected(v2_host, prepared_policy, name):
    root, value = prepared_policy
    path = root / name
    path.write_bytes(b"inventoried OS shadow")
    shadow = RuntimeInventoryFile(role="dependency", name=name, size_bytes=path.stat().st_size,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    value = RuntimeInventory.model_validate_json(value.model_copy(update={"files": (*value.files, shadow)}).model_dump_json())
    current = host_module.prepare_host_runtime_v2(runtime_root=root, inventory=value, device="cpu")
    with pytest.raises(NativeExecutionUnsupported, match="loaded_origin_unsupported"):
        current.verify_loaded_origins(runtime_root=root, inventory=value, loaded_paths=(path,))


def test_malformed_inventory_consumes_v2_witness(v2_host, prepared_policy):
    current = witness(prepared_policy)
    with pytest.raises(NativeExecutionUnsupported, match="loaded_origin_unsupported"):
        current.verify_loaded_origins(runtime_root=prepared_policy[0], inventory=None, loaded_paths=())
    with pytest.raises(NativeExecutionUnsupported, match="witness_consumed"):
        current.verify()


def test_v2_gpu_witness_uses_owned_profile_and_uuid_pinning(v2_host, prepared_policy, monkeypatch):
    from test_host_runtime import GPU
    root, value = prepared_policy
    seen = []
    def fake_probe(**kwargs):
        seen.append(kwargs["profile"].component_names)
        return v2_host["cuda"]
    monkeypatch.setattr(host_module, "_read_cuda_facts", fake_probe)
    current = host_module.prepare_host_runtime_v2(runtime_root=root, inventory=value,
        device="cuda:0", physical_uuid=GPU)
    assert seen == [parse_os_policy(owned_os_policy_bytes()).component_names]
    assert current.observation.device.uuid == GPU
    assert dict(current.child_environment)["CUDA_VISIBLE_DEVICES"] == GPU


def test_v2_static_advapi_imports_do_not_enable_the_native_backend(tmp_path):
    from app.domain.execution_runtime import HostDriverLibrary
    from test_pe_imports import image
    path = tmp_path / "nvcuda.dll"
    payload = bytes(image(("advapi32.dll", "kernel32.dll")))
    path.write_bytes(payload)
    descriptor = HostDriverLibrary(role="driver", slot="driver/nvcuda.dll", size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest())
    arguments = dict(windows_root=tmp_path, driver_sources=(host_module.DriverSource(descriptor.slot, path),),
        libraries=(descriptor,))
    with pytest.raises(NativeExecutionUnsupported, match="driver_import_closure_unsupported"):
        host_module._read_cuda_facts(**arguments)
    with pytest.raises(NativeExecutionUnsupported, match="cuda_backend_unsupported"):
        host_module._read_cuda_facts(**arguments, profile=parse_os_policy(owned_os_policy_bytes()))


@pytest.mark.parametrize("problem", ["missing", "role"])
def test_v2_report_cannot_be_adopted_without_required_policy_slot(setup, problem):
    client, path, project, *_ = setup
    value = inventory(1) if problem == "missing" else policy_inventory(role="entrypoint")
    RuntimeInventoryStore(path.parent).install(value)
    raw = spec2(setup, value.descriptor).model_dump(mode="json")
    raw["host_runtime"].update(trust_recipe="windows-os-selected-driver-v2",
        observation_recipe=host_module.RECIPE, observation_version=2)
    report = report_v2(setup, ExecutionSpecificationV2.model_validate_json(json.dumps(raw)))
    source = path.parent / "v2-policy-review.json"
    source.write_text(report.model_dump_json())
    # Independently configured operator, not a caller actor string.
    from test_execution_admission import headers
    response = client.post(f"/projects/{project}/provider-use-admissions", json={
        "idempotency_key": "v2-missing-policy", "review_reference": source.name,
        "review_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}, headers=headers())
    assert response.status_code == 409 and "os_policy_inventory_required" in response.text


def test_profile_change_changes_execution_digest_and_old_receipt_cannot_match(setup):
    _client, path, project, profile, *_ = setup
    predecessor = json.loads(owned_os_policy_bytes())
    predecessor["components"] = [item for item in predecessor["components"] if item["name"] not in ADDED_NAMES]
    assert len(predecessor["components"]) == 5
    value = policy_inventory(encoded(predecessor))
    RuntimeInventoryStore(path.parent).install(value)
    raw = spec2(setup, value.descriptor).model_dump(mode="json")
    raw["host_runtime"].update(trust_recipe="windows-os-selected-driver-v2",
        observation_recipe=host_module.RECIPE, observation_version=2)
    first = ExecutionSpecificationV2.model_validate_json(json.dumps(raw))
    receipt, _report, _payload = adopt_v2(setup, first, key="owned-policy")
    changed = policy_inventory()
    RuntimeInventoryStore(path.parent).install(changed)
    raw["runtime_inventory"] = changed.descriptor.model_dump(mode="json")
    second = ExecutionSpecificationV2.model_validate_json(json.dumps(raw))
    assert first.execution_sha256 != second.execution_sha256
    assert first.model_artifact_sha256 == second.model_artifact_sha256
    class Adapter:
        provider_name, model = profile.provider, profile.model
        def execution_use_snapshot(self):
            return {"snapshot_version": 2, "identity": second.identity.model_dump(mode="json"),
                "execution_specification": second.model_dump(mode="json"), "execution_sha256": second.execution_sha256}
    with Database(path) as db:
        service = ExecutionScopeService(db, path.parent)
        scope = service.prepare(project, purpose="internal_evaluation", admission_ids=(receipt,))
        job = JobRepository(db).create(_job(project))
        service.pin("job", job.id, scope, operation="generation", subject_sha256=job_use_identity(job))
        with pytest.raises(UseAdmissionError, match="execution_runtime_identity_changed"):
            service.require_worker_identity(job, Adapter(), capability_profile_id=profile.id, execution_admission_id=receipt)


@pytest.mark.parametrize("problem", [None, "policy", "inbox", "extra", "missing_policy"])
def test_command_binds_same_tree_policy_and_host_without_launch(layout, monkeypatch, problem):
    from app.providers.python_startup import prepare_python_command_v2
    from app.providers.prepared import ExecutionPreparationError
    import subprocess
    root = layout["windows_root"]
    for name in parse_os_policy(owned_os_policy_bytes()).component_names:
        (root / "System32" / name).write_bytes(b"synthetic OS binary")
    facts = host_module.WindowsFacts(root, 0x8664, 0, 26100, 1, root / "System32")
    monkeypatch.setattr(host_module.sys, "platform", "win32")
    monkeypatch.setattr(host_module, "_read_windows_facts", lambda: facts)
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: calls.append(args))
    source = layout["trees"][0].source / "site-packages/app/providers/windows_os_policy.json"
    if problem != "missing_policy": source.write_bytes(owned_os_policy_bytes())
    if problem == "missing_policy":
        with pytest.raises(NativeExecutionUnsupported, match="identity_changed"):
            prepare_python_command_v2(device="cpu", **layout)
    else:
        with prepare_python_command_v2(device="cpu", **layout) as command:
            assert command.host_observation.trust_recipe == "windows-os-selected-driver-v2"
            assert OS_POLICY_SLOT in {entry.name for entry in command.inventory.files}
            command.verify_loaded_origins((root / "System32/advapi32.dll",))
            if problem == "policy": (command.invocation.cwd / OS_POLICY_SLOT).write_bytes(b"changed")
            elif problem == "inbox": (root / "System32/advapi32.dll").unlink()
            elif problem == "extra": (command.invocation.cwd / "unlisted.dll").write_bytes(b"unknown")
            expected = "loader_lifecycle_unsupported" if problem is None else "changed"
            with pytest.raises(ExecutionPreparationError, match=expected): command.execute()
            with pytest.raises(ExecutionPreparationError, match="consumed"): command.execute()
    assert calls == []
    assert list(layout["staging_parent"].iterdir()) == []
