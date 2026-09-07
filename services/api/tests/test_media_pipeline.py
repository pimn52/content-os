from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest

from app.db import AssetRepository, ClipRepository, Database
from app.domain.models import Asset, Clip, RationalFps
from app.media.extraction import AudioExtraction, KeyframeExtraction, MediaExtractor
from app.media.ffprobe import FFProbeAdapter
from app.media.importer import MediaImporter
from app.media.pipeline import MediaAnalysisPipeline
from app.media.segmentation import FFmpegSceneDetector


def _persisted_asset(db: Database, root: Path, name: str, *, duration_ms: int = 2_000, has_audio: bool = True) -> Asset:
    source = root / name
    source.write_bytes(f"source:{name}".encode())
    asset = Asset(
        source_file=str(source),
        content_hash=hashlib.sha256(source.read_bytes()).hexdigest(),
        duration_ms=duration_ms,
        width=640,
        height=360,
        fps=RationalFps(numerator=25, denominator=1),
        has_audio=has_audio,
        authorization_reference="fixture-rights",
        imported_at=datetime.now(timezone.utc),
    )
    AssetRepository(db).create(asset)
    return asset


class _StaticSegmenter:
    def __init__(self, intervals: list[tuple[int, int]]) -> None:
        self.intervals = intervals

    def segment(self, asset: Asset) -> list[Clip]:
        return [
            Clip(asset_id=asset.id, start_ms=start, end_ms=end, asset_duration_ms=asset.duration_ms)
            for start, end in self.intervals
        ]


class _WritingExtractor:
    def __init__(self, root: Path) -> None:
        self.root = root

    def extract_audio(self, asset: Asset) -> AudioExtraction:
        path = self.root / "audio" / f"{asset.content_hash}.wav"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"RIFF test audio")
        return AudioExtraction(path)

    def extract_keyframe(self, asset: Asset, clip: Clip) -> KeyframeExtraction:
        path = self.root / "keyframes" / str(clip.id) / "frame.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"jpeg")
        return KeyframeExtraction(path, (clip.start_ms + clip.end_ms) / 2)


def test_extraction_failure_leaves_previous_clip_set_unchanged(tmp_path: Path) -> None:
    db = Database(tmp_path / "store.sqlite")
    try:
        asset = _persisted_asset(db, tmp_path, "asset.mp4")
        previous = Clip(id=uuid4(), asset_id=asset.id, start_ms=0, end_ms=asset.duration_ms, asset_duration_ms=asset.duration_ms)
        ClipRepository(db).create(previous)

        class FailingExtractor(_WritingExtractor):
            def extract_keyframe(self, asset: Asset, clip: Clip) -> KeyframeExtraction:
                raise RuntimeError("controlled extraction failure")

        pipeline = MediaAnalysisPipeline(db, _StaticSegmenter([(0, 1_000), (1_000, 2_000)]), FailingExtractor(tmp_path / "cache"))
        with pytest.raises(RuntimeError, match="controlled extraction failure"):
            pipeline.process(asset)
        assert ClipRepository(db).list_by_asset(asset.id) == [previous]
    finally:
        db.close()


def test_repeat_processing_is_stable_and_duplicate_free(tmp_path: Path) -> None:
    db = Database(tmp_path / "store.sqlite")
    try:
        asset = _persisted_asset(db, tmp_path, "asset.mp4")
        pipeline = MediaAnalysisPipeline(db, _StaticSegmenter([(0, 1_000), (1_000, 2_000)]), _WritingExtractor(tmp_path / "cache"))
        first = pipeline.process(asset)
        second = pipeline.process(asset)
        assert [clip.id for clip in first.clips] == [clip.id for clip in second.clips]
        assert first.audio_path == second.audio_path
        assert first.keyframe_paths == second.keyframe_paths
        assert ClipRepository(db).list_by_asset(asset.id) == list(first.clips)
        assert len(first.keyframe_paths) == len(set(first.keyframe_paths)) == 2
    finally:
        db.close()


