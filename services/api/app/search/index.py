"""Small, dependency-free Clip index with deterministic cosine search.

Embeddings are deliberately supplied by the caller.  This module only owns
their local persistence and retrieval; no provider key or network operation is
needed to build an index.
"""
from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
import re
from typing import Iterable, Literal, Sequence
from uuid import UUID

from app.db import ClipRepository, Database
from app.domain.models import Clip, ProjectFormat


class IndexError(ValueError):
    """Base error for invalid local index operations."""


def _vector(value: Sequence[float], *, name: str = "embedding") -> tuple[float, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise IndexError(f"{name} must be a non-empty numeric sequence")
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in value):
        raise IndexError(f"{name} must contain only numeric values")
    result = tuple(float(item) for item in value)
    if not result or any(not math.isfinite(item) for item in result):
        raise IndexError(f"{name} must contain finite values")
    return result


def compose_clip_text(clip: Clip) -> str:
    """Build stable searchable text from transcript, visual and Clip metadata."""
    fields: tuple[tuple[str, object], ...] = (
        ("transcript", clip.transcript),
        ("visual_description", clip.visual_description),
        ("people", clip.people),
        ("objects", clip.objects),
        ("location", clip.location),
        ("action", clip.action),
        ("shot_type", clip.shot_type),
        ("orientation", clip.orientation.value if clip.orientation else None),
        ("motion_level", clip.motion_level),
        ("quality_score", clip.quality_score),
        ("speech_quality", clip.speech_quality),
        ("face_visibility", clip.face_visibility),
        ("mouth_visibility", clip.mouth_visibility),
        ("talking_candidate", clip.talking_candidate),
    )
    parts: list[str] = []
    for name, value in fields:
        if value is None or value == [] or value == ():
            continue
        if isinstance(value, (list, tuple)):
            rendered = " ".join(str(item) for item in value)
        else:
            rendered = str(value)
        if rendered.strip():
            parts.append(f"{name}: {rendered}")
    return "\n".join(parts)


@dataclass(frozen=True)
class ClipIndex:
    clip_id: UUID
    asset_id: UUID
    searchable_text: str
    embedding: tuple[float, ...]


@dataclass(frozen=True)
class ClipSearchHit:
    clip: Clip
    score: float
    score_basis: Literal["embedding_similarity", "lexical_overlap"] = "embedding_similarity"


