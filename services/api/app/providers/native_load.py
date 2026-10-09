"""Stdlib-only fixed native graph and one-use controlled-load lifecycle.

Owned diagnostic construction supplies guards. No provider/receipt authority,
arbitrary callbacks into native code, model loading or download lives here.
"""
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re

from .pe_imports import PE_LIMIT, parse_pe_imports
from .pe_resources import classify_pe_resources, private_export_dependencies, ResourceClassification
from .runtime_primitives import NativeExecutionUnsupported, _exact_path, require_search_topology

FILE_LIMIT = 64
EDGE_LIMIT = 512
BYTE_LIMIT = 256 * 1024 * 1024
ROOT_PRIVATE = frozenset(('python312.dll', 'vcruntime140.dll', 'vcruntime140_1.dll'))


@dataclass(frozen=True)
class NativeNode:
    name: str
    size_bytes: int
    sha256: str
    resources: ResourceClassification


@dataclass(frozen=True)
class NativeDependencyPlan:
    target: str
    nodes: tuple[NativeNode, ...]
    edges: tuple[tuple[str, str, str], ...]
    required_loaded_base: tuple[str, ...]
    requires_os_sxs_binding: bool
    requires_dynamic_closure: bool

    @property
    def dispatch_authorized(self): return False


def read_member(root, entry):
    try:
        path = root / entry.name
        _exact_path(path, directory=False)
        if type(entry.size_bytes) is not int or not 0 < entry.size_bytes <= PE_LIMIT:
            raise ValueError()
        with path.open('rb') as source: payload = source.read(entry.size_bytes + 1)
        if len(payload) != entry.size_bytes or hashlib.sha256(payload).hexdigest() != entry.sha256:
            raise ValueError()
        return payload
    except (OSError, ValueError):
        raise NativeExecutionUnsupported('execution_native_plan_source_changed') from None


def plan_native_graph(root, inventory, target, os_names, *, dynamic_dependencies=None,
                      dynamic_closure_known=False, limits=(FILE_LIMIT, EDGE_LIMIT, BYTE_LIMIT)):
    """Caller independently verifies complete inventory/base/owned OS recipe."""
    file_limit, edge_limit, byte_limit = limits
    if not isinstance(target, Path) or target.suffix.casefold() not in ('.dll', '.pyd'):
        raise NativeExecutionUnsupported('execution_target_selection_unsupported')
    _exact_path(target, directory=False)
    if not target.is_relative_to(root):
        raise NativeExecutionUnsupported('execution_target_selection_unsupported')
    require_search_topology(tuple(e.name for e in inventory.files), inventory.directories,
        os_names, target_parent=target.parent.relative_to(root))
    entries = {e.name: e for e in inventory.files}
    selected = target.relative_to(root).as_posix()
    if selected not in entries: raise NativeExecutionUnsupported('execution_target_selection_unsupported')
    candidates = {}
    for entry in inventory.files:
        path = root / entry.name
        if path.suffix.casefold() in ('.dll', '.pyd') and (path.parent == target.parent or entry.name in ROOT_PRIVATE):
            name = path.name.casefold()
            if name in candidates: raise NativeExecutionUnsupported('execution_dll_basename_collision')
            candidates[name] = entry.name
    if len(candidates) > file_limit: raise NativeExecutionUnsupported('execution_native_plan_limit')
    if type(dynamic_closure_known) is not bool: raise NativeExecutionUnsupported('execution_native_plan_recipe_unsupported')
    dynamic = {} if dynamic_dependencies is None else dynamic_dependencies
    if type(dynamic) is not dict or len(dynamic) > file_limit:
        raise NativeExecutionUnsupported('execution_native_plan_recipe_unsupported')
    for slot, names in dynamic.items():
        if slot not in set(candidates.values()) | {'python.exe'} or type(names) is not tuple or len(names) > edge_limit:
            raise NativeExecutionUnsupported('execution_native_plan_recipe_unsupported')
        if any(type(n) is not str or not re.fullmatch(r'[a-z0-9_][a-z0-9_.-]*\.dll', n) or '..' in n for n in names):
            raise NativeExecutionUnsupported('execution_native_plan_recipe_unsupported')
    nodes, edges, required, total = {}, set(), set(), 0
    pending = [selected, 'python.exe']
    while pending:
        slot = pending.pop()
        if slot in nodes: continue
        if len(nodes) >= file_limit: raise NativeExecutionUnsupported('execution_native_plan_limit')
        entry = entries.get(slot)
        if entry is None or entry.role != ('interpreter' if slot == 'python.exe' else 'dependency'):
            raise NativeExecutionUnsupported('execution_native_plan_member_unsupported')
        total += entry.size_bytes
        if total > byte_limit: raise NativeExecutionUnsupported('execution_native_plan_limit')
        payload = read_member(root, entry)
        resources = classify_pe_resources(payload, role='bootstrap_exe' if slot == 'python.exe' else 'dll')
        nodes[slot] = NativeNode(slot, entry.size_bytes, entry.sha256, resources)
        dependencies = set(parse_pe_imports(payload).dependencies)
        dependencies.update(private_export_dependencies(payload))
        dependencies.update(dynamic.get(slot, ()))
        for name in sorted(dependencies):
            if name in os_names: resolved = 'OS:' + name
            elif name in candidates:
                resolved = candidates[name]
                if (root / resolved).parent != target.parent:
                    if resolved not in ROOT_PRIVATE:
                        raise NativeExecutionUnsupported('execution_native_plan_cross_directory_unsupported')
                    required.add(resolved)
                pending.append(resolved)
            else: raise NativeExecutionUnsupported('execution_native_plan_dependency_unknown')
            edges.add((slot, name, resolved))
            if len(edges) > edge_limit: raise NativeExecutionUnsupported('execution_native_plan_limit')
    return NativeDependencyPlan(selected, tuple(nodes[k] for k in sorted(nodes)), tuple(sorted(edges)),
        tuple(sorted(required)), any(n.resources.kind == 'requires_os_sxs_binding' for n in nodes.values()),
        not dynamic_closure_known)


