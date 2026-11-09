from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID
from types import SimpleNamespace

import pytest

from app.budget import ProviderCallLedger
from app.db import AssetRepository, BudgetPolicyRepository, ClipRepository, Database, IPProfileRepository, ProjectRepository, ProviderCallRepository
from app.domain.models import Asset, BudgetPolicy, Clip, CostCategory, IPProfile, Job, JobStatus, JobType, Project, RationalFps, UsageCost
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


def test_transcription_provider_failure_is_reconciled_and_retry_gets_new_call_record(tmp_path: Path) -> None:
    db = Database(tmp_path / "metered-retry.sqlite")
    try:
        asset = _asset(db, tmp_path, has_audio=True)
        profile = IPProfile(creator_name="Creator")
        IPProfileRepository(db).create(profile)
        project = Project(
            ip_profile_id=profile.id,
            title="Metered transcription",
            topic="Budget boundary",
            fps=RationalFps(numerator=25, denominator=1),
            created_at=datetime.now(timezone.utc),
        )
        ProjectRepository(db).create(project)
        policy = BudgetPolicy(project_id=project.id, allow_unknown_cost=True, updated_at=datetime.now(timezone.utc))
        with db.transaction():
            BudgetPolicyRepository(db).save(policy)
        job = AssetJobTargetStore(db).enqueue(
            _job(JobType.TRANSCRIBE_AUDIO, "metered-retry").model_copy(update={"project_id": project.id}),
            asset.id,
        )
        audio = tmp_path / "analysis.wav"
        audio.write_bytes(b"audio")

        class Extractor:
            def extract_audio(self, _: Asset) -> AudioExtraction:
                return AudioExtraction(audio)

        class Provider:
            def transcribe(self, _: Path, **__: object) -> TranscriptionResult:
                raise ASRRateLimitError("safe rate limited")

        class Persistence:
            def apply(self, asset_id: UUID, segments: object) -> object:
                raise AssertionError("failed ASR result must not persist")

        handler = AssetTranscriptionJobHandler(
            AssetJobTargetStore(db),
            AssetRepository(db),
            Extractor(),
            Provider(),
            Persistence(),
            ProviderCallLedger(db),
        )
        runner = _runner(JobStore(db), {JobType.TRANSCRIBE_AUDIO: handler})
        first = runner.run_once()
        second = runner.run_once()
        assert first is not None and first.status is JobStatus.PENDING
        assert second is not None and second.status is JobStatus.FAILED
        records = ProviderCallRepository(db).list_for_project(project.id)
        assert len(records) == 2
        assert [record.status for record in records] == ["failed", "failed"]
        assert [record.idempotency_key for record in records][0] != [record.idempotency_key for record in records][1]
        assert JobStore(db).get(job.id).attempt == 2  # type: ignore[union-attr]
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
        persisted = repository.list_by_asset(asset.id)
        assert [clip.transcript for clip in persisted] == ["first cross", "first cross second"]
        assert {clip.transcript_source for clip in persisted} == {"openai-compatible:runtime-asr"}
    finally:
        db.close()


