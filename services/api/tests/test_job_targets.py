from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from threading import Barrier
from uuid import uuid4

import pytest

from app.db import AssetRepository, ClipRepository, Database, JobRepository
from app.domain.models import Asset, Clip, Job, JobStatus, JobType, RationalFps
from app.jobs.targets import AssetJobIdempotencyConflict, AssetJobTargetStore, UnsupportedAssetJobType


def _asset(db: Database, tmp_path: Path) -> Asset:
    path = tmp_path / "asset.mp4"
    path.write_bytes(b"asset")
    value = Asset(source_file=str(path), content_hash="c" * 64, duration_ms=1000, width=10, height=10, fps=RationalFps(numerator=25, denominator=1), authorization_reference="rights", imported_at=datetime.now(timezone.utc))
    AssetRepository(db).create(value)
    return value


def _job(kind: JobType, key: str) -> Job:
    now = datetime.now(timezone.utc)
    return Job(type=kind, idempotency_key=key, created_at=now, updated_at=now)


def test_enqueue_target_is_atomic_and_idempotent(tmp_path: Path):
    db = Database(tmp_path / "store.sqlite")
    try:
        asset = _asset(db, tmp_path)
        store = AssetJobTargetStore(db)
        job = _job(JobType.ANALYZE_ASSET, "asset-key")
        created = store.enqueue(job, asset.id)
        repeated = store.enqueue(job.model_copy(update={"id": uuid4()}), asset.id)
        assert repeated.id == created.id
        assert store.target_for(created.id).asset_id == asset.id
        assert db.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        assert db.connection.execute("SELECT COUNT(*) FROM asset_job_targets").fetchone()[0] == 1
    finally:
        db.close()


def test_initial_local_analysis_is_idempotent_and_does_not_retry_failure(tmp_path: Path):
    with Database(tmp_path / 'initial.sqlite') as db:
        asset = _asset(db, tmp_path)
        store = AssetJobTargetStore(db)
        first = store.enqueue_initial_analysis(asset.id)
        assert first.type == JobType.ANALYZE_ASSET
        assert store.enqueue_initial_analysis(asset.id).id == first.id
        JobRepository(db).update(first.model_copy(update={'status': JobStatus.FAILED, 'error_code': 'analysis_failed'}))
        assert store.enqueue_initial_analysis(asset.id).status == JobStatus.FAILED
        assert len(JobRepository(db).list_for_asset(asset.id)) == 1


def test_intake_visual_sampling_is_bounded_idempotent_and_does_not_scan_old_jobs(tmp_path):
    with Database(tmp_path / 'sample.sqlite') as db:
        asset = _asset(db, tmp_path)
        store = AssetJobTargetStore(db)
        intake = store.enqueue_initial_analysis(asset.id)
        clips = [Clip(asset_id=asset.id, start_ms=i * 100, end_ms=(i + 1) * 100,
            asset_duration_ms=1000) for i in range(10)]
        for clip in clips:
            ClipRepository(db).create(clip)
        samples = store.enqueue_intake_visual_sample(intake, clips)
        assert {sample.payload.clip_ids[0] for sample in samples} == {clips[i].id for i in (0, 3, 6, 9)}
        assert all(sample.payload.index_embeddings is False for sample in samples)
        assert [j.id for j in store.enqueue_intake_visual_sample(intake, clips)] == [j.id for j in samples]
        JobRepository(db).update(samples[0].model_copy(update={'status': JobStatus.FAILED, 'error_code': 'provider_budget_blocked'}))
        assert store.enqueue_intake_visual_sample(intake, clips)[0].status is JobStatus.FAILED
        manual = store.enqueue(_job(JobType.ANALYZE_ASSET, 'legacy-analysis'), asset.id)
        assert store.enqueue_intake_visual_sample(manual, clips) == []


