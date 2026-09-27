"""One bounded five-Span normal Voice → Master → independent QA proof."""
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
EVIDENCE = DATA_ROOT / "evaluation-evidence" / "v48-normal-master"
MANIFEST = EVIDENCE / "v48-normal-master.json"
V43 = DATA_ROOT / "evaluation-evidence" / "v43-reference-continuity" / "v43-reference-continuity.json"
V41 = DATA_ROOT / "evaluation-evidence" / "v41-punctuation-delivery" / "v41-punctuation-delivery.json"
WINDOWS = ((28_240, 34_240), (240, 6_240))
REFERENCE_CLIP = "e3547414-ae03-5f1d-a2be-1b959fc75a58"
AUTHORIZATION = "v48:authorized-creator-voice-evaluation"


def load() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def save(value: dict) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def client() -> TestClient:
    os.environ["CONTENT_OS_DATA_ROOT"] = str(DATA_ROOT)
    return TestClient(create_app(DB_PATH))


def configure_voice() -> None:
    os.environ["CONTENT_OS_DATA_ROOT"] = str(DATA_ROOT)
    os.environ["CONTENT_OS_VOICE_PROVIDER"] = "omnivoice"
    os.environ["CONTENT_OS_OMNIVOICE_PYTHON"] = str(base.RUNTIME)
    os.environ["CONTENT_OS_OMNIVOICE_MODEL"] = str(base.MODEL)
    os.environ["CONTENT_OS_OMNIVOICE_NUM_STEP"] = "32"
    os.environ["CONTENT_OS_OMNIVOICE_SPEED"] = "1.0"
    os.environ["CONTENT_OS_OMNIVOICE_DEVICE"] = "cuda"
    os.environ["CONTENT_OS_OMNIVOICE_TIMEOUT_SECONDS"] = "900"


def configure_qa() -> None:
    os.environ["CONTENT_OS_DATA_ROOT"] = str(DATA_ROOT)
    os.environ["CONTENT_OS_VOICE_QA_ASR_MODEL"] = str(DATA_ROOT / "models" / "faster-whisper-medium")
    os.environ["CONTENT_OS_VOICE_QA_ASR_DEVICE"] = "cpu"
    os.environ["CONTENT_OS_VOICE_QA_ASR_COMPUTE_TYPE"] = "int8"


def run_one(job_type: JobType, expected_id: str) -> None:
    with Database(DB_PATH) as db:
        runner = build_runner(parse_config(["--once", "--db", str(DB_PATH), "--job-type", job_type.value]), db)
        outcome = runner.run_once()
        if outcome is None or str(outcome.id) != expected_id:
            raise ValueError(f"normal Worker did not claim the expected {job_type.value} Job")


def source_audio(record: dict, job_id: str) -> tuple[object, Path]:
    with Database(DB_PATH) as db:
        job = JobRepository(db).get(UUID(job_id))
        if job is None or job.status.value != "completed":
            raise ValueError(f"normal Voice Job did not complete: {None if job is None else job.error_code}")
        audios = [audio for audio in AudioAssetRepository(db).list()
                  if (audio.metadata.get("voice_generation") or {}).get("job_id") == job_id]
        if len(audios) != 1:
            raise ValueError("normal Voice Job has no unique generated AudioAsset")
        audio = audios[0]
        path = Path(audio.source_file)
        if not path.is_absolute():
            path = DATA_ROOT / path
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != audio.content_hash:
            raise ValueError("generated AudioAsset bytes are unavailable or changed")
        return audio, path


def qa_result(job_id: str, audio_id: str, exact_copy: str) -> dict:
    with Database(DB_PATH) as db:
        job = JobRepository(db).get(UUID(job_id))
        audio = AudioAssetRepository(db).get(UUID(audio_id))
        if job is None or audio is None:
            raise ValueError("normal QA Job or AudioAsset unavailable")
        generation = audio.metadata.get("voice_generation") or {}
        qa = generation.get("qa") or {}
        if (job.status.value != "completed" or generation.get("target_text") != exact_copy
                or generation.get("qa_state") != "verified" or qa.get("copy_coverage") != 1.0
                or any(qa.get(key) != 0 for key in ("missing_token_count", "duplicate_token_count", "substitution_token_count"))):
            raise ValueError(f"independent Voice QA did not pass full copy: {job.error_code}")
        return {"job_status": job.status.value, "qa": qa}