def test_local_asr_handler_uses_known_zero_cost_and_records_provenance(tmp_path: Path) -> None:
    db = Database(tmp_path / "local-asr.sqlite")
    try:
        asset = _asset(db, tmp_path, has_audio=True)
        clip = Clip(asset_id=asset.id, start_ms=0, end_ms=2_000, asset_duration_ms=2_000)
        ClipRepository(db).create(clip)
        profile = IPProfile(creator_name="Local ASR creator")
        IPProfileRepository(db).create(profile)
        project = Project(
            ip_profile_id=profile.id,
            title="Local ASR project",
            topic="real transcript",
            fps=RationalFps(numerator=25, denominator=1),
            created_at=datetime.now(timezone.utc),
        )
        ProjectRepository(db).create(project)
        budget = BudgetPolicy(project_id=project.id, allow_unknown_cost=True, updated_at=datetime.now(timezone.utc))
        BudgetPolicyRepository(db).save(budget)
        ProviderCallLedger(db).reserve(
            project_id=project.id,
            idempotency_key="pre-existing-unknown-call",
            operation="scene_planning",
            mode="runtime",
            provider="openai-compatible",
            model="planner",
            input_source="project:old",
            estimated_cost=UsageCost(category=CostCategory.LLM),
        )
        BudgetPolicyRepository(db).save(budget.model_copy(update={"allow_unknown_cost": False, "updated_at": datetime.now(timezone.utc)}))
        job = _job(JobType.TRANSCRIBE_AUDIO, "local-asr")
        job = job.model_copy(update={"project_id": project.id})
        AssetJobTargetStore(db).enqueue(job, asset.id)
        audio = tmp_path / "analysis.wav"
        audio.write_bytes(b"audio")

        class Extractor:
            def extract_audio(self, _: Asset) -> AudioExtraction:
                return AudioExtraction(audio)

        class Provider:
            model = "small"

            def transcribe(self, _: Path, **__: object) -> TranscriptionResult:
                return TranscriptionResult("real local text", (TranscriptionSegment(100, 1_900, "real local text"),), "en")

        handler = AssetTranscriptionJobHandler(
            AssetJobTargetStore(db),
            AssetRepository(db),
            Extractor(),
            Provider(),
            ClipTranscriptPersistence(db),
            ProviderCallLedger(db),
            provider_name="faster-whisper",
            provider_model="small",
        )
        result = _runner(JobStore(db), {JobType.TRANSCRIBE_AUDIO: handler}).run_once()

        assert result is not None and result.status is JobStatus.COMPLETED
        call = next(record for record in ProviderCallRepository(db).list_for_project(project.id) if record.provider == "faster-whisper")
        assert call.provider == "faster-whisper"
        assert call.estimated_cost.amount == 0
        assert call.estimated_cost.currency == "USD"
        assert ClipRepository(db).get(clip.id).transcript_source == "faster-whisper:small"  # type: ignore[union-attr]
    finally:
        db.close()


@pytest.mark.parametrize('scope_valid', [True, False])
@pytest.mark.parametrize('index_embeddings', [True, False])
def test_scoped_vision_handler_limits_extraction_indexing_and_metered_calls(tmp_path, scope_valid, index_embeddings):
    from uuid import uuid4
    from app.media.vision_pipeline import MediaVisionPipeline
    from app.providers.vision import ClipVisualAnalysis
    from app.domain.models import ClipIndexJobPayload
    with Database(tmp_path / 'scoped-handler.sqlite') as db:
        asset = _asset(db, tmp_path, has_audio=False)
        profile = IPProfileRepository(db).create(IPProfile(creator_name='Fixture'))
        project = ProjectRepository(db).create(Project(ip_profile_id=profile.id, title='Scoped fixture',
            topic='No runtime inference', fps=asset.fps, created_at=datetime.now(timezone.utc)))
        BudgetPolicyRepository(db).save(BudgetPolicy(project_id=project.id, allow_unknown_cost=True,
            max_calls=2, updated_at=datetime.now(timezone.utc)))
        clips = [ClipRepository(db).create(Clip(asset_id=asset.id,start_ms=i*1000,
            end_ms=(i+1)*1000,asset_duration_ms=2000)) for i in range(2)]
        frame = tmp_path / 'frame.jpg'; frame.write_bytes(b'fixture')
        seen = []
        class Resolver:
            def resolve(self, candidate, supplied):
                assert [c.id for c in supplied] == [clips[1].id]
                seen.append('extract')
                return [frame]
        class Provider:
            def analyze(self, path):
                seen.append('vision')
                return ClipVisualAnalysis('scoped',(),(),None,None,None,.8,None,None,False)
        class Indexer:
            def index_clips(self, supplied):
                assert [c.id for c in supplied] == [clips[1].id]
                seen.append('index')
        scope = [clips[1].id] if scope_valid else [uuid4()]
        AssetJobTargetStore(db).enqueue(_job(JobType.INDEX_CLIPS,'scoped-handler').model_copy(update={
            'project_id':project.id,'payload':ClipIndexJobPayload(clip_ids=scope,index_embeddings=index_embeddings)}),asset.id)
        handler = AssetVisionJobHandler(AssetJobTargetStore(db),AssetRepository(db),ClipRepository(db),
            Resolver(),MediaVisionPipeline(db,Provider()),Indexer() if index_embeddings else None,ProviderCallLedger(db))
        result = _runner(JobStore(db),{JobType.INDEX_CLIPS:handler}).run_once()
        calls = ProviderCallRepository(db).list_for_project(project.id)
        if scope_valid:
            assert result.status is JobStatus.COMPLETED
            assert seen == (['extract','vision','index'] if index_embeddings else ['extract','vision'])
            assert len(calls)==(2 if index_embeddings else 1)
            assert {c.operation for c in calls}==({'vision','embedding'} if index_embeddings else {'vision'})
            assert all(c.status=='completed' for c in calls)
            assert ClipRepository(db).get(clips[0].id)==clips[0]
        else:
            assert result.status is JobStatus.FAILED and result.error_code=='vision_clip_scope_invalid'
            assert seen==[] and calls==[]


