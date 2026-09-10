"""Local Clip indexing and vector search."""

from .index import (
    ClipIndex,
    ClipIndexRepository,
    ClipSearchHit,
    ClipSearchService,
    ClipTextSearchService,
    IndexError,
    compose_clip_text,
)
from .pipeline import ClipEmbeddingIndexer, ClipsNotFoundForIndex, EmbeddingCountMismatch, EmbeddingIndexError

__all__ = [
    "ClipIndex",
    "ClipIndexRepository",
    "ClipEmbeddingIndexer",
    "ClipSearchHit",
    "ClipSearchService",
    "ClipTextSearchService",
    "IndexError",
    "ClipsNotFoundForIndex",
    "EmbeddingCountMismatch",
    "EmbeddingIndexError",
    "compose_clip_text",
]
