"""Maintained vendor CLI using the existing LatentSync staging/normalization.

Preparation observes files only (plus the existing vendor device diagnostic).
The application must reserve a durable call before consuming this object.
Offline cache variables do not assert OS-level network isolation.
"""
import hashlib
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from uuid import UUID

from app.domain.maintained_runtime import MaintainedTalkingOrigin
from app.domain.models import ExecutionArtifact, ExecutionMachineObservation, ExecutionSpecificationV9, execution_canonical_sha256
from app.domain.talking_child import TalkingChildRequest
from app.domain.talking_recipe import REQUIRED_TALKING_ARTIFACTS, VAE_CACHE, VAE_REVISION
from app.execution_admission import configured_admission_actor, UseAdmissionError
from .latentsync import LatentSyncProvider
from .maintained_runtime import (_observe_inventory, _require_origin, _check_startup_roots,
    _distribution_versions, observe_host, _system_root, stop_maintained_process_tree)
from .maintained_voice_bundle import BUNDLE_SLOT, bundle_bytes
from .prepared import ExecutionPreparationError
from .runtime_tree import _exact_path, _digest
from .talking import TalkingReference

TALKING_MAIN = b"from app.providers.maintained_talking_execution import child_main\nraise SystemExit(child_main())\n"


def require_reference_coverage(request):
    """Existing adapter frame alignment must fit authorized forward footage."""
    duration = request.slice_plan.duration_ms if request.slice_plan is not None else request.narration.duration_ms
    required = LatentSyncProvider._model_duration_ms(duration)
    if request.reference_end_ms-request.reference_start_ms < required:
        raise ExecutionPreparationError("maintained_talking_reference_alignment_not_covered")
    return required


def talking_bundle_bytes():
    return bundle_bytes(TALKING_MAIN)


def observe_talking_inventory(environment, base, *, max_bytes=64*1024**3):
    return _observe_inventory(environment, base, max_bytes=max_bytes,
        entry_name=BUNDLE_SLOT, entry_source=talking_bundle_bytes())


def require_talking_origin(root, reference, inventory):
    origin = _require_origin(root, reference, inventory, MaintainedTalkingOrigin)
    raw = talking_bundle_bytes()
    rows = [r for r in inventory.files if r.role == "entrypoint"]
    if (len(rows) != 1 or rows[0].name != BUNDLE_SLOT or rows[0].size_bytes != len(raw)
        or rows[0].sha256 != hashlib.sha256(raw).hexdigest()):
        raise ExecutionPreparationError("maintained_talking_bundle_identity_changed")
    return origin


def observe_talking_artifacts(options):
    """Shared exact vendor/resources; observes files without runtime imports."""
    root, staging, environment, base = (Path(options[k]) for k in ("data_root", "staging_parent", "environment", "base"))
    for path in (root, staging):
        _exact_path(path, directory=True)
    roots = {k: Path(options[k]) for k in ("vendor", "hf", "torch", "tools")}
    if (not staging.is_relative_to(root) or any(not p.is_relative_to(root) for p in roots.values())
        or any(staging.is_relative_to(p) or p.is_relative_to(staging) for p in (*roots.values(), environment, base))
        or any(a.is_relative_to(b) or b.is_relative_to(a) for i, a in enumerate(roots.values()) for b in list(roots.values())[i+1:])):
        raise ExecutionPreparationError("maintained_talking_layout_invalid")
    artifacts, total = [], 0
    for label, directory in roots.items():
        _exact_path(directory, directory=True)
        for path in sorted(directory.rglob("*")):
            _exact_path(path, directory=path.is_dir())
            if path.is_dir():
                continue
            size, sha = _digest(path, options["max_bytes"]-total)
            total += size
            if len(artifacts) >= 511 or total > options["max_bytes"]:
                raise ExecutionPreparationError("maintained_talking_artifact_limit")
            artifacts.append(ExecutionArtifact(role="dependency", name=label+"/"+path.relative_to(directory).as_posix(), size_bytes=size, sha256=sha))
    checkpoint = Path(options["checkpoint"])
    if not checkpoint.is_relative_to(root):
        raise ExecutionPreparationError("maintained_talking_layout_invalid")
    size, sha = _digest(checkpoint, options["max_bytes"]-total)
    artifacts.append(ExecutionArtifact(role="weights", name="checkpoint/model.pt", size_bytes=size, sha256=sha))
    if not REQUIRED_TALKING_ARTIFACTS.issubset(x.name for x in artifacts):
        raise ExecutionPreparationError("maintained_talking_local_assets_missing")
    if (roots["hf"]/VAE_CACHE/"refs/main").read_text().strip() != VAE_REVISION:
        raise ExecutionPreparationError("maintained_talking_vae_revision_changed")
    # This recipe supports the existing tiny/384 configuration only. No YAML
    # loader/model import is needed to reject a different vendor configuration.
    config = (roots["vendor"]/"configs/unet/stage2.yaml").read_text()
    if sum(line.strip() == "cross_attention_dim: 384" for line in config.splitlines()) != 1:
        raise ExecutionPreparationError("maintained_talking_configuration_unsupported")
    return tuple(artifacts)


