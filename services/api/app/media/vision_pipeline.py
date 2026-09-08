"""All-or-nothing orchestration of keyframe vision analysis for persisted Clips."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence
from uuid import UUID

from app.db import AssetRepository, ClipRepository, Database
from app.domain.models import Asset, Clip
from app.providers.vision import ClipVisualAnalysis, VisionProvider

from .pipeline import MediaAnalysisResult
from .vision import ClipVisualMetadataPersistence, VisualMetadata


class VisionPipelineError(RuntimeError):
    pass


class VisionAssetNotPersisted(VisionPipelineError):
    pass


class VisionAssetIdentityMismatch(VisionPipelineError):
    pass


class VisionClipSetMismatch(VisionPipelineError):
    pass


class VisionKeyframeMismatch(VisionPipelineError):
    pass


class _VisualPersistence(Protocol):
    def apply(self, asset_id: UUID, results: Sequence[VisualMetadata], keyframe_paths: Sequence[Path]) -> list[Clip]: ...


@dataclass(frozen=True)
class VisionAnalysisResult:
    asset: Asset
    clips: tuple[Clip, ...]
    keyframe_paths: tuple[Path, ...]


class MediaVisionPipeline:
    """Call a provider outside a write transaction, then replace Clip visuals once."""

    def __init__(
        self,
        db: Database,
        provider: VisionProvider,
        persistence: _VisualPersistence | ClipVisualMetadataPersistence | None = None,
    ) -> None:
        self.db = db
        self.provider = provider
        self.assets = AssetRepository(db)
        self.clips = ClipRepository(db)
        self.persistence = ClipVisualMetadataPersistence(db) if persistence is None else persistence

    def process(
        self,
        asset_or_analysis: Asset | MediaAnalysisResult,
        clips: Sequence[Clip] | None = None,
        keyframe_paths: Sequence[str | Path] | None = None,
    ) -> VisionAnalysisResult:
        asset, input_clips, paths = self._inputs(asset_or_analysis, clips, keyframe_paths)
        stored_asset = self._stored_asset(asset)
        stored_clips = self.clips.list_by_asset(stored_asset.id)
        self._validate_clip_identity(stored_asset, stored_clips, input_clips)
        normalized_paths = self._validate_paths(paths, stored_clips)

        # Provider calls can be slow or remote. No SQLite write transaction is
        # open until every result is available and identity-bound below.
        metadata = tuple(
            _metadata_for(clip, path, self.provider.analyze(path))
            for clip, path in zip(stored_clips, normalized_paths)
        )
        updated = self.persistence.apply(stored_asset.id, metadata, normalized_paths)
        return VisionAnalysisResult(stored_asset, tuple(updated), normalized_paths)

    def analyze(
        self,
        asset_or_analysis: Asset | MediaAnalysisResult,
        clips: Sequence[Clip] | None = None,
        keyframe_paths: Sequence[str | Path] | None = None,
    ) -> VisionAnalysisResult:
        return self.process(asset_or_analysis, clips, keyframe_paths)

    @staticmethod
    def _inputs(
        asset_or_analysis: Asset | MediaAnalysisResult,
        clips: Sequence[Clip] | None,
        keyframe_paths: Sequence[str | Path] | None,
    ) -> tuple[Asset, Sequence[Clip], Sequence[str | Path]]:
        if isinstance(asset_or_analysis, MediaAnalysisResult):
            if clips is not None or keyframe_paths is not None:
                raise VisionPipelineError("media analysis result cannot be combined with separate clips or keyframes")
            return asset_or_analysis.asset, asset_or_analysis.clips, asset_or_analysis.keyframe_paths
        if clips is None or keyframe_paths is None:
            raise VisionPipelineError("asset vision analysis requires ordered Clips and keyframes")
        return asset_or_analysis, clips, keyframe_paths

    def _stored_asset(self, candidate: Asset) -> Asset:
        stored = self.assets.get(candidate.id)
        if stored is None:
            raise VisionAssetNotPersisted("asset is not persisted")
        if (
            candidate.id != stored.id
            or candidate.content_hash != stored.content_hash
            or candidate.duration_ms != stored.duration_ms
            or candidate.source_file != stored.source_file
        ):
            raise VisionAssetIdentityMismatch("asset does not match stored immutable identity")
        return stored

    @staticmethod
    def _validate_clip_identity(asset: Asset, stored: Sequence[Clip], supplied: Sequence[Clip]) -> None:
        if not stored or len(stored) != len(supplied):
            raise VisionClipSetMismatch("supplied Clip count does not match persisted Clips")
        for persisted, candidate in zip(stored, supplied):
            if (
                candidate.id != persisted.id
                or candidate.asset_id != asset.id
                or candidate.asset_duration_ms != asset.duration_ms
                or candidate.start_ms != persisted.start_ms
                or candidate.end_ms != persisted.end_ms
            ):
                raise VisionClipSetMismatch("supplied Clip order or identity does not match persisted Clips")

    @staticmethod
    def _validate_paths(paths: Sequence[str | Path], clips: Sequence[Clip]) -> tuple[Path, ...]:
        if len(paths) != len(clips):
            raise VisionKeyframeMismatch("keyframe count does not match Clip count")
        normalized = tuple(Path(path).resolve() for path in paths)
        if any(not path.is_file() for path in normalized):
            raise VisionKeyframeMismatch("a supplied keyframe file is missing")
        return normalized


def _metadata_for(clip: Clip, path: Path, analysis: ClipVisualAnalysis) -> VisualMetadata:
    if not isinstance(analysis, ClipVisualAnalysis):
        raise VisionPipelineError("vision provider returned an invalid analysis contract")
    return VisualMetadata.for_clip(clip, path, analysis)
