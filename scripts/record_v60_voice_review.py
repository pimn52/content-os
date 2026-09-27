"""Persist the user's explicit V60 six-dimension verdict on its exact Master."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from uuid import UUID

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api"))
from app.db import AudioAssetRepository, Database  # noqa: E402
from app.main import create_app  # noqa: E402

DATA = ROOT / "content-os-data"
PROJECT_ID = UUID("d45f42fb-06eb-4ec5-bded-66116ea6a872")
MASTER_ID = UUID("c1346d02-d967-4475-b5f4-4bdf7933b2d3")
SHA256 = "756492b4d61555654884276cde33f27f1cb57f1517181d712eb5b1977f21c480"
EVIDENCE = "user:2026-09-25-v60-six-dimension-publishability-pass"


def main() -> None:
    with Database(DATA / "content-os.sqlite3") as db:
        audio = AudioAssetRepository(db).get(MASTER_ID)
        if audio is None or audio.content_hash != SHA256 or not 30_000 <= audio.duration_ms <= 60_000:
            raise ValueError("exact V60 R1-duration Master identity changed")
        path = Path(audio.source_file)
        if not path.is_absolute():
            path = DATA / path
        if hashlib.sha256(path.read_bytes()).hexdigest() != SHA256:
            raise ValueError("exact user-reviewed WAV bytes changed")
        generation = audio.metadata.get("voice_generation") or {}
        if generation.get("qa_state") != "verified":
            raise ValueError("V60 independent technical QA is not verified")
        existing = generation.get("human_review")
        if existing is not None:
            if generation.get("human_review_state") != "approved" or existing.get("evidence_reference") != EVIDENCE:
                raise ValueError("a different immutable U-Voice review already exists")
            print(json.dumps({"audio_asset_id": str(MASTER_ID), "human_review_state": "approved", "reused": True}))
            return
    payload = {
        "approved": True,
        "evidence_reference": EVIDENCE,
        "findings": [
            "用户针对这条 32.56 秒 V60 Master，被问及相似度、自然度、重音、语速、停顿、修辞节奏及整体可发布性后回复：可以通过。",
            "六项均达到本条资产的可发布门槛；这不证明泛化的停顿/重音能力、自动场景规划或商业许可。",
        ],
        "likeness": "pass", "naturalness": "pass", "emphasis": "pass",
        "pace": "pass", "pauses": "pass", "rhythm": "pass",
    }
    with TestClient(create_app(DATA / "content-os.sqlite3")) as api:
        response = api.post(f"/projects/{PROJECT_ID}/voice-assets/{MASTER_ID}/human-review", json=payload)
        if response.status_code != 200:
            raise ValueError(f"normal U-Voice record rejected: {response.status_code} {response.text}")
        generation = response.json()["metadata"]["voice_generation"]
        if generation.get("human_review_state") != "approved" or generation.get("human_review", {}).get("evidence_reference") != EVIDENCE:
            raise ValueError("normal API did not persist the exact verdict")
    print(json.dumps({"audio_asset_id": str(MASTER_ID), "human_review_state": "approved", "evidence": EVIDENCE}))


if __name__ == "__main__":
    main()