def observe_talking(options):
    environment, base, root, staging = (Path(options[k]) for k in ("environment", "base", "data_root", "staging_parent"))
    _exact_path(environment, directory=True)
    # The owned adapter application uses the existing Python 3.12 contracts.
    # A legacy 3.10 installation cannot be silently treated as this recipe.
    cfg = (environment/"pyvenv.cfg").read_text()
    versions = [line.split("=",1)[1].strip() for line in cfg.splitlines()
        if "=" in line and line.split("=",1)[0].strip() == "version"]
    if len(versions) != 1 or not versions[0].startswith("3.12."):
        raise ExecutionPreparationError("maintained_talking_python_unsupported")
    artifacts = observe_talking_artifacts(options)
    inventory = observe_talking_inventory(environment, base, max_bytes=options["max_bytes"])
    origin = require_talking_origin(root, options["origin_reference"], inventory)
    if origin.network_isolation != "not_asserted" or configured_admission_actor(options["operator_key"]) != origin.actor:
        raise ExecutionPreparationError("maintained_talking_operator_required")
    _check_startup_roots(origin, environment, base)
    if _distribution_versions(environment, base) != {r.root+"/"+r.name.casefold(): r.version for r in origin.distributions}:
        raise ExecutionPreparationError("maintained_distribution_origin_mismatch")
    host = observe_host(environment, base, Path(options["vendor"]), root, device="cuda:0", gpu_uuid=options["gpu_uuid"])
    return ExecutionSpecificationV9(schema_version=9, trust_policy="maintained-local-process-v1", capability="talking",
        mode="local", provider="latentsync", model="LatentSync-1.5", runtime=options["runtime_label"], machine_id=options["machine_id"],
        observation_recipe="latentsync-maintained-talking-cli", observation_version=1, invocation_policy="maintained-latentsync-cli-v1",
        timeout_seconds=options["timeout_seconds"], model_artifacts=tuple(artifacts), runtime_inventory=inventory.descriptor,
        runtime_origin=options["origin_reference"], host_runtime=host,
        machine=ExecutionMachineObservation(installation_id=UUID(str(options["installation_id"])), host_sha256=host.host_sha256,
            device_sha256=execution_canonical_sha256(host.model_dump(mode="json"))), parameters=options["parameters"])


def child_main():
    """Standard adapter in an owned process, no alternate lip-sync engine."""
    import json
    import sys
    private = Path(sys.argv[1])
    options = json.loads((private/"options.json").read_bytes())
    request = TalkingChildRequest.model_validate_json((private/"request.json").read_bytes())
    request.verify_inputs(options["data_root"])
    require_reference_coverage(request)
    p = options["parameters"]
    provider = LatentSyncProvider(Path(options["environment"])/"Scripts/python.exe", options["vendor"], options["checkpoint"],
        ffmpeg_command=Path(options["tools"])/"ffmpeg.exe", inference_steps=p["inference_steps"], guidance_scale=p["guidance_scale"],
        seed=p["seed"], timeout_seconds=options["timeout_seconds"])
    # Child vendor CLI receives -B through PYTHONDONTWRITEBYTECODE as well.
    # Paths are absolute and quoted by adapter shell=False commands. Vendor
    # internals are covered by explicit operator/vendor/OS trust, not a sandbox.
    narration = request.narration
    if request.slice_plan is not None:
        from app.talking.slices import extract_talking_audio_slice
        narration = extract_talking_audio_slice(narration, request.slice_plan, private/"slice.wav",
            ffmpeg_command=Path(options["tools"])/"ffmpeg.exe",
            timeout_seconds=options["timeout_seconds"])
        # This is a transient derivative, not a relabelled Master original.
        narration = narration.model_copy(update={"content_hash": _digest(Path(narration.source_file), 4*1024**3)[1]})
    provider.synthesize(request.profile, narration,
        TalkingReference(request.reference_clip_id, Path(request.reference_path), request.reference_start_ms, request.reference_end_ms),
        private/"output.mp4")
    return 0


