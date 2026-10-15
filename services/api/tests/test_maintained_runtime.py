"""Synthetic files/processes only; no native imports or installed-runtime adoption."""
import hashlib
import json
from pathlib import Path
import subprocess
from uuid import UUID
import wave
import struct

import pytest
from pydantic import TypeAdapter, ValidationError

from app.domain.maintained_runtime import MaintainedEvidenceReference
from app.domain.models import ExecutionSpecification, ExecutionSpecificationRecord, ExecutionSpecificationV4
from app.execution_admission import ProviderUseAdmissionService, UseAdmissionError
from app.providers.maintained_runtime import (PROBE_SOURCE, observe_inventory, prepare_maintained_omnivoice,
    require_maintained_origin)
from app.providers.prepared import ExecutionPreparationError
from app.runtime_inventory import RuntimeInventoryError, RuntimeInventoryStore

KEY = "synthetic-diagnostic-operator-key-0123456789"


def reference(path, root):
    return MaintainedEvidenceReference(reference=path.relative_to(root).as_posix(),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest())


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    data, environment, base = (tmp_path / name for name in ("data", "env", "base"))
    model, staging = data / "model", data / "work"
    for root in (model, staging, environment, base):
        root.mkdir(parents=True)
    (environment / "Scripts").mkdir()
    (environment / "Scripts/python.exe").write_bytes(b"synthetic interpreter; never executed")
    (base / "python.exe").write_bytes(b"synthetic base")
    (environment / "pyvenv.cfg").write_text(f"home = {base}\ninclude-system-site-packages = false\nversion = 3.12.14\n")
    modules = {"torch": "torch/__init__.py", "numpy": "numpy/__init__.py", "soundfile": "soundfile.py",
        "omnivoice": "omnivoice/models/omnivoice.py"}
    versions = {"torch": "2.5.1+cu121", "numpy": "2.5.3", "soundfile": "0.14.0", "omnivoice": "0.2.1"}
    site = environment / "Lib/site-packages"
    for name, module in modules.items():
        path = site / module
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"# synthetic unexecuted module\n")
        meta = site / f"{name}-{versions[name]}.dist-info/METADATA"
        meta.parent.mkdir()
        meta.write_text(f"Name: {name}\nVersion: {versions[name]}\n")
    contents = {"config.json": {"model_type": "omnivoice"},
        "tokenizer_config.json": {"tokenizer_class": "Qwen2TokenizerFast"},
        "audio_tokenizer/config.json": {"model_type": "higgs_audio_v2_tokenizer"},
        "audio_tokenizer/preprocessor_config.json": {"sampling_rate": 24000},
        "tokenizer.json": {}, "model.safetensors": b"synthetic main", "audio_tokenizer/model.safetensors": b"synthetic auxiliary"}
    for name, value in contents.items():
        path = model / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value if isinstance(value, bytes) else json.dumps(value).encode())
    source = data / "source.txt"
    source.write_text("Synthetic operator distribution evidence, not installed-runtime provenance.")
    inventory = observe_inventory(environment, base, max_bytes=1024 * 1024)
    origin = dict(trust_policy="maintained-local-process-v1", evidence_class="operator_verified", actor="test-operator",
        runtime_inventory_sha256=inventory.descriptor.sha256,
        distributions=[dict(root="base", name="cpython", version="3.12.14", publisher="synthetic", source=reference(source, data).model_dump(mode="json")),
            *(dict(root="environment", name=name, version=version, publisher="synthetic", source=reference(source, data).model_dump(mode="json")) for name, version in versions.items())],
        startup_hooks=[], accepts_local_operator_vendor_os_trust=True, no_concurrent_runtime_updates=True, network_isolation="not_asserted",
        diagnostic_scope="imports_synthetic_pcm_only")
    origin_path = data / "origin.json"
    origin_path.write_text(json.dumps(origin))
    monkeypatch.setenv("CONTENT_OS_EXECUTION_ADMISSION_KEY_HASHES", json.dumps({"test-operator": hashlib.sha256(KEY.encode()).hexdigest()}))
    options = dict(environment=environment, base=base, model_root=model, data_root=data, staging_parent=staging,
        origin_reference=reference(origin_path, data), operator_key=KEY,
        parameters=dict(device="cpu", precision="float16", audio_tokenizer_precision="float32", seed=7),
        installation_id=UUID("12345678-1234-4234-8234-123456789abc"), runtime_label="synthetic", machine_id="synthetic",
        max_bytes=1024 * 1024)
    return dict(options=options, inventory=inventory, origin=origin, origin_path=origin_path, source=source,
        versions=versions, modules={name: site / module for name, module in modules.items()})


