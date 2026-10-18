from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.db.database import Database
from app.db.migrations import CURRENT_SCHEMA_VERSION


def _migration_versions(path: Path) -> tuple[int, ...]:
    with sqlite3.connect(path) as connection:
        return tuple(row[0] for row in connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ))


def test_existing_schema_startup_skips_migrations_and_preserves_journal_mode(tmp_path: Path) -> None:
    path = tmp_path / "existing.sqlite3"
    Database(path).close()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA journal_mode = DELETE")

    before_versions = _migration_versions(path)
    with Database(path, startup_mode="existing") as database:
        assert database.connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert tuple(row[0] for row in database.connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        )) == before_versions

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert _migration_versions(path) == before_versions
    assert before_versions[-1] == CURRENT_SCHEMA_VERSION


def test_existing_schema_startup_does_not_create_missing_database(tmp_path: Path) -> None:
    path = tmp_path / "missing.sqlite3"

    with pytest.raises(FileNotFoundError, match="existing database does not exist"):
        Database(path, startup_mode="existing")

    assert not path.exists()


def test_existing_schema_startup_rejects_old_migration_history_without_repair(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    Database(path).close()
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM schema_migrations WHERE version = ?", (CURRENT_SCHEMA_VERSION,))

    before_versions = _migration_versions(path)
    with pytest.raises(RuntimeError, match="does not match current version"):
        Database(path, startup_mode="existing")

    assert _migration_versions(path) == before_versions
    assert before_versions[-1] == CURRENT_SCHEMA_VERSION - 1


def test_app_existing_startup_from_environment_serves_without_migration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient
    from app.main import create_app

    path = tmp_path / "api.sqlite3"
    Database(path).close()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA journal_mode = DELETE")
    before_versions = _migration_versions(path)

    def reject_migration(*args: object) -> None:
        raise AssertionError("existing-schema application startup must not call migrations")

    monkeypatch.setattr("app.db.database.apply_migrations", reject_migration)
    monkeypatch.setenv("CONTENT_OS_DB_PATH", str(path))
    monkeypatch.setenv("CONTENT_OS_DB_STARTUP_MODE", "existing")
    with TestClient(create_app()) as client:
        assert client.get("/health").json()["status"] == "ok"
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert _migration_versions(path) == before_versions
