"""Parent-only fixed PCM bootstrap diagnostics. No native or launch authority."""
import json
from pathlib import Path
import re

from . import pcm_static_core as core
from .pcm_static_probe import PreparedPCMStaticProbe
from .native_load import plan_native_graph, read_member
from .pe_imports import parse_pe_imports
from .pe_resources import classify_pe_resources
from .prepared import ExecutionPreparationError, NativeExecutionUnsupported
from .windows_loader import require_no_redirection
from .windows_os_policy import read_prepared_os_policy, parse_os_policy

TARGETS = ('python312.dll', 'python3.dll', 'vcruntime140.dll', 'vcruntime140_1.dll')
BASE = ('python.exe', *TARGETS)
STDLIB = ('Lib/os.py', 'Lib/encodings/__init__.py')
HASHLIB_SOURCE = 'Lib/hashlib.py'
HASHLIB_TARGET = 'DLLs/_hashlib.pyd'
HASHLIB_DEPENDENCY = 'libcrypto-3-x64.dll'
PENDING = ('exact_stdlib_native_source_and_import_closure',
           'publisher_source_build_correspondence', 'actual_child_bootstrap_and_host',
           'same_live_child_finders_native_origins_context_search', 'terms_and_normal_worker')
IMPLEMENTATIONS = ('pcm_bootstrap_preflight.py', 'native_load.py', 'pe_imports.py',
    'pe_resources.py', 'pe_bounded.py', 'pe_data.py', 'runtime_primitives.py',
    'windows_loader.py', 'windows_os_policy.py', 'native_manifest.py',
    'pcm_static_core.py', 'pcm_static_probe.py', 'pcm_static_wire.py',
    'omnivoice_pcm_import.py', 'cpu_native_recipe.py')


class _CommandView:
    """Existing no-redirection consumer, with no caller-supplied observations."""
    def __init__(self, probe): self.probe = probe
    @property
    def invocation(self): return self.probe.invocation
    @property
    def inventory(self): return self.probe.boundary.tree.inventory
    @property
    def host_observation(self): return self.probe.host.observation
    def verify(self): return self.probe.verify()


def implementation_identity():
    directory = Path(__file__).resolve().parent
    return core.sha(core.canonical({name: core.sha(core.read_source(directory/name))
                                    for name in IMPLEMENTATIONS}))


def _target_record(root, inventory, target, os_names):
    try:
        plan = plan_native_graph(root, inventory, root/target, os_names)
        return dict(target=target, blocker=None, plan=dict(
            nodes=[dict(name=n.name, size_bytes=n.size_bytes, sha256=n.sha256,
                resources=n.resources.kind, manifest_sha256=n.resources.manifest_sha256)
                for n in plan.nodes], edges=[list(e) for e in plan.edges],
            required_loaded_base=list(plan.required_loaded_base),
            requires_os_sxs_binding=plan.requires_os_sxs_binding,
            requires_dynamic_closure=plan.requires_dynamic_closure))
    except NativeExecutionUnsupported as error:
        code = str(error)
        if not re.fullmatch('execution_[a-z0-9_]{1,90}', code):
            raise ExecutionPreparationError('execution_pcm_bootstrap_result_invalid') from None
        return dict(target=target, blocker=code, plan=None)


def _hashlib_record(root, inventory, entries, os_names):
    source = dict(name=HASHLIB_SOURCE, blocker='execution_pcm_hashlib_source_missing')
    row = entries.get(HASHLIB_SOURCE)
    if row is not None:
        if row.role != 'dependency' or not row.size_bytes:
            raise ExecutionPreparationError('execution_pcm_bootstrap_layout_unsupported')
        raw = core.read_source(root/HASHLIB_SOURCE)
        if len(raw) != row.size_bytes or core.sha(raw) != row.sha256:
            raise ExecutionPreparationError('execution_pcm_bootstrap_source_changed')
        source = dict(name=HASHLIB_SOURCE, blocker=None, size_bytes=len(raw), sha256=core.sha(raw))
    extension = dict(name=HASHLIB_TARGET, blocker='execution_pcm_hashlib_extension_missing')
    target = dict(target=HASHLIB_TARGET, blocker=extension['blocker'], plan=None)
    row = entries.get(HASHLIB_TARGET)
    if row is not None:
        if row.role != 'dependency' or not row.size_bytes:
            raise ExecutionPreparationError('execution_pcm_bootstrap_layout_unsupported')
        raw = read_member(root, row)
        imports = parse_pe_imports(raw)
        resources = classify_pe_resources(raw, role='dll')
        extension = dict(name=HASHLIB_TARGET, blocker=None, size_bytes=row.size_bytes,
            sha256=row.sha256, ordinary=list(imports.ordinary), delayed=list(imports.delayed),
            resources=resources.kind, manifest_sha256=resources.manifest_sha256)
        target = _target_record(root, inventory, HASHLIB_TARGET, os_names)
        if HASHLIB_DEPENDENCY not in imports.ordinary:
            target = dict(target=HASHLIB_TARGET,
                blocker='execution_pcm_hashlib_dependency_changed', plan=None)
        elif target['plan'] is not None and not any(
                edge[0] == HASHLIB_TARGET and edge[1] == HASHLIB_DEPENDENCY
                and not edge[2].startswith('OS:') for edge in target['plan']['edges']):
            target = dict(target=HASHLIB_TARGET,
                blocker='execution_pcm_hashlib_dependency_changed', plan=None)
    return dict(source=source, extension=extension, target=target,
        publisher_build_correspondence_verified=False, initializer_closure_verified=False)


