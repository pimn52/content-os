"""Local Clip indexing and vector search."""

from .index import (
    ClipIndex,
    ClipIndexRepository,
    ClipSearchHit,
    ClipSearchService,
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
    "IndexError",
    "ClipsNotFoundForIndex",
    "EmbeddingCountMismatch",
    "EmbeddingIndexError",
    "compose_clip_text",
]