def prepare(candidate):
    return prepare_maintained_omnivoice(**candidate["options"])


def test_oversized_installation_stops_before_hashing(candidate, monkeypatch):
    from app.providers import maintained_runtime as runtime

    monkeypatch.setattr(runtime, "MAX_MAINTAINED_RUNTIME_FILES", len(candidate["inventory"].files) - 1)
    def unexpected_hash(*_args, **_kwargs):
        pytest.fail("oversized installation must stop before reading file contents")
    monkeypatch.setattr(runtime, "_digest", unexpected_hash)
    with pytest.raises(ExecutionPreparationError, match="^maintained_inventory_limit$"):
        observe_inventory(candidate["options"]["environment"], candidate["options"]["base"],
            max_bytes=candidate["options"]["max_bytes"])


@pytest.mark.parametrize("budget", ["bytes", "manifest"])
def test_precount_byte_budgets_stop_before_hashing(candidate, monkeypatch, budget):
    from app.providers import maintained_runtime as runtime
    def unexpected_hash(*_args, **_kwargs):
        pytest.fail("oversized byte budgets must stop before hashing")
    monkeypatch.setattr(runtime, "_digest", unexpected_hash)
    maximum = candidate["options"]["max_bytes"]
    if budget == "manifest":
        monkeypatch.setattr(runtime, "MAX_RUNTIME_MANIFEST_BYTES", 100)
        code = "maintained_inventory_manifest_limit"
    else:
        maximum = 1
        code = "maintained_inventory_limit"
    with pytest.raises(ExecutionPreparationError, match="^" + code + "$"):
        observe_inventory(candidate["options"]["environment"], candidate["options"]["base"], max_bytes=maximum)


def replace_origin(candidate, change):
    origin = dict(candidate["origin"])
    origin.update(change)
    candidate["origin_path"].write_text(json.dumps(origin))
    candidate["options"]["origin_reference"] = reference(candidate["origin_path"], candidate["options"]["data_root"])


def test_schema4_and_detached_snapshot(candidate, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("must not execute"))
    with prepare(candidate) as prepared:
        raw = prepared.execution_use_snapshot()
        spec = TypeAdapter(ExecutionSpecificationRecord).validate_json(json.dumps(raw["execution_specification"]))
        assert type(spec) is ExecutionSpecificationV4
        assert spec.trust_policy == "maintained-local-process-v1"
        assert not hasattr(prepared, "execute")
        with pytest.raises(ValidationError):
            ExecutionSpecification.model_validate_json(spec.model_dump_json())
        raw["execution_specification"]["parameters"]["seed"] = 500
        assert prepared.specification.parameters["seed"] == 7
        assert prepared.verify().execution_sha256 == spec.execution_sha256


@pytest.mark.parametrize("field,value", [("schema_version", True), ("schema_version", "4"),
    ("trust_policy", "unknown"), ("observation_version", True)])
def test_ambiguous_policy_rejected(candidate, field, value):
    with prepare(candidate) as prepared:
        raw = prepared.specification.model_dump(mode="json")
        raw[field] = value
        with pytest.raises(ValidationError):
            TypeAdapter(ExecutionSpecificationRecord).validate_json(json.dumps(raw))
        raw.pop("trust_policy", None)
        with pytest.raises(ValidationError):
            TypeAdapter(ExecutionSpecificationRecord).validate_json(json.dumps(raw))


