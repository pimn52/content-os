"""One controlled V48 Span-2 replacement, then missing Spans and normal Master."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from uuid import UUID

import evaluate_v48_normal_master as base
from app.db import AudioAssetRepository, Database, JobRepository
from app.domain.models import JobType

EVIDENCE = base.DATA_ROOT / "evaluation-evidence" / "v50-master-completion"
MANIFEST = EVIDENCE / "v50-master-completion.json"


def load() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def save(value: dict) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def prepare() -> None:
    if MANIFEST.exists():
        raise ValueError("V50 already prepared; no duplicate replacement")
    old = json.loads(base.MANIFEST.read_text(encoding="utf-8"))
    diagnostic = json.loads((base.DATA_ROOT / "evaluation-evidence" / "v49-child-copy-attribution" / "v49-child-copy-attribution.json").read_text(encoding="utf-8"))
    if old["status"] != "blocked_child_copy_qa" or diagnostic["status"] != "qa_attribution_pass":
        raise ValueError("V48 failure or V49 attribution changed")
    children = {}
    with Database(base.DB_PATH) as db:
        for index in range(5):
            row = dict(old["children"][str(index)])
            if index < 2:
                audio = AudioAssetRepository(db).get(UUID(row["audio_asset_id"]))
                qa_job = JobRepository(db).get(UUID(row["qa_job_id"]))
                if (audio is None or qa_job is None or qa_job.status.value != "completed"
                        or audio.content_hash != row["sha256"]
                        or (audio.metadata.get("voice_generation") or {}).get("qa_state") != "verified"):
                    raise ValueError(f"V48 verified child {index} changed")
                row["status"] = "reused_verified"
            elif index == 2:
                if row["status"] != "blocked_voice_qa" or row["audio_asset_id"] != "78ccd38a-9524-476a-b4b2-e226c2dd7150":
                    raise ValueError("V48 failed child 2 changed")
                row = {key: row[key] for key in ("editorial_copy", "delivery_text", "selection")}
                row["status"] = "planned_replacement"
            else:
                if row["status"] != "planned":
                    raise ValueError(f"V48 missing child {index} was already generated")
            children[str(index)] = row
    if " ".join(children[str(index)]["editorial_copy"] for index in range(5)) != old["copy"]:
        raise ValueError("V50 five children do not preserve editorial exact copy")
    save({"package": "V50", "status": "generating", "project_id": old["project_id"],
          "profile_id": old["profile_id"], "copy": old["copy"],
          "authorization_reference": old["authorization_reference"],
          "reused_v48_failed_qa_audio_asset_id": "78ccd38a-9524-476a-b4b2-e226c2dd7150",
          "source_v48_manifest": str(base.MANIFEST), "source_v49_manifest": str(base.DATA_ROOT / "evaluation-evidence" / "v49-child-copy-attribution" / "v49-child-copy-attribution.json"),
          "children": children, "limits": "One new attempt for each of Spans 2/3/4; no retry, tuning, fourth version or provider switch."})
    print(json.dumps({"status": "prepared", "reused": [0, 1], "required": [2, 3, 4]}))


def child(index: int) -> None:
    if index not in (2, 3, 4):
        raise ValueError("V50 may generate only Spans 2, 3 and 4")
    record = load()
    row = record["children"][str(index)]
    if record["status"] != "generating" or row["status"] not in {"planned", "planned_replacement"}:
        raise ValueError(f"V50 Span {index} already started or package stopped")
    with base.client() as api:
        response = api.post(f"/projects/{base.base.PROJECT_ID}/voice-jobs", json={
            "idempotency_key": f"v50-normal-master-span-{index}-one",
            "voice_profile_id": str(base.base.PROFILE_ID), "text": row["editorial_copy"],
            "delivery_text": row["delivery_text"], "authorization_reference": record["authorization_reference"],
            "language": "zh", "reference_window": row["selection"],
        })
        if response.status_code != 201:
            raise ValueError(f"normal Span {index} Voice Job rejected: {response.status_code} {response.text}")
        row["voice_job_id"] = response.json()["id"]
        row["status"] = "voice_job_queued"
        save(record)
    base.configure_voice()
    try:
        base.run_one(JobType.GENERATE_VOICE, row["voice_job_id"])
        audio, path = base.source_audio(record, row["voice_job_id"])
        generation = audio.metadata.get("voice_generation") or {}
        if generation.get("target_text") != row["editorial_copy"] or generation.get("reference_window") != row["selection"]:
            raise ValueError("normal Span exact-copy/reference provenance mismatch")
        if row["delivery_text"] is not None and generation.get("provider_delivery_text") != row["delivery_text"]:
            raise ValueError("normal Span delivery provenance mismatch")
        row.update({"status": "voice_generated", "audio_asset_id": str(audio.id),
                    "path": str(path), "sha256": audio.content_hash, "duration_ms": audio.duration_ms})
        save(record)
    except Exception:
        row["status"] = "blocked_voice_execution"
        record["status"] = "blocked_voice_execution"
        save(record)
        raise
    print(json.dumps({"span": index, "status": row["status"], "audio_asset_id": row["audio_asset_id"]}))


def child_qa(index: int) -> None:
    record = load()
    row = record["children"][str(index)]
    if record["status"] != "generating" or row["status"] != "voice_generated":
        raise ValueError(f"V50 Span {index} is not ready for QA")
    with base.client() as api:
        response = api.post(f"/projects/{base.base.PROJECT_ID}/voice-qa-jobs", json={
            "idempotency_key": f"v50-normal-master-span-{index}-qa-one",
            "narration_audio_id": row["audio_asset_id"], "target_text": row["editorial_copy"],
        })
        if response.status_code != 201:
            raise ValueError(f"normal Span {index} QA Job rejected: {response.status_code} {response.text}")
        row["qa_job_id"] = response.json()["id"]
        row["status"] = "qa_job_queued"
        save(record)
    base.configure_qa()
    try:
        base.run_one(JobType.VERIFY_VOICE, row["qa_job_id"])
        row.update(base.qa_result(row["qa_job_id"], row["audio_asset_id"], row["editorial_copy"]))
        row["status"] = "qa_verified"
        save(record)
    except Exception:
        row["status"] = "blocked_voice_qa"
        record["status"] = "blocked_voice_qa"
        save(record)
        raise
    print(json.dumps({"span": index, "status": row["status"]}))


def compose() -> None:
    record = load()
    statuses = [record["children"][str(index)]["status"] for index in range(5)]
    if record["status"] != "generating" or statuses != ["reused_verified", "reused_verified", "qa_verified", "qa_verified", "qa_verified"]:
        raise ValueError("V50 requires two reused and three new independently verified children")
    ids = [record["children"][str(index)]["audio_asset_id"] for index in range(5)]
    with base.client() as api:
        response = api.post(f"/projects/{base.base.PROJECT_ID}/master-narration-candidates", json={
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
        path = base.DATA_ROOT / path
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != master["content_hash"]:
        record["status"] = "blocked_master_media"
        save(record)
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
        raise ValueError("V50 Master is not ready for fresh QA")
    master = record["master"]
    with base.client() as api:
        response = api.post(f"/projects/{base.base.PROJECT_ID}/voice-qa-jobs", json={
            "idempotency_key": "v50-normal-master-fresh-full-copy-qa-one",
            "narration_audio_id": master["audio_asset_id"], "target_text": record["copy"],
        })
        if response.status_code != 201:
            raise ValueError(f"normal Master QA Job rejected: {response.status_code} {response.text}")
        master["qa_job_id"] = response.json()["id"]
        record["status"] = "master_qa_queued"
        save(record)
    base.configure_qa()
    try:
        base.run_one(JobType.VERIFY_VOICE, master["qa_job_id"])
        master.update(base.qa_result(master["qa_job_id"], master["audio_asset_id"], record["copy"]))
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
        raise SystemExit("usage: evaluate_v50_master_completion.py prepare|child N|child-qa N|compose|master-qa")
