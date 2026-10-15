"""Read-only facts for a proposed crop; never positive visual or execution admission."""
from pathlib import Path
from typing import Literal
import hashlib
from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.db import AssetRepository, ClipRepository, Database
from app.domain.models import SourceCropWindow
from .crop_assessment import CropAssessmentRepository, CropProposal
from .local_paths import existing_local_media_file


class ViewportFeasibilityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposal: CropProposal
    source_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_height: int = Field(default=1920, ge=1080, le=1920, multiple_of=2, strict=True)
    source_subtitle_state: Literal["unknown", "not_observed", "known_burned_in"] = "unknown"
    known_subtitle_regions: list[SourceCropWindow] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def subtitle_regions_are_explicit(self):
        if self.source_subtitle_state == "known_burned_in" and not self.known_subtitle_regions:
            raise ValueError("known subtitles require supplied regions")
        if self.source_subtitle_state != "known_burned_in" and self.known_subtitle_regions:
            raise ValueError("subtitle regions require known_burned_in state")
        return self


class ViewportFeasibility(BaseModel):
    request: ViewportFeasibilityRequest
    status: Literal["blocked", "needs_review"]
    stop_reasons: list[str]
    source_hash_verified: bool
    crop_assessment: Literal["unusable", "unknown"]
    output_width: Literal[1080] = 1080
    retained_source_fraction: float | None
    upscale_factor: float | None
    quality_unknowns: list[str] = Field(default_factory=lambda: ["full_interval_face_head_clearance", "motion_continuity", "effective_visual_quality"])
    transform_admission: Literal["not_evaluated"] = "not_evaluated"
    external_calls: Literal[0] = 0


def inspect_viewport(db: Database, data_root: Path, request: ViewportFeasibilityRequest) -> ViewportFeasibility:
    proposal, crop = request.proposal, request.proposal.crop_window
    asset = AssetRepository(db).get(proposal.source_asset_id)
    clip = ClipRepository(db).get(proposal.source_clip_id)
    if asset is None:
        raise FileNotFoundError("asset not found")
    if clip is None or clip.asset_id != asset.id:
        raise ValueError("source Clip is unavailable or mismatched")
    stops: list[str] = []
    verified = False
    if request.source_content_hash != asset.content_hash:
        stops.append("source_identity_changed")
    else:
        try:
            source = existing_local_media_file(asset.source_file, data_root)
            with source.open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            verified = actual == asset.content_hash
            if not verified:
                stops.append("source_bytes_changed")
        except (OSError, ValueError):
            stops.append("source_file_unavailable")
    if proposal.start_ms < clip.start_ms or proposal.end_ms > min(clip.end_ms, asset.duration_ms):
        stops.append("interval_outside_source_clip")
    if crop.x + crop.width > asset.width or crop.y + crop.height > asset.height:
        stops.append("crop_outside_source")
    if crop.width * request.output_height != crop.height * 1080:
        stops.append("crop_aspect_mismatch")
    if request.source_subtitle_state == "unknown":
        stops.append("source_subtitles_unknown")
    for region in request.known_subtitle_regions:
        if region.x + region.width > asset.width or region.y + region.height > asset.height:
            stops.append("subtitle_region_outside_source")
        if crop.x < region.x + region.width and region.x < crop.x + crop.width and crop.y < region.y + region.height and region.y < crop.y + crop.height:
            stops.append("known_subtitle_overlap")
    decision = CropAssessmentRepository(db).decision(proposal) if verified else "unknown"
    if decision == "unusable":
        stops.append("persisted_face_boundary_failure")
    return ViewportFeasibility(request=request, status="blocked" if stops else "needs_review", stop_reasons=list(dict.fromkeys(stops)),
        source_hash_verified=verified, crop_assessment=decision,
        retained_source_fraction=None if "crop_outside_source" in stops else crop.width * crop.height / (asset.width * asset.height),
        upscale_factor=None if "crop_aspect_mismatch" in stops or "crop_outside_source" in stops else 1080 / crop.width)
