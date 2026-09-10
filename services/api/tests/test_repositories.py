from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest

from app.db import AssetRepository, ClipRepository, Database, IPProfileRepository, JobRepository, ProjectRepository, apply_migrations
from app.domain.models import Asset, Clip, IPProfile, Job, JobType, Project, RationalFps


def make_models():
    now = datetime.now(timezone.utc)
    profile = IPProfile(creator_name="Creator", domains=["design"])
    project = Project(ip_profile_id=profile.id, title="A project", topic="A topic", fps=RationalFps(numerator=30, denominator=1), created_at=now)
    asset = Asset(source_file="C:/素材 folder/take.mp4", content_hash="a" * 16, duration_ms=1_000, width=1920, height=1080, fps=RationalFps(numerator=30, denominator=1), authorization_reference="rights-1", imported_at=now)
    clip = Clip(asset_id=asset.id, start_ms=100, end_ms=500, asset_duration_ms=1_000)
    job = Job(project_id=project.id, type=JobType.IMPORT_ASSET, idempotency_key="import-1", created_at=now, updated_at=now)
    return profile, project, asset, clip, job


def test_crud_round_trip_and_reopen(tmp_path: Path):
    path = tmp_path / "中文 folder" / "content.sqlite"
    db = Database(path)
    profile, project, asset, clip, job = make_models()
    profiles, projects = IPProfileRepository(db), ProjectRepository(db)
    assets, clips, jobs = AssetRepository(db), ClipRepository(db), JobRepository(db)
    with db.transaction():
        profiles.create(profile)
        projects.create(project)
        assets.create(asset)
        clips.create(clip)
        jobs.create(job)
    assert clips.get(clip.id) == clip
    assert jobs.create(job) == job
    # Exercise update and delete for every repository.
    updated_profile = profile.model_copy(update={"audience": "builders"})
    updated_project = project.model_copy(update={"title": "Updated project"})
    updated_asset = asset.model_copy(update={"has_audio": True})
    updated_clip = clip.model_copy(update={"visual_description": "desk"})
    updated_job = job.model_copy(update={"attempt": 1})
    assert profiles.update(updated_profile) == updated_profile
    assert projects.update(updated_project) == updated_project
    assert assets.update(updated_asset) == updated_asset
    assert clips.update(updated_clip) == updated_clip
    assert jobs.update(updated_job) == updated_job
    assert profiles.get(profile.id) == updated_profile
    assert projects.get(project.id) == updated_project
    assert assets.get(asset.id) == updated_asset
    assert clips.get(clip.id) == updated_clip
    assert jobs.get(job.id) == updated_job
    assert profiles.list() == [updated_profile]
    assert projects.list() == [updated_project]
    assert assets.list() == [updated_asset]
    assert clips.list() == [updated_clip]
    assert jobs.list() == [updated_job]
    db.close()
    reopened = Database(path)
    assert reopened.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert [row[0] for row in reopened.connection.execute("SELECT version FROM schema_migrations ORDER BY version")] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18]
    assert IPProfileRepository(reopened).get(profile.id) == updated_profile
    assert JobRepository(reopened).list() == [updated_job]
    apply_migrations(reopened.connection)
    assert [row[0] for row in reopened.connection.execute("SELECT version FROM schema_migrations ORDER BY version")] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18]
    assert ClipRepository(reopened).delete(clip.id)
    assert JobRepository(reopened).delete(job.id)
    assert ProjectRepository(reopened).delete(project.id)
    assert IPProfileRepository(reopened).delete(profile.id)
    assert AssetRepository(reopened).delete(asset.id)
    reopened.close()


def test_relationship_and_clip_duration_guards():
    db = Database()
    profile, project, asset, clip, _ = make_models()
    IPProfileRepository(db).create(profile)
    ProjectRepository(db).create(project)
    AssetRepository(db).create(asset)
    with pytest.raises(ValueError):
        ClipRepository(db).create(clip.model_copy(update={"asset_duration_ms": 999}))
    with pytest.raises(sqlite3.IntegrityError):
        ProjectRepository(db).create(project.model_copy(update={"id": uuid4(), "ip_profile_id": uuid4()}))
    db.close()


def test_asset_update_cannot_invalidate_existing_clips():
    db = Database()
    _, _, asset, clip, _ = make_models()
    assets, clips = AssetRepository(db), ClipRepository(db)
    assets.create(asset)
    clips.create(clip)
    with pytest.raises(ValueError):
        assets.update(asset.model_copy(update={"duration_ms": 400}))
    assert assets.get(asset.id) == asset
    db.close()


def test_asset_content_hash_is_immutable_after_create():
    db = Database()
    _, _, asset, _, _ = make_models()
    assets = AssetRepository(db)
    assets.create(asset)
    with pytest.raises(ValueError, match="content_hash is immutable"):
        assets.update(asset.model_copy(update={"content_hash": "different-content-hash"}))
    assert assets.get(asset.id) == asset
    db.close()


def test_asset_update_missing_row_raises_key_error():
    db = Database()
    _, _, asset, _, _ = make_models()
    with pytest.raises(KeyError):
        AssetRepository(db).update(asset)
    db.close()


def test_transaction_rolls_back_all_writes():
    db = Database()
    profile, project, *_ = make_models()
    profiles, projects = IPProfileRepository(db), ProjectRepository(db)
    with pytest.raises(RuntimeError):
        with db.transaction():
            profiles.create(profile)
            projects.create(project)
            raise RuntimeError("injected")
    assert profiles.get(profile.id) is None
    assert projects.get(project.id) is None
    db.close()
