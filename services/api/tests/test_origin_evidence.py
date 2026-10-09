"""Complete bounded diagnostic evidence, not expanded admission authority."""
from dataclasses import replace
import hashlib
import json
from types import ModuleType, SimpleNamespace

import pytest
from pydantic import ValidationError

from app.providers import python_origin_child as child, python_origin_probe as probe
from app.providers.runtime_primitives import ExecutionPreparationError
from test_python_origin_probe import setup, report, Reader
from test_python_startup import layout


def row(name="bcrypt.dll", **changes):
    value = dict(kind="native", name=name, category="system_exact", origin_id="a" * 64,
        reason="unlisted_origin")
    value.update(changes)
    return value


def request(prepared):
    return dict(probe_version=2, nonce="a" * 64, inventory=prepared.inventory.model_dump(mode="json"),
        host_runtime=prepared.host_observation.canonical())


@pytest.mark.parametrize("problem", [None, "host", "tree"])
def test_complete_both_snapshots_stop_before_search_and_recheck_evidence(setup, problem):
    selection, facts = setup
    source = selection["base"] / "Lib/data/version.dll"
    source.parent.mkdir()
    source.write_bytes(b"retained private OS shadow")
    system = facts["value"].system_directory
    for name in ("unknown-bootstrap.dll", "unknown-crt.dll"):
        (system / name).write_bytes(b"unknown OS-shaped file, no trust")
    outside = selection["work_directory"] / "external.py"
    outside.write_bytes(b"outside source")
    with probe.prepare_origin_probe(**selection) as prepared:
        root = prepared.invocation.cwd
        module = ModuleType("foreign")
        module.__file__ = str(outside)
        calls = []
        class Observed(Reader):
            def native_origins(self):
                result = super().native_origins()
                if problem == "tree": (root / "extra.py").write_bytes(b"changed after scan")
                return result + (system / "unknown-bootstrap.dll", system / "unknown-crt.dll", root / "Lib/data/version.dll")
        def facts_reader():
            if problem == "host" and calls: return replace(facts["value"], revision=2)
            return facts["value"]
        value = child.observe(root, request(prepared), {"foreign": module},
            lambda directory: Observed(root, directory, calls), facts_reader)
        assert not value["passed"] and value["stage"] == "baseline"
        assert calls == ["origins"]  # no activation, search, load or retry
        if problem:
            assert not value["blocked_origins"]
            assert value["code"] == ("execution_host_runtime_changed" if problem == "host"
                else "execution_origin_probe_tree_changed")
        else:
            rows = value["blocked_origins"]
            assert len(rows) == 4
            assert {(r["name"], r["category"]) for r in rows} == {
                ("unknown-bootstrap.dll", "system_exact"), ("unknown-crt.dll", "system_exact"),
                ("version.dll", "private_inventory"), ("external.py", "outside")}
            assert next(r for r in rows if r["name"] == "version.dll")["reason"] == "os_named_wrong_origin"
            assert all(str(root) not in json.dumps(r) and str(system) not in json.dumps(r) for r in rows)
            assert len(probe.OriginProbeReport.model_validate_json(child.canonical(value)).blocked_origins) == 4


def test_collection_deterministic_and_inactive_unlisted_class_is_not_trust(setup):
    selection, facts = setup
    with probe.prepare_origin_probe(**selection) as prepared:
        root = prepared.invocation.cwd
        unknown = root / "unlisted.dll"
        unknown.write_bytes(b"unlisted")
        system = facts["value"].system_directory
        other = system / "other.dll"
        other.write_bytes(b"other")
        args = (root, request(prepared)["inventory"], system, child.load_profile(root))
        a = child.blocked_origins(*args, (("native", (unknown, other)),))
        b = child.blocked_origins(*args, (("native", (other, unknown)),))
        assert a == b
        assert next(r for r in a if r["name"] == "unlisted.dll")["category"] == "private_unlisted"
        assert a[0]["origin_id"] == hashlib.sha256(str(other).encode()).hexdigest()


def test_owned_finite_os_origins_pass_diagnostic_baseline_with_ordered_checks(setup):
    selection, facts = setup
    with probe.prepare_origin_probe(**selection) as prepared:
        root, calls = prepared.invocation.cwd, []
        module = ModuleType("__main__")
        module.__file__ = str(root / probe.ENTRY)
        class AllOwned(Reader):
            def native_origins(self):
                observed = super().native_origins()
                return observed + tuple(self.system / name for name in sorted(child.load_profile(root))
                    if self.system / name not in observed)
        value = child.observe(root, request(prepared), {"__main__": module},
            lambda system: AllOwned(root, system, calls), lambda: facts["value"])
        parsed = probe.OriginProbeReport.model_validate_json(child.canonical(value))
        assert parsed.passed and not parsed.blocked_origins
        assert calls == ["origins", "activation", "search", "origins"]


