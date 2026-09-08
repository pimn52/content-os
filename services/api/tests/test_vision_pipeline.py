from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.db import AssetRepository, ClipRepository, Database
from app.domain.models import Asset, Clip, RationalFps
from app.media.pipeline import MediaAnalysisResult
from app.media.vision_pipeline import (
    MediaVisionPipeline,
    VisionAssetIdentityMismatch,
    VisionClipSetMismatch,
    VisionKeyframeMismatch,
)
from app.providers.vision import ClipVisualAnalysis


def _fixture(tmp_path: Path):
    db = Database(tmp_path / "vision-pipeline.sqlite")
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    asset = Asset(source_file=str(source), content_hash="p" * 64, duration_ms=2_000, width=320, height=240, fps=RationalFps(numerator=25, denominator=1), authorization_reference="rights", imported_at=datetime.now(timezone.utc))
    AssetRepository(db).create(asset)
    clips = [
        Clip(asset_id=asset.id, start_ms=0, end_ms=1_000, asset_duration_ms=2_000, visual_description="old one", transcript="keep one"),
        Clip(asset_id=asset.id, start_ms=1_000, end_ms=2_000, asset_duration_ms=2_000, visual_description="old two", transcript="keep two"),
    ]
    repository = ClipRepository(db)
    for clip in clips:
        repository.create(clip)
    paths = (tmp_path / "frame-0.jpg", tmp_path / "frame-1.jpg")
    for path in paths:
        path.write_bytes(b"jpeg")
    return db, asset, clips, paths


class _Provider:
    def __init__(self, fail_at: int | None = None):
        self.paths = []
        self.fail_at = fail_at

    def analyze(self, path: str | Path) -> ClipVisualAnalysis:
        self.paths.append(Path(path))
        if self.fail_at is not None and len(self.paths) == self.fail_at:
            raise RuntimeError("provider failure")
        index = 0 if path.name.endswith("frame-0.jpg") else 1
        return ClipVisualAnalysis(f"new scene {index}", ("person",), ("desk",), "office", "working", "medium", 0.8, 0.7, 0.6, False)


def test_pipeline_provider_order_and_persistence(tmp_path: Path):
    db, asset, clips, paths = _fixture(tmp_path)
    try:
        provider = _Provider()
        result = MediaVisionPipeline(db, provider).process(asset, clips, paths)
        assert tuple(provider.paths) == paths
        assert [clip.visual_description for clip in result.clips] == ["new scene 0", "new scene 1"]
        persisted = ClipRepository(db).list_by_asset(asset.id)
        assert persisted == list(result.clips)
        assert [clip.transcript for clip in persisted] == ["keep one", "keep two"]
    finally:
        db.close()


def test_provider_failure_does_not_persist_partial_visuals(tmp_path: Path):
    db, asset, clips, paths = _fixture(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="provider failure"):
            MediaVisionPipeline(db, _Provider(fail_at=2)).process(asset, clips, paths)
        assert ClipRepository(db).list_by_asset(asset.id) == clips
    finally:
        db.close()


def test_identity_count_and_path_mismatches_are_rejected(tmp_path: Path):
    db, asset, clips, paths = _fixture(tmp_path)
    try:
        pipeline = MediaVisionPipeline(db, _Provider())
        with pytest.raises(VisionClipSetMismatch):
            pipeline.process(asset, clips[:1], paths[:1])
        with pytest.raises(VisionKeyframeMismatch):
            pipeline.process(asset, clips, paths[:1])
        with pytest.raises(VisionKeyframeMismatch):
            pipeline.process(asset, clips, (paths[0], tmp_path / "missing.jpg"))
        with pytest.raises(VisionAssetIdentityMismatch):
            pipeline.process(asset.model_copy(update={"content_hash": "q" * 64}), clips, paths)
    finally:
        db.close()


def test_media_analysis_result_input_is_repeatable_and_stable(tmp_path: Path):
    db, asset, clips, paths = _fixture(tmp_path)
    try:
        provider = _Provider()
        analysis = MediaAnalysisResult(asset, tuple(clips), None, tuple(paths))
        pipeline = MediaVisionPipeline(db, provider)
        first = pipeline.process(analysis)
        second = pipeline.process(analysis)
        assert first.keyframe_paths == second.keyframe_paths == paths
        assert [clip.id for clip in first.clips] == [clip.id for clip in second.clips]
        assert [clip.visual_description for clip in first.clips] == [clip.visual_description for clip in second.clips]
        assert len(provider.paths) == 4
    finally:
        db.close()
