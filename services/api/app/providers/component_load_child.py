"""Owned protocol5: one fixed CPython DLL mapping/reference, no export calls.

Target selection is code-owned. This limited operation does not establish
Python module initialization, arbitrary dynamic imports or model readiness.
"""
from dataclasses import asdict
import hashlib
import json
import struct
from types import SimpleNamespace

from .common_controls_binding import _compare_common_controls_v2
from .component_contexts import normalized_binding
from .native_load import (NativeLoadSession, ROOT_PRIVATE, plan_native_graph,
    read_member, compare_context_transition)
from .runtime_primitives import NativeExecutionUnsupported

TARGET = 'DLLs/_bz2.pyd'
RECIPE = 'cpython312-bz2-mapping-v1'


def plan_digest(plan):
    return hashlib.sha256(json.dumps(asdict(plan), sort_keys=True,
        separators=(',', ':'), ensure_ascii=True).encode('ascii')).hexdigest()


class Bz2MappingProbe:
    def __init__(self):
        self.evidence = dict(recipe=RECIPE, target=TARGET, target_sha256='', plan_sha256='',
            node_count=0, edge_count=0, load_attempts=0, release_attempts=0,
            postload_verified=False, release_verified=False, target_binding=None, cleanup_code='')
        self.evidence['target_requires_binding'] = False

    def run(self, *, root, inventory, windows_root, prediction, activation, reader,
            scanner, origins, contexts, verify, report):
        entries = tuple(SimpleNamespace(**row) for row in inventory['files'])
        fixed = {row.name: row for row in entries}
        descriptor = SimpleNamespace(files=entries, directories=tuple(inventory['directories']))
        evidence = self.evidence
        class Guard:
            def __init__(self):
                self.root, self.baseline = root, None
            def phase(self, name): report['stage'] = name
            def verify(self): verify()
            def origins(self):
                actual = origins()
                if self.baseline is None: self.baseline = actual
                return actual
            def contexts(self): return contexts()
            def plan(self):
                contracts = scanner.load_profile(root, include_contracts=True)
                plan = plan_native_graph(root, descriptor, root / TARGET, contracts,
                    dynamic_dependencies={}, dynamic_closure_known=True)
                evidence.update(target_sha256=fixed[TARGET].sha256, plan_sha256=plan_digest(plan),
                    node_count=len(plan.nodes), edge_count=len(plan.edges), target_requires_binding=
                    next(n for n in plan.nodes if n.name == TARGET).resources.kind == 'requires_os_sxs_binding')
                return plan
            def loaded_base(self, plan, native):
                nodes = {n.name: n for n in plan.nodes}
                for name in plan.required_loaded_base:
                    entry, node = fixed.get(name), nodes.get(name)
                    if name not in ROOT_PRIVATE or entry is None or node is None or (
                            entry.sha256 != node.sha256 or entry.size_bytes != node.size_bytes):
                        raise NativeExecutionUnsupported('execution_native_plan_source_changed')
                    read_member(root, entry)
                    if root / name not in native:
                        raise NativeExecutionUnsupported('execution_native_plan_base_not_loaded')
            def predict(self, plan):
                for node in plan.nodes:
                    payload = read_member(root, fixed[node.name])
                    if node.name != 'python.exe':
                        pe = struct.unpack_from('<I', payload, 60)[0]
                        if not struct.unpack_from('<H', payload, pe + 22)[0] & 0x2000:
                            raise NativeExecutionUnsupported('execution_native_target_not_dll')
                    if node.resources.kind != 'requires_os_sxs_binding' or node.name == 'python.exe': continue
                    source = root / node.name
                    metadata = activation.inspect_pe(source, node.sha256,
                        role='dll', language_policy='current_ui')
                    binding = _compare_common_controls_v2(metadata, source=source,
                        expected_source_sha256=node.sha256, application_directory=source.parent,
                        windows_root=windows_root, source_role='dll', query_role='created')
                    if normalized_binding(binding) != normalized_binding(prediction):
                        raise NativeExecutionUnsupported('execution_component_prediction_mismatch')
        guard = Guard()
        session = NativeLoadSession(reader, guard)
        try:
            session.begin()
            session.load()
            _, associated = contexts()
            binding = dict(associated).get(TARGET)
            evidence['target_binding'] = asdict(binding) if binding is not None else None
            evidence['postload_verified'] = True
            session.close()
            verify()
            native = origins()
            after = contexts()
            compare_context_transition(session.before, after, session.plan, native, root)
            evidence['release_verified'] = True
            return after
        except Exception as error:
            try: session.close()
            except Exception as cleanup: evidence['cleanup_code'] = str(cleanup)
            evidence['cleanup_code'] = evidence['cleanup_code'] or getattr(error, 'cleanup_code', '')
            raise
        finally:
            evidence.update(load_attempts=session.load_attempts, release_attempts=session.release_attempts)