def test_budget_change_resumes_only_uncharged_intake_samples_once(tmp_path):
    from app.budget import ProviderCallLedger
    from app.db import BudgetPolicyRepository
    from app.domain.models import BudgetPolicy, CostCategory, UsageCost

    with Database(tmp_path / 'budget-resume.sqlite') as db:
        asset = _asset(db, tmp_path)
        store = AssetJobTargetStore(db)
        intake = store.enqueue_initial_analysis(asset.id)
        clips = [ClipRepository(db).create(Clip(asset_id=asset.id, start_ms=i*250,
            end_ms=(i+1)*250, asset_duration_ms=1000)) for i in range(4)]
        samples = store.enqueue_intake_visual_sample(intake, clips)
        for index, sample in enumerate(samples):
            JobRepository(db).update(sample.model_copy(update={"status": JobStatus.FAILED,
                "error_code": "provider_budget_blocked" if index < 3 else "vision_invalid"}))
        policy = BudgetPolicy(allow_unknown_cost=True, max_calls=10, updated_at=datetime.now(timezone.utc))
        BudgetPolicyRepository(db).save(policy)
        # A previous reservation of any state makes the sample ineligible.
        ProviderCallLedger(db).reserve_execution(project_id=None,
            idempotency_key=f"job:{samples[2].id}:vision:keyframe-0", operation="vision",
            mode="runtime", provider="fixture", model="fixture", input_source="fixture",
            input_digest="c" * 64, estimated_cost=UsageCost(category=CostCategory.LLM))
        assert store.resume_budget_blocked_intake(policy) == 2
        assert store.resume_budget_blocked_intake(policy) == 0
        all_jobs = JobRepository(db).list_for_asset(asset.id)
        retries = [job for job in all_jobs if '~' in job.idempotency_key]
        assert len(retries) == 2 and all(job.status is JobStatus.PENDING for job in retries)
        assert JobRepository(db).get(samples[0].id).status is JobStatus.FAILED
        JobRepository(db).update(retries[0].model_copy(update={"status": JobStatus.FAILED, "error_code": "provider_budget_blocked"}))
        assert store.resume_budget_blocked_intake(policy) == 0
        new_policy = policy.model_copy(update={"max_calls": 11, "updated_at": datetime.now(timezone.utc)})
        BudgetPolicyRepository(db).save(new_policy)
        assert store.resume_budget_blocked_intake(new_policy) == 1


def test_budget_resume_is_atomic_across_workers(tmp_path):
    from app.db import BudgetPolicyRepository
    from app.domain.models import BudgetPolicy

    path = tmp_path / 'budget-race.sqlite'
    with Database(path) as db:
        asset = _asset(db, tmp_path)
        store = AssetJobTargetStore(db)
        intake = store.enqueue_initial_analysis(asset.id)
        clip = ClipRepository(db).create(Clip(asset_id=asset.id, start_ms=0, end_ms=1000, asset_duration_ms=1000))
        sample = store.enqueue_intake_visual_sample(intake, [clip])[0]
        JobRepository(db).update(sample.model_copy(update={"status": JobStatus.FAILED, "error_code": "provider_budget_blocked"}))
        policy = BudgetPolicy(allow_unknown_cost=True, max_calls=10, updated_at=datetime.now(timezone.utc))
        BudgetPolicyRepository(db).save(policy)

    gate = Barrier(2)
    def resume():
        with Database(path) as connection:
            gate.wait()
            return AssetJobTargetStore(connection).resume_budget_blocked_intake(policy)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: resume(), range(2))) == [0, 1]
    with Database(path) as db:
        assert len([job for job in JobRepository(db).list_for_asset(asset.id) if '~' in job.idempotency_key]) == 1


