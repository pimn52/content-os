from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.db import AssetRepository, ClipRepository, Database
from app.domain.models import Asset, Clip, RationalFps
from app.providers.embedding import EmbeddingBatch
from app.search import ClipEmbeddingIndexer, EmbeddingCountMismatch


def _clips(db: Database, root: Path) -> list[Clip]:
    source = root / "source.mp4"
    source.write_bytes(b"source")
    asset = Asset(
        source_file=str(source), content_hash="c" * 64, duration_ms=2_000,
        width=320, height=240, fps=RationalFps(numerator=25, denominator=1),
        authorization_reference="rights", imported_at=datetime.now(timezone.utc),
    )
    AssetRepository(db).create(asset)
    values = [
        Clip(asset_id=asset.id, start_ms=0, end_ms=1_000, asset_duration_ms=2_000, transcript="computer software"),
        Clip(asset_id=asset.id, start_ms=1_000, end_ms=2_000, asset_duration_ms=2_000, transcript="outdoor road"),
    ]
    for clip in values:
        ClipRepository(db).create(clip)
    return values


def test_index_and_natural_language_search_call_provider_outside_transaction(tmp_path: Path) -> None:
    db = Database(tmp_path / "pipeline.sqlite")
    try:
        clips = _clips(db, tmp_path)
        calls = []

        class Provider:
            def embed(self, texts):
                assert not db.connection.in_transaction
                calls.append(tuple(texts))
                if len(texts) == 2:
                    return EmbeddingBatch(((1.0, 0.0), (0.0, 1.0)))
                return EmbeddingBatch(((1.0, 0.0),))

        indexer = ClipEmbeddingIndexer(db, Provider())
        indexed = indexer.index_clips(clips)
        hits = indexer.search("本人坐在电脑前操作软件", top_k=1)
        assert [item.clip_id for item in indexed] == [clip.id for clip in clips]
        assert hits[0].clip.id == clips[0].id
        assert calls[0][0].startswith("transcript: computer software")
        assert calls[1] == ("本人坐在电脑前操作软件",)
    finally:
        db.close()


def test_provider_failure_or_count_mismatch_does_not_partially_index(tmp_path: Path) -> None:
    db = Database(tmp_path / "no-partial.sqlite")
    try:
        clips = _clips(db, tmp_path)

        class FailingProvider:
            def embed(self, texts):
                raise RuntimeError("provider failed")

        with pytest.raises(RuntimeError):
            ClipEmbeddingIndexer(db, FailingProvider()).index_clips(clips)
        assert db.connection.execute("SELECT COUNT(*) FROM clip_search_index").fetchone()[0] == 0

        class WrongCountProvider:
            def embed(self, texts):
                return EmbeddingBatch(((1.0, 0.0),))

        with pytest.raises(EmbeddingCountMismatch):
            ClipEmbeddingIndexer(db, WrongCountProvider()).index_clips(clips)
        assert db.connection.execute("SELECT COUNT(*) FROM clip_search_index").fetchone()[0] == 0
    finally:
        db.close()
