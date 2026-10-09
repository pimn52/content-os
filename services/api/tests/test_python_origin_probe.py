"""Synthetic fixed trees, ABI readers and subprocess transport; no real child."""
from dataclasses import replace
import io
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from app.providers import host_runtime, python_origin_child as child, windows_native
from app.providers import runtime_primitives
from app.providers import python_origin_probe as probe
from app.providers.prepared import ExecutionPreparationError
from app.providers.windows_os_policy import owned_os_policy_bytes, parse_os_policy
from test_python_startup import layout


@pytest.fixture
def setup(layout, monkeypatch):
    base = layout["trees"][0].source.parent
    for name in ("vcruntime140.dll", "vcruntime140_1.dll"):
        (base / name).write_bytes(b"synthetic VC runtime")
    system = layout["windows_root"] / "System32"
    for name in parse_os_policy(owned_os_policy_bytes()).component_names:
        (system / name).write_bytes(b"synthetic OS")
    facts = {"value": windows_native.WindowsFacts(system.parent, 0x8664, 0, 26100, 1, system)}
    monkeypatch.setattr(host_runtime.sys, "platform", "win32")
    monkeypatch.setattr(host_runtime, "_read_windows_facts", lambda: facts["value"])
    pkg = ModuleType("_content_os_host")
    pkg.__path__ = []
    monkeypatch.setitem(__import__("sys").modules, "_content_os_host", pkg)
    monkeypatch.setitem(__import__("sys").modules, "_content_os_host.windows_native", windows_native)
    monkeypatch.setitem(__import__("sys").modules, "_content_os_host.runtime_primitives", runtime_primitives)
    selection = dict(base=base, staging_parent=layout["staging_parent"], work_directory=layout["work_directory"])
    return selection, facts


def report(request, **changes):
    value = dict(probe_version=2, nonce=request["nonce"], passed=True, stage="complete",
        inventory_sha256=__import__("hashlib").sha256(child.canonical(request["inventory"])).hexdigest(),
        host_runtime=request["host_runtime"], native_count=2, python_count=1, code="", unknown_module="",
        blocked_origins=[])
    value.update(changes)
    return value


def test_owned_diagnostic_tree_and_fixed_transport_with_no_native_execute(setup, monkeypatch):
    selection, _ = setup
    calls = []
    def run(argv, **kwargs):
        request = json.loads(kwargs["input"])
        root = kwargs["cwd"]
        assert argv == [str(root / "python.exe"), "-I", "-S", "-B", str(root / probe.ENTRY)]
        assert kwargs["shell"] is False and kwargs["check"] is False and kwargs["timeout"] == 15
        assert not {"PATH", "PYTHONPATH", "PYTHONHOME", "CUDA_VISIBLE_DEVICES"} & set(kwargs["env"])
        assert request["host_runtime"]["device"] == {"kind": "cpu"}
        names = {entry["name"] for entry in request["inventory"]["files"]}
        assert not any("omnivoice" in name for name in names)
        assert "Lib/site-packages/_content_os_host/windows_native.py" in names
        assert len(probe.owned_probe_files()) == 8
        kwargs["stdout"].write(child.canonical(report(request)))
        calls.append(request)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(probe.subprocess, "run", run)
    with probe.prepare_origin_probe(**selection) as prepared:
        result = prepared.run_probe()
        assert result.passed
        with pytest.raises(ExecutionPreparationError, match="consumed"): prepared.run_probe()
    assert len(calls) == 1
    assert not list(selection["staging_parent"].iterdir())


@pytest.mark.parametrize("problem", ["nonce", "digest", "host", "code", "stage", "count", "returncode", "bool_version",
    "unknown", "empty", "large", "post_tree", "post_host", "timeout", "launch"])
def test_transport_and_revalidation_failures_consume_without_retry(setup, monkeypatch, problem):
    selection, facts = setup
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        if problem == "timeout": raise probe.subprocess.TimeoutExpired(argv, 15)
        if problem == "launch": raise OSError()
        request = json.loads(kwargs["input"])
        value = report(request)
        if problem == "nonce": value["nonce"] = "0" * 64
        elif problem == "digest": value["inventory_sha256"] = "0" * 64
        elif problem == "host": value["host_runtime"] = dict(request["host_runtime"], revision=2)
        elif problem == "code": value["code"] = "execution_false_success"
        elif problem == "stage": value["stage"] = "baseline"
        elif problem == "count": value["native_count"] = 0
        elif problem == "bool_version": value["probe_version"] = True
        elif problem == "unknown": value["extra"] = "not an observation"
        elif problem == "post_tree": (kwargs["cwd"] / "added.py").write_bytes(b"unlisted")
        elif problem == "post_host": facts["value"] = replace(facts["value"], revision=2)
        payload = b"" if problem == "empty" else b"x" * 65537 if problem == "large" else child.canonical(value)
        kwargs["stdout"].write(payload)
        return SimpleNamespace(returncode=2 if problem == "returncode" else 0)
    monkeypatch.setattr(probe.subprocess, "run", run)
    with probe.prepare_origin_probe(**selection) as prepared:
        with pytest.raises(ExecutionPreparationError): prepared.run_probe()
        with pytest.raises(ExecutionPreparationError, match="consumed"): prepared.run_probe()
    assert len(calls) == 1
    assert not list(selection["staging_parent"].iterdir())


