"""Versioned, repeatable SQLite migrations."""
from __future__ import annotations

import sqlite3

CURRENT_SCHEMA_VERSION = 1

_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (1, (
    """CREATE TABLE IF NOT EXISTS ip_profiles (
        id TEXT PRIMARY KEY NOT NULL,
        payload TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS projects (
        id TEXT PRIMARY KEY NOT NULL,
        ip_profile_id TEXT NOT NULL REFERENCES ip_profiles(id),
        payload TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS assets (
        id TEXT PRIMARY KEY NOT NULL,
        duration_ms INTEGER NOT NULL CHECK (duration_ms > 0),
        payload TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS clips (
        id TEXT PRIMARY KEY NOT NULL,
        asset_id TEXT NOT NULL REFERENCES assets(id),
        start_ms INTEGER NOT NULL CHECK (start_ms >= 0),
        end_ms INTEGER NOT NULL CHECK (end_ms > start_ms),
        asset_duration_ms INTEGER NOT NULL CHECK (asset_duration_ms > 0),
        payload TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY NOT NULL,
        project_id TEXT REFERENCES projects(id),
        idempotency_key TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL,
        attempt INTEGER NOT NULL CHECK (attempt >= 0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        error_code TEXT,
        error_message TEXT,
        lease_owner TEXT,
        lease_expires_at TEXT,
        payload TEXT NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS jobs_claimable_idx
        ON jobs(status, lease_expires_at, created_at, id)""",
    )),
)


def apply_migrations(connection: sqlite3.Connection) -> int:
    connection.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY NOT NULL,
        applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    for version, sql in _MIGRATIONS:
        if connection.execute("SELECT 1 FROM schema_migrations WHERE version = ?", (version,)).fetchone() is None:
            connection.execute("BEGIN")
            try:
                for statement in sql:
                    connection.execute(statement)
                connection.execute("INSERT INTO schema_migrations(version) VALUES (?)", (version,))
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()
    return CURRENT_SCHEMA_VERSION