@pytest.mark.parametrize(('status_code', 'error_code'), [(400, 'vision_request_rejected'), (503, 'vision_temporarily_unavailable')])
def test_vision_failure_keeps_prior_clip_and_charges_each_attempt_once(tmp_path, status_code, error_code):
    from app.domain.models import ClipIndexJobPayload
    from app.media.vision_pipeline import MediaVisionPipeline
    from app.providers.vision import ClipVisualAnalysis
    with Database(tmp_path / 'partial-vision.sqlite') as db:
        asset = _asset(db, tmp_path, has_audio=False)
        profile = IPProfileRepository(db).create(IPProfile(creator_name='Fixture'))
        project = ProjectRepository(db).create(Project(ip_profile_id=profile.id, title='Fixture',
            topic='Fixture', fps=asset.fps, created_at=datetime.now(timezone.utc)))
        BudgetPolicyRepository(db).save(BudgetPolicy(project_id=project.id, allow_unknown_cost=True,
            max_calls=2, updated_at=datetime.now(timezone.utc)))
        clips = [ClipRepository(db).create(Clip(asset_id=asset.id, start_ms=i*1000,
            end_ms=(i+1)*1000, asset_duration_ms=2000)) for i in range(2)]
        frame = tmp_path / 'partial.jpg'; frame.write_bytes(b'frame')
        class Resolver:
            def resolve(self, candidate, supplied): return [frame, frame]
        class Provider:
            count = 0
            def analyze(self, path):
                self.count += 1
                if self.count == 2: raise VisionHTTPError(status_code)
                return ClipVisualAnalysis('first frame', (), (), None, None, None, .8, None, None, False)
        job = _job(JobType.INDEX_CLIPS, 'partial-vision').model_copy(update={
            'project_id': project.id, 'payload': ClipIndexJobPayload(clip_ids=[c.id for c in clips], index_embeddings=False)})
        AssetJobTargetStore(db).enqueue(job, asset.id)
        handler = AssetVisionJobHandler(AssetJobTargetStore(db), AssetRepository(db), ClipRepository(db),
            Resolver(), MediaVisionPipeline(db, Provider()), None, ProviderCallLedger(db))
        result = _runner(JobStore(db), {JobType.INDEX_CLIPS: handler}).run_once()
        assert result.status is JobStatus.FAILED and result.error_code == error_code
        assert ClipRepository(db).get(clips[0].id).visual_description == 'first frame'
        assert ClipRepository(db).get(clips[1].id).visual_description is None
        calls = ProviderCallRepository(db).list_for_project(project.id)
        assert [call.status for call in calls] == ['completed', 'failed']
        facts = AssetRepository(db).get(asset.id).metadata['visual_analysis']
        assert facts[str(clips[0].id)]['coverage'] == 'sampled_keyframe'
        assert str(clips[1].id) not in facts


