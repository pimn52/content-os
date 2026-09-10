from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

import pytest

from app.backup import BackupError, create_backup, restore_backup, verify_backup
from app.db import AssetRepository, Database
from app.domain.models import Asset, RationalFps


def test_backup_verify_and_restore_relocate_local_asset(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    media = data_root / "assets" / "originals" / "clip.media"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"local media")
    database = data_root / "content-os.sqlite3"
    asset = Asset(
        source_file=str(media), content_hash="a" * 64, duration_ms=1_000, width=320, height=180,
        fps=RationalFps(numerator=25, denominator=1), authorization_reference="local-test",
        imported_at=datetime.now(timezone.utc),
    )
    with Database(database) as db:
        AssetRepository(db).create(asset)

    archive = tmp_path / "backup.zip"
    created = create_backup(data_root, database, archive)
    assert created["schema_version"] == 18
    assert verify_backup(archive)["verified"] is True

    restored_root = tmp_path / "restored"
    restored = restore_backup(archive, restored_root)
    assert restored["restored_to"] == str(restored_root.resolve())
    with Database(restored_root / "content-os.sqlite3") as db:
        restored_asset = AssetRepository(db).get(asset.id)
    assert restored_asset is not None
    assert Path(restored_asset.source_file) == restored_root / "assets" / "originals" / "clip.media"
    assert Path(restored_asset.source_file).read_bytes() == b"local media"


def test_restore_refuses_existing_target_and_backup_inside_data_root(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    database = data_root / "content-os.sqlite3"
    with Database(database):
        pass
    with pytest.raises(BackupError, match="outside the data root"):
        create_backup(data_root, database, data_root / "backup.zip")

    archive = tmp_path / "backup.zip"
    create_backup(data_root, database, archive)
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(BackupError, match="must not already exist"):
        restore_backup(archive, existing)


def test_backup_cli_emits_unicode_paths_safely_for_windows_console(tmp_path: Path) -> None:
    data_root = tmp_path / "本地数据"
    data_root.mkdir()
    database = data_root / "content-os.sqlite3"
    with Database(database):
        pass
    archive = tmp_path / "备份.zip"
    completed = subprocess.run(
        [sys.executable, "scripts/backup_local.py", "create", "--data-root", str(data_root), "--output", str(archive)],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[3],
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["archive"] == str(archive.resolve())
