"""Exact-source Talking reference review claims, not an automatic detector."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.db import AssetRepository, ClipRepository, Database
from app.domain.models import SourceKind


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
            or asset.source_kind not in {SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET}
            or not asset.authorization_reference.strip()):
            raise ValueError("Talking suitability source rights or Asset/Clip are unavailable")
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
