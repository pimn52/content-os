from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID
from types import SimpleNamespace

import pytest

from app.db import AssetRepository, ClipRepository, Database
from app.domain.models import Asset, Clip, Job, JobStatus, JobType, RationalFps
from app.jobs import JobRunner, JobStore
from app.jobs.handlers import AssetAnalysisJobHandler, AssetTranscriptionJobHandler, AssetVisionJobHandler
from app.jobs.targets import AssetJobTargetStore
from app.media.extraction import AudioExtraction
from app.media.segmentation import FFmpegBinaryMissing, FFmpegTimeout
from app.media.transcripts import ClipTranscriptPersistence
from app.providers.asr import ASRHTTPError, ASRRateLimitError, TranscriptionResult, TranscriptionSegment
from app.providers.vision import VisionHTTPError, VisionRateLimitError
from app.providers.embedding import EmbeddingRateLimitError


def _asset(db: Database, tmp_path: Path, *, has_audio: bool) -> Asset:
    source = tmp_path / "asset.mp4"
    source.write_bytes(b"source")
    asset = Asset(
        source_file=str(source),
        content_hash=("a" if has_audio else "b") * 64,
        duration_ms=2_000,
        width=10,
        height=10,
        fps=RationalFps(numerator=25, denominator=1),
        has_audio=has_audio,
        authorization_reference="rights",
        imported_at=datetime.now(timezone.utc),
    )
    AssetRepository(db).create(asset)
    return asset


def _job(kind: JobType, key: str) -> Job:
    now = datetime.now(timezone.utc)
    return Job(type=kind, idempotency_key=key, created_at=now, updated_at=now)


def _runner(store: JobStore, handlers: dict[JobType, object]) -> JobRunner:
    return JobRunner(
        store,
        handlers,  # type: ignore[arg-type]
        worker_id="handler-worker",
        lease_duration=timedelta(minutes=1),
        max_attempts=2,
    )


def test_analysis_handler_completes_and_does_not_hold_claim_transaction(tmp_path: Path) -> None:
    db = Database(tmp_path / "handlers.sqlite")
    try:
        asset = _asset(db, tmp_path, has_audio=False)
        target_store = AssetJobTargetStore(db)
        job = target_store.enqueue(_job(JobType.ANALYZE_ASSET, "analysis"), asset.id)
        seen: list[UUID] = []

        class Pipeline:
            def process(self, candidate: Asset) -> object:
                assert not db.connection.in_transaction
                seen.append(candidate.id)
                return object()

        result = _runner(
            JobStore(db),
            {JobType.ANALYZE_ASSET: AssetAnalysisJobHandler(target_store, AssetRepository(db), Pipeline())},  # type: ignore[arg-type]
        ).run_once()

        assert result is not None and result.status is JobStatus.COMPLETED
        assert seen == [asset.id]
        assert JobStore(db).get(job.id).status is JobStatus.COMPLETED  # type: ignore[union-attr]
    finally:
        db.close()


@pytest.mark.parametrize(
    ("failure", "expected_status", "expected_code"),
    [
        (FFmpegTimeout("private ffmpeg output"), JobStatus.PENDING, "media_analysis_timeout"),
        (FFmpegBinaryMissing("C:/private/path/ffmpeg.exe"), JobStatus.FAILED, "media_analysis_invalid"),
    ],
)
def test_analysis_handler_classifies_segmentation_failures_safely(
    tmp_path: Path,
    failure: Exception,
    expected_status: JobStatus,
    expected_code: str,
) -> None:
    db = Database(tmp_path / "segmentation.sqlite")
    try:
        asset = _asset(db, tmp_path, has_audio=False)
        target_store = AssetJobTargetStore(db)
        target_store.enqueue(_job(JobType.ANALYZE_ASSET, f"segmentation-{expected_code}"), asset.id)

        class Pipeline:
            def process(self, _: Asset) -> object:
                raise failure

        result = _runner(
            JobStore(db),
            {JobType.ANALYZE_ASSET: AssetAnalysisJobHandler(target_store, AssetRepository(db), Pipeline())},  # type: ignore[arg-type]
        ).run_once()

        assert result is not None
        assert result.status is expected_status
        assert result.error_code == expected_code
        assert "private" not in (result.error_message or "")
        assert "ffmpeg.exe" not in (result.error_message or "")
    finally:
        db.close()


