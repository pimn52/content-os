from datetime import datetime, timezone
import sqlite3
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient
import pytest

from app.asset_management import (
    asset_is_active,
    asset_governance_history,
    asset_references,
    asset_scope_allows_project,
    compute_asset_fingerprint,
    near_duplicate_assets,
    record_asset_governance_event,
    update_asset_lifecycle,
)
from app.db import AssetGovernanceEventRepository, AssetRepository, AssetUsageRepository, ClipRepository, Database, IPProfileRepository, ProjectRepository
from app.domain.models import Asset, AssetUsageEvent, Clip, IPProfile, Project, RationalFps, SourceKind
from app.main import create_app


NOW = datetime.now(timezone.utc)


def _asset(name: str, *, owner: str, hashes: list[str], state: str = "active") -> Asset:
    return Asset(
        source_file=f"/media/{name}.mp4",
        content_hash=(name.encode().hex() + "0" * 64)[:64],
        duration_ms=3_000,
        width=1080,
        height=1920,
        fps=RationalFps(numerator=30, denominator=1),
        authorization_reference="creator-intake",
        imported_at=NOW,
        metadata={
            "ip_profile_id": owner,
            "reuse_role": "ordinary",
            "lifecycle_state": state,
            "perceptual_fingerprint": {
                "schema_version": "1.0",
                "algorithm": "dhash64-sampled-video-keyframes-v1",
                "sample_count": len(hashes),
                "source_keyframe_count": len(hashes),
                "samples": [
                    {"timestamp_ms": index * 1_000, "hash": value}
                    for index, value in enumerate(hashes)
                ],
            },
        },
    )


def _seed(path: Path):
    db = Database(path)
    profile = IPProfile(creator_name="Creator")
    IPProfileRepository(db).create(profile)
    owner = str(profile.id)
    project = Project(
        ip_profile_id=profile.id,
        title="Material governance",
        topic="asset reuse",
        fps=RationalFps(numerator=30, denominator=1),
        created_at=NOW,
    )
    ProjectRepository(db).create(project)
    source = _asset("source", owner=owner, hashes=["ffffffffffffffff", "fffffffffffffffe"])
    near = _asset("near", owner=owner, hashes=["ffffffffffffffff", "fffffffffffffffc"])
    archived = _asset("archived", owner=owner, hashes=["ffffffffffffffff"], state="archived")
    other_owner = _asset("other-owner", owner="00000000-0000-0000-0000-000000000001", hashes=["ffffffffffffffff"])
    for asset in (source, near, archived, other_owner):
        AssetRepository(db).create(asset)
    clip = Clip(asset_id=source.id, start_ms=0, end_ms=3_000, asset_duration_ms=3_000)
    ClipRepository(db).create(clip)
    AssetUsageRepository(db).create(AssetUsageEvent(
        project_id=project.id,
        output_version="render:fixture",
        event_key="fixture-usage-event",
        media_id=source.id,
        media_kind="video",
        clip_id=clip.id,
        used_at=NOW,
    ))
    return db, source, near, archived, other_owner


def test_near_duplicates_stay_within_owner_scope_and_skip_archived(tmp_path: Path) -> None:
    db, source, near, archived, other_owner = _seed(tmp_path / "management.sqlite")
    try:
        result = near_duplicate_assets(db, source.id, max_distance=4)
        assert [item["asset_id"] for item in result["matches"]] == [near.id]
        assert archived.id not in {item["asset_id"] for item in result["matches"]}
        assert other_owner.id not in {item["asset_id"] for item in result["matches"]}
        inclusive = near_duplicate_assets(db, source.id, max_distance=4, include_archived=True)
        assert archived.id in {item["asset_id"] for item in inclusive["matches"]}
    finally:
        db.close()


def test_creator_media_requires_explicit_ip_scope_while_stock_can_be_shared(tmp_path: Path) -> None:
    db, source, _, _, other_owner = _seed(tmp_path / "scope.sqlite")
    try:
        profile_id = UUID(source.metadata["ip_profile_id"])
        unowned = _asset("unowned", owner="", hashes=["0000000000000000"])
        unowned = unowned.model_copy(update={"metadata": {
            key: value for key, value in unowned.metadata.items() if key != "ip_profile_id"
        }})
        stock = unowned.model_copy(update={"source_kind": SourceKind.STOCK})

        assert asset_scope_allows_project(source, profile_id)
        assert not asset_scope_allows_project(unowned, profile_id)
        assert not asset_scope_allows_project(other_owner, profile_id)
        assert asset_scope_allows_project(stock, profile_id)
        assert asset_scope_allows_project(unowned, None)
        assert not asset_scope_allows_project(source, None)
        assert asset_scope_allows_project(stock, None)
    finally:
        db.close()


