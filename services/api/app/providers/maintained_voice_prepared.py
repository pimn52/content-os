"""Read-only maintained Voice source identity; no executable child or grant."""
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

from app.domain.maintained_runtime import MaintainedGenerationOrigin
from app.domain.models import ExecutionMachineObservation, ExecutionSpecificationV5, ExecutionSpecificationV6, execution_canonical_sha256
from app.execution_admission import configured_admission_actor
from .maintained_runtime import (_observe_inventory, _require_origin, _check_startup_roots,
    _distribution_versions, _model_artifacts, observe_host)
from .maintained_voice import MaintainedVoiceParameters
from .prepared import ExecutionPreparationError
from .runtime_tree import _exact_path

RECIPE_SLOT = "entry/voice_recipe.py"


def recipe_bytes():
    path = Path(__file__).with_name("maintained_voice.py").resolve()
    _exact_path(path, directory=False)
    raw = path.read_bytes()
    if not 0 < len(raw) <= 1024*1024:
        raise ExecutionPreparationError("maintained_voice_recipe_size_invalid")
    return raw


def observe_voice_inventory(environment, base, *, max_bytes):
    return _observe_inventory(environment, base, max_bytes=max_bytes,
        entry_name=RECIPE_SLOT, entry_source=recipe_bytes())


def require_voice_origin(data_root, reference, inventory):
    origin = _require_origin(data_root, reference, inventory, MaintainedGenerationOrigin)
    raw = recipe_bytes()
    rows = [row for row in inventory.files if row.name == RECIPE_SLOT]
    if (len(rows) != 1 or rows[0].role != "entrypoint" or rows[0].size_bytes != len(raw)
        or rows[0].sha256 != hashlib.sha256(raw).hexdigest()
        or any(row.name == "entry/probe.py" for row in inventory.files)):
        raise ExecutionPreparationError("maintained_voice_recipe_identity_changed")
    return origin


def require_voice_specification(spec):
    """Current reader check, without importing or invoking a vendor runtime."""
    if type(spec) not in (ExecutionSpecificationV5, ExecutionSpecificationV6):
        raise ExecutionPreparationError("maintained_voice_specification_required")
    params = MaintainedVoiceParameters.model_validate(spec.parameters)
    if params.model_dump() != spec.parameters:
        raise ExecutionPreparationError("maintained_voice_parameters_not_expanded")


def _observe(options, *, packaged=False, timeout_seconds=None):
    environment, base, model_root, data_root, staging_parent = (
        Path(options[name]) for name in ("environment", "base", "model_root", "data_root", "staging_parent"))
    for root in (model_root, data_root, staging_parent):
        _exact_path(root, directory=True)
    if (not model_root.is_relative_to(data_root) or not staging_parent.is_relative_to(data_root)
        or any(staging_parent.is_relative_to(root) for root in (environment, base, model_root))):
        raise ExecutionPreparationError("maintained_staging_boundary_invalid")
    parameters = MaintainedVoiceParameters.model_validate(options["parameters"]).model_dump()
    if packaged:
        from .maintained_voice_execution import observe_child_inventory, require_child_origin
        inventory = observe_child_inventory(environment, base, max_bytes=options["max_bytes"])
        origin = require_child_origin(data_root, options["origin_reference"], inventory)
    else:
        inventory = observe_voice_inventory(environment, base, max_bytes=options["max_bytes"])
        origin = require_voice_origin(data_root, options["origin_reference"], inventory)
    if origin.network_isolation != "not_asserted":
        raise ExecutionPreparationError("maintained_external_network_control_unverified")
    if configured_admission_actor(options["operator_key"]) != origin.actor:
        raise ExecutionPreparationError("maintained_generation_operator_required")
    _check_startup_roots(origin, environment, base)
    if _distribution_versions(environment, base) != {
        item.root+"/"+item.name.casefold(): item.version for item in origin.distributions}:
        raise ExecutionPreparationError("maintained_distribution_origin_mismatch")
    host = observe_host(environment, base, model_root, data_root,
        device=parameters["device"], gpu_uuid=options["gpu_uuid"])
    artifacts = _model_artifacts(model_root, options["max_bytes"])
    spec_type = ExecutionSpecificationV6 if packaged else ExecutionSpecificationV5
    spec = spec_type(schema_version=6 if packaged else 5, trust_policy="maintained-local-process-v1",
        capability="voice", mode="local", provider="omnivoice", model=str(model_root),
        runtime=options["runtime_label"], machine_id=options["machine_id"],
        observation_recipe="omnivoice-maintained-clone-child" if packaged else "omnivoice-maintained-clone-identity", observation_version=1,
        model_artifacts=artifacts, runtime_inventory=inventory.descriptor,
        runtime_origin=options["origin_reference"], host_runtime=host,
        machine=ExecutionMachineObservation(installation_id=UUID(str(options["installation_id"])),
            host_sha256=host.host_sha256, device_sha256=execution_canonical_sha256(host.model_dump(mode="json"))),
        parameters=parameters, **(dict(invocation_policy="isolated-zip-voice-v1", timeout_seconds=timeout_seconds) if packaged else {}))
    require_voice_specification(spec)
    return spec