def test_recover_interrupted_intake_visual_handoff_without_replaying_failed_calls(tmp_path):
    from app.media.pipeline import MediaAnalysisPipeline
    with Database(tmp_path / 'intake-recover.sqlite') as db:
        asset = _asset(db, tmp_path)
        targets = AssetJobTargetStore(db)
        job = targets.enqueue_initial_analysis(asset.id)
        clips = MediaAnalysisPipeline._normalize_clips(asset, [
            Clip(asset_id=asset.id, start_ms=i * 200, end_ms=(i + 1) * 200, asset_duration_ms=1000)
            for i in range(5)])
        for clip in clips:
            ClipRepository(db).create(clip)
        # A crash after cutting and after the first enqueue leaves a running lease.
        db.connection.execute("UPDATE jobs SET status='running',lease_expires_at=? WHERE id=?",
            ('2000-01-01T00:00:00+00:00', str(job.id)))
        first = targets.enqueue_intake_visual_sample(job, clips[:1])[0]
        JobRepository(db).update(first.model_copy(update={'status': JobStatus.FAILED, 'error_code': 'vision_failed'}))
        # A later budget attempt for the same Clip is not a second sampled Clip.
        targets.enqueue(first.model_copy(update={'id': uuid4(), 'idempotency_key': first.idempotency_key + '~budget'}), asset.id)
        assert targets.recover_intake_visual_samples() == 3
        assert targets.recover_intake_visual_samples() == 0
        jobs = JobRepository(db).list_for_asset(asset.id)
        samples = [item for item in jobs if item.type is JobType.INDEX_CLIPS]
        assert len(samples) == 5
        assert next(item for item in samples if item.id == first.id).status is JobStatus.FAILED


def test_recovery_does_not_infer_completed_cut_from_manual_or_incomplete_clips(tmp_path):
    with Database(tmp_path / 'intake-recover-invalid.sqlite') as db:
        asset = _asset(db, tmp_path)
        targets = AssetJobTargetStore(db)
        job = targets.enqueue_initial_analysis(asset.id)
        JobRepository(db).update(job.model_copy(update={'status': JobStatus.COMPLETED}))
        ClipRepository(db).create(Clip(asset_id=asset.id, start_ms=0, end_ms=1000, asset_duration_ms=1000))
        assert targets.recover_intake_visual_samples() == 0
        assert len(JobRepository(db).list_for_asset(asset.id)) == 1


def test_local_analysis_hands_new_intake_to_vision_queue(tmp_path):
    from types import SimpleNamespace
    from app.jobs.handlers import AssetAnalysisJobHandler
    with Database(tmp_path / 'handoff.sqlite') as db:
        asset = _asset(db, tmp_path)
        targets = AssetJobTargetStore(db)
        job = targets.enqueue_initial_analysis(asset.id)
        class Pipeline:
            def process(self, source):
                clip = Clip(asset_id=source.id, start_ms=0, end_ms=1000, asset_duration_ms=1000)
                ClipRepository(db).create(clip)
                return SimpleNamespace(clips=[clip])
        AssetAnalysisJobHandler(targets, AssetRepository(db), Pipeline())(job.model_copy(update={'status': JobStatus.RUNNING}))
        jobs = JobRepository(db).list_for_asset(asset.id)
        assert len(jobs) == 2
        assert [j.type for j in jobs].count(JobType.INDEX_CLIPS) == 1


def test_external_reimport_keeps_existing_clips_and_visual_evidence(tmp_path):
    from datetime import timedelta
    from app.jobs.handlers import AssetAnalysisJobHandler
    from app.jobs.runner import JobRunner
    from app.jobs.store import JobStore
    with Database(tmp_path / 'external-existing.sqlite') as db:
        asset = _asset(db, tmp_path)
        clip = Clip(asset_id=asset.id, start_ms=0, end_ms=1000, asset_duration_ms=1000,
            visual_description='已有区间判断')
        ClipRepository(db).create(clip)
        evidence = {'coverage': 'video_interval_analysis', 'source_content_hash': asset.content_hash,
            'clip_interval': {'start_ms': 0, 'end_ms': 1000}, 'scene_matches': {'prior-use': {'supported': True}}}
        AssetRepository(db).update(asset.model_copy(update={'metadata': {'visual_analysis': {str(clip.id): evidence}}}))
        targets = AssetJobTargetStore(db)
        job = targets.enqueue(_job(JobType.ANALYZE_ASSET, f'external-analysis:{asset.id}'), asset.id)
        class NoRecut:
            def process(self, source):
                pytest.fail('existing external footage must not be cut again')
        runner = JobRunner(JobStore(db), {JobType.ANALYZE_ASSET: AssetAnalysisJobHandler(targets, AssetRepository(db), NoRecut())},
            worker_id='test', lease_duration=timedelta(seconds=30), max_attempts=1)
        assert runner.run_once(job_id=job.id).status == JobStatus.COMPLETED
        assert ClipRepository(db).get(clip.id) == clip
        assert AssetRepository(db).get(asset.id).metadata['visual_analysis'][str(clip.id)] == evidence
        assert len(JobRepository(db).list_for_asset(asset.id)) == 1