@pytest.mark.parametrize("target", ["interpreter", "base", "module", "model", "entry", "source", "extra"])
def test_changed_bytes_consume_preparation(candidate, target):
    with prepare(candidate) as prepared:
        options = candidate["options"]
        paths = {"interpreter": options["environment"] / "Scripts/python.exe", "base": options["base"] / "python.exe",
            "module": candidate["modules"]["torch"], "model": options["model_root"] / "model.safetensors",
            "entry": Path(prepared._temporary.name) / "probe.py", "source": candidate["source"],
            "extra": options["environment"] / "new-code.py"}
        path = paths[target]
        old_time = path.stat().st_mtime_ns if path.exists() else None
        payload = path.read_bytes() if path.exists() else b"new"
        path.write_bytes(b"x" * len(payload))
        if old_time is not None:
            import os
            os.utime(path, ns=(old_time, old_time))
        with pytest.raises((ExecutionPreparationError, RuntimeInventoryError)):
            prepared.verify()
        with pytest.raises(ExecutionPreparationError, match="consumed"):
            prepared.verify()


@pytest.mark.parametrize("change,reason", [({"operator_key": "wrong"}, "operator_required"),
    ({"parameters": dict(device="auto", precision="float16", audio_tokenizer_precision="float32")}, "preparation_invalid"),
    ({"parameters": dict(device="cpu", precision="float16")}, "parameters_required")])
def test_no_ambient_authority_or_parameter_fallback(candidate, change, reason):
    candidate["options"].update(change)
    with pytest.raises(ExecutionPreparationError, match=reason):
        prepare(candidate)
    assert list(candidate["options"]["staging_parent"].iterdir()) == []


def test_origin_not_a_version_string_or_hash_echo(candidate):
    replace_origin(candidate, {"evidence_class": "fixture"})
    with pytest.raises(RuntimeInventoryError, match="origin_invalid"):
        prepare(candidate)
    replace_origin(candidate, {"runtime_inventory_sha256": "0" * 64})
    with pytest.raises(RuntimeInventoryError, match="origin_invalid"):
        prepare(candidate)


def test_extra_startup_hook_and_pyc_are_observed(candidate):
    options = candidate["options"]
    hook = options["environment"] / "Lib/site-packages/extra.pth"
    hook.write_text("import unexpected_code\n")
    inventory = observe_inventory(options["environment"], options["base"], max_bytes=1024 * 1024)
    replace_origin(candidate, {"runtime_inventory_sha256": inventory.descriptor.sha256})
    with pytest.raises(RuntimeInventoryError, match="origin_invalid"):
        prepare(candidate)
    cache = options["environment"] / "__pycache__"
    cache.mkdir()
    (cache / "entry.pyc").write_bytes(b"executable bytecode")
    after = observe_inventory(options["environment"], options["base"], max_bytes=1024 * 1024)
    assert after.descriptor != inventory.descriptor


def test_even_accepted_hook_cannot_add_external_code_root(candidate, tmp_path):
    options = candidate["options"]
    outside = tmp_path / "outside"
    outside.mkdir()
    hook = options["environment"] / "Lib/site-packages/external.pth"
    hook.write_text(str(outside) + "\n")
    inventory = observe_inventory(options["environment"], options["base"], max_bytes=1024 * 1024)
    replace_origin(candidate, {"runtime_inventory_sha256": inventory.descriptor.sha256,
        "startup_hooks": ["environment/Lib/site-packages/external.pth"]})
    with pytest.raises(ExecutionPreparationError, match="startup_root_unsupported"):
        prepare(candidate)


def fake_child(candidate, monkeypatch, *, failure=None):
    calls = []
    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        private = Path(kwargs["cwd"])
        if failure != "partial":
            report = dict(candidate["versions"], python_version="3.12.14", frames=280, model_loaded=False,
                imports={"omnivoice.models.omnivoice" if name == "omnivoice" else name: str(path)
                         for name, path in candidate["modules"].items()})
            if failure == "outside":
                report["imports"]["torch"] = str(candidate["source"])
            (private / "probe-result.json").write_text(json.dumps(report))
            with wave.open(str(private / "synthetic.wav"), "wb") as output:
                output.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                output.writeframes(struct.pack("<280h", *([-32768, -16384, -1, 0, 1, 16384, 32767] * 40)))
        class Child:
            pid = 123456
            def wait(self, timeout):
                if failure == "timeout":
                    raise subprocess.TimeoutExpired(argv, timeout)
                return 1 if failure == "nonzero" else 0
            def poll(self):
                return None if failure == "timeout" else 0
        return Child()
    monkeypatch.setattr(subprocess, "Popen", popen)
    return calls