class PreparedMaintainedVoiceIdentity:
    """Retain source identity for frozen comparison, deliberately no execute.

    Application/child packaging and input-bound invocation are still absent.
    Model files stay at their selected root and are freshly rehashed; this is
    integrity observation, not atomic protection from a hostile administrator.
    """
    def __init__(self, temporary, options, spec):
        self._temporary, self._options = temporary, options
        self._spec_json, self._state = spec.model_dump_json(), "prepared"

    @property
    def specification(self):
        return ExecutionSpecificationV5.model_validate_json(self._spec_json)

    def verify(self):
        if self._state != "prepared":
            raise ExecutionPreparationError("maintained_preparation_consumed")
        try:
            current = _observe(self._options)
            path = Path(self._temporary.name) / "voice_recipe.py"
            _exact_path(path, directory=False)
            if path.read_bytes() != recipe_bytes() or current != self.specification:
                raise ExecutionPreparationError("maintained_voice_prepared_identity_changed")
            return current
        except Exception:
            self._state = "failed"
            raise ExecutionPreparationError("maintained_voice_prepared_identity_changed") from None

    def execution_use_snapshot(self):
        spec = self.verify()
        return dict(snapshot_version=2, identity=spec.identity.model_dump(mode="json"),
            execution_specification=spec.model_dump(mode="json"), execution_sha256=spec.execution_sha256)

    def close(self):
        self._state = "closed"
        self._temporary.cleanup()

    def __enter__(self): return self
    def __exit__(self, *_args): self.close()


def prepare_maintained_voice_identity(*, environment, base, model_root, data_root, staging_parent,
        origin_reference, operator_key, parameters, installation_id, runtime_label, machine_id,
        gpu_uuid=None, max_bytes=64*1024**3):
    """Independent current observation; no expected hash or executable callback."""
    temporary = None
    try:
        options = dict(environment=environment, base=base, model_root=model_root, data_root=data_root,
            staging_parent=staging_parent, origin_reference=type(origin_reference).model_validate_json(origin_reference.model_dump_json()),
            operator_key=operator_key, parameters=MaintainedVoiceParameters.model_validate(parameters).model_dump(),
            installation_id=installation_id, runtime_label=runtime_label, machine_id=machine_id,
            gpu_uuid=gpu_uuid, max_bytes=max_bytes)
        spec = _observe(options)
        temporary = TemporaryDirectory(prefix="maintained-voice-identity-", dir=staging_parent)
        (Path(temporary.name) / "voice_recipe.py").write_bytes(recipe_bytes())
        prepared = PreparedMaintainedVoiceIdentity(temporary, options, spec)
        prepared.verify()
        return prepared
    except Exception as error:
        if temporary is not None: temporary.cleanup()
        if isinstance(error, ExecutionPreparationError): raise
        raise ExecutionPreparationError("maintained_voice_preparation_invalid") from None