def _record(probe, version=1):
    if type(version) is not int or version not in (1, 2):
        raise ExecutionPreparationError('execution_pcm_bootstrap_version_unsupported')
    if type(probe) is not PreparedPCMStaticProbe:
        raise ExecutionPreparationError('execution_pcm_bootstrap_probe_required')
    inventory = probe.verify()
    root = probe.boundary.tree.root
    identity = implementation_identity()
    policy_bytes = read_prepared_os_policy(runtime_root=root, inventory=inventory)
    policy = parse_os_policy(policy_bytes)
    require_no_redirection(_CommandView(probe))
    entries = {r.name: r for r in inventory.files}
    base = []
    for name in (*BASE, *STDLIB):
        row = entries.get(name)
        if (row is None or row.role != ('interpreter' if name == 'python.exe' else 'dependency')
                or not row.size_bytes):
            raise ExecutionPreparationError('execution_pcm_bootstrap_layout_unsupported')
        if name in BASE:
            # Actual bounded parser/classifier, no mock graph or PE execution.
            raw = read_member(root, row)
            imports = parse_pe_imports(raw)
            resources = classify_pe_resources(raw, role='bootstrap_exe' if name == 'python.exe' else 'dll')
            base.append(dict(name=name, sha256=row.sha256, size_bytes=row.size_bytes,
                ordinary=list(imports.ordinary), delayed=list(imports.delayed),
                resources=resources.kind, manifest_sha256=resources.manifest_sha256))
        else:
            raw = core.read_source(root/name)
            if len(raw) != row.size_bytes or core.sha(raw) != row.sha256:
                raise ExecutionPreparationError('execution_pcm_bootstrap_source_changed')
    os_names = policy.component_names | policy.contract_names
    targets = [_target_record(root, inventory, target, os_names) for target in TARGETS]
    extra = {} if version == 1 else dict(hashlib_prerequisites=
        _hashlib_record(root, inventory, entries, os_names))
    probe.verify()
    if implementation_identity() != identity:
        raise ExecutionPreparationError('execution_pcm_bootstrap_implementation_changed')
    # No expected-report input: these facts are freshly recomputed from bytes.
    return dict(recipe=f'pcm-bootstrap-static-prerequisites-v{version}', version=version,
        implementation_sha256=identity, inventory_sha256=inventory.descriptor.sha256,
        os_policy_sha256=core.sha(policy_bytes), base=base, targets=targets,
        probe_binding_sha256=core.sha(core.canonical(json.loads(probe._request())['binding'])),
        pending=list(PENDING), loading_authorized=False, dispatch_authorized=False, **extra)


class PreparedPCMBootstrapPreflight:
    """Same probe/source/host identity on reuse; a report never permits loading."""
    def __init__(self, probe, *, version=1):
        self._probe = probe
        self._version = version
        self._record = core.canonical(_record(probe, version))
        self._state = 'prepared'

    @property
    def identity_sha256(self): return core.sha(self._record)

    def verify(self):
        if self._state != 'prepared':
            raise ExecutionPreparationError('execution_pcm_bootstrap_preflight_consumed')
        try:
            current = core.canonical(_record(self._probe, self._version))
            if current != self._record:
                raise ExecutionPreparationError('execution_pcm_bootstrap_identity_changed')
            return json.loads(current)  # Detached facts, not a reusable authority object.
        except Exception:
            self._state = 'failed'
            raise

    def require_native_preparation(self):
        raise NativeExecutionUnsupported('execution_native_dependency_closure_unsupported')

    def close(self): self._state = 'closed'  # Caller owns probe and runtime.


def prepare_pcm_bootstrap_preflight(probe):
    result = PreparedPCMBootstrapPreflight(probe)
    result.verify()
    return result


def prepare_pcm_hashlib_preflight(probe):
    """Explicit v2 diagnostic; current file identity is not publisher provenance."""
    result = PreparedPCMBootstrapPreflight(probe, version=2)
    result.verify()
    return result
