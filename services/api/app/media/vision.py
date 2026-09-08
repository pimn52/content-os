"""Provider-neutral visual metadata mapping and atomic Clip persistence."""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence
from uuid import UUID

from app.db import ClipRepository, Database
from app.domain.models import Clip
from app.providers.vision import ClipVisualAnalysis


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

    @classmethod
    def for_clip(cls, clip: Clip, keyframe_path: str | Path, analysis: ClipVisualAnalysis) -> "VisualMetadata":
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
    updates = {field: getattr(values, field) for field in _VISUAL_FIELDS}
    updates["people"] = list(values.people)
    updates["objects"] = list(values.objects)
    merged = clip.model_dump(mode="python")
    merged.update(updates)
    return Clip.model_validate(merged)


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
            clips = self.clips.list_by_asset(asset_id)
            if not clips:
                raise NoClipsForVisualMetadata(f"asset {asset_id} has no Clips")
            if len(normalized) != len(clips):
                raise VisualMetadataMismatch("visual result count does not match persisted Clips")
            if any(result.asset_id != asset_id for result in normalized):
                raise VisualMetadataMismatch("visual result asset identity does not match target Asset")
            if [result.clip_id for result in normalized] != [clip.id for clip in clips]:
                raise VisualMetadataMismatch("visual result Clip order or identity does not match persisted Clips")
            mapped = [map_visual_metadata(clip, result) for clip, result in zip(clips, normalized)]
            for clip in mapped:
                self.clips.update(clip)
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
    return VisualMetadata(
        clip_id=clip_id, asset_id=asset_id,
        visual_description=_optional_text(value("visual_description"), 5_000), people=people, objects=objects,
        location=_optional_text(value("location"), 500), action=_optional_text(value("action"), 500), shot_type=_optional_text(value("shot_type"), 100),
        quality_score=scores["quality_score"], face_visibility=scores["face_visibility"], mouth_visibility=scores["mouth_visibility"],
        talking_candidate=talking, keyframe_path=None if value("keyframe_path") is None else str(value("keyframe_path")),
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
