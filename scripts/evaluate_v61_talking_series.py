"""One normal, bounded V61 Talking series on the U-Voice-approved V60 Master."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from uuid import UUID

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api"))
from app.db import AssetRepository, AudioAssetRepository, ClipRepository, Database, JobRepository  # noqa: E402
from app.domain.models import JobStatus, JobType  # noqa: E402
from app.main import create_app  # noqa: E402
from app.media.ffprobe import FFProbeAdapter  # noqa: E402
from app.runtime import resolve_local_executable  # noqa: E402
from app.talking.slices import plan_talking_audio_slice  # noqa: E402
from app.talking_qa import apply_talking_qa, verify_talking_output  # noqa: E402
from app.worker_cli import build_runner, parse_config  # noqa: E402

DATA = ROOT / "content-os-data"
DB_PATH = DATA / "content-os.sqlite3"
PROJECT_ID = UUID("d45f42fb-06eb-4ec5-bded-66116ea6a872")
MASTER_ID = UUID("c1346d02-d967-4475-b5f4-4bdf7933b2d3")
MASTER_SHA256 = "756492b4d61555654884276cde33f27f1cb57f1517181d712eb5b1977f21c480"
PROFILE_ID = UUID("c47d4a97-2ff0-457e-bc41-8fcd744365e4")
SOURCE_CLIP_ID = UUID("e3547414-ae03-5f1d-a2be-1b959fc75a58")
AUTHORIZATION = "u1-20260913-biyingjie-tim-internal"
MACHINE_ID = "asus-rtx3060-laptop-6gb"
EVIDENCE = DATA / "evaluation-evidence" / "v61-talking-series"
MANIFEST = EVIDENCE / "manifest.json"


def save(value: dict) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def source_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return DATA.parent / path if path.parts and path.parts[0].casefold() == DATA.name.casefold() else DATA / path


def prepare() -> None:
    if MANIFEST.exists():
        raise ValueError("V61 series is already prepared")
    with Database(DB_PATH) as db:
        master = AudioAssetRepository(db).get(MASTER_ID)
        clip = ClipRepository(db).get(SOURCE_CLIP_ID)
        if master is None or clip is None or master.content_hash != MASTER_SHA256:
            raise ValueError("exact V60 Master or authorized source Clip changed")
        generation = master.metadata.get("voice_generation") or {}
        if generation.get("qa_state") != "verified" or generation.get("human_review_state") != "approved":
            raise ValueError("exact Master lacks automated QA or user Voice approval")
        if hashlib.sha256(source_path(master.source_file).read_bytes()).hexdigest() != MASTER_SHA256:
            raise ValueError("exact Master bytes changed")
    payload = {
        "idempotency_key": "v61-new-topic-v60-master-source-forward-once",
        "talking_profile_id": str(PROFILE_ID), "reference_clip_id": str(SOURCE_CLIP_ID),
        "narration_audio_id": str(MASTER_ID), "authorization_reference": AUTHORIZATION,
        "execution_machine_id": MACHINE_ID, "terminal_face_closeout": False,
    }
    with TestClient(create_app(DB_PATH)) as api:
        response = api.post(f"/projects/{PROJECT_ID}/talking-slice-series-jobs", json=payload)
        if response.status_code != 201:
            raise ValueError(f"normal Talking series admission failed: {response.status_code} {response.text}")
        series = response.json()
    jobs = series["jobs"]
    if not jobs or len(jobs) > 20:
        raise ValueError("Talking series returned no children or an unexpected unbounded count")
    save({
        "package": "V61", "status": "generating", "series_id": series["id"],
        "project_id": str(PROJECT_ID), "master_audio_id": str(MASTER_ID),
        "master_sha256": MASTER_SHA256, "source_clip_id": str(SOURCE_CLIP_ID),
        "reference_windows": [], "child_job_ids": [job["id"] for job in jobs],
        "children": [{"index": index, "job_id": job["id"], "status": "queued"}
                     for index, job in enumerate(jobs)],
        "limits": "one execution per declared child; no automatic retry, source/provider/setting change or human verdict",
    })
    print(json.dumps({"status": "generating", "series_id": series["id"], "children": len(jobs)}))


def configure() -> None:
    runtime = DATA / "latentsync-runtime310"
    repo = DATA / "latentsync"
    checkpoint = DATA / "latentsync-checkpoints" / "latentsync_unet.pt"
    runner = DATA / "latentsync-evaluation-20260915" / "run_latentsync_math.py"
    ffmpeg = runtime / "Library" / "bin" / "ffmpeg.exe"
    for path in (runtime / "python.exe", repo, checkpoint, runner, ffmpeg):
        if not path.exists():
            raise ValueError(f"verified Talking runtime component unavailable: {path}")
    os.environ.update({
        "CONTENT_OS_DATA_ROOT": str(DATA),
        "CONTENT_OS_TALKING_PROVIDER": "latentsync",
        "CONTENT_OS_LATENTSYNC_PYTHON": str(runtime / "python.exe"),
        "CONTENT_OS_LATENTSYNC_REPO": str(repo),
        "CONTENT_OS_LATENTSYNC_CHECKPOINT": str(checkpoint),
        "CONTENT_OS_LATENTSYNC_RUNNER": str(runner),
        "CONTENT_OS_LATENTSYNC_UNET_CONFIG": str(repo / "configs" / "unet" / "stage2.yaml"),
        "CONTENT_OS_LATENTSYNC_FFMPEG": str(ffmpeg),
        "CONTENT_OS_LATENTSYNC_STEPS": "20",
        "CONTENT_OS_LATENTSYNC_GUIDANCE_SCALE": "1.5",
        "CONTENT_OS_LATENTSYNC_SEED": "1247",
        "CONTENT_OS_LATENTSYNC_TIMEOUT_SECONDS": "1200",
    })


def run() -> int:
    record = load()
    if record["status"] != "generating":
        raise ValueError("V61 series is not in the declared generating state")
    configure()
    config = parse_config([
        "--db", str(DB_PATH), "--data-root", str(DATA),
        "--job-type", "generate_talking", "--max-attempts", "1",
        "--lease-seconds", "1500", "--heartbeat-seconds", "30",
    ])
    with Database(DB_PATH) as db:
        jobs = JobRepository(db)
        assets = AssetRepository(db)
        master = AudioAssetRepository(db).get(MASTER_ID)
        if master is None or master.metadata.get("voice_generation", {}).get("human_review_state") != "approved":
            raise ValueError("V61 exact Master approval unavailable")
        runner = build_runner(config, db)
        probe = FFProbeAdapter(resolve_local_executable("ffprobe"))
        for index, row in enumerate(record["children"]):
            job_id = UUID(row["job_id"])
            job = jobs.get(job_id)
            if job is None or job.payload.slice_series_id != UUID(record["series_id"]) or job.payload.slice_series_index != index:
                raise ValueError(f"V61 child {index} identity changed")
            if job.status is JobStatus.PENDING:
                if index and jobs.get(UUID(record["children"][index - 1]["job_id"])).status is not JobStatus.COMPLETED:
                    raise ValueError("preceding Talking child is not completed")
                result = runner.run_once(allowed_types={JobType.GENERATE_TALKING})
                if result is None or result.id != job_id:
                    raise ValueError(f"Worker did not execute expected child {index}")
                job = result
            if job.status is not JobStatus.COMPLETED:
                row.update(status="blocked_runtime", error_code=job.error_code)
                record["status"] = "blocked_runtime"
                save(record)
                print(json.dumps({"index": index, "status": row["status"], "error_code": job.error_code}), flush=True)
                return 2
            matching = [asset for asset in assets.list()
                        if asset.metadata.get("talking_generation", {}).get("job_id") == str(job_id)]
            if len(matching) != 1:
                raise ValueError(f"V61 child {index} has no unique output")
            asset = matching[0]
            generation = asset.metadata["talking_generation"]
            if generation["qa_state"] == "pending":
                plan = plan_talking_audio_slice(
                    master, start_segment_index=job.payload.slice_start_segment_index,
                    end_segment_index=job.payload.slice_end_segment_index,
                    max_duration_ms=job.payload.slice_max_duration_ms,
                )
                narration = master.model_copy(update={"duration_ms": plan.duration_ms})
                report = verify_talking_output(
                    asset, narration, probe,
                    evidence_reference=f"v61:series:{record['series_id']}:child:{index}:real-media-probe",
                )
                with db.transaction():
                    asset = assets.update(apply_talking_qa(asset, report))
                generation = asset.metadata["talking_generation"]
            path = source_path(asset.source_file)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != asset.content_hash:
                raise ValueError(f"V61 child {index} media bytes changed")
            review = EVIDENCE / "review" / f"{index + 1:02d}.mp4"
            review.parent.mkdir(parents=True, exist_ok=True)
            if not review.exists():
                shutil.copyfile(path, review)
            if hashlib.sha256(review.read_bytes()).hexdigest() != digest:
                raise ValueError(f"V61 review copy {index} differs from generated Asset")
            row.update(status="qa_verified" if generation["qa_state"] == "verified" else "blocked_qa",
                       asset_id=str(asset.id), sha256=digest, review_file=str(review),
                       duration_ms=asset.duration_ms,
                       duration_drift_ms=generation.get("qa", {}).get("duration_drift_ms"))
            save(record)
            print(json.dumps({"index": index, "status": row["status"], "asset_id": row["asset_id"],
                              "duration_ms": row["duration_ms"], "drift_ms": row["duration_drift_ms"]}), flush=True)
            if row["status"] != "qa_verified":
                record["status"] = "blocked_qa"
                save(record)
                return 3
    record["status"] = "awaiting_u_talking"
    save(record)
    print(json.dumps({"status": record["status"], "series_id": record["series_id"],
                      "children": len(record["children"])}), flush=True)
    return 0


if __name__ == "__main__":
    action = sys.argv[1]
    if action == "prepare":
        prepare()
    elif action == "run":
        raise SystemExit(run())
    else:
        raise SystemExit("prepare|run")