def test_single_owned_probe_no_ambient_credentials(candidate, monkeypatch):
    calls = fake_child(candidate, monkeypatch)
    monkeypatch.setenv("PYTHONPATH", "unobserved-code")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-secret")
    monkeypatch.setenv("SystemRoot", "unobserved-system")
    with prepare(candidate) as prepared:
        result = prepared.probe_import_pcm()
        assert result["model_loaded"] is False
        argv, settings = calls[0]
        assert argv[1:3] == ["-I", "-B"] and settings["shell"] is False
        assert "PYTHONPATH" not in settings["env"] and "OPENAI_API_KEY" not in settings["env"]
        assert settings["env"]["SystemRoot"] != "unobserved-system"
        with pytest.raises(ExecutionPreparationError, match="consumed"):
            prepared.probe_import_pcm()
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["partial", "nonzero", "outside", "timeout"])
def test_probe_failure_is_terminal_and_timeout_stops_owned_tree(candidate, monkeypatch, failure):
    calls = fake_child(candidate, monkeypatch, failure=failure)
    stopped = []
    with prepare(candidate) as prepared:
        monkeypatch.setattr(prepared, "_stop_tree", lambda child: stopped.append(child.pid))
        with pytest.raises(ExecutionPreparationError):
            prepared.probe_import_pcm()
        with pytest.raises(ExecutionPreparationError, match="consumed"):
            prepared.probe_import_pcm()
    assert len(calls) == 1
    assert stopped == ([123456] if failure == "timeout" else [])


def test_entry_is_owned_source_and_compiles_without_runtime():
    compile(PROBE_SOURCE, "owned-diagnostic", "exec")
    assert b"from_pretrained" not in PROBE_SOURCE


def test_legacy_serialization_and_digests_match_prechange_goldens():
    golden = json.loads((Path(__file__).parent / "fixtures/execution_schema_legacy.json").read_text(encoding="utf-8"))
    assert golden["baseline"] == "bd5ba63"
    adapter = TypeAdapter(ExecutionSpecificationRecord)
    for case in golden["cases"]:
        value = adapter.validate_json(json.dumps(case["input"]))
        assert value.model_dump_json() == case["serialized"]
        assert value.execution_sha256 == case["execution_sha256"]
        assert value.model_artifact_sha256 == case["model_artifact_sha256"]


def test_current_host_drift_consumes_preparation(candidate, monkeypatch):
    with prepare(candidate) as prepared:
        monkeypatch.setattr("app.providers.maintained_runtime.platform.version", lambda: "changed OS")
        with pytest.raises(ExecutionPreparationError, match="identity_changed"):
            prepared.verify()


def test_external_network_record_is_not_actual_enforcement(candidate):
    replace_origin(candidate, {"network_isolation": "external_control", "network_control":
        reference(candidate["source"], candidate["options"]["data_root"]).model_dump(mode="json")})
    with pytest.raises(ExecutionPreparationError, match="external_network_control_unverified"):
        prepare(candidate)