@pytest.mark.parametrize('first_result', ['completed', 'interrupted', 'changed_frame'])
def test_index_lease_recovery_never_resends_paid_or_uncertain_call(tmp_path, first_result):
    from app.domain.models import ClipIndexJobPayload
    from app.jobs.runner import JobExecutionError
    from app.media.vision_pipeline import MediaVisionPipeline
    from app.providers.vision import ClipVisualAnalysis
    with Database(tmp_path / 'recovered-vision.sqlite') as db:
        asset = _asset(db, tmp_path, has_audio=False)
        profile = IPProfileRepository(db).create(IPProfile(creator_name='Fixture'))
        project = ProjectRepository(db).create(Project(ip_profile_id=profile.id, title='Fixture',
            topic='Fixture', fps=asset.fps, created_at=datetime.now(timezone.utc)))
        BudgetPolicyRepository(db).save(BudgetPolicy(project_id=project.id, allow_unknown_cost=True,
            max_calls=4, updated_at=datetime.now(timezone.utc)))
        clip = ClipRepository(db).create(Clip(asset_id=asset.id, start_ms=0, end_ms=2000, asset_duration_ms=2000))
        frame = tmp_path / 'frame.jpg'; frame.write_bytes(b'frame')
        class Resolver:
            def resolve(self, candidate, supplied): return [frame]
        class Provider:
            count = 0
            def analyze(self, path):
                self.count += 1
                if first_result == 'interrupted': raise KeyboardInterrupt()
                return ClipVisualAnalysis('frame', (), (), None, None, None, .8, None, None, False)
        provider = Provider()
        targets = AssetJobTargetStore(db)
        job = targets.enqueue(_job(JobType.INDEX_CLIPS, 'recovery').model_copy(update={
            'project_id': project.id, 'payload': ClipIndexJobPayload(clip_ids=[clip.id], index_embeddings=False)}), asset.id)
        handler = AssetVisionJobHandler(targets, AssetRepository(db), ClipRepository(db), Resolver(),
            MediaVisionPipeline(db, provider), None, ProviderCallLedger(db))
        attempt = job.model_copy(update={'status': JobStatus.RUNNING, 'attempt': 1})
        if first_result == 'interrupted':
            with pytest.raises(KeyboardInterrupt): handler(attempt)
        else:
            handler(attempt)
            assert ClipRepository(db).get(clip.id).visual_description == 'frame'
        if first_result == 'changed_frame': frame.write_bytes(b'different frame')
        if first_result == 'completed':
            handler(attempt.model_copy(update={'attempt': 2}))
        else:
            with pytest.raises(JobExecutionError) as failure:
                handler(attempt.model_copy(update={'attempt': 2}))
            assert failure.value.retryable is False
            assert failure.value.code == ('provider_call_accounting_failed' if first_result == 'changed_frame'
                else 'provider_execution_recovery_required')
        assert provider.count == 1
        assert len(ProviderCallRepository(db).list_for_project(project.id)) == 1
        if first_result == 'completed':
            manual = targets.enqueue(_job(JobType.INDEX_CLIPS, 'manual-reindex').model_copy(update={
                'project_id': project.id, 'payload': ClipIndexJobPayload(clip_ids=[clip.id], index_embeddings=False)}), asset.id)
            handler(manual.model_copy(update={'status': JobStatus.RUNNING, 'attempt': 1}))
            assert provider.count == 2


