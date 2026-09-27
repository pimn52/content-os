"""Bind V62's exact child U-Talking verdict and build one review-only whole-run preview."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from uuid import UUID

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api"))
from app.db import (  # noqa: E402
    AssetRepository, AudioAssetRepository, ClipRepository, Database, JobRepository,
    TalkingRunRepository, TalkingSliceSeriesContinuityReviewRepository, TalkingSliceSeriesRepository,
)
from app.main import create_app  # noqa: E402
from app.media import FFProbeAdapter, MediaImporter  # noqa: E402
from app.runtime import resolve_local_executable  # noqa: E402
from app.talking.runs import TalkingRunAssembler  # noqa: E402
from app.talking.series_review import assess_talking_slice_series  # noqa: E402

DATA = ROOT / "content-os-data"
DB = DATA / "content-os.sqlite3"
SERIES_ID = UUID("d4c236ae-2b89-42d3-89f0-9db8068689ec")
PROJECT_ID = UUID("d45f42fb-06eb-4ec5-bded-66116ea6a872")
V61 = DATA / "evaluation-evidence" / "v61-talking-series" / "manifest.json"
V62 = DATA / "evaluation-evidence" / "v62-talking-child" / "recovery.json"
EVIDENCE = DATA / "evaluation-evidence" / "v63-talking-continuity"
PREVIEW = EVIDENCE / "review-only.mp4"
MANIFEST = EVIDENCE / "manifest.json"
REVIEW_REFERENCE = "user:2026-09-27-v62-ordered-ten-talking-videos-pass"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    v61 = json.loads(V61.read_text(encoding="utf-8"))
    v62 = json.loads(V62.read_text(encoding="utf-8"))
    rows = v61["children"][:9] + [{
        "index": 9, "job_id": v62["replacement_job_id"], "asset_id": v62["asset_id"],
        "sha256": v62["sha256"], "review_file": v62["review_file"], "status": v62["status"],
    }]
    if len(rows) != 10 or any(row["index"] != index or row["status"] != "qa_verified" for index, row in enumerate(rows)):
        raise ValueError("exact ordered ten-child QA set unavailable")
    if any(sha256(Path(row["review_file"])) != row["sha256"] for row in rows):
        raise ValueError("one or more user-reviewed video bytes changed")
    with Database(DB) as db:
        series = TalkingSliceSeriesRepository(db).get(SERIES_ID)
        if series is None or series.project_id != PROJECT_ID or [str(value) for value in series.child_job_ids] != [row["job_id"] for row in rows]:
            raise ValueError("persisted series differs from exact reviewed child set")
        assets = AssetRepository(db)
        for row in rows:
            asset = assets.get(UUID(row["asset_id"]))
            if asset is None or asset.content_hash != row["sha256"] or asset.metadata.get("talking_generation", {}).get("qa_state") != "verified":
                raise ValueError("reviewed child Asset or QA evidence changed")
    with TestClient(create_app(DB)) as api:
        for index, row in enumerate(rows):
            path = f"/projects/{PROJECT_ID}/talking-assets/{row['asset_id']}/human-review"
            with Database(DB) as db:
                current = AssetRepository(db).get(UUID(row["asset_id"]))
            generation = current.metadata["talking_generation"]
            if generation.get("human_review_state") == "approved":
                prior = generation.get("human_review", {})
                if prior.get("evidence_reference") != REVIEW_REFERENCE:
                    raise ValueError("child already has a different immutable human verdict")
                continue
            if generation.get("human_review_state") is not None:
                raise ValueError("child already has a conflicting human verdict")
            response = api.post(path, json={
                "approved": True, "evidence_reference": REVIEW_REFERENCE,
                "findings": [
                    f"用户审核 V62 所列第 {index + 1}/10 段精确视频后回复：通过。",
                    "该结论仅绑定本段生成视频；整段拼接预览仍须单独审核。",
                ],
            })
            if response.status_code != 200:
                raise ValueError(f"normal U-Talking review failed at {index}: {response.status_code} {response.text}")
    with Database(DB) as db:
        series = TalkingSliceSeriesRepository(db).get(SERIES_ID)
        assets = AssetRepository(db)
        jobs = JobRepository(db)
        review = assess_talking_slice_series(series, [jobs.get(job_id) for job_id in series.child_job_ids], assets.list())
        if not review.ready_for_human_continuity_review or len(review.children) != 10:
            raise ValueError("ten child reviews did not produce the normal continuity gate")
        if TalkingSliceSeriesContinuityReviewRepository(db).get_by_series_id(SERIES_ID) is not None or TalkingRunRepository(db).get_by_series_id(SERIES_ID) is not None:
            raise ValueError("continuity or TalkingRun already exists")
        master = AudioAssetRepository(db).get(series.narration_audio_id)
        if master is None or not master.transcript_segments:
            raise ValueError("verified Master timing unavailable")
        start_ms, end_ms = master.transcript_segments[0].start_ms, master.transcript_segments[-1].end_ms
        ordered_assets = [assets.get(child.output_asset_id) for child in review.children]
        if any(asset is None for asset in ordered_assets):
            raise ValueError("approved child media missing")
        ffprobe = FFProbeAdapter(resolve_local_executable("ffprobe"))
        assembler = TalkingRunAssembler(
            assets=assets, audios=AudioAssetRepository(db), clips=ClipRepository(db),
            jobs=jobs, reviews=TalkingSliceSeriesContinuityReviewRepository(db),
            runs=TalkingRunRepository(db), importer=MediaImporter(db, DATA, ffprobe),
            output_root=EVIDENCE, ffmpeg_command=resolve_local_executable("ffmpeg"),
        )
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        if not PREVIEW.is_file():
            generated = assembler._assemble_media(ordered_assets, master, start_ms, end_ms, SERIES_ID)
            generated.replace(PREVIEW)
        probe = ffprobe.probe(PREVIEW)
        streams = probe.metadata.get("streams")
        types = {item.get("codec_type") for item in streams if isinstance(item, dict)} if isinstance(streams, list) else set()
        if not {"video", "audio"}.issubset(types) or abs(probe.duration_ms - (end_ms - start_ms)) > 80 or not 30_000 <= probe.duration_ms <= 60_000:
            raise ValueError("whole review preview failed playable 30–60s media/timing QA")
        result = {
            "package": "V63", "status": "awaiting_u_review", "series_id": str(SERIES_ID),
            "ordered_child_asset_ids": [row["asset_id"] for row in rows],
            "child_review_reference": REVIEW_REFERENCE,
            "preview": str(PREVIEW), "preview_sha256": sha256(PREVIEW),
            "duration_ms": probe.duration_ms, "master_speech_interval_ms": [start_ms, end_ms],
            "video_audio_streams_verified": True, "review_only": True,
            "continuity_review_submitted": False, "talking_run_admitted": False,
        }
        MANIFEST.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
