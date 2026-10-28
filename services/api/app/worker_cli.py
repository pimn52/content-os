"""Command-line assembly and loop for the local Content OS worker."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import signal
import socket
import subprocess
import tempfile
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from threading import Event
from typing import Sequence
from uuid import UUID

from app.db import AssetRepository, AudioAssetRepository, ClipRepository, Database, ImageAssetRepository, JobRepository, ProjectRepository, TalkingProfileRepository, VoiceProfileRepository
from app.budget import ProviderCallLedger
from app.domain.models import JobStatus, JobType, VoiceReferenceWindowSelection
from app.jobs.handlers import AssetAnalysisJobHandler, AssetTranscriptionJobHandler, AudioAssetTranscriptionJobHandler, AssetVisionJobHandler, ExtractedKeyframeResolver, RenderVideoJobHandler, TalkingGenerationJobHandler, TalkingQaJobHandler, VoiceBoundaryAlignmentJobHandler, VoiceGenerationJobHandler, VoiceQaJobHandler
from app.jobs.runner import JobRunner
from app.jobs.store import JobStore
from app.jobs.targets import AssetJobTargetStore, AudioAssetJobTargetStore
from app.jobs.worker import JobWorker
from app.media.extraction import FFmpegExtractionService
from app.media.ffprobe import FFProbeAdapter
from app.media.audio_importer import AudioImporter
from app.media.voice_reference import reference_extraction_command
from app.media.importer import MediaImporter
from app.media.pipeline import MediaAnalysisPipeline
from app.media.segmentation import FFmpegSceneDetector
from app.media.transcripts import AudioTranscriptPersistence, ClipTranscriptPersistence
from app.media.vision_pipeline import MediaVisionPipeline
from app.providers.asr import ASRConfigurationError, FasterWhisperASRProvider, OpenAICompatibleASRProvider
from app.providers.embedding import EmbeddingConfigurationError, OpenAICompatibleEmbeddingProvider
from app.providers.vision import OpenAICompatibleVisionProvider, VisionConfigurationError
from app.providers.latentsync import LatentSyncProvider
from app.providers.talking import TalkingConfigurationError
from app.providers.voice import OmniVoiceProvider, VoiceSynthesisResult, VoiceConfigurationError, VoiceConnectionError, VoiceInputError, VoiceProviderResponseError, VoiceTimeout
from app.production_runs import production_run_jobs as _production_run_jobs, ProductionRunService
from app.talking.planned_preview import TalkingRunPreviewJobHandler
from app.search import ClipEmbeddingIndexer
from app.renderer import RemotionRenderer
from app.runtime import resolve_local_executable, vision_api_key
from app.voice_performance import authorized_voice_reference_choices, resolve_authorized_voice_reference_choice, VoicePerformancePlanningError
from app.voice_pace import VoicePaceCandidateJobHandler


def _vision_provider_from_env():
    key = vision_api_key(os.environ)
    if key is None: return None
    return OpenAICompatibleVisionProvider(key,
        base_url=os.environ.get("CONTENT_OS_VISION_BASE_URL", "https://api.openai.com/v1"),
        model=os.environ.get("CONTENT_OS_VISION_MODEL", "gpt-4.1-mini"),
        protocol=os.environ.get("CONTENT_OS_VISION_PROTOCOL", "responses"),
        video_input=os.environ.get("CONTENT_OS_VISION_VIDEO_INPUT", "false").lower() == "true",
        thinking_type=os.environ.get("CONTENT_OS_VISION_THINKING_TYPE"),
        timeout_seconds=float(os.environ.get("CONTENT_OS_VISION_TIMEOUT_SECONDS", "120")),
        detail=os.environ.get("CONTENT_OS_VISION_DETAIL", "high"))


@dataclass(frozen=True)
class WorkerConfig:
    db_path: Path
    data_root: Path
    worker_id: str
    lease_seconds: float
    heartbeat_seconds: float
    poll_seconds: float
    max_attempts: int
    once: bool
    job_types: tuple[JobType, ...]
    gpu_resource_key: str
    ffmpeg: str
    ffprobe: str
    job_id: UUID | None = None
    production_run_id: UUID | None = None
    production_only: bool = False


def _resolve_omnivoice_reference_path(source_file: str, data_root: str | Path) -> Path:
    """Resolve imported media, including one legacy bare-file form.

    Current imports retain paths below ``assets/originals``.  A small number
    of older local records retained only the content-addressed filename.
    Treat that form as the standard originals location when the direct
    data-root relative path is absent; do not search arbitrary directories or
    mutate the persisted asset while a job is running.
    """

    path = Path(source_file)
    if path.is_absolute():
        return path
    root = Path(data_root).resolve()
    direct = root.parent / path if path.parts and path.parts[0].casefold() == root.name.casefold() else root / path
    if direct.is_file() or len(path.parts) != 1:
        return direct
    legacy_original = root / "assets" / "originals" / path.name
    return legacy_original if legacy_original.is_file() else direct


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="content-os-worker", description="Run local Content OS jobs.")
    parser.add_argument("--db", dest="db_path", default=None, help="SQLite path (default CONTENT_OS_DB_PATH or content-os-data/content-os.sqlite3)")
    parser.add_argument("--data-root", default=None, help="Derived media root (default CONTENT_OS_DATA_ROOT or the DB directory)")
    parser.add_argument("--once", action="store_true", help="Claim and execute at most one eligible job")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--job-id", type=UUID, default=None, help="Execute only this Job UUID; requires --once")
    scope.add_argument("--production-run-id", type=UUID, default=None,
                       help="Execute only Jobs referenced by this ProductionRun; stop after a failed attempt")
    scope.add_argument("--production-only",action="store_true",help="只处理普通生产引用和正常媒体任务，不执行旧孤立生成队列")
    parser.add_argument("--worker-id", default=None)
    parser.add_argument("--lease-seconds", type=float, default=300.0)
    parser.add_argument("--heartbeat-seconds", type=float, default=30.0)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--job-type", dest="job_types", action="append", choices=[item.value for item in (JobType.PLAN_CONTENT,JobType.ANALYZE_ASSET, JobType.TRANSCRIBE_AUDIO, JobType.TRANSCRIBE_AUDIO_ASSET, JobType.INDEX_CLIPS, JobType.GENERATE_VOICE, JobType.CREATE_VOICE_PACE_CANDIDATE, JobType.VERIFY_VOICE, JobType.ALIGN_VOICE_BOUNDARIES, JobType.GENERATE_TALKING, JobType.VERIFY_TALKING, JobType.PREPARE_TALKING_RUN_PREVIEW, JobType.RENDER)], help="Restrict handlers; repeat to select multiple")
    parser.add_argument("--gpu-resource-key", default=None, help="Serialize local Voice/Talking inference sharing this GPU (default gpu:<hostname>)")
    parser.add_argument("--ffmpeg", default=resolve_local_executable("ffmpeg"))
    parser.add_argument("--ffprobe", default=resolve_local_executable("ffprobe"))
    return parser


def parse_config(argv: Sequence[str] | None = None) -> WorkerConfig:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.job_id is not None and not args.once:
        parser.error("--job-id requires --once")
    db_path = Path(args.db_path or os.environ.get("CONTENT_OS_DB_PATH") or Path("content-os-data") / "content-os.sqlite3")
    data_root = Path(args.data_root or os.environ.get("CONTENT_OS_DATA_ROOT") or db_path.parent)
    worker_id = args.worker_id or f"worker-{socket.gethostname()}-{os.getpid()}"
    if (
        not math.isfinite(args.lease_seconds) or not math.isfinite(args.heartbeat_seconds)
        or not math.isfinite(args.poll_seconds)
        or args.lease_seconds <= 0 or args.heartbeat_seconds <= 0 or args.poll_seconds <= 0
        or args.heartbeat_seconds >= args.lease_seconds
    ):
        parser.error("heartbeat-seconds must be positive and shorter than lease-seconds")
    if args.max_attempts < 1 or args.max_attempts > 100:
        parser.error("max-attempts must be 1..100")
    production_types=(JobType.GENERATE_VOICE,JobType.VERIFY_VOICE,JobType.GENERATE_TALKING,JobType.VERIFY_TALKING,JobType.RENDER)
    defaults=((JobType.PLAN_CONTENT,)+production_types if args.production_only else
              production_types if args.production_run_id is not None else (JobType.ANALYZE_ASSET,JobType.TRANSCRIBE_AUDIO))
    selected=tuple(JobType(value) for value in args.job_types) if args.job_types else defaults
    if args.production_run_id is not None and any(job_type not in {
        JobType.GENERATE_VOICE, JobType.VERIFY_VOICE, JobType.GENERATE_TALKING, JobType.VERIFY_TALKING, JobType.RENDER,
    } for job_type in selected):
        parser.error("production-run-id supports only Voice, Talking, their QA and Render Jobs")
    gpu_resource_key = args.gpu_resource_key or os.environ.get("CONTENT_OS_GPU_RESOURCE_KEY") or f"gpu:{socket.gethostname()}"
    if not isinstance(gpu_resource_key, str) or not gpu_resource_key.strip() or len(gpu_resource_key.strip()) > 500:
        parser.error("gpu-resource-key must be a non-empty string of at most 500 characters")
    return WorkerConfig(
        db_path, data_root, worker_id, args.lease_seconds, args.heartbeat_seconds,
        args.poll_seconds, 1 if args.production_run_id is not None or args.production_only else args.max_attempts,
        args.once, selected, gpu_resource_key.strip(), args.ffmpeg, args.ffprobe,
        args.job_id, args.production_run_id,args.production_only,
    )


def build_runner(config: WorkerConfig, db: Database) -> JobRunner:
    # The same configured root owns media AND current use-admission evidence.
    # Never infer it from Worker CWD or the SQLite file's containing directory.
    db.data_root = config.data_root.resolve()
    store = JobStore(db)
    targets = AssetJobTargetStore(db)
    assets = AssetRepository(db)
    extractor = FFmpegExtractionService(config.data_root, command=config.ffmpeg)
    handlers = {}
    voice_provider=talking_provider=None
    if JobType.ANALYZE_ASSET in config.job_types:
        detector = FFmpegSceneDetector(config.ffmpeg)
        pipeline = MediaAnalysisPipeline(db, detector, extractor)
        handlers[JobType.ANALYZE_ASSET] = AssetAnalysisJobHandler(targets, assets, pipeline)
    if JobType.TRANSCRIBE_AUDIO in config.job_types or JobType.TRANSCRIBE_AUDIO_ASSET in config.job_types:
        provider_kind = os.environ.get("CONTENT_OS_ASR_PROVIDER", "openai-compatible").strip().lower()
        if provider_kind == "local":
            provider = FasterWhisperASRProvider(
                model=os.environ.get("CONTENT_OS_ASR_LOCAL_MODEL", "small"),
                device=os.environ.get("CONTENT_OS_ASR_DEVICE", "cpu"),
                compute_type=os.environ.get("CONTENT_OS_ASR_COMPUTE_TYPE", "int8"),
                download_root=os.environ.get("CONTENT_OS_ASR_MODEL_ROOT") or None,
            )
            provider_name = "faster-whisper"
        elif provider_kind in {"", "openai-compatible"}:
            api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("CONTENT_OS_ASR_API_KEY")
            if not api_key:
                raise ASRConfigurationError("ASR API key must be supplied at runtime")
            provider = OpenAICompatibleASRProvider(
                api_key,
                base_url=os.environ.get("OPENAI_BASE_URL", os.environ.get("CONTENT_OS_ASR_BASE_URL", "https://api.openai.com/v1")),
                model=os.environ.get("OPENAI_MODEL", os.environ.get("CONTENT_OS_ASR_MODEL", "whisper-1")),
            )
            provider_name = "openai-compatible"
        else:
            raise ASRConfigurationError("CONTENT_OS_ASR_PROVIDER must be local or openai-compatible")
        ledger = ProviderCallLedger(db)
        if JobType.TRANSCRIBE_AUDIO in config.job_types:
            handlers[JobType.TRANSCRIBE_AUDIO] = AssetTranscriptionJobHandler(
                targets, assets, extractor, provider, ClipTranscriptPersistence(db), ledger,
                provider_name=provider_name, provider_model=provider.model,
            )
        if JobType.TRANSCRIBE_AUDIO_ASSET in config.job_types:
            handlers[JobType.TRANSCRIBE_AUDIO_ASSET] = AudioAssetTranscriptionJobHandler(
                AudioAssetJobTargetStore(db), AudioAssetRepository(db), provider,
                AudioTranscriptPersistence(db), ledger, provider_name=provider_name,
                provider_model=provider.model, data_root=config.data_root,
            )
    if JobType.INDEX_CLIPS in config.job_types:
        provider = _vision_provider_from_env()
        if provider is None:
            raise VisionConfigurationError("vision credential file must be configured")
        embedding_key = os.environ.get("CONTENT_OS_EMBEDDING_API_KEY") or os.environ.get("OPENAI_API_KEY")
        indexer = None
        embedding_model = "unconfigured"
        if embedding_key:
            raw_dimensions = os.environ.get("CONTENT_OS_EMBEDDING_DIMENSIONS")
            try:
                dimensions = None if raw_dimensions is None else int(raw_dimensions)
            except ValueError as exc:
                raise EmbeddingConfigurationError("embedding dimensions must be an integer") from exc
            embedding_provider = OpenAICompatibleEmbeddingProvider(
                embedding_key,
                base_url=os.environ.get("CONTENT_OS_EMBEDDING_BASE_URL") or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
                model=os.environ.get("CONTENT_OS_EMBEDDING_MODEL", "text-embedding-3-small"),
                dimensions=dimensions,
            )
            indexer = ClipEmbeddingIndexer(db, embedding_provider)
            embedding_model = embedding_provider.model
        handlers[JobType.INDEX_CLIPS] = AssetVisionJobHandler(
            targets,
            assets,
            ClipRepository(db),
            ExtractedKeyframeResolver(extractor),
            MediaVisionPipeline(db, provider),
            indexer,
            ProviderCallLedger(db),
            vision_model=provider.model,
            embedding_model=embedding_model,
        )
    if JobType.RENDER in config.job_types:
        renderer_dir = Path(__file__).resolve().parents[3] / "apps" / "renderer"
        handlers[JobType.RENDER] = RenderVideoJobHandler(
            ProjectRepository(db),
            RemotionRenderer(
                AssetRepository(db), ClipRepository(db), images=ImageAssetRepository(db),
                audios=AudioAssetRepository(db), renderer_dir=renderer_dir,
            ),
            config.data_root / "renders",
        )
    if JobType.GENERATE_VOICE in config.job_types:
        provider = _build_voice_provider(config, db)
        voice_provider=provider
        handlers[JobType.GENERATE_VOICE] = VoiceGenerationJobHandler(
            VoiceProfileRepository(db), AudioAssetRepository(db),
            AudioImporter(db, config.data_root, FFProbeAdapter(config.ffprobe)),
            provider, config.data_root / "generated", ProviderCallLedger(db),
        )
    if JobType.CREATE_VOICE_PACE_CANDIDATE in config.job_types:
        handlers[JobType.CREATE_VOICE_PACE_CANDIDATE] = VoicePaceCandidateJobHandler(
            AudioAssetRepository(db), JobRepository(db),
            AudioImporter(db, config.data_root, FFProbeAdapter(config.ffprobe)),
            config.data_root, config.ffmpeg,
        )
    if JobType.VERIFY_VOICE in config.job_types or JobType.ALIGN_VOICE_BOUNDARIES in config.job_types:
        qa_model = os.environ.get("CONTENT_OS_VOICE_QA_ASR_MODEL", "").strip()
        if not qa_model or not Path(qa_model).exists():
            raise ASRConfigurationError("CONTENT_OS_VOICE_QA_ASR_MODEL must point to a local ASR model directory")
        qa_provider = FasterWhisperASRProvider(
            model=qa_model,
            device=os.environ.get("CONTENT_OS_VOICE_QA_ASR_DEVICE", "cpu"),
            compute_type=os.environ.get("CONTENT_OS_VOICE_QA_ASR_COMPUTE_TYPE", "int8"),
            vad_filter=False,
            word_timestamps=True,
        )
        if JobType.VERIFY_VOICE in config.job_types:
            handlers[JobType.VERIFY_VOICE] = VoiceQaJobHandler(
                AudioAssetRepository(db), qa_provider, ProviderCallLedger(db),
                provider_name="faster-whisper", provider_model=qa_model,
                max_silence_ms=int(os.environ.get("CONTENT_OS_VOICE_QA_MAX_SILENCE_MS", "2000")),
                max_leading_silence_ms=int(os.environ.get("CONTENT_OS_VOICE_QA_MAX_LEADING_SILENCE_MS", "500")),
                data_root=config.data_root,
                repair_execution={"runtime": os.environ.get("CONTENT_OS_VOICE_QA_RUNTIME_ID"),
                    "machine_id": os.environ.get("CONTENT_OS_VOICE_QA_MACHINE_ID"),
                    "parameters": {"device": os.environ.get("CONTENT_OS_VOICE_QA_ASR_DEVICE", "cpu"),
                        "compute_type": os.environ.get("CONTENT_OS_VOICE_QA_ASR_COMPUTE_TYPE", "int8"),
                        "vad_filter": False, "word_timestamps": True,
                        "max_silence_ms": int(os.environ.get("CONTENT_OS_VOICE_QA_MAX_SILENCE_MS", "2000")),
                        "max_leading_silence_ms": int(os.environ.get("CONTENT_OS_VOICE_QA_MAX_LEADING_SILENCE_MS", "500"))}},
            )
        if JobType.ALIGN_VOICE_BOUNDARIES in config.job_types:
            handlers[JobType.ALIGN_VOICE_BOUNDARIES] = VoiceBoundaryAlignmentJobHandler(
                AudioAssetRepository(db), JobRepository(db), qa_provider, ProviderCallLedger(db),
                data_root=config.data_root, provider_model=qa_model,
            )
    if JobType.GENERATE_TALKING in config.job_types:
        provider = _build_talking_provider(config)
        talking_provider=provider
        handlers[JobType.GENERATE_TALKING] = TalkingGenerationJobHandler(
            TalkingProfileRepository(db),
            AudioAssetRepository(db),
            AssetRepository(db),
            MediaImporter(db, config.data_root, FFProbeAdapter(config.ffprobe)),
            provider,
            config.data_root / "generated",
            ProviderCallLedger(db),
            ffmpeg_command=config.ffmpeg,
            execution_runtime=getattr(provider, "runtime_label", None) or os.environ.get("CONTENT_OS_TALKING_RUNTIME_ID"),
            execution_machine_id=getattr(provider, "machine_id", None) or os.environ.get("CONTENT_OS_TALKING_MACHINE_ID"),
        )
    if JobType.VERIFY_TALKING in config.job_types:
        handlers[JobType.VERIFY_TALKING] = TalkingQaJobHandler(
            AssetRepository(db), data_root=config.data_root, probe=FFProbeAdapter(config.ffprobe),
        )
    if JobType.PREPARE_TALKING_RUN_PREVIEW in config.job_types:
        handlers[JobType.PREPARE_TALKING_RUN_PREVIEW] = TalkingRunPreviewJobHandler(
            db, config.data_root, ffmpeg_command=config.ffmpeg, probe=FFProbeAdapter(config.ffprobe),
        )
    gpu_job_types = {JobType.GENERATE_VOICE, JobType.GENERATE_TALKING}
    resource_keys = {job_type: config.gpu_resource_key for job_type in config.job_types if job_type in gpu_job_types}
    from app.video_production import VideoProductionService,ContentPlanJobHandler
    from app.media.planning import VisualMaterialResolver
    from app.costs import UnknownProviderCostEstimator
    vision = _vision_provider_from_env() if JobType.PLAN_CONTENT in config.job_types else None
    estimator = vision.cost_estimator() if callable(getattr(vision,"cost_estimator",None)) else UnknownProviderCostEstimator()
    resolver = VisualMaterialResolver(db,config.data_root,vision,extractor,
        FFProbeAdapter(config.ffprobe),estimator) if JobType.PLAN_CONTENT in config.job_types else None
    acquirer = None
    if resolver is not None:
        from app.external_acquisition import BoundedExternalAcquirer, analyze_exact_job, reserve_production_tail
        from app.external_media import ExternalMediaService
        from app.external_media_connection import configured_external_sources, external_provider
        from app.web_discovery_connection import configured_web_providers, web_provider, web_provider_connections
        from app.web_discovery import WebDiscoveryService
        analysis = AssetAnalysisJobHandler(targets, assets,
            MediaAnalysisPipeline(db, FFmpegSceneDetector(config.ffmpeg), extractor))
        local_runner = JobRunner(store, {JobType.ANALYZE_ASSET: analysis},
            worker_id=f"{config.worker_id}:external-analysis", lease_duration=timedelta(seconds=config.lease_seconds),
            heartbeat_interval=timedelta(seconds=config.heartbeat_seconds), max_attempts=1)
        def external_service(source, project, *, after_master=False, remaining_talking_calls=1):
            return ExternalMediaService(db, config.data_root, external_provider(source, project),
                MediaImporter(db, config.data_root, FFProbeAdapter(config.ffprobe)),
                reservation_guard=lambda operation: reserve_production_tail(db, project, operation,
                    revision_reserve_calls=1+resolver.max_analysis_batches,after_master=after_master,
                    remaining_talking_calls=remaining_talking_calls))
        web_capabilities = {value["id"]: value for value in web_provider_connections()}
        def web_service(source, project, *, after_master=False, remaining_talking_calls=1):
            return WebDiscoveryService(db, web_provider(source),
                supports_fetch=web_capabilities[source]["fetch"],
                reservation_guard=lambda operation: reserve_production_tail(db, project, operation,
                    revision_reserve_calls=1+resolver.max_analysis_batches,after_master=after_master,
                    remaining_talking_calls=remaining_talking_calls))
        acquirer = BoundedExternalAcquirer(db, config.data_root, configured_external_sources(), external_service,
            lambda job_id: analyze_exact_job(db, local_runner, job_id),
            web_sources=configured_web_providers(), web_service_factory=web_service)
    planner=None
    if JobType.PLAN_CONTENT in config.job_types:
        from app.scene_planning import _scene_planner_from_env
        planner=_scene_planner_from_env()
    planner_estimator=UnknownProviderCostEstimator()
    automatic=VideoProductionService(db,config.data_root,voice_provider=voice_provider,talking_provider=talking_provider,
        material_resolver=resolver, material_acquirer=acquirer,planner=planner,planner_cost_estimator=planner_estimator)
    if planner is not None:
        handlers[JobType.PLAN_CONTENT]=ContentPlanJobHandler(automatic,planner,planner_estimator)
    def completed(job):
        service=ProductionRunService(db,config.data_root)
        if job.type is JobType.GENERATE_VOICE:
            service.reconcile_completed_voice_job(job.id,qa_worker_ready=JobType.VERIFY_VOICE in handlers)
        elif job.type is JobType.GENERATE_TALKING:
            service.reconcile_completed_talking_job(job.id,qa_worker_ready=JobType.VERIFY_TALKING in handlers)
        elif job.type is JobType.VERIFY_TALKING:
            service.reconcile_completed_talking_qa_job(job.id)
        automatic.completed(job)
    return JobRunner(
        store, handlers, worker_id=config.worker_id,
        lease_duration=timedelta(seconds=config.lease_seconds),
        heartbeat_interval=timedelta(seconds=config.heartbeat_seconds), max_attempts=config.max_attempts,
        resource_keys_by_type=resource_keys,
        on_completed=completed,
    )


def _build_voice_provider(config: WorkerConfig, db: Database) -> OmniVoiceProvider:
    """Build the configured local clone runtime for ordinary Voice Jobs."""
    provider_kind = os.environ.get("CONTENT_OS_VOICE_PROVIDER", "").strip().lower()
    if provider_kind not in {"omnivoice", "omnivoice-maintained"}:
        raise VoiceConfigurationError(
            "no Voice provider is configured; select omnivoice or omnivoice-maintained and its runtime paths before running Voice jobs"
        )

    maintained_options = None
    if provider_kind == "omnivoice-maintained":
        from app.providers.maintained_voice_provider import read_maintained_voice_options
        maintained_options = read_maintained_voice_options(
            os.environ.get("CONTENT_OS_MAINTAINED_VOICE_CONFIG", ""), config.data_root)

    def required_file(name: str) -> Path:
        value = os.environ.get(name, "").strip()
        if not value:
            raise VoiceConfigurationError(f"{name} must be supplied for the selected Voice provider")
        path = Path(value)
        if not path.is_file():
            raise VoiceConfigurationError(f"{name} does not point to a local file")
        return path

    runtime = (maintained_options["environment"] / "Scripts/python.exe" if maintained_options is not None
               else required_file("CONTENT_OS_OMNIVOICE_PYTHON"))
    model_value = (str(maintained_options["model_root"]) if maintained_options is not None
                   else os.environ.get("CONTENT_OS_OMNIVOICE_MODEL", "").strip())
    if not model_value or not Path(model_value).exists():
        raise VoiceConfigurationError("CONTENT_OS_OMNIVOICE_MODEL must point to a local model snapshot")
    model = Path(model_value)
    try:
        num_step = (maintained_options["parameters"]["num_step"] if maintained_options is not None
                    else int(os.environ.get("CONTENT_OS_OMNIVOICE_NUM_STEP", "32")))
        speed = (maintained_options["parameters"]["speed"] if maintained_options is not None
                 else float(os.environ.get("CONTENT_OS_OMNIVOICE_SPEED", "1.0")))
        timeout_seconds = (maintained_options["timeout_seconds"] if maintained_options is not None
                           else float(os.environ.get("CONTENT_OS_OMNIVOICE_TIMEOUT_SECONDS", "900")))
    except ValueError as exc:
        raise VoiceConfigurationError("OmniVoice step, speed and timeout settings must be numeric") from exc
    if num_step < 1 or not math.isfinite(speed) or speed <= 0 or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise VoiceConfigurationError("OmniVoice step, speed and timeout settings must be positive")
    # Freeze effective configuration before reservation. Later ambient env
    # edits must not change the actual command behind its execution receipt.
    device = (maintained_options["parameters"]["device"] if maintained_options is not None
              else os.environ.get("CONTENT_OS_OMNIVOICE_DEVICE", "cpu").strip())
    if not re.fullmatch(r"cpu|cuda(?::[0-9]+)?", device):
        raise VoiceConfigurationError("CONTENT_OS_OMNIVOICE_DEVICE must be an explicit CPU or CUDA device")
    clips = ClipRepository(db)
    assets = AssetRepository(db)
    ffmpeg = Path(config.ffmpeg)

    def reference_choices(profile: object):
        reference_ids = getattr(profile, "reference_clip_ids", ())
        referenced = [clips.get(item) for item in reference_ids]
        available = [item for item in referenced if item is not None]
        resolved_assets = {asset.id: asset for asset in (assets.get(clip.asset_id) for clip in available) if asset is not None}
        try:
            return authorized_voice_reference_choices(profile, available, resolved_assets, data_root=config.data_root)
        except VoicePerformancePlanningError as exc:
            raise VoiceInputError(str(exc)) from exc

    def provider_language(language: str | None) -> str | None:
        # The public contract uses compact language codes.  OmniVoice accepts
        # them, but its current CLI/model path is materially more reliable
        # when Chinese/English are passed as full language names.
        normalized = language.strip().lower() if isinstance(language, str) and language.strip() else None
        return {"zh": "Chinese", "en": "English"}.get(normalized, language.strip() if normalized else None)

    def synthesize(profile: object, text: str, target: Path, language: str | None, selection: VoiceReferenceWindowSelection | None = None) -> Path:
        choices = reference_choices(profile)
        if not choices:
            raise VoiceInputError("OmniVoice requires a user-confirmed complete reference before inference")
        if selection is None:
            selection, window = choices[0]
        else:
            if not isinstance(selection, VoiceReferenceWindowSelection):
                raise VoiceInputError("selected Voice reference is invalid")
            try:
                window = resolve_authorized_voice_reference_choice(selection, choices)
            except VoicePerformancePlanningError as exc:
                raise VoiceInputError(str(exc)) from exc
        source_path = Path(window.source_file)
        if not source_path.is_file():
            raise VoiceInputError("OmniVoice reference Clip points to unavailable media")
        digest = hashlib.sha256()
        with source_path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != selection.source_content_hash:
            raise VoiceInputError("selected Voice reference source changed since authorization")
        reference_start_ms, reference_end_ms, reference_text = window.start_ms, window.end_ms, window.transcript
        resolved_language = provider_language(language or getattr(profile, "language", None))
        target.parent.mkdir(parents=True, exist_ok=True)
        if maintained_options is not None:
            from app.providers.maintained_voice_provider import synthesize_maintained_voice,read_generation_segments
            path=synthesize_maintained_voice(options=maintained_options, data_root=config.data_root,
                ffmpeg=ffmpeg, window=window, selection=selection, text=text, target=target,
                language=resolved_language or "Chinese")
            return VoiceSynthesisResult(path,generation_segments=read_generation_segments(path,text,config.data_root))
        with tempfile.TemporaryDirectory(prefix="content-os-omnivoice-", dir=str(target.parent)) as temporary:
            reference_audio = Path(temporary) / "reference.wav"
            try:
                extraction = subprocess.run(
                    reference_extraction_command(ffmpeg, source_path, reference_audio, reference_start_ms, reference_end_ms,
                        canonical=window.source_timebase == "analysis_wav", source_timebase=window.source_timebase),
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                raise VoiceTimeout("reference audio extraction timed out") from exc
            if extraction.returncode != 0 or not reference_audio.is_file() or reference_audio.stat().st_size == 0:
                raise VoiceProviderResponseError("OmniVoice reference audio extraction failed")
            command = [
                str(runtime), "-m", "omnivoice.cli.infer", "--model", str(model), "--text", text,
                "--ref_audio", str(reference_audio), "--ref_text", reference_text, "--output", str(target),
                "--language", resolved_language or "Chinese", "--num_step", str(num_step),
                "--speed", str(speed), "--device", device,
            ]
            try:
                completed = subprocess.run(
                    command,
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                raise VoiceTimeout("local OmniVoice inference timed out") from exc
            except OSError as exc:
                raise VoiceConnectionError("local OmniVoice runtime could not be started") from exc
            if completed.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
                raise VoiceProviderResponseError("local OmniVoice inference failed")
        return target

    provider = OmniVoiceProvider(model=str(model), synthesizer=synthesize, reference_synthesizer=synthesize)
    provider.repair_execution = ({"runtime": maintained_options["runtime_label"],
        "machine_id": maintained_options["machine_id"], "parameters": maintained_options["parameters"]}
        if maintained_options is not None else {"runtime": os.environ.get("CONTENT_OS_VOICE_RUNTIME_ID"),
        "machine_id": os.environ.get("CONTENT_OS_VOICE_MACHINE_ID"),
        "parameters": {"runtime_python": str(runtime), "num_step": num_step, "speed": speed, "timeout_seconds": timeout_seconds,
            "device": device}})
    return provider


def _build_talking_provider(config: WorkerConfig) -> LatentSyncProvider:
    """Build an explicitly selected optional Talking provider from runtime env."""
    provider_kind = os.environ.get("CONTENT_OS_TALKING_PROVIDER", "").strip().lower()
    if provider_kind == "maintained-latentsync":
        from app.providers.maintained_talking import configured_maintained_talking_provider
        return configured_maintained_talking_provider(config, os.environ)
    if not provider_kind:
        raise TalkingConfigurationError(
            "no Talking provider is currently admitted; set CONTENT_OS_TALKING_PROVIDER=latentsync and its runtime paths before running Talking jobs"
        )
    if provider_kind != "latentsync":
        raise TalkingConfigurationError("CONTENT_OS_TALKING_PROVIDER must be latentsync in this revision")

    def required(name: str) -> str:
        value = os.environ.get(name, "").strip()
        if not value:
            raise TalkingConfigurationError(f"{name} must be supplied for the selected Talking provider")
        return value

    def optional_int(name: str, default: int) -> int:
        value = os.environ.get(name)
        if value is None or not value.strip():
            return default
        try:
            return int(value)
        except ValueError as exc:
            raise TalkingConfigurationError(f"{name} must be an integer") from exc

    def optional_float(name: str, default: float) -> float:
        value = os.environ.get(name)
        if value is None or not value.strip():
            return default
        try:
            return float(value)
        except ValueError as exc:
            raise TalkingConfigurationError(f"{name} must be a number") from exc

    return LatentSyncProvider(
        required("CONTENT_OS_LATENTSYNC_PYTHON"),
        required("CONTENT_OS_LATENTSYNC_REPO"),
        required("CONTENT_OS_LATENTSYNC_CHECKPOINT"),
        model=os.environ.get("CONTENT_OS_TALKING_MODEL", "LatentSync-1.5").strip() or "LatentSync-1.5",
        runner_path=os.environ.get("CONTENT_OS_LATENTSYNC_RUNNER") or None,
        unet_config_path=os.environ.get("CONTENT_OS_LATENTSYNC_UNET_CONFIG") or None,
        # The bundled Remotion FFmpeg is sufficient for most Core media work,
        # but the LatentSync post-process needs the provider runtime's filter
        # set (notably ``tpad``). Keep the override at the worker boundary so
        # the Core/provider contract remains vendor-neutral.
        ffmpeg_command=os.environ.get("CONTENT_OS_LATENTSYNC_FFMPEG") or config.ffmpeg,
        inference_steps=optional_int("CONTENT_OS_LATENTSYNC_STEPS", 20),
        guidance_scale=optional_float("CONTENT_OS_LATENTSYNC_GUIDANCE_SCALE", 1.5),
        seed=optional_int("CONTENT_OS_LATENTSYNC_SEED", 1247),
        timeout_seconds=optional_float("CONTENT_OS_LATENTSYNC_TIMEOUT_SECONDS", 900.0),
    )




def _run_production_run(config: WorkerConfig, db: Database, runner: JobRunner, stop: Event) -> int:
    """Use existing exact-Job claims and completion callbacks within one Run."""
    assert config.production_run_id is not None
    row, jobs = _production_run_jobs(db, config.production_run_id)
    if row["stage"] == "cancelled":
        return 0
    if any(job.status in {JobStatus.FAILED, JobStatus.CANCELLED}
           or job.status is JobStatus.PENDING and job.attempt > 0 for job in jobs):
        return 1
    # Recover a completion callback interrupted by process shutdown. These
    # methods reconcile only the exact referenced Job, never the legacy backlog.
    service = ProductionRunService(db, config.data_root)
    for job in jobs:
        if job.status is not JobStatus.COMPLETED:
            continue
        if job.type is JobType.GENERATE_VOICE:
            service.reconcile_completed_voice_job(job.id, qa_worker_ready=JobType.VERIFY_VOICE in runner.handler_types)
        elif job.type is JobType.GENERATE_TALKING:
            service.reconcile_completed_talking_job(job.id, qa_worker_ready=JobType.VERIFY_TALKING in runner.handler_types)
        elif job.type is JobType.VERIFY_TALKING:
            service.reconcile_completed_talking_qa_job(job.id)
    while not stop.is_set():
        row, jobs = _production_run_jobs(db, config.production_run_id)
        if row["stage"] == "cancelled":
            return 0
        if any(job.status in {JobStatus.FAILED, JobStatus.CANCELLED}
               or job.status is JobStatus.PENDING and job.attempt > 0 for job in jobs):
            return 1
        if row["stage"] == "render" and any(
            job.type is JobType.RENDER and job.status is JobStatus.COMPLETED for job in jobs
        ):
            return 0
        processed = False
        for job in jobs:
            if job.type not in runner.handler_types or job.status not in {JobStatus.PENDING, JobStatus.RUNNING}:
                continue
            result = runner.run_once(allowed_types=runner.handler_types, job_id=job.id)
            if result is None:
                # An expired targeted lease reaches the single-attempt ceiling
                # in JobStore without repeating provider execution.
                latest = JobRepository(db).get(job.id)
                if latest is not None and latest.status in {JobStatus.FAILED, JobStatus.CANCELLED}:
                    return 1
                continue
            if result.status is JobStatus.FAILED:
                return 1
            if config.once:
                return 0
            processed = True
            break  # Reopen references: the callback may have queued QA or Render.
        if config.once:
            return 0
        if not processed:
            stop.wait(config.poll_seconds)
    return 0


def _run_product_queue(config,db,runner,stop):
    """常驻普通生产 Worker；不取旧孤立生成任务，不重试失败调用。"""
    from app.video_production import IN_PROGRESS_REASONS
    while not stop.is_set():
        candidates=[]
        for row in db.connection.execute("SELECT id FROM jobs WHERE json_extract(payload,'$.type')='plan_content' AND status IN ('pending','running') ORDER BY created_at"):
            candidates.append(JobRepository(db).get(UUID(row[0])))
        for row in db.connection.execute("SELECT * FROM production_runs WHERE stage IN ('voice','awaiting_talking','render')").fetchall():
            if row["render_job_id"]:
                render=JobRepository(db).get(UUID(row["render_job_id"]))
                if render and render.status is JobStatus.COMPLETED:continue
            pending=IN_PROGRESS_REASONS
            reasons=json.loads(row["waiting_stop_reasons"])
            if (any(not value.startswith(pending) and value != 'planned_talking_qa_failed' for value in reasons)
                or json.loads(row["voice_qa_auto_stop_reasons"])):continue
            if row["start_job_id"]:
                parent=JobRepository(db).get(UUID(row["start_job_id"]))
                if parent and parent.status in {JobStatus.FAILED,JobStatus.CANCELLED}:continue
            _,jobs=_production_run_jobs(db,UUID(row["id"]))
            if any(job.status in {JobStatus.FAILED,JobStatus.CANCELLED} for job in jobs):
                if not row['automatic'] or row['repair_allowance'] <= 0:continue
                from app.production_runs import ProductionRunNotReady,ProductionRunConflict
                if row['stage']=='voice' and {JobType.GENERATE_VOICE,JobType.VERIFY_VOICE}<=runner.handler_types:
                    from app.voice_repair import VoiceRepairService
                    repair=VoiceRepairService(db,config.data_root)
                elif {JobType.GENERATE_TALKING,JobType.VERIFY_TALKING}<=runner.handler_types:
                    from app.talking.repair import TalkingRepairService
                    repair=TalkingRepairService(db,config.data_root)
                else:continue
                try:
                    repaired=repair.reconcile_automatic_qa_failure(UUID(row['project_id']),UUID(row['id']))
                except (ProductionRunNotReady,ProductionRunConflict,LookupError):continue
                if repaired is None:continue
                if 'planned_talking_qa_failed' in reasons:
                    with db.transaction(immediate=True):
                        current=db.connection.execute('SELECT waiting_stop_reasons FROM production_runs WHERE id=?',(row['id'],)).fetchone()
                        kept=[value for value in json.loads(current[0]) if value != 'planned_talking_qa_failed']
                        db.connection.execute('UPDATE production_runs SET waiting_stop_reasons=? WHERE id=?',(json.dumps(kept),row['id']))
                    if any(not value.startswith(pending) for value in kept):continue
                _,jobs=_production_run_jobs(db,UUID(row['id']))
            elif 'planned_talking_qa_failed' in reasons:
                continue
            for job in jobs:
                if job.status is JobStatus.COMPLETED:
                    runner.reconcile_completed(job)
            _,jobs=_production_run_jobs(db,UUID(row["id"]))
            candidates.extend(jobs)
        processed=False
        for job in candidates:
            if job.type in runner.handler_types and job.status in {JobStatus.PENDING,JobStatus.RUNNING}:
                result=runner.run_once(allowed_types=runner.handler_types,job_id=job.id)
                if result is not None:
                    processed=True
                    break
        if not processed:
            media_types=runner.handler_types & {JobType.ANALYZE_ASSET,JobType.TRANSCRIBE_AUDIO,JobType.TRANSCRIBE_AUDIO_ASSET,JobType.INDEX_CLIPS}
            if media_types:
                processed=runner.run_once(allowed_types=media_types) is not None
        if config.once:return 0
        if not processed:stop.wait(config.poll_seconds)
    return 0


def run(config: WorkerConfig, *, stop_event: Event | None = None) -> int:
    if config.job_id is not None and not config.once:
        raise ValueError("--job-id requires --once")
    if config.job_id is not None and config.production_run_id is not None:
        raise ValueError("--job-id and --production-run-id are mutually exclusive")
    if config.production_run_id is not None or config.production_only:
        config = replace(config, max_attempts=1)
    stop = stop_event or Event()
    db = Database(config.db_path)
    db.data_root = Path(config.data_root).resolve()
    try:
        runner = build_runner(config, db)
        if config.production_only:
            return _run_product_queue(config,db,runner,stop)
        if config.production_run_id is not None:
            return _run_production_run(config, db, runner, stop)
        if config.job_id is not None:
            # Targeted claim can reclaim this Job's expired lease itself.
            # Leave unrelated backlog and bulk recovery/reconciliation alone.
            runner.run_once(allowed_types=runner.handler_types, job_id=config.job_id)
            return 0
        if {JobType.GENERATE_VOICE, JobType.VERIFY_VOICE} & set(config.job_types):
            ProductionRunService(db, config.data_root).reconcile_completed_voice_jobs_once(
                qa_worker_ready=JobType.VERIFY_VOICE in runner.handler_types,
            )
        if {JobType.GENERATE_TALKING, JobType.VERIFY_TALKING} & set(config.job_types):
            ProductionRunService(db, config.data_root).reconcile_completed_talking_jobs_once(
                qa_worker_ready=JobType.VERIFY_TALKING in runner.handler_types,
            )
            ProductionRunService(db, config.data_root).reconcile_completed_talking_qa_jobs_once()
        if config.once:
            runner.recover_expired()
            runner.run_once(allowed_types=runner.handler_types)
        else:
            JobWorker(runner, idle_interval=timedelta(seconds=config.poll_seconds)).run_forever(stop)
        return 0
    finally:
        db.close()


def main(argv: Sequence[str] | None = None) -> int:
    config = parse_config(argv)
    stop = Event()

    def request_stop(signum: int, frame: object) -> None:
        stop.set()

    previous_int = signal.signal(signal.SIGINT, request_stop)
    previous_term = signal.signal(getattr(signal, "SIGTERM", signal.SIGINT), request_stop)
    try:
        return run(config, stop_event=stop)
    finally:
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(getattr(signal, "SIGTERM", signal.SIGINT), previous_term)


if __name__ == "__main__":
    raise SystemExit(main())