def test_evidence_overflow_never_returns_truncated_list(setup):
    selection, facts = setup
    paths = []
    for index in range(257):
        path = selection["work_directory"] / f"unknown{index}.dll"
        path.write_bytes(b"outside")
        paths.append(path)
    with probe.prepare_origin_probe(**selection) as prepared:
        with pytest.raises(child.ProbeFailure, match="evidence_overflow"):
            child.blocked_origins(prepared.invocation.cwd, request(prepared)["inventory"],
                facts["value"].system_directory, child.load_profile(prepared.invocation.cwd),
                (("native", tuple(paths)),))


@pytest.mark.parametrize("problem", [None, "unknown", "active"])
def test_v2_empty_or_blocked_baseline_uses_existing_ordered_gates(setup, problem):
    selection, facts = setup
    with probe.prepare_origin_probe(**selection) as prepared:
        root, calls = prepared.invocation.cwd, []
        module = ModuleType("__main__")
        module.__file__ = str(root / probe.ENTRY)
        value = child.observe(root, request(prepared), {"__main__": module},
            lambda system: Reader(root, system, calls, problem), lambda: facts["value"])
        parsed = probe.OriginProbeReport.model_validate_json(child.canonical(value))
        assert parsed.passed == (problem is None)
        if problem is None:
            assert not parsed.blocked_origins and calls == ["origins", "activation", "search", "origins"]
        elif problem == "unknown":
            assert len(parsed.blocked_origins) == 1 and calls == ["origins"]
        else:
            assert not parsed.blocked_origins and calls == ["origins", "activation"]


@pytest.mark.parametrize("problem", ["passed", "missing", "duplicate", "order", "counts", "stage",
    "code", "name", "category", "identity", "kind", "overflow", "byte_limit", "extra"])
def test_report_rejects_incoherent_or_unbounded_evidence(problem):
    value = dict(probe_version=2, nonce="a" * 64, passed=False, stage="baseline", inventory_sha256="b" * 64,
        host_runtime=None, native_count=2048, python_count=1, code="execution_loaded_origin_unsupported",
        unknown_module="bcrypt.dll", blocked_origins=[row()])
    if problem == "passed": value["passed"] = True
    elif problem == "missing": value["blocked_origins"] = []
    elif problem == "duplicate": value["blocked_origins"] *= 2
    elif problem == "order": value["blocked_origins"] = [row("z.dll"), row()]
    elif problem == "counts": value["native_count"] = 0
    elif problem == "stage": value["stage"] = "complete"
    elif problem == "code": value["code"] = "execution_other_stop"
    elif problem in ("name", "category", "identity", "kind", "extra"):
        field, data = {"name": ("name", "C:/secret.dll"), "category": ("category", "trusted"),
            "identity": ("origin_id", "bad"), "kind": ("kind", "provider"), "extra": ("path", "secret")}[problem]
        value["blocked_origins"][0][field] = data
    elif problem == "overflow": value["blocked_origins"] = [row(f"x{i:04}.dll") for i in range(257)]
    else:
        value["blocked_origins"] = [row(f"x{i:04}" + "a" * 240 + ".dll") for i in range(160)]
        value["unknown_module"] = value["blocked_origins"][0]["name"]
        assert len(child.canonical(value["blocked_origins"])) > 48 * 1024
    with pytest.raises(ValidationError): probe.OriginProbeReport.model_validate_json(json.dumps(value))


@pytest.mark.parametrize("problem", ["downgrade", "host", "digest"])
def test_parent_rejects_downgrade_or_stale_blocked_evidence(setup, monkeypatch, problem):
    selection, _ = setup
    def run(*args, **kwargs):
        current = json.loads(kwargs["input"])
        assert current["probe_version"] == 2
        value = report(current, passed=False, stage="baseline", code="execution_loaded_origin_unsupported",
            unknown_module="bcrypt.dll", blocked_origins=[row()])
        if problem == "downgrade":
            value["probe_version"] = 1
            value.pop("blocked_origins")
            assert probe.OriginProbeReportV1.model_validate_json(json.dumps(value)).probe_version == 1
        elif problem == "host": value["host_runtime"]["revision"] += 1
        else: value["inventory_sha256"] = "0" * 64
        kwargs["stdout"].write(child.canonical(value))
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(probe.subprocess, "run", run)
    with probe.prepare_origin_probe(**selection) as prepared:
        with pytest.raises(ExecutionPreparationError, match="protocol_invalid"): prepared.run_probe()
        with pytest.raises(ExecutionPreparationError, match="consumed"): prepared.run_probe()
