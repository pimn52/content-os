"""Separate one-use policy2 CPU diagnostic recipe; no normal dispatch route."""
from pathlib import Path
from typing import Literal, Any, ClassVar

from pydantic import Field, field_validator, model_validator

from app.domain.execution_runtime import (RuntimeContract, HostRuntimeObservationV3Policy2,
    CommonControlsBindingPolicy2, runtime_path)
from .component_contexts import created_binding, host_observation
from .cpython_base import select_cpython312_base
from .host_runtime import prepare_host_runtime_v2
from .prepared import ExecutionPreparationError, NativeExecutionUnsupported, PendingInvocation
from .python_origin_probe import PreparedOriginProbe, BlockedOrigin, owned_probe_files, ENTRY, TREE_LIMIT
from .runtime_tree import OwnedRuntimeFile, prepare_runtime_tree, _scan_private
from .runtime_primitives import _exact_path
from .windows_activation import WindowsActivationReader
from .windows_component_policy import owned_component_policy_bytes
from .windows_native import _read_windows_facts
from .common_controls_binding import COMPARISON_CHECKPOINTS


class PythonOriginFailure(RuntimeContract):
    checkpoint: Literal['registry', 'count', 'module', 'attributes', 'missing_file',
        'file_value', 'exact_path', 'empty']
    module_name: str = Field(pattern=r'^(?:[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)?$', max_length=255)


class AssociatedBinding(RuntimeContract):
    name: str
    binding: CommonControlsBindingPolicy2
    _name = field_validator('name')(runtime_path)


class PythonOwnerEvidence(RuntimeContract):
    recipe: Literal['cpython312-expat-owner-v1']
    owner: Literal['DLLs/pyexpat.pyd']
    owner_sha256: str = Field(pattern='^[0-9a-f]{64}$')
    modules: tuple[str, ...] = Field(min_length=2, max_length=4)
    data_sha256: tuple[str, str]
    wrapper_sha256: str | None

    @model_validator(mode='after')
    def coherence(self):
        import re
        direct = ('pyexpat.errors', 'pyexpat.model')
        aliases = ('xml.parsers.expat.errors', 'xml.parsers.expat.model')
        if self.modules not in (direct, direct + aliases): raise ValueError('component_protocol_invalid')
        if any(not re.fullmatch('[0-9a-f]{64}', value) for value in self.data_sha256): raise ValueError('component_protocol_invalid')
        if self.modules == direct:
            if self.wrapper_sha256 is not None: raise ValueError('component_protocol_invalid')
        elif type(self.wrapper_sha256) is not str or not re.fullmatch('[0-9a-f]{64}', self.wrapper_sha256):
            raise ValueError('component_protocol_invalid')
        return self


class ComponentProbeReport(RuntimeContract):
    _origin_stages: ClassVar[tuple[str, ...]] = ('baseline', 'recheck')
    probe_version: Literal[3]
    nonce: str = Field(pattern='^[0-9a-f]{64}$')
    passed: bool
    stage: Literal['tree', 'host', 'baseline', 'contexts', 'search', 'recheck', 'complete']
    inventory_sha256: str = Field(pattern='^(?:[0-9a-f]{64})?$')
    host_runtime: HostRuntimeObservationV3Policy2 | None
    native_count: int = Field(ge=0, le=2048)
    python_count: int = Field(ge=0, le=2048)
    code: str = Field(pattern='^(?:execution_[a-z0-9_]+)?$', max_length=100)
    unknown_module: str = Field(max_length=255)
    blocked_origins: tuple[BlockedOrigin, ...] = Field(max_length=256)
    effective: CommonControlsBindingPolicy2 | None
    associated: tuple[AssociatedBinding, ...] = Field(max_length=64)
    python_origin_failure: PythonOriginFailure | None = None

    @property
    def dispatch_authorized(self): return False

    @field_validator('probe_version', mode='before')
    @classmethod
    def version(cls, value):
        if type(value) is not int: raise ValueError('component_protocol_invalid')
        return value

    @model_validator(mode='after')
    def coherence(self):
        if self.python_origin_failure is not None:
            if (self.passed or self.stage not in self._origin_stages
                or self.code != 'execution_python_origin_unsupported' or self.blocked_origins):
                raise ValueError('component_protocol_invalid')
        names = tuple(row.name for row in self.associated)
        if len(set(names)) != len(names): raise ValueError('component_protocol_invalid')
        if self.passed:
            if self.effective is None or self.host_runtime is None or self.blocked_origins:
                raise ValueError('component_protocol_invalid')
            expected = self.host_runtime.binding.model_dump(mode='json')
            keys = ('component_policy_version', 'component_policy_sha256', 'manifest_contract_sha256', 'common_controls', 'resource')
            for row in (self.effective, *(item.binding for item in self.associated)):
                if any(row.model_dump(mode='json')[key] != expected[key] for key in keys):
                    raise ValueError('component_protocol_invalid')
            if (self.host_runtime.binding.query_role != 'created' or self.effective.query_role != 'effective'
                or self.effective.source_role != 'bootstrap_exe'
                or self.effective.source_sha256 != self.host_runtime.binding.source_sha256
                or any(row.binding.query_role != 'associated' or row.binding.source_role != 'dll' for row in self.associated)):
                raise ValueError('component_protocol_invalid')
        if self.blocked_origins:
            rows = tuple((r.kind, r.name, r.category, r.origin_id) for r in self.blocked_origins)
            if (self.passed or self.stage not in self._origin_stages or self.code != 'execution_loaded_origin_unsupported'
                or rows != tuple(sorted(set(rows))) or self.unknown_module != self.blocked_origins[0].name):
                raise ValueError('component_protocol_invalid')
        elif self.code == 'execution_loaded_origin_unsupported':
            raise ValueError('component_protocol_invalid')
        return self


