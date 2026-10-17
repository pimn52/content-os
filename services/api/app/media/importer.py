"""Chunked local media import with content-addressed deduplication."""
from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterator
from uuid import uuid4

from app.db import AssetRepository, Database
from app.domain.models import Asset, Job, SourceKind
from app.execution_scope import register_generated_import_scope

from .ffprobe import FFProbeAdapter, ProbeMetadata


class MediaImportError(RuntimeError):
    pass


MAX_CHUNK_SIZE = 64 * 1024 * 1024


def _place_exclusive(temp: Path, destination: Path) -> bool:
    """Retain a temp file without overwriting a content-addressed destination."""
    try:
        os.link(temp, destination)
    except FileExistsError:
        temp.unlink(missing_ok=True)
        return False
    except PermissionError:
        created_destination = False
        destination_exists = False
        try:
            with temp.open("rb") as source:
                try:
                    target = destination.open("xb")
                except FileExistsError:
                    destination_exists = True
                else:
                    created_destination = True
                    with target:
                        shutil.copyfileobj(source, target)
                        target.flush()
                        os.fsync(target.fileno())
            if destination_exists:
                temp.unlink(missing_ok=True)
                return False
            temp.unlink(missing_ok=True)
        except BaseException:
            if created_destination:
                try:
                    destination.unlink(missing_ok=True)
                except OSError:
                    pass
            raise
        return True
    try:
        temp.unlink(missing_ok=True)
    except BaseException:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return True


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

    def import_path(self, source: str | Path, authorization_reference: str, *, source_kind: SourceKind = SourceKind.USER_ASSET, generated_job: Job | None = None) -> Asset:
        source_path = Path(source)
        if not source_path.is_file():
            raise FileNotFoundError(source_path)
        with source_path.open("rb") as stream:
            return self._import_stream(stream, source_path.name, authorization_reference, source_kind, generated_job)

    def import_stream(self, stream: BinaryIO, filename: str, authorization_reference: str, *, source_kind: SourceKind = SourceKind.USER_ASSET) -> Asset:
        return self._import_stream(stream, filename, authorization_reference, source_kind)

    def prepare_path(self, source: str | Path, authorization_reference: str, *, source_kind: SourceKind) -> PreparedMediaImport:
        """Observe/copy media outside a writer lock; grant no scope or DB row.

        The application must retain the result inside its own transaction and
        atomically write its producer provenance and scope there. This is not a
        replacement for the normal guarded import entry point.
        """
        if self.db.connection.in_transaction:
            raise MediaImportError("media_preparation_inside_transaction")
        source_path = Path(source)
        temp = self.originals / f".import-{secrets.token_hex(12)}.tmp"
        try:
            with source_path.open("rb") as stream:
                content_hash = self._copy_stream(stream, temp)
            metadata = self.probe.probe(temp)
            destination = self.originals / f"{content_hash}.media"
            value = self._asset(destination, content_hash, authorization_reference, source_kind, metadata)
            return PreparedMediaImport(self, temp, value)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise

    def _copy_stream(self, stream: BinaryIO, temp: Path) -> str:
        digest = hashlib.sha256()
        with temp.open("xb") as target:
            while chunk := stream.read(self.chunk_size):
                digest.update(chunk)
                target.write(chunk)
        return digest.hexdigest()

    def _import_stream(self, stream: BinaryIO, filename: str, authorization_reference: str, source_kind: SourceKind, generated_job: Job | None = None) -> Asset:
        temp = self.originals / f".import-{secrets.token_hex(12)}.tmp"
        try:
            content_hash = self._copy_stream(stream, temp)
            repository = AssetRepository(self.db)
            existing = repository.get_by_content_hash(content_hash)
            if existing is not None:
                if generated_job is None:
                    return self._existing_or_error(existing)
                with self._write_transaction():
                    register_generated_import_scope(self.db, "asset", existing, generated_job)
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
                        register_generated_import_scope(self.db, "asset", existing, generated_job)
                        return self._existing_or_error(existing)
                    repository.create(value)
                    register_generated_import_scope(self.db, "asset", value, generated_job)
                    newly_created = self._place(temp, destination)
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
                    with self._write_transaction():
                        register_generated_import_scope(self.db, "asset", winner, generated_job)
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
        return _place_exclusive(temp, destination)

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