def prepare() -> None:
    if MANIFEST.exists():
        raise ValueError("V48 already prepared; no duplicate five-Span run")
    v43 = json.loads(V43.read_text(encoding="utf-8"))
    v41 = json.loads(V41.read_text(encoding="utf-8"))
    if v43["status"] != "u_voice_bounded_pass" or v43["copy"] != v41["copy"]:
        raise ValueError("V43 reviewed copy evidence is unavailable or changed")
    with client() as api:
        response = api.get(f"/voice-profiles/{base.PROFILE_ID}/reference-windows")
        if response.status_code != 200:
            raise ValueError(f"normal authorized reference listing failed: {response.text}")
        choices = response.json()
    selections = []
    for start, end in WINDOWS:
        matches = [row["selection"] for row in choices if row["selection"]["clip_id"] == REFERENCE_CLIP
                   and row["selection"]["start_ms"] == start and row["selection"]["end_ms"] == end]
        if len(matches) != 1:
            raise ValueError("one V43-reviewed reference window is not a current authorized normal choice")
        selections.append(matches[0])
    children = {}
    for index in range(5):
        row = (v43 if index < 3 else v41)["children"][str(index)]
        editorial, delivery = row["editorial_copy"], row["delivery_text"]
        children[str(index)] = {"status": "planned", "editorial_copy": editorial,
                                "delivery_text": delivery if delivery != editorial else None,
                                "selection": selections[0 if index < 3 else 1]}
    if " ".join(children[str(index)]["editorial_copy"] for index in range(5)) != v43["copy"]:
        raise ValueError("five planned Spans do not preserve the V43 exact copy")
    save({"package": "V48", "status": "generating", "project_id": str(base.PROJECT_ID),
          "profile_id": str(base.PROFILE_ID), "copy": v43["copy"], "authorization_reference": AUTHORIZATION,
          "source_review_manifest": str(V43), "children": children,
          "limits": "One normal generation and one independent QA per Span; no tuning or automatic retry."})
    print(json.dumps({"status": "prepared", "child_count": 5}))


def child(index: int) -> None:
    record = load()
    row = record["children"][str(index)]
    if row["status"] != "planned":
        raise ValueError(f"V48 Span {index} already started")
    with client() as api:
        response = api.post(f"/projects/{base.PROJECT_ID}/voice-jobs", json={
            "idempotency_key": f"v48-normal-master-span-{index}-one",
            "voice_profile_id": str(base.PROFILE_ID), "text": row["editorial_copy"],
            "delivery_text": row["delivery_text"], "authorization_reference": AUTHORIZATION,
            "language": "zh", "reference_window": row["selection"],
        })
        if response.status_code != 201:
            raise ValueError(f"normal Span {index} Voice Job rejected: {response.status_code} {response.text}")
        row["voice_job_id"] = response.json()["id"]
        row["status"] = "voice_job_queued"
        save(record)
    configure_voice()
    try:
        run_one(JobType.GENERATE_VOICE, row["voice_job_id"])
        audio, path = source_audio(record, row["voice_job_id"])
        generation = audio.metadata.get("voice_generation") or {}
        if generation.get("target_text") != row["editorial_copy"]:
            raise ValueError("normal Span did not preserve editorial/delivery provenance")
        if row["delivery_text"] is not None and generation.get("provider_delivery_text") != row["delivery_text"]:
            raise ValueError("normal Span did not preserve provider delivery text")
        if generation.get("reference_window") != row["selection"]:
            raise ValueError("normal Span did not apply the selected reference")
        row.update({"status": "voice_generated", "audio_asset_id": str(audio.id),
                    "path": str(path), "sha256": audio.content_hash, "duration_ms": audio.duration_ms})
        save(record)
    except Exception:
        row["status"] = "blocked_voice_execution"
        save(record)
        raise
    print(json.dumps({"span": index, "status": row["status"], "audio_asset_id": row["audio_asset_id"]}))


