"""Atomic persistence of asset-targeted analysis jobs."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from app.db.database import Database
from app.db.repositories import AssetRepository, ClipRepository, JobRepository, _model, _utc_timestamp
from app.domain.models import AudioAssetTranscriptionJobPayload, BudgetPolicy, ClipIndexJobPayload, CostCategory, Job, JobStatus, JobType, UsageCost


ALLOWED_ASSET_JOB_TYPES = frozenset({JobType.ANALYZE_ASSET, JobType.TRANSCRIBE_AUDIO, JobType.INDEX_CLIPS})


class AssetJobTargetError(RuntimeError):
    pass


class UnsupportedAssetJobType(AssetJobTargetError):
    pass


class AssetJobIdempotencyConflict(AssetJobTargetError):
    pass


@dataclass(frozen=True)
class AssetJobTarget:
    job_id: UUID
    asset_id: UUID


class AssetJobTargetStore:
    """Create an allowed asset job and its target in one writer transaction."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.jobs = JobRepository(db)
        self.db.connection.execute("PRAGMA busy_timeout = 5000")

    def enqueue(self, job: Job, asset_id: UUID) -> Job:
        if job.type not in ALLOWED_ASSET_JOB_TYPES:
            raise UnsupportedAssetJobType(f"job type {job.type.value} cannot target an asset")
        self.db.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.connection.execute("SELECT * FROM jobs WHERE idempotency_key = ?", (job.idempotency_key,)).fetchone()
            if row is not None:
                existing = _model(row, Job)
                target = self._target_for(existing.id)
                if (
                    existing.type != job.type
                    or existing.project_id != job.project_id
                    or target is None
                    or target.asset_id != asset_id
                    or existing.payload != job.payload
                ):
                    raise AssetJobIdempotencyConflict("idempotency key is already bound to a different job type or asset")
                self.db.connection.commit()
                return existing
            # JobRepository.create remains parameterized and the UNIQUE key is
            # still the database authority under independent connections.
            created = self.jobs.create(job)
            try:
                self.db.connection.execute(
                    "INSERT INTO asset_job_targets(job_id, asset_id) VALUES (?, ?)",
                    (str(created.id), str(asset_id)),
                )
            except sqlite3.IntegrityError:
                raise
            self.db.connection.commit()
            return created
        except BaseException:
            self.db.connection.rollback()
            raise

    def enqueue_initial_analysis(self, asset_id: UUID) -> Job | None:
        """Schedule local analysis once, preserving existing Clips and failed evidence."""
        self.db.connection.execute("BEGIN IMMEDIATE")
        try:
            if self.db.connection.execute("SELECT 1 FROM assets WHERE id=?", (str(asset_id),)).fetchone() is None:
                raise LookupError("asset not found")
            if self.db.connection.execute("SELECT 1 FROM clips WHERE asset_id=? LIMIT 1", (str(asset_id),)).fetchone():
                self.db.connection.commit()
                return None
            previous = self.db.connection.execute(
                """SELECT j.* FROM jobs j JOIN asset_job_targets t ON t.job_id=j.id
                   WHERE t.asset_id=? AND json_extract(j.payload,'$.type')=?
                   ORDER BY j.created_at DESC, j.id DESC LIMIT 1""",
                (str(asset_id), JobType.ANALYZE_ASSET.value),
            ).fetchone()
            if previous is not None:
                self.db.connection.commit()
                return _model(previous, Job)
            now = datetime.now(timezone.utc)
            job = Job(id=uuid4(), type=JobType.ANALYZE_ASSET, project_id=None,
                idempotency_key=f"intake:analyze:{asset_id}", created_at=now, updated_at=now)
            created = self.jobs.create(job)
            self.db.connection.execute("INSERT INTO asset_job_targets(job_id,asset_id) VALUES (?,?)",
                (str(created.id), str(asset_id)))
            self.db.connection.commit()
            return created
        except BaseException:
            self.db.connection.rollback()
            raise

    def target_for(self, job_id: UUID) -> AssetJobTarget | None:
        return self._target_for(job_id)

    def has_clips(self, asset_id: UUID) -> bool:
        return self.db.connection.execute(
            "SELECT 1 FROM clips WHERE asset_id=? LIMIT 1", (str(asset_id),)
        ).fetchone() is not None

    def enqueue_intake_visual_sample(self, analysis_job: Job, clips: list) -> list[Job]:
        """Bound new intake to four representative clips; older assets are not scanned."""
        if (analysis_job.type is not JobType.ANALYZE_ASSET
            or not analysis_job.idempotency_key.startswith("intake:analyze:") or not clips):
            return []
        target = self.target_for(analysis_job.id)
        if target is None or any(clip.asset_id != target.asset_id for clip in clips):
            raise AssetJobTargetError("intake clip target mismatch")
        ordered = sorted(clips, key=lambda clip: (clip.start_ms, str(clip.id)))
        count = min(4, len(ordered))
        indexes = [0] if count == 1 else [round(i * (len(ordered) - 1) / (count - 1)) for i in range(count)]
        jobs = []
        for i in indexes:
            clip = ordered[i]
            now = datetime.now(timezone.utc)
            job = Job(type=JobType.INDEX_CLIPS, project_id=analysis_job.project_id,
                idempotency_key=f"intake:vision:{analysis_job.id}:{clip.id}", created_at=now, updated_at=now,
                payload=ClipIndexJobPayload(clip_ids=[clip.id], index_embeddings=False))
            jobs.append(self.enqueue(job, target.asset_id))
        return jobs

    def resume_budget_blocked_intake(self, policy: BudgetPolicy) -> int:
        """Budget changes can resume an uncharged intake sample once per setting."""
        if self.db.connection.in_transaction:
            return self._resume_budget_blocked_intake_locked(policy)
        with self.db.transaction(immediate=True):
            return self._resume_budget_blocked_intake_locked(policy)

    def _resume_budget_blocked_intake_locked(self, policy: BudgetPolicy) -> int:
        from app.budget import BudgetLimitError, ProviderCallLedger, provider_call_input_digest
        from app.db import BudgetPolicyRepository

        current = BudgetPolicyRepository(self.db).get_by_project(None)
        if policy.project_id is not None or current != policy:
            return 0
        try:
            ProviderCallLedger(self.db).check_reservation(project_id=None,
                estimated_cost=UsageCost(category=CostCategory.LLM))
        except BudgetLimitError:
            return 0
        policy_key = provider_call_input_digest(policy.model_dump(mode="json", exclude={"id", "updated_at"}))
        rows = self.db.connection.execute("""
            SELECT j.*, t.asset_id AS target_asset_id FROM jobs j
            JOIN asset_job_targets t ON t.job_id=j.id
            WHERE j.project_id IS NULL AND j.idempotency_key LIKE 'intake:vision:%'
            ORDER BY j.created_at, j.rowid
        """).fetchall()
        # A retry retains its original sample key before '~'; only its latest
        # state is relevant. The old failed jobs remain as history.
        latest = {}
        for row in rows:
            job = _model(row, Job)
            root = job.idempotency_key.split("~", 1)[0]
            latest[root] = (job, UUID(row["target_asset_id"]))
        created = 0
        for root, (job, asset_id) in latest.items():
            if (job.type is not JobType.INDEX_CLIPS or job.status is not JobStatus.FAILED
                or job.error_code != "provider_budget_blocked"
                or not isinstance(job.payload, ClipIndexJobPayload)
                or job.payload.index_embeddings or not job.payload.clip_ids
                or len(job.payload.clip_ids) != 1):
                continue
            # Any earlier invocation in this sample's history is ambiguous or
            # billable; only requests stopped before reservation are eligible.
            invoked = self.db.connection.execute("""
                SELECT 1 FROM jobs j JOIN provider_call_records p
                ON p.idempotency_key LIKE 'job:' || j.id || ':%'
                WHERE j.idempotency_key = ? OR j.idempotency_key LIKE ? LIMIT 1
            """, (root, root + "~%")).fetchone()
            if invoked is not None:
                continue
            key = root + "~" + policy_key
            if self.jobs.get_by_idempotency_key(key) is not None:
                continue
            now = datetime.now(timezone.utc)
            new_job = self.jobs.create(Job(type=JobType.INDEX_CLIPS, idempotency_key=key,
                project_id=None, payload=job.payload, created_at=now, updated_at=now))
            self.db.connection.execute("INSERT INTO asset_job_targets(job_id,asset_id) VALUES (?,?)",
                (str(new_job.id), str(asset_id)))
            created += 1
        return created

    def recover_intake_visual_samples(self) -> int:
        """Repair only interrupted handoffs; never replay failed provider jobs."""
        from app.media.pipeline import MediaAnalysisPipeline, MediaPipelineError

        rows = self.db.connection.execute("""
            SELECT j.*, t.asset_id AS target_asset_id FROM jobs j
            JOIN asset_job_targets t ON t.job_id=j.id
            WHERE j.idempotency_key = 'intake:analyze:' || t.asset_id
              AND (j.status='completed'
                OR (j.status='running' AND j.lease_expires_at IS NOT NULL AND j.lease_expires_at <= ?)
                OR (j.status='failed' AND j.error_code='attempt_budget_exhausted'))
              AND EXISTS (SELECT 1 FROM clips c WHERE c.asset_id=t.asset_id)
              AND (SELECT COUNT(DISTINCT json_extract(v.payload,'$.payload.clip_ids[0]')) FROM jobs v
                   WHERE v.idempotency_key LIKE 'intake:vision:' || j.id || ':%')
                  < MIN(4, (SELECT COUNT(*) FROM clips c WHERE c.asset_id=t.asset_id))
            ORDER BY j.created_at LIMIT 64
        """, (_utc_timestamp(datetime.now(timezone.utc)),)).fetchall()
        recovered = 0
        assets, clips_repo = AssetRepository(self.db), ClipRepository(self.db)
        for row in rows:
            job, asset_id = _model(row, Job), UUID(row['target_asset_id'])
            asset = assets.get(asset_id)
            clips = clips_repo.list_by_asset(asset_id)
            if asset is None or not clips:
                continue
            try:
                expected = MediaAnalysisPipeline._normalize_clips(asset, clips)
            except MediaPipelineError:
                continue
            if [(c.id, c.start_ms, c.end_ms) for c in clips] != [(c.id, c.start_ms, c.end_ms) for c in expected]:
                continue
            before = self.db.connection.execute("SELECT COUNT(DISTINCT json_extract(payload,'$.payload.clip_ids[0]')) FROM jobs WHERE idempotency_key LIKE ?",
                (f'intake:vision:{job.id}:%',)).fetchone()[0]
            self.enqueue_intake_visual_sample(job, clips)
            recovered += max(0, min(4, len(clips)) - before)
        return recovered

    def _target_for(self, job_id: UUID) -> AssetJobTarget | None:
        row = self.db.connection.execute("SELECT job_id, asset_id FROM asset_job_targets WHERE job_id = ?", (str(job_id),)).fetchone()
        if row is None:
            return None
        return AssetJobTarget(UUID(row["job_id"]), UUID(row["asset_id"]))


