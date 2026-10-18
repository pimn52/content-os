"""Small SQLite database wrapper used by the Content OS repositories."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .migrations import CURRENT_SCHEMA_VERSION, MIGRATION_VERSIONS, apply_migrations


class Database:
    """A configured connection and an explicit transaction boundary."""

    def __init__(self, path: str | Path = ":memory:", *, startup_mode: str = "migrate") -> None:
        self.path = str(path)
        if startup_mode not in ("migrate", "existing"):
            raise ValueError("database startup mode must be 'migrate' or 'existing'")
        if startup_mode == "existing":
            if self.path in (":memory:", "") or self.path.startswith("file:"):
                raise ValueError("existing-schema startup requires an existing filesystem database path")
            existing_path = Path(self.path).resolve()
            if not existing_path.is_file():
                raise FileNotFoundError(f"existing database does not exist: {existing_path}")
            connection_target = f"{existing_path.as_uri()}?mode=rw"
            self.connection = sqlite3.connect(connection_target, uri=True, isolation_level=None)
        else:
            if self.path not in (":memory:", "") and not self.path.startswith("file:"):
                parent = Path(self.path).parent
                if str(parent) not in ("", "."):
                    parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(self.path, isolation_level=None)

        try:
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys = ON")
            self.connection.execute("PRAGMA busy_timeout = 5000")
            if startup_mode == "migrate":
                # WAL is useful for the ordinary local application startup.
                if self.path != ":memory:":
                    self.connection.execute("PRAGMA journal_mode = WAL")
                self.connection.execute("PRAGMA synchronous = NORMAL")
                apply_migrations(self.connection)
            else:
                self._verify_current_schema()
        except BaseException:
            self.connection.close()
            raise

    def _verify_current_schema(self) -> None:
        try:
            versions = tuple(row[0] for row in self.connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ))
        except sqlite3.Error as exc:
            raise RuntimeError("existing database has no readable schema migration history") from exc
        if versions != MIGRATION_VERSIONS or not versions or versions[-1] != CURRENT_SCHEMA_VERSION:
            actual = versions[-1] if versions else 0
            raise RuntimeError(
                f"existing database schema version {actual} does not match current version {CURRENT_SCHEMA_VERSION}"
            )

    @property
    def conn(self) -> sqlite3.Connection:
        """Compatibility alias for callers that prefer ``db.conn``."""
        return self.connection

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """Commit all writes, or roll all of them back if the block fails.

        ``immediate`` acquires SQLite's write reservation before callers read
        mutable budget state.  Provider-call reservations use it so two local
        processes cannot both observe spare budget and then overspend it.
        """
        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def open_database(path: str | Path = ":memory:") -> Database:
    return Database(path)
