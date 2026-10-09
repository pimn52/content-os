"""Persisted negative-only evidence for one proposed source interval and crop.

An observed face-box crossing can disqualify that exact proposal. No amount of
non-crossing evidence from this seam can certify a crop as usable.
"""
from __future__ import annotations

import json
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.db import AssetRepository, ClipRepository, Database
from app.domain.models import SourceCropWindow


class CropProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene_plan_id: UUID | None = None
    source_asset_id: UUID
    source_clip_id: UUID
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    crop_window: SourceCropWindow

    @model_validator(mode="after")
    def valid_interval(self) -> "CropProposal":
        if self.end_ms <= self.start_ms:
            raise ValueError("crop proposal interval must be positive")
        return self


class FaceBoxObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    time_ms: int = Field(ge=0)
    face_box: SourceCropWindow
    confidence: float = Field(gt=0, le=1)


class CropAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID = Field(default_factory=uuid4)
    proposal: CropProposal
    source_content_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    evidence_class: Literal["fixture", "assisted_test", "runtime"]
    method: str = Field(min_length=1, max_length=100)
    method_version: str = Field(min_length=1, max_length=100)
    coverage: Literal["sampled_frames", "full_frame_scan"]
    observations: tuple[FaceBoxObservation, ...] = ()
    evidence_reference: str = Field(min_length=1, max_length=2_000)

    @model_validator(mode="after")
    def observations_in_interval(self) -> "CropAssessment":
        if any(not self.proposal.start_ms <= item.time_ms < self.proposal.end_ms for item in self.observations):
            raise ValueError("face observations must fall within the assessed interval")
        return self

    @property
    def decision(self) -> Literal["unusable", "unknown"]:
        crop = self.proposal.crop_window
        for observation in self.observations:
            face = observation.face_box
            if (face.x < crop.x or face.y < crop.y or
                face.x + face.width > crop.x + crop.width or
                face.y + face.height > crop.y + crop.height):
                return "unusable"
        return "unknown"


class CropAssessmentRepository:
    def __init__(self, db: Database):
        self.db = db
        self.assets = AssetRepository(db)
        self.clips = ClipRepository(db)

    def create(self, value: CropAssessment) -> CropAssessment:
        asset = self.assets.get(value.proposal.source_asset_id)
        clip = self.clips.get(value.proposal.source_clip_id)
        if asset is None or clip is None or clip.asset_id != asset.id:
            raise ValueError("crop assessment source Asset/Clip is unavailable or mismatched")
        if value.source_content_hash != asset.content_hash:
            raise ValueError("crop assessment source hash is stale")
        if value.proposal.start_ms < clip.start_ms or value.proposal.end_ms > clip.end_ms:
            raise ValueError("crop assessment interval exceeds source Clip")
        crop = value.proposal.crop_window
        if crop.x + crop.width > asset.width or crop.y + crop.height > asset.height:
            raise ValueError("crop assessment window exceeds source dimensions")
        if any(item.face_box.x + item.face_box.width > asset.width or
               item.face_box.y + item.face_box.height > asset.height for item in value.observations):
            raise ValueError("observed face box exceeds source dimensions")
        self.db.connection.execute(
            "INSERT INTO crop_assessments(id, source_asset_id, source_clip_id, payload) VALUES (?, ?, ?, ?)",
            (str(value.id), str(asset.id), str(clip.id), value.model_dump_json()),
        )
        return value

    def decision(self, proposal: CropProposal) -> Literal["unusable", "unknown"]:
        asset = self.assets.get(proposal.source_asset_id)
        clip = self.clips.get(proposal.source_clip_id)
        if asset is None or clip is None or clip.asset_id != asset.id:
            return "unknown"
        if proposal.start_ms < clip.start_ms or proposal.end_ms > clip.end_ms:
            return "unknown"
        rows = self.db.connection.execute(
            "SELECT payload FROM crop_assessments WHERE source_asset_id = ? AND source_clip_id = ? ORDER BY rowid",
            (str(asset.id), str(clip.id)),
        )
        for row in rows:
            assessment = CropAssessment.model_validate(json.loads(row["payload"]))
            if (assessment.source_content_hash == asset.content_hash and
                assessment.proposal.start_ms == proposal.start_ms and
                assessment.proposal.end_ms == proposal.end_ms and
                assessment.proposal.crop_window == proposal.crop_window and
                assessment.evidence_class != "fixture" and
                assessment.decision == "unusable"):
                return "unusable"
        return "unknown"