class ComponentFailureEvidence(RuntimeContract):
    role: Literal['effective', 'associated']
    source: str
    checkpoint: str
    metadata: dict[str, Any]
    _source = field_validator('source')(runtime_path)

    @model_validator(mode='after')
    def bounded(self):
        import json
        if (self.checkpoint not in (*COMPARISON_CHECKPOINTS, 'binding_mismatch')
            or len(json.dumps(self.metadata, ensure_ascii=True, allow_nan=False).encode('ascii')) > 48 * 1024
            or (self.role == 'effective' and self.source != 'python.exe')):
            raise ValueError('component_protocol_invalid')
        return self


class ComponentOwnerProbeReport(ComponentProbeReport):
    _owner_stages: ClassVar[tuple[str, ...]] = ('baseline', 'contexts', 'search', 'recheck', 'complete')
    _context_stages: ClassVar[tuple[str, ...]] = ('contexts', 'recheck')
    probe_version: Literal[4]
    python_source_recipe: Literal['cpython312-expat-owner-v1']
    python_owner: PythonOwnerEvidence | None
    python_owner_checkpoint: Literal['owner_file', 'native', 'owner_module', 'children', 'data',
        'wrapper', 'stability', 'python', 'registry'] | None = None
    component_failure: ComponentFailureEvidence | None = None

    @model_validator(mode='after')
    def owner_coherence(self):
        if self.component_failure is not None:
            expected_code = ('execution_component_context_mismatch'
                if self.component_failure.checkpoint == 'binding_mismatch'
                else 'execution_common_controls_policy2_unsupported')
            if self.passed or self.stage not in self._context_stages or self.code != expected_code:
                raise ValueError('component_protocol_invalid')
        if (self.python_owner_checkpoint is not None) != (self.code == 'execution_python_owner_unsupported'):
            raise ValueError('component_protocol_invalid')
        if self.python_owner_checkpoint is not None and (self.passed or self.stage not in self._origin_stages):
            raise ValueError('component_protocol_invalid')
        if self.passed and self.python_owner is None: raise ValueError('component_protocol_invalid')
        if self.python_owner is not None and self.stage not in self._owner_stages:
            raise ValueError('component_protocol_invalid')
        return self