def test_index_recovers_completed_call_after_interruption_before_clip_save(tmp_path):
    from app.domain.models import ClipIndexJobPayload
    from app.media.vision_pipeline import MediaVisionPipeline
    from app.providers.vision import ClipVisualAnalysis
    with Database(tmp_path / 'vision-result-recovery.sqlite') as db:
        asset = _asset(db, tmp_path, has_audio=False)
        BudgetPolicyRepository(db).save(BudgetPolicy(allow_unknown_cost=True, max_calls=1,
            updated_at=datetime.now(timezone.utc)))
        clip = ClipRepository(db).create(Clip(asset_id=asset.id, start_ms=0, end_ms=2000, asset_duration_ms=2000))
        frame = tmp_path / 'saved-frame.jpg'; frame.write_bytes(b'frame')
        class Resolver:
            def resolve(self, candidate, supplied): return [frame]
        class Provider:
            count = 0
            def analyze(self, path):
                self.count += 1
                return ClipVisualAnalysis('saved frame', ('person',), ('camera',), None, None, None, .8, None, None, False,
                    subtitle_regions=((.1, .1, .2, .2),))
        provider = Provider()
        targets = AssetJobTargetStore(db)
        job = targets.enqueue(_job(JobType.INDEX_CLIPS, 'result-recovery').model_copy(update={
            'payload': ClipIndexJobPayload(clip_ids=[clip.id], index_embeddings=False)}), asset.id)
        pipeline = MediaVisionPipeline(db, provider)
        persistence = pipeline.persistence
        class Interrupted:
            def apply(self, *args, **kwargs): raise KeyboardInterrupt()
        pipeline.persistence = Interrupted()
        handler = AssetVisionJobHandler(targets, AssetRepository(db), ClipRepository(db), Resolver(), pipeline,
            None, ProviderCallLedger(db))
        attempt = job.model_copy(update={'status': JobStatus.RUNNING, 'attempt': 1})
        with pytest.raises(KeyboardInterrupt): handler(attempt)
        assert ClipRepository(db).get(clip.id).visual_description is None
        calls = ProviderCallRepository(db).list_for_project(None)
        assert len(calls) == 1 and calls[0].status == 'completed'
        pipeline.persistence = persistence
        handler(attempt.model_copy(update={'attempt': 2}))
        assert provider.count == 1
        assert ClipRepository(db).get(clip.id).visual_description == 'saved frame'
        assert len(ProviderCallRepository(db).list_for_project(None)) == 1


@pytest.mark.parametrize('scenario', ['completed', 'interrupted', 'record_removed'])
def test_intake_expired_lease_replays_only_a_completed_visual_response(tmp_path, scenario):
    from app.domain.models import ClipIndexJobPayload
    from app.media.vision_pipeline import MediaVisionPipeline
    from app.providers.vision import ClipVisualAnalysis
    with Database(tmp_path / 'intake-lease.sqlite') as db:
        asset = _asset(db, tmp_path, has_audio=False)
        BudgetPolicyRepository(db).save(BudgetPolicy(allow_unknown_cost=True, max_calls=1,
            updated_at=datetime.now(timezone.utc)))
        clip = ClipRepository(db).create(Clip(asset_id=asset.id, start_ms=0, end_ms=2000, asset_duration_ms=2000))
        frame = tmp_path / 'frame.jpg'; frame.write_bytes(b'frame')
        class Resolver:
            def resolve(self, candidate, supplied): return [frame]
        class Provider:
            count = 0
            def analyze(self, path):
                self.count += 1
                if scenario == 'interrupted': raise KeyboardInterrupt()
                return ClipVisualAnalysis('observed', (), (), None, None, None, .8, None, None, False)
        provider = Provider()
        targets = AssetJobTargetStore(db)
        job = targets.enqueue(_job(JobType.INDEX_CLIPS, 'intake:vision:parent:clip').model_copy(update={
            'payload': ClipIndexJobPayload(clip_ids=[clip.id], index_embeddings=False)}), asset.id)
        pipeline = MediaVisionPipeline(db, provider)
        persistence = pipeline.persistence
        class Interrupted:
            def apply(self, *args, **kwargs): raise KeyboardInterrupt()
        pipeline.persistence = Interrupted()
        handler = AssetVisionJobHandler(targets, AssetRepository(db), ClipRepository(db), Resolver(), pipeline,
            None, ProviderCallLedger(db))
        store, start = JobStore(db), datetime.now(timezone.utc)
        initial = store.claim('old-worker', timedelta(seconds=1), max_attempts=1,
            allowed_types={JobType.INDEX_CLIPS}, now=start, job_id=job.id)
        with pytest.raises(KeyboardInterrupt): handler(initial)
        assert ClipRepository(db).get(clip.id).visual_description is None
        recovered = store.claim('new-worker', timedelta(seconds=10), max_attempts=1,
            allowed_types={JobType.INDEX_CLIPS}, now=start+timedelta(seconds=2), job_id=job.id)
        if scenario == 'interrupted':
            assert recovered is None
            assert store.get(job.id).status is JobStatus.FAILED
        else:
            assert recovered is not None and recovered.attempt == 2
            pipeline.persistence = persistence
            if scenario == 'record_removed':
                from app.jobs.runner import JobExecutionError
                db.connection.execute("DELETE FROM provider_call_records")
                with pytest.raises(JobExecutionError, match='saved provider result'):
                    handler(recovered)
                assert not ProviderCallRepository(db).list_for_project(None)
            else:
                handler(recovered)
                assert store.complete(job.id, 'new-worker', now=start+timedelta(seconds=3)).status is JobStatus.COMPLETED
                assert ClipRepository(db).get(clip.id).visual_description == 'observed'
        assert provider.count == 1
        assert len(ProviderCallRepository(db).list_for_project(None)) == (0 if scenario == 'record_removed' else 1)


