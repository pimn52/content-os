"""Provider-neutral visual metadata mapping and atomic Clip persistence."""
from __future__ import annotations

import math
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence
from uuid import UUID

from app.db import AssetRepository, ClipRepository, Database
from app.domain.models import Clip
from app.providers.vision import ClipVisualAnalysis, validate_visual_geometry


class VisualMetadataError(RuntimeError):
    pass


class VisualMetadataMismatch(VisualMetadataError):
    pass


class NoClipsForVisualMetadata(VisualMetadataError):
    pass


@dataclass(frozen=True)
class VisualMetadata:
    clip_id: UUID
    asset_id: UUID
    visual_description: str | None = None
    people: tuple[str, ...] = ()
    objects: tuple[str, ...] = ()
    location: str | None = None
    action: str | None = None
    shot_type: str | None = None
    quality_score: float | None = None
    face_visibility: float | None = None
    mouth_visibility: float | None = None
    talking_candidate: bool = False
    keyframe_path: str | None = None
    comparison_layout: str = "unknown"
    subtitle_regions: tuple[tuple[float,float,float,float], ...] | None = None
    keyframe_timestamp_ms: float | None = None
    source_content_hash: str | None = None
    clip_interval_ms: tuple[int, int] | None = None

    @classmethod
    def for_clip(cls, clip: Clip, keyframe_path: str | Path, analysis: ClipVisualAnalysis, *, timestamp_ms: float | None = None,
        source_content_hash: str | None = None) -> "VisualMetadata":
        """Bind a provider result, which intentionally carries no IDs, to a Clip."""
        if not isinstance(analysis, ClipVisualAnalysis):
            raise TypeError("analysis must be ClipVisualAnalysis")
        return cls(
            clip_id=clip.id, asset_id=clip.asset_id, keyframe_path=str(keyframe_path),
            visual_description=analysis.visual_description, people=analysis.people,
            objects=analysis.objects, location=analysis.location, action=analysis.action,
            shot_type=analysis.shot_type, quality_score=analysis.quality_score,
            face_visibility=analysis.face_visibility, mouth_visibility=analysis.mouth_visibility,
            talking_candidate=analysis.talking_candidate,
            comparison_layout=analysis.comparison_layout,subtitle_regions=analysis.subtitle_regions,
            keyframe_timestamp_ms=timestamp_ms,
            source_content_hash=source_content_hash, clip_interval_ms=(clip.start_ms, clip.end_ms),
        )


_VISUAL_FIELDS = (
    "visual_description", "people", "objects", "location", "action", "shot_type",
    "quality_score", "face_visibility", "mouth_visibility", "talking_candidate",
)


def map_visual_metadata(clip: Clip, result: VisualMetadata | Mapping[str, object] | object) -> Clip:
    """Authoritatively replace visual fields while retaining all other Clip data."""
    values = _coerce(result)
    if values.clip_id != clip.id or values.asset_id != clip.asset_id:
        raise VisualMetadataMismatch("visual result identity does not match Clip")
    if values.clip_interval_ms is not None and values.clip_interval_ms != (clip.start_ms, clip.end_ms):
        raise VisualMetadataMismatch("Clip interval changed during visual analysis")
    updates = {field: getattr(values, field) for field in _VISUAL_FIELDS}
    updates["people"] = list(values.people)
    updates["objects"] = list(values.objects)
    merged = clip.model_dump(mode="python")
    merged.update(updates)
    return Clip.model_validate(merged)


def selected_visual_clips(clips: Sequence[Clip], clip_ids: Sequence[UUID] | None) -> list[Clip]:
    """Preserve stored ordering and reject unknown/duplicate explicit scopes."""
    if clip_ids is None:
        return list(clips)
    wanted = set(clip_ids)
    if not wanted or len(wanted) != len(clip_ids) or not wanted <= {clip.id for clip in clips}:
        raise VisualMetadataMismatch("selected Clip scope must be nonempty, unique and belong to the Asset")
    return [clip for clip in clips if clip.id in wanted]


