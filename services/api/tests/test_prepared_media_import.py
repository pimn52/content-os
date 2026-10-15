"""Synthetic media bytes/static probe; no FFmpeg, model or real DB adoption."""
from pathlib import Path
import pytest
from app.db import Database, AssetRepository
from app.domain.models import SourceKind
from app.media.importer import MediaImporter, MediaImportError
from test_media_import import _StaticProbe


@pytest.fixture
def media(tmp_path):
    with Database() as db:
        source = tmp_path / "synthetic.mp4"
        source.write_bytes(b"synthetic video bytes, not a playable artifact")
        yield db, MediaImporter(db, tmp_path / "data", _StaticProbe()), source


def prepared(media):
    db, importer, source = media
    return importer.prepare_path(source, "synthetic rights", source_kind=SourceKind.AI_VIDEO)


def test_preparation_observes_without_creating_asset_or_scope(media):
    db, _, _ = media
    with prepared(media) as item:
        assert AssetRepository(db).get(item.asset.id) is None
        assert not Path(item.asset.source_file).exists()
        assert db.connection.execute("SELECT count(*) FROM execution_use_scopes").fetchone()[0] == 0
    assert not list(media[1].originals.glob(".import-*"))


def test_commit_joins_outer_transaction_without_nested_begin(media):
    db, _, _ = media
    with prepared(media) as item:
        item.verify()
        with db.transaction(immediate=True):
            asset = item._retain({"synthetic_producer": "test-only"})
            assert db.connection.in_transaction
        assert AssetRepository(db).get(asset.id) == asset
    assert Path(asset.source_file).is_file()


def test_outer_rollback_removes_only_our_file(media):
    db, _, _ = media
    with prepared(media) as item:
        item.verify()
        with pytest.raises(ValueError, match="sidecar rejected"):
            with db.transaction(immediate=True):
                asset = item._retain({})
                raise ValueError("sidecar rejected")
        assert AssetRepository(db).get(asset.id) is None
    assert not Path(asset.source_file).exists()


def test_alias_needs_authenticated_application_replay(media):
    db, importer, source = media
    winner = importer.import_path(source, "old rights")
    with prepared(media) as item:
        item.verify()
        with pytest.raises(MediaImportError, match="hash_alias"):
            with db.transaction(immediate=True):
                item._retain({"new_producer": "cannot overwrite"})
    assert AssetRepository(db).get(winner.id) == winner
    assert Path(winner.source_file).read_bytes() == source.read_bytes()


def test_destination_conflict_never_overwrites_existing_bytes(media):
    db, _, _ = media
    with prepared(media) as item:
        item.verify()
        destination = Path(item.asset.source_file)
        destination.write_bytes(b"unowned retained path")
        with pytest.raises(MediaImportError, match="path_conflict"):
            with db.transaction(immediate=True):
                item._retain({})
    assert destination.read_bytes() == b"unowned retained path"
    assert AssetRepository(db).get(item.asset.id) is None


def test_changed_staging_and_second_retention_denied(media):
    db, _, _ = media
    with prepared(media) as item:
        item.verify()
        item._temp.write_bytes(b"changed staging")
        with pytest.raises(MediaImportError, match="changed"):
            with db.transaction(immediate=True):
                item._retain({})
        with pytest.raises(MediaImportError, match="consumed"):
            with db.transaction(immediate=True):
                item._retain({})
    assert AssetRepository(db).get(item.asset.id) is None


def test_expensive_observation_and_cleanup_forbidden_inside_lock(media):
    db, importer, source = media
    with prepared(media) as item:
        with db.transaction(immediate=True):
            with pytest.raises(MediaImportError, match="preparation_inside"):
                importer.prepare_path(source, "rights", source_kind=SourceKind.AI_VIDEO)
            with pytest.raises(MediaImportError, match="observation_inside"):
                item.verify()
            with pytest.raises(MediaImportError, match="cleanup_inside"):
                item.close()
        with pytest.raises(MediaImportError, match="consumed"):
            with db.transaction(immediate=True):
                item._retain({})  # Unverified preparation cannot be committed.


def test_retention_requires_transaction(media):
    with prepared(media) as item:
        item.verify()
        with pytest.raises(MediaImportError, match="transaction_required"):
            item._retain({})


def test_failed_output_can_be_quarantined_without_public_asset(media):
    db, importer, source = media
    with prepared(media) as item:
        item.verify()
        with pytest.raises(ValueError):
            with db.transaction(immediate=True):
                item._retain({})
                raise ValueError("late authority loss")
        quarantine = item.quarantine(importer.data_root / "execution-quarantine")
        assert not Path(item.asset.source_file).exists()
    assert quarantine.read_bytes() == source.read_bytes()
    assert AssetRepository(db).get(item.asset.id) is None
