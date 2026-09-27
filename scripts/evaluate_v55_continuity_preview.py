"""Make a non-admitted review preview from nine human-approved Talking children."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api"))

from app.db import (  # noqa: E402
    AssetRepository, AudioAssetRepository, ClipRepository, Database, JobRepository,
    TalkingRunRepository, TalkingSliceSeriesContinuityReviewRepository,
    TalkingSliceSeriesRepository,
)
from app.media import FFProbeAdapter, MediaImporter  # noqa: E402
from app.runtime import resolve_local_executable  # noqa: E402
from app.talking.runs import TalkingRunAssembler  # noqa: E402
from app.talking.series_review import assess_talking_slice_series  # noqa: E402

DATA_ROOT = ROOT / "content-os-data"
SERIES_ID = UUID("9cc362bc-7cfc-44de-bf8b-b05041eacbbe")
EVIDENCE_DIR = DATA_ROOT / "evaluation-evidence" / "v55-continuity-preview"
PREVIEW = EVIDENCE_DIR / "v55-review-only.mp4"


def main() -> None:
    with Database(DATA_ROOT / "content-os.sqlite3") as db:
        series = TalkingSliceSeriesRepository(db).get(SERIES_ID)
        if series is None or TalkingRunRepository(db).get_by_series_id(SERIES_ID) is not None:
            raise ValueError("The exact series is missing or already admitted as a TalkingRun")
        assets = AssetRepository(db)
        jobs = JobRepository(db)
        children = [jobs.get(job_id) for job_id in series.child_job_ids]
        review = assess_talking_slice_series(series, children, assets.list())
        if not review.ready_for_human_continuity_review or len(review.children) != 9:
            raise ValueError("All nine exact child videos need technical and human approval")
        if TalkingSliceSeriesContinuityReviewRepository(db).get_by_series_id(SERIES_ID) is not None:
            raise ValueError("Continuity review has already been submitted")
        master = AudioAssetRepository(db).get(series.narration_audio_id)
        if master is None or not master.transcript_segments:
            raise ValueError("Verified Master speech timing is unavailable")
        start_ms, end_ms = master.transcript_segments[0].start_ms, master.transcript_segments[-1].end_ms
        ordered_assets = [assets.get(child.output_asset_id) for child in review.children]
        if any(asset is None for asset in ordered_assets):
            raise ValueError("Review child media is missing")
        ffprobe = FFProbeAdapter(resolve_local_executable("ffprobe"))
        assembler = TalkingRunAssembler(
            assets=assets, audios=AudioAssetRepository(db), clips=ClipRepository(db),
            jobs=jobs, reviews=TalkingSliceSeriesContinuityReviewRepository(db),
            runs=TalkingRunRepository(db), importer=MediaImporter(db, DATA_ROOT, ffprobe),
            output_root=EVIDENCE_DIR, ffmpeg_command=resolve_local_executable("ffmpeg"),
        )
        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        if not PREVIEW.is_file():
            generated = assembler._assemble_media(ordered_assets, master, start_ms, end_ms, SERIES_ID)
            generated.replace(PREVIEW)
        metadata = ffprobe.probe(PREVIEW)
        streams = metadata.metadata.get("streams")
        types = {item.get("codec_type") for item in streams if isinstance(item, dict)} if isinstance(streams, list) else set()
        if not {"video", "audio"}.issubset(types) or abs(metadata.duration_ms - (end_ms - start_ms)) > 80:
            raise ValueError("Review preview failed independent media/timing probe")
        digest = hashlib.sha256()
        with PREVIEW.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        print(json.dumps({
            "series_id": str(SERIES_ID), "preview": str(PREVIEW),
            "sha256": digest.hexdigest(), "duration_ms": metadata.duration_ms,
            "master_speech_start_ms": start_ms, "master_speech_end_ms": end_ms,
            "children": len(review.children), "review_only": True,
            "talking_run_admitted": False,
        }))


if __name__ == "__main__":
    main()
