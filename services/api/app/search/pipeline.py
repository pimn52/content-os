"""Provider-neutral orchestration for Clip indexing and semantic retrieval."""
from __future__ import annotations

from typing import Sequence
from uuid import UUID

from app.db import ClipRepository, Database
from app.domain.models import Clip, ProjectFormat
from app.providers.embedding import EmbeddingBatch, EmbeddingProvider

from .index import ClipIndex, ClipSearchHit, ClipSearchService, IndexError, compose_clip_text


class EmbeddingIndexError(RuntimeError):
    pass


class ClipsNotFoundForIndex(EmbeddingIndexError):
    pass


class EmbeddingCountMismatch(EmbeddingIndexError):
    pass


class ClipEmbeddingIndexer:
    """Embed outside SQLite write transactions, then persist one atomic batch."""

    def __init__(self, db: Database, provider: EmbeddingProvider, search: ClipSearchService | None = None) -> None:
        self.db = db
        self.provider = provider
        self.clips = ClipRepository(db)
        self.search_service = search or ClipSearchService(db)

    def index_asset(self, asset_id: UUID) -> list[ClipIndex]:
        return self.index_clips(self.clips.list_by_asset(asset_id))

    def index_clips(self, clips: Sequence[Clip]) -> list[ClipIndex]:
        if not clips:
            raise ClipsNotFoundForIndex("no Clips are available for indexing")
        texts = tuple(compose_clip_text(clip) for clip in clips)
        if self.db.connection.in_transaction:
            raise EmbeddingIndexError("embedding provider cannot run inside a database transaction")
        batch = self.provider.embed(texts)
        if not isinstance(batch, EmbeddingBatch) or len(batch.vectors) != len(clips):
            raise EmbeddingCountMismatch("embedding result count does not match Clip count")
        return self.search_service.index_clips(zip(clips, batch.vectors))

    def search(
        self,
        query: str,
        *,
        top_k: int = 10,
        asset_id: UUID | None = None,
        orientation: ProjectFormat | str | None = None,
        talking_candidate: bool | None = None,
    ) -> list[ClipSearchHit]:
        if not isinstance(query, str) or not query.strip() or len(query) > 10_000:
            raise IndexError("search query must be non-empty and at most 10000 characters")
        if self.db.connection.in_transaction:
            raise EmbeddingIndexError("embedding provider cannot run inside a database transaction")
        batch = self.provider.embed((query.strip(),))
        if not isinstance(batch, EmbeddingBatch) or len(batch.vectors) != 1:
            raise EmbeddingCountMismatch("query embedding result count must be one")
        return self.search_service.search(
            batch.vectors[0],
            top_k=top_k,
            asset_id=asset_id,
            orientation=orientation,
            talking_candidate=talking_candidate,
        )
