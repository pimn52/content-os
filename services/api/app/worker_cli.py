"""Command-line assembly and loop for the local Content OS worker."""
from __future__ import annotations

import argparse
import math
import os
import signal
import socket
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from threading import Event
from typing import Sequence

from app.db import AssetRepository, AudioAssetRepository, ClipRepository, Database, ImageAssetRepository, ProjectRepository, TalkingProfileRepository, VoiceProfileRepository
from app.budget import ProviderCallLedger
from app.domain.models import JobType
from app.jobs.handlers import AssetAnalysisJobHandler, AssetTranscriptionJobHandler, AssetVisionJobHandler, ExtractedKeyframeResolver, RenderVideoJobHandler, TalkingGenerationJobHandler, VoiceGenerationJobHandler, VoiceQaJobHandler
from app.jobs.runner import JobRunner
from app.jobs.store import JobStore
from app.jobs.targets import AssetJobTargetStore
from app.jobs.worker import JobWorker
from app.media.extraction import FFmpegExtractionService
from app.media.ffprobe import FFProbeAdapter
from app.media.audio_importer import AudioImporter
from app.media.importer import MediaImporter
from app.media.pipeline import MediaAnalysisPipeline
from app.media.segmentation import FFmpegSceneDetector
from app.media.transcripts import ClipTranscriptPersistence
from app.media.vision_pipeline import MediaVisionPipeline
from app.providers.asr import ASRConfigurationError, FasterWhisperASRProvider, OpenAICompatibleASRProvider
from app.providers.embedding import EmbeddingConfigurationError, OpenAICompatibleEmbeddingProvider
from app.providers.vision import OpenAICompatibleVisionProvider, VisionConfigurationError
from app.providers.latentsync import LatentSyncProvider
from app.providers.talking import TalkingConfigurationError
from app.providers.voice import OmniVoiceProvider, VoiceConfigurationError, VoiceConnectionError, VoiceInputError, VoiceProviderResponseError, VoiceTimeout
from app.search import ClipEmbeddingIndexer
from app.renderer import RemotionRenderer
from app.runtime import resolve_local_executable
from app.voice_performance import select_voice_reference_windows


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
    parser.add_argument("--worker-id", default=None)
    parser.add_argument("--lease-seconds", type=float, default=300.0)
    parser.add_argument("--heartbeat-seconds", type=float, default=30.0)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--job-type", dest="job_types", action="append", choices=[item.value for item in (JobType.ANALYZE_ASSET, JobType.TRANSCRIBE_AUDIO, JobType.INDEX_CLIPS, JobType.GENERATE_VOICE, JobType.VERIFY_VOICE, JobType.GENERATE_TALKING, JobType.RENDER)], help="Restrict handlers; repeat to select multiple")
    parser.add_argument("--gpu-resource-key", default=None, help="Serialize local Voice/Talking inference sharing this GPU (default gpu:<hostname>)")
    parser.add_argument("--ffmpeg", default=resolve_local_executable("ffmpeg"))
    parser.add_argument("--ffprobe", default=resolve_local_executable("ffprobe"))
    return parser


def parse_config(argv: Sequence[str] | None = None) -> WorkerConfig:
    parser = build_parser()
    args = parser.parse_args(argv)
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
    selected = tuple(JobType(value) for value in args.job_types) if args.job_types else (JobType.ANALYZE_ASSET, JobType.TRANSCRIBE_AUDIO)
    gpu_resource_key = args.gpu_resource_key or os.environ.get("CONTENT_OS_GPU_RESOURCE_KEY") or f"gpu:{socket.gethostname()}"
    if not isinstance(gpu_resource_key, str) or not gpu_resource_key.strip() or len(gpu_resource_key.strip()) > 500:
        parser.error("gpu-resource-key must be a non-empty string of at most 500 characters")
    return WorkerConfig(
        db_path, data_root, worker_id, args.lease_seconds, args.heartbeat_seconds,
        args.poll_seconds, args.max_attempts, args.once, selected, gpu_resource_key.strip(), args.ffmpeg, args.ffprobe,
    )