def test_transcription_handler_rate_limit_retries_outside_transaction(tmp_path: Path) -> None:
    db = Database(tmp_path / "retry.sqlite")
    try:
        asset = _asset(db, tmp_path, has_audio=True)
        job = AssetJobTargetStore(db).enqueue(_job(JobType.TRANSCRIBE_AUDIO, "retry"), asset.id)
        audio = tmp_path / "analysis.wav"
        audio.write_bytes(b"audio")

        class Extractor:
            def extract_audio(self, candidate: Asset) -> AudioExtraction:
                assert candidate.id == asset.id
                assert not db.connection.in_transaction
                return AudioExtraction(audio)

        class Provider:
            def transcribe(self, audio_path: Path, **_: object) -> TranscriptionResult:
                assert audio_path == audio
                assert not db.connection.in_transaction
                raise ASRRateLimitError("safe rate limited")

        class Persistence:
            def apply(self, asset_id: UUID, segments: object) -> object:
                raise AssertionError("rate-limited ASR result must not persist")

        result = _runner(
            JobStore(db),
            {
                JobType.TRANSCRIBE_AUDIO: AssetTranscriptionJobHandler(
                    AssetJobTargetStore(db), AssetRepository(db), Extractor(), Provider(), Persistence()
                ),
            },
        ).run_once()

        assert result is not None and result.status is JobStatus.PENDING
        assert result.error_code == "asr_temporarily_unavailable"
        assert JobStore(db).get(job.id).attempt == 1  # type: ignore[union-attr]
    finally:
        db.close()


@pytest.mark.parametrize(
    ("status_code", "expected_status", "expected_error"),
    [
        (503, JobStatus.PENDING, "asr_temporarily_unavailable"),
        (400, JobStatus.FAILED, "asr_request_rejected"),
    ],
)
def test_transcription_handler_classifies_generic_asr_http_status(
    tmp_path: Path,
    status_code: int,
    expected_status: JobStatus,
    expected_error: str,
) -> None:
    db = Database(tmp_path / f"http-{status_code}.sqlite")
    try:
        asset = _asset(db, tmp_path, has_audio=True)
        AssetJobTargetStore(db).enqueue(_job(JobType.TRANSCRIBE_AUDIO, f"http-{status_code}"), asset.id)
        audio = tmp_path / "analysis.wav"
        audio.write_bytes(b"audio")

        class Extractor:
            def extract_audio(self, _: Asset) -> AudioExtraction:
                return AudioExtraction(audio)

        class Provider:
            def transcribe(self, _: Path, **__: object) -> TranscriptionResult:
                raise ASRHTTPError(status_code)

        class Persistence:
            def apply(self, asset_id: UUID, segments: object) -> object:
                raise AssertionError("failed ASR response must not persist")

        result = _runner(
            JobStore(db),
            {
                JobType.TRANSCRIBE_AUDIO: AssetTranscriptionJobHandler(
                    AssetJobTargetStore(db), AssetRepository(db), Extractor(), Provider(), Persistence()
                ),
            },
        ).run_once()
        assert result is not None
        assert result.status is expected_status
        assert result.error_code == expected_error
    finally:
        db.close()


def test_transcription_handler_no_audio_and_missing_target_terminally_fail(tmp_path: Path) -> None:
    db = Database(tmp_path / "terminal.sqlite")
    try:
        silent = _asset(db, tmp_path, has_audio=False)
        target_store = AssetJobTargetStore(db)
        no_audio = target_store.enqueue(_job(JobType.TRANSCRIBE_AUDIO, "silent"), silent.id)

        class MustNotExtract:
            def extract_audio(self, asset: Asset) -> AudioExtraction:
                raise AssertionError("silent asset must not reach extraction")

        handler = AssetTranscriptionJobHandler(
            target_store, AssetRepository(db), MustNotExtract(), object(), object()  # type: ignore[arg-type]
        )
        first = _runner(JobStore(db), {JobType.TRANSCRIBE_AUDIO: handler}).run_once()
        assert first is not None and first.status is JobStatus.FAILED
        assert first.error_code == "asset_has_no_audio"
        assert JobStore(db).get(no_audio.id).status is JobStatus.FAILED  # type: ignore[union-attr]

        missing_target = JobStore(db).enqueue(_job(JobType.TRANSCRIBE_AUDIO, "no-target"))
        second = _runner(JobStore(db), {JobType.TRANSCRIBE_AUDIO: handler}).run_once()
        assert second is not None and second.id == missing_target.id
        assert second.status is JobStatus.FAILED
        assert second.error_code == "asset_target_missing"
    finally:
        db.close()


def test_handler_terminalizes_claimed_job_of_the_wrong_type(tmp_path: Path) -> None:
    db = Database(tmp_path / "type.sqlite")
    try:
        asset = _asset(db, tmp_path, has_audio=True)
        target_store = AssetJobTargetStore(db)
        target_store.enqueue(_job(JobType.ANALYZE_ASSET, "wrong-type"), asset.id)
        handler = AssetTranscriptionJobHandler(
            target_store, AssetRepository(db), object(), object(), object()  # type: ignore[arg-type]
        )

        result = _runner(JobStore(db), {JobType.ANALYZE_ASSET: handler}).run_once()

        assert result is not None and result.status is JobStatus.FAILED
        assert result.error_code == "job_type_mismatch"
    finally:
        db.close()


