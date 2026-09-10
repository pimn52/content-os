from datetime import datetime, timezone
from pathlib import Path
import sqlite3

import pytest

from app.db import AssetRepository, ClipRepository, Database
from app.domain.models import Asset, Clip, ProjectFormat, RationalFps
from app.search import ClipIndexRepository, ClipSearchService, ClipTextSearchService, IndexError, compose_clip_text


def _setup(tmp_path: Path):
    db = Database(tmp_path / "search.sqlite")
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    asset = Asset(
        source_file=str(source), content_hash="a" * 64, duration_ms=2_000,
        width=320, height=240, fps=RationalFps(numerator=25, denominator=1),
        authorization_reference="rights", imported_at=datetime.now(timezone.utc),
    )
    AssetRepository(db).create(asset)
    clips = [
        Clip(asset_id=asset.id, start_ms=0, end_ms=1_000, asset_duration_ms=2_000,
             transcript="software tutorial", visual_description="person at desk",
             objects=["computer"], orientation=ProjectFormat.VERTICAL, talking_candidate=True),
        Clip(asset_id=asset.id, start_ms=1_000, end_ms=2_000, asset_duration_ms=2_000,
             transcript="cooking recipe", visual_description="kitchen counter",
             objects=["pan"], orientation=ProjectFormat.HORIZONTAL, talking_candidate=False),
    ]
    for clip in clips:
        ClipRepository(db).create(clip)
    return db, asset, clips


def test_index_round_trip_text_and_filters(tmp_path: Path):
    db, asset, clips = _setup(tmp_path)
    try:
        service = ClipSearchService(db)
        assert "transcript: software tutorial" in compose_clip_text(clips[0])
        service.index_clips(((clips[0], (1.0, 0.0)), (clips[1], (0.0, 1.0))))
        assert ClipIndexRepository(db).get(clips[0].id).embedding == (1.0, 0.0)
        assert ClipRepository(db).get(clips[0].id).embedding_ref == f"sqlite:clip_search_index:{clips[0].id}"
        hits = service.search((1.0, 0.0), top_k=2, asset_id=asset.id, orientation=ProjectFormat.VERTICAL, talking_candidate=True)
        assert [(hit.clip.id, hit.score) for hit in hits] == [(clips[0].id, 1.0)]
        assert service.search((0.0, 1.0), top_k=1)[0].clip.id == clips[1].id
    finally:
        db.close()


def test_search_stable_tie_order_and_update_delete(tmp_path: Path):
    db, _, clips = _setup(tmp_path)
    try:
        repo = ClipIndexRepository(db)
        repo.upsert_many(((clips[1], (1.0, 0.0)), (clips[0], (1.0, 0.0))))
        service = ClipSearchService(db, repo)
        hits = service.search((1.0, 0.0), top_k=10)
        assert [hit.clip.id for hit in hits] == sorted((clip.id for clip in clips), key=str)
        updated = repo.upsert(clips[0], (0.0, 1.0))
        assert updated.embedding == (0.0, 1.0)
        assert repo.delete(clips[1].id) is True
        assert repo.get(clips[1].id) is None
        assert ClipRepository(db).get(clips[1].id).embedding_ref is None
    finally:
        db.close()


def test_vectors_validate_dimensions_finite_and_atomic_batch(tmp_path: Path):
    db, asset, clips = _setup(tmp_path)
    try:
        repo = ClipIndexRepository(db)
        with pytest.raises(IndexError):
            repo.upsert(clips[0], (1.0, float("nan")))
        with pytest.raises(IndexError):
            repo.upsert_many(((clips[0], (1.0, 0.0)), (clips[1], (1.0,))))
        with pytest.raises(IndexError):
            repo.upsert(clips[0], (True, 0.0))
        missing = Clip(asset_id=asset.id, start_ms=0, end_ms=100, asset_duration_ms=2_000)
        with pytest.raises(sqlite3.IntegrityError):
            repo.upsert_many(((clips[0], (1.0, 0.0)), (missing, (0.0, 1.0))))
        assert repo.list() == []
        repo.upsert(clips[0], (1.0, 0.0))
        with pytest.raises(IndexError, match="local index"):
            repo.upsert(clips[1], (1.0,))
        with pytest.raises(IndexError):
            ClipSearchService(db, repo).search((1.0,), top_k=1)
        with pytest.raises(IndexError):
            ClipSearchService(db, repo).search((1.0, 0.0), top_k=0)
    finally:
        db.close()


def test_lexical_search_is_explicit_and_does_not_write_embedding_index(tmp_path: Path):
    db, asset, clips = _setup(tmp_path)
    try:
        service = ClipTextSearchService(db)
        hits = service.search("person computer", top_k=2, asset_id=asset.id, orientation="vertical")

        assert [hit.clip.id for hit in hits] == [clips[0].id]
        assert hits[0].score_basis == "lexical_overlap"
        assert ClipIndexRepository(db).list() == []
    finally:
        db.close()