class PreparedMaintainedTalkingChild:
    def __init__(self, temporary, options, spec, request):
        self._temporary, self._options = temporary, options
        self._spec_json, self._request_raw = spec.model_dump_json(), request.model_dump_json().encode()
        self._state = "prepared"
        import json
        self._options_raw = json.dumps({k: str(v) if isinstance(v, Path) else v for k,v in options.items()
            if k not in ("operator_key", "origin_reference", "installation_id", "gpu_uuid")}, sort_keys=True).encode()
        self._bundle_raw = talking_bundle_bytes()
        self._write_files()

    @property
    def specification(self):
        return ExecutionSpecificationV9.model_validate_json(self._spec_json)

    def request_snapshot(self):
        return TalkingChildRequest.model_validate_json(self._request_raw)

    def _files(self):
        return (("application.pyz", self._bundle_raw), ("request.json", self._request_raw), ("options.json", self._options_raw))

    def _write_files(self):
        for name, raw in self._files():
            (Path(self._temporary.name)/name).write_bytes(raw)

    def _verify_files(self):
        for name, raw in self._files():
            path = Path(self._temporary.name)/name
            _exact_path(path, directory=False)
            if path.read_bytes() != raw:
                raise ExecutionPreparationError("maintained_talking_child_identity_changed")

    def verify(self):
        if self._state != "prepared":
            raise ExecutionPreparationError("maintained_preparation_consumed")
        try:
            self._verify_files()
            self.request_snapshot().verify_inputs(self._options["data_root"])
            require_reference_coverage(self.request_snapshot())
            if observe_talking(self._options) != self.specification:
                raise ValueError()
            return self.specification
        except Exception:
            self._state = "failed"
            raise ExecutionPreparationError("maintained_talking_child_identity_changed") from None

    def execution_use_snapshot(self):
        spec = self.verify()
        return dict(snapshot_version=2, identity=spec.identity.model_dump(mode="json"),
            execution_specification=spec.model_dump(mode="json"), execution_sha256=spec.execution_sha256)

    def execute(self):
        return self._execute()

    def execute_owned(self, lease):
        from app.jobs.lease_fence import OwnedJobLease
        if type(lease) is not OwnedJobLease:
            raise ExecutionPreparationError("evaluation_talking_owned_lease_required")
        lease.require()
        return self._execute(lease)

    def _execute(self, lease=None):
        spec = self.verify()
        self._state = "consumed"
        private, system = Path(self._temporary.name), _system_root()
        env = dict(SystemRoot=str(system), PATH=str(Path(self._options["tools"]))+os.pathsep+str(system/"System32"),
            TEMP=str(private), TMP=str(private), HF_HOME=str(self._options["hf"]), TORCH_HOME=str(self._options["torch"]),
            HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1", PYTHONDONTWRITEBYTECODE="1")
        child = None
        try:
            if lease is not None: lease.require()
            child = subprocess.Popen([str(Path(self._options["environment"])/"Scripts/python.exe"), "-I", "-B",
                str(private/"application.pyz"), str(private)], cwd=private, env=env, shell=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}))
            if child.wait(timeout=spec.timeout_seconds) != 0:
                raise ExecutionPreparationError("maintained_talking_child_failed")
            self._verify_files()
            self.request_snapshot().verify_inputs(self._options["data_root"])
            if observe_talking(self._options) != spec:
                raise ExecutionPreparationError("maintained_talking_child_identity_changed")
            output = private/"output.mp4"
            _exact_path(output, directory=False)
            if not 0 < output.stat().st_size <= 4*1024**3:
                raise ExecutionPreparationError("maintained_talking_output_limit")
            return output
        except BaseException as exc:
            self._state = "failed"
            if child is not None and child.poll() is None:
                stop_maintained_process_tree(child)
            if isinstance(exc, subprocess.TimeoutExpired):
                raise ExecutionPreparationError("maintained_talking_child_timeout") from None
            if isinstance(exc, (ExecutionPreparationError, KeyboardInterrupt, UseAdmissionError)):
                raise
            raise ExecutionPreparationError("maintained_talking_child_failed") from None

    def close(self):
        self._state = "closed"
        self._temporary.cleanup()

    def __enter__(self): return self
    def __exit__(self, *_args): self.close()


def prepare_maintained_talking_child(*, request, **options):
    temporary = None
    try:
        request = TalkingChildRequest.model_validate_json(request.model_dump_json())
        options = dict(options)
        spec = observe_talking(options)
        request.verify_inputs(options["data_root"])
        require_reference_coverage(request)
        temporary = TemporaryDirectory(prefix="maintained-talking-child-", dir=options["staging_parent"])
        prepared = PreparedMaintainedTalkingChild(temporary, options, spec, request)
        prepared.verify()
        return prepared
    except Exception as exc:
        if temporary is not None: temporary.cleanup()
        if isinstance(exc, ExecutionPreparationError): raise
        raise ExecutionPreparationError("maintained_talking_preparation_invalid") from None
