"""Small, provider-neutral handlers for asset-targeted local jobs."""
from __future__ import annotations

from typing import Protocol, Sequence
from uuid import UUID

from app.domain.models import Asset, Job, JobStatus, JobType
from app.media.extraction import (
    AudioExtraction,
    AudioUnavailableError,
    ExtractionBinaryMissing,
    ExtractionOutputError,
    ExtractionProcessError,
    ExtractionTimeout,
    ExtractionValidationError,
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