class ClipIndexRepository:
    """Persistence operations for content-addressed Clip vectors."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, clip_id: UUID) -> ClipIndex | None:
        row = self.db.connection.execute("SELECT * FROM clip_search_index WHERE clip_id = ?", (str(clip_id),)).fetchone()
        return None if row is None else self._row(row)

    def list(self, asset_id: UUID | None = None) -> list[ClipIndex]:
        if asset_id is None:
            rows = self.db.connection.execute("SELECT * FROM clip_search_index ORDER BY clip_id").fetchall()
        else:
            rows = self.db.connection.execute("SELECT * FROM clip_search_index WHERE asset_id = ? ORDER BY clip_id", (str(asset_id),)).fetchall()
        return [self._row(row) for row in rows]

    def upsert(self, clip: Clip, embedding: Sequence[float]) -> ClipIndex:
        return self.upsert_many(((clip, embedding),))[0]

    def upsert_many(self, values: Iterable[tuple[Clip, Sequence[float]]]) -> list[ClipIndex]:
        prepared = [(clip, _vector(vector)) for clip, vector in values]
        if not prepared:
            return []
        dimensions = {len(vector) for _, vector in prepared}
        if len(dimensions) != 1:
            raise IndexError("all embeddings in one batch must have the same dimension")
        connection = self.db.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            dimension = next(iter(dimensions))
            existing_dimension = connection.execute(
                "SELECT embedding_dim FROM clip_search_metadata WHERE singleton = 1"
            ).fetchone()
            if existing_dimension is not None and int(existing_dimension["embedding_dim"]) != dimension:
                raise IndexError("embedding dimension does not match the local index")
            connection.execute(
                "INSERT OR IGNORE INTO clip_search_metadata(singleton, embedding_dim) VALUES (1, ?)",
                (dimension,),
            )
            for clip, vector in prepared:
                stored = ClipRepository(self.db).get(clip.id)
                if stored is None or stored.asset_id != clip.asset_id:
                    raise sqlite3.IntegrityError("Clip does not exist for indexing")
                candidate_at_current_ref = Clip.model_validate({
                    **clip.model_dump(mode="python"), "embedding_ref": stored.embedding_ref,
                })
                if stored != candidate_at_current_ref:
                    raise IndexError("Clip changed before it could be indexed")
                connection.execute(
                    """INSERT INTO clip_search_index
                       (clip_id, asset_id, orientation, talking_candidate, searchable_text, embedding, embedding_dim)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(clip_id) DO UPDATE SET
                         asset_id=excluded.asset_id, orientation=excluded.orientation,
                         talking_candidate=excluded.talking_candidate, searchable_text=excluded.searchable_text,
                         embedding=excluded.embedding, embedding_dim=excluded.embedding_dim""",
                    (str(clip.id), str(clip.asset_id), None if clip.orientation is None else clip.orientation.value,
                     int(stored.talking_candidate), compose_clip_text(stored), json.dumps(vector, separators=(",", ":")), len(vector)),
                )
                updated = Clip.model_validate({
                    **stored.model_dump(mode="python"),
                    "embedding_ref": f"sqlite:clip_search_index:{clip.id}",
                })
                ClipRepository(self.db).update(updated)
            result = [self.get(clip.id) for clip, _ in prepared]
            connection.commit()
            return [item for item in result if item is not None]
        except BaseException:
            connection.rollback()
            raise

    def delete(self, clip_id: UUID) -> bool:
        connection = self.db.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = connection.execute("DELETE FROM clip_search_index WHERE clip_id = ?", (str(clip_id),))
            clip = ClipRepository(self.db).get(clip_id)
            if cursor.rowcount == 1 and clip is not None and clip.embedding_ref == f"sqlite:clip_search_index:{clip_id}":
                ClipRepository(self.db).update(Clip.model_validate({
                    **clip.model_dump(mode="python"), "embedding_ref": None,
                }))
            connection.commit()
            return cursor.rowcount == 1
        except BaseException:
            connection.rollback()
            raise

    @staticmethod
    def _row(row: sqlite3.Row) -> ClipIndex:
        return ClipIndex(UUID(row["clip_id"]), UUID(row["asset_id"]), row["searchable_text"], _vector(json.loads(row["embedding"])))


class ClipSearchService:
    def __init__(self, db: Database, repository: ClipIndexRepository | None = None) -> None:
        self.db = db
        self.index = repository or ClipIndexRepository(db)

    def index_clip(self, clip: Clip, embedding: Sequence[float]) -> ClipIndex:
        return self.index.upsert(clip, embedding)

    def index_clips(self, values: Iterable[tuple[Clip, Sequence[float]]]) -> list[ClipIndex]:
        return self.index.upsert_many(values)

    def search(
        self,
        embedding: Sequence[float],
        *,
        top_k: int = 10,
        asset_id: UUID | None = None,
        orientation: ProjectFormat | str | None = None,
        talking_candidate: bool | None = None,
    ) -> list[ClipSearchHit]:
        if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 1_000:
            raise IndexError("top_k must be an integer from 1 to 1000")
        query = _vector(embedding, name="query embedding")
        dimension = self.db.connection.execute(
            "SELECT embedding_dim FROM clip_search_metadata WHERE singleton = 1"
        ).fetchone()
        if dimension is not None and int(dimension["embedding_dim"]) != len(query):
            raise IndexError("query embedding dimension does not match indexed vector")
        clauses: list[str] = []
        params: list[object] = []
        if asset_id is not None:
            clauses.append("asset_id = ?")
            params.append(str(asset_id))
        if orientation is not None:
            value = orientation.value if isinstance(orientation, ProjectFormat) else str(orientation)
            if value not in {item.value for item in ProjectFormat}:
                raise IndexError("invalid orientation filter")
            clauses.append("orientation = ?")
            params.append(value)
        if talking_candidate is not None:
            if not isinstance(talking_candidate, bool):
                raise IndexError("talking_candidate filter must be boolean")
            clauses.append("talking_candidate = ?")
            params.append(int(talking_candidate))
        sql = "SELECT clip_id, embedding FROM clip_search_index"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        rows = self.db.connection.execute(sql, params).fetchall()
        scored: list[tuple[float, str]] = []
        query_norm = math.sqrt(sum(item * item for item in query))
        for row in rows:
            values = _vector(json.loads(row["embedding"]))
            norm = math.sqrt(sum(item * item for item in values))
            raw_score = 0.0 if query_norm == 0 or norm == 0 else sum(a * b for a, b in zip(query, values)) / (query_norm * norm)
            score = max(-1.0, min(1.0, raw_score))
            scored.append((score, row["clip_id"]))
        scored.sort(key=lambda item: (-item[0], item[1]))
        hits: list[ClipSearchHit] = []
        clips = ClipRepository(self.db)
        for score, clip_id in scored[:top_k]:
            clip = clips.get(UUID(clip_id))
            if clip is not None:
                hits.append(ClipSearchHit(clip, score))
        return hits


class ClipTextSearchService:
    """Small-sample local text retrieval without pretending to be embedding search.

    This is an explicit fallback for environments where no embedding provider
    is configured. It ranks persisted Clip descriptions/transcripts by token
    overlap and marks every hit so callers can show the weaker evidence basis.
    It never writes vectors or changes the embedding index.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    def search(
        self,
        query: str,
        *,
        top_k: int = 10,
        asset_id: UUID | None = None,
        orientation: ProjectFormat | str | None = None,
        talking_candidate: bool | None = None,
    ) -> list[ClipSearchHit]:
        _validate_top_k(top_k)
        if not isinstance(query, str) or not query.strip() or len(query) > 10_000:
            raise IndexError("search query must be non-empty and at most 10000 characters")
        query_terms = _lexical_terms(query)
        if not query_terms:
            raise IndexError("search query must contain searchable text")
        clips = ClipRepository(self.db).list()
        scored: list[tuple[float, str, Clip]] = []
        for clip in clips:
            if asset_id is not None and clip.asset_id != asset_id:
                continue
            if orientation is not None:
                value = orientation.value if isinstance(orientation, ProjectFormat) else str(orientation)
                if value not in {item.value for item in ProjectFormat}:
                    raise IndexError("invalid orientation filter")
                if clip.orientation is None or clip.orientation.value != value:
                    continue
            if talking_candidate is not None:
                if not isinstance(talking_candidate, bool):
                    raise IndexError("talking_candidate filter must be boolean")
                if clip.talking_candidate != talking_candidate:
                    continue
            clip_terms = _lexical_terms(_clip_content_text(clip))
            overlap = query_terms.intersection(clip_terms)
            if not overlap:
                continue
            score = len(overlap) / len(query_terms)
            scored.append((max(0.0, min(1.0, score)), str(clip.id), clip))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [ClipSearchHit(clip, score, "lexical_overlap") for score, _, clip in scored[:top_k]]


def _validate_top_k(top_k: object) -> None:
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 1_000:
        raise IndexError("top_k must be an integer from 1 to 1000")


def _clip_content_text(clip: Clip) -> str:
    """Return Clip fields without searchable field labels."""
    values: list[str] = []
    for value in (
        clip.transcript, clip.visual_description, *clip.people, *clip.objects,
        clip.location, clip.action, clip.shot_type, clip.orientation.value if clip.orientation else None,
    ):
        if isinstance(value, str) and value.strip():
            values.append(value)
    return " ".join(values)


def _lexical_terms(value: str) -> set[str]:
    """Tokenize Latin words and overlapping CJK bigrams/trigrams."""
    terms: set[str] = set()
    for match in re.finditer(r"[a-z0-9]+|[\u4e00-\u9fff]+", value.lower()):
        token = match.group(0)
        if token.isascii():
            terms.add(token)
            continue
        terms.update(character for character in token if character not in "的了是在和与及我你他她其这那一个为从到对中有无也更")
        terms.update(token[index:index + 2] for index in range(len(token) - 1))
        terms.update(token[index:index + 3] for index in range(len(token) - 2))
    return {term for term in terms if term.strip()}
