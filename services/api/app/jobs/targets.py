"""Atomic persistence of asset-targeted analysis jobs."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from uuid import UUID

from app.db.database import Database
from app.db.repositories import JobRepository, _model
from app.domain.models import AudioAssetTranscriptionJobPayload, Job, JobType


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

    def target_for(self, job_id: UUID) -> AssetJobTarget | None:
        return self._target_for(job_id)

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
