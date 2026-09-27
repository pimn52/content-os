"""Execute the single V62 replacement Talking child, preserving V61 evidence."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sys
from uuid import UUID

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api"))
from app.db import AssetRepository, AudioAssetRepository, Database, JobRepository, TalkingSliceSeriesRepository  # noqa: E402
from app.domain.models import JobStatus, JobType  # noqa: E402
from app.main import create_app  # noqa: E402
from app.media.ffprobe import FFProbeAdapter  # noqa: E402
from app.runtime import resolve_local_executable  # noqa: E402
from app.talking.slices import plan_talking_audio_slice  # noqa: E402
from app.talking_qa import apply_talking_qa, verify_talking_output  # noqa: E402
from app.worker_cli import build_runner, parse_config  # noqa: E402
from evaluate_v61_talking_series import DATA, DB_PATH, PROJECT_ID, MASTER_ID, configure, source_path  # noqa: E402

EVIDENCE = DATA / "evaluation-evidence" / "v62-talking-child"
MANIFEST = EVIDENCE / "recovery.json"
SERIES_ID = UUID("d4c236ae-2b89-42d3-89f0-9db8068689ec")
FAILED_ID = UUID("16d88b58-e26a-4f1c-a5cb-07b84eba4104")
REFERENCE = "v62:staging.json:source-and-audio-staging-valid"


def save(record: dict) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def prepare() -> None:
    if MANIFEST.exists():
        raise ValueError("V62 recovery already prepared")
    staging = json.loads((EVIDENCE / "staging.json").read_text(encoding="utf-8"))
    if staging.get("status") != "deterministic_input_staging_pass" or staging.get("failed_job_id") != str(FAILED_ID):
        raise ValueError("V62 exact failed-child input diagnostic is unavailable")
    with TestClient(create_app(DB_PATH)) as api:
        response = api.post(
            f"/projects/{PROJECT_ID}/talking-slice-series/{SERIES_ID}/recover-failed-child",
            json={"failed_job_id": str(FAILED_ID), "evidence_reference": REFERENCE},
        )
        if response.status_code != 201:
            raise ValueError(f"bounded normal recovery refused: {response.status_code} {response.text}")
        replacement = response.json()
    save({
        "package": "V62", "status": "prepared", "series_id": str(SERIES_ID),
        "failed_job_id": str(FAILED_ID), "replacement_job_id": replacement["id"],
        "recovery_reference": REFERENCE, "source_diagnostic": str(EVIDENCE / "staging.json"),
        "limit": "one same-payload replacement Job and one ProviderCall attempt; no further retry",
    })
    print(json.dumps({"status": "prepared", "replacement_job_id": replacement["id"]}), flush=True)


def run() -> int:
    record = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if record["status"] != "prepared":
        raise ValueError("V62 replacement is not prepared")
    configure()
    config = parse_config([
        "--db", str(DB_PATH), "--data-root", str(DATA),
        "--job-type", "generate_talking", "--max-attempts", "1",
        "--lease-seconds", "1500", "--heartbeat-seconds", "30",
    ])
    replacement_id = UUID(record["replacement_job_id"])
    with Database(DB_PATH) as db:
        series = TalkingSliceSeriesRepository(db).get(SERIES_ID)
        job = JobRepository(db).get(replacement_id)
        master = AudioAssetRepository(db).get(MASTER_ID)
        if series is None or job is None or master is None or series.child_job_ids[-1] != replacement_id:
            raise ValueError("exact recovery series, Job or Master unavailable")
        if job.status is not JobStatus.PENDING or len(series.recovery_history) != 1:
            raise ValueError("replacement is not the single pending recovery")
        record["status"] = "running"
        save(record)
        result = build_runner(config, db).run_once(allowed_types={JobType.GENERATE_TALKING})
        if result is None or result.id != replacement_id:
            raise ValueError("Worker did not execute the exact replacement child")
        record["job_status"] = result.status.value
        record["error_code"] = result.error_code
        record["error_message"] = result.error_message
        if result.status is not JobStatus.COMPLETED:
            record["status"] = "blocked_runtime"
            save(record)
            print(json.dumps({"status": record["status"], "error_code": result.error_code}), flush=True)
            return 2
        matches = [asset for asset in AssetRepository(db).list()
                   if asset.metadata.get("talking_generation", {}).get("job_id") == str(replacement_id)]
        if len(matches) != 1:
            record["status"] = "blocked_output_identity"
            save(record)
            return 2
        asset = matches[0]
        plan = plan_talking_audio_slice(
            master, start_segment_index=job.payload.slice_start_segment_index,
            end_segment_index=job.payload.slice_end_segment_index,
            max_duration_ms=job.payload.slice_max_duration_ms,
        )
        report = verify_talking_output(
            asset, master.model_copy(update={"duration_ms": plan.duration_ms}),
            FFProbeAdapter(resolve_local_executable("ffprobe")),
            evidence_reference=f"v62:series:{SERIES_ID}:replacement:real-media-probe",
        )
        with db.transaction():
            asset = AssetRepository(db).update(apply_talking_qa(asset, report))
        path = source_path(asset.source_file)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != asset.content_hash:
            raise ValueError("replacement Asset hash differs from real file")
        review = DATA / "evaluation-evidence" / "v61-talking-series" / "review" / "10.mp4"
        if review.exists():
            raise ValueError("review file 10 already exists")
        shutil.copyfile(path, review)
        if hashlib.sha256(review.read_bytes()).hexdigest() != digest:
            raise ValueError("replacement review copy hash differs")
        generation = asset.metadata["talking_generation"]
        record.update({
            "status": "qa_verified" if generation["qa_state"] == "verified" else "blocked_qa",
            "asset_id": str(asset.id), "sha256": digest, "review_file": str(review),
            "duration_ms": asset.duration_ms, "duration_drift_ms": generation.get("qa", {}).get("duration_drift_ms"),
        })
        save(record)
        print(json.dumps({"status": record["status"], "asset_id": str(asset.id), "drift_ms": record["duration_drift_ms"]}), flush=True)
        return 0 if record["status"] == "qa_verified" else 2


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "prepare":
        prepare()
    elif command == "run":
        sys.exit(run())
    else:
        raise SystemExit("Usage: evaluate_v62_talking_recovery.py prepare|run")
