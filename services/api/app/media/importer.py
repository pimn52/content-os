"""Chunked local media import with content-addressed deduplication."""
from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterator
from uuid import uuid4

from app.db import AssetRepository, Database
from app.domain.models import Asset, SourceKind

from .ffprobe import FFProbeAdapter, ProbeMetadata


class MediaImportError(RuntimeError):
    pass


MAX_CHUNK_SIZE = 64 * 1024 * 1024


class MediaImporter:
    def __init__(self, db: Database, data_root: str | Path, probe: FFProbeAdapter | None = None, chunk_size: int = 1024 * 1024):
        self.db = db
        if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or not 0 < chunk_size <= MAX_CHUNK_SIZE:
            raise ValueError(f"chunk_size must be an integer from 1 to {MAX_CHUNK_SIZE}")
        self.chunk_size = chunk_size
        self.data_root = Path(data_root)
        self.originals = self.data_root / "assets" / "originals"
        self.originals.mkdir(parents=True, exist_ok=True)
        self.probe = probe or FFProbeAdapter()

    def import_path(self, source: str | Path, authorization_reference: str, *, source_kind: SourceKind = SourceKind.USER_ASSET) -> Asset:
        source_path = Path(source)
        if not source_path.is_file():
            raise FileNotFoundError(source_path)
        with source_path.open("rb") as stream:
            return self._import_stream(stream, source_path.name, authorization_reference, source_kind)

    def import_stream(self, stream: BinaryIO, filename: str, authorization_reference: str, *, source_kind: SourceKind = SourceKind.USER_ASSET) -> Asset:
        return self._import_stream(stream, filename, authorization_reference, source_kind)

    def _import_stream(self, stream: BinaryIO, filename: str, authorization_reference: str, source_kind: SourceKind) -> Asset:
        temp = self.originals / f".import-{secrets.token_hex(12)}.tmp"
        digest = hashlib.sha256()
        try:
            with temp.open("xb") as target:
                while True:
                    chunk = stream.read(self.chunk_size)
                    if not chunk:
                        break
                    digest.update(chunk)
                    target.write(chunk)
            content_hash = digest.hexdigest()
            repository = AssetRepository(self.db)
            existing = repository.get_by_content_hash(content_hash)
            if existing is not None:
                return self._existing_or_error(existing)
            # One content hash maps to one retained local original regardless
            # of the source filename or extension supplied by a concurrent
            # importer. ffprobe identifies the file by contents, not suffix.
            destination = self.originals / f"{content_hash}.media"
            metadata = self.probe.probe(temp)
            value = self._asset(destination, content_hash, authorization_reference, source_kind, metadata)
            newly_created = False
            try:
                with self._write_transaction():
                    existing = repository.get_by_content_hash(content_hash)
                    if existing is not None:
                        return self._existing_or_error(existing)
                    repository.create(value)
                    newly_created = self._place(temp, destination)
                    if not newly_created:
                        self._verify_content_hash(destination, content_hash)
                return value
            except sqlite3.IntegrityError as conflict:
                # A contender that wins the unique hash row is the only one
                # allowed to place its final file. This loser never touches it.
                try:
                    winner = repository.get_by_content_hash(content_hash)
                except Exception:
                    raise conflict
                if winner is not None:
                    return self._existing_or_error(winner)
                if newly_created:
                    self._cleanup_created(destination)
                raise
            except Exception:
                if newly_created:
                    self._cleanup_created(destination)
                raise
        finally:
            temp.unlink(missing_ok=True)

    @staticmethod
    def _place(temp: Path, destination: Path) -> bool:
        # Linking is exclusive: unlike an exists/replace sequence it cannot
        # overwrite another importer's retained original. Temp and destination
        # share a directory, so this is an atomic same-volume operation.
        try:
            os.link(temp, destination)
        except FileExistsError:
            temp.unlink(missing_ok=True)
            return False
        else:
            temp.unlink(missing_ok=True)
            return True

    @staticmethod
    def _cleanup_created(path: Path) -> None:
        """Best-effort cleanup must never replace the original persistence error."""
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        """Use a SQLite writer lock as the final-path ownership boundary."""
        self.db.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.connection.rollback()
            raise
        else:
            try:
                self.db.connection.commit()
            except BaseException:
                self.db.connection.rollback()
                raise

    def _existing_or_error(self, asset: Asset) -> Asset:
        path = Path(asset.source_file)
        if not path.is_file():
            raise MediaImportError(f"existing asset {asset.id} points to missing media: {path}")
        return asset

    @staticmethod
    def _verify_content_hash(path: Path, expected_hash: str) -> None:
        if not path.is_file():
            raise MediaImportError(f"retained media path is missing: {path}")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(MAX_CHUNK_SIZE):
                digest.update(chunk)
        if digest.hexdigest() != expected_hash:
            raise MediaImportError(f"retained media path does not match content hash: {path}")

    @staticmethod
    def _asset(path: Path, content_hash: str, authorization_reference: str, source_kind: SourceKind, metadata: ProbeMetadata) -> Asset:
        return Asset(id=uuid4(), source_kind=source_kind, source_file=str(path), content_hash=content_hash, duration_ms=metadata.duration_ms, width=metadata.width, height=metadata.height, fps=metadata.fps, has_audio=metadata.has_audio, authorization_reference=authorization_reference, imported_at=datetime.now(timezone.utc), metadata=metadata.metadata)
