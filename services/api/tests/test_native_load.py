"""One-use load outcomes with fake ABI; no live LoadLibrary/child calls."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.providers.native_load import NativeLoadSession, NativeDependencyPlan, NativeNode, compare_context_transition
from app.providers.pe_resources import ResourceClassification
from app.providers.runtime_primitives import NativeExecutionUnsupported


@pytest.fixture
def lifecycle(tmp_path):
    root = tmp_path
    node = NativeNode('DLLs/target.pyd', 1, 'a'*64, ResourceClassification('requires_os_sxs_binding', 1, 'b'*64))
    plan = NativeDependencyPlan(node.name, (node,), (), (), True, False)
    context = ('effective', (('base.dll', 'baseline-binding'),))
    class Guard:
        def __init__(self):
            self.root, self.baseline = root, frozenset((root / 'python.exe',))
            self.current, self.native, self.problem, self.calls = context, self.baseline, None, []
        def phase(self, name): self.calls.append(name)
        def verify(self):
            if self.problem == 'changed' and 'load' in self.calls:
                raise NativeExecutionUnsupported('execution_fixed_runtime_changed')
        def contexts(self): return self.current
        def origins(self):
            if self.problem == 'unknown' and 'postload' in self.calls:
                raise NativeExecutionUnsupported('execution_loaded_origin_unsupported')
            return self.native
        def plan(self): return replace(plan, requires_dynamic_closure=self.problem == 'dynamic')
        def loaded_base(self, plan, native):
            if self.problem == 'base': raise NativeExecutionUnsupported('execution_native_plan_base_not_loaded')
        def predict(self, plan):
            if self.problem == 'prediction': raise NativeExecutionUnsupported('execution_component_prediction_mismatch')
    guard = Guard()
    class Reader:
        def restrict_search(self):
            if guard.problem == 'search': raise NativeExecutionUnsupported('execution_dll_search_unsupported')
        def load(self, path):
            assert path == root / plan.target
            if guard.problem == 'load': raise NativeExecutionUnsupported('execution_target_load_failed')
            if guard.problem != 'missing': guard.native |= frozenset((path,))
            if guard.problem == 'unplanned': guard.native |= frozenset((root / 'unplanned.dll',))
            added = () if guard.problem == 'missing_context' else ((plan.target, 'verified-target-binding'),)
            guard.current = ('other' if guard.problem == 'default' else context[0],
                () if guard.problem == 'baseline_context' else context[1] + added)
            return 11
        def release(self, handle):
            assert handle == 11
            guard.calls.append('free')
            if guard.problem == 'release': raise NativeExecutionUnsupported('execution_target_release_failed')
    return NativeLoadSession(Reader(), guard), guard


def test_handle_available_only_after_postchecks_and_reference_released_once(lifecycle):
    session, guard = lifecycle
    session.begin()
    assert session.load() == 11 and session.state == 'loaded'
    with pytest.raises(NativeExecutionUnsupported): session.load()
    session.close(); session.close()
    assert session.handle is None and session.release_attempts == 1
    assert guard.calls == ['preflight', 'load', 'postload', 'release', 'free']


@pytest.mark.parametrize('problem', ['dynamic', 'base', 'prediction', 'search', 'load', 'unknown', 'missing',
    'unplanned', 'missing_context', 'default', 'baseline_context', 'changed'])
def test_failure_consumes_and_postload_failures_release_without_exposing_handle(lifecycle, problem):
    session, guard = lifecycle
    guard.problem = problem
    with pytest.raises(NativeExecutionUnsupported):
        session.begin(); session.load()
    assert session.state in ('failed', 'closed') and session.handle is None
    with pytest.raises(NativeExecutionUnsupported): session.begin()
    if problem in ('dynamic', 'base', 'prediction', 'search'): assert session.load_attempts == 0
    if problem not in ('dynamic', 'base', 'prediction', 'search', 'load'): assert session.release_attempts == 1


def test_release_failure_is_consumed_and_cannot_retry_native_reference(lifecycle):
    session, guard = lifecycle
    session.begin(); session.load(); guard.problem = 'release'
    with pytest.raises(NativeExecutionUnsupported, match='release_failed'): session.close()
    session.close()
    assert session.release_attempts == 1 and session.handle is None and session.state == 'closed'


def test_primary_postcheck_error_keeps_cleanup_failure(lifecycle):
    session, guard = lifecycle
    session.begin(); guard.problem = 'unknown'
    session.reader.release = lambda _: (_ for _ in ()).throw(NativeExecutionUnsupported('execution_target_release_failed'))
    with pytest.raises(NativeExecutionUnsupported, match='loaded_origin_unsupported') as error: session.load()
    assert error.value.cleanup_code == 'execution_target_release_failed'
    assert session.release_attempts == 1


def test_loaded_target_reference_does_not_claim_unload(lifecycle):
    session, guard = lifecycle
    guard.native |= frozenset((guard.root / 'DLLs/target.pyd',))
    session.begin(); session.load(); session.close()
    assert guard.root / 'DLLs/target.pyd' in guard.native


def test_new_associated_context_must_belong_to_a_planned_manifest(tmp_path):
    plan = NativeDependencyPlan('target.dll', (), (), (), False, False)
    before = ('effective', (('base.dll', 'verified'),))
    after = ('effective', (*before[1], ('unplanned.dll', 'verified')))
    with pytest.raises(NativeExecutionUnsupported, match='context_changed'):
        compare_context_transition(before, after, plan, frozenset(), tmp_path)
