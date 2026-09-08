"""Command-line assembly and loop for the local Content OS worker."""
from __future__ import annotations

import argparse
import math
import os
import signal
import socket
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from threading import Event
from typing import Sequence

from app.db import AssetRepository, Database
from app.domain.models import JobType
from app.jobs.handlers import AssetAnalysisJobHandler, AssetTranscriptionJobHandler
from app.jobs.runner import JobRunner
from app.jobs.store import JobStore
from app.jobs.targets import AssetJobTargetStore
from app.jobs.worker import JobWorker
from app.media.extraction import FFmpegExtractionService
from app.media.pipeline import MediaAnalysisPipeline
from app.media.segmentation import FFmpegSceneDetector
from app.media.transcripts import ClipTranscriptPersistence
from app.providers.asr import ASRConfigurationError, OpenAICompatibleASRProvider


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
    ffmpeg: str


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
    parser.add_argument("--job-type", dest="job_types", action="append", choices=[item.value for item in (JobType.ANALYZE_ASSET, JobType.TRANSCRIBE_AUDIO)], help="Restrict handlers; repeat to select both")
    parser.add_argument("--ffmpeg", default=os.environ.get("CONTENT_OS_FFMPEG", "ffmpeg"))
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
    return WorkerConfig(db_path, data_root, worker_id, args.lease_seconds, args.heartbeat_seconds, args.poll_seconds, args.max_attempts, args.once, selected, args.ffmpeg)


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
        api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("CONTENT_OS_ASR_API_KEY")
        if not api_key:
            raise ASRConfigurationError("ASR API key must be supplied at runtime")
        provider = OpenAICompatibleASRProvider(
            api_key,
            base_url=os.environ.get("OPENAI_BASE_URL", os.environ.get("CONTENT_OS_ASR_BASE_URL", "https://api.openai.com/v1")),
            model=os.environ.get("OPENAI_MODEL", os.environ.get("CONTENT_OS_ASR_MODEL", "whisper-1")),
        )
        handlers[JobType.TRANSCRIBE_AUDIO] = AssetTranscriptionJobHandler(targets, assets, extractor, provider, ClipTranscriptPersistence(db))
    return JobRunner(store, handlers, worker_id=config.worker_id, lease_duration=timedelta(seconds=config.lease_seconds), heartbeat_interval=timedelta(seconds=config.heartbeat_seconds), max_attempts=config.max_attempts)


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
