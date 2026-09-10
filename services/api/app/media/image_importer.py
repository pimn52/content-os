"""Dependency-free local image import for screenshot/chart visuals."""
from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import struct
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterator
from uuid import uuid4

from app.db import Database, ImageAssetRepository
from app.domain.models import ImageAsset, SourceKind


class ImageImportError(RuntimeError):
    pass


MAX_IMAGE_BYTES = 128 * 1024 * 1024
_JPEG_SOF_MARKERS = {
    0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
    0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
}


class ImageImporter:
    def __init__(self, db: Database, data_root: str | Path) -> None:
        self.db = db
        self.data_root = Path(data_root)
        self.images = self.data_root / "assets" / "images"
        self.images.mkdir(parents=True, exist_ok=True)

    def import_path(
        self,
        source: str | Path,
        authorization_reference: str,
        *,
        source_kind: SourceKind = SourceKind.SCREENSHOT,
    ) -> ImageAsset:
        path = Path(source)
        if not path.is_file():
            raise FileNotFoundError(path)
        with path.open("rb") as stream:
            return self.import_stream(stream, path.name, authorization_reference, source_kind=source_kind)

    def import_stream(
        self,
        stream: BinaryIO,
        filename: str,
        authorization_reference: str,
        *,
        source_kind: SourceKind = SourceKind.SCREENSHOT,
    ) -> ImageAsset:
        if source_kind not in {SourceKind.SCREENSHOT, SourceKind.CHART}:
            raise ImageImportError("image source_kind must be screenshot or chart")
        temp = self.images / f".import-{secrets.token_hex(12)}.tmp"
        digest = hashlib.sha256()
        size = 0
        try:
            with temp.open("xb") as target:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_IMAGE_BYTES:
                        raise ImageImportError("image exceeds the local size limit")
                    digest.update(chunk)
                    target.write(chunk)
            content_hash = digest.hexdigest()
            repository = ImageAssetRepository(self.db)
            existing = repository.get_by_content_hash(content_hash)
            if existing is not None:
                return self._existing_or_error(existing)
            width, height, image_format = _probe_image(temp)
            suffix = ".jpg" if image_format == "jpeg" else f".{image_format}"
            destination = self.images / f"{content_hash}{suffix}"
            value = ImageAsset(
                id=uuid4(), source_kind=source_kind, source_file=str(destination), content_hash=content_hash,
                width=width, height=height, authorization_reference=authorization_reference,
                imported_at=datetime.now(timezone.utc), metadata={"filename": filename, "format": image_format},
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
    def _existing_or_error(asset: ImageAsset) -> ImageAsset:
        if not Path(asset.source_file).is_file():
            raise ImageImportError("existing image asset points to missing media")
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


def _probe_image(path: Path) -> tuple[int, int, str]:
    data = path.read_bytes()
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        if width > 0 and height > 0:
            return width, height, "png"
    if data.startswith(b"\xff\xd8"):
        width, height = _jpeg_dimensions(data)
        return width, height, "jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP" and len(data) >= 30 and data[12:16] == b"VP8X":
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        if width > 0 and height > 0:
            return width, height, "webp"
    raise ImageImportError("unsupported or malformed image; supported formats are PNG, JPEG, and VP8X WebP")


def _jpeg_dimensions(data: bytes) -> tuple[int, int]:
    offset = 2
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            offset += 1
            continue
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(data):
            break
        length = int.from_bytes(data[offset:offset + 2], "big")
        if length < 2 or offset + length > len(data):
            break
        if marker in _JPEG_SOF_MARKERS and length >= 7:
            height = int.from_bytes(data[offset + 3:offset + 5], "big")
            width = int.from_bytes(data[offset + 5:offset + 7], "big")
            if width > 0 and height > 0:
                return width, height
        offset += length
    raise ImageImportError("JPEG has no valid dimensions")


def _verify_hash(path: Path, expected: str) -> None:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != expected:
        raise ImageImportError("retained image does not match its content hash")