def test_archive_preserves_references_and_removes_asset_from_new_use(tmp_path: Path) -> None:
    db, source, _, _, _ = _seed(tmp_path / "lifecycle.sqlite")
    try:
        references = asset_references(db, source.id)
        assert references["delete_safe"] is False
        assert {item["kind"] for item in references["references"]} >= {"clip", "asset_usage"}

        archived, archived_references = update_asset_lifecycle(
            db, source.id, state="archived", reason="superseded",
        )
        history = asset_governance_history(db, source.id)
        assert not asset_is_active(archived)
        assert archived.metadata["lifecycle_reason"] == "superseded"
        assert archived_references == references
        assert any(event.event_type == "lifecycle_changed" for event in history["events"])
        assert AssetRepository(db).get(source.id).content_hash == source.content_hash

        restored, _ = update_asset_lifecycle(db, source.id, state="active")
        assert asset_is_active(restored)
        assert "lifecycle_reason" not in restored.metadata
    finally:
        db.close()


def test_governance_events_are_product_owned_and_ordered(tmp_path: Path) -> None:
    db, source, _, _, _ = _seed(tmp_path / "governance-events.sqlite")
    try:
        with db.transaction():
            first_event = record_asset_governance_event(
                db, source, "classification_changed", "normal_ui",
                details={"before": {"reuse_role": "ordinary"}, "after": {"reuse_role": "signature"}},
            )
            record_asset_governance_event(
                db, source, "fingerprint_failed", "worker_analysis",
                details={"error_code": "fixture"},
            )
        history = asset_governance_history(db, source.id)
        assert [item.event_type for item in history["events"]] == [
            "classification_changed", "fingerprint_failed",
        ]
        assert history["events"][0].actor == "normal_ui"
        assert history["ownership"]["reuse_role"] == "ordinary"
        with pytest.raises(sqlite3.IntegrityError):
            db.connection.execute("UPDATE asset_governance_events SET event_type='imported' WHERE id=?", (str(first_event.id),))
        with pytest.raises(sqlite3.IntegrityError):
            db.connection.execute("DELETE FROM asset_governance_events WHERE id=?", (str(first_event.id),))
        with pytest.raises(ValueError, match="append-only"):
            AssetGovernanceEventRepository(db).delete(first_event.id)
    finally:
        db.close()


def test_existing_keyframes_can_backfill_a_perceptual_fingerprint(tmp_path: Path, monkeypatch) -> None:
    db = Database(tmp_path / "fingerprint-backfill.sqlite")
    try:
        asset = _asset("backfill", owner="00000000-0000-0000-0000-000000000001", hashes=[])
        asset = asset.model_copy(update={"metadata": {
            key: value for key, value in asset.metadata.items() if key != "perceptual_fingerprint"
        }})
        AssetRepository(db).create(asset)
        clip = Clip(asset_id=asset.id, start_ms=0, end_ms=3_000, asset_duration_ms=3_000)
        ClipRepository(db).create(clip)
        frame = tmp_path / "assets" / "keyframes" / str(clip.id) / "frame.jpg"
        frame.parent.mkdir(parents=True)
        frame.write_bytes(b"jpeg")

        class Fingerprinter:
            def __init__(self, command):
                self.command = command

            def fingerprint(self, frames):
                assert frames == [(frame, 1500.0)]
                return {
                    "schema_version": "1.0",
                    "algorithm": "dhash64-sampled-video-keyframes-v1",
                    "sample_count": 1,
                    "source_keyframe_count": 1,
                    "samples": [{"timestamp_ms": 1500, "hash": "ffffffffffffffff"}],
                }

        import app.media.fingerprint as fingerprint_module
        monkeypatch.setattr(fingerprint_module, "FFmpegFrameFingerprinter", Fingerprinter)
        updated = compute_asset_fingerprint(db, tmp_path, asset.id, ffmpeg="ffmpeg")

        assert updated.metadata["perceptual_fingerprint"]["sample_count"] == 1
    finally:
        db.close()


def test_lifecycle_api_exposes_reference_impact(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle-api.sqlite"
    db, source, _, _, _ = _seed(path)
    db.close()
    with TestClient(create_app(data_path=path)) as client:
        response = client.patch(
            f"/assets/{source.id}/lifecycle",
            json={"state": "archived", "reason": "creator archived"},
        )
        governance = client.get(f"/assets/{source.id}/governance")

    assert response.status_code == 200
    body = response.json()
    assert body["asset"]["metadata"]["lifecycle_state"] == "archived"
    assert body["references"]["delete_safe"] is False
    assert governance.status_code == 200
    assert any(event["event_type"] == "lifecycle_changed" for event in governance.json()["events"])
