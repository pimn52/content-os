"""Owned local ASR preparation and once-only child, reusing maintained inventory."""
import hashlib
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from uuid import UUID
from app.domain.asr_child import ASRChildRequest, ASRChildResult
from app.domain.asr_recipe import ASR_PARAMETERS, ASR_MODEL_FILES, validate_local_features
from app.domain.maintained_runtime import MaintainedASROrigin
from app.domain.models import (ExecutionArtifact, ExecutionMachineObservation, ExecutionSpecificationV7,
    ExecutionSpecificationV8, ASRNoSiteStartup, execution_canonical_sha256)
from app.execution_admission import configured_admission_actor
from .maintained_runtime import (_observe_inventory, _require_origin, _check_startup_roots,
    _distribution_versions, observe_host, _system_root, stop_maintained_process_tree)
from .maintained_voice_bundle import BUNDLE_SLOT, bundle_bytes
from .prepared import ExecutionPreparationError, PendingInvocation
from .runtime_tree import _exact_path, _digest

ASR_MAIN = b"from app.providers.maintained_asr_child import main\nraise SystemExit(main())\n"

def asr_bundle_bytes(invocation_policy="isolated-zip-asr-v1"):
    if invocation_policy == "isolated-zip-asr-v1":
        return bundle_bytes(ASR_MAIN)
    if invocation_policy != "isolated-zip-asr-v2":
        raise ExecutionPreparationError("maintained_asr_recipe_invalid")
    source = Path(__file__).with_name("asr_no_site_bootstrap.py")
    _exact_path(source, directory=False)
    return bundle_bytes(source.read_bytes())


def _bundle(policy):
    # Preserve the existing v1 entrypoint and its testing/validation seam.
    return asr_bundle_bytes() if policy == "isolated-zip-asr-v1" else asr_bundle_bytes(policy)

def observe_asr_inventory(environment, base, *, max_bytes=64*1024**3, invocation_policy="isolated-zip-asr-v1"):
    return _observe_inventory(environment, base, max_bytes=max_bytes,
        entry_name=BUNDLE_SLOT, entry_source=_bundle(invocation_policy))

def require_asr_origin(root, reference, inventory, invocation_policy="isolated-zip-asr-v1"):
    origin = _require_origin(root, reference, inventory, MaintainedASROrigin)
    rows = [r for r in inventory.files if r.role == "entrypoint"]
    raw = _bundle(invocation_policy)
    if (len(rows) != 1 or rows[0].name != BUNDLE_SLOT or rows[0].size_bytes != len(raw)
        or rows[0].sha256 != hashlib.sha256(raw).hexdigest()):
        raise ExecutionPreparationError("maintained_asr_bundle_identity_changed")
    return origin


def _no_site_startup(environment, base, inventory):
    startup = ASRNoSiteStartup(policy="cpython312-no-site-v1", environment_root=str(environment), base_root=str(base))
    files, directories = {r.name for r in inventory.files}, set(inventory.directories)
    if any(name.casefold().endswith("._pth") for name in files):
        raise ExecutionPreparationError("maintained_asr_no_site_pth_unsupported")
    for path, logical in ((environment/"Lib/site-packages", "environment/Lib/site-packages"),
                          (base/"Lib", "base/Lib"), (base/"DLLs", "base/DLLs")):
        _exact_path(path, directory=True)
        if logical not in directories:
            raise ExecutionPreparationError("maintained_asr_startup_root_missing")
    archive = base/"python312.zip"
    if archive.exists():
        _exact_path(archive, directory=False)
        if "base/python312.zip" not in files:
            raise ExecutionPreparationError("maintained_asr_startup_root_missing")
    cfg = (environment/"pyvenv.cfg").read_text(encoding="utf-8")
    version = [line.split("=", 1)[1].strip() for line in cfg.splitlines() if line.split("=", 1)[0].strip() == "version"]
    if len(version) != 1 or not version[0].startswith("3.12."):
        raise ExecutionPreparationError("maintained_asr_startup_python_unsupported")
    return startup