def test_single_attempt_worker_resumes_saved_intake_response_without_calling_provider(tmp_path):
    from app.domain.models import ClipIndexJobPayload
    from app.media.vision_pipeline import MediaVisionPipeline
    from app.providers.vision import ClipVisualAnalysis
    with Database(tmp_path / 'intake-worker.sqlite') as db:
        asset = _asset(db, tmp_path, has_audio=False)
        BudgetPolicyRepository(db).save(BudgetPolicy(allow_unknown_cost=True, max_calls=1,
            updated_at=datetime.now(timezone.utc)))
        clip = ClipRepository(db).create(Clip(asset_id=asset.id, start_ms=0, end_ms=2000, asset_duration_ms=2000))
        frame = tmp_path / 'frame.jpg'; frame.write_bytes(b'frame')
        class Resolver:
            def resolve(self, candidate, supplied): return [frame]
        class Provider:
            count = 0
            def analyze(self, path):
                self.count += 1
                return ClipVisualAnalysis('observed', (), (), None, None, None, .8, None, None, False)
        provider = Provider()
        targets = AssetJobTargetStore(db)
        job = targets.enqueue(_job(JobType.INDEX_CLIPS, 'intake:vision:parent:worker').model_copy(update={
            'payload': ClipIndexJobPayload(clip_ids=[clip.id], index_embeddings=False)}), asset.id)
        pipeline = MediaVisionPipeline(db, provider)
        persistence = pipeline.persistence
        class Interrupted:
            def apply(self, *args, **kwargs): raise KeyboardInterrupt()
        pipeline.persistence = Interrupted()
        handler = AssetVisionJobHandler(targets, AssetRepository(db), ClipRepository(db), Resolver(), pipeline,
            None, ProviderCallLedger(db))
        runner = JobRunner(JobStore(db), {JobType.INDEX_CLIPS: handler}, worker_id='worker',
            lease_duration=timedelta(seconds=10), max_attempts=1)
        with pytest.raises(KeyboardInterrupt): runner.run_once(job_id=job.id)
        db.connection.execute('UPDATE jobs SET lease_expires_at=? WHERE id=?',
            ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(), str(job.id)))
        pipeline.persistence = persistence
        result = runner.run_once(job_id=job.id)
        assert result.status is JobStatus.COMPLETED and result.attempt == 2
        assert ClipRepository(db).get(clip.id).visual_description == 'observed'
        assert provider.count == 1 and len(ProviderCallRepository(db).list_for_project(None)) == 1


