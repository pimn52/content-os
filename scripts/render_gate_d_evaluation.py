"""Render an isolated 30–60s hybrid Talking timeline through the normal worker.

This is a Gate D technical-integration harness.  It keeps the generated
Talking scene's verified new narration, mutes B-roll source audio, uses only
authorized local media, and records an explicit limitation: the surrounding
typography/B-roll copy is not additional generated speech and therefore does
not by itself satisfy the final 30–60 second voice gate.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from app.assembly import milliseconds_to_frames, source_interval_to_frames
from app.db import AssetRepository, AudioAssetRepository, ClipRepository, Database, JobRepository, ProjectRepository
from app.domain.models import Clip, Job, JobType, RenderVideoJobPayload, SourceKind, VideoCaption, VideoScene, VideoSpec, VideoVisual, VerticalReframeMode
from app.jobs import JobStore
from app.media import FFProbeAdapter, MediaImporter
from app.renderer import render_output_path
from app.runtime import resolve_local_executable
from app.worker_cli import parse_config, run


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--talking-asset-id", required=True)
    parser.add_argument("--narration-audio-id", required=True)
    parser.add_argument("--broll-video", action="append", required=True, type=Path, help="Authorized production B-roll; supply at least three.")
    parser.add_argument("--authorization-reference", required=True)
    return parser


def _visual(asset, clip, *, center_crop: bool = False, bottom_crop_ratio: float = 0) -> VideoVisual:
    return VideoVisual(
        source_kind=asset.source_kind,
        authorization_reference=asset.authorization_reference,
        asset_id=asset.id,
        clip_id=clip.id,
        clip_start_ms=clip.start_ms,
        clip_end_ms=clip.end_ms,
        source_duration_ms=asset.duration_ms,
        vertical_reframe_mode=VerticalReframeMode.CENTER_CROP if center_crop else VerticalReframeMode.CONTAIN,
        vertical_reframe_evidence_reference=("human-review:2026-09-14-talking-center-crop" if center_crop else None),
        source_bottom_crop_ratio=bottom_crop_ratio,
    )


def main() -> int:
    args = _parser().parse_args()
    if len(args.broll_video) < 3:
        raise SystemExit("supply at least three authorized B-roll videos")
    for path in args.broll_video:
        if not path.resolve().is_file():
            raise SystemExit(f"B-roll video is unavailable: {path}")
    data_root = args.data_root.resolve()
    database_path = args.database.resolve()
    db = Database(database_path)
    try:
        projects = ProjectRepository(db)
        project = projects.get(UUID(args.project_id))
        talking = AssetRepository(db).get(UUID(args.talking_asset_id))
        narration = AudioAssetRepository(db).get(UUID(args.narration_audio_id))
        if project is None or talking is None or narration is None:
            raise SystemExit("project, Talking asset, or narration asset is unavailable")
        generation = talking.metadata.get("talking_generation")
        if talking.source_kind is not SourceKind.AI_VIDEO or not isinstance(generation, dict) or generation.get("qa_state") != "verified":
            raise SystemExit("Talking asset must be an automated-QA-verified generated video")
        clips = ClipRepository(db)
        talking_clip = Clip(
            asset_id=talking.id, start_ms=0, end_ms=3_937, asset_duration_ms=talking.duration_ms,
            talking_candidate=True,
        )
        clips.create(talking_clip)
        importer = MediaImporter(db, data_root, FFProbeAdapter(resolve_local_executable("ffprobe")))
        broll: list[tuple[object, Clip]] = []
        for index, source in enumerate(args.broll_video[:3]):
            asset = importer.import_path(source.resolve(), args.authorization_reference)
            start_ms = min((index + 1) * 30_000, max(0, asset.duration_ms - 8_000))
            end_ms = min(asset.duration_ms, start_ms + 8_000)
            if end_ms - start_ms < 1_000:
                raise SystemExit("B-roll is too short for a reviewable timeline")
            clip = clips.create(Clip(asset_id=asset.id, start_ms=start_ms, end_ms=end_ms, asset_duration_ms=asset.duration_ms))
            broll.append((asset, clip))

        def frames(milliseconds: int) -> int:
            return milliseconds_to_frames(milliseconds, project.fps)

        durations = (3_937, 8_000, 8_000, 8_000, 6_000)
        source_frames = (
            source_interval_to_frames(talking_clip.start_ms, talking_clip.end_ms, project.fps),
            *(source_interval_to_frames(clip.start_ms, clip.end_ms, project.fps) for _, clip in broll),
        )
        frame_durations = (*source_frames, frames(durations[4]))
        starts: list[int] = []
        cursor = 0
        for duration in frame_durations:
            starts.append(cursor)
            cursor += duration
        scenes = [
            VideoScene(
                scene_id="new-talking-hook", start_frame=starts[0], duration_frames=frame_durations[0],
                visual=_visual(talking, talking_clip, center_crop=True), narration_asset_id=narration.id,
                narration_start_ms=0, narration_end_ms=narration.duration_ms,
                captions=[
                    VideoCaption(start_ms=0, end_ms=1_260, text="先把重点说清楚"),
                    VideoCaption(start_ms=1_940, end_ms=3_700, text="再让每个镜头服务于结论"),
                ],
            ),
            *[
                VideoScene(
                    scene_id=f"authorized-broll-{index + 1}", start_frame=starts[index + 1], duration_frames=frame_durations[index + 1],
                    # The reviewed B-roll sources include their original
                    # burned-in captions. Render-time center crop removes the
                    # conflicting lower third without modifying originals.
                    visual=_visual(asset, clip, center_crop=True, bottom_crop_ratio=0.18),
                    caption=text,
                )
                for index, ((asset, clip), text) in enumerate(zip(
                    broll,
                    ("结论先行：观众先知道该记住什么。", "镜头只补充结论需要的证据。", "把重复表达留在剪辑里，而不是重拍里。"),
                ))
            ],
            VideoScene(
                scene_id="typography-close", start_frame=starts[4], duration_frames=frame_durations[4],
                visual=VideoVisual(source_kind=SourceKind.TYPOGRAPHY, authorization_reference="local-typography"),
                caption="重点清楚，镜头才有意义。",
            ),
        ]
        spec = VideoSpec(
            project_id=project.id, format=project.format, width=project.resolution_width,
            height=project.resolution_height, fps=project.fps, scenes=scenes,
        )
        render_id = uuid4()
        now = datetime.now(timezone.utc)
        job = Job(
            project_id=project.id, type=JobType.RENDER,
            idempotency_key=f"gate-d-talking-integration:{render_id}", created_at=now, updated_at=now,
            payload=RenderVideoJobPayload(project_id=project.id, render_id=render_id, video_spec=spec),
        )
        JobStore(db).enqueue(job)
        spec_path = data_root / "gate-d-talking-integration-video-spec.json"
        spec_path.write_text(spec.model_dump_json(indent=2) + "\n", encoding="utf-8")
    finally:
        db.close()

    config = parse_config([
        "--db", str(database_path), "--data-root", str(data_root), "--once",
        "--job-type", JobType.RENDER.value, "--lease-seconds", "1200", "--heartbeat-seconds", "20",
    ])
    if run(config) != 0:
        raise SystemExit("render worker did not exit cleanly")
    db = Database(database_path)
    try:
        completed = JobRepository(db).get(job.id)
        output = render_output_path(data_root / "renders", project.id, render_id)
        if completed is None or completed.status.value != "completed" or not output.is_file() or output.stat().st_size == 0:
            raise SystemExit("normal render worker did not produce the expected local MP4")
        report = {
            "job_id": str(job.id), "job_status": completed.status.value,
            "render_id": str(render_id), "output": str(output), "output_bytes": output.stat().st_size,
            "duration_target_ms": sum(durations),
            "talking_asset_id": str(talking.id), "narration_asset_id": str(narration.id),
            "limitations": [
                "Only the first 3.937 seconds use verified new generated narration and Talking.",
                "The remaining B-roll/typography captions are silent technical assembly content, not a 30–60 second cloned-voice proof.",
                "Talking uses an explicit human-reviewed center crop; caption-conflicting B-roll also uses a 0.18 bottom safety crop. No automatic face-detection crop is claimed.",
                "U-Talking and U-Product remain required before product-gate acceptance.",
            ],
        }
    finally:
        db.close()
    report_path = data_root / "gate-d-talking-integration.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
