"""Strict, deterministic VideoSpec assembly from persisted continuous Clips."""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
import re
from uuid import UUID

from app.db import AssetRepository, AudioAssetRepository, ClipRepository, ImageAssetRepository
from app.domain.models import (
    Asset,
    AudioAsset,
    CandidateAsset,
    Clip,
    CostCategory,
    ImageAsset,
    Project,
    RationalFps,
    ScenePlan,
    SourceKind,
    TranscriptSegment,
    UsageCost,
    VideoCaption,
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


class NarrationRequiredForNewScript(VideoSpecAssemblyError):
    pass


class NarrationBindingError(VideoSpecAssemblyError):
    pass


_REAL_CONTINUOUS_SOURCES = frozenset((SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET))
_TEXT_TOKEN = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]", re.IGNORECASE)
_TERMINAL_PURPOSES = frozenset(("close", "closing", "boundary", "cta", "outro", "ending"))
_SOURCE_TAIL_MAX_MS = 4_000
_SOURCE_PAUSE_BOUNDARY_MS = 80


class VideoSpecAssembler:
    """Build a strict in-memory VideoSpec without media writes or provider calls.

    ``selected_by_scene`` is keyed by ``ScenePlan.id``. A non-recommended
    candidate is accepted only when its ScenePlan ID is explicitly listed in
    ``explicit_scene_ids``; this makes the otherwise user-driven override
    visible at the call boundary.
    """

    def __init__(
        self,
        assets: AssetRepository,
        clips: ClipRepository,
        images: ImageAssetRepository | None = None,
        audios: AudioAssetRepository | None = None,
    ) -> None:
        self.assets = assets
        self.clips = clips
        self.images = images
        self.audios = audios

    def assemble(
        self,
        project: Project,
        scenes: Sequence[ScenePlan],
        selected_by_scene: Mapping[UUID, CandidateAsset],
        *,
        explicit_scene_ids: Iterable[UUID] = (),
        narration_asset_ids: Mapping[UUID, UUID] | None = None,
        narration_required: bool = False,
    ) -> VideoSpec:
        if not isinstance(project, Project):
            raise InvalidCandidateSelection("assembly requires a Project contract")
        ordered = _ordered_scenes(scenes, project.id)
        selections = _selections(selected_by_scene, ordered)
        explicit = _explicit_ids(explicit_scene_ids, ordered)
        narration = _narration_ids(narration_asset_ids, ordered)
        if narration_required:
            missing = next((scene.scene_id for scene in ordered if scene.id not in narration), None)
            if missing is not None:
                raise NarrationRequiredForNewScript(
                    f"scene {missing!r} has new script text but no narration audio; import or bind an authorized narration before rendering"
                )
        narration_audio: dict[UUID, AudioAsset] = {}
        if narration:
            if self.audios is None:
                raise CandidateNotFound("narration audio repository is unavailable")
            for scene_id, audio_id in narration.items():
                audio = self.audios.get(audio_id)
                if audio is None:
                    raise CandidateNotFound("selected narration audio does not exist")
                narration_audio[scene_id] = audio
            if narration_required and len({audio.id for audio in narration_audio.values()}) != len(narration_audio):
                raise NarrationBindingError(
                    "each new-script scene must bind a separate authorized narration asset; split a full recording into scene files before rendering"
                )
        video_scenes: list[VideoScene] = []
        start_frame = 0
        selected_sources: list[SourceKind] = []
        for scene in ordered:
            candidate = selections[scene.id]
            audio = narration_audio.get(scene.id)
            duration_ms = scene.duration_target_ms
            narration_start_ms: int | None = None
            narration_end_ms: int | None = None
            if narration_required and audio is not None:
                # New scenes follow the measured recording duration instead of
                # the old fixed target window, so captions and speech do not
                # end mid-sentence or continue over unrelated source audio.
                duration_ms = audio.duration_ms
                narration_start_ms = 0
                narration_end_ms = audio.duration_ms
            if candidate.source_kind == SourceKind.CAPTURE or candidate.requires_capture:
                raise CaptureGapSelected(f"scene {scene.scene_id!r} requires capture and cannot be assembled")
            if not candidate.recommended and scene.id not in explicit:
                raise InvalidCandidateSelection(f"scene {scene.scene_id!r} must use a recommended or explicit candidate")
            if candidate.source_kind == SourceKind.TYPOGRAPHY:
                duration_frames = milliseconds_to_frames(duration_ms, project.fps)
                video_scenes.append(VideoScene(
                    scene_id=scene.scene_id,
                    start_frame=start_frame,
                    duration_frames=duration_frames,
                    visual=VideoVisual(
                        source_kind=SourceKind.TYPOGRAPHY,
                        authorization_reference="local-typography",
                    ),
                    narration_asset_id=narration.get(scene.id),
                    narration_start_ms=narration_start_ms,
                    narration_end_ms=narration_end_ms,
                    caption=scene.voice_text,
                ))
                start_frame += duration_frames
                selected_sources.append(SourceKind.TYPOGRAPHY)
                continue
            if candidate.source_kind in {SourceKind.SCREENSHOT, SourceKind.CHART}:
                image = self._stored_image(candidate)
                duration_frames = milliseconds_to_frames(duration_ms, project.fps)
                video_scenes.append(VideoScene(
                    scene_id=scene.scene_id,
                    start_frame=start_frame,
                    duration_frames=duration_frames,
                    visual=VideoVisual(
                        source_kind=image.source_kind,
                        authorization_reference=image.authorization_reference,
                        asset_id=image.id,
                    ),
                    narration_asset_id=narration.get(scene.id),
                    narration_start_ms=narration_start_ms,
                    narration_end_ms=narration_end_ms,
                    caption=scene.voice_text,
                ))
                start_frame += duration_frames
                selected_sources.append(image.source_kind)
                continue
            asset, clip = self._stored_real_clip(scene, candidate)
            clip_start_ms, clip_end_ms = clip.start_ms, clip.end_ms
            original_clip_end_ms = clip.end_ms
            captions: list[VideoCaption] = []
            source_interval_aligned = False
            if audio is not None:
                captions = _audio_captions(audio, duration_ms)
                if narration_required:
                    # Check the persisted source boundary before deriving the
                    # exact visual interval. Updating ``clip_end_ms`` first
                    # used to make a too-long narration appear valid until the
                    # renderer rejected it later.
                    if duration_ms > original_clip_end_ms - clip_start_ms:
                        raise InsufficientSourceDuration(
                            f"scene {scene.scene_id!r} narration duration exceeds its continuous Clip"
                        )
                    requested_frames = milliseconds_to_frames(duration_ms, project.fps)
                    available_frames = source_interval_to_frames(
                        clip_start_ms,
                        clip_start_ms + duration_ms,
                        project.fps,
                    )
                    if available_frames < requested_frames:
                        raise InsufficientSourceDuration(
                            f"scene {scene.scene_id!r} narration duration cannot fit the Clip at the project frame rate"
                        )
                    # The source audio is muted, so there is no hidden tail
                    # that could continue after the new narration ends.
                    clip_end_ms = clip_start_ms + duration_ms
            elif not narration_required:
                aligned = _source_sentence_interval(
                    scene.voice_text,
                    clip,
                    extend_trailing_context=_is_terminal_purpose(scene.purpose),
                )
                if aligned is not None:
                    clip_start_ms, clip_end_ms = aligned
                    duration_ms = clip_end_ms - clip_start_ms
                    source_interval_aligned = True
                captions = _source_captions(clip, clip_start_ms, clip_end_ms)
            if duration_ms > clip_end_ms - clip_start_ms:
                raise InsufficientSourceDuration(f"scene {scene.scene_id!r} target duration exceeds its continuous Clip")
            duration_frames = (
                source_interval_to_frames(clip_start_ms, clip_end_ms, project.fps)
                if source_interval_aligned
                else milliseconds_to_frames(duration_ms, project.fps)
            )
            visual = VideoVisual(
                source_kind=asset.source_kind,
                authorization_reference=asset.authorization_reference,
                asset_id=asset.id,
                clip_id=clip.id,
                clip_start_ms=clip_start_ms,
                clip_end_ms=clip_end_ms,
                source_duration_ms=asset.duration_ms,
            )
            video_scenes.append(VideoScene(
                scene_id=scene.scene_id,
                start_frame=start_frame,
                duration_frames=duration_frames,
                visual=visual,
                narration_asset_id=narration.get(scene.id),
                narration_start_ms=narration_start_ms,
                narration_end_ms=narration_end_ms,
                # Keep the planned text in the contract for draft identity and
                # render-job idempotency. The Remotion source-audio path hides
                # this field unless a narration asset is actually attached;
                # only persisted transcript captions may appear over source
                # audio.
                caption=scene.voice_text,
                captions=captions,
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

    def _stored_image(self, candidate: CandidateAsset) -> ImageAsset:
        if self.images is None or candidate.asset_id is None or candidate.clip_id is not None:
            raise CandidateNotFound("selected static visual is unavailable")
        image = self.images.get(candidate.asset_id)
        if image is None or image.source_kind != candidate.source_kind:
            raise CandidateNotFound("selected static visual does not exist")
        return image


def milliseconds_to_frames(duration_ms: int, fps: RationalFps) -> int:
    """Ceiling conversion using integer arithmetic, never a binary float."""
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms <= 0:
        raise InvalidCandidateSelection("scene duration must be a positive integer number of milliseconds")
    if not isinstance(fps, RationalFps):
        raise InvalidCandidateSelection("fps must be a RationalFps contract")
    numerator = duration_ms * fps.numerator
    denominator = 1_000 * fps.denominator
    return (numerator + denominator - 1) // denominator


def source_interval_to_frames(start_ms: int, end_ms: int, fps: RationalFps) -> int:
    """Return the frame-safe capacity of a source interval.

    Source-led scenes use the exact persisted ASR interval. A ceiling on the
    scene duration alone can exceed the number of complete composition frames
    available after the source start is ceiled and source end is floored.
    Keeping this conversion here makes the VideoSpec renderable without
    extending playback beyond the authorized semantic interval.
    """
    if isinstance(start_ms, bool) or not isinstance(start_ms, int) or start_ms < 0:
        raise InvalidCandidateSelection("source interval start must be a non-negative integer number of milliseconds")
    if isinstance(end_ms, bool) or not isinstance(end_ms, int) or end_ms <= start_ms:
        raise InvalidCandidateSelection("source interval end must be greater than its start")
    start_numerator = start_ms * fps.numerator
    end_numerator = end_ms * fps.numerator
    denominator = 1_000 * fps.denominator
    start_frame = (start_numerator + denominator - 1) // denominator
    end_frame = end_numerator // denominator
    if end_frame <= start_frame:
        raise InsufficientSourceDuration("source interval is shorter than one composition frame")
    return end_frame - start_frame


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


def _narration_ids(narration_asset_ids: Mapping[UUID, UUID] | None, scenes: Sequence[ScenePlan]) -> dict[UUID, UUID]:
    if narration_asset_ids is None:
        return {}
    if not isinstance(narration_asset_ids, Mapping):
        raise InvalidCandidateSelection("narration assets must be keyed by ScenePlan ID")
    expected = {scene.id for scene in scenes}
    values = dict(narration_asset_ids)
    if any(not isinstance(scene_id, UUID) or not isinstance(audio_id, UUID) for scene_id, audio_id in values.items()):
        raise InvalidCandidateSelection("narration assets must use UUID values")
    if not set(values).issubset(expected):
        raise InvalidCandidateSelection("narration assets must belong to supplied ScenePlans")
    return values


def _source_sentence_interval(
    voice_text: str,
    clip: Clip,
    *,
    extend_trailing_context: bool = False,
) -> tuple[int, int] | None:
    """Use persisted ASR boundaries when a source-led scene has real text.

    If the scene text cannot be matched to timestamped transcript text, this
    returns ``None`` and the caller retains the original Clip interval instead
    of guessing a semantic cut.
    """
    if not clip.transcript_segments:
        return None
    target_token_list = _TEXT_TOKEN.findall(voice_text.lower())
    target_tokens = set(target_token_list)
    target_text = "".join(target_token_list)
    if not target_text:
        return None

    segments = [
        segment
        for segment in clip.transcript_segments
        if segment.end_ms > clip.start_ms and segment.start_ms < clip.end_ms
    ]
    # Whisper-style ASR commonly returns one short segment per phrase. Match
    # the normalized text across adjacent persisted segments before falling
    # back to the historical single-segment token overlap rule. The returned
    # interval still uses complete ASR boundaries, so it never invents a cut
    # inside a spoken segment.
    for start_index, start_segment in enumerate(segments):
        normalized = ""
        spans: list[tuple[TranscriptSegment, int, int]] = []
        previous_end_ms: int | None = None
        for end_segment in segments[start_index:]:
            if previous_end_ms is not None and end_segment.start_ms - previous_end_ms > 5_000:
                break
            segment_text = "".join(_TEXT_TOKEN.findall(end_segment.text.lower()))
            segment_start_index = len(normalized)
            normalized += segment_text
            spans.append((end_segment, segment_start_index, len(normalized)))
            match_start = normalized.find(target_text)
            if match_start >= 0:
                match_end = match_start + len(target_text)
                matched_start = next(segment for segment, span_start, span_end in spans if span_start <= match_start < span_end)
                matched_end = next(segment for segment, span_start, span_end in spans if span_start < match_end <= span_end)
                start_ms = max(clip.start_ms, matched_start.start_ms)
                end_ms = min(clip.end_ms, matched_end.end_ms)
                if extend_trailing_context:
                    matched_end_index = next(index for index, segment in enumerate(segments) if segment is matched_end)
                    end_ms = _extend_trailing_context(segments, matched_end_index, end_ms, clip.end_ms)
                if end_ms - start_ms >= 250:
                    return start_ms, end_ms
            previous_end_ms = end_segment.end_ms
            if len(normalized) > len(target_text) and target_text not in normalized:
                break

    matches = []
    for segment in segments:
        segment_tokens = set(_TEXT_TOKEN.findall(segment.text.lower()))
        if not segment_tokens:
            continue
        overlap = len(target_tokens & segment_tokens) / len(target_tokens)
        segment_text = "".join(_TEXT_TOKEN.findall(segment.text.lower()))
        if overlap >= 0.5 or target_text in segment_text:
            matches.append(segment)
    if not matches:
        return None
    start_ms = max(clip.start_ms, min(segment.start_ms for segment in matches))
    end_ms = min(clip.end_ms, max(segment.end_ms for segment in matches))
    if end_ms - start_ms < 250:
        return None
    return start_ms, end_ms


def _is_terminal_purpose(purpose: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", purpose.lower()).strip()
    return any(token in _TERMINAL_PURPOSES for token in normalized.split())


def _extend_trailing_context(
    segments: Sequence[TranscriptSegment],
    matched_end_index: int,
    end_ms: int,
    clip_end_ms: int,
) -> int:
    """Carry a short, contiguous real-ASR tail into terminal scenes.

    ScenePlan text is often selected from a rolling transcript window. For a
    closing scene, stopping exactly at the matched phrase can leave the
    speaker's next consequence hanging. This helper only consumes persisted
    adjacent transcript segments, stops at a real pause or punctuation, and
    never invents text or crosses the selected Clip interval.
    """
    initial_end_ms = end_ms
    for segment in segments[matched_end_index + 1:]:
        if segment.start_ms - end_ms > _SOURCE_PAUSE_BOUNDARY_MS:
            break
        next_end_ms = min(clip_end_ms, segment.end_ms)
        if next_end_ms <= end_ms or next_end_ms - initial_end_ms > _SOURCE_TAIL_MAX_MS:
            break
        end_ms = next_end_ms
        if re.search(r"[.!?。！？；;]", segment.text):
            break
    return end_ms


def _source_captions(clip: Clip, start_ms: int, end_ms: int) -> list[VideoCaption]:
    """Project persisted source-relative transcript intervals onto one scene."""
    captions: list[VideoCaption] = []
    for segment in clip.transcript_segments:
        caption_start = max(start_ms, segment.start_ms)
        caption_end = min(end_ms, segment.end_ms)
        if caption_end <= caption_start:
            continue
        captions.append(VideoCaption(
            start_ms=caption_start - start_ms,
            end_ms=caption_end - start_ms,
            text=segment.text,
        ))
    return captions


def _audio_captions(audio: AudioAsset, duration_ms: int) -> list[VideoCaption]:
    return [
        VideoCaption(
            start_ms=segment.start_ms,
            end_ms=min(segment.end_ms, duration_ms),
            text=segment.text,
        )
        for segment in audio.transcript_segments
        if segment.start_ms < duration_ms and min(segment.end_ms, duration_ms) > segment.start_ms
    ]


def _local_assembly_cost(sources: Sequence[SourceKind]) -> UsageCost:
    # The existing contract supports one category only. RENDER accurately
    # describes the aggregate assembly operation while recording no provider
    # charge for the already-local source Clips.
    return UsageCost(
        category=CostCategory.RENDER,
        amount=Decimal("0"),
        currency="USD",
        note=f"{len(sources)} selected local visuals; no provider cost.",
    )
