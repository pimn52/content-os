"""Explicit repositories for the five persisted domain models."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Generic, TypeVar
from uuid import UUID

from app.domain.models import Asset, Clip, IPProfile, Job, Project

from .database import Database

ModelT = TypeVar("ModelT")


def _payload(model: ModelT) -> str:
    return json.dumps(model.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))  # type: ignore[attr-defined]


def _model(row: sqlite3.Row, cls: type[ModelT]) -> ModelT:
    return cls.model_validate(json.loads(row["payload"]))  # type: ignore[attr-defined]


def _utc_timestamp(value: datetime) -> str:
    """Use a fixed-width UTC encoding that sorts lexicographically by time."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _job_payload(job: Job) -> str:
    """Serialize Job timestamps with the same fixed-width UTC encoding as columns."""
    data = job.model_dump(mode="json")
    data["created_at"] = _utc_timestamp(job.created_at)
    data["updated_at"] = _utc_timestamp(job.updated_at)
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class _Repository(Generic[ModelT]):
    table: str
    model: type[ModelT]

    def __init__(self, db: Database):
        self.db = db

    def get(self, item_id: UUID) -> ModelT | None:
        row = self.db.connection.execute(f"SELECT * FROM {self.table} WHERE id = ?", (str(item_id),)).fetchone()
        return None if row is None else _model(row, self.model)

    def list(self) -> list[ModelT]:
        rows = self.db.connection.execute(f"SELECT * FROM {self.table} ORDER BY rowid").fetchall()
        return [_model(row, self.model) for row in rows]

    def delete(self, item_id: UUID) -> bool:
        cursor = self.db.connection.execute(f"DELETE FROM {self.table} WHERE id = ?", (str(item_id),))
        return cursor.rowcount == 1


class IPProfileRepository(_Repository[IPProfile]):
    table, model = "ip_profiles", IPProfile

    def create(self, value: IPProfile) -> IPProfile:
        self.db.connection.execute("INSERT INTO ip_profiles(id, payload) VALUES (?, ?)", (str(value.id), _payload(value)))
        return value

    def update(self, value: IPProfile) -> IPProfile:
        cursor = self.db.connection.execute("UPDATE ip_profiles SET payload = ? WHERE id = ?", (_payload(value), str(value.id)))
        if cursor.rowcount != 1:
            raise KeyError(value.id)
        return value


class ProjectRepository(_Repository[Project]):
    table, model = "projects", Project

    def create(self, value: Project) -> Project:
        self.db.connection.execute("INSERT INTO projects(id, ip_profile_id, payload) VALUES (?, ?, ?)", (str(value.id), str(value.ip_profile_id), _payload(value)))
        return value

    def update(self, value: Project) -> Project:
        cursor = self.db.connection.execute("UPDATE projects SET ip_profile_id = ?, payload = ? WHERE id = ?", (str(value.ip_profile_id), _payload(value), str(value.id)))
        if cursor.rowcount != 1:
            raise KeyError(value.id)
        return value


class AssetRepository(_Repository[Asset]):
    table, model = "assets", Asset

    def __init__(self, db: Database):
        super().__init__(db)
        self.db.connection.execute("PRAGMA busy_timeout = 5000")

    def create(self, value: Asset) -> Asset:
        self.db.connection.execute("INSERT INTO assets(id, duration_ms, content_hash, payload) VALUES (?, ?, ?, ?)", (str(value.id), value.duration_ms, value.content_hash, _payload(value)))
        return value

    def update(self, value: Asset) -> Asset:
        existing = self.db.connection.execute("SELECT content_hash FROM assets WHERE id = ?", (str(value.id),)).fetchone()
        if existing is None:
            raise KeyError(value.id)
        if existing["content_hash"] != value.content_hash:
            raise ValueError("asset content_hash is immutable")
        clips = self.db.connection.execute("SELECT asset_duration_ms, end_ms FROM clips WHERE asset_id = ?", (str(value.id),)).fetchall()
        if any(int(row["asset_duration_ms"]) != value.duration_ms or int(row["end_ms"]) > value.duration_ms for row in clips):
            raise ValueError("asset duration update would invalidate existing clips")
        cursor = self.db.connection.execute("UPDATE assets SET duration_ms = ?, content_hash = ?, payload = ? WHERE id = ?", (value.duration_ms, value.content_hash, _payload(value), str(value.id)))
        if cursor.rowcount != 1:
            raise KeyError(value.id)
        return value

    def get_by_content_hash(self, content_hash: str) -> Asset | None:
        row = self.db.connection.execute("SELECT * FROM assets WHERE content_hash = ?", (content_hash,)).fetchone()
        return None if row is None else _model(row, self.model)


