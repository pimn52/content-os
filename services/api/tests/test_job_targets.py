from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from threading import Barrier
from uuid import uuid4

import pytest

from app.db import AssetRepository, Database, JobRepository
from app.domain.models import Asset, Job, JobType, RationalFps
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
