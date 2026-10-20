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


def test_sampled_source_geometry_is_persisted_but_cannot_authorize_interval_crop(tmp_path):
    from dataclasses import replace
    from app.assembly.edit_plan import presentation_for_style,_default_edit_plan
    from app.assembly.video_spec import _talking_visual_treatment
    from app.domain.models import CandidateAsset,CostCategory,Project,UsageCost,VisualStyleTokens,VisualIntent
    from test_video_spec_assembly import _scene
    db,asset,clips,paths=_fixture(tmp_path)
    asset=AssetRepository(db).update(asset.model_copy(update={'width':1280,'height':720}))
    class Geometry(_Provider):
        def analyze(self,path):
            return replace(super().analyze(path),comparison_layout='side_by_side_equal_halves',subtitle_regions=((.3,.88,.4,.08),))
    try:
        MediaVisionPipeline(db,Geometry()).process(asset,clips,paths)
        stored=AssetRepository(db).get(asset.id)
        facts=stored.metadata['visual_analysis'][str(clips[0].id)]
        assert facts['source_content_hash']==asset.content_hash and facts['clip_interval']==dict(start_ms=0,end_ms=1000)
        assert 'covered_start_ms' not in facts and 'covered_end_ms' not in facts
        assert facts['coverage']=='sampled_keyframe'
        project=Project(title='Geometry',topic='Geometry',ip_profile_id=__import__('uuid').uuid4(),fps=asset.fps,created_at=asset.imported_at)
        scene=_scene(project,0).model_copy(update={'visual_intent':VisualIntent(subject='bitrate comparison',action='compare')})
        candidate=CandidateAsset(scene_plan_id=scene.id,source_kind=asset.source_kind,asset_id=asset.id,clip_id=clips[0].id,
            match_score=1,why=['synthetic source'],estimated_cost=UsageCost(category=CostCategory.USER_ASSET,amount=None))
        entry=_default_edit_plan(project,[scene],{scene.id:candidate}).scenes[0]
        with pytest.raises(ValueError,match='full_interval_clearance_required'):
            presentation_for_style(entry,VisualStyleTokens(),stored,scene_plan=scene,clip=clips[0])
        assert ClipRepository(db).get(clips[0].id).transcript=='keep one'
        assert 'source_bottom_crop_ratio' not in entry.model_dump(mode='json')  # Historical default serialization stays stable.
        changed=stored.model_copy(update={'content_hash':'b'*64})
        with pytest.raises(ValueError,match='geometry_unknown'):
            presentation_for_style(entry,VisualStyleTokens(name='content-os-portrait-comparison'),changed,scene_plan=scene,clip=clips[0])
    finally:db.close()


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


def test_explicit_clip_refresh_preserves_unselected_clip_and_geometry(tmp_path):
    db, asset, clips, paths = _fixture(tmp_path)
    try:
        full = MediaVisionPipeline(db, _Provider()).process(asset, clips, paths)
        untouched = ClipRepository(db).get(clips[0].id)
        old_facts = full.asset.metadata['visual_analysis'][str(clips[0].id)]
        provider = _Provider()
        refreshed = MediaVisionPipeline(db, provider).process(full.asset, clips[1:], paths[1:],
            selected_clip_ids=[clips[1].id])
        assert provider.paths == [paths[1]]
        assert [clip.id for clip in refreshed.clips] == [clips[1].id]
        assert ClipRepository(db).get(clips[0].id) == untouched
        assert refreshed.asset.metadata['visual_analysis'][str(clips[0].id)] == old_facts
        from uuid import uuid4
        from app.media.vision import VisualMetadataMismatch
        for scope in ([], [clips[1].id, clips[1].id], [uuid4()]):
            with pytest.raises(VisualMetadataMismatch):
                MediaVisionPipeline(db, provider).process(full.asset, clips[1:], paths[1:], selected_clip_ids=scope)
        assert provider.paths == [paths[1]]
    finally:
        db.close()


def test_scoped_provider_failure_preserves_all_source_facts(tmp_path):
    db, asset, clips, paths = _fixture(tmp_path)
    try:
        initial = MediaVisionPipeline(db, _Provider()).process(asset, clips, paths)
        before = ClipRepository(db).list_by_asset(asset.id)
        with pytest.raises(RuntimeError, match='provider failure'):
            MediaVisionPipeline(db, _Provider(fail_at=1)).process(initial.asset, before[1:], paths[1:],
                selected_clip_ids=[clips[1].id])
        assert ClipRepository(db).list_by_asset(asset.id) == before
        assert AssetRepository(db).get(asset.id) == initial.asset
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