class NativeLoadEvidence(RuntimeContract):
    recipe: Literal['cpython312-bz2-mapping-v1']
    target: Literal['DLLs/_bz2.pyd']
    target_sha256: str = Field(pattern='^(?:[0-9a-f]{64})?$')
    plan_sha256: str = Field(pattern='^(?:[0-9a-f]{64})?$')
    node_count: int = Field(ge=0, le=64)
    edge_count: int = Field(ge=0, le=512)
    load_attempts: int = Field(ge=0, le=1)
    release_attempts: int = Field(ge=0, le=1)
    postload_verified: bool
    release_verified: bool
    target_requires_binding: bool
    target_binding: CommonControlsBindingPolicy2 | None
    cleanup_code: str = Field(pattern='^(?:execution_[a-z0-9_]+)?$', max_length=100)

    @model_validator(mode='after')
    def coherence(self):
        if (self.release_attempts > self.load_attempts
            or (self.postload_verified and self.load_attempts != 1)
            or (self.release_verified and (not self.postload_verified or self.release_attempts != 1 or self.cleanup_code))):
            raise ValueError('component_protocol_invalid')
        if self.target_binding is not None and (not self.target_requires_binding
            or self.target_binding.query_role != 'associated' or self.target_binding.source_role != 'dll'
            or self.target_binding.source_sha256 != self.target_sha256):
            raise ValueError('component_protocol_invalid')
        if self.postload_verified and self.target_requires_binding and self.target_binding is None:
            raise ValueError('component_protocol_invalid')
        return self


class NativeLoadProbeReport(ComponentOwnerProbeReport):
    _origin_stages: ClassVar[tuple[str, ...]] = ('baseline', 'recheck', 'preflight', 'load', 'postload', 'release')
    _owner_stages: ClassVar[tuple[str, ...]] = ComponentOwnerProbeReport._owner_stages + ('preflight', 'load', 'postload', 'release')
    _context_stages: ClassVar[tuple[str, ...]] = ('contexts', 'recheck', 'preflight', 'postload', 'release')
    probe_version: Literal[5]
    stage: Literal['tree', 'host', 'baseline', 'contexts', 'search', 'preflight', 'load', 'postload', 'release', 'recheck', 'complete']
    native_load: NativeLoadEvidence | None

    @model_validator(mode='after')
    def load_coherence(self):
        if self.passed and (self.native_load is None or not self.native_load.release_verified
            or not self.native_load.plan_sha256 or not self.native_load.target_sha256 or not self.native_load.node_count):
            raise ValueError('component_protocol_invalid')
        return self


def owned_component_probe_files():
    """Fixed exact stdlib-only helper list; never copy/import app.providers."""
    directory = Path(__file__).parent
    rows = []
    for item in owned_probe_files():
        if item.destination == ENTRY:
            marker = b'COMPONENT_PROTOCOL = False'
            if item.payload.count(marker) != 1:
                raise ExecutionPreparationError('execution_component_bundle_invalid')
            item = OwnedRuntimeFile(item.destination, item.payload.replace(marker, b'COMPONENT_PROTOCOL = True'), item.role)
        rows.append(item)
    for name in ('windows_activation', 'native_manifest', 'pe_imports', 'pe_resources', 'pe_data',
        'component_manifest', 'common_controls_binding', 'windows_component_policy', 'component_contexts', 'component_child'):
        source = directory / (name + '.py')
        _exact_path(source, directory=False)
        with source.open('rb') as stream: payload = stream.read(1024 * 1024 + 1)
        rows.append(OwnedRuntimeFile('Lib/site-packages/_content_os_host/' + name + '.py', payload))
    rows.append(OwnedRuntimeFile('Lib/site-packages/_content_os_host/windows_component_policy.json', owned_component_policy_bytes()))
    return tuple(rows)


def owned_component_owner_probe_files():
    rows = list(owned_component_probe_files())
    for index, row in enumerate(rows):
        if row.destination == ENTRY:
            marker = b'OWNER_PROTOCOL = False'
            if row.payload.count(marker) != 1: raise ExecutionPreparationError('execution_component_bundle_invalid')
            rows[index] = OwnedRuntimeFile(row.destination, row.payload.replace(marker, b'OWNER_PROTOCOL = True'), row.role)
    source = Path(__file__).with_name('python_owner_origins.py')
    _exact_path(source, directory=False)
    with source.open('rb') as stream: payload = stream.read(1024 * 1024 + 1)
    rows.append(OwnedRuntimeFile('Lib/site-packages/_content_os_host/python_owner_origins.py', payload))
    return tuple(rows)


