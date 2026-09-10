"""Local import for user-provided narration or recorded voice audio."""
from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterator, Protocol
from uuid import uuid4

from app.db import AudioAssetRepository, Database
from app.domain.models import AudioAsset, SourceKind

from .ffprobe import AudioProbeMetadata, FFProbeAdapter, ProbeError


class AudioImportError(RuntimeError):
    pass


class AudioProbe(Protocol):
    def probe_audio(self, path: str | Path) -> AudioProbeMetadata: ...


class AudioImporter:
    def __init__(self, db: Database, data_root: str | Path, probe: AudioProbe | None = None) -> None:
        self.db = db
        self.data_root = Path(data_root)
        self.originals = self.data_root / "assets" / "audio-originals"
        self.originals.mkdir(parents=True, exist_ok=True)
        self.probe = probe or FFProbeAdapter()

    def import_path(
        self,
        source: str | Path,
        authorization_reference: str,
        *,
        source_kind: SourceKind = SourceKind.USER_ASSET,
        language: str | None = None,
    ) -> AudioAsset:
        path = Path(source)
        if not path.is_file():
            raise FileNotFoundError(path)
        with path.open("rb") as stream:
            return self.import_stream(
                stream, path.name, authorization_reference, source_kind=source_kind,
                language=language,
            )

    def import_stream(
        self,
        stream: BinaryIO,
        filename: str,
        authorization_reference: str,
        *,
        source_kind: SourceKind = SourceKind.USER_ASSET,
        language: str | None = None,
    ) -> AudioAsset:
        if source_kind not in {SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET}:
            raise AudioImportError("audio source_kind must be user_asset or historical_asset")
        temp = self.originals / f".import-{secrets.token_hex(12)}.tmp"
        digest = hashlib.sha256()
        try:
            with temp.open("xb") as target:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
                    target.write(chunk)
            content_hash = digest.hexdigest()
            repository = AudioAssetRepository(self.db)
            existing = repository.get_by_content_hash(content_hash)
            if existing is not None:
                return self._existing_or_error(existing)
            try:
                metadata = self.probe.probe_audio(temp)
            except ProbeError as exc:
                raise AudioImportError("audio probe failed") from exc
            suffix = Path(filename).suffix.lower() or ".audio"
            if suffix not in {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".webm"}:
                suffix = ".audio"
            destination = self.originals / f"{content_hash}{suffix}"
            value = AudioAsset(
                id=uuid4(), source_kind=source_kind, source_file=str(destination), content_hash=content_hash,
                duration_ms=metadata.duration_ms, sample_rate=metadata.sample_rate, channels=metadata.channels,
                language=language or metadata.language, authorization_reference=authorization_reference,
                imported_at=datetime.now(timezone.utc), metadata={"filename": filename},
            )
            placed = False
            try:
                with self._write_transaction():
                    existing = repository.get_by_content_hash(content_hash)
                    if existing is not None:
                        return self._existing_or_error(existing)
                    repository.create(value)
                    placed = self._place(temp, destination)
                    if not placed:
                        _verify_hash(destination, content_hash)
                return value
            except sqlite3.IntegrityError:
                winner = repository.get_by_content_hash(content_hash)
                if winner is not None:
                    return self._existing_or_error(winner)
                if placed:
                    destination.unlink(missing_ok=True)
                raise
            except Exception:
                if placed:
                    destination.unlink(missing_ok=True)
                raise
        finally:
            temp.unlink(missing_ok=True)

    @staticmethod
    def _place(temp: Path, destination: Path) -> bool:
        try:
            os.link(temp, destination)
        except FileExistsError:
            temp.unlink(missing_ok=True)
            return False
        temp.unlink(missing_ok=True)
        return True

    @staticmethod
    def _existing_or_error(asset: AudioAsset) -> AudioAsset:
        if not Path(asset.source_file).is_file():
            raise AudioImportError("existing audio asset points to missing media")
        return asset

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        self.db.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.connection.rollback()
            raise
        else:
            self.db.connection.commit()


def _verify_hash(path: Path, expected: str) -> None:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != expected:
        raise AudioImportError("retained audio does not match its content hash")
