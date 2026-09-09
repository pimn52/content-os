"""Tests for the persistent local-fixture report entry point."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parents[3]


def _load_usage_module():
    spec = importlib.util.spec_from_file_location("record_local_fixture_usage", ROOT / "scripts" / "record_local_fixture_usage.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_usage_ledger_is_explicitly_unmetered_and_uses_requested_run_id() -> None:
    ledger = _load_usage_module().build_ledger("repeatable-fixture")
    run = ledger["runs"][0]
    assert ledger["usage_status"] == "unmetered"
    assert run["run_id"] == "repeatable-fixture"
    assert run["codex_model_label"] == "gpt-5.6-luna"
    assert run["input_tokens"] is None
    assert run["output_tokens"] is None
    assert run["actual_cost_usd"] is None


def test_runner_requires_explicit_ffmpeg_and_ffprobe_without_creating_artifacts(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment.pop("CONTENT_OS_FFMPEG", None)
    environment.pop("CONTENT_OS_FFPROBE", None)
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_local_fixture_report.py"), "--runs-root", str(tmp_path), "--run-id", "missing-tools"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 2
    assert "CONTENT_OS_FFMPEG and CONTENT_OS_FFPROBE must both be set" in completed.stderr
    assert list(tmp_path.iterdir()) == []


def test_runner_removes_staging_and_records_a_failure_report(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment["CONTENT_OS_FFMPEG"] = "missing-fixture-ffmpeg"
    environment["CONTENT_OS_FFPROBE"] = "missing-fixture-ffprobe"
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_local_fixture_report.py"), "--runs-root", str(tmp_path), "--run-id", "tool-failure"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert completed.returncode != 0
    assert not (tmp_path / ".tool-failure.staging").exists()
    assert not (tmp_path / "tool-failure").exists()
    report = json.loads((tmp_path / "tool-failure.failed.json").read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["message"] == "Fixture staging directory was removed; no partial report was retained."
