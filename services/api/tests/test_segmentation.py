from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.db import Database
from app.domain.models import Asset, RationalFps
from app.media.ffprobe import FFProbeAdapter
from app.media.importer import MediaImporter
from app.media.segmentation import (
    FFmpegBinaryMissing,
    FFmpegMalformedOutput,
    FFmpegProcessError,
    FFmpegSceneDetector,
    FFmpegTimeout,
    NonVideoSource,
    SourceFileMissing,
    continuous_clips,
    parse_scene_timestamps,
)


def _asset(path: Path, duration_ms: int = 3_000) -> Asset:
    return Asset(
        source_file=str(path),
        content_hash="a" * 64,
        duration_ms=duration_ms,
        width=640,
        height=360,
        fps=RationalFps(numerator=30, denominator=1),
        authorization_reference="fixture-rights",
        imported_at=datetime.now(timezone.utc),
    )


def _fake_ffmpeg(tmp_path: Path, body: str) -> tuple[str, str]:
    script = tmp_path / "fake ffmpeg.py"
    script.write_text(body, encoding="utf-8")
    return sys.executable, str(script)


def test_parser_clamps_sorts_deduplicates_and_uses_conservative_ms_rounding() -> None:
    output = "\n".join((
        "frame:2 pts:0 pts_time:2.0000",
        "frame:1 pts:0 pts_time:1.0001",
        "frame:3 pts:0 pts_time:2.0000",
        "frame:4 pts:0 pts_time:-0.1",
        "frame:5 pts:0 pts_time:99.0",
    ))
    assert parse_scene_timestamps(output, 3_000) == [0, 1001, 2000, 3000]


def test_continuous_clips_merge_short_fragments_and_cover_source(tmp_path: Path) -> None:
    asset = _asset(tmp_path / "source.mp4", duration_ms=2_000)
    clips = continuous_clips(asset, [1_500, 500, 550, 500], min_clip_duration_ms=200)

    assert [(clip.start_ms, clip.end_ms) for clip in clips] == [(0, 550), (550, 1500), (1500, 2000)]
    assert all(clip.end_ms > clip.start_ms for clip in clips)
    assert clips[0].start_ms == 0 and clips[-1].end_ms == asset.duration_ms
    assert all(left.end_ms == right.start_ms for left, right in zip(clips, clips[1:]))
    assert all(clip.asset_id == asset.id and clip.asset_duration_ms == asset.duration_ms for clip in clips)


def test_no_detected_cuts_returns_one_source_spanning_clip(tmp_path: Path) -> None:
    asset = _asset(tmp_path / "source.mp4", duration_ms=123)
    clips = continuous_clips(asset, [], min_clip_duration_ms=500)
    assert [(clip.start_ms, clip.end_ms) for clip in clips] == [(0, 123)]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"threshold": float("nan")}, "threshold"),
        ({"threshold": -0.01}, "threshold"),
        ({"threshold": 1.01}, "threshold"),
        ({"timeout_seconds": 0}, "timeout_seconds"),
        ({"timeout_seconds": float("inf")}, "timeout_seconds"),
        ({"min_clip_duration_ms": 0}, "min_clip_duration_ms"),
        ({"min_clip_duration_ms": True}, "min_clip_duration_ms"),
    ],
)
def test_detector_configuration_is_validated(kwargs: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        FFmpegSceneDetector(**kwargs)


def test_detector_uses_argv_and_returns_actual_clip_timestamps(tmp_path: Path) -> None:
    source = tmp_path / "clip with 中文.mp4"
    source.write_bytes(b"not inspected by fake")
    command = _fake_ffmpeg(tmp_path, "import sys\nprint('frame:1 pts_time:1.25')\n")
    clips = FFmpegSceneDetector(command, min_clip_duration_ms=100).segment(_asset(source))
    assert [(clip.start_ms, clip.end_ms) for clip in clips] == [(0, 1250), (1250, 3000)]


def test_detector_typed_subprocess_errors(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fixture")
    asset = _asset(source)
    with pytest.raises(FFmpegBinaryMissing):
        FFmpegSceneDetector(("missing-ffmpeg-for-test",)).segment(asset)
    with pytest.raises(FFmpegTimeout):
        FFmpegSceneDetector(_fake_ffmpeg(tmp_path, "import time\ntime.sleep(1)\n"), timeout_seconds=0.01).segment(asset)
    with pytest.raises(FFmpegProcessError):
        FFmpegSceneDetector(_fake_ffmpeg(tmp_path, "import sys\nsys.stderr.write('broken')\nsys.exit(4)\n")).segment(asset)
    with pytest.raises(NonVideoSource):
        FFmpegSceneDetector(_fake_ffmpeg(tmp_path, "import sys\nsys.stderr.write(\"Stream map '0:v:0' matches no streams.\")\nsys.exit(1)\n")).segment(asset)
    with pytest.raises(FFmpegMalformedOutput):
        FFmpegSceneDetector(_fake_ffmpeg(tmp_path, "print('not metadata')\n")).segment(asset)
    with pytest.raises(SourceFileMissing):
        FFmpegSceneDetector(_fake_ffmpeg(tmp_path, "print()\n")).segment(_asset(tmp_path / "missing.mp4"))


def test_real_ffmpeg_hard_cuts_import_and_segment(tmp_path: Path) -> None:
    ffmpeg = os.environ.get("CONTENT_OS_FFMPEG") or shutil.which("ffmpeg")
    ffprobe = os.environ.get("CONTENT_OS_FFPROBE") or shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("real ffmpeg/ffprobe binaries are unavailable")
    source = tmp_path / "硬切 scenes with spaces.mp4"
    subprocess.run(
        [
            ffmpeg, "-y",
            "-f", "lavfi", "-i", "color=c=red:size=160x120:rate=25:duration=0.6",
            "-f", "lavfi", "-i", "color=c=blue:size=160x120:rate=25:duration=0.6",
            "-f", "lavfi", "-i", "color=c=green:size=160x120:rate=25:duration=0.6",
            "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0,format=yuv420p",
            "-c:v", "mpeg4", str(source),
        ],
        check=True, capture_output=True,
    )
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    db = Database(tmp_path / "store.sqlite")
    try:
        importer = MediaImporter(db, tmp_path / "data", FFProbeAdapter(ffprobe))
        asset = importer.import_path(source, "fixture-rights")
        originals = list((tmp_path / "data" / "assets" / "originals").iterdir())
        clips = FFmpegSceneDetector(ffmpeg, threshold=0.1, min_clip_duration_ms=100).segment(asset)

        assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
        assert hashlib.sha256(Path(asset.source_file).read_bytes()).hexdigest() == source_hash
        assert list((tmp_path / "data" / "assets" / "originals").iterdir()) == originals
        assert clips[0].start_ms == 0 and clips[-1].end_ms == asset.duration_ms
        assert all(clip.end_ms > clip.start_ms for clip in clips)
        assert all(left.end_ms == right.start_ms for left, right in zip(clips, clips[1:]))
        assert all(clip.asset_id == asset.id and clip.asset_duration_ms == asset.duration_ms for clip in clips)
        boundaries = [clip.start_ms for clip in clips[1:]]
        assert any(abs(boundary - 600) <= 80 for boundary in boundaries)
        assert any(abs(boundary - 1200) <= 80 for boundary in boundaries)
    finally:
        db.close()
