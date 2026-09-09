"""Run the local media fixture and atomically persist a reproducible report.

No provider is called.  FFmpeg and ffprobe must be explicitly injected through
CONTENT_OS_FFMPEG and CONTENT_OS_FFPROBE so the report records its toolchain.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEST_PATH = ROOT / "services" / "api" / "tests" / "test_local_codex_fixture_e2e.py"
DEFAULT_RUNS_ROOT = ROOT / "content-os-data" / "test-runs"


def _default_run_id() -> str:
    return "local-codex-fixture-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _failure_report(path: Path, run_id: str, returncode: int, command: list[str]) -> None:
    path.write_text(json.dumps({
        "schema_version": 1,
        "run_id": run_id,
        "status": "failed",
        "returncode": returncode,
        "command": command,
        "failed_at": datetime.now(timezone.utc).isoformat(),
        "message": "Fixture staging directory was removed; no partial report was retained.",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Persist the local Codex fixture E2E report.")
    parser.add_argument("--run-id", default=_default_run_id(), help="unique report directory name")
    parser.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT)
    args = parser.parse_args()
    if not args.run_id or Path(args.run_id).name != args.run_id or args.run_id in {".", ".."}:
        parser.error("--run-id must be a simple directory name")
    if not os.environ.get("CONTENT_OS_FFMPEG") or not os.environ.get("CONTENT_OS_FFPROBE"):
        parser.error("CONTENT_OS_FFMPEG and CONTENT_OS_FFPROBE must both be set")

    runs_root = args.runs_root.resolve()
    final_dir = runs_root / args.run_id
    staging_dir = runs_root / f".{args.run_id}.staging"
    failure_path = runs_root / f"{args.run_id}.failed.json"
    if final_dir.exists() or staging_dir.exists() or failure_path.exists():
        parser.error(f"run id already exists or is reserved: {args.run_id}")
    runs_root.mkdir(parents=True, exist_ok=True)
    staging_dir.mkdir()
    command = [sys.executable, "-m", "pytest", str(TEST_PATH)]
    environment = os.environ.copy()
    environment["CONTENT_OS_LOCAL_FIXTURE_ARTIFACT_DIR"] = str(staging_dir)
    completed = subprocess.run(command, cwd=ROOT, env=environment)
    if completed.returncode:
        shutil.rmtree(staging_dir)
        _failure_report(failure_path, args.run_id, completed.returncode, command)
        return completed.returncode

    ledger_command = [sys.executable, str(ROOT / "scripts" / "record_local_fixture_usage.py"), "--run-id", args.run_id, "--ledger", str(staging_dir / "usage-ledger.json")]
    ledger = subprocess.run(ledger_command, cwd=ROOT)
    if ledger.returncode:
        shutil.rmtree(staging_dir)
        _failure_report(failure_path, args.run_id, ledger.returncode, ledger_command)
        return ledger.returncode
    staging_dir.rename(final_dir)
    print(final_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