def imported_visual_facts(asset, result):
    """Keep imported frame observations separate from full-interval clearance."""
    return dict(source_content_hash=asset.content_hash,
        clip_interval=dict(start_ms=result.start_ms, end_ms=result.end_ms),
        coverage="sampled_keyframes" if result.keyframes else "unverified_summary",
        comparison_layout="unknown", subtitle_regions=None,
        evidence_reference=f"analysis-clip:{result.clip_id}",
        observations=[dict(timestamp_ms=frame.timestamp_ms, reference=frame.reference,
            description=frame.description, visible_text=list(result.available_subtitles)) for frame in result.keyframes])


def source_output_presentation(project, asset, clip, interval):
    """只读复用装配规则；能排版不等于用途或成片质量通过。"""
    from app.assembly.edit_plan import source_subtitle_updates,comparison_clearance,EditPlanPreflightError
    from app.domain.models import ProjectFormat
    from .visual_facts import full_interval_geometry
    facts=full_interval_geometry(asset,clip,interval)
    if not facts:
        return {"available":False,"reason":"source_interval_unverified"}
    if not facts.get("source_subtitle_clearance_verified"):
        return {"available":False,"reason":"source_subtitle_clearance_required"}
    try:
        updates=source_subtitle_updates(asset,clip,interval)
        layouts=["full_frame_contain"]
        if (project.format is ProjectFormat.VERTICAL and asset.width*9==asset.height*16
            and facts.get("comparison_layout")=="side_by_side_equal_halves"):
            comparison_clearance(asset,clip,interval)
            layouts.append("stacked_comparison")
    except EditPlanPreflightError as error:
        return {"available":False,"reason":str(error)}
    return {"available":True,"output_format":project.format.value,"layouts":layouts,
        "landscape_secondary":project.format is ProjectFormat.VERTICAL and asset.width>asset.height,
        "source_bottom_crop_ratio":updates.get("source_bottom_crop_ratio",0),
        "evidence_reference":facts["evidence_reference"],"quality_state":"unverified"}


def planning_visual_evidence(db, asset, clip, *, project=None):
    """Expose where a description was observed, without inventing temporal coverage."""
    from .visual_facts import footage_geometry, full_interval_geometry
    analysis = asset.metadata.get("visual_analysis", {})
    stored = analysis.get(str(clip.id), {}) if isinstance(analysis, dict) else {}
    interval = stored.get("clip_interval", {}) if isinstance(stored, dict) else {}
    interval = (interval.get("start_ms"), interval.get("end_ms")) if isinstance(interval, dict) else None
    facts = footage_geometry(asset, clip, interval) if interval is not None else {}
    bound = bool(facts)
    checked = {}
    reviews=asset.metadata.get("source_presentation_reviews",{})
    reviewed=reviews.get(str(clip.id),{}) if isinstance(reviews,dict) else {}
    for candidate in (reviewed,stored):
        scope=candidate.get("clip_interval",{}) if isinstance(candidate,dict) else {}
        scope=(scope.get("start_ms"),scope.get("end_ms")) if isinstance(scope,dict) else None
        observed=full_interval_geometry(asset,clip,scope) if scope is not None else {}
        if observed:
            checked=observed;facts=observed;bound=True;break
    if not facts:
        # Legacy bundles lacked a source-hash snapshot. Read their actual frame
        # times as retrieval hints; never retrofit them as verified geometry.
        from app.db import AnalysisResultRepository
        ref = asset.metadata.get("r1_analysis", {})
        try:
            bundle = AnalysisResultRepository(db).get(UUID(ref["bundle_id"])) if isinstance(ref, dict) and ref.get("bundle_id") else None
        except (ValueError, TypeError):
            bundle = None
        result = next((item for item in bundle.results if item.clip_id == clip.id and item.asset_id == asset.id
            and item.start_ms == clip.start_ms and item.end_ms == clip.end_ms), None) if bundle else None
        if result is not None:
            facts = imported_visual_facts(asset, result)
    observations = facts.get("observations", [])
    if not facts:
        return {"coverage": "unverified_summary"}
    hints = []
    clips = ClipRepository(db).list_by_asset(asset.id)
    for item in observations[:8]:
        if not isinstance(item, dict) or type(item.get("timestamp_ms")) not in (int, float):
            continue
        timestamp = item["timestamp_ms"]
        containing = [c for c in clips if clip.start_ms <= c.start_ms <= timestamp < c.end_ms <= clip.end_ms]
        if containing:
            nearest = min(containing, key=lambda c:c.end_ms-c.start_ms)
            if nearest.end_ms-nearest.start_ms < clip.end_ms-clip.start_ms:
                hints.append({"timestamp_ms":timestamp,"start_ms":nearest.start_ms,"end_ms":nearest.end_ms,
                    "duration_ms":nearest.end_ms-nearest.start_ms})
    result = {"coverage": facts.get("coverage", "unverified_summary"), "source_hash_bound": bound,
        "interval": facts.get("clip_interval"),
        "observations": [{"timestamp_ms": item.get("timestamp_ms"),
            "description": str(item.get("description") or "")[:240]}
            for item in observations[:8] if isinstance(item, dict)]}
    if hints:
        result["local_interval_hints"] = hints
    if checked:
        result.update(visual_description=checked.get("visual_description") or clip.visual_description or "",
            evidence_reference=checked["evidence_reference"],
            comparison_layout=checked.get("comparison_layout","unknown"),
            source_subtitle_clearance_verified=checked.get("source_subtitle_clearance_verified") is True)
        output=None
        if project is not None:
            output=source_output_presentation(project,asset,clip,(checked["covered_start_ms"],checked["covered_end_ms"]))
            result["output_presentation"]=output
        if result["source_subtitle_clearance_verified"] and (output is None or output["available"]):
            start,end=checked["covered_start_ms"],checked["covered_end_ms"]
            result["usable_interval"]={"start_ms":start,"end_ms":end,"duration_ms":end-start}
    return result


