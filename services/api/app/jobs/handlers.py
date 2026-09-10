"""Small, provider-neutral handlers for asset-targeted local jobs."""
from __future__ import annotations

from pathlib import Path
from decimal import Decimal
from typing import Literal, Protocol, Sequence
from uuid import UUID

from app.budget import BudgetLimitError, ProviderCallError, ProviderCallLedger, provider_call_input_digest
from app.db import ClipRepository
from app.domain.models import Asset, Clip, CostCategory, Job, JobStatus, JobType, ProviderCallRecord, UsageCost
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
        ledger: ProviderCallLedger | None = None,
        provider_name: str = "openai-compatible",
        provider_model: str = "runtime-asr",
    ) -> None:
        self._target_store = target_store
        self._assets = assets
        self._extractor = extractor
        self._provider = provider
        self._transcript_persistence = transcript_persistence
        self._ledger = ledger
        self._provider_name = provider_name
        self._provider_model = provider_model

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

        call = _reserve_job_provider_call(
            self._ledger,
            job,
            operation="asr",
            category=CostCategory.ASR,
            provider=self._provider_name,
            model=self._provider_model,
            input_source=f"asset:{asset.id}:audio",
            known_local_cost=self._provider_name == "faster-whisper",
        )
        try:
            result = self._provider.transcribe(audio.path)
        except (ASRRateLimitError, ASRTimeout, ASRConnectionError):
            _finish_job_provider_call(self._ledger, job, call, status="failed", error_code="asr_temporarily_unavailable")
            raise JobExecutionError("asr_temporarily_unavailable", "transcription provider is temporarily unavailable", retryable=True) from None
        except ASRHTTPError as error:
            retryable = error.status_code >= 500 or error.status_code in {408, 409, 425}
            code = "asr_temporarily_unavailable" if retryable else "asr_request_rejected"
            message = "transcription provider is temporarily unavailable" if retryable else "transcription provider rejected the request"
            _finish_job_provider_call(self._ledger, job, call, status="failed", error_code=code)
            raise JobExecutionError(code, message, retryable=retryable) from None
        except (ASRAuthenticationError, ASRConfigurationError, ASRInputError, ASRProviderResponseError):
            _finish_job_provider_call(self._ledger, job, call, status="failed", error_code="asr_invalid_request")
            raise JobExecutionError("asr_invalid_request", "transcription request cannot be processed", retryable=False) from None
        _finish_job_provider_call(self._ledger, job, call, status="completed")

        try:
            self._transcript_persistence.apply(
                asset.id,
                result.segments,
                source_reference=f"{self._provider_name}:{self._provider_model}",
            )
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
        ledger: ProviderCallLedger | None = None,
        provider_name: str = "openai-compatible",
        vision_model: str = "runtime-vision",
        embedding_model: str = "runtime-embedding",
    ) -> None:
        self._target_store = target_store
        self._assets = assets
        self._clips = clips
        self._keyframes = keyframes
        self._pipeline = pipeline
        self._indexer = indexer
        self._ledger = ledger
        self._provider_name = provider_name
        self._vision_model = vision_model
        self._embedding_model = embedding_model

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
            if self._ledger is None:
                result = self._pipeline.process(asset, clips, paths)
            else:
                vision_provider = _MeteredVisionProvider(
                    self._pipeline.provider,
                    self._ledger,
                    job,
                    asset.id,
                    self._provider_name,
                    self._vision_model,
                )
                result = self._pipeline.process(asset, clips, paths, provider=vision_provider)
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
        embedding_call = _reserve_job_provider_call(
            self._ledger,
            job,
            operation="embedding",
            category=CostCategory.LLM,
            provider=self._provider_name,
            model=self._embedding_model,
            input_source=f"asset:{asset.id}:clips:{len(result.clips)}",
        )
        try:
            self._indexer.index_clips(result.clips)
        except (EmbeddingRateLimitError, EmbeddingTimeout, EmbeddingConnectionError):
            _finish_job_provider_call(self._ledger, job, embedding_call, status="failed", error_code="embedding_temporarily_unavailable")
            raise JobExecutionError("embedding_temporarily_unavailable", "embedding provider is temporarily unavailable", retryable=True) from None
        except EmbeddingHTTPError as error:
            retryable = error.status_code >= 500 or error.status_code in {408, 409, 425}
            code = "embedding_temporarily_unavailable" if retryable else "embedding_request_rejected"
            message = "embedding provider is temporarily unavailable" if retryable else "embedding provider rejected the request"
            _finish_job_provider_call(self._ledger, job, embedding_call, status="failed", error_code=code)
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
            _finish_job_provider_call(self._ledger, job, embedding_call, status="failed", error_code="embedding_invalid")
            raise JobExecutionError("embedding_invalid", "Clip embeddings cannot be produced or persisted", retryable=False) from None