def owned_native_load_probe_files():
    rows = list(owned_component_owner_probe_files())
    for index, row in enumerate(rows):
        if row.destination == ENTRY:
            marker = b'NATIVE_PROTOCOL = False'
            if row.payload.count(marker) != 1: raise ExecutionPreparationError('execution_component_bundle_invalid')
            rows[index] = OwnedRuntimeFile(row.destination, row.payload.replace(marker, b'NATIVE_PROTOCOL = True'), row.role)
    for name in ('native_load', 'component_load_child'):
        source = Path(__file__).with_name(name + '.py')
        _exact_path(source, directory=False)
        with source.open('rb') as stream: payload = stream.read(1024 * 1024 + 1)
        rows.append(OwnedRuntimeFile('Lib/site-packages/_content_os_host/' + name + '.py', payload))
    return tuple(rows)


def owned_soundfile_handle_probe_files():
    """Native-load diagnostic bundle plus its frozen stdlib lease core."""
    rows = list(owned_native_load_probe_files())
    if any(row.destination == 'Lib/site-packages/_content_os_host/soundfile_handle.py' for row in rows):
        raise ExecutionPreparationError('execution_component_bundle_invalid')
    source = Path(__file__).with_name('soundfile_handle.py')
    _exact_path(source, directory=False)
    with source.open('rb') as stream:
        payload = stream.read(1024 * 1024 + 1)
    if not 0 < len(payload) <= 1024 * 1024:
        raise ExecutionPreparationError('execution_component_bundle_invalid')
    rows.append(OwnedRuntimeFile('Lib/site-packages/_content_os_host/soundfile_handle.py', payload))
    return tuple(rows)


class PreparedComponentHost:
    def __init__(self, tree, base_host):
        self.tree, self.base_host = tree, base_host
        self._state = 'prepared'
        self._facts, self._binding = self._observe()
        self._observation = HostRuntimeObservationV3Policy2.model_validate(host_observation(self._facts, self._binding))

    @property
    def windows_root(self): return self._facts.root

    @property
    def observation(self): return HostRuntimeObservationV3Policy2.model_validate_json(self._observation.model_dump_json())

    def _observe(self):
        inventory = self.tree.inventory
        if _scan_private(self.tree.root, {row.name: row.role for row in inventory.files}, TREE_LIMIT).descriptor != inventory.descriptor:
            raise ExecutionPreparationError('execution_fixed_runtime_changed')
        base_observation = self.base_host.verify()
        facts = _read_windows_facts()
        if (facts.root != self.base_host.windows_root or facts.build != base_observation.build
                or facts.revision != base_observation.revision or base_observation.device.kind != 'cpu'):
            raise NativeExecutionUnsupported('execution_host_runtime_changed')
        files = tuple((row.name, row.sha256) for row in self.tree.inventory.files)
        binding = created_binding(WindowsActivationReader(facts.system_directory), self.tree.root, files, facts.root)
        if _read_windows_facts() != facts:
            raise NativeExecutionUnsupported('execution_host_runtime_changed')
        if _scan_private(self.tree.root, {row.name: row.role for row in inventory.files}, TREE_LIMIT).descriptor != inventory.descriptor:
            raise ExecutionPreparationError('execution_fixed_runtime_changed')
        return facts, binding

    def verify(self):
        if self._state != 'prepared': raise NativeExecutionUnsupported('execution_host_witness_consumed')
        try:
            if self._observe() != (self._facts, self._binding):
                raise NativeExecutionUnsupported('execution_host_runtime_changed')
            return self.observation
        except Exception:
            self._state = 'failed'
            raise


class PreparedComponentProbe(PreparedOriginProbe):
    _probe_version = 3
    _report_type = ComponentProbeReport

    def _validate_extra(self, report):
        if (report.blocked_origins or report.python_origin_failure is not None) and report.inventory_sha256 != self.inventory.descriptor.sha256:
            raise ValueError('component_protocol_invalid')
        if report.passed:
            files = {row.name: row.sha256 for row in self.inventory.files}
            if any(files.get(row.name) != row.binding.source_sha256 for row in report.associated):
                raise ValueError('component_protocol_invalid')


class PreparedComponentOwnerProbe(PreparedComponentProbe):
    _probe_version = 4
    _report_type = ComponentOwnerProbeReport

    def _validate_extra(self, report):
        super()._validate_extra(report)
        if report.python_owner is not None:
            files = {row.name: row.sha256 for row in self.inventory.files}
            if (report.inventory_sha256 != self.inventory.descriptor.sha256
                or files.get(report.python_owner.owner) != report.python_owner.owner_sha256
                or (report.python_owner.wrapper_sha256 is not None
                    and files.get('Lib/xml/parsers/expat.py') != report.python_owner.wrapper_sha256)):
                raise ValueError('component_protocol_invalid')


