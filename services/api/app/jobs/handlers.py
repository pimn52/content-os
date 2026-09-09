"""Small, provider-neutral handlers for asset-targeted local jobs."""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, Sequence
from uuid import UUID

from app.db import ClipRepository
from app.domain.models import Asset, Clip, Job, JobStatus, JobType
from app.renderer import LocalResourceError, RemotionRenderer, RenderInputError, RenderProcessError, RenderTimeout, UnauthorizedVisualError, render_output_path
from app.media.extraction import (
    AudioExtraction,
    AudioUnavailableError,
    ExtractionBinaryMissing,
    ExtractionOutputError,
    ExtractionProcessError,
    ExtractionTimeout,
    ExtractionValidationError,
    KeyframeExtraction,
)
from app.media.pipeline import (
    AssetIdentityMismatch,
    AssetNotPersisted,
    ClipSetPersistenceError,
    MediaAnalysisPipeline,
    MediaPipelineError,
)
from app.media.segmentation import (
    FFmpegBinaryMissing,
    FFmpegProcessError,
    FFmpegTimeout,
    SegmentationError,
)
from app.media.transcripts import ClipTranscriptPersistence, NoClipsForAsset
from app.media.vision import VisualMetadataError
from app.media.vision_pipeline import (
    MediaVisionPipeline,
    VisionAssetIdentityMismatch,
    VisionAssetNotPersisted,
    VisionClipSetMismatch,
    VisionKeyframeMismatch,
    VisionPipelineError,
)
from app.providers.asr import (
    ASRAuthenticationError,
    ASRConfigurationError,
    ASRConnectionError,
    ASRHTTPError,
    ASRInputError,
    ASRProvider,
    ASRProviderResponseError,
    ASRRateLimitError,
    ASRTimeout,
)
from app.providers.embedding import (
    EmbeddingAuthenticationError,
    EmbeddingConfigurationError,
    EmbeddingConnectionError,
    EmbeddingHTTPError,
    EmbeddingInputError,
    EmbeddingProviderResponseError,
    EmbeddingRateLimitError,
    EmbeddingTimeout,
)
from app.providers.vision import (
    VisionAuthenticationError,
    VisionConfigurationError,
    VisionConnectionError,
    VisionHTTPError,
    VisionInputError,
    VisionProviderResponseError,
    VisionRateLimitError,
    VisionTimeout,
)
from app.search import EmbeddingCountMismatch, EmbeddingIndexError, IndexError

from .runner import JobExecutionError
from .targets import AssetJobTarget, AssetJobTargetError


class AssetTargetLookup(Protocol):
    def target_for(self, job_id: UUID) -> AssetJobTarget | None: ...


class AssetLookup(Protocol):
    def get(self, asset_id: UUID) -> Asset | None: ...


class AudioExtractor(Protocol):
    def extract_audio(self, asset: Asset) -> AudioExtraction: ...


class TranscriptPersistence(Protocol):
    def apply(self, asset_id: UUID, segments: Sequence[object]) -> object: ...


class KeyframeResolver(Protocol):
    def resolve(self, asset: Asset, clips: Sequence[Clip]) -> Sequence[str]: ...


class KeyframeExtractor(Protocol):
    def extract_keyframe(self, asset: Asset, clip: Clip) -> KeyframeExtraction: ...


class ClipIndexer(Protocol):
    def index_clips(self, clips: Sequence[Clip]) -> object: ...


class ProjectLookup(Protocol):
    def get(self, project_id: UUID) -> object | None: ...


class RenderVideoJobHandler:
    """Render one persisted VideoSpec to its fixed local output path."""

    def __init__(self, projects: ProjectLookup, renderer: RemotionRenderer, output_root: str | Path) -> None:
        self._projects = projects
        self._renderer = renderer
        self._output_root = Path(output_root).resolve()

    def __call__(self, job: Job) -> None:
        if job.status is not JobStatus.RUNNING:
            raise JobExecutionError("job_not_claimed", "job must be claimed before execution", retryable=False)
        if job.type is not JobType.RENDER or job.payload is None:
            raise JobExecutionError("render_payload_invalid", "render job input is invalid", retryable=False)
        payload = job.payload
        if job.project_id != payload.project_id or self._projects.get(payload.project_id) is None:
            raise JobExecutionError("render_project_missing", "render project is unavailable", retryable=False)
        output = render_output_path(self._output_root, payload.project_id, payload.render_id)
        try:
            self._renderer.render(payload.video_spec, output)
        except RenderTimeout:
            output.unlink(missing_ok=True)
            raise JobExecutionError("render_timeout", "local renderer timed out", retryable=True) from None
        except RenderProcessError:
            output.unlink(missing_ok=True)
            raise JobExecutionError("render_processing_failed", "local renderer failed", retryable=True) from None
        except (RenderInputError, LocalResourceError, UnauthorizedVisualError):
            output.unlink(missing_ok=True)
            raise JobExecutionError("render_invalid", "render input is not processable", retryable=False) from None


