"""Strict, deterministic VideoSpec assembly from persisted continuous Clips."""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from uuid import UUID

from app.db import AssetRepository, ClipRepository
from app.domain.models import (
    Asset,
    CandidateAsset,
    Clip,
    CostCategory,
    Project,
    RationalFps,
    ScenePlan,
    SourceKind,
    UsageCost,
    VideoScene,
    VideoSpec,
    VideoVisual,
)


class VideoSpecAssemblyError(ValueError):
    """Base error for local contract/identity failures during assembly."""


class InvalidCandidateSelection(VideoSpecAssemblyError):
    pass


class CaptureGapSelected(VideoSpecAssemblyError):
    pass


class CandidateNotFound(VideoSpecAssemblyError):
    pass


class AssetNotFound(VideoSpecAssemblyError):
    pass


class AssetIdentityMismatch(VideoSpecAssemblyError):
    pass


class InsufficientSourceDuration(VideoSpecAssemblyError):
    pass


_REAL_CONTINUOUS_SOURCES = frozenset((SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET))


class VideoSpecAssembler:
    """Build a strict in-memory VideoSpec without media writes or provider calls.

    ``selected_by_scene`` is keyed by ``ScenePlan.id``. A non-recommended
    candidate is accepted only when its ScenePlan ID is explicitly listed in
    ``explicit_scene_ids``; this makes the otherwise user-driven override
    visible at the call boundary.
    """

    def __init__(self, assets: AssetRepository, clips: ClipRepository) -> None:
        self.assets = assets
        self.clips = clips

    def assemble(
        self,
        project: Project,
        scenes: Sequence[ScenePlan],
        selected_by_scene: Mapping[UUID, CandidateAsset],
        *,
        explicit_scene_ids: Iterable[UUID] = (),
    ) -> VideoSpec:
        if not isinstance(project, Project):
            raise InvalidCandidateSelection("assembly requires a Project contract")
        ordered = _ordered_scenes(scenes, project.id)
        selections = _selections(selected_by_scene, ordered)
        explicit = _explicit_ids(explicit_scene_ids, ordered)
        video_scenes: list[VideoScene] = []
        start_frame = 0
        selected_sources: list[SourceKind] = []
        for scene in ordered:
            candidate = selections[scene.id]
            if candidate.source_kind == SourceKind.CAPTURE or candidate.requires_capture:
                raise CaptureGapSelected(f"scene {scene.scene_id!r} requires capture and cannot be assembled")
            if not candidate.recommended and scene.id not in explicit:
                raise InvalidCandidateSelection(f"scene {scene.scene_id!r} must use a recommended or explicit candidate")
            asset, clip = self._stored_real_clip(scene, candidate)
            if scene.duration_target_ms > clip.end_ms - clip.start_ms:
                raise InsufficientSourceDuration(f"scene {scene.scene_id!r} target duration exceeds its continuous Clip")
            duration_frames = milliseconds_to_frames(scene.duration_target_ms, project.fps)
            visual = VideoVisual(
                source_kind=asset.source_kind,
                authorization_reference=asset.authorization_reference,
                asset_id=asset.id,
                clip_id=clip.id,
                clip_start_ms=clip.start_ms,
                clip_end_ms=clip.end_ms,
                source_duration_ms=asset.duration_ms,
            )
            video_scenes.append(VideoScene(
                scene_id=scene.scene_id,
                start_frame=start_frame,
                duration_frames=duration_frames,
                visual=visual,
                caption=scene.voice_text,
            ))
            start_frame += duration_frames
            selected_sources.append(asset.source_kind)
        return VideoSpec(
            project_id=project.id,
            format=project.format,
            width=project.resolution_width,
            height=project.resolution_height,
            fps=project.fps,
            scenes=video_scenes,
            estimated_cost=_local_assembly_cost(selected_sources),
        )

    def _stored_real_clip(self, scene: ScenePlan, candidate: CandidateAsset) -> tuple[Asset, Clip]:
        if candidate.source_kind not in _REAL_CONTINUOUS_SOURCES:
            raise InvalidCandidateSelection(f"scene {scene.scene_id!r} does not select a real continuous Clip")
        if candidate.asset_id is None or candidate.clip_id is None:
            raise CandidateNotFound(f"scene {scene.scene_id!r} candidate must reference both Asset and Clip")
        clip = self.clips.get(candidate.clip_id)
        if clip is None:
            raise CandidateNotFound(f"selected Clip for scene {scene.scene_id!r} does not exist")
        asset = self.assets.get(candidate.asset_id)
        if asset is None:
            raise AssetNotFound(f"selected Asset for scene {scene.scene_id!r} does not exist")
        if (
            clip.asset_id != asset.id
            or asset.source_kind != candidate.source_kind
            or clip.asset_duration_ms != asset.duration_ms
            or clip.end_ms > asset.duration_ms
        ):
            raise AssetIdentityMismatch(f"selected Asset and Clip do not match for scene {scene.scene_id!r}")
        return asset, clip