class ClipRepository(_Repository[Clip]):
    table, model = "clips", Clip

    def list_by_asset(self, asset_id: UUID) -> list[Clip]:
        rows = self.db.connection.execute(
            "SELECT * FROM clips WHERE asset_id = ? ORDER BY start_ms, end_ms, id", (str(asset_id),)
        ).fetchall()
        return [_model(row, Clip) for row in rows]

    def create(self, value: Clip) -> Clip:
        self._validate_asset(value)
        self.db.connection.execute("INSERT INTO clips(id, asset_id, start_ms, end_ms, asset_duration_ms, payload) VALUES (?, ?, ?, ?, ?, ?)", (str(value.id), str(value.asset_id), value.start_ms, value.end_ms, value.asset_duration_ms, _payload(value)))
        return value

    def update(self, value: Clip) -> Clip:
        self._validate_asset(value)
        cursor = self.db.connection.execute("UPDATE clips SET asset_id = ?, start_ms = ?, end_ms = ?, asset_duration_ms = ?, payload = ? WHERE id = ?", (str(value.asset_id), value.start_ms, value.end_ms, value.asset_duration_ms, _payload(value), str(value.id)))
        if cursor.rowcount != 1:
            raise KeyError(value.id)
        return value

    def upsert(self, value: Clip) -> Clip:
        self._validate_asset(value)
        self.db.connection.execute(
            """INSERT INTO clips(id, asset_id, start_ms, end_ms, asset_duration_ms, payload)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   asset_id = excluded.asset_id,
                   start_ms = excluded.start_ms,
                   end_ms = excluded.end_ms,
                   asset_duration_ms = excluded.asset_duration_ms,
                   payload = excluded.payload""",
            (
                str(value.id), str(value.asset_id), value.start_ms, value.end_ms,
                value.asset_duration_ms, _payload(value),
            ),
        )
        return value

    def _validate_asset(self, value: Clip) -> None:
        row = self.db.connection.execute("SELECT duration_ms FROM assets WHERE id = ?", (str(value.asset_id),)).fetchone()
        if row is None:
            raise sqlite3.IntegrityError("clip asset does not exist")
        duration = int(row["duration_ms"])
        if value.asset_duration_ms != duration:
            raise ValueError("clip asset_duration_ms does not match stored asset duration")
        if value.end_ms > duration:
            raise ValueError("clip interval exceeds stored asset duration")


class JobRepository(_Repository[Job]):
    table, model = "jobs", Job

    def __init__(self, db: Database):
        super().__init__(db)
        # Each SQLite connection needs its own busy timeout.  This lets a
        # second local process wait for an idempotent insert to commit instead
        # of spuriously failing with "database is locked".
        self.db.connection.execute("PRAGMA busy_timeout = 5000")

    def create(self, value: Job) -> Job:
        self.db.connection.execute(
            """INSERT INTO jobs(
                    id, project_id, idempotency_key, status, attempt,
                    created_at, updated_at, error_code, error_message, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(idempotency_key) DO NOTHING""",
            (
                str(value.id), None if value.project_id is None else str(value.project_id), value.idempotency_key,
                value.status.value, value.attempt, _utc_timestamp(value.created_at), _utc_timestamp(value.updated_at),
                value.error_code, value.error_message, _job_payload(value),
            ),
        )
        # Fetch after the insert attempt, rather than before it: the UNIQUE
        # constraint is the concurrency authority for separate connections.
        row = self.db.connection.execute("SELECT * FROM jobs WHERE idempotency_key = ?", (value.idempotency_key,)).fetchone()
        if row is None:  # A conflicting primary key is not an idempotency hit.
            raise sqlite3.IntegrityError("job insert did not persist")
        return _model(row, Job)

    def update(self, value: Job) -> Job:
        cursor = self.db.connection.execute(
            """UPDATE jobs
               SET project_id = ?, idempotency_key = ?, status = ?, attempt = ?,
                   created_at = ?, updated_at = ?, error_code = ?, error_message = ?,
                   lease_owner = CASE WHEN ? = 'running' THEN lease_owner ELSE NULL END,
                   lease_expires_at = CASE WHEN ? = 'running' THEN lease_expires_at ELSE NULL END,
                   payload = ?
               WHERE id = ?""",
            (
                None if value.project_id is None else str(value.project_id), value.idempotency_key,
                value.status.value, value.attempt, _utc_timestamp(value.created_at), _utc_timestamp(value.updated_at),
                value.error_code, value.error_message, value.status.value, value.status.value,
                _job_payload(value), str(value.id),
            ),
        )
        if cursor.rowcount != 1:
            raise KeyError(value.id)
        return value