def build_runner(config: WorkerConfig, db: Database) -> JobRunner:
    store = JobStore(db)
    targets = AssetJobTargetStore(db)
    assets = AssetRepository(db)
    extractor = FFmpegExtractionService(config.data_root, command=config.ffmpeg)
    handlers = {}
    if JobType.ANALYZE_ASSET in config.job_types:
        detector = FFmpegSceneDetector(config.ffmpeg)
        pipeline = MediaAnalysisPipeline(db, detector, extractor)
        handlers[JobType.ANALYZE_ASSET] = AssetAnalysisJobHandler(targets, assets, pipeline)
    if JobType.TRANSCRIBE_AUDIO in config.job_types:
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
        handlers[JobType.TRANSCRIBE_AUDIO] = AssetTranscriptionJobHandler(
            targets,
            assets,
            extractor,
            provider,
            ClipTranscriptPersistence(db),
            ProviderCallLedger(db),
            provider_name=provider_name,
            provider_model=provider.model,
        )
    if JobType.INDEX_CLIPS in config.job_types:
        api_key = os.environ.get("CONTENT_OS_VISION_API_KEY") or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise VisionConfigurationError("vision API key must be supplied at runtime")
        provider = OpenAICompatibleVisionProvider(
            api_key,
            base_url=os.environ.get("CONTENT_OS_VISION_BASE_URL") or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            model=os.environ.get("CONTENT_OS_VISION_MODEL", "gpt-4.1-mini"),
            detail=os.environ.get("CONTENT_OS_VISION_DETAIL", "low"),
        )
        embedding_key = os.environ.get("CONTENT_OS_EMBEDDING_API_KEY") or os.environ.get("OPENAI_API_KEY")
        if not embedding_key:
            raise EmbeddingConfigurationError("embedding API key must be supplied at runtime")
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
        handlers[JobType.INDEX_CLIPS] = AssetVisionJobHandler(
            targets,
            assets,
            ClipRepository(db),
            ExtractedKeyframeResolver(extractor),
            MediaVisionPipeline(db, provider),
            ClipEmbeddingIndexer(db, embedding_provider),
            ProviderCallLedger(db),
            vision_model=provider.model,
            embedding_model=embedding_provider.model,
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
        handlers[JobType.GENERATE_VOICE] = VoiceGenerationJobHandler(
            VoiceProfileRepository(db),
            AudioAssetRepository(db),
            AudioImporter(db, config.data_root, FFProbeAdapter(config.ffprobe)),
            provider,
            config.data_root / "generated",
            ProviderCallLedger(db),
        )
    if JobType.VERIFY_VOICE in config.job_types:
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
        handlers[JobType.VERIFY_VOICE] = VoiceQaJobHandler(
            AudioAssetRepository(db), qa_provider, ProviderCallLedger(db),
            provider_name="faster-whisper", provider_model=qa_model,
            max_silence_ms=int(os.environ.get("CONTENT_OS_VOICE_QA_MAX_SILENCE_MS", "2000")),
            max_leading_silence_ms=int(os.environ.get("CONTENT_OS_VOICE_QA_MAX_LEADING_SILENCE_MS", "500")),
        )
    if JobType.GENERATE_TALKING in config.job_types:
        provider = _build_talking_provider(config)
        handlers[JobType.GENERATE_TALKING] = TalkingGenerationJobHandler(
            TalkingProfileRepository(db),
            AudioAssetRepository(db),
            AssetRepository(db),
            MediaImporter(db, config.data_root, FFProbeAdapter(config.ffprobe)),
            provider,
            config.data_root / "generated",
            ProviderCallLedger(db),
            ffmpeg_command=config.ffmpeg,
        )
    gpu_job_types = {JobType.GENERATE_VOICE, JobType.GENERATE_TALKING}
    resource_keys = {job_type: config.gpu_resource_key for job_type in config.job_types if job_type in gpu_job_types}
    return JobRunner(
        store, handlers, worker_id=config.worker_id,
        lease_duration=timedelta(seconds=config.lease_seconds),
        heartbeat_interval=timedelta(seconds=config.heartbeat_seconds), max_attempts=config.max_attempts,
        resource_keys_by_type=resource_keys,
    )


def _build_voice_provider(config: WorkerConfig, db: Database) -> OmniVoiceProvider:
    """Build the optional local OmniVoice benchmark provider from explicit paths."""
    provider_kind = os.environ.get("CONTENT_OS_VOICE_PROVIDER", "").strip().lower()
    if provider_kind != "omnivoice":
        raise VoiceConfigurationError(
            "no Voice provider is configured; set CONTENT_OS_VOICE_PROVIDER=omnivoice and its runtime paths before running Voice jobs"
        )

    def required_file(name: str) -> Path:
        value = os.environ.get(name, "").strip()
        if not value:
            raise VoiceConfigurationError(f"{name} must be supplied for the selected Voice provider")
        path = Path(value)
        if not path.is_file():
            raise VoiceConfigurationError(f"{name} does not point to a local file")
        return path

    runtime = required_file("CONTENT_OS_OMNIVOICE_PYTHON")
    model_value = os.environ.get("CONTENT_OS_OMNIVOICE_MODEL", "").strip()
    if not model_value or not Path(model_value).exists():
        raise VoiceConfigurationError("CONTENT_OS_OMNIVOICE_MODEL must point to a local model snapshot")
    model = Path(model_value)
    try:
        num_step = int(os.environ.get("CONTENT_OS_OMNIVOICE_NUM_STEP", "32"))
        speed = float(os.environ.get("CONTENT_OS_OMNIVOICE_SPEED", "1.0"))
        timeout_seconds = float(os.environ.get("CONTENT_OS_OMNIVOICE_TIMEOUT_SECONDS", "900"))
    except ValueError as exc:
        raise VoiceConfigurationError("OmniVoice step, speed and timeout settings must be numeric") from exc
    if num_step < 1 or not math.isfinite(speed) or speed <= 0 or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise VoiceConfigurationError("OmniVoice step, speed and timeout settings must be positive")

    clips = ClipRepository(db)
    assets = AssetRepository(db)
    ffmpeg = Path(config.ffmpeg)

    def reference_window(clip: object) -> tuple[int, int, str]:
        """Use one observed spoken interval as the cloning reference.

        Long ordinary clips are valid source material, but feeding an entire
        minute of speech to OmniVoice can cause it to replay a tail of the
        reference before starting the requested copy.  The provider boundary
        may derive a short window from the persisted ASR evidence; Core still
        stores the original Clip and transcript unchanged.
        """

        clip_start = int(getattr(clip, "start_ms"))
        clip_end = int(getattr(clip, "end_ms"))
        segments = getattr(clip, "transcript_segments", ())
        for segment in segments:
            start = getattr(segment, "start_ms", None)
            end = getattr(segment, "end_ms", None)
            text_value = getattr(segment, "text", None)
            if not isinstance(start, int) or not isinstance(end, int) or not isinstance(text_value, str):
                continue
            if start < clip_start or end > clip_end or end <= start or not text_value.strip():
                continue
            # A single clean sentence is a better reference than replaying a
            # whole source-led monologue.  Keep the original absolute timing.
            return start, end, text_value.strip()
        transcript = getattr(clip, "transcript", None)
        if not isinstance(transcript, str) or not transcript.strip():
            raise VoiceInputError("OmniVoice reference Clip needs a real transcript before Voice generation")
        return clip_start, clip_end, transcript.strip()

    def selected_reference_window(profile: object) -> tuple[Path, int, int, str]:
        reference_ids = getattr(profile, "reference_clip_ids", None)
        if not reference_ids:
            raise VoiceInputError("OmniVoice requires at least one consented reference Clip")
        referenced = [clips.get(item) for item in reference_ids]
        available = [item for item in referenced if item is not None]
        if not available:
            raise VoiceInputError("OmniVoice reference Clip needs a real transcript before Voice generation")
        policy = os.environ.get("CONTENT_OS_OMNIVOICE_REFERENCE_POLICY", "first_segment").strip().lower()
        if policy == "best_window":
            resolved_assets = {asset.id: asset for asset in (assets.get(clip.asset_id) for clip in available) if asset is not None}
            windows = select_voice_reference_windows(available, resolved_assets, data_root=config.data_root)
            if windows:
                raw_index = os.environ.get("CONTENT_OS_OMNIVOICE_REFERENCE_WINDOW_INDEX", "0").strip()
                try:
                    window_index = int(raw_index)
                except ValueError as exc:
                    raise VoiceConfigurationError("CONTENT_OS_OMNIVOICE_REFERENCE_WINDOW_INDEX must be a non-negative integer") from exc
                if window_index < 0 or window_index >= len(windows):
                    raise VoiceConfigurationError("CONTENT_OS_OMNIVOICE_REFERENCE_WINDOW_INDEX is outside available authorized windows")
                window = windows[window_index]
                return Path(window.source_file), window.start_ms, window.end_ms, window.transcript
        elif policy != "first_segment":
            raise VoiceConfigurationError("CONTENT_OS_OMNIVOICE_REFERENCE_POLICY must be first_segment or best_window")
        clip = available[0]
        asset = assets.get(clip.asset_id)
        if asset is None:
            raise VoiceInputError("OmniVoice reference Clip points to unavailable media")
        start_ms, end_ms, transcript = reference_window(clip)
        return _resolve_omnivoice_reference_path(asset.source_file, config.data_root), start_ms, end_ms, transcript

    def provider_language(language: str | None) -> str | None:
        # The public contract uses compact language codes.  OmniVoice accepts
        # them, but its current CLI/model path is materially more reliable
        # when Chinese/English are passed as full language names.
        normalized = language.strip().lower() if isinstance(language, str) and language.strip() else None
        return {"zh": "Chinese", "en": "English"}.get(normalized, language.strip() if normalized else None)

    def synthesize(profile: object, text: str, target: Path, language: str | None) -> Path:
        source_path, reference_start_ms, reference_end_ms, reference_text = selected_reference_window(profile)
        if not source_path.is_file():
            raise VoiceInputError("OmniVoice reference Clip points to unavailable media")
        resolved_language = provider_language(language or getattr(profile, "language", None))
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="content-os-omnivoice-", dir=str(target.parent)) as temporary:
            reference_audio = Path(temporary) / "reference.wav"
            try:
                extraction = subprocess.run(
                    [
                        str(ffmpeg), "-y", "-ss", f"{reference_start_ms / 1000:.3f}", "-i", str(source_path),
                        "-t", f"{(reference_end_ms - reference_start_ms) / 1000:.3f}", "-vn", "-ac", "1", "-ar", "24000",
                        "-c:a", "pcm_s16le", str(reference_audio),
                    ],
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
                "--speed", str(speed), "--device", os.environ.get("CONTENT_OS_OMNIVOICE_DEVICE", "cpu"),
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

    return OmniVoiceProvider(model=str(model), synthesizer=synthesize)


def _build_talking_provider(config: WorkerConfig) -> LatentSyncProvider:
    """Build an explicitly selected optional Talking provider from runtime env."""
    provider_kind = os.environ.get("CONTENT_OS_TALKING_PROVIDER", "").strip().lower()
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


def run(config: WorkerConfig, *, stop_event: Event | None = None) -> int:
    stop = stop_event or Event()
    db = Database(config.db_path)
    try:
        runner = build_runner(config, db)
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