@dataclass(frozen=True)
class AudioAssetJobTarget:
    job_id: UUID
    audio_asset_id: UUID


class AudioAssetJobTargetStore:
    """Persist idempotent ASR jobs for existing authorized AudioAssets."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.jobs = JobRepository(db)
        self.db.connection.execute("PRAGMA busy_timeout = 5000")

    def enqueue(self, job: Job, audio_asset_id: UUID) -> Job:
        if job.type is not JobType.TRANSCRIBE_AUDIO_ASSET:
            raise UnsupportedAssetJobType(f"job type {job.type.value} cannot target an audio asset")
        if (not isinstance(job.payload, AudioAssetTranscriptionJobPayload)
            or job.payload.audio_asset_id != audio_asset_id):
            raise AssetJobIdempotencyConflict("audio transcription payload must bind the target asset")
        self.db.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.connection.execute("SELECT * FROM jobs WHERE idempotency_key = ?", (job.idempotency_key,)).fetchone()
            if row is not None:
                existing = _model(row, Job)
                target = self.target_for(existing.id)
                if (existing.type != job.type or existing.project_id != job.project_id
                    or target is None or target.audio_asset_id != audio_asset_id
                    or existing.payload != job.payload):
                    raise AssetJobIdempotencyConflict("idempotency key is already bound to different audio")
                self.db.connection.commit()
                return existing
            created = self.jobs.create(job)
            self.db.connection.execute(
                "INSERT INTO audio_asset_job_targets(job_id, audio_asset_id) VALUES (?, ?)",
                (str(created.id), str(audio_asset_id)),
            )
            self.db.connection.commit()
            return created
        except BaseException:
            self.db.connection.rollback()
            raise

    def target_for(self, job_id: UUID) -> AudioAssetJobTarget | None:
        row = self.db.connection.execute(
            "SELECT job_id,audio_asset_id FROM audio_asset_job_targets WHERE job_id=?", (str(job_id),),
        ).fetchone()
        if row is None:
            return None
        return AudioAssetJobTarget(UUID(row["job_id"]), UUID(row["audio_asset_id"]))
