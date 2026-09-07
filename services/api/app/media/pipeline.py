"""Idempotent composition of segmentation, extraction, and Clip persistence."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Protocol, Sequence
from uuid import UUID, uuid5

from app.db import AssetRepository, ClipRepository, Database
from app.domain.models import Asset, Clip

from .extraction import AudioExtraction, KeyframeExtraction


class MediaPipelineError(RuntimeError):
    """Base error for a media analysis slice."""


class AssetIdentityMismatch(MediaPipelineError):
    pass


class AssetNotPersisted(MediaPipelineError):
    pass


class ClipSetPersistenceError(MediaPipelineError):
    pass


class _Segmenter(Protocol):
    def segment(self, asset: Asset) -> list[Clip]: ...


class _Extractor(Protocol):
    def extract_audio(self, asset: Asset) -> AudioExtraction: ...

    def extract_keyframe(self, asset: Asset, clip: Clip) -> KeyframeExtraction: ...


_CLIP_NAMESPACE = UUID("c7f8f225-d0c3-5e1f-9c3e-b1a3a0343d63")


@dataclass(frozen=True)
class MediaAnalysisResult:
    asset: Asset
    clips: tuple[Clip, ...]
    audio_path: Path | None
    keyframe_paths: tuple[Path, ...]


class MediaAnalysisPipeline:
    """Run expensive local media analysis before atomically replacing Clip rows."""

    def __init__(self, db: Database, segmenter: _Segmenter, extractor: _Extractor) -> None:
        self.db = db
        self.segmenter = segmenter
        self.extractor = extractor
        self.assets = AssetRepository(db)
        self.clips = ClipRepository(db)

    def process(self, asset: Asset) -> MediaAnalysisResult:
        stored = self._stored_identity(asset)
        # Segmentation and FFmpeg-derived files deliberately happen before the
        # short write transaction. A failure here leaves rows untouched; usable
        # cache files may safely be reused on retry by the extractor.
        normalized = self._normalize_clips(stored, self.segmenter.segment(stored))
        audio_path = self.extractor.extract_audio(stored).path if stored.has_audio else None
        keyframe_paths = tuple(self.extractor.extract_keyframe(stored, clip).path for clip in normalized)
        self._replace_clip_set(stored, normalized)
        return MediaAnalysisResult(stored, tuple(normalized), audio_path, keyframe_paths)

    def analyze(self, asset: Asset) -> MediaAnalysisResult:
        """Alias for service callers that prefer analysis terminology."""
        return self.process(asset)

    def _stored_identity(self, candidate: Asset) -> Asset:
        stored = self.assets.get(candidate.id)
        if stored is None:
            raise AssetNotPersisted(f"asset {candidate.id} is not persisted")
        if (
            stored.id != candidate.id
            or stored.content_hash != candidate.content_hash
            or stored.duration_ms != candidate.duration_ms
            or stored.source_file != candidate.source_file
        ):
            raise AssetIdentityMismatch("input asset does not match its stored immutable identity")
        return stored

    @staticmethod
    def _normalize_clips(asset: Asset, detected: Sequence[Clip]) -> list[Clip]:
        normalized: list[Clip] = []
        for clip in detected:
            if clip.asset_id != asset.id or clip.asset_duration_ms != asset.duration_ms:
                raise MediaPipelineError("segmenter returned a clip for a different asset identity")
            stable_id = uuid5(_CLIP_NAMESPACE, f"{asset.id}:{clip.start_ms}:{clip.end_ms}")
            normalized.append(clip.model_copy(update={"id": stable_id}))
        if not normalized:
            raise MediaPipelineError("segmenter returned no clips")
        ordered = sorted(normalized, key=lambda clip: (clip.start_ms, clip.end_ms, str(clip.id)))
        if ordered[0].start_ms != 0 or ordered[-1].end_ms != asset.duration_ms:
            raise MediaPipelineError("segmenter clips must cover the complete source")
        if any(left.end_ms != right.start_ms for left, right in zip(ordered, ordered[1:])):
            raise MediaPipelineError("segmenter clips must be continuous and non-overlapping")
        return ordered

    def _replace_clip_set(self, asset: Asset, desired: Sequence[Clip]) -> None:
        desired_ids = {clip.id for clip in desired}
        with self._write_transaction():
            # Re-check inside the write transaction so an out-of-band Asset
            # edit cannot cause stale expensive work to overwrite a new row.
            self._stored_identity(asset)
            existing = self.clips.list_by_asset(asset.id)
            try:
                for clip in desired:
                    self.clips.upsert(clip)
                for clip in existing:
                    if clip.id not in desired_ids:
                        self.clips.delete(clip.id)
            except Exception as exc:
                raise ClipSetPersistenceError("could not atomically replace the clip set") from exc

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        self.db.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.connection.rollback()
            raise
        else:
            try:
                self.db.connection.commit()
            except BaseException:
                self.db.connection.rollback()
                raise


MediaAnalysisService = MediaAnalysisPipeline