class ClipVisualMetadataPersistence:
    """Atomically apply one visual result and keyframe per persisted Clip."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.clips = ClipRepository(db)

    def apply(
        self,
        asset_id: UUID,
        results: Sequence[VisualMetadata | Mapping[str, object] | object],
        keyframe_paths: Sequence[str | Path],
        *, selected_clip_ids: Sequence[UUID] | None = None,
    ) -> list[Clip]:
        normalized = [_coerce(result) for result in results]
        if len(normalized) != len(keyframe_paths):
            raise VisualMetadataMismatch("keyframe count does not match visual result count")
        paths = [Path(path).expanduser().resolve() for path in keyframe_paths]
        if any(not path.is_file() for path in paths):
            raise FileNotFoundError("every visual keyframe must be an existing file")
        for result, path in zip(normalized, paths):
            if result.keyframe_path is None or Path(result.keyframe_path).expanduser().resolve() != path:
                raise VisualMetadataMismatch("visual result keyframe does not match supplied keyframe")
        self.db.connection.execute("BEGIN IMMEDIATE")
        try:
            clips = selected_visual_clips(self.clips.list_by_asset(asset_id), selected_clip_ids)
            if not clips:
                raise NoClipsForVisualMetadata(f"asset {asset_id} has no Clips")
            if len(normalized) != len(clips):
                raise VisualMetadataMismatch("visual result count does not match persisted Clips")
            if any(result.asset_id != asset_id for result in normalized):
                raise VisualMetadataMismatch("visual result asset identity does not match target Asset")
            if [result.clip_id for result in normalized] != [clip.id for clip in clips]:
                raise VisualMetadataMismatch("visual result Clip order or identity does not match persisted Clips")
            assets = AssetRepository(self.db)
            asset = assets.get(asset_id)
            if asset is None or any(result.source_content_hash is not None and result.source_content_hash != asset.content_hash for result in normalized):
                raise VisualMetadataMismatch("source identity changed during visual analysis")
            for clip, result in zip(clips, normalized):
                if result.keyframe_timestamp_ms is not None and not clip.start_ms <= result.keyframe_timestamp_ms < clip.end_ms:
                    raise VisualMetadataMismatch("visual keyframe timestamp is outside its Clip")
            mapped = [map_visual_metadata(clip, result) for clip, result in zip(clips, normalized)]
            for clip in mapped:
                self.clips.update(clip)
            if asset is not None:
                geometry = dict(asset.metadata.get("visual_analysis",{}))
                for clip,result,path in zip(clips,normalized,paths):
                    frame_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                    geometry[str(clip.id)] = dict(source_content_hash=asset.content_hash,clip_interval=dict(start_ms=clip.start_ms,end_ms=clip.end_ms),
                        comparison_layout=result.comparison_layout,subtitle_regions=None if result.subtitle_regions is None else [list(box) for box in result.subtitle_regions],
                        evidence_reference=f"vision-keyframe:{clip.id}:{frame_hash}",coverage="sampled_keyframe",
                        observations=[dict(timestamp_ms=result.keyframe_timestamp_ms, frame_sha256=frame_hash,
                            reference=str(path), description=result.visual_description)])
                assets.update(asset.model_copy(update={"metadata":{**asset.metadata,"visual_analysis":geometry}}))
            self.db.connection.commit()
            return mapped
        except BaseException:
            self.db.connection.rollback()
            raise


def _coerce(result: VisualMetadata | Mapping[str, object] | object) -> VisualMetadata:
    def value(name: str, default: object = None) -> object:
        if isinstance(result, Mapping):
            return result.get(name, default)
        return getattr(result, name, default)

    clip_id, asset_id = value("clip_id"), value("asset_id")
    if not isinstance(clip_id, UUID) or not isinstance(asset_id, UUID):
        raise VisualMetadataMismatch("visual result must provide UUID clip_id and asset_id")
    people = _strings(value("people", ()), 30)
    objects = _strings(value("objects", ()), 100)
    scores: dict[str, float | None] = {}
    for field in ("quality_score", "face_visibility", "mouth_visibility"):
        score = value(field)
        if score is not None:
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(float(score)) or not 0 <= float(score) <= 1:
                raise ValueError(f"{field} must be a finite score from 0 through 1")
            scores[field] = float(score)
        else:
            scores[field] = None
    talking = value("talking_candidate", False)
    if not isinstance(talking, bool):
        raise ValueError("talking_candidate must be a boolean")
    layout,regions=value("comparison_layout","unknown"),value("subtitle_regions")
    validate_visual_geometry(layout,regions)
    timestamp = value("keyframe_timestamp_ms")
    if timestamp is not None and (isinstance(timestamp, bool) or not isinstance(timestamp, (int, float))
        or not math.isfinite(timestamp) or timestamp < 0):
        raise VisualMetadataMismatch("visual keyframe timestamp must be finite and nonnegative")
    interval = value("clip_interval_ms")
    if interval is not None and (not isinstance(interval, (list, tuple)) or len(interval) != 2
        or any(type(v) is not int for v in interval) or not 0 <= interval[0] < interval[1]):
        raise VisualMetadataMismatch("invalid visual Clip interval")
    return VisualMetadata(
        clip_id=clip_id, asset_id=asset_id,
        visual_description=_optional_text(value("visual_description"), 5_000), people=people, objects=objects,
        location=_optional_text(value("location"), 500), action=_optional_text(value("action"), 500), shot_type=_optional_text(value("shot_type"), 100),
        quality_score=scores["quality_score"], face_visibility=scores["face_visibility"], mouth_visibility=scores["mouth_visibility"],
        talking_candidate=talking, keyframe_path=None if value("keyframe_path") is None else str(value("keyframe_path")),
        comparison_layout=layout,subtitle_regions=regions,
        keyframe_timestamp_ms=timestamp,
        source_content_hash=value("source_content_hash"), clip_interval_ms=None if interval is None else tuple(interval),
    )


def _strings(value: object, maximum_count: int) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ValueError("visual people/objects must be a sequence of strings")
    if len(value) > maximum_count:
        raise ValueError("visual people/objects exceed their item limit")
    if any(not isinstance(item, str) for item in value):
        raise ValueError("visual people/objects contain invalid items")
    cleaned = tuple(" ".join(item.split()) for item in value if item.strip())
    if any(len(item) > 500 for item in cleaned):
        raise ValueError("visual people/objects contain invalid items")
    return cleaned


def _optional_text(value: object, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("visual text fields must be strings or None")
    cleaned = " ".join(value.split())
    if not cleaned or len(cleaned) > maximum:
        raise ValueError("visual text field is empty or exceeds its limit")
    return cleaned
