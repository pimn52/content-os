"""One evidence-backed V57 final-Span replacement and normal Master QA."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from uuid import UUID

import evaluate_v48_normal_master as base
import evaluate_v57_assisted_master as previous
from app.db import AudioAssetRepository, Database
from app.domain.models import JobType

MANIFEST = base.DATA_ROOT / "evaluation-evidence" / "v59-assisted-master" / "manifest.json"


def save(value: dict) -> None:
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def prepare() -> None:
    if MANIFEST.exists():
        raise ValueError("V59 is already prepared")
    old = previous.load()
    diagnostic = json.loads((base.DATA_ROOT / "evaluation-evidence" / "v58-copy-conflict" / "diagnostic.json").read_text(encoding="utf-8"))
    if old["status"] != "blocked_copy_qa" or diagnostic["status"] != "diagnostic_complete":
        raise ValueError("bounded V57/V58 evidence is unavailable")
    if old["children"][5]["status"] != "blocked_copy_qa" or diagnostic["audio_asset_id"] != old["children"][5]["audio_asset_id"]:
        raise ValueError("diagnostic does not cover the exact failed final Span")
    with Database(base.DB_PATH) as db:
        for row in old["children"][:5]:
            audio = AudioAssetRepository(db).get(UUID(row["audio_asset_id"]))
            if row["status"] != "qa_verified" or audio is None or audio.content_hash != row["sha256"] or audio.metadata["voice_generation"]["qa_state"] != "verified":
                raise ValueError("a reused V57 child is not independently verified")
    save({
        "package": "V59", "status": "replacement_planned",
        "project_id": old["project_id"], "copy": old["copy"],
        "evidence_class": old["evidence_class"], "reference_selection": old["reference_selection"],
        "reused_children": [{"audio_asset_id": row["audio_asset_id"], "sha256": row["sha256"],
                             "copy": row["copy"]} for row in old["children"][:5]],
        "failed_child_audio_asset_id": old["children"][5]["audio_asset_id"],
        "replacement": {"copy": old["children"][5]["copy"], "status": "planned"},
        "limits": "one identical-settings replacement, fresh child QA, one Master and fresh Master QA",
    })
    print(json.dumps({"status": "replacement_planned", "reused_children": 5}))


def replace() -> None:
    record = load()
    row = record["replacement"]
    if record["status"] != "replacement_planned" or row["status"] != "planned":
        raise ValueError("replacement is not ready for its only attempt")
    with base.client() as api:
        response = api.post(f"/projects/{previous.PROJECT_ID}/voice-jobs", json={
            "idempotency_key": "v59-assisted-final-span-replacement-once",
            "voice_profile_id": str(previous.PROFILE_ID), "text": row["copy"],
            "authorization_reference": previous.AUTHORIZATION, "language": "zh",
            "reference_window": record["reference_selection"],
        })
        if response.status_code != 201:
            raise ValueError(f"replacement Voice Job rejected: {response.status_code} {response.text}")
        row["voice_job_id"], row["status"] = response.json()["id"], "queued"
        save(record)
    base.configure_voice()
    try:
        base.run_one(JobType.GENERATE_VOICE, row["voice_job_id"])
        audio, path = base.source_audio(record, row["voice_job_id"])
        generation = audio.metadata["voice_generation"]
        if generation["target_text"] != row["copy"] or generation["reference_window"] != record["reference_selection"]:
            raise ValueError("replacement copy/reference provenance differs")
        row.update(status="generated", audio_asset_id=str(audio.id), media=str(path),
                   sha256=audio.content_hash, duration_ms=audio.duration_ms)
        record["status"] = "replacement_generated"
        save(record)
    except Exception:
        record["status"] = row["status"] = "blocked_replacement_generation"
        save(record)
        raise
    print(json.dumps({"status": record["status"], "duration_ms": row["duration_ms"]}))


def child_qa() -> None:
    record = load()
    row = record["replacement"]
    if record["status"] != "replacement_generated":
        raise ValueError("replacement is not generated")
    with base.client() as api:
        response = api.post(f"/projects/{previous.PROJECT_ID}/voice-qa-jobs", json={
            "idempotency_key": "v59-assisted-final-span-qa-once",
            "narration_audio_id": row["audio_asset_id"], "target_text": row["copy"],
        })
        if response.status_code != 201:
            raise ValueError(f"replacement QA Job rejected: {response.status_code} {response.text}")
        row["qa_job_id"], row["status"] = response.json()["id"], "qa_queued"
        save(record)
    base.configure_qa()
    try:
        base.run_one(JobType.VERIFY_VOICE, row["qa_job_id"])
        row.update(base.qa_result(row["qa_job_id"], row["audio_asset_id"], row["copy"]))
        row["status"] = record["status"] = "replacement_qa_verified"
        save(record)
    except Exception:
        record["status"] = row["status"] = "blocked_replacement_qa"
        save(record)
        raise
    print(json.dumps({"status": record["status"]}))


def compose() -> None:
    record = load()
    if record["status"] != "replacement_qa_verified":
        raise ValueError("replacement QA has not passed")
    ids = [row["audio_asset_id"] for row in record["reused_children"]] + [record["replacement"]["audio_asset_id"]]
    with base.client() as api:
        response = api.post(f"/projects/{previous.PROJECT_ID}/master-narration-candidates", json={
            "take_audio_ids": ids, "compact_quiet_seams": True,
        })
        if response.status_code != 201:
            record["status"] = "blocked_master_composition"
            save(record)
            raise ValueError(f"normal Master composition failed: {response.status_code} {response.text}")
        master = response.json()
    path = Path(master["source_file"])
    if not path.is_absolute():
        path = base.DATA_ROOT / path
    generation = master["metadata"]["voice_generation"]
    if (not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != master["content_hash"]
            or generation["composition"]["source_take_audio_ids"] != ids):
        record["status"] = "blocked_master_provenance"
        save(record)
        raise ValueError("normal Master bytes or ordered provenance differ")
    record["master"] = {"audio_asset_id": master["id"], "media": str(path),
                        "sha256": master["content_hash"], "duration_ms": master["duration_ms"]}
    record["status"] = "master_qa_pending"
    save(record)
    print(json.dumps({"status": record["status"], "duration_ms": master["duration_ms"]}))


def master_qa() -> None:
    record = load()
    if record["status"] != "master_qa_pending":
        raise ValueError("Master is not ready for fresh QA")
    master = record["master"]
    target = " ".join(previous.PARTS)
    with base.client() as api:
        response = api.post(f"/projects/{previous.PROJECT_ID}/voice-qa-jobs", json={
            "idempotency_key": "v59-assisted-master-full-copy-qa-once",
            "narration_audio_id": master["audio_asset_id"], "target_text": target,
        })
        if response.status_code != 201:
            raise ValueError(f"Master QA Job rejected: {response.status_code} {response.text}")
        master["qa_job_id"] = response.json()["id"]
        record["status"] = "master_qa_queued"
        save(record)
    base.configure_qa()
    try:
        base.run_one(JobType.VERIFY_VOICE, master["qa_job_id"])
        master.update(base.qa_result(master["qa_job_id"], master["audio_asset_id"], target))
        record["status"] = "awaiting_u_voice"
        save(record)
    except Exception:
        record["status"] = "blocked_master_qa"
        save(record)
        raise
    print(json.dumps({"status": record["status"], "master": master["audio_asset_id"]}))


if __name__ == "__main__":
    phase = sys.argv[1]
    if phase == "prepare":
        prepare()
    elif phase == "run":
        replace()
        child_qa()
        compose()
        master_qa()
    else:
        raise SystemExit("prepare|run")