def test_explicit_cuda_observed_and_driver_drift_invalidates(candidate, monkeypatch, tmp_path):
    system = tmp_path / "os"
    (system / "System32").mkdir(parents=True)
    (system / "System32/nvidia-smi.exe").write_bytes(b"synthetic driver diagnostic")
    monkeypatch.setattr("app.providers.maintained_runtime._system_root", lambda: system)
    driver = ["999.1"]
    gpu = "GPU-12345678-1234-4234-8234-123456789abc"
    def vendor_query(argv, **kwargs):
        assert argv == [str(system / "System32/nvidia-smi.exe"),
            "--query-gpu=uuid,driver_version", "--format=csv,noheader"]
        assert kwargs == dict(shell=False, check=False, capture_output=True, timeout=10)
        return subprocess.CompletedProcess(argv, 0,
            stdout=f"{gpu}, {driver[0]}\n".encode(), stderr=b"")
    monkeypatch.setattr(subprocess, "run", vendor_query)
    candidate["options"]["parameters"]["device"] = "cuda:0"
    candidate["options"]["gpu_uuid"] = gpu
    with prepare(candidate) as prepared:
        assert prepared.specification.host_runtime.gpu_uuid == gpu
        driver[0] = "999.2"
        with pytest.raises(ExecutionPreparationError, match="identity_changed"):
            prepared.verify()


def test_unknown_cuda_identity_is_not_an_observation(candidate):
    from app.domain.maintained_runtime import MaintainedHostObservation
    with prepare(candidate) as prepared:
        raw = prepared.specification.host_runtime.model_dump(mode="json")
        raw.update(device="cuda:0", gpu_uuid="GPU-" + "-" * 36, driver_version="999.1", driver_probe_sha256="a" * 64)
        with pytest.raises(ValidationError):
            MaintainedHostObservation.model_validate_json(json.dumps(raw))


def test_alias_root_rejected(candidate, tmp_path):
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(candidate["options"]["environment"], target_is_directory=True)
    except OSError:
        pytest.skip("symlink privilege unavailable")
    candidate["options"]["environment"] = alias
    with pytest.raises(ExecutionPreparationError, match="path_invalid"):
        prepare(candidate)


def test_schema4_adoption_validates_retained_origin_bytes(candidate):
    # Exercise the shared report-reader branch without any API or real DB.
    # A fake report wrapper cannot bypass the actual TypeAdapter in _report.
    from app.domain.models import ProviderUseLicenseReviewV2
    from uuid import uuid4
    with prepare(candidate) as prepared:
        spec = prepared.specification
        data = candidate["options"]["data_root"]
        RuntimeInventoryStore(data).install_v2(observe_inventory(candidate["options"]["environment"], candidate["options"]["base"], max_bytes=1024 * 1024))
        terms = data / "terms.txt"
        terms.write_text("Synthetic test terms only")
        report = ProviderUseLicenseReviewV2(policy_version=2, evidence_class="operator_verified", project_id=uuid4(),
            purpose="internal_evaluation", capability_profile_id=uuid4(), configuration_sha256="a" * 64,
            identity=spec.identity, intended_activity="Synthetic permission test", license_source="test",
            license_version="test", license_terms_reference="terms.txt", license_terms_sha256=hashlib.sha256(terms.read_bytes()).hexdigest(),
            permits_intended_activity=True, permitted_operations=["generation"], findings=["synthetic only"],
            execution_specification=spec, execution_sha256=spec.execution_sha256)
        path = data / "review.json"
        path.write_text(report.model_dump_json())
        service = ProviderUseAdmissionService(None, data)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert service._report("review.json", digest) == report
        candidate["source"].write_text("changed")
        with pytest.raises(UseAdmissionError, match="origin_evidence_changed"):
            service._report("review.json", digest)


from test_execution_admission import setup


