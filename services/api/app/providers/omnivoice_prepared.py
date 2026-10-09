"""Actual model staging and independent identity, not a native invocation.

No report/receipt/hash inputs, injected loader or model import. Full native
dependency/device/dtype closure is still required before normal dispatch.
"""
from pathlib import Path
from tempfile import TemporaryDirectory

from app.domain.models import ExecutionArtifact, ExecutionSpecificationV3
from app.domain.execution_runtime import HostRuntimeObservationV3Policy2
from app.runtime_inventory import require_host_policy_inventory, RuntimeInventoryError
from .component_probe import PreparedComponentHost
from .machine_observation import observe_windows_cpu
from .omnivoice_local import OmniVoiceLocalParameters, select_local_omnivoice
from .omnivoice_recipe_v2 import (OmniVoiceLocalParametersV2, parse_local_parameters,
    select_parameterized_model)
from .omnivoice_entry_v2 import require_recipe_sources
from .omnivoice_recipe_v3 import OmniVoiceLocalParametersV3
from .omnivoice_entry_v3 import require_pcm_recipe_sources
from .omnivoice_pcm_import import require_pcm_boundary
from .prepared import ExecutionPreparationError, NativeExecutionUnsupported
from .runtime_tree import PreparedRuntimeTree, _members, _digest, _exact_path


def _manifest(root, parameters, limit):
    selection = select_parameterized_model(root, parameters)
    paths, directories = {}, set()
    _members(root, '', paths, directories)
    roles = {item.name: item.role for item in selection.artifacts}
    rows, artifacts, total = [], [], 0
    for name, path in sorted(paths.items()):
        size, digest = _digest(path, limit)
        total += size
        if total > limit:
            raise ExecutionPreparationError('execution_model_size_limit')
        rows.append((name, size, digest))
        if name in roles:
            artifacts.append(ExecutionArtifact(role=roles[name], name=name,
                size_bytes=size, sha256=digest))
    return tuple(sorted(directories)), tuple(rows), tuple(artifacts)


def _cleanup(temporary):
    try:
        _exact_path(Path(temporary.name), directory=True)
        temporary.cleanup()
    except (OSError, ValueError, RuntimeError):
        temporary._finalizer.detach()
        raise ExecutionPreparationError('execution_model_cleanup_failed') from None


class PreparedOmniVoiceIdentity:
    """Keep model copies and the same runtime/host alive; never authorizes run.

    Runtime/host ownership belongs to caller. Closing this object only removes
    its unique model copy. File/host observation must occur outside SQL locks.
    """
    __slots__ = ('_temporary', '_runtime', '_host', '_data_root', '_limit',
        '_manifest', '_spec_json', '_state', '_pcm_boundary')

    def __init__(self, temporary, runtime, host, data_root, limit, manifest, specification, pcm_boundary=None):
        self._temporary, self._runtime, self._host = temporary, runtime, host
        self._data_root, self._limit, self._manifest = data_root, limit, manifest
        self._spec_json, self._state = specification.model_dump_json(), 'prepared'
        self._pcm_boundary = pcm_boundary

    @property
    def model_root(self): return Path(self._temporary.name)

    @property
    def specification(self): return ExecutionSpecificationV3.model_validate_json(self._spec_json)

    def execution_use_snapshot(self):
        if self._state != 'prepared':
            raise ExecutionPreparationError('execution_preparation_consumed')
        try:
            spec = self.specification
            parameters = parse_local_parameters(spec.parameters)
            if self._host.tree is not self._runtime:
                raise ExecutionPreparationError('execution_prepared_runtime_mismatch')
            if _manifest(self.model_root, parameters, self._limit) != self._manifest:
                raise ExecutionPreparationError('execution_fixed_model_changed')
            inventory = self._runtime.verify()
            if type(parameters) is OmniVoiceLocalParametersV2:
                require_recipe_sources(self._runtime.root / 'Lib/site-packages')
            if type(parameters) is OmniVoiceLocalParametersV3:
                require_pcm_recipe_sources(self._runtime.root, inventory=inventory)
                require_pcm_boundary(self._pcm_boundary, tree=self._runtime)
            observed = self._host.verify()
            machine = observe_windows_cpu(data_root=self._data_root, device=parameters.device)
            if (inventory.descriptor != spec.runtime_inventory or observed != spec.host_runtime
                    or machine != spec.machine):
                raise ExecutionPreparationError('execution_prepared_identity_changed')
            require_host_policy_inventory(observed, inventory)
            # Recheck trees after host/machine observation; no stale file witness.
            if (_manifest(self.model_root, parameters, self._limit) != self._manifest
                    or self._runtime.verify().descriptor != spec.runtime_inventory):
                raise ExecutionPreparationError('execution_prepared_identity_changed')
            if type(parameters) is OmniVoiceLocalParametersV3:
                require_pcm_recipe_sources(self._runtime.root, inventory=self._runtime.verify())
                require_pcm_boundary(self._pcm_boundary, tree=self._runtime)
            return dict(snapshot_version=2, identity=spec.identity.model_dump(mode='json'),
                execution_specification=spec.model_dump(mode='json'), execution_sha256=spec.execution_sha256)
        except Exception as error:
            self._state = 'failed'
            if isinstance(error, ExecutionPreparationError): raise
            raise ExecutionPreparationError('execution_prepared_identity_changed') from None

    def require_native_preparation(self):
        # Identity preparation is not a Torch/Transformers native load witness.
        raise NativeExecutionUnsupported('execution_native_dependency_closure_unsupported')

    def close(self):
        if self._state == 'closed': return
        self._state = 'closed'
        _cleanup(self._temporary)

    def __enter__(self): return self
    def __exit__(self, *_args): self.close()


