"""Persist the exact V55 continuity verdict and admit its reviewed V54 series."""
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

DATA_ROOT = ROOT / "content-os-data"
PROJECT_ID = UUID("c710747f-4742-4a6f-b80d-1d46527f6c90")
SERIES_ID = UUID("9cc362bc-7cfc-44de-bf8b-b05041eacbbe")
PREVIEW = DATA_ROOT / "evaluation-evidence" / "v55-continuity-preview" / "v55-review-only.mp4"
PREVIEW_SHA256 = "920f0e9b975a43cfe06efb064fd541dfa5bf5c1493003cbe2fdfe41bf3579e66"
EVIDENCE_REFERENCE = "user:2026-09-25-v55-review-only-preview-pass"
FINDINGS = [
    "用户针对 V55 九段连续预览的拼接连续性和整体可发布性，回复：通过。",
    "仅适用于该 0–26.300s V54 系列及其已审核子视频，不代表 30–60s R1 或商业许可。",
]


def main() -> None:
    digest = hashlib.sha256(PREVIEW.read_bytes()).hexdigest()
    if digest != PREVIEW_SHA256:
        raise ValueError("V55 review preview changed after the human verdict")
    with Database(DATA_ROOT / "content-os.sqlite3") as db:
        review = TalkingSliceSeriesContinuityReviewRepository(db).get_by_series_id(SERIES_ID)
        run = TalkingRunRepository(db).get_by_series_id(SERIES_ID)
    if run is not None and review is None:
        raise ValueError("TalkingRun exists without continuity review")
    base = f"/projects/{PROJECT_ID}/talking-slice-series/{SERIES_ID}"
    with TestClient(create_app(DATA_ROOT / "content-os.sqlite3")) as client:
        readiness = client.get(base)
        if readiness.status_code != 200:
            raise ValueError(f"series readiness failed: {readiness.status_code} {readiness.text}")
        state = readiness.json()
        if len(state["children"]) != 9 or not state["ready_for_human_continuity_review"]:
            raise ValueError("the exact nine-child series is not ready for continuity approval")
        response = client.post(f"{base}/continuity-review", json={
            "approved": True,
            "evidence_reference": EVIDENCE_REFERENCE,
            "findings": FINDINGS,
        })
        if response.status_code != 201:
            raise ValueError(f"continuity review failed: {response.status_code} {response.text}")
        persisted_review = response.json()
        response = client.post(f"{base}/talking-run")
        if response.status_code != 201:
            raise ValueError(f"TalkingRun admission failed: {response.status_code} {response.text}")
        admitted = response.json()
    with Database(DATA_ROOT / "content-os.sqlite3") as db:
        assets, clips = AssetRepository(db), ClipRepository(db)
        asset = assets.get(UUID(admitted["assembled_asset_id"]))
        clip = clips.get(UUID(admitted["assembled_clip_id"]))
        if asset is None or clip is None:
            raise ValueError("TalkingRun is missing its imported Asset or Clip")
        media = Path(asset.source_file)
        media_hash = hashlib.sha256(media.read_bytes()).hexdigest()
        if media_hash != PREVIEW_SHA256 or asset.content_hash != media_hash:
            raise ValueError("admitted media bytes differ from the exact approved preview")
        probe = FFProbeAdapter(resolve_local_executable("ffprobe")).probe(media)
        streams = probe.metadata.get("streams", [])
        stream_types = {item.get("codec_type") for item in streams if isinstance(item, dict)}
        run_meta = asset.metadata.get("talking_run", {})
        if (
            not {"video", "audio"}.issubset(stream_types)
            or abs(probe.duration_ms - 26_300) > 80
            or clip.asset_id != asset.id or clip.start_ms != 0
            or clip.end_ms != asset.duration_ms or not clip.talking_candidate
            or run_meta.get("continuity_review_id") != persisted_review["id"]
            or run_meta.get("child_job_ids") != persisted_review["child_job_ids"]
            or run_meta.get("master_narration_audio_id") != admitted["master_narration_audio_id"]
        ):
            raise ValueError("TalkingRun real media, full Clip or provenance verification failed")
        resolved_asset, resolved_clip = VideoSpecAssembler(assets, clips)._stored_real_clip(
            SimpleNamespace(scene_id="v56-admission-probe"),
            SimpleNamespace(source_kind=asset.source_kind, asset_id=asset.id, clip_id=clip.id),
        )
        if resolved_asset.id != asset.id or resolved_clip.id != clip.id:
            raise ValueError("VideoSpec rejected the admitted TalkingRun Asset/Clip")
    print(json.dumps({
        "continuity_review_id": persisted_review["id"],
        "talking_run_id": admitted["id"],
        "asset_id": str(asset.id), "clip_id": str(clip.id),
        "media": str(media), "sha256": media_hash, "duration_ms": probe.duration_ms,
        "video_spec_eligible": True, "children": len(admitted["child_evidence"]),
    }))


if __name__ == "__main__":
    main()