def test_transcription_handler_writes_asr_segments_to_clips(tmp_path: Path) -> None:
    db = Database(tmp_path / "transcripts.sqlite")
    try:
        asset = _asset(db, tmp_path, has_audio=True)
        clips = [
            Clip(asset_id=asset.id, start_ms=0, end_ms=1_000, asset_duration_ms=2_000),
            Clip(asset_id=asset.id, start_ms=1_000, end_ms=2_000, asset_duration_ms=2_000),
        ]
        repository = ClipRepository(db)
        for clip in clips:
            repository.create(clip)
        AssetJobTargetStore(db).enqueue(_job(JobType.TRANSCRIBE_AUDIO, "segments"), asset.id)
        audio = tmp_path / "analysis.wav"
        audio.write_bytes(b"audio")

        class Extractor:
            def extract_audio(self, _: Asset) -> AudioExtraction:
                assert not db.connection.in_transaction
                return AudioExtraction(audio)

        class Provider:
            def transcribe(self, _: Path, **__: object) -> TranscriptionResult:
                assert not db.connection.in_transaction
                return TranscriptionResult(
                    "first cross second",
                    (
                        TranscriptionSegment(100, 1_100, "first cross"),
                        TranscriptionSegment(1_200, 1_500, "second"),
                    ),
                )

        handler = AssetTranscriptionJobHandler(
            AssetJobTargetStore(db),
            AssetRepository(db),
            Extractor(),
            Provider(),
            ClipTranscriptPersistence(db),
        )
        result = _runner(JobStore(db), {JobType.TRANSCRIBE_AUDIO: handler}).run_once()

        assert result is not None and result.status is JobStatus.COMPLETED
        assert [clip.transcript for clip in repository.list_by_asset(asset.id)] == ["first cross", "first cross second"]
    finally:
        db.close()


def test_vision_handler_completes_with_ordered_local_keyframes(tmp_path: Path) -> None:
    db = Database(tmp_path / "vision-handler.sqlite")
    try:
        asset = _asset(db, tmp_path, has_audio=False)
        clips = [
            Clip(asset_id=asset.id, start_ms=0, end_ms=1_000, asset_duration_ms=2_000),
            Clip(asset_id=asset.id, start_ms=1_000, end_ms=2_000, asset_duration_ms=2_000),
        ]
        repository = ClipRepository(db)
        for clip in clips:
            repository.create(clip)
        AssetJobTargetStore(db).enqueue(_job(JobType.INDEX_CLIPS, "vision-success"), asset.id)
        keyframes = []
        for index in range(2):
            path = tmp_path / f"frame-{index}.jpg"
            path.write_bytes(b"frame")
            keyframes.append(str(path))
        seen = []

        class Resolver:
            def resolve(self, candidate: Asset, supplied: list[Clip]) -> list[str]:
                assert not db.connection.in_transaction
                assert candidate.id == asset.id
                assert [clip.id for clip in supplied] == [clip.id for clip in clips]
                return keyframes

        class Pipeline:
            def process(self, candidate: Asset, supplied: list[Clip], paths: list[str]) -> object:
                assert not db.connection.in_transaction
                seen.append((candidate.id, [clip.id for clip in supplied], paths))
                return object()

        handler = AssetVisionJobHandler(AssetJobTargetStore(db), AssetRepository(db), repository, Resolver(), Pipeline())  # type: ignore[arg-type]
        result = _runner(JobStore(db), {JobType.INDEX_CLIPS: handler}).run_once()
        assert result is not None and result.status is JobStatus.COMPLETED
        assert seen == [(asset.id, [clip.id for clip in clips], keyframes)]
    finally:
        db.close()


