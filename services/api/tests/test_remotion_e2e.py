"""Real local FFmpeg + Remotion fixture for the Task 015 render slice."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from app.assembly import VideoSpecAssembler
from app.db import AssetRepository, ClipRepository, Database
from app.domain.models import CandidateAsset, Clip, CostCategory, Project, RationalFps, ScenePlan, SourceKind, UsageCost, VisualIntent
from app.media import FFProbeAdapter, MediaImporter
from app.renderer import RemotionRenderer


def _binaries() -> tuple[str, str]:
    ffmpeg = os.environ.get("CONTENT_OS_FFMPEG") or shutil.which("ffmpeg")
    ffprobe = os.environ.get("CONTENT_OS_FFPROBE") or shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("real FFmpeg/ffprobe binaries are unavailable")
    return ffmpeg, ffprobe


def _make_source(ffmpeg: str, output: Path, color: str, frequency: int) -> None:
    subprocess.run(
        [
            ffmpeg, "-y", "-f", "lavfi", "-i", f"color=c={color}:size=360x640:rate=30",
            "-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=48000", "-t", "0.8",
            "-c:v", "mpeg4", "-q:v", "2", "-c:a", "aac", "-shortest", str(output),
        ],
        check=True, capture_output=True,
    )


def _scene(project: Project, number: int) -> ScenePlan:
    return ScenePlan(
        project_id=project.id, scene_id=f"scene_{number:02d}", order=number,
        purpose="hook" if number == 0 else "explain", voice_text=f"Local caption {number}", duration_target_ms=400,
        visual_intent=VisualIntent(subject="creator", action="demonstrating software", framing="close"),
        preferred_sources=[SourceKind.USER_ASSET], fallback_sources=[SourceKind.CAPTURE],
    )


def _candidate(scene: ScenePlan, asset_id, clip_id) -> CandidateAsset:
    return CandidateAsset(
        scene_plan_id=scene.id, source_kind=SourceKind.USER_ASSET, asset_id=asset_id, clip_id=clip_id,
        match_score=1.0, why=["fixture local continuous Clip"], recommended=True,
        estimated_cost=UsageCost(category=CostCategory.USER_ASSET, amount=Decimal("0"), currency="USD"),
    )


def _probe_output(ffprobe: str, output: Path) -> dict[str, object]:
    completed = subprocess.run(
        [
            ffprobe, "-v", "error", "-count_frames", "-show_entries",
            "format=duration:stream=codec_type,width,height,r_frame_rate,avg_frame_rate,nb_read_frames,duration",
            "-of", "json", str(output),
        ],
        check=True, capture_output=True, text=True,
    )
    return json.loads(completed.stdout)


def test_real_remotion_renders_persisted_two_clip_vertical_timeline(tmp_path: Path) -> None:
    ffmpeg, ffprobe = _binaries()
    source_one = tmp_path / "red source with spaces.mp4"
    source_two = tmp_path / "blue source 中文.mp4"
    _make_source(ffmpeg, source_one, "red", 440)
    _make_source(ffmpeg, source_two, "blue", 660)
    source_hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (source_one, source_two)}

    db = Database(tmp_path / "render.sqlite")
    try:
        importer = MediaImporter(db, tmp_path / "data root", FFProbeAdapter(ffprobe))
        assets = [
            importer.import_path(source_one, "fixture-rights-one"),
            importer.import_path(source_two, "fixture-rights-two"),
        ]
        clips = [
            Clip(asset_id=asset.id, start_ms=100, end_ms=500, asset_duration_ms=asset.duration_ms)
            for asset in assets
        ]
        clip_repo = ClipRepository(db)
        for clip in clips:
            clip_repo.create(clip)
        project = Project(
            ip_profile_id=uuid4(), title="Real renderer fixture", topic="Local source reuse",
            resolution_width=360, resolution_height=640, fps=RationalFps(numerator=30, denominator=1),
            created_at=datetime.now(timezone.utc),
        )
        scenes = [_scene(project, 0), _scene(project, 1)]
        spec = VideoSpecAssembler(AssetRepository(db), clip_repo).assemble(
            project, scenes, {scene.id: _candidate(scene, asset.id, clip.id) for scene, asset, clip in zip(scenes, assets, clips)},
        )
        assert [scene.start_frame for scene in spec.scenes] == [0, 12]
        assert [scene.duration_frames for scene in spec.scenes] == [12, 12]
        output = tmp_path / "rendered vertical.mp4"
        result = RemotionRenderer(
            AssetRepository(db), clip_repo, renderer_dir=Path(__file__).parents[3] / "apps" / "renderer", timeout_seconds=120,
        ).render(spec, output)

        assert result == output.resolve() and result.is_file() and result.stat().st_size > 0
        metadata = _probe_output(ffprobe, output)
        video = next(stream for stream in metadata["streams"] if stream["codec_type"] == "video")
        audio = next(stream for stream in metadata["streams"] if stream["codec_type"] == "audio")
        assert (video["width"], video["height"]) == (360, 640)
        assert video["r_frame_rate"] == "30/1"
        assert int(video["nb_read_frames"]) == 24
        assert audio["codec_type"] == "audio"
        duration = Decimal(str(metadata["format"]["duration"]))
        assert Decimal("0.75") <= duration <= Decimal("0.90")
        # The two source files and retained content-addressed originals survive exactly unchanged.
        for path, digest in source_hashes.items():
            assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
        assert all(hashlib.sha256(Path(asset.source_file).read_bytes()).hexdigest() == asset.content_hash for asset in assets)
    finally:
        db.close()