def observe_asr(options):
    environment, base, model, root, staging = (Path(options[k]) for k in
        ("environment", "base", "model_root", "data_root", "staging_parent"))
    for path in (model, root, staging):
        _exact_path(path, directory=True)
    if (not model.is_relative_to(root) or not staging.is_relative_to(root)
        or any(staging.is_relative_to(p) for p in (environment, base, model))):
        raise ExecutionPreparationError("maintained_staging_boundary_invalid")
    # Complete local model directory, not just the required load files.
    paths = sorted(model.rglob("*"))
    if len(paths) > 512:
        raise ExecutionPreparationError("maintained_asr_model_limit")
    artifacts, total = [], 0
    for path in paths:
        _exact_path(path, directory=path.is_dir())
        if path.is_dir():
            continue
        size = path.stat().st_size
        total += size
        if size <= 0 or total > options["max_bytes"]:
            raise ExecutionPreparationError("maintained_asr_model_limit")
        name = path.relative_to(model).as_posix()
        artifacts.append(ExecutionArtifact(role="weights" if name == "model.bin" else "config",
            name=name, size_bytes=size, sha256=_digest(path, options["max_bytes"])[1]))
    if not set(ASR_MODEL_FILES).issubset(r.name for r in artifacts):
        raise ExecutionPreparationError("maintained_asr_local_assets_missing")
    try:
        validate_local_features(model)
    except ValueError as exc:
        raise ExecutionPreparationError(str(exc)) from exc
    policy = options.get("invocation_policy", "isolated-zip-asr-v1")
    inventory = observe_asr_inventory(environment, base, max_bytes=options["max_bytes"], invocation_policy=policy)
    origin = require_asr_origin(root, options["origin_reference"], inventory, policy)
    if origin.network_isolation != "not_asserted":
        raise ExecutionPreparationError("maintained_external_network_control_unverified")
    if configured_admission_actor(options["operator_key"]) != origin.actor:
        raise ExecutionPreparationError("maintained_asr_operator_required")
    startup = None
    if policy == "isolated-zip-asr-v2":
        startup = _no_site_startup(environment, base, inventory)
    else:
        _check_startup_roots(origin, environment, base)
    if _distribution_versions(environment, base) != {r.root+"/"+r.name.casefold(): r.version for r in origin.distributions}:
        raise ExecutionPreparationError("maintained_distribution_origin_mismatch")
    host = observe_host(environment, base, model, root, device="cpu", gpu_uuid=None)
    specification = ExecutionSpecificationV8 if startup else ExecutionSpecificationV7
    return specification(schema_version=8 if startup else 7, trust_policy="maintained-local-process-v1",
        capability="asr", mode="local", provider="faster-whisper", model=str(model),
        runtime=options["runtime_label"], machine_id=options["machine_id"],
        observation_recipe="faster-whisper-maintained-qa-child", observation_version=2 if startup else 1,
        invocation_policy=policy, timeout_seconds=options["timeout_seconds"],
        model_artifacts=tuple(artifacts), runtime_inventory=inventory.descriptor,
        runtime_origin=options["origin_reference"], host_runtime=host,
        machine=ExecutionMachineObservation(installation_id=UUID(str(options["installation_id"])),
            host_sha256=host.host_sha256, device_sha256=execution_canonical_sha256(host.model_dump(mode="json"))),
        parameters=dict(ASR_PARAMETERS), **({"startup": startup} if startup else {}))

class PreparedMaintainedASRChild:
    def __init__(self, temporary, options, spec, request):
        self._temporary, self._options = temporary, options
        self._spec_json, self._request_raw = spec.model_dump_json(), request.model_dump_json().encode()
        self._state = "prepared"
        self._invocation = self._expected_invocation()

    def _expected_invocation(self):
        private, system = Path(self._temporary.name), _system_root()
        options, spec = self._options, self.specification
        env = dict(SystemRoot=str(system), PATH=str(options["base"])+os.pathsep+str(system/"System32"),
            TEMP=str(private), TMP=str(private), HF_HOME=str(private/"cache"),
            HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1")
        flags = ("-I", "-S", "-B") if type(spec) is ExecutionSpecificationV8 else ("-I", "-B")
        return PendingInvocation(argv=(str(Path(options["environment"])/"Scripts/python.exe"),
            *flags, str(private/"application.pyz"), str(private/"request.json")),
            cwd=private, environment=tuple(sorted(env.items())), timeout_seconds=spec.timeout_seconds)

    @property
    def specification(self):
        schema = ExecutionSpecificationV8 if self._options.get("invocation_policy") == "isolated-zip-asr-v2" else ExecutionSpecificationV7
        return schema.model_validate_json(self._spec_json)
    @property
    def invocation(self):
        return self._invocation
    def request_snapshot(self):
        return ASRChildRequest.model_validate_json(self._request_raw)
    def _files(self):
        spec = self.specification
        files = [("application.pyz", _bundle(spec.invocation_policy)), ("request.json", self._request_raw)]
        if type(spec) is ExecutionSpecificationV8:
            files.append(("startup.json", spec.startup.model_dump_json().encode()))
        for name, expected in files:
            path = Path(self._temporary.name)/name
            _exact_path(path, directory=False)
            if path.stat().st_size != len(expected) or path.read_bytes() != expected:
                raise ExecutionPreparationError("maintained_asr_child_identity_changed")
    def verify(self):
        if self._state != "prepared":
            raise ExecutionPreparationError("maintained_preparation_consumed")
        try:
            self._files()
            if self._invocation != self._expected_invocation():
                raise ValueError()
            request = self.request_snapshot()
            if not Path(request.audio_path).is_relative_to(Path(self._options["data_root"])):
                raise ValueError()
            request.verify_audio()
            if request.model_root != self.specification.model:
                raise ValueError()
            return self.specification
        except Exception:
            self._state = "failed"
            raise ExecutionPreparationError("maintained_asr_child_identity_changed") from None
    def execute(self):
        spec = self.verify()
        self._state = "consumed"
        child = None
        try:
            call = self.invocation
            child = subprocess.Popen(list(call.argv), cwd=call.cwd, env=dict(call.environment), shell=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}))
            if child.wait(timeout=call.timeout_seconds) != 0:
                raise ExecutionPreparationError("maintained_asr_child_failed")
            output = Path(self._temporary.name)/"result.json"
            _exact_path(output, directory=False)
            if not 0 < output.stat().st_size <= ASR_PARAMETERS["output_max_bytes"]:
                raise ExecutionPreparationError("maintained_asr_output_limit")
            result = ASRChildResult.model_validate_json(output.read_bytes())
            result.transcription(self.request_snapshot().verify_audio())
            self._files()
            return result
        except BaseException as error:
            self._state = "failed"
            if child is not None and child.poll() is None:
                stop_maintained_process_tree(child)
            if isinstance(error, subprocess.TimeoutExpired):
                raise ExecutionPreparationError("maintained_asr_child_timeout") from None
            if isinstance(error, (ExecutionPreparationError, KeyboardInterrupt)):
                raise
            raise ExecutionPreparationError("maintained_asr_child_failed") from None
    def close(self):
        self._state = "closed"
        self._temporary.cleanup()
    def __enter__(self): return self
    def __exit__(self, *_args): self.close()