@pytest.mark.parametrize("problem", ["tree", "host"])
def test_prelaunch_change_never_starts_a_child(setup, monkeypatch, problem):
    selection, facts = setup
    monkeypatch.setattr(probe.subprocess, "run", lambda *args, **kwargs: pytest.fail("no launch"))
    with probe.prepare_origin_probe(**selection) as prepared:
        if problem == "tree": (prepared.invocation.cwd / "foreign.py").write_bytes(b"added")
        else: facts["value"] = replace(facts["value"], revision=2)
        with pytest.raises(ExecutionPreparationError): prepared.run_probe()


class Reader:
    def __init__(self, root, system, calls, problem=None):
        self.root, self.system, self.calls, self.problem = root, system, calls, problem
    def native_origins(self):
        self.calls.append("origins")
        result = (self.root / "python.exe", self.system / "kernel32.dll")
        if self.problem == "unknown":
            path = self.system / "unknown-runtime.dll"
            path.write_bytes(b"unapproved OS component")
            result += (path,)
        return result
    def require_no_activation(self):
        self.calls.append("activation")
        if self.problem == "active": raise runtime_primitives.NativeExecutionUnsupported("execution_activation_context_unsupported")
    def restrict_search(self): self.calls.append("search")


@pytest.mark.parametrize("problem", [None, "unknown", "active", "tree", "host", "recheck_host"])
def test_child_independent_whole_tree_and_actual_readers(setup, problem):
    selection, facts = setup
    with probe.prepare_origin_probe(**selection) as prepared:
        root = prepared.invocation.cwd
        request = {"probe_version": 1, "nonce": "a" * 64, "inventory": prepared.inventory.model_dump(mode="json"),
            "host_runtime": prepared.host_observation.canonical()}
        calls = []
        module = ModuleType("__main__")
        module.__file__ = str(root / probe.ENTRY)
        if problem == "tree": (root / "extra.py").write_bytes(b"added")
        if problem == "host": facts["value"] = replace(facts["value"], revision=2)
        def fact_reader():
            current = facts["value"]
            if problem == "recheck_host" and "search" in calls: return replace(current, revision=2)
            return current
        result = child.observe(root, request, {"__main__": module},
            lambda system: Reader(root, system, calls, problem), fact_reader)
        assert result["passed"] == (problem is None)
        if problem is None:
            assert result["stage"] == "complete"
            assert calls == ["origins", "activation", "search", "origins"]
        if problem in ("tree", "host"): assert calls == []
        if problem == "unknown":
            assert result["unknown_module"] == "unknown-runtime.dll"
            assert calls == ["origins"]
        if problem == "active": assert calls == ["origins", "activation"]


def test_structured_unknown_origin_is_a_failure_result_not_runtime_admission(setup, monkeypatch):
    selection, _ = setup
    def run(argv, **kwargs):
        request = json.loads(kwargs["input"])
        kwargs["stdout"].write(child.canonical(report(request, passed=False, stage="baseline",
            code="execution_loaded_origin_unsupported", unknown_module="ucrtbase.dll", blocked_origins=[
                {"kind": "native", "name": "ucrtbase.dll", "category": "system_exact",
                    "origin_id": "a" * 64, "reason": "unlisted_origin"}])))
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(probe.subprocess, "run", run)
    with probe.prepare_origin_probe(**selection) as prepared:
        result = prepared.run_probe()
        assert not result.passed and result.unknown_module == "ucrtbase.dll"
        with pytest.raises(ExecutionPreparationError, match="consumed"): prepared.run_probe()


@pytest.mark.parametrize("problem", ["extra", "empty", "duplicate", "bool", "nonce", "oversized", "format"])
def test_bounded_canonical_child_protocol(problem):
    value = {"probe_version": 1, "nonce": "a" * 64, "inventory": {}, "host_runtime": {}}
    if problem == "extra": value["argv"] = "arbitrary target"
    elif problem == "bool": value["probe_version"] = True
    elif problem == "nonce": value["nonce"] = "bad"
    raw = child.canonical(value)
    if problem == "empty": raw = b""
    elif problem == "duplicate": raw = raw.replace(b'"probe_version":1', b'"probe_version":1,"probe_version":1')
    elif problem == "oversized": raw = b"x" * (child.REQUEST_LIMIT + 1)
    elif problem == "format": raw += b"\n"
    with pytest.raises(child.ProbeFailure, match="protocol_invalid"): child.read_request(io.BytesIO(raw))