def child_qa(index: int) -> None:
    record = load()
    row = record["children"][str(index)]
    if row["status"] != "voice_generated":
        raise ValueError(f"V48 Span {index} is not ready for independent QA")
    with client() as api:
        response = api.post(f"/projects/{base.PROJECT_ID}/voice-qa-jobs", json={
            "idempotency_key": f"v48-normal-master-span-{index}-qa-one",
            "narration_audio_id": row["audio_asset_id"], "target_text": row["editorial_copy"],
        })
        if response.status_code != 201:
            raise ValueError(f"normal Span {index} QA Job rejected: {response.status_code} {response.text}")
        row["qa_job_id"] = response.json()["id"]
        row["status"] = "qa_job_queued"
        save(record)
    configure_qa()
    try:
        run_one(JobType.VERIFY_VOICE, row["qa_job_id"])
        row.update(qa_result(row["qa_job_id"], row["audio_asset_id"], row["editorial_copy"]))
        row["status"] = "qa_verified"
        save(record)
    except Exception:
        row["status"] = "blocked_voice_qa"
        save(record)
        raise
    print(json.dumps({"span": index, "status": row["status"]}))


def compose() -> None:
    record = load()
    if record["status"] != "generating" or any(row["status"] != "qa_verified" for row in record["children"].values()):
        raise ValueError("V48 requires five independently verified children")
    ids = [record["children"][str(index)]["audio_asset_id"] for index in range(5)]
    with client() as api:
        response = api.post(f"/projects/{base.PROJECT_ID}/master-narration-candidates", json={
            "take_audio_ids": ids, "compact_quiet_seams": True,
        })
        if response.status_code != 201:
            record["status"] = "blocked_master_composition"
            record["composition_error"] = {"status_code": response.status_code, "detail": response.json()}
            save(record)
            raise ValueError(f"normal Master composition failed: {response.status_code} {response.text}")
        master = response.json()
    generation = master["metadata"]["voice_generation"]
    composition = generation["composition"]
    if (generation["target_text"] != record["copy"] or generation["qa_state"] != "pending"
            or composition["source_take_audio_ids"] != ids or "measured_seam_compaction" not in composition):
        record["status"] = "blocked_master_provenance"
        save(record)
        raise ValueError("normal Master exact-copy/source/cut provenance mismatch")
    path = Path(master["source_file"])
    if not path.is_absolute():
        path = DATA_ROOT / path
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != master["content_hash"]:
        raise ValueError("normal Master bytes are unavailable or changed")
    record["master"] = {"audio_asset_id": master["id"], "path": str(path),
                        "sha256": master["content_hash"], "duration_ms": master["duration_ms"],
                        "composition": composition}
    record["status"] = "master_qa_pending"
    save(record)
    print(json.dumps({"status": record["status"], "master_audio_asset_id": master["id"]}))


def master_qa() -> None:
    record = load()
    if record["status"] != "master_qa_pending":
        raise ValueError("V48 Master is not ready for fresh QA")
    master = record["master"]
    with client() as api:
        response = api.post(f"/projects/{base.PROJECT_ID}/voice-qa-jobs", json={
            "idempotency_key": "v48-normal-master-fresh-full-copy-qa-one",
            "narration_audio_id": master["audio_asset_id"], "target_text": record["copy"],
        })
        if response.status_code != 201:
            raise ValueError(f"normal Master QA Job rejected: {response.status_code} {response.text}")
        master["qa_job_id"] = response.json()["id"]
        record["status"] = "master_qa_queued"
        save(record)
    configure_qa()
    try:
        run_one(JobType.VERIFY_VOICE, master["qa_job_id"])
        master.update(qa_result(master["qa_job_id"], master["audio_asset_id"], record["copy"]))
        record["status"] = "awaiting_u_review"
        save(record)
    except Exception:
        record["status"] = "blocked_master_qa"
        save(record)
        raise
    print(json.dumps({"status": record["status"], "master_audio_asset_id": master["audio_asset_id"]}))


if __name__ == "__main__":
    phase = sys.argv[1]
    if phase == "prepare":
        prepare()
    elif phase == "child":
        child(int(sys.argv[2]))
    elif phase == "child-qa":
        child_qa(int(sys.argv[2]))
    elif phase == "compose":
        compose()
    elif phase == "master-qa":
        master_qa()
    else:
        raise SystemExit("usage: evaluate_v48_normal_master.py prepare|child N|child-qa N|compose|master-qa")