class ExtractedKeyframeResolver:
    """Resolve deterministic local keyframes through the media extractor."""

    def __init__(self, extractor: KeyframeExtractor) -> None:
        self._extractor = extractor

    def resolve(self, asset: Asset, clips: Sequence[Clip]) -> tuple[str, ...]:
        return tuple(str(self._extractor.extract_keyframe(asset, clip).path) for clip in clips)


class AssetAnalysisJobHandler:
    """Resolve an analysis target, then run the local media pipeline."""

    def __init__(
        self,
        target_store: AssetTargetLookup,
        assets: AssetLookup,
        pipeline: MediaAnalysisPipeline,
    ) -> None:
        self._target_store = target_store
        self._assets = assets
        self._pipeline = pipeline

    def __call__(self, job: Job) -> None:
        asset = _resolve_asset_job(job, JobType.ANALYZE_ASSET, self._target_store, self._assets)
        try:
            self._pipeline.process(asset)
        except (ExtractionTimeout, FFmpegTimeout):
            raise JobExecutionError("media_analysis_timeout", "media analysis timed out", retryable=True) from None
        except (ExtractionProcessError, FFmpegProcessError):
            raise JobExecutionError("media_analysis_processing_failed", "media analysis could not complete", retryable=True) from None
        except (
            AssetIdentityMismatch,
            AssetNotPersisted,
            ClipSetPersistenceError,
            MediaPipelineError,
            ExtractionBinaryMissing,
            ExtractionOutputError,
            ExtractionValidationError,
            AudioUnavailableError,
            FFmpegBinaryMissing,
            SegmentationError,
        ):
            raise JobExecutionError("media_analysis_invalid", "media analysis input is not processable", retryable=False) from None


class AssetTranscriptionJobHandler:
    """Extract local analysis audio, transcribe it, and atomically update Clips."""

    def __init__(
        self,
        target_store: AssetTargetLookup,
        assets: AssetLookup,
        extractor: AudioExtractor,
        provider: ASRProvider,
        transcript_persistence: TranscriptPersistence | ClipTranscriptPersistence,
    ) -> None:
        self._target_store = target_store
        self._assets = assets
        self._extractor = extractor
        self._provider = provider
        self._transcript_persistence = transcript_persistence

    def __call__(self, job: Job) -> None:
        asset = _resolve_asset_job(job, JobType.TRANSCRIBE_AUDIO, self._target_store, self._assets)
        if not asset.has_audio:
            raise JobExecutionError("asset_has_no_audio", "asset has no audio stream to transcribe", retryable=False)
        try:
            audio = self._extractor.extract_audio(asset)
        except ExtractionTimeout:
            raise JobExecutionError("audio_extraction_timeout", "audio extraction timed out", retryable=True) from None
        except ExtractionProcessError:
            raise JobExecutionError("audio_extraction_failed", "audio extraction could not complete", retryable=True) from None
        except (
            AudioUnavailableError,
            ExtractionBinaryMissing,
            ExtractionOutputError,
            ExtractionValidationError,
        ):
            raise JobExecutionError("audio_extraction_invalid", "audio cannot be extracted for transcription", retryable=False) from None

        try:
            result = self._provider.transcribe(audio.path)
        except (ASRRateLimitError, ASRTimeout, ASRConnectionError):
            raise JobExecutionError("asr_temporarily_unavailable", "transcription provider is temporarily unavailable", retryable=True) from None
        except ASRHTTPError as error:
            retryable = error.status_code >= 500 or error.status_code in {408, 409, 425}
            code = "asr_temporarily_unavailable" if retryable else "asr_request_rejected"
            message = "transcription provider is temporarily unavailable" if retryable else "transcription provider rejected the request"
            raise JobExecutionError(code, message, retryable=retryable) from None
        except (ASRAuthenticationError, ASRConfigurationError, ASRInputError, ASRProviderResponseError):
            raise JobExecutionError("asr_invalid_request", "transcription request cannot be processed", retryable=False) from None

        try:
            self._transcript_persistence.apply(asset.id, result.segments)
        except NoClipsForAsset:
            raise JobExecutionError("clips_missing", "asset has no clips available for transcription", retryable=False) from None
        except (TypeError, ValueError):
            raise JobExecutionError("transcript_invalid", "transcription result cannot be persisted", retryable=False) from None


