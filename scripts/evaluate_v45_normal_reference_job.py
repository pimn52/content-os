"""One normal selected-reference Voice Job and independent QA on new copy.

Each phase is explicit and idempotent. There is one generation Job, no tuning
loop and no automatic human admission.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api"))
import evaluate_v40_phrase_continuity as base  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app.db import AudioAssetRepository, Database, JobRepository  # noqa: E402
from app.domain.models import JobType  # noqa: E402
from app.main import create_app  # noqa: E402
from app.worker_cli import build_runner, parse_config  # noqa: E402

DATA_ROOT = base.DATA_ROOT
DB_PATH = DATA_ROOT / "content-os.sqlite3"
EVIDENCE = DATA_ROOT / "evaluation-evidence" / "v45-normal-reference-job"
MANIFEST = EVIDENCE / "v45-normal-reference-job.json"
COPY = "短视频开头先回答问题，再解释原因。别让观众等你想完，清楚比停顿更重要。"
WINDOW_CLIP = "e3547414-ae03-5f1d-a2be-1b959fc75a58"
WINDOW_START = 28_240
WINDOW_END = 34_240


def load() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def save(record: dict) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def prepare() -> None:
    if MANIFEST.exists():
        raise ValueError("V45 already prepared; no second generation Job")
    os.environ["CONTENT_OS_DATA_ROOT"] = str(DATA_ROOT)
    with TestClient(create_app(DB_PATH)) as client:
        response = client.get(f"/voice-profiles/{base.PROFILE_ID}/reference-windows")
        if response.status_code != 200:
            raise ValueError(f"normal reference listing failed: {response.status_code} {response.text}")
        matching = [row for row in response.json() if row["selection"]["clip_id"] == WINDOW_CLIP
                    and row["selection"]["start_ms"] == WINDOW_START
                    and row["selection"]["end_ms"] == WINDOW_END]
        if len(matching) != 1:
            raise ValueError("V43-reviewed authorized reference is not one current normal choice")
        selection = matching[0]["selection"]
        payload = {
            "idempotency_key": "v45-normal-selected-reference-new-copy-one",
            "voice_profile_id": str(base.PROFILE_ID), "text": COPY,
            "authorization_reference": "v45:authorized-creator-voice-evaluation",
            "language": "zh", "reference_window": selection,
        }
        created = client.post(f"/projects/{base.PROJECT_ID}/voice-jobs", json=payload)
        if created.status_code != 201:
            raise ValueError(f"normal Voice Job creation failed: {created.status_code} {created.text}")
        record = {"package": "V45", "status": "voice_job_queued", "copy": COPY,
                  "project_id": str(base.PROJECT_ID), "profile_id": str(base.PROFILE_ID),
                  "selection": selection, "voice_job_id": created.json()["id"],
                  "limitations": "One non-commercial OmniVoice evaluation; independent QA and U-Voice remain separate."}
        save(record)
        print(json.dumps({"status": record["status"], "voice_job_id": record["voice_job_id"]}))


def configure_voice() -> None:
    os.environ["CONTENT_OS_DATA_ROOT"] = str(DATA_ROOT)
    os.environ["CONTENT_OS_VOICE_PROVIDER"] = "omnivoice"
    os.environ["CONTENT_OS_OMNIVOICE_PYTHON"] = str(base.RUNTIME)
    os.environ["CONTENT_OS_OMNIVOICE_MODEL"] = str(base.MODEL)
    os.environ["CONTENT_OS_OMNIVOICE_NUM_STEP"] = "32"
    os.environ["CONTENT_OS_OMNIVOICE_SPEED"] = "1.0"
    os.environ["CONTENT_OS_OMNIVOICE_DEVICE"] = "cuda"
    os.environ["CONTENT_OS_OMNIVOICE_TIMEOUT_SECONDS"] = "900"


def run_voice() -> None:
    record = load()
    if record["status"] != "voice_job_queued":
        raise ValueError("V45 Voice Job is not queued for its one execution")
    configure_voice()
    with Database(DB_PATH) as db:
        config = parse_config(["--once", "--db", str(DB_PATH), "--job-type", JobType.GENERATE_VOICE.value])
        outcome = build_runner(config, db).run_once()
        job = JobRepository(db).get(UUID(record["voice_job_id"]))
        if outcome is None or job is None:
            raise ValueError("normal Voice worker did not execute the queued V45 Job")
        record["voice_job_status"] = job.status.value
        record["voice_job_error_code"] = job.error_code
        audios = [audio for audio in AudioAssetRepository(db).list()
                  if (audio.metadata.get("voice_generation") or {}).get("job_id") == record["voice_job_id"]]
        if job.status.value != "completed" or len(audios) != 1:
            record["status"] = "blocked_voice_execution"
            save(record)
            raise ValueError(f"V45 normal Voice execution did not complete: {job.error_code}")
        audio = audios[0]
        applied = audio.metadata["voice_generation"].get("reference_window")
        if applied != record["selection"]:
            record["status"] = "blocked_reference_provenance"
            save(record)
            raise ValueError("normal generated AudioAsset lacks exact selected reference provenance")
        source = DATA_ROOT / audio.source_file if not Path(audio.source_file).is_absolute() else Path(audio.source_file)
        if not source.is_file():
            record["status"] = "blocked_missing_audio"
            save(record)
            raise ValueError("generated AudioAsset file is unavailable")
        record.update({"status": "voice_generated_qa_pending", "audio_asset_id": str(audio.id),
                       "audio_file": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                       "duration_ms": audio.duration_ms})
        save(record)
        print(json.dumps({"status": record["status"], "audio_asset_id": record["audio_asset_id"]}))


def queue_qa() -> None:
    record = load()
    if record["status"] != "voice_generated_qa_pending":
        raise ValueError("V45 QA cannot be queued in the current state")
    os.environ["CONTENT_OS_DATA_ROOT"] = str(DATA_ROOT)
    with TestClient(create_app(DB_PATH)) as client:
        created = client.post(f"/projects/{base.PROJECT_ID}/voice-qa-jobs", json={
            "idempotency_key": "v45-normal-selected-reference-fresh-qa-one",
            "narration_audio_id": record["audio_asset_id"],
            "target_text": record["copy"],
        })
        if created.status_code != 201:
            raise ValueError(f"normal Voice QA Job creation failed: {created.status_code} {created.text}")
        record.update({"status": "qa_job_queued", "qa_job_id": created.json()["id"]})
        save(record)
        print(json.dumps({"status": record["status"], "qa_job_id": record["qa_job_id"]}))


def run_qa() -> None:
    record = load()
    if record["status"] != "qa_job_queued":
        raise ValueError("V45 QA Job is not queued for its one execution")
    os.environ["CONTENT_OS_DATA_ROOT"] = str(DATA_ROOT)
    os.environ["CONTENT_OS_VOICE_QA_ASR_MODEL"] = str(DATA_ROOT / "models" / "faster-whisper-medium")
    os.environ["CONTENT_OS_VOICE_QA_ASR_DEVICE"] = "cpu"
    os.environ["CONTENT_OS_VOICE_QA_ASR_COMPUTE_TYPE"] = "int8"
    with Database(DB_PATH) as db:
        config = parse_config(["--once", "--db", str(DB_PATH), "--job-type", JobType.VERIFY_VOICE.value])
        outcome = build_runner(config, db).run_once()
        job = JobRepository(db).get(UUID(record["qa_job_id"]))
        audio = AudioAssetRepository(db).get(UUID(record["audio_asset_id"]))
        if outcome is None or job is None or audio is None:
            raise ValueError("normal Voice QA worker did not execute V45 Job")
        generation = audio.metadata.get("voice_generation") or {}
        qa = generation.get("qa") or {}
        record.update({"qa_job_status": job.status.value, "qa_job_error_code": job.error_code,
                       "qa_state": generation.get("qa_state"), "qa": qa})
        if (job.status.value == "completed" and generation.get("qa_state") == "verified"
                and qa.get("copy_coverage") == 1.0 and qa.get("missing_token_count") == 0
                and qa.get("duplicate_token_count") == 0 and qa.get("substitution_token_count") == 0):
            record["status"] = "awaiting_u_review"
        else:
            record["status"] = "blocked_voice_qa"
        save(record)
        print(json.dumps({"status": record["status"], "qa_job_status": job.status.value,
                          "qa_state": generation.get("qa_state")}, ensure_ascii=False))


if __name__ == "__main__":
    {"prepare": prepare, "voice": run_voice, "queue-qa": queue_qa, "qa": run_qa}[sys.argv[1]]()