def test_replacing_stale_target_clips_does_not_touch_another_asset(tmp_path: Path) -> None:
    db = Database(tmp_path / "store.sqlite")
    try:
        target = _persisted_asset(db, tmp_path, "target.mp4")
        other = _persisted_asset(db, tmp_path, "other.mp4")
        repository = ClipRepository(db)
        stale = Clip(id=uuid4(), asset_id=target.id, start_ms=0, end_ms=target.duration_ms, asset_duration_ms=target.duration_ms)
        untouched = Clip(id=uuid4(), asset_id=other.id, start_ms=0, end_ms=other.duration_ms, asset_duration_ms=other.duration_ms)
        repository.create(stale)
        repository.create(untouched)

        result = MediaAnalysisPipeline(db, _StaticSegmenter([(0, 1_000), (1_000, 2_000)]), _WritingExtractor(tmp_path / "cache")).process(target)
        assert repository.list_by_asset(target.id) == list(result.clips)
        assert repository.list_by_asset(other.id) == [untouched]
    finally:
        db.close()


def test_concurrent_pipeline_runs_keep_one_deterministic_clip_set(tmp_path: Path) -> None:
    path = tmp_path / "store.sqlite"
    data_root = tmp_path / "cache"
    seed = Database(path)
    asset = _persisted_asset(seed, tmp_path, "asset.mp4")
    seed.close()
    barrier = Barrier(2)

    def run(_: int) -> list[str]:
        db = Database(path)
        try:
            pipeline = MediaAnalysisPipeline(db, _StaticSegmenter([(0, 1_000), (1_000, 2_000)]), _WritingExtractor(data_root))
            barrier.wait()
            return [str(clip.id) for clip in pipeline.process(asset).clips]
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        identities = list(pool.map(run, range(2)))
    db = Database(path)
    try:
        persisted = ClipRepository(db).list_by_asset(asset.id)
        assert identities[0] == identities[1] == [str(clip.id) for clip in persisted]
        assert len(persisted) == 2
    finally:
        db.close()


def test_real_import_segment_extract_and_persist_slice(tmp_path: Path) -> None:
    ffmpeg = os.environ.get("CONTENT_OS_FFMPEG") or shutil.which("ffmpeg")
    ffprobe = os.environ.get("CONTENT_OS_FFPROBE") or shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("real ffmpeg/ffprobe binaries are unavailable")
    source = tmp_path / "hard cuts audio 中文.mp4"
    subprocess.run(
        [
            ffmpeg, "-y",
            "-f", "lavfi", "-i", "color=c=red:size=160x120:rate=25:duration=0.6",
            "-f", "lavfi", "-i", "color=c=blue:size=160x120:rate=25:duration=0.6",
            "-f", "lavfi", "-i", "color=c=green:size=160x120:rate=25:duration=0.6",
            "-f", "lavfi", "-i", "sine=frequency=700:sample_rate=48000:duration=1.8",
            "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0,format=yuv420p[v]",
            "-map", "[v]", "-map", "3:a", "-shortest", "-c:v", "mpeg4", "-c:a", "aac", str(source),
        ],
        check=True, capture_output=True,
    )
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    db = Database(tmp_path / "store.sqlite")
    try:
        data_root = tmp_path / "data"
        asset = MediaImporter(db, data_root, FFProbeAdapter(ffprobe)).import_path(source, "fixture-rights")
        pipeline = MediaAnalysisPipeline(
            db,
            FFmpegSceneDetector(ffmpeg, threshold=0.1, min_clip_duration_ms=100),
            MediaExtractor(data_root, command=ffmpeg),
        )
        first = pipeline.process(asset)
        second = pipeline.process(asset)
        persisted = ClipRepository(db).list_by_asset(asset.id)

        assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
        assert hashlib.sha256(Path(asset.source_file).read_bytes()).hexdigest() == source_hash
        assert persisted == list(first.clips) == list(second.clips)
        assert [clip.id for clip in first.clips] == [clip.id for clip in second.clips]
        assert first.audio_path is not None and first.audio_path.is_file() and first.audio_path.stat().st_size > 0
        assert len(first.keyframe_paths) == len(first.clips)
        assert all(path.is_file() and path.stat().st_size > 0 for path in first.keyframe_paths)
        assert first.keyframe_paths == second.keyframe_paths
        assert first.clips[0].start_ms == 0 and first.clips[-1].end_ms == asset.duration_ms
        assert all(left.end_ms == right.start_ms for left, right in zip(first.clips, first.clips[1:]))
        assert any(abs(clip.start_ms - 600) <= 80 for clip in first.clips[1:])
        assert any(abs(clip.start_ms - 1200) <= 80 for clip in first.clips[1:])
    finally:
        db.close()