def milliseconds_to_frames(duration_ms: int, fps: RationalFps) -> int:
    """Ceiling conversion using integer arithmetic, never a binary float."""
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms <= 0:
        raise InvalidCandidateSelection("scene duration must be a positive integer number of milliseconds")
    if not isinstance(fps, RationalFps):
        raise InvalidCandidateSelection("fps must be a RationalFps contract")
    numerator = duration_ms * fps.numerator
    denominator = 1_000 * fps.denominator
    return (numerator + denominator - 1) // denominator


def _ordered_scenes(scenes: Sequence[ScenePlan], project_id: UUID) -> tuple[ScenePlan, ...]:
    if isinstance(scenes, (str, bytes)):
        raise InvalidCandidateSelection("assembly requires one or more ScenePlans")
    values = tuple(scenes)
    if not values or any(not isinstance(scene, ScenePlan) for scene in values):
        raise InvalidCandidateSelection("assembly requires one or more ScenePlan contracts")
    if any(scene.project_id != project_id for scene in values):
        raise InvalidCandidateSelection("all ScenePlans must belong to the supplied Project")
    ordered = tuple(sorted(values, key=lambda scene: (scene.order, scene.scene_id, str(scene.id))))
    if [scene.order for scene in ordered] != list(range(len(ordered))):
        raise InvalidCandidateSelection("ScenePlan orders must be contiguous and start at zero")
    if len({scene.id for scene in ordered}) != len(ordered) or len({scene.scene_id for scene in ordered}) != len(ordered):
        raise InvalidCandidateSelection("ScenePlan IDs and scene_id values must be unique")
    return ordered


def _selections(selected_by_scene: Mapping[UUID, CandidateAsset], scenes: Sequence[ScenePlan]) -> dict[UUID, CandidateAsset]:
    if not isinstance(selected_by_scene, Mapping):
        raise InvalidCandidateSelection("selected candidates must be keyed by ScenePlan ID")
    expected = {scene.id for scene in scenes}
    if set(selected_by_scene) != expected:
        raise InvalidCandidateSelection("selected candidates must contain exactly one candidate per ScenePlan")
    values = dict(selected_by_scene)
    if any(not isinstance(scene_id, UUID) or not isinstance(candidate, CandidateAsset) or candidate.scene_plan_id != scene_id for scene_id, candidate in values.items()):
        raise InvalidCandidateSelection("each selected candidate must match its ScenePlan ID")
    return values


def _explicit_ids(explicit_scene_ids: Iterable[UUID], scenes: Sequence[ScenePlan]) -> frozenset[UUID]:
    if isinstance(explicit_scene_ids, (str, bytes)):
        raise InvalidCandidateSelection("explicit scene IDs must be UUID values")
    try:
        values = frozenset(explicit_scene_ids)
    except TypeError as exc:
        raise InvalidCandidateSelection("explicit scene IDs must be UUID values") from exc
    expected = {scene.id for scene in scenes}
    if not values.issubset(expected) or any(not isinstance(scene_id, UUID) for scene_id in values):
        raise InvalidCandidateSelection("explicit scene IDs must belong to supplied ScenePlans")
    return values


def _local_assembly_cost(sources: Sequence[SourceKind]) -> UsageCost:
    # The existing contract supports one category only. RENDER accurately
    # describes the aggregate assembly operation while recording no provider
    # charge for the already-local source Clips.
    return UsageCost(
        category=CostCategory.RENDER,
        amount=Decimal("0"),
        currency="USD",
        note=f"{len(sources)} selected local continuous Clip(s); no provider cost.",
    )