def _reserve_job_provider_call(
    ledger: ProviderCallLedger | None,
    job: Job,
    *,
    operation: Literal["asr", "vision", "embedding"],
    category: CostCategory,
    provider: str,
    model: str,
    input_source: str,
    call_key: str = "call",
    known_local_cost: bool = False,
) -> ProviderCallRecord | None:
    if ledger is None:
        return None
    if job.project_id is None:
        raise JobExecutionError(
            "provider_project_required",
            "provider-backed asset jobs require a project for budget accounting",
            retryable=False,
        )
    try:
        estimated_cost = (
            UsageCost(
                category=category,
                amount=Decimal("0"),
                currency="USD",
                provider=provider,
                note="local provider execution; no external provider charge",
            )
            if known_local_cost
            else UsageCost(
                category=category,
                provider=provider,
                note="runtime provider price is not observable; set an explicit budget policy or estimate",
            )
        )
        reservation = ledger.reserve_execution(
            project_id=job.project_id,
            idempotency_key=f"job:{job.id}:{operation}:{job.attempt}:{call_key}",
            operation=operation,
            mode="runtime",
            provider=provider,
            model=model,
            input_source=input_source,
            input_digest=provider_call_input_digest({
                "project_id": str(job.project_id),
                "job_id": str(job.id),
                "operation": operation,
                "provider": provider,
                "model": model,
                "input_source": input_source,
                "call_key": call_key,
            }),
            estimated_cost=estimated_cost,
            allow_existing_unknown_cost=known_local_cost,
        )
        if not reservation.owner:
            raise JobExecutionError(
                "provider_execution_recovery_required",
                "a matching provider operation already exists; inspect its job and provider-call state before retrying",
                retryable=False,
            )
        return reservation.record
    except BudgetLimitError:
        raise JobExecutionError("provider_budget_blocked", "provider call blocked by the budget policy", retryable=False) from None
    except ProviderCallError:
        raise JobExecutionError("provider_call_accounting_failed", "provider call could not be reserved", retryable=True) from None


def _finish_job_provider_call(
    ledger: ProviderCallLedger | None,
    job: Job,
    call: ProviderCallRecord | None,
    *,
    status: Literal["completed", "failed"],
    error_code: str | None = None,
) -> None:
    if ledger is None or call is None or job.project_id is None:
        return
    try:
        ledger.finish(
            project_id=job.project_id,
            call_id=call.id,
            status=status,
            usage_observable=False,
            error_code=error_code,
        )
    except ProviderCallError:
        raise JobExecutionError("provider_call_accounting_failed", "provider call result could not be reconciled", retryable=True) from None


class _MeteredVisionProvider:
    """Count each keyframe request while preserving the provider contract."""

    def __init__(self, provider: object, ledger: ProviderCallLedger, job: Job, asset_id: UUID, provider_name: str, model: str) -> None:
        self._provider = provider
        self._ledger = ledger
        self._job = job
        self._asset_id = asset_id
        self._provider_name = provider_name
        self._model = model
        self._index = 0

    def analyze(self, keyframe_path: str | Path) -> object:
        index = self._index
        self._index += 1
        call = _reserve_job_provider_call(
            self._ledger,
            self._job,
            operation="vision",
            category=CostCategory.LLM,
            provider=self._provider_name,
            model=self._model,
            input_source=f"asset:{self._asset_id}:keyframe:{index}",
            call_key=f"keyframe-{index}",
        )
        try:
            result = self._provider.analyze(keyframe_path)  # type: ignore[attr-defined]
        except Exception:
            _finish_job_provider_call(self._ledger, self._job, call, status="failed", error_code="vision_provider_failed")
            raise
        _finish_job_provider_call(self._ledger, self._job, call, status="completed")
        return result


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
