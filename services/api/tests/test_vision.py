from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.db import AssetRepository, ClipRepository, Database
from app.domain.models import Asset, Clip, RationalFps
from app.media.vision import ClipVisualMetadataPersistence, VisualMetadata, VisualMetadataMismatch, map_visual_metadata
from app.providers.vision import ClipVisualAnalysis


def _setup(tmp_path: Path):
    db = Database(tmp_path / "vision.sqlite")
    source = tmp_path / "asset.mp4"
    source.write_bytes(b"asset")
    asset = Asset(source_file=str(source), content_hash="v" * 64, duration_ms=2000, width=10, height=10, fps=RationalFps(numerator=25, denominator=1), authorization_reference="rights", imported_at=datetime.now(timezone.utc))
    AssetRepository(db).create(asset)
    clips = [Clip(asset_id=asset.id, start_ms=0, end_ms=1000, asset_duration_ms=2000, transcript="keep", used_count=3), Clip(asset_id=asset.id, start_ms=1000, end_ms=2000, asset_duration_ms=2000, transcript="keep2")]
    for clip in clips:
        ClipRepository(db).create(clip)
    return db, asset, clips


def _results(asset, clips, root: Path, suffix=""):
    return [VisualMetadata(clip.id, asset.id, visual_description=f"scene {index}{suffix}", people=("person",), objects=("desk",), quality_score=0.8, talking_candidate=True, keyframe_path=str(root / f"frame-{index}.jpg")) for index, clip in enumerate(clips)]


def test_visual_mapping_replaces_visual_fields_and_preserves_non_visual_fields(tmp_path):
    db, asset, clips = _setup(tmp_path)
    try:
        for index in range(2):
            (tmp_path / f"frame-{index}.jpg").write_bytes(b"jpeg")
        service = ClipVisualMetadataPersistence(db)
        paths = [str(tmp_path / "frame-0.jpg"), str(tmp_path / "frame-1.jpg")]
        first = service.apply(asset.id, _results(asset, clips, tmp_path), paths)
        second = service.apply(asset.id, _results(asset, clips, tmp_path, "-again"), paths)
        assert [clip.visual_description for clip in first] == ["scene 0", "scene 1"]
        assert [clip.visual_description for clip in second] == ["scene 0-again", "scene 1-again"]
        assert [clip.transcript for clip in second] == ["keep", "keep2"]
        assert [clip.used_count for clip in second] == [3, 0]
    finally:
        db.close()


def test_mismatch_and_update_failure_roll_back(tmp_path, monkeypatch):
    db, asset, clips = _setup(tmp_path)
    try:
        for index in range(2):
            (tmp_path / f"frame-{index}.jpg").write_bytes(b"jpeg")
        service = ClipVisualMetadataPersistence(db)
        with pytest.raises(VisualMetadataMismatch):
            service.apply(asset.id, _results(asset, clips[:1], tmp_path), [str(tmp_path / "frame-0.jpg")])
        original = service.clips.update
        calls = 0
        def fail_second(clip):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("injected")
            return original(clip)
        monkeypatch.setattr(service.clips, "update", fail_second)
        paths = [str(tmp_path / "frame-0.jpg"), str(tmp_path / "frame-1.jpg")]
        with pytest.raises(RuntimeError):
            service.apply(asset.id, _results(asset, clips, tmp_path), paths)
        assert ClipRepository(db).list_by_asset(asset.id) == clips
    finally:
        db.close()


def test_pure_mapping_rejects_identity_mismatch():
    clip = Clip(asset_id=__import__("uuid").uuid4(), start_ms=0, end_ms=10, asset_duration_ms=10)
    with pytest.raises(VisualMetadataMismatch):
        map_visual_metadata(clip, {"clip_id": clip.id, "asset_id": __import__("uuid").uuid4()})


def test_provider_result_wrapper_and_keyframe_validation(tmp_path):
    db, asset, clips = _setup(tmp_path)
    try:
        frame = tmp_path / "provider.jpg"
        frame.write_bytes(b"jpeg")
        analysis = ClipVisualAnalysis("desk scene", ("person",), ("desk",), "office", "working", "medium", 0.9, 0.8, 0.7, True)
        result = VisualMetadata.for_clip(clips[0], frame, analysis)
        assert result.clip_id == clips[0].id and result.asset_id == asset.id
        with pytest.raises(VisualMetadataMismatch):
            ClipVisualMetadataPersistence(db).apply(asset.id, [result], [frame])
        with pytest.raises(FileNotFoundError):
            ClipVisualMetadataPersistence(db).apply(asset.id, [result, result], [frame, tmp_path / "missing.jpg"])
    finally:
        db.close()


@pytest.mark.parametrize("bad", [
    {"quality_score": float("nan")},
    {"people": ["person"] * 31},
    {"visual_description": "x" * 5_001},
])
def test_invalid_visual_values_do_not_reach_database(tmp_path, bad):
    db, asset, clips = _setup(tmp_path)
    try:
        frame = tmp_path / "frame.jpg"
        frame.write_bytes(b"jpeg")
        result = VisualMetadata.for_clip(clips[0], frame, ClipVisualAnalysis(None, (), (), None, None, None, None, None, None, False))
        values = {"clip_id": result.clip_id, "asset_id": result.asset_id, "keyframe_path": str(frame), **bad}
        with pytest.raises((ValueError, VisualMetadataMismatch)):
            ClipVisualMetadataPersistence(db).apply(asset.id, [values, VisualMetadata.for_clip(clips[1], frame, ClipVisualAnalysis(None, (), (), None, None, None, None, None, None, False))], [frame, frame])
        assert ClipRepository(db).list_by_asset(asset.id) == clips
    finally:
        db.close()