@pytest.mark.parametrize("problem", ["bytes", "timeout", "bool"])
def test_probe_bounds_reject_before_copy(setup, problem):
    selection, _ = setup
    selection.update({"bytes": {"max_bytes": probe.TREE_LIMIT + 1}, "timeout": {"timeout_seconds": 16},
        "bool": {"timeout_seconds": True}}[problem])
    with pytest.raises(ExecutionPreparationError, match="bounds_invalid"):
        probe.prepare_origin_probe(**selection)
    assert not list(selection["staging_parent"].iterdir())


def test_shared_public_error_identity_is_preserved():
    from app.providers.prepared import NativeExecutionUnsupported, ExecutionPreparationError
    assert NativeExecutionUnsupported is runtime_primitives.NativeExecutionUnsupported
    assert issubclass(NativeExecutionUnsupported, ExecutionPreparationError)


def test_child_version_origin_requires_exact_os_directory(setup):
    selection, facts = setup
    with probe.prepare_origin_probe(**selection) as prepared:
        root = prepared.invocation.cwd
        value = prepared.inventory.model_dump(mode="json")
        names = child.load_profile(root)
        system = facts["value"].system_directory
        child.check_origins(root, value, system, names, (system / "version.dll",))
        private = root / "Lib/unused/version.dll"
        private.parent.mkdir()
        private.write_bytes(b"private same name")
        # Membership cannot grant system-component authority, even for a
        # hypothetical inventoried same-name file.
        value["files"].append({"name": "Lib/unused/version.dll"})
        with pytest.raises(child.ProbeFailure, match="loaded_origin_unsupported"):
            child.check_origins(root, value, system, names, (private,))


def test_retained_stdlib_venv_executable_is_inventory_not_search_root(setup, monkeypatch):
    selection, _ = setup
    source = selection["base"] / "Lib/venv/scripts/nt/python.exe"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"retained stdlib venv bootstrap, not the selected interpreter")
    def run(*args, **kwargs):
        request = json.loads(kwargs["input"])
        assert (kwargs["cwd"] / "Lib/venv/scripts/nt/python.exe").read_bytes() == source.read_bytes()
        kwargs["stdout"].write(child.canonical(report(request)))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(probe.subprocess, "run", run)
    with probe.prepare_origin_probe(**selection) as prepared:
        assert "Lib/venv/scripts/nt/python.exe" in {entry.name for entry in prepared.inventory.files}
        root = prepared.invocation.cwd
        request = {"probe_version": 1, "nonce": "a" * 64,
            "inventory": prepared.inventory.model_dump(mode="json"),
            "host_runtime": prepared.host_observation.canonical()}
        module = ModuleType("__main__")
        module.__file__ = str(root / probe.ENTRY)
        result = child.observe(root, request, {"__main__": module},
            lambda system: Reader(root, system, []), lambda: setup[1]["value"])
        assert result["passed"]
        with pytest.raises(runtime_primitives.NativeExecutionUnsupported, match="basename_collision"):
            child.check_origins(root, request["inventory"], setup[1]["value"].system_directory,
                frozenset(), (root / "python.exe", root / "Lib/venv/scripts/nt/python.exe"))
        assert prepared.run_probe().passed


def test_inactive_helper_byte_change_still_consumes_before_launch(setup, monkeypatch):
    selection, _ = setup
    source = selection["base"] / "Lib/venv/scripts/nt/python.exe"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"retained helper")
    monkeypatch.setattr(probe.subprocess, "run", lambda *args, **kwargs: pytest.fail("no launch"))
    with probe.prepare_origin_probe(**selection) as prepared:
        (prepared.invocation.cwd / "Lib/venv/scripts/nt/python.exe").write_bytes(b"changed helper")
        with pytest.raises(ExecutionPreparationError):
            prepared.run_probe()


def test_main_emits_changed_tree_stop_before_any_owned_helper_import(setup, monkeypatch):
    selection, _ = setup
    with probe.prepare_origin_probe(**selection) as prepared:
        root = prepared.invocation.cwd
        request = {"probe_version": 1, "nonce": "a" * 64, "inventory": prepared.inventory.model_dump(mode="json"),
            "host_runtime": prepared.host_observation.canonical()}
        (root / "unlisted.py").write_bytes(b"added")
        output = io.BytesIO()
        monkeypatch.setattr(child, "guard_startup", lambda system: str(root))
        monkeypatch.setattr(child, "sys", SimpleNamespace(stdin=SimpleNamespace(buffer=io.BytesIO(child.canonical(request))),
            stdout=SimpleNamespace(buffer=output)))
        monkeypatch.setattr(child, "observe", lambda *args: pytest.fail("helper must not run"))
        with pytest.raises(SystemExit) as stopped: child.main()
        assert stopped.value.code == 1
        result = json.loads(output.getvalue())
        assert result["code"] == "execution_origin_probe_tree_changed" and result["stage"] == "tree"
