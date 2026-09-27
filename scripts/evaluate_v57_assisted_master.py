"""Bounded new-copy Voice rehearsal; assisted copy is not scene-planner proof."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from uuid import UUID

import evaluate_v48_normal_master as base
from app.db import AudioAssetRepository, Database
from app.domain.models import JobType
from prepare_v57_assisted_draft import COPY

PROJECT_ID = UUID("d45f42fb-06eb-4ec5-bded-66116ea6a872")
PROFILE_ID = UUID("29294db2-d3c6-45c2-b467-6a02e853c826")
MANIFEST = base.DATA_ROOT / "evaluation-evidence" / "v57-assisted-master" / "manifest.json"
PARTS = (
    "把长采访剪成短视频，最容易犯的错，是先找一句听起来很炸的话。",
    "可一句话离开上下文，观点可能就变了。",
    "我的做法是先找一个能独立回答的问题，再保留支撑答案的证据。",
    "开头把问题抛给观众，中间只放一条最有力的例子，",
    "最后交代这段话原本在讨论什么。",
    "这样剪出来的短视频，不只是抓眼球，也不会把受访者的意思剪歪。",
)
AUTHORIZATION = "v57:authorized-creator-voice-evaluation"


def save(manifest: dict) -> None:
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def prepare() -> None:
    if MANIFEST.exists() or "".join(PARTS) != COPY:
        raise ValueError("V57 manifest exists or Span copy differs from editable draft")
    with base.client() as api:
        draft = api.get(f"/projects/{PROJECT_ID}/draft")
        response = api.get(f"/voice-profiles/{PROFILE_ID}/reference-windows")
        if draft.status_code != 200 or draft.json()["script"] != COPY or response.status_code != 200:
            raise ValueError("normal draft or authorized reference choice is unavailable")
        matches = [
            value["selection"] for value in response.json()
            if value["selection"]["clip_id"] == base.REFERENCE_CLIP
            and value["selection"]["start_ms"] == 28_240
            and value["selection"]["end_ms"] == 34_240
        ]
        if len(matches) != 1:
            raise ValueError("reviewed 6s reference window is unavailable")
    save({
        "package": "V57", "status": "generating", "project_id": str(PROJECT_ID),
        "copy": COPY, "evidence_class": "assisted editor input, not product scene-planner output",
        "reference_selection": matches[0],
        "limits": "one normal OmniVoice attempt and one independent QA per Span; stop on failure",
        "children": [{"copy": part, "status": "planned"} for part in PARTS],
    })
    print(json.dumps({"status": "prepared", "children": len(PARTS)}))


def child(index: int) -> None:
    manifest = load()
    row = manifest["children"][index]
    if manifest["status"] != "generating" or row["status"] != "planned":
        raise ValueError("Span is not ready for its single bounded attempt")
    with base.client() as api:
        response = api.post(f"/projects/{PROJECT_ID}/voice-jobs", json={
            "idempotency_key": f"v57-assisted-span-{index}-once",
            "voice_profile_id": str(PROFILE_ID), "text": row["copy"],
            "authorization_reference": AUTHORIZATION, "language": "zh",
            "reference_window": manifest["reference_selection"],
        })
        if response.status_code != 201:
            raise ValueError(f"Voice Job rejected: {response.status_code} {response.text}")
        row["voice_job_id"], row["status"] = response.json()["id"], "queued"
        save(manifest)
    base.configure_voice()
    try:
        base.run_one(JobType.GENERATE_VOICE, row["voice_job_id"])
        audio, path = base.source_audio(manifest, row["voice_job_id"])
        if audio.metadata["voice_generation"]["target_text"] != row["copy"]:
            raise ValueError("generated copy provenance differs")
        row.update(status="generated", audio_asset_id=str(audio.id), media=str(path),
                   sha256=audio.content_hash, duration_ms=audio.duration_ms)
        save(manifest)
    except Exception:
        manifest["status"] = row["status"] = "blocked_voice_execution"
        save(manifest)
        raise
    print(json.dumps({"span": index, "status": row["status"], "duration_ms": row["duration_ms"]}))


def qa(index: int) -> None:
    manifest = load()
    row = manifest["children"][index]
    if manifest["status"] != "generating" or row["status"] != "generated":
        raise ValueError("Span is not ready for independent QA")
    with base.client() as api:
        response = api.post(f"/projects/{PROJECT_ID}/voice-qa-jobs", json={
            "idempotency_key": f"v57-assisted-span-{index}-qa-once",
            "narration_audio_id": row["audio_asset_id"], "target_text": row["copy"],
        })
        if response.status_code != 201:
            raise ValueError(f"Voice QA Job rejected: {response.status_code} {response.text}")
        row["qa_job_id"], row["status"] = response.json()["id"], "qa_queued"
        save(manifest)
    base.configure_qa()
    try:
        base.run_one(JobType.VERIFY_VOICE, row["qa_job_id"])
        row.update(base.qa_result(row["qa_job_id"], row["audio_asset_id"], row["copy"]))
        row["status"] = "qa_verified"
        save(manifest)
    except Exception:
        manifest["status"] = row["status"] = "blocked_copy_qa"
        save(manifest)
        raise
    print(json.dumps({"span": index, "status": row["status"]}))


def compose() -> None:
    manifest = load()
    if manifest["status"] != "generating" or any(row["status"] != "qa_verified" for row in manifest["children"]):
        raise ValueError("every child requires fresh independent QA")
    ids = [row["audio_asset_id"] for row in manifest["children"]]
    with base.client() as api:
        response = api.post(f"/projects/{PROJECT_ID}/master-narration-candidates", json={
            "take_audio_ids": ids, "compact_quiet_seams": True,
        })
        if response.status_code != 201:
            raise ValueError(f"normal Master composition failed: {response.status_code} {response.text}")
        master = response.json()
    path = Path(master["source_file"])
    if not path.is_absolute():
        path = base.DATA_ROOT / path
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != master["content_hash"]:
        raise ValueError("Master bytes/provenance are unavailable")
    manifest["master"] = {"audio_asset_id": master["id"], "media": str(path),
                          "sha256": master["content_hash"], "duration_ms": master["duration_ms"]}
    manifest["status"] = "master_qa_pending"
    save(manifest)
    print(json.dumps({"status": manifest["status"], "duration_ms": master["duration_ms"]}))


def master_qa() -> None:
    manifest = load()
    if manifest["status"] != "master_qa_pending":
        raise ValueError("Master is not ready for independent QA")
    master = manifest["master"]
    target = " ".join(PARTS)
    with base.client() as api:
        response = api.post(f"/projects/{PROJECT_ID}/voice-qa-jobs", json={
            "idempotency_key": "v57-assisted-master-qa-once",
            "narration_audio_id": master["audio_asset_id"], "target_text": target,
        })
        if response.status_code != 201:
            raise ValueError(f"Master QA Job rejected: {response.status_code} {response.text}")
        master["qa_job_id"] = response.json()["id"]
        manifest["status"] = "master_qa_queued"
        save(manifest)
    base.configure_qa()
    try:
        base.run_one(JobType.VERIFY_VOICE, master["qa_job_id"])
        master.update(base.qa_result(master["qa_job_id"], master["audio_asset_id"], target))
        manifest["status"] = "awaiting_u_voice"
        save(manifest)
    except Exception:
        manifest["status"] = "blocked_master_qa"
        save(manifest)
        raise
    print(json.dumps({"status": manifest["status"], "master": master["audio_asset_id"]}))


if __name__ == "__main__":
    phase = sys.argv[1]
    if phase == "prepare":
        prepare()
    elif phase == "child":
        child(int(sys.argv[2]))
    elif phase == "qa":
        qa(int(sys.argv[2]))
    elif phase == "compose":
        compose()
    elif phase == "master-qa":
        master_qa()
    elif phase == "run":
        for index in range(len(PARTS)):
            state = load()["children"][index]["status"]
            if state == "planned":
                child(index)
                state = "generated"
            if state == "generated":
                qa(index)
            elif state != "qa_verified":
                raise ValueError(f"Span {index} stopped at {state}")
        compose()
        master_qa()
    else:
        raise SystemExit("prepare|child N|qa N|compose|master-qa|run")
