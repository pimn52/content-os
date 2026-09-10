"""Versioned, repeatable SQLite migrations."""
from __future__ import annotations

import sqlite3

CURRENT_SCHEMA_VERSION = 17

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
    (2, (
        "ALTER TABLE assets ADD COLUMN content_hash TEXT",
        """UPDATE assets SET content_hash = json_extract(payload, '$.content_hash')
           WHERE content_hash IS NULL AND json_valid(payload)
             AND json_type(payload, '$.content_hash') = 'text'""",
        """CREATE TABLE _migration_asset_hash_validation (
               content_hash TEXT NOT NULL CHECK (length(trim(content_hash)) > 0)
           )""",
        "INSERT INTO _migration_asset_hash_validation(content_hash) SELECT content_hash FROM assets",
        "DROP TABLE _migration_asset_hash_validation",
        "CREATE UNIQUE INDEX IF NOT EXISTS assets_content_hash_uq ON assets(content_hash)",
        """CREATE TRIGGER IF NOT EXISTS assets_content_hash_required_insert
           BEFORE INSERT ON assets
           WHEN NEW.content_hash IS NULL OR length(trim(NEW.content_hash)) = 0
           BEGIN SELECT RAISE(ABORT, 'assets.content_hash is required'); END""",
        """CREATE TRIGGER IF NOT EXISTS assets_content_hash_required_update
           BEFORE UPDATE OF content_hash ON assets
           WHEN NEW.content_hash IS NULL OR length(trim(NEW.content_hash)) = 0
           BEGIN SELECT RAISE(ABORT, 'assets.content_hash is required'); END""",
    )),
    (3, (
        """CREATE TABLE IF NOT EXISTS asset_job_targets (
               job_id TEXT PRIMARY KEY NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
               asset_id TEXT NOT NULL REFERENCES assets(id)
           )""",
        "CREATE INDEX IF NOT EXISTS asset_job_targets_asset_idx ON asset_job_targets(asset_id)",
    )),
    (4, (
        """CREATE TABLE IF NOT EXISTS clip_search_metadata (
               singleton INTEGER PRIMARY KEY NOT NULL CHECK (singleton = 1),
               embedding_dim INTEGER NOT NULL CHECK (embedding_dim > 0)
           )""",
        """CREATE TABLE IF NOT EXISTS clip_search_index (
               clip_id TEXT PRIMARY KEY NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
               asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
               orientation TEXT,
               talking_candidate INTEGER NOT NULL CHECK (talking_candidate IN (0, 1)),
               searchable_text TEXT NOT NULL,
               embedding TEXT NOT NULL,
               embedding_dim INTEGER NOT NULL CHECK (embedding_dim > 0)
           )""",
        "CREATE INDEX IF NOT EXISTS clip_search_asset_idx ON clip_search_index(asset_id)",
        "CREATE INDEX IF NOT EXISTS clip_search_filter_idx ON clip_search_index(orientation, talking_candidate)",
    )),
    (5, (
        """CREATE TABLE IF NOT EXISTS project_drafts (
               project_id TEXT PRIMARY KEY NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
               version INTEGER NOT NULL CHECK (version >= 0),
               updated_at TEXT NOT NULL,
               payload TEXT NOT NULL
           )""",
    )),
    (6, (
        """CREATE TABLE IF NOT EXISTS ip_profile_revisions (
               profile_id TEXT NOT NULL REFERENCES ip_profiles(id) ON DELETE CASCADE,
               version INTEGER NOT NULL CHECK (version > 0),
               created_at TEXT NOT NULL,
               payload TEXT NOT NULL,
               PRIMARY KEY(profile_id, version)
           )""",
    )),
    (7, (
        """CREATE TABLE IF NOT EXISTS analysis_result_bundles (
               id TEXT PRIMARY KEY NOT NULL,
               input_hash TEXT NOT NULL UNIQUE,
               mode TEXT NOT NULL CHECK (mode IN ('assisted_test', 'runtime')),
               analyzed_at TEXT NOT NULL,
               payload TEXT NOT NULL
           )""",
        "CREATE INDEX IF NOT EXISTS analysis_result_bundles_analyzed_idx ON analysis_result_bundles(analyzed_at)",
    )),
    (8, (
        """CREATE TABLE IF NOT EXISTS image_assets (
               id TEXT PRIMARY KEY NOT NULL,
               content_hash TEXT NOT NULL UNIQUE,
               payload TEXT NOT NULL
           )""",
    )),
    (9, (
        """CREATE TABLE IF NOT EXISTS audio_assets (
               id TEXT PRIMARY KEY NOT NULL,
               content_hash TEXT NOT NULL UNIQUE,
               payload TEXT NOT NULL
           )""",
    )),
    (10, (
        """CREATE TABLE IF NOT EXISTS asset_usage_events (
               id TEXT PRIMARY KEY NOT NULL,
               project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
               event_key TEXT NOT NULL UNIQUE,
               payload TEXT NOT NULL
           )""",
        "CREATE INDEX IF NOT EXISTS asset_usage_project_idx ON asset_usage_events(project_id)",
    )),
    (11, (
        """CREATE TABLE IF NOT EXISTS publication_records (
               id TEXT PRIMARY KEY NOT NULL,
               project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
               output_version TEXT NOT NULL,
               platform TEXT NOT NULL,
               payload TEXT NOT NULL,
               UNIQUE(project_id, output_version, platform)
           )""",
        "CREATE INDEX IF NOT EXISTS publication_project_idx ON publication_records(project_id)",
    )),
    (12, (
        """CREATE TABLE IF NOT EXISTS content_feedback (
               id TEXT PRIMARY KEY NOT NULL,
               project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
               output_version TEXT NOT NULL,
               payload TEXT NOT NULL,
               UNIQUE(project_id, output_version)
           )""",
        "CREATE INDEX IF NOT EXISTS feedback_project_idx ON content_feedback(project_id)",
    )),
    (13, (
        """CREATE TABLE IF NOT EXISTS budget_policies (
               id TEXT PRIMARY KEY NOT NULL,
               scope_key TEXT NOT NULL UNIQUE,
               payload TEXT NOT NULL
           )""",
        """CREATE TABLE IF NOT EXISTS provider_call_records (
               id TEXT PRIMARY KEY NOT NULL,
               project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
               idempotency_key TEXT NOT NULL UNIQUE,
               status TEXT NOT NULL,
               created_at TEXT NOT NULL,
               completed_at TEXT,
               payload TEXT NOT NULL
           )""",
        "CREATE INDEX IF NOT EXISTS provider_calls_project_idx ON provider_call_records(project_id, created_at, id)",
        "CREATE INDEX IF NOT EXISTS provider_calls_status_idx ON provider_call_records(status)",
    )),
    (14, (
        """CREATE TABLE IF NOT EXISTS content_opportunities (
               id TEXT PRIMARY KEY NOT NULL,
               dedupe_key TEXT NOT NULL UNIQUE,
               status TEXT NOT NULL,
               created_at TEXT NOT NULL,
               payload TEXT NOT NULL
           )""",
        "CREATE INDEX IF NOT EXISTS content_opportunities_status_idx ON content_opportunities(status, created_at)",
    )),
    (15, (
        """CREATE TABLE IF NOT EXISTS account_connections (
               id TEXT PRIMARY KEY NOT NULL,
               provider TEXT NOT NULL,
               account_external_id TEXT NOT NULL,
               payload TEXT NOT NULL,
               UNIQUE(provider, account_external_id)
           )""",
        "CREATE INDEX IF NOT EXISTS account_connections_provider_idx ON account_connections(provider)",
        """CREATE TABLE IF NOT EXISTS historical_content (
               id TEXT PRIMARY KEY NOT NULL,
               account_connection_id TEXT NOT NULL REFERENCES account_connections(id) ON DELETE CASCADE,
               external_id TEXT NOT NULL,
               payload TEXT NOT NULL,
               UNIQUE(account_connection_id, external_id)
           )""",
        "CREATE INDEX IF NOT EXISTS historical_content_account_idx ON historical_content(account_connection_id)",
    )),
    (16, (
        """CREATE TABLE IF NOT EXISTS voice_profiles (
               id TEXT PRIMARY KEY NOT NULL,
               payload TEXT NOT NULL
           )""",
        "CREATE INDEX IF NOT EXISTS voice_profiles_provider_idx ON voice_profiles(json_extract(payload, '$.provider'))",
        """CREATE TABLE IF NOT EXISTS talking_profiles (
               id TEXT PRIMARY KEY NOT NULL,
               payload TEXT NOT NULL
           )""",
        "CREATE INDEX IF NOT EXISTS talking_profiles_provider_idx ON talking_profiles(json_extract(payload, '$.provider'))",
    )),
    (17, (
        """CREATE TABLE IF NOT EXISTS shoot_tasks (
               id TEXT PRIMARY KEY NOT NULL,
               project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
               scene_plan_id TEXT NOT NULL,
               status TEXT NOT NULL CHECK (status IN ('confirmed', 'fulfilled', 'dismissed')),
               asset_id TEXT REFERENCES assets(id),
               created_at TEXT NOT NULL,
               updated_at TEXT NOT NULL,
               payload TEXT NOT NULL,
               UNIQUE(project_id, scene_plan_id)
           )""",
        "CREATE INDEX IF NOT EXISTS shoot_tasks_project_idx ON shoot_tasks(project_id, created_at, id)",
        "CREATE INDEX IF NOT EXISTS shoot_tasks_asset_idx ON shoot_tasks(asset_id)",
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
