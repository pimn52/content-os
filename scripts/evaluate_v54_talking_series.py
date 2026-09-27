"""Run only the remaining V54 Talking children with per-child real-media QA."""
from __future__ import annotations

import json
from pathlib import Path
import sys
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api"))

from app.db import AssetRepository, AudioAssetRepository, Database, JobRepository  # noqa: E402
from app.domain.models import JobStatus, JobType  # noqa: E402
from app.media.ffprobe import FFProbeAdapter  # noqa: E402
from app.runtime import resolve_local_executable  # noqa: E402
from app.talking.slices import plan_talking_audio_slice  # noqa: E402
from app.talking_qa import apply_talking_qa, verify_talking_output  # noqa: E402
from app.worker_cli import build_runner, parse_config  # noqa: E402

DATA_ROOT = ROOT / "content-os-data"
DB_PATH = DATA_ROOT / "content-os.sqlite3"
SERIES_ID = UUID("9cc362bc-7cfc-44de-bf8b-b05041eacbbe")
MASTER_ID = UUID("a24fa94e-6147-47f0-a015-92951ef5f10d")
JOB_IDS = tuple(UUID(value) for value in (
    "6d42cb44-812c-4b30-80cb-6cb39beb3202",
    "cc3e09f7-6c96-4116-a693-6a177f7e3b78",
    "bd8d5468-3161-4af4-9db2-359239bb82f9",
    "cc654c09-4a4d-43a7-8097-00a52c1dc19a",
    "33fefa78-eff7-499f-bcbc-7645495f3f76",
    "86c130f5-a4f8-43fa-9416-3e742be3071a",
    "5f34268c-1b34-4092-8fb4-25ee072d0b58",
    "bbd72428-ba50-46bd-bd85-2894a6c59217",
    "00581de8-7e37-4dd6-a9a0-d9c62458a661",
))


def main() -> int:
    config = parse_config([
        "--db", str(DB_PATH), "--data-root", str(DATA_ROOT),
        "--job-type", "generate_talking", "--max-attempts", "1",
        "--lease-seconds", "1500", "--heartbeat-seconds", "30",
    ])
    with Database(DB_PATH) as db:
        jobs = JobRepository(db)
        assets = AssetRepository(db)
        master = AudioAssetRepository(db).get(MASTER_ID)
        if master is None or master.metadata.get("voice_generation", {}).get("human_review_state") != "approved":
            raise ValueError("V54 requires the exact U-Voice-approved Master")
        runner = build_runner(config, db)
        probe = FFProbeAdapter(resolve_local_executable("ffprobe"))
        for index, job_id in enumerate(JOB_IDS):
            job = jobs.get(job_id)
            if job is None or job.payload.slice_series_id != SERIES_ID or job.payload.slice_series_index != index:
                raise ValueError(f"V54 child {index} identity changed")
            if job.status is JobStatus.PENDING:
                if index and jobs.get(JOB_IDS[index - 1]).status is not JobStatus.COMPLETED:
                    raise ValueError("V54 preceding child is not complete")
                result = runner.run_once(allowed_types={JobType.GENERATE_TALKING})
                if result is None or result.id != job_id:
                    raise ValueError(f"Worker did not execute V54 child {index}")
                job = result
            if job.status is not JobStatus.COMPLETED:
                print(json.dumps({"index": index, "job_id": str(job_id), "status": job.status.value,
                                  "error_code": job.error_code}), flush=True)
                return 2
            matching = [asset for asset in assets.list()
                        if asset.metadata.get("talking_generation", {}).get("job_id") == str(job_id)]
            if len(matching) != 1:
                raise ValueError(f"V54 child {index} does not have exactly one output")
            asset = matching[0]
            qa_state = asset.metadata["talking_generation"]["qa_state"]
            if qa_state == "pending":
                plan = plan_talking_audio_slice(
                    master,
                    start_segment_index=job.payload.slice_start_segment_index,
                    end_segment_index=job.payload.slice_end_segment_index,
                    max_duration_ms=job.payload.slice_max_duration_ms,
                )
                narration = master.model_copy(update={"duration_ms": plan.duration_ms})
                report = verify_talking_output(
                    asset, narration, probe,
                    evidence_reference=f"v54:series:{SERIES_ID}:child:{index}:real-media-probe",
                )
                with db.transaction():
                    assets.update(apply_talking_qa(asset, report))
                qa_state = "verified" if report.automated_verified else "failed"
                drift = report.duration_drift_ms
            else:
                drift = asset.metadata["talking_generation"].get("qa", {}).get("duration_drift_ms")
            print(json.dumps({"index": index, "job_id": str(job_id), "asset_id": str(asset.id),
                              "status": job.status.value, "qa_state": qa_state,
                              "duration_drift_ms": drift}), flush=True)
            if qa_state != "verified":
                return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