def test_requested_indexing_without_embedding_fails_before_vision(tmp_path):
    with Database(tmp_path/'no-embedding.sqlite') as db:
        asset=_asset(db,tmp_path,has_audio=False)
        ClipRepository(db).create(Clip(asset_id=asset.id,start_ms=0,end_ms=2000,asset_duration_ms=2000))
        job=AssetJobTargetStore(db).enqueue(_job(JobType.INDEX_CLIPS,'no-embedding'),asset.id)
        class Never:
            def resolve(self,*args):raise AssertionError('must reject before extraction/provider')
        handler=AssetVisionJobHandler(AssetJobTargetStore(db),AssetRepository(db),ClipRepository(db),Never(),object())
        result=_runner(JobStore(db),{JobType.INDEX_CLIPS:handler}).run_once(job_id=job.id)
        assert result.status is JobStatus.FAILED and result.error_code=='embedding_not_configured'


def test_vision_handler_completes_with_ordered_local_keyframes(tmp_path: Path) -> None:
    from app.domain.models import ClipIndexJobPayload
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
        AssetJobTargetStore(db).enqueue(_job(JobType.INDEX_CLIPS, "vision-success").model_copy(update={
            'payload':ClipIndexJobPayload(clip_ids=[c.id for c in clips],index_embeddings=False)}), asset.id)
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
            def process(self, candidate: Asset, supplied: list[Clip], paths: list[str], **kwargs) -> object:
                assert not db.connection.in_transaction
                seen.append((candidate.id, [clip.id for clip in supplied], paths))
                return object()

        handler = AssetVisionJobHandler(AssetJobTargetStore(db), AssetRepository(db), repository, Resolver(), Pipeline())  # type: ignore[arg-type]
        result = _runner(JobStore(db), {JobType.INDEX_CLIPS: handler}).run_once()
        assert result is not None and result.status is JobStatus.COMPLETED
        assert seen == [(asset.id, [clip.id], [path]) for clip, path in zip(clips, keyframes)]
    finally:
        db.close()


@pytest.mark.parametrize(
    ("failure", "expected_status", "expected_code"),
    [
        (VisionRateLimitError("private-key"), JobStatus.FAILED, "vision_temporarily_unavailable"),
        (VisionHTTPError(503), JobStatus.FAILED, "vision_temporarily_unavailable"),
        (VisionHTTPError(400), JobStatus.FAILED, "vision_request_rejected"),
    ],
)
def test_vision_handler_classifies_provider_failures_without_leaking_details(
    tmp_path: Path, failure: Exception, expected_status: JobStatus, expected_code: str
) -> None:
    from app.domain.models import ClipIndexJobPayload
    db = Database(tmp_path / f"vision-{expected_code}-{expected_status.value}.sqlite")
    try:
        asset = _asset(db, tmp_path, has_audio=False)
        clip = Clip(asset_id=asset.id, start_ms=0, end_ms=2_000, asset_duration_ms=2_000)
        clips = ClipRepository(db)
        clips.create(clip)
        AssetJobTargetStore(db).enqueue(_job(JobType.INDEX_CLIPS, f"{expected_code}-{expected_status.value}").model_copy(update={
            'payload':ClipIndexJobPayload(clip_ids=[clip.id],index_embeddings=False)}), asset.id)
        frame = tmp_path / "frame.jpg"
        frame.write_bytes(b"frame")

        class Resolver:
            def resolve(self, candidate: Asset, supplied: list[Clip]) -> list[str]:
                return [str(frame)]

        class Pipeline:
            def process(self, candidate: Asset, supplied: list[Clip], paths: list[str], **kwargs) -> object:
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
            def process(self, candidate, supplied, paths, **kwargs):
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
            def process(self, candidate, supplied, paths, **kwargs):
                return SimpleNamespace(clips=(clip,))

        class Indexer:
            def index_clips(self, supplied):
                raise EmbeddingRateLimitError("private embedding key")

        handler = AssetVisionJobHandler(
            AssetJobTargetStore(db), AssetRepository(db), clips, Resolver(), Pipeline(), Indexer()  # type: ignore[arg-type]
        )
        result = _runner(JobStore(db), {JobType.INDEX_CLIPS: handler}).run_once()
        assert result is not None and result.status is JobStatus.FAILED
        assert result.error_code == "embedding_temporarily_unavailable"
        assert "private" not in (result.error_message or "")
    finally:
        db.close()