class PreparedNativeLoadProbe(PreparedComponentOwnerProbe):
    _probe_version = 5
    _report_type = NativeLoadProbeReport

    def _validate_extra(self, report):
        super()._validate_extra(report)
        evidence = report.native_load
        if evidence is None or not evidence.plan_sha256: return
        from .native_load import plan_native_graph
        from .component_load_child import plan_digest, TARGET
        from .windows_os_policy import owned_os_policy_bytes, parse_os_policy
        policy = parse_os_policy(owned_os_policy_bytes())
        plan = plan_native_graph(self.invocation.cwd, self.inventory, self.invocation.cwd / TARGET,
            policy.component_names | policy.contract_names, dynamic_dependencies={}, dynamic_closure_known=True)
        node = next(n for n in plan.nodes if n.name == TARGET)
        if (evidence.plan_sha256 != plan_digest(plan) or evidence.target_sha256 != node.sha256
            or evidence.node_count != len(plan.nodes) or evidence.edge_count != len(plan.edges)
            or evidence.target_requires_binding != (node.resources.kind == 'requires_os_sxs_binding')):
            raise ValueError('component_protocol_invalid')


def prepare_component_probe(*, base: Path, staging_parent: Path, work_directory: Path,
        max_bytes: int = TREE_LIMIT, timeout_seconds: float = 15):
    return _prepare_component_probe(base=base, staging_parent=staging_parent, work_directory=work_directory,
        max_bytes=max_bytes, timeout_seconds=timeout_seconds, owner=False)


def prepare_component_owner_probe(*, base: Path, staging_parent: Path, work_directory: Path,
        max_bytes: int = TREE_LIMIT, timeout_seconds: float = 15):
    return _prepare_component_probe(base=base, staging_parent=staging_parent, work_directory=work_directory,
        max_bytes=max_bytes, timeout_seconds=timeout_seconds, owner=True)


def prepare_native_load_probe(*, base: Path, staging_parent: Path, work_directory: Path,
        max_bytes: int = TREE_LIMIT, timeout_seconds: float = 20):
    return _prepare_component_probe(base=base, staging_parent=staging_parent, work_directory=work_directory,
        max_bytes=max_bytes, timeout_seconds=timeout_seconds, owner=True, native=True)


def _prepare_component_probe(*, base, staging_parent, work_directory, max_bytes, timeout_seconds, owner, native=False):
    """Preparing queries metadata; actual use needs a separately declared budget."""
    tree = None
    try:
        if (type(max_bytes) is not int or not 0 < max_bytes <= TREE_LIMIT
            or type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= (20 if native else 15)):
            raise ExecutionPreparationError('execution_origin_probe_bounds_invalid')
        _exact_path(work_directory, directory=True)
        trees, files = select_cpython312_base(base)
        tree = prepare_runtime_tree(trees=trees, files=files, staging_parent=staging_parent, max_bytes=max_bytes,
            max_files=10000, owned_files=owned_native_load_probe_files() if native else owned_component_owner_probe_files() if owner else owned_component_probe_files(),
            owned_recipe='native-load-probe-v5' if native else 'component-owner-probe-v4' if owner else 'component-probe-v3')
        if len(tree.inventory.directories) > 3000 or work_directory.is_relative_to(tree.root):
            raise ExecutionPreparationError('execution_origin_probe_bounds_invalid')
        host = PreparedComponentHost(tree, prepare_host_runtime_v2(runtime_root=tree.root, inventory=tree.inventory, device='cpu'))
        environment = (('SystemRoot', str(host.windows_root)), ('WINDIR', str(host.windows_root)),
            ('TEMP', str(work_directory)), ('TMP', str(work_directory)), ('HF_HUB_OFFLINE', '1'), ('TRANSFORMERS_OFFLINE', '1'))
        invocation = PendingInvocation((str(tree.root / 'python.exe'), '-I', '-S', '-B', str(tree.root / ENTRY)),
            tree.root, environment, timeout_seconds)
        result = (PreparedNativeLoadProbe if native else PreparedComponentOwnerProbe if owner else PreparedComponentProbe)(tree, host, invocation)
        result.verify()
        return result
    except Exception:
        if tree is not None: tree.close()
        raise