class PreparedMediaImport:
    """Private, single-use media staging owned through a ledger transaction.

    Hash aliases are deliberately rejected here: only the owning application
    can authenticate an existing result, rather than laundering old metadata
    or grants through ordinary importer deduplication.
    """
    def __init__(self, importer: MediaImporter, temp: Path, asset: Asset):
        self._importer, self._temp = importer, temp
        self._asset_json = asset.model_dump_json()
        self._state, self._placed = "prepared", False
        self._verified_stat = None

    @property
    def asset(self) -> Asset:
        return Asset.model_validate_json(self._asset_json)

    def verify(self) -> None:
        if self._state not in ("prepared", "verified"):
            raise MediaImportError("media_preparation_consumed")
        if self._importer.db.connection.in_transaction:
            raise MediaImportError("media_observation_inside_transaction")
        self._importer._verify_content_hash(self._temp, self.asset.content_hash)
        self._verified_stat = self._temp.stat()
        self._state = "verified"

    def _claim_retention(self, metadata: dict) -> Asset:
        if self._state != "verified":
            raise MediaImportError("media_preparation_consumed")
        db = self._importer.db
        if not db.connection.in_transaction:
            raise MediaImportError("media_retention_transaction_required")
        self._state = "consumed"
        if self._temp.stat() != self._verified_stat:
            raise MediaImportError("media_preparation_changed")
        value = self.asset.model_copy(update={"metadata": dict(metadata)})
        repository = AssetRepository(db)
        if repository.get_by_content_hash(value.content_hash) is not None:
            raise MediaImportError("media_retention_hash_alias_requires_replay")
        return value

    def _place_retained(self, value: Asset) -> Asset:
        self._placed = self._importer._place(self._temp, Path(value.source_file))
        if not self._placed:
            raise MediaImportError("media_retention_path_conflict")
        return value

    def _retain(self, metadata: dict) -> Asset:
        """Internal ordinary row/file primitive; normal repository guards apply."""
        value = self._claim_retention(metadata)
        AssetRepository(self._importer.db).create(value)
        return self._place_retained(value)

    def _retain_evaluation_talking(self, service, operation, job, context, metadata: dict) -> Asset:
        """Named role writer; never a callback/permission flag on generic import."""
        from app.evaluation_talking import EvaluationTalkingService
        if type(service) is not EvaluationTalkingService or service.db is not self._importer.db:
            raise MediaImportError("media_retention_owned_service_required")
        value = self._claim_retention(metadata)
        service._pin_output(operation,job,context,value)
        # Ordinary repository writes intentionally invoke the generic producer
        # guard. This exact application writer has different authority roles.
        self._importer.db.connection.execute("INSERT INTO assets(id,duration_ms,content_hash,payload) VALUES (?,?,?,?)",
            (str(value.id),value.duration_ms,value.content_hash,value.model_dump_json()))
        return self._place_retained(value)

    def close(self) -> None:
        # Close after the owning transaction ends: rolled-back bytes must not
        # survive, while a successfully committed original must remain intact.
        if self._importer.db.connection.in_transaction:
            raise MediaImportError("media_cleanup_inside_transaction")
        self._temp.unlink(missing_ok=True)
        if self._placed:
            value = self.asset
            retained = AssetRepository(self._importer.db).get_by_content_hash(value.content_hash)
            if retained is None:
                self._importer._cleanup_created(Path(value.source_file))
        self._state = "closed"

    def quarantine(self, directory: Path) -> Path:
        """Retain unadmitted bytes privately after a failed outer commit."""
        if self._importer.db.connection.in_transaction:
            raise MediaImportError("media_cleanup_inside_transaction")
        value = self.asset
        if AssetRepository(self._importer.db).get(value.id) is not None:
            raise MediaImportError("media_quarantine_committed_asset")
        source = Path(value.source_file) if self._placed else self._temp
        self._importer._verify_content_hash(source, value.content_hash)
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / f"{value.id}.quarantine"
        if not self._importer._place(source, destination):
            raise MediaImportError("media_quarantine_path_conflict")
        self._placed = False
        self._state = "quarantined"
        return destination

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