def test_schema4_receipt_does_not_open_normal_dispatch(candidate, setup, monkeypatch):
    from uuid import uuid4
    from app.db import Database, ProviderMachineCapabilityProfileRepository, ProviderCallRepository
    from app.execution_admission import configuration_digest
    from test_execution_admission import OPERATOR_KEY
    from test_execution_attestation import adopt_v2
    monkeypatch.setenv("CONTENT_OS_EXECUTION_ADMISSION_KEY_HASHES", json.dumps({
        "operator": hashlib.sha256(OPERATOR_KEY.encode()).hexdigest(),
        "test-operator": hashlib.sha256(KEY.encode()).hexdigest()}))
    client, path, project, *_ = setup
    with prepare(candidate) as prepared:
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
        for source in (candidate["origin_path"], candidate["source"]):
            (path.parent / source.name).write_bytes(source.read_bytes())
        RuntimeInventoryStore(path.parent).install_v2(observe_inventory(candidate["options"]["environment"], candidate["options"]["base"], max_bytes=1024 * 1024))
        receipt, _, _ = adopt_v2(case, spec, key="schema4-test")
        request = dict(purpose="internal_evaluation", use_admission_ids=[str(receipt)])
        preview = client.post(f"/projects/{project}/production-preflight", json=request)
        assert preview.status_code == 200 and "evaluation_execution_not_integrated" in preview.json()["stop_reasons"]
        response = client.post(f"/projects/{project}/production-runs", json=dict(request,
            idempotency_key="schema4-still-closed", expected_fingerprint=preview.json()["fingerprint"]))
        assert response.status_code == 409
        with Database(path) as db:
            assert db.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
            assert ProviderCallRepository(db).list_for_project(project) == []


def test_maintained_template_is_bound_without_changing_legacy_selector(candidate):
    from app.providers.omnivoice_local import select_local_omnivoice
    from app.providers.prepared import NativeExecutionUnsupported
    root = candidate["options"]["model_root"]
    template = root / "chat_template.jinja"
    template.write_text("{{ user_content }}")
    with pytest.raises(NativeExecutionUnsupported, match="artifact_layout_unsupported"):
        select_local_omnivoice(root)
    with prepare(candidate) as prepared:
        artifact = next(a for a in prepared.specification.model_artifacts if a.name == "chat_template.jinja")
        assert artifact.role == "tokenizer"
        assert artifact.sha256 == hashlib.sha256(template.read_bytes()).hexdigest()
        template.write_text("{{ changed_content }}")
        with pytest.raises(ExecutionPreparationError, match="identity_changed"):
            prepared.verify()


@pytest.mark.parametrize("change", ["extra", "remote", "tokenizer"])
def test_maintained_model_keeps_explicit_artifact_and_config_gates(candidate, change):
    root = candidate["options"]["model_root"]
    if change == "extra":
        (root / "unexplained.py").write_text("# not admitted")
    else:
        path = root / "tokenizer_config.json"
        raw = json.loads(path.read_text())
        raw["auto_map" if change == "remote" else "tokenizer_file"] = {} if change == "remote" else "../../other.json"
        path.write_text(json.dumps(raw))
    with pytest.raises(ExecutionPreparationError):
        prepare(candidate)


@pytest.mark.parametrize("case", ["dynamic_namespace", "outside_file", "relative_file"])
def test_owned_probe_checks_concrete_module_files_not_namespace_class_metadata(tmp_path, case):
    import ast
    import types
    # Execute the actual owned probe's inspection loop without importing providers.
    main = next(n for n in ast.parse(PROBE_SOURCE.decode()).body
        if isinstance(n, ast.FunctionDef) and n.name == "main")
    loop = next(n for n in main.body if isinstance(n, ast.For))
    root = tmp_path / "runtime"
    root.mkdir()
    concrete = types.ModuleType("torch._ops")
    concrete.__file__ = str(root / "_ops.py")
    class DynamicNamespace(types.ModuleType):
        __file__ = "_ops.py"
    namespace = DynamicNamespace("torch.ops")
    modules = {"torch._ops": concrete, "torch.ops": namespace}
    if case != "dynamic_namespace":
        external = types.ModuleType("external")
        external.__file__ = str(tmp_path / "outside.py") if case == "outside_file" else "outside.py"
        modules["external"] = external
    context = dict(sys=types.SimpleNamespace(modules=modules), Path=Path,
        roots=[root], imports={}, __file__=str(tmp_path / "probe.py"))
    code = compile(ast.Module(body=[loop], type_ignores=[]), "owned-module-inspection", "exec")
    if case == "dynamic_namespace":
        exec(code, context)
        assert context["imports"] == {"torch._ops": str(root / "_ops.py")}
    else:
        with pytest.raises(RuntimeError, match="maintained_import_outside_roots"):
            exec(code, context)
