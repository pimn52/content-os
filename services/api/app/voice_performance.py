"""Execution-only planning for the local OmniVoice performance experiment.

NarrationPerformanceUnit remains the exact-copy semantic/editorial layer.
VoiceGenerationSpan is a separate provider-call boundary: it may contain more
than one adjacent Unit when the adapter's local evidence says that a natural
longer phrase is more reliable.  Neither object claims word-level acoustic
control.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Sequence
from uuid import UUID
import wave

from app.domain.models import Asset, Clip, NarrationDeliverySegment, NarrationPause, NarrationPerformancePlan, SourceKind, VoiceProfile, VoiceReferenceWindowSelection
from app.narration_performance import compile_narration_delivery_plan, validate_narration_performance_plan


class VoicePerformancePlanningError(ValueError):
    pass


VOICE_REFERENCE_CONFIRMATION_PREFIX = "owner-confirmed-reference:"


def is_confirmed_voice_reference(clip: Clip) -> bool:
    source = clip.transcript_source
    return isinstance(source, str) and source.startswith(VOICE_REFERENCE_CONFIRMATION_PREFIX) and bool(
        source.removeprefix(VOICE_REFERENCE_CONFIRMATION_PREFIX).strip()
    )


@dataclass(frozen=True)
class VoiceReferenceWindow:
    clip_id: UUID
    asset_id: UUID
    start_ms: int
    end_ms: int
    transcript: str
    source_file: str
    rank_score: float | None
    source_timebase: str = "asset_pts"
    source_content_hash: str | None = None
    asset_content_hash: str | None = None
    source_duration_ms: int | None = None

    def __post_init__(self) -> None:
        if self.end_ms <= self.start_ms or not self.transcript.strip() or not self.source_file:
            raise ValueError("Voice reference window must be a non-empty local source interval")


def voice_reference_window_selection(window: VoiceReferenceWindow) -> VoiceReferenceWindowSelection:
    """Pin both the source bytes and the parent Asset that carries its rights."""
    if window.source_content_hash is None:
        raise VoicePerformancePlanningError("Voice reference source hash is unavailable")
    return VoiceReferenceWindowSelection(
        clip_id=window.clip_id,
        asset_id=window.asset_id,
        source_content_hash=window.source_content_hash,
        start_ms=window.start_ms,
        end_ms=window.end_ms,
        transcript_sha256=hashlib.sha256(window.transcript.encode("utf-8")).hexdigest(),
        source_timebase=window.source_timebase,
        asset_content_hash=window.asset_content_hash if window.source_timebase == "analysis_wav" else None,
    )


@dataclass(frozen=True)
class NarrationPerformanceUnit:
    index: int
    start_char: int
    end_char: int
    text: str
    segments: tuple[NarrationDeliverySegment, ...]
    pause_after: NarrationPause | None

    def __post_init__(self) -> None:
        if self.end_char <= self.start_char or not self.text.strip():
            raise ValueError("Voice performance unit must contain spoken exact copy")
        if "".join(segment.text for segment in self.segments) != self.text:
            raise ValueError("Voice performance unit must preserve its source segments")


@dataclass(frozen=True)
class VoiceGenerationSpan:
    """One bounded provider-call candidate composed from adjacent Units.

    The span retains every Unit (and consequently its emphasis, pace, pause
    and rhythm provenance).  Only its final boundary can be materialized as a
    composed silence; internal pause intent remains visible for QA/review and
    must not be represented as an unverified provider effect.
    """

    index: int
    start_char: int
    end_char: int
    text: str
    units: tuple[NarrationPerformanceUnit, ...]

    def __post_init__(self) -> None:
        if self.end_char <= self.start_char or not self.text.strip() or not self.units:
            raise ValueError("Voice generation span must contain spoken exact copy")
        if self.start_char != self.units[0].start_char or self.end_char != self.units[-1].end_char:
            raise ValueError("Voice generation span character provenance must match its units")
        if "".join(unit.text for unit in self.units) != self.text:
            raise ValueError("Voice generation span must preserve its source units")
        if any(left.end_char != right.start_char for left, right in zip(self.units, self.units[1:])):
            raise ValueError("Voice generation span units must be adjacent")

    @property
    def pause_after(self) -> NarrationPause | None:
        return self.units[-1].pause_after


@dataclass(frozen=True)
class VoicePerformanceRenderPlan:
    copy_fingerprint: str
    performance_plan_fingerprint: str
    units: tuple[NarrationPerformanceUnit, ...]


def plan_voice_performance_units(copy: str, plan: NarrationPerformancePlan) -> VoicePerformanceRenderPlan:
    """Group exact-copy delivery segments at semantic pauses or sentence ends."""
    validate_narration_performance_plan(plan, copy)
    delivery = compile_narration_delivery_plan(plan, copy)
    units: list[NarrationPerformanceUnit] = []
    buffered: list[NarrationDeliverySegment] = []
    for segment in delivery.segments:
        buffered.append(segment)
        boundary = segment.pause_after is not None or _sentence_end(segment.text) or segment.end_char == len(copy)
        if not boundary:
            continue
        text = "".join(item.text for item in buffered)
        if text.strip():
            units.append(NarrationPerformanceUnit(
                index=len(units), start_char=buffered[0].start_char, end_char=buffered[-1].end_char,
                text=text, segments=tuple(buffered), pause_after=segment.pause_after,
            ))
        buffered = []
    if buffered:
        raise VoicePerformancePlanningError("Voice performance units must end at a semantic or sentence boundary")
    if not units or "".join(unit.text for unit in units) != copy:
        raise VoicePerformancePlanningError("Voice performance plan must preserve every copy character")
    return VoicePerformanceRenderPlan(
        copy_fingerprint=delivery.copy_fingerprint,
        performance_plan_fingerprint=delivery.performance_plan_fingerprint,
        units=tuple(units),
    )


def plan_voice_generation_spans(
    render_plan: VoicePerformanceRenderPlan, *, maximum_adjacent_units: int,
) -> tuple[VoiceGenerationSpan, ...]:
    """Make adapter-scoped call boundaries without changing editorial Units.

    ``maximum_adjacent_units`` is an explicit execution-profile choice, not a
    global product duration limit.  The local OmniVoice V16 experiment uses two
    adjacent Units because its prior short-take evidence makes one tiny call per
    semantic Unit a poor generation-integrity boundary.  A future adapter or
    verified profile may choose another value without changing the editorial
    contract.
    """
    if maximum_adjacent_units < 1:
        raise ValueError("Voice generation span requires at least one unit")
    if not render_plan.units:
        raise VoicePerformancePlanningError("Voice generation span requires semantic units")
    spans: list[VoiceGenerationSpan] = []
    for start in range(0, len(render_plan.units), maximum_adjacent_units):
        grouped = render_plan.units[start:start + maximum_adjacent_units]
        spans.append(VoiceGenerationSpan(
            index=len(spans), start_char=grouped[0].start_char, end_char=grouped[-1].end_char,
            text="".join(unit.text for unit in grouped), units=grouped,
        ))
    if "".join(span.text for span in spans) != "".join(unit.text for unit in render_plan.units):
        raise VoicePerformancePlanningError("Voice generation spans must preserve every copy character")
    return tuple(spans)


def select_voice_reference_windows(
    clips: Sequence[Clip], assets: dict[UUID, Asset], *, data_root: str | Path | None = None,
    minimum_ms: int = 5_000, maximum_ms: int = 10_000,
) -> tuple[VoiceReferenceWindow, ...]:
    """Rank ASR candidates against the exact cached analysis-WAV clock.

    ASR timestamps belong to the generated 16 kHz mono analysis WAV, not the
    original Asset PTS timeline. This proposes candidates; it never approves
    their transcript, sentence boundaries, or audio quality.
    """
    if minimum_ms <= 0 or maximum_ms < minimum_ms:
        raise ValueError("Voice reference window bounds are invalid")
    candidates: list[VoiceReferenceWindow] = []
    grouped: dict[UUID, list[Clip]] = {}
    for clip in clips:
        if not is_confirmed_voice_reference(clip) and clip.asset_id in assets:
            grouped.setdefault(clip.asset_id, []).append(clip)
    for asset_id, asset_clips in grouped.items():
        asset = assets[asset_id]
        source_path = _analysis_audio_path(asset, data_root)
        if source_path is None:
            continue
        try:
            with wave.open(str(source_path), "rb") as audio:
                if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 16_000):
                    continue
                duration_ms = audio.getnframes() * 1000 // audio.getframerate()
        except (OSError, wave.Error):
            continue
        # Clip intervals use original Asset time. TranscriptSegment offsets use
        # the cached analysis WAV time, so deduplicate ASR rows without clipping
        # them against Clip.start_ms/end_ms.
        segment_clips: dict[tuple[int, int, str], Clip] = {}
        for clip in asset_clips:
            if not (clip.transcript_source or "").startswith("faster-whisper:"):
                continue
            for segment in clip.transcript_segments:
                if segment.end_ms <= duration_ms:
                    segment_clips.setdefault((segment.start_ms, segment.end_ms, segment.text.strip()), clip)
        segments = sorted(segment_clips, key=lambda item: (item[0], item[1], item[2]))
        audio_hash = _file_sha256(source_path)
        for start in range(len(segments)):
            end = start
            text: list[str] = []
            while end < len(segments):
                segment = segments[end]
                if segment[1] - segments[start][0] > maximum_ms:
                    break
                text.append(segment[2])
                duration = segment[1] - segments[start][0]
                if duration >= minimum_ms:
                    # Duration is only a display-order hint. It says nothing
                    # about sentence completeness, transcript accuracy, or audio cleanliness.
                    rank_score = -abs(duration - 8_000)
                    owner = segment_clips[segments[start]]
                    candidates.append(VoiceReferenceWindow(
                        clip_id=owner.id, asset_id=asset.id, start_ms=segments[start][0],
                        end_ms=segment[1], transcript=" ".join(text), source_file=str(source_path),
                        rank_score=rank_score, source_timebase="analysis_wav", source_content_hash=audio_hash,
                        asset_content_hash=asset.content_hash, source_duration_ms=duration_ms,
                    ))
                end += 1
    return tuple(sorted(candidates, key=lambda item: (-(item.rank_score or 0), item.clip_id.hex, item.start_ms, item.end_ms)))


def authorized_voice_reference_choices(
    profile: VoiceProfile, clips: Sequence[Clip], assets: dict[UUID, Asset], *, data_root: str | Path,
) -> tuple[tuple[VoiceReferenceWindowSelection, VoiceReferenceWindow], ...]:
    """Return only exact, user-confirmed Clip bindings in profile order."""
    if not profile.consent.confirmed:
        raise VoicePerformancePlanningError("Voice reference choices require explicit profile consent")
    allowed_ids = set(profile.reference_clip_ids)
    by_id = {clip.id: clip for clip in clips if clip.id in allowed_ids}
    choices = []
    for clip_id in profile.reference_clip_ids:
        clip = by_id.get(clip_id)
        if clip is None or not is_confirmed_voice_reference(clip):
            continue
        asset = assets.get(clip.asset_id)
        transcript = clip.transcript.strip() if isinstance(clip.transcript, str) else ""
        if asset is None or not transcript:
            continue
        if (len(clip.transcript_segments) != 1
                or clip.transcript_segments[0].start_ms != clip.start_ms
                or clip.transcript_segments[0].end_ms != clip.end_ms
                or clip.transcript_segments[0].text != transcript):
            continue
        source_timebase = "asset_pts"
        source_hash = asset.content_hash
        source_duration_ms = asset.duration_ms
        source_file = asset.source_file
        if clip.reference_source_timebase == "analysis_wav":
            if (data_root is None or clip.reference_source_file is None
                    or clip.reference_source_content_hash is None
                    or clip.reference_source_duration_ms is None
                    or asset.source_kind not in {SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET}
                    or not asset.has_audio
                    or asset.metadata.get("ip_profile_id") not in (None, str(profile.ip_profile_id))):
                continue
            source_timebase = "analysis_wav"
            source_hash = clip.reference_source_content_hash
            source_duration_ms = clip.reference_source_duration_ms
            source_file = clip.reference_source_file
        source_path = _resolve_source_path(source_file, data_root)
        if not source_path.is_file():
            continue
        if source_timebase == "analysis_wav":
            try:
                if not source_path.resolve().is_relative_to(Path(data_root).resolve()):
                    continue
                with wave.open(str(source_path), "rb") as audio:
                    if ((audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 16_000)
                            or audio.getnframes() * 1000 // audio.getframerate() != source_duration_ms):
                        continue
                if _file_sha256(source_path) != source_hash:
                    continue
            except (OSError, ValueError, wave.Error):
                continue
        window = VoiceReferenceWindow(
            clip_id=clip.id, asset_id=asset.id, start_ms=clip.start_ms,
            end_ms=clip.end_ms, transcript=transcript, source_file=str(source_path), rank_score=None,
            source_timebase=source_timebase, source_content_hash=source_hash,
            asset_content_hash=asset.content_hash, source_duration_ms=source_duration_ms,
        )
        choices.append((voice_reference_window_selection(window), window))
    return tuple(choices)


def resolve_authorized_voice_reference_choice(
    selection: VoiceReferenceWindowSelection, choices: Sequence[tuple[VoiceReferenceWindowSelection, VoiceReferenceWindow]],
) -> VoiceReferenceWindow:
    """Reject stale, unauthorized or changed windows instead of falling back."""
    for available, window in choices:
        if available == selection:
            return window
    raise VoicePerformancePlanningError("selected Voice reference window is no longer authorized or current")


def _sentence_end(text: str) -> bool:
    return bool(text.rstrip()) and text.rstrip()[-1] in "。！？!?；;"


def _resolve_source_path(source_file: str, data_root: str | Path | None) -> Path:
    path = Path(source_file)
    if path.is_absolute() or data_root is None:
        return path
    root = Path(data_root).resolve()
    return root.parent / path if path.parts and path.parts[0].casefold() == root.name.casefold() else root / path


def _analysis_audio_path(asset: Asset, data_root: str | Path | None) -> Path | None:
    if data_root is None:
        return None
    root = Path(data_root).resolve()
    path = (root / "assets" / "audio" / f"{asset.content_hash}-16000-1.wav").resolve()
    try:
        if not path.is_relative_to(root) or not path.is_file():
            return None
    except OSError:
        return None
    return path


def _file_sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


__all__ = [
    "NarrationPerformanceUnit", "VoiceGenerationSpan", "VoicePerformancePlanningError", "VoicePerformanceRenderPlan",
    "VoiceReferenceWindow",
    "plan_voice_generation_spans", "plan_voice_performance_units", "select_voice_reference_windows",
    "authorized_voice_reference_choices", "resolve_authorized_voice_reference_choice",
    "voice_reference_window_selection",
]
