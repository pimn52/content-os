"""Asset ownership scope, lifecycle, reference checks, and near-duplicate lookup."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from app.db import (
    AssetGovernanceEventRepository,
    AssetRepository,
    AssetUsageRepository,
    ClipRepository,
    Database,
    ProjectDraftRepository,
    TalkingProfileRepository,
    VoiceProfileRepository,
)
from app.domain.models import AssetGovernanceEvent


class AssetManagementError(ValueError):
    pass


def asset_is_active(asset) -> bool:
    return asset.metadata.get("lifecycle_state") != "archived"


def _ownership_scope(asset) -> str | None:
    value = asset.metadata.get("ip_profile_id")
    return value if isinstance(value, str) and value else None


def record_asset_governance_event(
    db: Database,
    asset,
    event_type: str,
    actor: str,
    *,
    details: dict[str, object] | None = None,
    evidence_refs: list[str] | None = None,
) -> AssetGovernanceEvent:
    event = AssetGovernanceEvent(
        asset_id=asset.id,
        asset_content_hash=asset.content_hash,
        event_type=event_type,
        actor=actor,
        occurred_at=datetime.now(timezone.utc),
        details=details or {},
        evidence_refs=evidence_refs or [f"asset:{asset.id}"],
    )
    return AssetGovernanceEventRepository(db).create(event)


def asset_scope_allows_project(asset, ip_profile_id: UUID | None) -> bool:
    """Creator/historical originals require explicit current-IP ownership."""

    scope = _ownership_scope(asset)
    if asset.source_kind.value in {"user_asset", "historical_asset", "ai_broll"}:
        if ip_profile_id is None:
            # Generic projects predating IP profiles may use only unowned
            # material. An owned asset still needs an explicit owner match.
            return scope is None
        return scope == str(ip_profile_id)
    return scope in (None, str(ip_profile_id))


def visual_evidence_coverage(asset, clips) -> dict[str, object]:
    """Expose searchable Clip description coverage without making it an admission gate."""
    ordered = sorted(clips, key=lambda clip: (clip.start_ms, str(clip.id)))
    missing = [
        clip.id for clip in ordered
        if not isinstance(clip.visual_description, str) or not clip.visual_description.strip()
    ]
    return {
        "total_clips": len(ordered),
        "described_clips": len(ordered) - len(missing),
        "missing_clip_ids": missing,
        "creator_owned": _ownership_scope(asset) is not None
        and asset.source_kind.value not in {"stock", "typography"},
    }


def compute_asset_fingerprint(
    db: Database,
    data_root: str | Path,
    asset_id: UUID,
    *,
    ffmpeg: str | list[str] | tuple[str, ...],
):
    from app.media.fingerprint import FFmpegFrameFingerprinter

    repository = AssetRepository(db)
    asset = repository.get(asset_id)
    if asset is None:
        raise KeyError(asset_id)
    existing = asset.metadata.get("perceptual_fingerprint")
    if isinstance(existing, dict):
        if not isinstance(asset.metadata.get("near_duplicate_scan"), dict):
            try:
                return refresh_near_duplicate_snapshot(
                    db, asset_id, actor="normal_ui",
                )
            except AssetManagementError:
                return AssetRepository(db).get(asset_id)
        return asset
    keyframes = []
    for clip in ClipRepository(db).list_by_asset(asset.id):
        directory = Path(data_root) / "assets" / "keyframes" / str(clip.id)
        candidates = sorted(path for path in directory.glob("*.jpg") if path.is_file())
        if candidates:
            keyframes.append((candidates[0], (clip.start_ms + clip.end_ms) / 2))
    if not keyframes:
        raise AssetManagementError("asset_keyframes_not_ready")
    fingerprint = FFmpegFrameFingerprinter(ffmpeg).fingerprint(keyframes)
    metadata = dict(asset.metadata)
    metadata["perceptual_fingerprint"] = fingerprint
    metadata.pop("perceptual_fingerprint_error", None)
    updated = asset.model_copy(update={"metadata": metadata})
    with db.transaction():
        repository.update(updated)
        record_asset_governance_event(
            db, updated, "fingerprint_created", "normal_ui",
            details={
                "algorithm": fingerprint["algorithm"],
                "sample_count": fingerprint["sample_count"],
                "source_keyframe_count": fingerprint["source_keyframe_count"],
            },
        )
    try:
        return refresh_near_duplicate_snapshot(db, asset_id, actor="normal_ui")
    except AssetManagementError:
        return AssetRepository(db).get(asset_id)


def near_duplicate_assets(
    db: Database,
    asset_id: UUID,
    *,
    max_distance: int = 12,
    limit: int = 10,
    include_archived: bool = False,
) -> dict[str, object]:
    from app.media.fingerprint import FingerprintError, fingerprint_distance

    if type(max_distance) is not int or not 0 <= max_distance <= 64:
        raise AssetManagementError("max_distance_must_be_0_to_64")
    if type(limit) is not int or not 1 <= limit <= 50:
        raise AssetManagementError("limit_must_be_1_to_50")
    repository = AssetRepository(db)
    target = repository.get(asset_id)
    if target is None:
        raise KeyError(asset_id)
    fingerprint = target.metadata.get("perceptual_fingerprint")
    if not isinstance(fingerprint, dict) or not isinstance(fingerprint.get("samples"), list):
        raise AssetManagementError("asset_fingerprint_not_ready")
    scope = _ownership_scope(target)
    matches = []
    for candidate in repository.list():
        if candidate.id == target.id or not include_archived and not asset_is_active(candidate):
            continue
        if _ownership_scope(candidate) != scope:
            continue
        other = candidate.metadata.get("perceptual_fingerprint")
        if (not isinstance(other, dict) or other.get("algorithm") != fingerprint.get("algorithm")
            or not isinstance(other.get("samples"), list)):
            continue
        try:
            distance = fingerprint_distance(fingerprint, other)
        except FingerprintError:
            continue
        if distance <= max_distance:
            matches.append({
                "asset_id": candidate.id,
                "distance": distance,
                "score": round(1 - distance / 64, 4),
                "source_kind": candidate.source_kind,
                "duration_ms": candidate.duration_ms,
                "width": candidate.width,
                "height": candidate.height,
                "lifecycle_state": candidate.metadata.get("lifecycle_state", "active"),
            })
    matches.sort(key=lambda item: (item["distance"], str(item["asset_id"])))
    return {
        "asset_id": target.id,
        "algorithm": fingerprint["algorithm"],
        "max_distance": max_distance,
        "matches": matches[:limit],
    }


def refresh_near_duplicate_snapshot(
    db: Database,
    asset_id: UUID,
    *,
    max_distance: int = 12,
    limit: int = 10,
    actor: str = "worker_analysis",
) -> object:
    repository = AssetRepository(db)
    try:
        result = near_duplicate_assets(
            db, asset_id, max_distance=max_distance, limit=limit,
        )
    except AssetManagementError as exc:
        asset = repository.get(asset_id)
        if asset is not None:
            metadata = dict(asset.metadata)
            metadata["near_duplicate_scan"] = {
                "state": "failed",
                "error_code": str(exc),
                "scanned_at": datetime.now(timezone.utc).isoformat(),
            }
            failed = asset.model_copy(update={"metadata": metadata})
            with db.transaction():
                repository.update(failed)
                record_asset_governance_event(
                    db, failed, "near_duplicate_scan_failed", actor,
                    details={"error_code": str(exc)},
                )
        raise
    matches = [{
        "asset_id": str(item["asset_id"]),
        "distance": item["distance"],
        "score": item["score"],
        "source_kind": getattr(item["source_kind"], "value", item["source_kind"]),
        "lifecycle_state": item["lifecycle_state"],
    } for item in result["matches"]]
    scanned_at = datetime.now(timezone.utc).isoformat()
    snapshot = {
        "state": "completed",
        "algorithm": result["algorithm"],
        "max_distance": max_distance,
        "scanned_at": scanned_at,
        "matches": matches,
    }
    asset = repository.get(asset_id)
    if asset is None:
        raise KeyError(asset_id)
    metadata = dict(asset.metadata)
    metadata["near_duplicate_scan"] = snapshot
    updated = asset.model_copy(update={"metadata": metadata})
    with db.transaction():
        repository.update(updated)
        record_asset_governance_event(
            db, updated, "near_duplicate_scan", actor,
            details={
                "algorithm": result["algorithm"],
                "max_distance": max_distance,
                "match_count": len(matches),
                "matches": matches,
            },
        )
    return updated


def asset_references(db: Database, asset_id: UUID) -> dict[str, object]:
    asset = AssetRepository(db).get(asset_id)
    if asset is None:
        raise KeyError(asset_id)
    clips = ClipRepository(db).list_by_asset(asset.id)
    clip_ids = {clip.id for clip in clips}
    references: list[dict[str, object]] = [
        {"kind": "clip", "id": clip.id, "project_id": None} for clip in clips
    ]
    for draft in ProjectDraftRepository(db).list():
        if any(scene.preferred_clip_id in clip_ids for scene in draft.scenes):
            references.append({"kind": "draft_scene", "id": draft.project_id, "project_id": draft.project_id})
        if any(candidate.asset_id == asset.id for candidate in draft.confirmed):
            references.append({"kind": "draft_confirmed_asset", "id": draft.project_id, "project_id": draft.project_id})
        if draft.video_spec is not None and any(
            scene.visual.asset_id == asset.id for scene in draft.video_spec.scenes
        ):
            references.append({"kind": "video_spec", "id": draft.project_id, "project_id": draft.project_id})
    for profile in VoiceProfileRepository(db).list():
        if clip_ids.intersection(profile.reference_clip_ids):
            references.append({"kind": "voice_profile", "id": profile.id, "project_id": None})
    for profile in TalkingProfileRepository(db).list():
        if clip_ids.intersection(profile.reference_clip_ids):
            references.append({"kind": "talking_profile", "id": profile.id, "project_id": None})
    for event in AssetUsageRepository(db).list():
        if event.media_id == asset.id:
            references.append({"kind": "asset_usage", "id": event.id, "project_id": event.project_id})
    for row in db.connection.execute("SELECT id, project_id, visual_source_uses FROM production_runs"):
        try:
            uses = json.loads(row["visual_source_uses"])
        except (TypeError, ValueError):
            continue
        if any(isinstance(use, dict) and use.get("source_hash") == asset.content_hash for use in uses):
            references.append({"kind": "production_run", "id": UUID(row["id"]), "project_id": UUID(row["project_id"])})
    unique = {(item["kind"], str(item["id"]), None if item["project_id"] is None else str(item["project_id"])): item
              for item in references}
    ordered = sorted(unique.values(), key=lambda item: (item["kind"], str(item["id"])))
    return {
        "asset_id": asset.id,
        "reference_count": len(ordered),
        "references": ordered,
        "delete_safe": len(ordered) == 0,
    }


def asset_governance_history(db: Database, asset_id: UUID) -> dict[str, object]:
    asset = AssetRepository(db).get(asset_id)
    if asset is None:
        raise KeyError(asset_id)
    events = AssetGovernanceEventRepository(db).list_for_asset(asset_id)
    return {
        "asset": asset,
        "ownership": {
            "ip_profile_id": asset.metadata.get("ip_profile_id"),
            "reuse_role": asset.metadata.get("reuse_role"),
        },
        "lifecycle": {
            "state": asset.metadata.get("lifecycle_state", "active"),
            "updated_at": asset.metadata.get("lifecycle_updated_at"),
            "reason": asset.metadata.get("lifecycle_reason"),
        },
        "perceptual_fingerprint": asset.metadata.get("perceptual_fingerprint"),
        "near_duplicate_scan": asset.metadata.get("near_duplicate_scan"),
        "events": events,
        "references": asset_references(db, asset_id),
    }


def update_asset_lifecycle(
    db: Database,
    asset_id: UUID,
    *,
    state: str,
    reason: str | None = None,
) -> tuple[object, dict[str, object]]:
    if state not in {"active", "archived"}:
        raise AssetManagementError("asset_lifecycle_state_invalid")
    repository = AssetRepository(db)
    current = repository.get(asset_id)
    if current is None:
        raise KeyError(asset_id)
    references = asset_references(db, asset_id)
    metadata = dict(current.metadata)
    metadata["lifecycle_state"] = state
    metadata["lifecycle_updated_at"] = datetime.now(timezone.utc).isoformat()
    if state == "archived":
        metadata["lifecycle_reason"] = (reason or "creator_archived").strip()[:500]
    else:
        metadata.pop("lifecycle_reason", None)
    updated = current.model_copy(update={"metadata": metadata})
    with db.transaction():
        repository.update(updated)
        record_asset_governance_event(
            db, updated, "lifecycle_changed", "normal_ui",
            details={
                "before": current.metadata.get("lifecycle_state", "active"),
                "after": state,
                "reason": metadata.get("lifecycle_reason"),
                "reference_count": references["reference_count"],
            },
        )
    return updated, references
