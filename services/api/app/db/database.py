"""Small SQLite database wrapper used by the Content OS repositories."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .migrations import apply_migrations


class Database:
    """A configured connection and an explicit transaction boundary."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path not in (":memory:", "") and not self.path.startswith("file:"):
            parent = Path(self.path).parent
            if str(parent) not in ("", "."):
                parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        # WAL is useful for a local file and SQLite reports memory for :memory:.
        if self.path != ":memory:":
            self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = NORMAL")
        apply_migrations(self.connection)

    @property
    def conn(self) -> sqlite3.Connection:
        """Compatibility alias for callers that prefer ``db.conn``."""
        return self.connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Commit all writes, or roll all of them back if the block fails."""
        self.connection.execute("BEGIN")
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
