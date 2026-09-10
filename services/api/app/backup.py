"""Portable, local-only backup and restore for the Content OS data root."""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4


class BackupError(RuntimeError):
    """Raised when a backup cannot be safely created, verified, or restored."""


BACKUP_FORMAT_VERSION = 1
_SKIP_LIVE_SUFFIXES = {"-wal", "-shm", "-journal"}
_PATH_PAYLOAD_TABLES = ("assets", "image_assets", "audio_assets")


def create_backup(
    data_root: str | Path,
    database: str | Path,
    output: str | Path,
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Create a consistent zip backup without modifying the source data root."""
    root = _existing_directory(data_root, "data root")
    db_path = _existing_file(database, "database")
    _require_inside(db_path, root, "database")
    archive = Path(output).expanduser().resolve()
    if archive == db_path or archive.is_relative_to(root):
        raise BackupError("backup output must be outside the data root")
    archive.parent.mkdir(parents=True, exist_ok=True)
    if archive.exists():
        if not overwrite:
            raise BackupError(f"backup output already exists: {archive}")
        if not archive.is_file():
            raise BackupError(f"backup output is not a file: {archive}")
        archive.unlink()

    database_member = db_path.relative_to(root).as_posix()
    schema_version = _schema_version(db_path)
    manifest: dict[str, Any]
    with tempfile.TemporaryDirectory(prefix=".content-os-backup-", dir=str(archive.parent)) as temp_dir:
        snapshot = Path(temp_dir) / database_member
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        _sqlite_snapshot(db_path, snapshot)
        files: list[dict[str, Any]] = []
        with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
            for path in sorted(root.rglob("*")):
                if not path.is_file() or _is_live_sqlite_sidecar(path, db_path):
                    continue
                member = database_member if path == db_path else path.relative_to(root).as_posix()
                source = snapshot if path == db_path else path
                digest, size = _file_digest(source)
                files.append({"path": member, "sha256": digest, "size": size})
                bundle.write(source, member)
            manifest = {
                "format_version": BACKUP_FORMAT_VERSION,
                "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "source_data_root": str(root),
                "database_path": database_member,
                "schema_version": schema_version,
                "files": files,
            }
            bundle.writestr("manifest.json", _json_bytes(manifest))
    return manifest | {"archive": str(archive)}


def verify_backup(archive: str | Path) -> dict[str, Any]:
    """Verify archive integrity and every manifest file hash without extracting it."""
    archive_path = _existing_file(archive, "backup archive")
    with zipfile.ZipFile(archive_path) as bundle:
        manifest = _read_manifest(bundle)
        if bundle.testzip() is not None:
            raise BackupError("backup archive contains a corrupt member")
        names = set(bundle.namelist())
        for entry in manifest["files"]:
            member = entry["path"]
            if member not in names:
                raise BackupError(f"backup member is missing: {member}")
            with bundle.open(member, "r") as stream:
                digest, size = _stream_digest(stream)
            if digest != entry["sha256"] or size != entry["size"]:
                raise BackupError(f"backup hash mismatch: {member}")
    return {"archive": str(archive_path), "verified": True, "files": len(manifest["files"]), "schema_version": manifest["schema_version"]}


def restore_backup(archive: str | Path, target_dir: str | Path) -> dict[str, Any]:
    """Restore into a new directory and relocate data-root-owned media paths."""
    archive_path = _existing_file(archive, "backup archive")
    target = Path(target_dir).expanduser().resolve()
    if target.exists():
        raise BackupError(f"restore target must not already exist: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}.restore-{uuid4().hex}"
    try:
        with zipfile.ZipFile(archive_path) as bundle:
            manifest = _read_manifest(bundle)
            for member in bundle.namelist():
                _safe_member(member)
            for member in bundle.namelist():
                if member == "manifest.json" or member.endswith("/"):
                    continue
                destination = staging / Path(*PurePosixPath(member).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(member, "r") as source, destination.open("xb") as sink:
                    shutil.copyfileobj(source, sink)
        _verify_directory(staging, manifest)
        restored_db = staging / Path(*PurePosixPath(manifest["database_path"]).parts)
        _relocate_media_paths(restored_db, Path(manifest["source_data_root"]), target, staging)
        staging.replace(target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {
        "archive": str(archive_path),
        "restored_to": str(target),
        "database": str(target / Path(*PurePosixPath(manifest["database_path"]).parts)),
        "files": len(manifest["files"]),
        "schema_version": manifest["schema_version"],
    }


def _existing_directory(path: str | Path, label: str) -> Path:
    value = Path(path).expanduser().resolve()
    if not value.is_dir():
        raise BackupError(f"{label} does not exist or is not a directory: {value}")
    return value


def _existing_file(path: str | Path, label: str) -> Path:
    value = Path(path).expanduser().resolve()
    if not value.is_file():
        raise BackupError(f"{label} does not exist or is not a file: {value}")
    return value


def _require_inside(path: Path, root: Path, label: str) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise BackupError(f"{label} must be inside the data root") from exc


def _is_live_sqlite_sidecar(path: Path, database: Path) -> bool:
    if path == database:
        return False
    return path.parent == database.parent and any(path.name == database.name + suffix for suffix in _SKIP_LIVE_SUFFIXES)


def _schema_version(database: Path) -> int:
    with sqlite3.connect(str(database)) as connection:
        row = connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
    return int(row[0]) if row else 0


def _sqlite_snapshot(source: Path, destination: Path) -> None:
    source_connection = sqlite3.connect(str(source))
    destination_connection = sqlite3.connect(str(destination))
    try:
        source_connection.backup(destination_connection)
    except sqlite3.Error as exc:
        raise BackupError(f"SQLite snapshot failed: {exc}") from exc
    finally:
        destination_connection.close()
        source_connection.close()


def _file_digest(path: Path) -> tuple[str, int]:
    with path.open("rb") as stream:
        return _stream_digest(stream)


def _stream_digest(stream: Any) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _safe_member(member: str) -> None:
    path = PurePosixPath(member)
    if not member or path.is_absolute() or ".." in path.parts or "\\" in member:
        raise BackupError(f"unsafe backup member: {member!r}")


def _read_manifest(bundle: zipfile.ZipFile) -> dict[str, Any]:
    try:
        manifest = json.loads(bundle.read("manifest.json"))
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError("backup manifest is missing or invalid") from exc
    if not isinstance(manifest, dict) or manifest.get("format_version") != BACKUP_FORMAT_VERSION:
        raise BackupError("unsupported backup format")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise BackupError("backup manifest has no files")
    seen: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise BackupError("backup manifest contains an invalid file entry")
        member = entry["path"]
        _safe_member(member)
        if member in seen or member == "manifest.json":
            raise BackupError(f"duplicate or reserved backup member: {member}")
        if not isinstance(entry.get("sha256"), str) or not isinstance(entry.get("size"), int) or entry["size"] < 0:
            raise BackupError(f"invalid backup hash entry: {member}")
        seen.add(member)
    database_path = manifest.get("database_path")
    if not isinstance(database_path, str) or database_path not in seen:
        raise BackupError("backup database member is missing")
    if not isinstance(manifest.get("source_data_root"), str) or not isinstance(manifest.get("schema_version"), int):
        raise BackupError("backup manifest metadata is invalid")
    return manifest


def _verify_directory(root: Path, manifest: dict[str, Any]) -> None:
    for entry in manifest["files"]:
        path = root / Path(*PurePosixPath(entry["path"]).parts)
        if not path.is_file():
            raise BackupError(f"restored file is missing: {entry['path']}")
        digest, size = _file_digest(path)
        if digest != entry["sha256"] or size != entry["size"]:
            raise BackupError(f"restored file hash mismatch: {entry['path']}")
def _relocate_media_paths(database: Path, old_root: Path, new_root: Path, physical_root: Path) -> None:
    if not database.is_file():
        raise BackupError(f"restored database is missing: {database}")
    connection = sqlite3.connect(str(database))
    try:
        connection.execute("BEGIN")
        for table in _PATH_PAYLOAD_TABLES:
            rows = connection.execute(f"SELECT id, payload FROM {table}").fetchall()
            for item_id, raw_payload in rows:
                try:
                    payload = json.loads(raw_payload)
                except (TypeError, json.JSONDecodeError) as exc:
                    raise BackupError(f"invalid payload in restored table {table}") from exc
                source_file = payload.get("source_file") if isinstance(payload, dict) else None
                relative = _backup_relative_path(source_file, old_root)
                if relative is None:
                    continue
                if not (physical_root / relative).is_file():
                    raise BackupError(f"restored media file is missing: {relative}")
                payload["source_file"] = str(new_root / relative)
                connection.execute(
                    f"UPDATE {table} SET payload = ? WHERE id = ?",
                    (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")), item_id),
                )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _backup_relative_path(source_file: Any, old_root: Path) -> Path | None:
    if not isinstance(source_file, str) or not source_file:
        return None
    candidate = Path(source_file)
    if candidate.is_absolute():
        try:
            return candidate.resolve().relative_to(old_root)
        except ValueError:
            return None
    normalized = source_file.replace("\\", "/")
    old_name = old_root.name.replace("\\", "/")
    for prefix in (old_name, old_root.as_posix()):
        if normalized == prefix:
            return Path(".")
        if normalized.startswith(prefix + "/"):
            return Path(*PurePosixPath(normalized[len(prefix) + 1 :]).parts)
    if normalized.startswith("assets/"):
        return Path(*PurePosixPath(normalized).parts)
    return None
