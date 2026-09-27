"""Persist the exact V63 verdict and admit its reviewed whole TalkingRun."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import UUID

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api"))
from app.assembly.video_spec import VideoSpecAssembler  # noqa: E402
from app.db import AssetRepository, ClipRepository, Database, TalkingRunRepository, TalkingSliceSeriesContinuityReviewRepository  # noqa: E402
from app.main import create_app  # noqa: E402
from app.media import FFProbeAdapter  # noqa: E402
from app.runtime import resolve_local_executable  # noqa: E402

DATA = ROOT / "content-os-data"
DB = DATA / "content-os.sqlite3"
PROJECT_ID = UUID("d45f42fb-06eb-4ec5-bded-66116ea6a872")
SERIES_ID = UUID("d4c236ae-2b89-42d3-89f0-9db8068689ec")
EVIDENCE = DATA / "evaluation-evidence" / "v64-talking-run"
V63_MANIFEST = DATA / "evaluation-evidence" / "v63-talking-continuity" / "manifest.json"
REFERENCE = "user:2026-09-27-v63-whole-preview-continuity-publishability-pass"


def main() -> None:
    v63 = json.loads(V63_MANIFEST.read_text(encoding="utf-8"))
    preview = Path(v63["preview"])
    digest = hashlib.sha256(preview.read_bytes()).hexdigest()
    if v63["status"] != "awaiting_u_review" or digest != v63["preview_sha256"] or v63["duration_ms"] != 32_400:
        raise ValueError("exact user-reviewed preview changed")
    with Database(DB) as db:
        review = TalkingSliceSeriesContinuityReviewRepository(db).get_by_series_id(SERIES_ID)
        run = TalkingRunRepository(db).get_by_series_id(SERIES_ID)
    if run is not None and review is None:
        raise ValueError("TalkingRun exists without continuity review")
    base = f"/projects/{PROJECT_ID}/talking-slice-series/{SERIES_ID}"
    with TestClient(create_app(DB)) as api:
        readiness = api.get(base)
        if readiness.status_code != 200:
            raise ValueError(f"series readiness unavailable: {readiness.status_code} {readiness.text}")
        state = readiness.json()
        if len(state["children"]) != 10 or not state["ready_for_human_continuity_review"]:
            raise ValueError("exact ten-child series is not ready for continuity approval")
        response = api.post(f"{base}/continuity-review", json={
            "approved": True, "evidence_reference": REFERENCE,
            "findings": [
                "用户审核 V63 精确 32.400 秒整体预览，对段间衔接、画面连续性和整体可发布性回复：均通过。",
                "仅适用于该预览及其对应的十段系列；不代表最终混剪视频或商业授权通过。",
            ],
        })
        if response.status_code != 201:
            raise ValueError(f"continuity review failed: {response.status_code} {response.text}")
        persisted_review = response.json()
        response = api.post(f"{base}/talking-run")
        if response.status_code != 201:
            raise ValueError(f"TalkingRun admission failed: {response.status_code} {response.text}")
        admitted = response.json()
    with Database(DB) as db:
        assets, clips = AssetRepository(db), ClipRepository(db)
        asset = assets.get(UUID(admitted["assembled_asset_id"]))
        clip = clips.get(UUID(admitted["assembled_clip_id"]))
        if asset is None or clip is None:
            raise ValueError("TalkingRun Asset/Clip missing")
        media = Path(asset.source_file)
        media_hash = hashlib.sha256(media.read_bytes()).hexdigest()
        probe = FFProbeAdapter(resolve_local_executable("ffprobe")).probe(media)
        streams = probe.metadata.get("streams", [])
        stream_types = {item.get("codec_type") for item in streams if isinstance(item, dict)}
        run_meta = asset.metadata.get("talking_run", {})
        if (
            media_hash != digest or asset.content_hash != digest
            or not {"video", "audio"}.issubset(stream_types)
            or abs(probe.duration_ms - 32_400) > 80
            or clip.asset_id != asset.id or clip.start_ms != 0
            or clip.end_ms != asset.duration_ms or not clip.talking_candidate
            or run_meta.get("continuity_review_id") != persisted_review["id"]
            or run_meta.get("child_job_ids") != persisted_review["child_job_ids"]
            or run_meta.get("master_narration_audio_id") != admitted["master_narration_audio_id"]
        ):
            raise ValueError("TalkingRun media, timing or provenance mismatch")
        stored_asset, stored_clip = VideoSpecAssembler(assets, clips)._stored_real_clip(
            SimpleNamespace(scene_id="v64-admission-probe"),
            SimpleNamespace(source_kind=asset.source_kind, asset_id=asset.id, clip_id=clip.id),
        )
        if stored_asset.id != asset.id or stored_clip.id != clip.id:
            raise ValueError("normal VideoSpec rejected the admitted TalkingRun Clip")
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    result = {
        "package": "V64", "status": "pass", "series_id": str(SERIES_ID),
        "continuity_review_id": persisted_review["id"], "talking_run_id": admitted["id"],
        "asset_id": str(asset.id), "clip_id": str(clip.id), "media": str(media),
        "sha256": media_hash, "duration_ms": probe.duration_ms,
        "preview_byte_identical": True, "video_spec_eligible": True,
        "children": len(admitted["child_evidence"]), "final_video_rendered": False,
    }
    (EVIDENCE / "manifest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