def test_initial_local_analysis_respects_existing_clips_and_jobs(tmp_path: Path):
    with Database(tmp_path / 'existing.sqlite') as db:
        asset = _asset(db, tmp_path)
        store = AssetJobTargetStore(db)
        ClipRepository(db).create(Clip(asset_id=asset.id, start_ms=0, end_ms=1000, asset_duration_ms=1000))
        assert store.enqueue_initial_analysis(asset.id) is None
        assert not JobRepository(db).list_for_asset(asset.id)
        other = asset.model_copy(update={'id': uuid4(), 'content_hash': 'd' * 64})
        AssetRepository(db).create(other)
        manual = store.enqueue(_job(JobType.ANALYZE_ASSET, 'already-running'), other.id)
        assert store.enqueue_initial_analysis(other.id).id == manual.id
        assert len(JobRepository(db).list_for_asset(other.id)) == 1


def test_conflicts_and_unsupported_type_do_not_pollute(tmp_path: Path):
    db = Database(tmp_path / "store.sqlite")
    try:
        asset = _asset(db, tmp_path)
        other = asset.model_copy(update={"id": uuid4(), "content_hash": "d" * 64})
        AssetRepository(db).create(other)
        store = AssetJobTargetStore(db)
        store.enqueue(_job(JobType.ANALYZE_ASSET, "same"), asset.id)
        with pytest.raises(AssetJobIdempotencyConflict):
            store.enqueue(_job(JobType.TRANSCRIBE_AUDIO, "same"), asset.id)
        with pytest.raises(AssetJobIdempotencyConflict):
            store.enqueue(_job(JobType.ANALYZE_ASSET, "same"), other.id)
        with pytest.raises(AssetJobIdempotencyConflict):
            store.enqueue(_job(JobType.ANALYZE_ASSET, "same").model_copy(update={"project_id": uuid4()}), asset.id)
        with pytest.raises(UnsupportedAssetJobType):
            store.enqueue(_job(JobType.RENDER, "render"), asset.id)
        assert db.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        assert db.connection.execute("SELECT COUNT(*) FROM asset_job_targets").fetchone()[0] == 1
    finally:
        db.close()


def test_deleting_job_cascades_target(tmp_path: Path):
    db = Database(tmp_path / "cascade.sqlite")
    try:
        asset = _asset(db, tmp_path)
        job = AssetJobTargetStore(db).enqueue(_job(JobType.ANALYZE_ASSET, "cascade"), asset.id)
        assert db.connection.execute("SELECT COUNT(*) FROM asset_job_targets").fetchone()[0] == 1
        assert JobRepository(db).delete(job.id)
        assert db.connection.execute("SELECT COUNT(*) FROM asset_job_targets").fetchone()[0] == 0
    finally:
        db.close()


def test_target_fk_failure_rolls_back_job(tmp_path: Path):
    db = Database(tmp_path / "store.sqlite")
    try:
        store = AssetJobTargetStore(db)
        with pytest.raises(sqlite3.IntegrityError):
            store.enqueue(_job(JobType.ANALYZE_ASSET, "bad-target"), uuid4())
        assert db.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    finally:
        db.close()


def test_concurrent_same_idempotency_returns_one_job(tmp_path: Path):
    path = tmp_path / "concurrent.sqlite"
    seed = Database(path)
    asset = _asset(seed, tmp_path)
    seed.close()
    barrier = Barrier(2)

    def run(_: int):
        db = Database(path)
        try:
            store = AssetJobTargetStore(db)
            barrier.wait()
            return store.enqueue(_job(JobType.TRANSCRIBE_AUDIO, "concurrent"), asset.id)
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = list(pool.map(run, range(2)))
    assert jobs[0].id == jobs[1].id
    db = Database(path)
    try:
        assert db.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        assert db.connection.execute("SELECT COUNT(*) FROM asset_job_targets").fetchone()[0] == 1
    finally:
        db.close()