def compare_context_transition(before, after, plan, origins, root):
    """Keep prior role evidence; independently verified new contexts must be planned."""
    effective, associated = before
    current, added = after
    old, new = dict(associated), dict(added)
    if current != effective or any(new.get(name) != binding for name, binding in old.items()):
        raise NativeExecutionUnsupported('execution_component_context_changed')
    manifest_nodes = {n.name for n in plan.nodes if n.resources.kind == 'requires_os_sxs_binding' and n.name != 'python.exe'}
    if set(new) - set(old) - manifest_nodes:
        raise NativeExecutionUnsupported('execution_component_context_changed')
    if any(root / name in origins and name not in new for name in manifest_nodes):
        raise NativeExecutionUnsupported('execution_target_context_missing')


class NativeLoadSession:
    """One target/reference; guards derive facts in the actual owned child."""
    def __init__(self, reader, guard):
        self.reader, self.guard = reader, guard
        self.state, self.handle, self.load_attempts, self.release_attempts = 'new', None, 0, 0
        self.plan = self.before = None
        self._borrows = set()

    def borrow_handle(self):
        if (self.state != 'loaded' or type(self.handle) is not int
                or not 0 < self.handle <= 0xffffffffffffffff):
            raise NativeExecutionUnsupported('execution_native_handle_not_loaded')
        reference = NativeHandleBorrow(self)
        self._borrows.add(reference)
        return reference

    def begin(self):
        if self.state != 'new': raise NativeExecutionUnsupported('execution_loader_consumed')
        self.state = 'failed'
        self.guard.verify()
        self.guard.origins()
        self.before = self.guard.contexts()
        self.reader.restrict_search()
        self.guard.verify()
        self.guard.origins()
        if self.guard.contexts() != self.before:
            raise NativeExecutionUnsupported('execution_component_context_changed')
        self.state = 'ready'

    def load(self):
        if self.state != 'ready': raise NativeExecutionUnsupported('execution_loader_consumed')
        self.state = 'failed'
        try:
            self.guard.phase('preflight')
            self.plan = self.guard.plan()
            if self.plan.requires_dynamic_closure:
                raise NativeExecutionUnsupported('execution_native_dynamic_closure_unsupported')
            native = self.guard.origins()
            self.guard.loaded_base(self.plan, native)
            self.guard.predict(self.plan)
            self.guard.verify()
            if self.guard.contexts() != self.before:
                raise NativeExecutionUnsupported('execution_component_context_changed')
            self.guard.phase('load')
            self.load_attempts += 1
            self.handle = self.reader.load(self.guard.root / self.plan.target)
            if not self.handle: raise NativeExecutionUnsupported('execution_target_load_failed')
            self.guard.phase('postload')
            native = self.guard.origins()
            if self.guard.root / self.plan.target not in native:
                raise NativeExecutionUnsupported('execution_target_origin_missing')
            graph = {self.guard.root / node.name for node in self.plan.nodes}
            if any(path.is_relative_to(self.guard.root) and path not in graph for path in native - self.guard.baseline):
                raise NativeExecutionUnsupported('execution_native_unplanned_origin')
            compare_context_transition(self.before, self.guard.contexts(), self.plan, native, self.guard.root)
            self.guard.verify()
            self.state = 'loaded'
            return self.handle
        except Exception as failure:
            try: self.close()
            except Exception as cleanup:
                failure.cleanup_code = str(cleanup)
            raise

    def close(self):
        if self.state == 'closed': return
        if self._borrows:
            raise NativeExecutionUnsupported('execution_native_handle_in_use')
        self.state = 'closed'
        if self.handle is not None:
            handle, self.handle = self.handle, None
            self.guard.phase('release')
            self.release_attempts += 1
            self.reader.release(handle)

    def __enter__(self): self.begin(); return self
    def __exit__(self, *_): self.close()


class NativeHandleBorrow:
    """Non-serializable same-session lease; never loads or releases native code."""
    def __init__(self, session):
        self._session, self._handle, self._closed = session, session.handle, False

    def require(self, session):
        if (session is not self._session or self._closed or self not in session._borrows
                or session.state != 'loaded' or session.handle != self._handle):
            raise NativeExecutionUnsupported('execution_native_handle_borrow_changed')
        return self._handle

    def close(self):
        if self._closed: return
        self._closed = True
        self._session._borrows.discard(self)