class AssetVisionJobHandler:
    """Resolve existing Clips/keyframes, then atomically persist vision metadata."""

    def __init__(
        self,
        target_store: AssetTargetLookup,
        assets: AssetLookup,
        clips: ClipRepository,
        keyframes: KeyframeResolver,
        pipeline: MediaVisionPipeline,
        indexer: ClipIndexer | None = None,
    ) -> None:
        self._target_store = target_store
        self._assets = assets
        self._clips = clips
        self._keyframes = keyframes
        self._pipeline = pipeline
        self._indexer = indexer

    def __call__(self, job: Job) -> None:
        asset = _resolve_asset_job(job, JobType.INDEX_CLIPS, self._target_store, self._assets)
        clips = self._clips.list_by_asset(asset.id)
        if not clips:
            raise JobExecutionError("clips_missing", "asset has no clips available for visual analysis", retryable=False)
        try:
            paths = self._keyframes.resolve(asset, clips)
        except ExtractionTimeout:
            raise JobExecutionError("keyframe_extraction_timeout", "keyframe extraction timed out", retryable=True) from None
        except ExtractionProcessError:
            raise JobExecutionError("keyframe_extraction_failed", "keyframe extraction could not complete", retryable=True) from None
        except (ExtractionBinaryMissing, ExtractionOutputError, ExtractionValidationError):
            raise JobExecutionError("keyframes_invalid", "keyframes cannot be extracted for visual analysis", retryable=False) from None
        except (FileNotFoundError, TypeError, ValueError):
            raise JobExecutionError("keyframes_missing", "keyframes are unavailable for visual analysis", retryable=False) from None
        try:
            result = self._pipeline.process(asset, clips, paths)
        except (VisionRateLimitError, VisionTimeout, VisionConnectionError):
            raise JobExecutionError("vision_temporarily_unavailable", "vision provider is temporarily unavailable", retryable=True) from None
        except VisionHTTPError as error:
            retryable = error.status_code >= 500 or error.status_code in {408, 409, 425}
            code = "vision_temporarily_unavailable" if retryable else "vision_request_rejected"
            message = "vision provider is temporarily unavailable" if retryable else "vision provider rejected the request"
            raise JobExecutionError(code, message, retryable=retryable) from None
        except (
            VisionAuthenticationError,
            VisionConfigurationError,
            VisionInputError,
            VisionProviderResponseError,
            VisionAssetIdentityMismatch,
            VisionAssetNotPersisted,
            VisionClipSetMismatch,
            VisionKeyframeMismatch,
            VisionPipelineError,
            VisualMetadataError,
        ):
            raise JobExecutionError("vision_invalid", "visual analysis input is not processable", retryable=False) from None
        if self._indexer is None:
            return
        try:
            self._indexer.index_clips(result.clips)
        except (EmbeddingRateLimitError, EmbeddingTimeout, EmbeddingConnectionError):
            raise JobExecutionError("embedding_temporarily_unavailable", "embedding provider is temporarily unavailable", retryable=True) from None
        except EmbeddingHTTPError as error:
            retryable = error.status_code >= 500 or error.status_code in {408, 409, 425}
            code = "embedding_temporarily_unavailable" if retryable else "embedding_request_rejected"
            message = "embedding provider is temporarily unavailable" if retryable else "embedding provider rejected the request"
            raise JobExecutionError(code, message, retryable=retryable) from None
        except (
            EmbeddingAuthenticationError,
            EmbeddingConfigurationError,
            EmbeddingInputError,
            EmbeddingProviderResponseError,
            EmbeddingCountMismatch,
            EmbeddingIndexError,
            IndexError,
        ):
            raise JobExecutionError("embedding_invalid", "Clip embeddings cannot be produced or persisted", retryable=False) from None


def _resolve_asset_job(
    job: Job,
    expected_type: JobType,
    target_store: AssetTargetLookup,
    assets: AssetLookup,
) -> Asset:
    if job.status is not JobStatus.RUNNING:
        raise JobExecutionError("job_not_claimed", "job must be claimed before execution", retryable=False)
    if job.type is not expected_type:
        raise JobExecutionError("job_type_mismatch", "job type does not match this handler", retryable=False)
    try:
        target = target_store.target_for(job.id)
    except AssetJobTargetError:
        raise JobExecutionError("asset_target_invalid", "asset job target cannot be resolved", retryable=False) from None
    if target is None:
        raise JobExecutionError("asset_target_missing", "asset job target is missing", retryable=False)
    asset = assets.get(target.asset_id)
    if asset is None:
        raise JobExecutionError("asset_missing", "targeted asset is missing", retryable=False)
    return asset