def prepare_omnivoice_identity(*, model_root: Path, parameters: OmniVoiceLocalParameters,
        runtime: PreparedRuntimeTree, host: PreparedComponentHost, data_root: Path,
        staging_parent: Path, max_model_bytes: int, provider: str, model: str,
        runtime_label: str, machine_id: str, pcm_boundary=None):
    """Read actual selected files, copy, independently rescan, observe host.

    Caller must keep the same owned prepared runtime/host alive and retain its
    normal source/license/budget gates. No expected identity or executable API.
    """
    temporary = None
    try:
        if (type(parameters) not in (OmniVoiceLocalParameters, OmniVoiceLocalParametersV2, OmniVoiceLocalParametersV3) or parameters.device != 'cpu'
                or type(runtime) is not PreparedRuntimeTree or type(host) is not PreparedComponentHost
                or host.tree is not runtime or type(max_model_bytes) is not int
                or not 0 < max_model_bytes <= 64 * 1024**3):
            raise ExecutionPreparationError('execution_model_preparation_invalid')
        parameters = parse_local_parameters(parameters.model_dump())
        _exact_path(model_root, directory=True)
        _exact_path(staging_parent, directory=True)
        if staging_parent.is_relative_to(model_root) or staging_parent.is_relative_to(runtime.root):
            raise ExecutionPreparationError('execution_model_preparation_invalid')
        inventory, observation = runtime.verify(), host.verify()
        if type(parameters) is OmniVoiceLocalParametersV2:
            require_recipe_sources(runtime.root / 'Lib/site-packages')
        if type(parameters) is OmniVoiceLocalParametersV3:
            require_pcm_recipe_sources(runtime.root, inventory=inventory)
            require_pcm_boundary(pcm_boundary, tree=runtime)
        elif pcm_boundary is not None:
            raise ExecutionPreparationError('omnivoice_pcm_import_boundary_wrong_version')
        observation = HostRuntimeObservationV3Policy2.model_validate_json(observation.model_dump_json())
        require_host_policy_inventory(observation, inventory)
        machine = observe_windows_cpu(data_root=data_root, device=parameters.device)
        before = _manifest(model_root, parameters, max_model_bytes)
        temporary = TemporaryDirectory(prefix='content-os-model-', dir=staging_parent)
        target = Path(temporary.name)
        for directory in before[0]: (target / directory).mkdir(parents=True, exist_ok=True)
        total = 0
        for name, _, _ in before[1]:
            source = model_root / name
            _exact_path(source, directory=False)
            with source.open('rb') as incoming, (target / name).open('xb') as outgoing:
                for block in iter(lambda: incoming.read(1024 * 1024), b''):
                    total += len(block)
                    if total > max_model_bytes: raise ExecutionPreparationError('execution_model_size_limit')
                    outgoing.write(block)
        actual = _manifest(target, parameters, max_model_bytes)
        if _manifest(model_root, parameters, max_model_bytes) != before or actual != before:
            raise ExecutionPreparationError('execution_model_source_changed')
        spec = ExecutionSpecificationV3(schema_version=3, capability='voice', provider=provider,
            model=model, runtime=runtime_label, machine_id=machine_id,
            observation_recipe=('omnivoice-local-cpu-pcm' if type(parameters) is OmniVoiceLocalParametersV3
                else 'omnivoice-local-cpu-components' if type(parameters) is OmniVoiceLocalParametersV2
                else 'omnivoice-local-fixed-identity'),
            observation_version=(3 if type(parameters) is OmniVoiceLocalParametersV3
                else 2 if type(parameters) is OmniVoiceLocalParametersV2 else 1),
            model_artifacts=actual[2], runtime_inventory=inventory.descriptor,
            host_runtime=observation, machine=machine, parameters=parameters.model_dump())
        prepared = PreparedOmniVoiceIdentity(temporary, runtime, host, data_root,
            max_model_bytes, actual, spec, pcm_boundary)
        prepared.execution_use_snapshot()
        return prepared
    except Exception as error:
        if temporary is not None: _cleanup(temporary)
        if isinstance(error, ExecutionPreparationError): raise
        if isinstance(error, RuntimeInventoryError):
            raise ExecutionPreparationError(str(error)) from None
        raise ExecutionPreparationError('execution_model_preparation_invalid') from None
