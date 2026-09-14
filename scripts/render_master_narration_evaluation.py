"""Render a persisted QA-verified MasterNarration through the normal worker.

This bounded integration proof uses one already-verified Talking visual and
one authorized B-roll visual.  It validates that the master audio plays once
across both timed scenes; it does not claim that 7.44 seconds is the R1 gate.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from app.assembly import milliseconds_to_frames
from app.db import AssetRepository, AudioAssetRepository, ClipRepository, Database, JobRepository, ProjectRepository
from app.domain.models import Job, JobType, MasterNarration, RenderVideoJobPayload, SourceKind, VideoCaption, VideoScene, VideoSpec, VideoVisual, VerticalReframeMode
from app.jobs import JobStore
from app.renderer import render_output_path
from app.worker_cli import parse_config, run


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--master-audio-id", required=True)
    parser.add_argument("--talking-asset-id", required=True)
    parser.add_argument("--talking-clip-id", required=True)
    parser.add_argument("--broll-asset-id", required=True)
    parser.add_argument("--broll-clip-id", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    db = Database(args.database.resolve())
    try:
        project = ProjectRepository(db).get(UUID(args.project_id))
        audio = AudioAssetRepository(db).get(UUID(args.master_audio_id))
        talking = AssetRepository(db).get(UUID(args.talking_asset_id))
        broll = AssetRepository(db).get(UUID(args.broll_asset_id))
        clips = ClipRepository(db)
        talking_clip = clips.get(UUID(args.talking_clip_id))
        broll_clip = clips.get(UUID(args.broll_clip_id))
        if None in (project, audio, talking, broll, talking_clip, broll_clip):
            raise SystemExit("project, master audio, or selected local visual is unavailable")
        generation = audio.metadata.get("voice_generation")
        talking_generation = talking.metadata.get("talking_generation")
        if not isinstance(generation, dict) or generation.get("qa_state") != "verified":
            raise SystemExit("master narration must have independent verified voice QA")
        if talking.source_kind is not SourceKind.AI_VIDEO or not isinstance(talking_generation, dict) or talking_generation.get("qa_state") != "verified":
            raise SystemExit("Talking visual must have independent verified Talking QA")
        if len(audio.transcript_segments) < 2 or audio.duration_ms < 2_000:
            raise SystemExit("master narration needs real timed multi-segment audio")
        split_ms = 3_480
        if talking_clip.end_ms - talking_clip.start_ms < split_ms or broll_clip.end_ms - broll_clip.start_ms < audio.duration_ms - split_ms:
            raise SystemExit("selected continuous visual is too short for master narration interval")
        first_frames = milliseconds_to_frames(split_ms, project.fps)
        total_frames = milliseconds_to_frames(audio.duration_ms, project.fps)
        master = MasterNarration(
            audio_asset_id=audio.id, start_ms=0, end_ms=audio.duration_ms,
            transcript_source=audio.transcript_source or "independent-asr", transcript_segments=audio.transcript_segments,
        )
        spec = VideoSpec(
            project_id=project.id, format=project.format, width=project.resolution_width, height=project.resolution_height, fps=project.fps,
            master_narration=master,
            scenes=[
                VideoScene(
                    scene_id="master-talking-hook", start_frame=0, duration_frames=first_frames,
                    visual=VideoVisual(source_kind=talking.source_kind, authorization_reference=talking.authorization_reference, asset_id=talking.id, clip_id=talking_clip.id, clip_start_ms=talking_clip.start_ms, clip_end_ms=talking_clip.start_ms + split_ms, source_duration_ms=talking.duration_ms, vertical_reframe_mode=VerticalReframeMode.CENTER_CROP, vertical_reframe_evidence_reference="human-review:2026-09-14-talking-center-crop"),
                    narration_asset_id=audio.id, narration_start_ms=0, narration_end_ms=split_ms,
                    captions=[VideoCaption(start_ms=439, end_ms=3120, text=audio.transcript_segments[0].text)],
                ),
                VideoScene(
                    scene_id="master-broll-proof", start_frame=first_frames, duration_frames=total_frames-first_frames,
                    visual=VideoVisual(source_kind=broll.source_kind, authorization_reference=broll.authorization_reference, asset_id=broll.id, clip_id=broll_clip.id, clip_start_ms=broll_clip.start_ms, clip_end_ms=broll_clip.start_ms + audio.duration_ms-split_ms, source_duration_ms=broll.duration_ms, vertical_reframe_mode=VerticalReframeMode.CENTER_CROP, vertical_reframe_evidence_reference="human-review:2026-09-14-broll-center-crop", source_bottom_crop_ratio=0.18),
                    narration_asset_id=audio.id, narration_start_ms=split_ms, narration_end_ms=audio.duration_ms,
                    captions=[VideoCaption(start_ms=3679-split_ms, end_ms=7000-split_ms, text=audio.transcript_segments[1].text)],
                ),
            ],
        )
        render_id, now = uuid4(), datetime.now(timezone.utc)
        job = Job(project_id=project.id, type=JobType.RENDER, idempotency_key=f"master-narration-evaluation:{render_id}", created_at=now, updated_at=now, payload=RenderVideoJobPayload(project_id=project.id, render_id=render_id, video_spec=spec))
        JobStore(db).enqueue(job)
        (args.data_root.resolve() / "master-narration-evaluation-video-spec.json").write_text(spec.model_dump_json(indent=2)+"\n", encoding="utf-8")
    finally:
        db.close()
    config = parse_config(["--db", str(args.database.resolve()), "--data-root", str(args.data_root.resolve()), "--once", "--job-type", "render", "--lease-seconds", "1200", "--heartbeat-seconds", "20"])
    if run(config) != 0:
        raise SystemExit("normal render worker failed")
    db = Database(args.database.resolve())
    try:
        completed = JobRepository(db).get(job.id)
        output = render_output_path(args.data_root.resolve() / "renders", project.id, render_id)
        if completed is None or completed.status.value != "completed" or not output.is_file() or output.stat().st_size == 0:
            raise SystemExit("normal render worker produced no MP4")
        report = {"job_id": str(job.id), "render_id": str(render_id), "output": str(output), "bytes": output.stat().st_size, "master_audio_id": str(audio.id), "duration_ms": audio.duration_ms, "limitations": ["Two-scene 7.44s integration proof only; not a 30–60s full-voice product result.", "B-roll uses explicit review-backed center/bottom crop; no automatic face crop claim."]}
        (args.data_root.resolve() / "master-narration-evaluation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False))
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