def prepare_maintained_asr_child(*, environment, base, model_root, data_root, staging_parent,
        origin_reference, operator_key, installation_id, runtime_label, machine_id, request, deployment_specification,
        timeout_seconds=600, max_bytes=64*1024**3, invocation_policy="isolated-zip-asr-v1"):
    temporary = None
    try:
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 3600:
            raise ValueError()
        request = ASRChildRequest.model_validate_json(request.model_dump_json())
        options = dict(environment=environment, base=base, model_root=model_root, data_root=data_root,
            staging_parent=staging_parent, origin_reference=type(origin_reference).model_validate_json(origin_reference.model_dump_json()),
            operator_key=operator_key, installation_id=installation_id, runtime_label=runtime_label,
            machine_id=machine_id, timeout_seconds=timeout_seconds, max_bytes=max_bytes, invocation_policy=invocation_policy)
        # Retained deployment/permission facts; this is not a fresh runtime witness.
        spec = deployment_specification
        if (type(spec) not in (ExecutionSpecificationV7, ExecutionSpecificationV8)
            or spec.parameters != ASR_PARAMETERS or spec.invocation_policy != invocation_policy
            or spec.timeout_seconds != timeout_seconds or spec.runtime != runtime_label
            or spec.machine_id != machine_id or str(spec.machine.installation_id) != str(installation_id)
            or spec.model != str(model_root)):
            raise ExecutionPreparationError("maintained_asr_configuration_changed")
        if type(spec) is ExecutionSpecificationV8 and (spec.startup.environment_root != str(environment) or spec.startup.base_root != str(base)):
            raise ExecutionPreparationError("maintained_asr_configuration_changed")
        for name in ASR_MODEL_FILES:
            _exact_path(Path(model_root)/name, directory=False)
        validate_local_features(Path(model_root))
        _exact_path(Path(environment)/"Scripts/python.exe", directory=False)
        if request.model_root != spec.model or not Path(request.audio_path).is_relative_to(Path(data_root)):
            raise ValueError()
        request.verify_audio()
        temporary = TemporaryDirectory(prefix="maintained-asr-child-", dir=staging_parent)
        private = Path(temporary.name)
        (private/"application.pyz").write_bytes(_bundle(spec.invocation_policy))
        if type(spec) is ExecutionSpecificationV8:
            (private/"startup.json").write_bytes(spec.startup.model_dump_json().encode())
        (private/"request.json").write_bytes(request.model_dump_json().encode())
        prepared = PreparedMaintainedASRChild(temporary, options, spec, request)
        prepared.verify()
        return prepared
    except Exception as error:
        if temporary is not None: temporary.cleanup()
        if isinstance(error, ExecutionPreparationError): raise
        raise ExecutionPreparationError("maintained_asr_preparation_invalid") from None
