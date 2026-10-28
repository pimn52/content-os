"""本人动作来源登记、确切视频区间事实入池与历史人工复核记录。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.db import AssetRepository, ClipRepository, Database, IPProfileRepository, TalkingProfileRepository
from app.domain.models import Clip, GazeDirection, SourceKind, TalkingReferenceAssessment


_MOTION_CLIPS = UUID("1a7e8a90-8fe1-4d3b-892c-78dfe66958c8")


def motion_source_binding(db, asset, *, project=None):
    """身份来自创作者登记；视觉模型只判断区间条件，不认人。"""
    value = asset.metadata.get("talking_motion_source")
    if (asset.source_kind not in {SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET, SourceKind.STOCK}
        or asset.metadata.get("r1_identity") != "creator" or not isinstance(value, dict)
        or value.get("purpose") != "lip_sync_motion" or value.get("source_content_hash") != asset.content_hash):
        return None
    try:
        ip_id = UUID(asset.metadata["ip_profile_id"])
        profile = TalkingProfileRepository(db).get(UUID(value["talking_profile_id"]))
    except (KeyError, ValueError, TypeError):
        return None
    if (profile is None or not profile.consent.confirmed or profile.ip_profile_id != ip_id
        or project is not None and project.ip_profile_id != ip_id):
        return None
    return profile


def _remove_machine_references(db, asset, *, analysed_clip_id=None):
    """只停用本入口自动加入的引用，保留旧Clip与核验证据。"""
    removed = set()
    records = asset.metadata.get("talking_motion_analysis", {})
    if not isinstance(records, dict):
        return removed
    for identity, record in records.items():
        if analysed_clip_id is not None and identity != str(analysed_clip_id):
            continue
        if isinstance(record, dict):
            for value in record.get("reference_clip_ids", []):
                try:
                    removed.add(UUID(value))
                except (ValueError, TypeError):
                    continue
    clips = ClipRepository(db)
    for identity in removed:
        clip = clips.get(identity)
        if clip is not None and clip.asset_id == asset.id:
            clips.update(clip.model_copy(update={"talking_candidate": False}))
    profiles = TalkingProfileRepository(db)
    for profile in profiles.list():
        if removed.intersection(profile.reference_clip_ids):
            profiles.update(profile.model_copy(update={"reference_clip_ids": [
                identity for identity in profile.reference_clip_ids if identity not in removed]}))
    return removed


def register_motion_source(db, asset, *, identity, ip_profile_id=None, talking_profile_id=None,
        motion_reference=None, reuse_role=None):
    """普通素材用途登记；入库授权和Profile同意沿用，不新加许可门槛。须由调用方提交事务。"""
    metadata = dict(asset.metadata)
    metadata["r1_identity"] = identity
    if reuse_role is not None:
        metadata["reuse_role"] = reuse_role
    if ip_profile_id is not None:
        if IPProfileRepository(db).get(ip_profile_id) is None:
            raise ValueError("motion_source_ip_unavailable")
        metadata["ip_profile_id"] = str(ip_profile_id)
    if motion_reference is True:
        if identity != "creator" or talking_profile_id is None or ip_profile_id is None:
            raise ValueError("motion_source_creator_and_profile_required")
        if asset.source_kind not in {SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET, SourceKind.STOCK}:
            raise ValueError("motion_source_must_be_original_video")
        profiles = TalkingProfileRepository(db)
        profile = profiles.get(talking_profile_id)
        if profile is None or not profile.consent.confirmed:
            raise ValueError("motion_source_profile_consent_missing")
        if profile.ip_profile_id not in (None, ip_profile_id):
            raise ValueError("motion_source_profile_ip_mismatch")
        if profile.ip_profile_id is None:
            profiles.update(profile.model_copy(update={"ip_profile_id": ip_profile_id}))
        new_binding = {"purpose": "lip_sync_motion", "talking_profile_id": str(profile.id),
            "source_content_hash": asset.content_hash}
        if metadata.get("talking_motion_source") != new_binding:
            _remove_machine_references(db, asset)
        metadata["talking_motion_source"] = new_binding
    elif motion_reference is False or identity != "creator":
        _remove_machine_references(db, asset)
        metadata.pop("talking_motion_source", None)
    elif metadata.get("talking_motion_source") and metadata.get("ip_profile_id") != asset.metadata.get("ip_profile_id"):
        _remove_machine_references(db, asset)
        metadata.pop("talking_motion_source", None)
    return AssetRepository(db).update(asset.model_copy(update={"metadata": metadata}))


def admit_observed_motion_intervals(db, asset, analysed_clip, intervals, *, evidence_reference, video_sha256,
        analysis_call_id=None, analysis_batch_key=None):
    """把整段核验的合格连续子区间入池，不外推到相邻Clip或母片。须在既有分析事务中调用。"""
    profile = motion_source_binding(db, asset)
    if profile is None:
        return asset
    state = "observed"
    if intervals is None:
        state = "motion_observation_unavailable"
    elif any(row.end_offset_ms > analysed_clip.end_ms-analysed_clip.start_ms for row in intervals):
        state = "invalid_interval_scope"
    elif asset.height <= asset.width:
        state = "portrait_source_required"
    eligible = []
    if state == "observed":
        eligible = [row for row in intervals if row.single_person_continuous and row.motion_continuous
            and row.head_clearance and row.face_visibility >= .6 and row.mouth_visibility >= .6
            and row.quality_score >= .6 and row.burned_in_subtitles is False]
    # 重叠观察保留最长一条，不把相邻独立观察拼成更长合格区间。
    selected = []
    for row in sorted(eligible, key=lambda row: (-(row.end_offset_ms-row.start_offset_ms), row.start_offset_ms)):
        if not any(row.start_offset_ms < prior.end_offset_ms and prior.start_offset_ms < row.end_offset_ms for prior in selected):
            selected.append(row)
    clips = ClipRepository(db)
    _remove_machine_references(db, asset, analysed_clip_id=analysed_clip.id)
    profile = TalkingProfileRepository(db).get(profile.id)
    references = list(profile.reference_clip_ids)
    minted = []
    for row in sorted(selected, key=lambda value: value.start_offset_ms):
        start, end = analysed_clip.start_ms+row.start_offset_ms, analysed_clip.start_ms+row.end_offset_ms
        identity = uuid5(_MOTION_CLIPS, f"{asset.id}:{asset.content_hash}:{start}:{end}")
        if identity not in references and len(references) >= 100:
            state = "profile_reference_limit"
            break
        clip = clips.get(identity) or Clip(id=identity, asset_id=asset.id, start_ms=start, end_ms=end,
            asset_duration_ms=asset.duration_ms)
        if (clip.asset_id, clip.start_ms, clip.end_ms) != (asset.id, start, end):
            raise ValueError("motion_source_clip_identity_changed")
        clips.upsert(clip.model_copy(update={"talking_candidate": True,
            "face_visibility": row.face_visibility, "mouth_visibility": row.mouth_visibility,
            "quality_score": row.quality_score,
            "talking_reference_assessment": TalkingReferenceAssessment(start_gaze=GazeDirection(row.start_gaze),
                end_gaze=GazeDirection(row.end_gaze), expression_labels=row.expression_labels,
                burned_in_subtitles=row.burned_in_subtitles, evidence_reference=evidence_reference)}))
        minted.append(str(identity))
        if identity not in references:
            references.append(identity)
    TalkingProfileRepository(db).update(profile.model_copy(update={"reference_clip_ids": references}))
    metadata = dict(asset.metadata)
    records = dict(metadata.get("talking_motion_analysis", {}))
    records[str(analysed_clip.id)] = {"state": state, "source_content_hash": asset.content_hash,
        "analysed_interval": {"start_ms": analysed_clip.start_ms, "end_ms": analysed_clip.end_ms},
        "coverage": "machine_video_interval", "method": "provider_video_interval_analysis",
        "video_sha256": video_sha256, "evidence_reference": evidence_reference,
        "analysis_call_id": str(analysis_call_id) if analysis_call_id is not None else None,
        "analysis_batch_key": analysis_batch_key,
        "observations": None if intervals is None else [row.model_dump(mode="json") for row in intervals],
        "reference_clip_ids": minted}
    metadata["talking_motion_analysis"] = records
    return AssetRepository(db).update(asset.model_copy(update={"metadata": metadata}))


def restore_observed_motion_references(db, asset):
    """用途重新启用时复用同源整段事实；单帧、旧衍生认可与缺失分析Clip不参与。"""
    from app.providers.vision import TalkingMotionInterval
    profile = motion_source_binding(db, asset)
    if profile is None:
        return asset
    records = asset.metadata.get("talking_motion_analysis", {})
    if not isinstance(records, dict):
        return asset
    for identity, record in records.items():
        if not isinstance(record, dict) or record.get("state") not in {"observed", "profile_reference_limit"}:
            continue
        try:
            clip = ClipRepository(db).get(UUID(identity))
            facts = asset.metadata.get("visual_analysis", {}).get(identity, {})
            if (clip is None or clip.asset_id != asset.id
                or record.get("source_content_hash") != asset.content_hash
                or record.get("analysed_interval") != {"start_ms":clip.start_ms,"end_ms":clip.end_ms}
                or facts.get("source_content_hash") != asset.content_hash
                or facts.get("coverage") != "video_interval_analysis" or facts.get("analysis_valid") is not True
                or facts.get("analysis_video_sha256") != record.get("video_sha256")
                or facts.get("evidence_reference") != record.get("evidence_reference")):
                continue
            values = [TalkingMotionInterval.model_validate(row) for row in record["observations"]]
            minted = [ClipRepository(db).get(UUID(value)) for value in record["reference_clip_ids"]]
        except (ValueError, KeyError, TypeError):
            continue
        if (not minted and record.get("state") == "observed"
            or minted and all(reference is not None and reference.talking_candidate
                and reference.id in profile.reference_clip_ids for reference in minted)):
            continue
        asset = admit_observed_motion_intervals(db, asset, clip, values,
            evidence_reference=record["evidence_reference"], video_sha256=record["video_sha256"],
            analysis_call_id=record.get("analysis_call_id"), analysis_batch_key=record.get("analysis_batch_key"))
        profile = TalkingProfileRepository(db).get(profile.id)
    return asset


class TalkingSourceSuitabilityAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID = Field(default_factory=uuid4)
    idempotency_key: str = Field(min_length=1, max_length=500)
    source_asset_id: UUID
    source_clip_id: UUID
    source_content_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    presentation: Literal["source_native_portrait", "unspecified"]
    evidence_class: Literal["fixture", "assisted_test"]
    method: Literal["human_full_interval_review"]
    method_version: str = Field(min_length=1, max_length=100)
    coverage: Literal["full_interval_continuous", "partial"]
    face_head_clearance: Literal["pass", "fail", "unknown"]
    motion_continuity: Literal["pass", "fail", "unknown"]
    subtitle_clearance: Literal["pass", "fail", "unknown"]
    effective_quality: Literal["pass", "fail", "unknown"]
    reviewer_reference: str = Field(min_length=1, max_length=500)
    evidence_reference: str = Field(min_length=1, max_length=2_000)

    @model_validator(mode="after")
    def positive_interval(self) -> "TalkingSourceSuitabilityAssessment":
        if self.end_ms <= self.start_ms:
            raise ValueError("Talking suitability interval must be positive")
        return self

    @property
    def decision(self) -> Literal["review_claimed_suitable", "unusable", "unknown"]:
        checks = (self.face_head_clearance, self.motion_continuity,
                  self.subtitle_clearance, self.effective_quality)
        if self.evidence_class == "fixture":
            return "unknown"
        if "fail" in checks:
            return "unusable"
        if (self.coverage != "full_interval_continuous"
            or self.presentation != "source_native_portrait" or "unknown" in checks):
            return "unknown"
        return "review_claimed_suitable"


class TalkingSourceSuitabilityRepository:
    def __init__(self, db: Database, data_root: str | Path):
        self.db = db
        self.data_root = Path(data_root).resolve()

    def get(self, assessment_id: UUID) -> TalkingSourceSuitabilityAssessment | None:
        row = self.db.connection.execute(
            "SELECT payload FROM talking_source_suitability WHERE id = ?", (str(assessment_id),),
        ).fetchone()
        return None if row is None else TalkingSourceSuitabilityAssessment.model_validate(json.loads(row["payload"]))

    def get_by_idempotency_key(self, key: str) -> TalkingSourceSuitabilityAssessment | None:
        row = self.db.connection.execute(
            "SELECT payload FROM talking_source_suitability WHERE idempotency_key = ?", (key,),
        ).fetchone()
        return None if row is None else TalkingSourceSuitabilityAssessment.model_validate_json(row["payload"])

    def list_rows_for_clip(self, clip_id: UUID) -> list[tuple[str, str]]:
        rows = self.db.connection.execute(
            "SELECT id, payload FROM talking_source_suitability WHERE source_clip_id = ? ORDER BY id",
            (str(clip_id),),
        ).fetchall()
        return [(row["id"], row["payload"]) for row in rows]

    def create(self, value: TalkingSourceSuitabilityAssessment) -> TalkingSourceSuitabilityAssessment:
        self.require_current_source(value)
        existing = self.db.connection.execute(
            "SELECT payload FROM talking_source_suitability WHERE idempotency_key = ?", (value.idempotency_key,),
        ).fetchone()
        if existing is not None:
            prior = TalkingSourceSuitabilityAssessment.model_validate(json.loads(existing["payload"]))
            if prior.model_dump(exclude={"id"}) != value.model_dump(exclude={"id"}):
                raise ValueError("Talking suitability idempotency key belongs to different evidence")
            return prior
        if self.get(value.id) is not None:
            raise ValueError("Talking suitability ID belongs to different evidence")
        self.db.connection.execute(
            """INSERT INTO talking_source_suitability(id, idempotency_key, source_asset_id, source_clip_id, payload)
               VALUES (?, ?, ?, ?, ?)""",
            (str(value.id), value.idempotency_key, str(value.source_asset_id), str(value.source_clip_id),
             value.model_dump_json()),
        )
        return value

    def require_current_source(self, value: TalkingSourceSuitabilityAssessment) -> None:
        asset = AssetRepository(self.db).get(value.source_asset_id)
        clip = ClipRepository(self.db).get(value.source_clip_id)
        if (asset is None or clip is None or clip.asset_id != asset.id
            or asset.source_kind not in {SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET}):
            raise ValueError("Talking suitability Asset/Clip is unavailable")
        if (value.source_content_hash != asset.content_hash or value.start_ms != clip.start_ms
            or value.end_ms != clip.end_ms):
            raise ValueError("Talking suitability source identity or exact Clip interval changed")
        if value.presentation == "source_native_portrait" and asset.height <= asset.width:
            raise ValueError("source-native portrait claim requires a portrait source")
        source = self._resolve(asset.source_file)
        if not source.is_file():
            raise ValueError("Talking suitability source file is unavailable")
        with source.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != value.source_content_hash:
            raise ValueError("Talking suitability source bytes changed")

    def _resolve(self, stored: str) -> Path:
        value = Path(stored)
        if value.is_absolute():
            return value.resolve()  # Legacy imported absolute paths.
        parts = value.parts
        source = (self.data_root.parent / value if parts and parts[0].casefold() == self.data_root.name.casefold()
                  else self.data_root / value).resolve()
        if not source.is_relative_to(self.data_root):
            raise ValueError("Talking suitability source path escapes data root")
        return source
