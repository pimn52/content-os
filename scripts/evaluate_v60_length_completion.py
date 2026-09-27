"""Add one substantive ending to the assisted draft, then verify a 30–60s Master."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from uuid import UUID

import evaluate_v48_normal_master as base
import evaluate_v57_assisted_master as v57
import evaluate_v59_assisted_master as v59
from app.db import AudioAssetRepository, Database
from app.domain.models import JobType

ENDING = "对内容负责，不只是避免断章取义，更要让观众带走一个完整的判断，而不是只带走一阵情绪。"
COPY = v57.COPY + ENDING
MANIFEST = base.DATA_ROOT / "evaluation-evidence" / "v60-length-completion" / "manifest.json"


def save(value: dict) -> None:
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def prepare() -> None:
    if MANIFEST.exists():
        raise ValueError("V60 already prepared")
    old = v59.load()
    if old["status"] != "awaiting_u_voice" or old["master"]["duration_ms"] >= 30_000:
        raise ValueError("V59 verified underlength evidence is unavailable")
    ids = [row["audio_asset_id"] for row in old["reused_children"]] + [old["replacement"]["audio_asset_id"]]
    with Database(base.DB_PATH) as db:
        for audio_id in ids:
            audio = AudioAssetRepository(db).get(UUID(audio_id))
            if audio is None or audio.metadata["voice_generation"]["qa_state"] != "verified":
                raise ValueError("one reused child is not QA-verified")
    with base.client() as api:
        current = api.get(f"/projects/{v57.PROJECT_ID}/draft")
        if current.status_code != 200 or current.json()["script"] != v57.COPY:
            raise ValueError("assisted draft differs from the V57 reviewed starting copy")
        response = api.put(f"/projects/{v57.PROJECT_ID}/draft", json={
            "topic": current.json()["topic"], "script": COPY,
        })
        if response.status_code != 200 or response.json()["script"] != COPY:
            raise ValueError("normal editable draft revision failed")
    save({
        "package": "V60", "status": "planned", "project_id": str(v57.PROJECT_ID),
        "copy": COPY, "evidence_class": "assisted editor input, not product scene-planner output",
        "ending": ENDING, "reference_selection": old["reference_selection"],
        "reused_audio_ids": ids, "underlength_master_id": old["master"]["audio_asset_id"],
        "new_span": {"copy": ENDING, "status": "planned"},
        "limits": "one new ending Span, one child QA, one new Master and full-copy QA",
    })
    print(json.dumps({"status": "planned", "reused_children": len(ids), "new_chars": len(ENDING)}))


def run() -> None:
    record = load()
    row = record["new_span"]
    if record["status"] != "planned":
        raise ValueError("V60 is not ready for its only ending generation")
    with base.client() as api:
        response = api.post(f"/projects/{v57.PROJECT_ID}/voice-jobs", json={
            "idempotency_key": "v60-new-topic-ending-span-once",
            "voice_profile_id": str(v57.PROFILE_ID), "text": ENDING,
            "authorization_reference": v57.AUTHORIZATION, "language": "zh",
            "reference_window": record["reference_selection"],
        })
        if response.status_code != 201:
            raise ValueError(f"ending Voice Job rejected: {response.status_code} {response.text}")
        row["voice_job_id"], row["status"] = response.json()["id"], "queued"
        save(record)
    base.configure_voice()
    try:
        base.run_one(JobType.GENERATE_VOICE, row["voice_job_id"])
        audio, path = base.source_audio(record, row["voice_job_id"])
        generation = audio.metadata["voice_generation"]
        if generation["target_text"] != ENDING or generation["reference_window"] != record["reference_selection"]:
            raise ValueError("ending copy/reference provenance differs")
        row.update(status="generated", audio_asset_id=str(audio.id), media=str(path),
                   sha256=audio.content_hash, duration_ms=audio.duration_ms)
        record["status"] = "ending_generated"
        save(record)
    except Exception:
        record["status"] = row["status"] = "blocked_ending_generation"
        save(record)
        raise
    print(json.dumps({"status": record["status"], "duration_ms": row["duration_ms"]}))
    with base.client() as api:
        response = api.post(f"/projects/{v57.PROJECT_ID}/voice-qa-jobs", json={
            "idempotency_key": "v60-new-topic-ending-qa-once",
            "narration_audio_id": row["audio_asset_id"], "target_text": ENDING,
        })
        if response.status_code != 201:
            raise ValueError(f"ending QA Job rejected: {response.status_code} {response.text}")
        row["qa_job_id"], row["status"] = response.json()["id"], "qa_queued"
        save(record)
    base.configure_qa()
    try:
        base.run_one(JobType.VERIFY_VOICE, row["qa_job_id"])
        row.update(base.qa_result(row["qa_job_id"], row["audio_asset_id"], ENDING))
        row["status"] = record["status"] = "ending_qa_verified"
        save(record)
    except Exception:
        record["status"] = row["status"] = "blocked_ending_qa"
        save(record)
        raise
    print(json.dumps({"status": record["status"]}))
    ids = record["reused_audio_ids"] + [row["audio_asset_id"]]
    with base.client() as api:
        response = api.post(f"/projects/{v57.PROJECT_ID}/master-narration-candidates", json={
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
    if (not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != master["content_hash"]
            or master["metadata"]["voice_generation"]["composition"]["source_take_audio_ids"] != ids):
        record["status"] = "blocked_master_provenance"
        save(record)
        raise ValueError("new Master bytes or ordered provenance differ")
    record["master"] = {"audio_asset_id": master["id"], "media": str(path),
                        "sha256": master["content_hash"], "duration_ms": master["duration_ms"]}
    if not 30_000 <= master["duration_ms"] <= 60_000:
        record["status"] = "blocked_master_duration"
        save(record)
        raise ValueError(f"new Master is outside 30–60s: {master['duration_ms']}ms")
    record["status"] = "master_qa_pending"
    save(record)
    print(json.dumps({"status": record["status"], "duration_ms": master["duration_ms"]}))
    master_qa()


def master_qa() -> None:
    record = load()
    if record["status"] != "master_qa_pending":
        raise ValueError("V60 Master is not ready for independent QA")
    master = record["master"]
    target = " ".join((*v57.PARTS, ENDING))
    with base.client() as api:
        response = api.post(f"/projects/{v57.PROJECT_ID}/voice-qa-jobs", json={
            "idempotency_key": "v60-new-topic-master-full-copy-qa-once",
            "narration_audio_id": master["audio_asset_id"], "target_text": target,
        })
        if response.status_code != 201:
            raise ValueError(f"new Master QA Job rejected: {response.status_code} {response.text}")
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
    print(json.dumps({"status": record["status"], "master": master["audio_asset_id"],
                      "duration_ms": master["duration_ms"]}))


if __name__ == "__main__":
    if sys.argv[1] == "prepare":
        prepare()
    elif sys.argv[1] == "run":
        run()
    elif sys.argv[1] == "master-qa":
        master_qa()
    else:
        raise SystemExit("prepare|run|master-qa")
