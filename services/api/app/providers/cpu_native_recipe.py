"""Owned CPU static recipe and evidence gaps; no loader or dispatch authority."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re

from app.domain.execution_runtime import runtime_path
from .pe_bounded import BoundedPEView, open_bounded_pe, EXPORT_DIRECTORY_LIMIT, EXPORT_LIMIT
from .pe_resources import (read_resource_leaves, classify_resource_view,
    export_dependencies_view, ResourceClassification)
from .runtime_primitives import NativeExecutionUnsupported, _exact_path
from .windows_os_policy import owned_os_policy_bytes, parse_os_policy
from .pcm_static_core import environment_values as _environment_values

PREFIX = 'Lib/site-packages/'
BASE = ('python.exe', 'python312.dll', 'vcruntime140.dll', 'vcruntime140_1.dll',
        'msvcp140.dll', 'vcomp140.dll')
GRAPH_BYTES = 512*1024**2
GRAPH_NODES = 64
GRAPH_EDGES = 512


def _owned():
    path = Path(__file__).with_suffix('.json')
    _exact_path(path, directory=False)
    with path.open('rb') as stream:
        raw = stream.read(64*1024 + 1)
    try:
        value = json.loads(raw)
        if (len(raw) > 64*1024 or value['recipe'] != 'omnivoice-cpu-native-static-v1'
                or type(value['version']) is not int or value['version'] != 1
                or len(value['members']) != 23 or len(value['manifests']) != 3):
            raise ValueError()
        if len({x['name'].casefold() for x in value['members']}) != 23:
            raise ValueError()
        for item in value['members']:
            runtime_path(item['name'])
        for digest, form in value['manifests'].items():
            data = bytes.fromhex(form['hex'])
            if (hashlib.sha256(data).hexdigest() != digest or len(data) != form['size_bytes']
                    or form['ids'] != [24,2,1033] or form['codepage'] not in (0,1252)):
                raise ValueError()
        return value, hashlib.sha256(raw).hexdigest()
    except (ValueError, KeyError, TypeError):
        raise NativeExecutionUnsupported('execution_cpu_owned_recipe_invalid') from None


def _member(policy, name):
    return next((m for m in policy['members'] if m['name'] == name), None)


def classify_cpu_resources(view, *, member_name, size_bytes, sha256):
    """Exact owned whole-member identity plus raw resource bytes; context pending."""
    policy, _ = _owned()
    expected = _member(policy, member_name)
    if (expected is None or expected['size_bytes'] != size_bytes
            or expected['sha256'] != sha256 or type(view) is not BoundedPEView
            or view._source.size != size_bytes or view._source.digest != sha256):
        raise NativeExecutionUnsupported('execution_cpu_member_identity_changed')
    leaves = read_resource_leaves(view)
    manifests = [x for x in leaves or () if x.ids[0] == 24]
    digest = expected['manifest_sha256']
    if digest is None:
        if manifests:
            raise NativeExecutionUnsupported('execution_cpu_manifest_changed')
        return ResourceClassification('no_resources' if leaves is None else 'data_resources', len(leaves or ()))
    form = policy['manifests'][digest]
    if (len(manifests) != 1 or manifests[0].ids != tuple(form['ids'])
            or manifests[0].codepage != form['codepage']
            or manifests[0].data != bytes.fromhex(form['hex'])):
        raise NativeExecutionUnsupported('execution_cpu_manifest_changed')
    return ResourceClassification('requires_private_root_context', len(leaves), digest)


@dataclass(frozen=True)
class PrivateRootPrediction:
    source_sha256: str
    manifest_sha256: str
    source: str
    source_parent: str
    root_flags: int
    metadata: object

    @property
    def dispatch_authorized(self): return False


def compare_private_root_metadata(metadata, *, source, source_sha256, manifest_sha256):
    """Fake/actual metadata comparator. Caller must independently query the file."""
    try:
        policy, _ = _owned()
        _exact_path(source, directory=False)
        _exact_path(source.parent, directory=True)
        if manifest_sha256 not in policy['manifests']:
            raise ValueError()
        if (metadata.format_version != 1 or type(metadata.flags) is not int or metadata.flags != 0
                or metadata.root_manifest != str(source) or metadata.root_configuration
                or Path(metadata.application_directory) != source.parent
                or metadata.path_types != (2,1,2) or len(metadata.assemblies) != 1):
            raise ValueError()
        root = metadata.assemblies[0]
        if (type(root.flags) is not int or not 0 <= root.flags <= 0xffffffff
                or root.identity or root.manifest != str(source) or root.policy or root.directory
                or root.files or root.satellite or (root.manifest_type, root.policy_type) != (2,1)
                or root.manifest_version != (1,0) or root.policy_version != (0,0)):
            raise ValueError()
        # Hash is verified independently by the bounded source reader, not the
        # metadata query. Never turn metadata into a source receipt.
        if (type(source_sha256) is not str or len(source_sha256) != 64
                or any(c not in '0123456789abcdef' for c in source_sha256)):
            raise ValueError()
        return PrivateRootPrediction(source_sha256, manifest_sha256, str(source),
            str(source.parent), root.flags, metadata)
    except (ValueError, AttributeError, TypeError, OSError):
        raise NativeExecutionUnsupported('execution_cpu_private_context_unsupported') from None


def compare_private_root_association(prediction, actual):
    """Root/source/role facts stay exact; never accepts arbitrary query failure."""
    if type(prediction) is not PrivateRootPrediction or actual != prediction.metadata:
        raise NativeExecutionUnsupported('execution_cpu_private_context_changed')


@dataclass(frozen=True)
class CPUEnvironment:
    root: Path
    work_directory: Path
    windows_root: Path
    environment: tuple[tuple[str,str], ...]
    absent_prefix: Path
    expected_userbase: Path

    def canonical_bytes(self):
        return json.dumps({'recipe_sha256': _owned()[1], 'environment': dict(self.environment)},
            sort_keys=True, separators=(',', ':')).encode()

    def verify(self):
        _exact_path(self.root, directory=True)
        if (self.absent_prefix != self.root / '_cpu_disabled'
                or self.expected_userbase != self.absent_prefix / 'userbase'
                or self.environment != _environment_values(self.root, self.work_directory, self.windows_root)):
            raise NativeExecutionUnsupported('execution_cpu_environment_invalid')
        if os.path.lexists(self.absent_prefix):
            raise NativeExecutionUnsupported('execution_cpu_external_probe_changed')




def cpu_environment(*, root, work_directory, windows_root):
    for path in (root, work_directory, windows_root):
        _exact_path(path, directory=True)
    if (work_directory.is_relative_to(root) or windows_root.is_relative_to(root)
            or root.is_relative_to(work_directory) or root.is_relative_to(windows_root)):
        raise NativeExecutionUnsupported('execution_cpu_environment_invalid')
    value = CPUEnvironment(root, work_directory, windows_root,
        _environment_values(root, work_directory, windows_root),
        root / '_cpu_disabled', root / '_cpu_disabled/userbase')
    value.verify()
    return value


def compare_cpu_prefixes(environment, *, exec_prefix, base_exec_prefix, userbase):
    environment.verify()
    if (exec_prefix != str(environment.root) or base_exec_prefix != str(environment.root)
            or userbase != str(environment.expected_userbase)):
        raise NativeExecutionUnsupported('execution_cpu_prefix_observation_changed')


def require_cpu_sources(root, inventory):
    """Pins the source chain and absence facts, without importing it."""
    policy, digest = _owned()
    entries = {item.name: item for item in inventory.files}
    for name, expected in policy['sources'].items():
        entry = entries.get(PREFIX + name)
        if entry is None or entry.role != 'dependency' or entry.sha256 != expected:
            raise NativeExecutionUnsupported('execution_cpu_loader_source_changed')
        path = root / entry.name
        _exact_path(path, directory=False)
        with path.open('rb') as stream: raw = stream.read(4*1024**2 + 1)
        if len(raw) != entry.size_bytes or len(raw) > 4*1024**2 or hashlib.sha256(raw).hexdigest() != expected:
            raise NativeExecutionUnsupported('execution_cpu_loader_source_changed')
    prefix = (PREFIX + 'torio/lib/_torio_ffmpeg').casefold()
    lib = (PREFIX + 'torio/lib/libtorio_ffmpeg').casefold()
    for name in (*inventory.directories, *entries):
        lower = name.casefold()
        if any(lower == p or lower.startswith((p+'.', p+'/')) for p in (prefix, lib)):
            raise NativeExecutionUnsupported('execution_cpu_ffmpeg_exclusion_changed')
    if _owned()[1] != digest:
        raise NativeExecutionUnsupported('execution_cpu_owned_recipe_changed')
    return digest


@dataclass(frozen=True)
class CPUFileFact:
    name: str
    size_bytes: int
    sha256: str
    imports: tuple[str, ...]
    resources: ResourceClassification


@dataclass(frozen=True)
class CPUStaticPlan:
    policy_sha256: str
    os_policy_sha256: str
    roots: tuple[str, ...]
    nodes: tuple[CPUFileFact, ...]
    edges: tuple[tuple[str,str,str], ...]
    excluded_members: tuple[str, ...]
    pending: tuple[tuple[str,str], ...]
    private_policy_sha256: str | None = None

    @property
    def dispatch_authorized(self): return False


def _excluded(name):
    return name.startswith(('torch/bin/', 'functorch/', 'torio/'))


def plan_cpu_facts(facts):
    """Pure diagnostic graph; an observed fact cannot certify dynamic closure."""
    return _plan_cpu_facts(facts)


def plan_cpu_private_facts(facts):
    """Explicit new profile; exact private bytes/terms/context gaps stay visible."""
    from .private_native_recipe import _owned as private_owned
    return _plan_cpu_facts(facts, private_profile=private_owned())


@dataclass(frozen=True)
class CPUAcquiredStaticPlan:
    plan: CPUStaticPlan
    acquired_policy_sha256: str
    import_supplement_sha256: str

    @property
    def dispatch_authorized(self): return False


def plan_cpu_acquired_facts(facts):
    """Same bounded engine, separate source identities; old profiles unchanged."""
    from .acquired_native_recipe import (owned_acquired_bytes,parse_acquired_descriptor,
        describe_acquired_sources,describe_acquired_import_supplement)
    if (type(facts) is not tuple or len(facts)>GRAPH_NODES
            or any(type(f) is not CPUFileFact or type(f.name) is not str for f in facts)):
        raise NativeExecutionUnsupported('execution_cpu_graph_invalid')
    policy,identity=parse_acquired_descriptor(owned_acquired_bytes())
    rows={r['slot']:r for r in policy['members']}
    known=tuple(f for f in facts if f.name in rows and rows[f.name]['dependencies'] is not None)
    extra=tuple(f for f in facts if f.name in rows and rows[f.name]['dependencies'] is None)
    if len(extra)>1:raise NativeExecutionUnsupported('execution_acquired_member_changed')
    observed=describe_acquired_sources(known,descriptor_sha256=identity)
    supplement=describe_acquired_import_supplement(extra[0] if extra else None)
    plan=_plan_cpu_facts(facts,acquired_profile=(policy,observed.pending))
    return CPUAcquiredStaticPlan(plan,identity,supplement.supplement_sha256)


def _plan_cpu_facts(facts, *, private_profile=None, acquired_profile=None):
    policy, digest = _owned()
    os_bytes = owned_os_policy_bytes()
    os_policy = parse_os_policy(os_bytes)
    roots = tuple(sorted(PREFIX+m['name'] for m in policy['members'] if not _excluded(m['name'])))
    roots += tuple(name for name in BASE if name == 'python.exe')
    eligible = {PREFIX+m['name'] for m in policy['members'] if not _excluded(m['name'])} | set(BASE)
    if private_profile is not None:
        from .private_native_recipe import AUDIO_SLOTS, require_private_fact, private_pending
        private_policy, private_identity = private_profile
        roots += AUDIO_SLOTS
        eligible |= set(AUDIO_SLOTS)
    if acquired_profile is not None:
        if private_profile is not None:raise NativeExecutionUnsupported('execution_cpu_profile_conflict')
        acquired_policy,acquired_pending=acquired_profile
        acquired_slots=tuple(row['slot'] for row in acquired_policy['members'])
        roots=tuple(dict.fromkeys(roots+acquired_slots))
        eligible |= set(acquired_slots)
    if type(facts) is not tuple or len(facts) > GRAPH_NODES or any(type(f) is not CPUFileFact for f in facts):
        raise NativeExecutionUnsupported('execution_cpu_graph_invalid')
    by_slot, candidates = {}, {}
    for fact in facts:
        runtime_path(fact.name)
        if fact.name not in eligible or fact.name in by_slot:
            raise NativeExecutionUnsupported('execution_cpu_graph_member_unsupported')
        if (type(fact.size_bytes) is not int or fact.size_bytes <= 0
                or type(fact.sha256) is not str or not re.fullmatch('[a-f0-9]{64}', fact.sha256)
                or type(fact.imports) is not tuple or len(fact.imports) > GRAPH_EDGES
                or any(type(n) is not str or not re.fullmatch(r'[a-z0-9_][a-z0-9_.-]*\.(dll|pyd)', n)
                    or '..' in n for n in fact.imports)
                or fact.imports != tuple(sorted(set(fact.imports)))):
            raise NativeExecutionUnsupported('execution_cpu_graph_invalid')
        native = _member(policy, fact.name.removeprefix(PREFIX))
        if private_profile is not None and native is None:
            require_private_fact(private_policy, fact)
        if native and (fact.size_bytes != native['size_bytes'] or fact.sha256 != native['sha256']):
            raise NativeExecutionUnsupported('execution_cpu_member_identity_changed')
        if (type(fact.resources) is not ResourceClassification
                or fact.resources.kind not in ('no_resources','data_resources',
                    'requires_private_root_context','requires_os_sxs_binding')
                or native and (fact.resources.manifest_sha256 != native['manifest_sha256']
                    or native['manifest_sha256'] is not None
                    and fact.resources.kind != 'requires_private_root_context')):
            raise NativeExecutionUnsupported('execution_cpu_graph_resource_changed')
        basename = Path(fact.name).name.casefold()
        if acquired_profile is not None and basename in os_policy.component_names|os_policy.contract_names:
            raise NativeExecutionUnsupported('execution_cpu_graph_os_shadow')
        if basename in candidates:
            raise NativeExecutionUnsupported('execution_cpu_graph_basename_collision')
        by_slot[fact.name], candidates[basename] = fact, fact.name
    pending = {('dynamic_closure', 'not_verified'), ('private_context_prediction', 'not_verified'),
        ('startup_import_origins', 'not_verified'), ('audio_sources', 'soundfile_cffi_libsndfile'),
        ('compiler_sources', 'fixed_vc140_set_required')}
    if private_profile is not None: pending |= private_pending(private_policy)
    if acquired_profile is not None: pending |= set(acquired_pending)
    edges, nodes, total = set(), {}, 0
    edge_count = 0
    todo = list(roots)
    while todo:
        slot = todo.pop()
        if slot in nodes: continue
        fact = by_slot.get(slot)
        if fact is None:
            pending.add(('missing_private_member', slot)); continue
        total += fact.size_bytes
        if total > GRAPH_BYTES or len(nodes) >= GRAPH_NODES:
            raise NativeExecutionUnsupported('execution_cpu_graph_limit')
        nodes[slot] = fact
        for name in fact.imports:
            edge_count += 1
            if edge_count > GRAPH_EDGES:
                raise NativeExecutionUnsupported('execution_cpu_graph_limit')
            if name in os_policy.component_names | os_policy.contract_names:
                resolved = 'OS:' + name
            elif name in candidates:
                resolved = candidates[name]; todo.append(resolved)
            else:
                pending.add(('unresolved_dependency', slot + ':' + name)); continue
            edges.add((slot, name, resolved))
            if len(edges) > GRAPH_EDGES:
                raise NativeExecutionUnsupported('execution_cpu_graph_limit')
    return CPUStaticPlan(digest, hashlib.sha256(os_bytes).hexdigest(), roots,
        tuple(nodes[k] for k in sorted(nodes)), tuple(sorted(edges)),
        tuple(PREFIX+m['name'] for m in policy['members'] if _excluded(m['name'])), tuple(sorted(pending)),
        private_identity if private_profile is not None else None)


def inspect_cpu_runtime(root, inventory):
    """Actual file facts for the selected subset; all runtime conditions pending."""
    _exact_path(root, directory=True)
    policy, digest = _owned()
    entries = {e.name: e for e in inventory.files}
    facts = []
    slots = [PREFIX+m['name'] for m in policy['members'] if not _excluded(m['name'])] + list(BASE)
    for slot in slots:
        entry = entries.get(slot)
        if entry is None: continue
        if entry.role != ('interpreter' if slot == 'python.exe' else 'dependency'):
            raise NativeExecutionUnsupported('execution_cpu_graph_member_unsupported')
        member = _member(policy, slot.removeprefix(PREFIX))
        if member and (entry.size_bytes != member['size_bytes'] or entry.sha256 != member['sha256']):
            raise NativeExecutionUnsupported('execution_cpu_member_identity_changed')
        with open_bounded_pe(root / slot, size_bytes=entry.size_bytes, sha256=entry.sha256) as view:
            resources = (classify_cpu_resources(view, member_name=member['name'],
                size_bytes=entry.size_bytes, sha256=entry.sha256) if member else
                classify_resource_view(view, role='bootstrap_exe' if slot == 'python.exe' else 'dll'))
            forwarders = export_dependencies_view(view, directory_limit=EXPORT_DIRECTORY_LIMIT,
                export_limit=EXPORT_LIMIT, suffixes=('.dll','.pyd'))
            imports = tuple(sorted(set(view.imports.dependencies) | set(forwarders)))
            facts.append(CPUFileFact(slot, entry.size_bytes, entry.sha256, imports, resources))
    if _owned()[1] != digest:
        raise NativeExecutionUnsupported('execution_cpu_owned_recipe_changed')
    return plan_cpu_facts(tuple(facts))
