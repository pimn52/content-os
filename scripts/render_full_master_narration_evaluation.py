"""Render a 30s verified master narration through the normal Render worker.

The first scene is a separately QA-verified Talking asset.  Later scenes use
authorized continuous B-roll intervals.  This is an integration proof, not a
replacement for the required human U-Talking/U-Product quality judgments.
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
    parser.add_argument("--broll-clip-ids", required=True, nargs=4)
    return parser


def _captions(audio, scene_start_ms: int, scene_end_ms: int) -> list[VideoCaption]:
    return [
        VideoCaption(
            start_ms=max(segment.start_ms, scene_start_ms) - scene_start_ms,
            end_ms=min(segment.end_ms, scene_end_ms) - scene_start_ms,
            text=segment.text,
        )
        for segment in audio.transcript_segments
        if segment.end_ms > scene_start_ms and segment.start_ms < scene_end_ms
    ]


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
        broll_clips = [clips.get(UUID(item)) for item in args.broll_clip_ids]
        if any(item is None for item in (project, audio, talking, broll, talking_clip, *broll_clips)):
            raise SystemExit("project, master audio, or selected local visual is unavailable")
        assert project is not None and audio is not None and talking is not None and broll is not None and talking_clip is not None
        selected_broll = [item for item in broll_clips if item is not None]
        generation = audio.metadata.get("voice_generation")
        talking_generation = talking.metadata.get("talking_generation")
        if not isinstance(generation, dict) or generation.get("qa_state") != "verified":
            raise SystemExit("master narration must have independent verified voice QA")
        if talking.source_kind is not SourceKind.AI_VIDEO or not isinstance(talking_generation, dict) or talking_generation.get("qa_state") != "verified":
            raise SystemExit("Talking visual must have independent verified Talking QA")
        if audio.duration_ms < 30_000 or len(audio.transcript_segments) < 7:
            raise SystemExit("this full evaluation needs a real 30s multi-segment master narration")
        intervals = [(0, 3480), (3480, 11480), (11480, 19480), (19480, 27480), (27480, audio.duration_ms)]
        if talking_clip.end_ms - talking_clip.start_ms < 3480:
            raise SystemExit("Talking clip is too short for the hook interval")
        for clip, (start_ms, end_ms) in zip(selected_broll, intervals[1:]):
            if clip.end_ms - clip.start_ms < end_ms - start_ms:
                raise SystemExit("selected continuous B-roll clip is too short for its narration interval")
        master = MasterNarration(audio_asset_id=audio.id, start_ms=0, end_ms=audio.duration_ms, transcript_source=audio.transcript_source or "independent-asr", transcript_segments=audio.transcript_segments)
        scene_specs = []
        for index, (start_ms, end_ms) in enumerate(intervals):
            start_frame = 0 if start_ms == 0 else milliseconds_to_frames(start_ms, project.fps)
            end_frame = milliseconds_to_frames(end_ms, project.fps)
            if index == 0:
                visual = VideoVisual(source_kind=talking.source_kind, authorization_reference=talking.authorization_reference, asset_id=talking.id, clip_id=talking_clip.id, clip_start_ms=talking_clip.start_ms, clip_end_ms=talking_clip.start_ms + (end_ms - start_ms), source_duration_ms=talking.duration_ms, vertical_reframe_mode=VerticalReframeMode.CENTER_CROP, vertical_reframe_evidence_reference="human-review:2026-09-14-talking-center-crop")
                scene_id = "full-master-talking-hook"
            else:
                clip = selected_broll[index - 1]
                visual = VideoVisual(source_kind=broll.source_kind, authorization_reference=broll.authorization_reference, asset_id=broll.id, clip_id=clip.id, clip_start_ms=clip.start_ms, clip_end_ms=clip.start_ms + (end_ms - start_ms), source_duration_ms=broll.duration_ms, vertical_reframe_mode=VerticalReframeMode.CENTER_CROP, vertical_reframe_evidence_reference="human-review:2026-09-14-broll-center-crop", source_bottom_crop_ratio=0.18)
                scene_id = f"full-master-broll-{index}"
            scene_specs.append(VideoScene(scene_id=scene_id, start_frame=start_frame, duration_frames=end_frame - start_frame, visual=visual, narration_asset_id=audio.id, narration_start_ms=start_ms, narration_end_ms=end_ms, captions=_captions(audio, start_ms, end_ms)))
        spec = VideoSpec(project_id=project.id, format=project.format, width=project.resolution_width, height=project.resolution_height, fps=project.fps, master_narration=master, scenes=scene_specs)
        render_id, now = uuid4(), datetime.now(timezone.utc)
        job = Job(project_id=project.id, type=JobType.RENDER, idempotency_key=f"full-master-narration-evaluation:{render_id}", created_at=now, updated_at=now, payload=RenderVideoJobPayload(project_id=project.id, render_id=render_id, video_spec=spec))
        JobStore(db).enqueue(job)
        (args.data_root.resolve() / "full-master-narration-evaluation-video-spec.json").write_text(spec.model_dump_json(indent=2) + "\n", encoding="utf-8")
    finally:
        db.close()
    config = parse_config(["--db", str(args.database.resolve()), "--data-root", str(args.data_root.resolve()), "--once", "--job-type", "render", "--lease-seconds", "1800", "--heartbeat-seconds", "20"])
    if run(config) != 0:
        raise SystemExit("normal render worker failed")
    db = Database(args.database.resolve())
    try:
        completed = JobRepository(db).get(job.id)
        output = render_output_path(args.data_root.resolve() / "renders", project.id, render_id)
        if completed is None or completed.status.value != "completed" or not output.is_file() or output.stat().st_size == 0:
            raise SystemExit("normal render worker produced no MP4")
        report = {"job_id": str(job.id), "render_id": str(render_id), "output": str(output), "bytes": output.stat().st_size, "master_audio_id": str(audio.id), "duration_ms": audio.duration_ms, "scenes": len(scene_specs), "limitations": ["Technical full-flow evidence only; human U-Talking and U-Product quality gates remain required.", "B-roll uses explicit review-backed center/bottom crop; no automatic face-crop claim."]}
        (args.data_root.resolve() / "full-master-narration-evaluation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False))
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