@pytest.mark.parametrize(
    ("failure", "expected_status", "expected_code"),
    [
        (VisionRateLimitError("private-key"), JobStatus.PENDING, "vision_temporarily_unavailable"),
        (VisionHTTPError(503), JobStatus.PENDING, "vision_temporarily_unavailable"),
        (VisionHTTPError(400), JobStatus.FAILED, "vision_request_rejected"),
    ],
)
def test_vision_handler_classifies_provider_failures_without_leaking_details(
    tmp_path: Path, failure: Exception, expected_status: JobStatus, expected_code: str
) -> None:
    db = Database(tmp_path / f"vision-{expected_code}-{expected_status.value}.sqlite")
    try:
        asset = _asset(db, tmp_path, has_audio=False)
        clip = Clip(asset_id=asset.id, start_ms=0, end_ms=2_000, asset_duration_ms=2_000)
        clips = ClipRepository(db)
        clips.create(clip)
        AssetJobTargetStore(db).enqueue(_job(JobType.INDEX_CLIPS, f"{expected_code}-{expected_status.value}"), asset.id)
        frame = tmp_path / "frame.jpg"
        frame.write_bytes(b"frame")

        class Resolver:
            def resolve(self, candidate: Asset, supplied: list[Clip]) -> list[str]:
                return [str(frame)]

        class Pipeline:
            def process(self, candidate: Asset, supplied: list[Clip], paths: list[str]) -> object:
                raise failure

        handler = AssetVisionJobHandler(AssetJobTargetStore(db), AssetRepository(db), clips, Resolver(), Pipeline())  # type: ignore[arg-type]
        result = _runner(JobStore(db), {JobType.INDEX_CLIPS: handler}).run_once()
        assert result is not None and result.status is expected_status
        assert result.error_code == expected_code
        assert "private-key" not in (result.error_message or "")
    finally:
        db.close()


def test_vision_handler_without_clips_fails_before_keyframe_resolution(tmp_path: Path) -> None:
    db = Database(tmp_path / "vision-no-clips.sqlite")
    try:
        asset = _asset(db, tmp_path, has_audio=False)
        AssetJobTargetStore(db).enqueue(_job(JobType.INDEX_CLIPS, "vision-no-clips"), asset.id)

        class MustNotResolve:
            def resolve(self, candidate: Asset, supplied: list[Clip]) -> list[str]:
                raise AssertionError("missing Clips must fail first")

        handler = AssetVisionJobHandler(
            AssetJobTargetStore(db), AssetRepository(db), ClipRepository(db), MustNotResolve(), object()  # type: ignore[arg-type]
        )
        result = _runner(JobStore(db), {JobType.INDEX_CLIPS: handler}).run_once()
        assert result is not None and result.status is JobStatus.FAILED
        assert result.error_code == "clips_missing"
    finally:
        db.close()


def test_vision_handler_passes_updated_clips_to_embedding_indexer(tmp_path: Path) -> None:
    db = Database(tmp_path / "vision-index.sqlite")
    try:
        asset = _asset(db, tmp_path, has_audio=False)
        clip = Clip(asset_id=asset.id, start_ms=0, end_ms=2_000, asset_duration_ms=2_000)
        clips = ClipRepository(db)
        clips.create(clip)
        AssetJobTargetStore(db).enqueue(_job(JobType.INDEX_CLIPS, "vision-index"), asset.id)
        frame = tmp_path / "frame.jpg"
        frame.write_bytes(b"frame")
        updated = clip.model_copy(update={"visual_description": "person using computer"})
        seen = []

        class Resolver:
            def resolve(self, candidate, supplied):
                return [str(frame)]

        class Pipeline:
            def process(self, candidate, supplied, paths):
                return SimpleNamespace(clips=(updated,))

        class Indexer:
            def index_clips(self, supplied):
                seen.extend(supplied)

        handler = AssetVisionJobHandler(
            AssetJobTargetStore(db), AssetRepository(db), clips, Resolver(), Pipeline(), Indexer()  # type: ignore[arg-type]
        )
        result = _runner(JobStore(db), {JobType.INDEX_CLIPS: handler}).run_once()
        assert result is not None and result.status is JobStatus.COMPLETED
        assert seen == [updated]
    finally:
        db.close()


def test_embedding_rate_limit_retries_without_leaking_provider_detail(tmp_path: Path) -> None:
    db = Database(tmp_path / "embedding-retry.sqlite")
    try:
        asset = _asset(db, tmp_path, has_audio=False)
        clip = Clip(asset_id=asset.id, start_ms=0, end_ms=2_000, asset_duration_ms=2_000)
        clips = ClipRepository(db)
        clips.create(clip)
        AssetJobTargetStore(db).enqueue(_job(JobType.INDEX_CLIPS, "embedding-retry"), asset.id)
        frame = tmp_path / "frame.jpg"
        frame.write_bytes(b"frame")

        class Resolver:
            def resolve(self, candidate, supplied):
                return [str(frame)]

        class Pipeline:
            def process(self, candidate, supplied, paths):
                return SimpleNamespace(clips=(clip,))

        class Indexer:
            def index_clips(self, supplied):
                raise EmbeddingRateLimitError("private embedding key")

        handler = AssetVisionJobHandler(
            AssetJobTargetStore(db), AssetRepository(db), clips, Resolver(), Pipeline(), Indexer()  # type: ignore[arg-type]
        )
        result = _runner(JobStore(db), {JobType.INDEX_CLIPS: handler}).run_once()
        assert result is not None and result.status is JobStatus.PENDING
        assert result.error_code == "embedding_temporarily_unavailable"
        assert "private" not in (result.error_message or "")
    finally:
        db.close()
