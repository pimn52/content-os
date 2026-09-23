"""Execution-only planning for the local OmniVoice performance experiment.

NarrationPerformanceUnit remains the exact-copy semantic/editorial layer.
VoiceGenerationSpan is a separate provider-call boundary: it may contain more
than one adjacent Unit when the adapter's local evidence says that a natural
longer phrase is more reliable.  Neither object claims word-level acoustic
control.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Sequence
from uuid import UUID

from app.domain.models import Asset, AudioAsset, Clip, NarrationDeliverySegment, NarrationPause, NarrationPerformancePlan, TranscriptSegment
from app.narration_performance import compile_narration_delivery_plan, validate_narration_performance_plan


class VoicePerformancePlanningError(ValueError):
    pass


@dataclass(frozen=True)
class VoiceReferenceWindow:
    clip_id: UUID
    asset_id: UUID
    start_ms: int
    end_ms: int
    transcript: str
    source_file: str
    score: float

    def __post_init__(self) -> None:
        if self.end_ms <= self.start_ms or not self.transcript.strip() or not self.source_file:
            raise ValueError("Voice reference window must be a non-empty local source interval")


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


@dataclass(frozen=True)
class VoicePerformanceComposition:
    output_path: Path
    expected_duration_ms: int
    transcript_segments: tuple[TranscriptSegment, ...]
    pause_after_ms: tuple[int, ...]


class VoicePerformanceComposer:
    """Concatenate independently QA-verified local takes with visible pauses."""

    def __init__(self, ffmpeg_command: str | Path = "ffmpeg", *, timeout_seconds: float = 120.0) -> None:
        self.ffmpeg_command, self.timeout_seconds = str(ffmpeg_command), timeout_seconds

    def compose(self, takes: Sequence[AudioAsset], spans: Sequence[VoiceGenerationSpan], output_path: str | Path) -> VoicePerformanceComposition:
        if len(takes) != len(spans) or not takes:
            raise VoicePerformancePlanningError("Performance composition requires one verified take per generation span")
        pauses = tuple(pause_duration_ms(span.pause_after) for span in spans)
        output = Path(output_path).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        command = [self.ffmpeg_command, "-y"]
        input_count = 0
        for take, pause_ms in zip(takes, pauses):
            generation = take.metadata.get("voice_generation")
            if not Path(take.source_file).is_file() or not isinstance(generation, dict) or generation.get("qa_state") != "verified":
                raise VoicePerformancePlanningError("Performance composition requires local QA-verified takes")
            command.extend(("-i", take.source_file))
            input_count += 1
            if pause_ms:
                command.extend(("-f", "lavfi", "-t", f"{pause_ms / 1000:.3f}", "-i", "anullsrc=r=24000:cl=mono"))
                input_count += 1
        inputs = "".join(f"[{index}:a]" for index in range(input_count))
        command.extend(("-filter_complex", f"{inputs}concat=n={input_count}:v=0:a=1[out]", "-map", "[out]", "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", str(output)))
        try:
            completed = subprocess.run(command, shell=False, capture_output=True, text=True, timeout=self.timeout_seconds, check=False)
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise VoicePerformancePlanningError("Local performance composition could not start") from exc
        if completed.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
            raise VoicePerformancePlanningError("Local performance composition did not produce playable audio")
        offset = 0
        segments: list[TranscriptSegment] = []
        for take, pause_ms in zip(takes, pauses):
            for segment in take.transcript_segments:
                segments.append(TranscriptSegment(start_ms=offset + segment.start_ms, end_ms=offset + segment.end_ms, text=segment.text))
            offset += take.duration_ms + pause_ms
        return VoicePerformanceComposition(output, offset, tuple(segments), pauses)


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
    minimum_ms: int = 3_000, maximum_ms: int = 10_000,
) -> tuple[VoiceReferenceWindow, ...]:
    """Return ranked authorized speech windows; no arbitrary media scan occurs."""
    if minimum_ms <= 0 or maximum_ms < minimum_ms:
        raise ValueError("Voice reference window bounds are invalid")
    candidates: list[VoiceReferenceWindow] = []
    for clip in clips:
        asset = assets.get(clip.asset_id)
        source_path = None if asset is None else _resolve_source_path(asset.source_file, data_root)
        if source_path is None or not source_path.is_file():
            continue
        segments = [item for item in clip.transcript_segments if item.start_ms >= clip.start_ms and item.end_ms <= clip.end_ms]
        for start in range(len(segments)):
            end = start
            text: list[str] = []
            while end < len(segments):
                segment = segments[end]
                if segment.end_ms - segments[start].start_ms > maximum_ms:
                    break
                text.append(segment.text.strip())
                duration = segment.end_ms - segments[start].start_ms
                if duration >= minimum_ms:
                    quality = float(clip.speech_quality) if clip.speech_quality is not None else 0.5
                    # Prefer a compact ~6s internally continuous window; score
                    # is only selection provenance, not a quality claim.
                    score = quality - abs(duration - 6_000) / 100_000
                    candidates.append(VoiceReferenceWindow(
                        clip_id=clip.id, asset_id=asset.id, start_ms=segments[start].start_ms,
                        end_ms=segment.end_ms, transcript=" ".join(text), source_file=str(source_path), score=score,
                    ))
                end += 1
    return tuple(sorted(candidates, key=lambda item: (-item.score, item.clip_id.hex, item.start_ms, item.end_ms)))


def pause_duration_ms(pause: NarrationPause | None) -> int:
    """Actual local composition settings, distinct from a semantic pause cue."""
    return {None: 0, NarrationPause.BRIEF: 180, NarrationPause.BEAT: 420, NarrationPause.LONG: 700}[pause]


def _sentence_end(text: str) -> bool:
    return bool(text.rstrip()) and text.rstrip()[-1] in "。！？!?；;"


def _resolve_source_path(source_file: str, data_root: str | Path | None) -> Path:
    path = Path(source_file)
    if path.is_absolute() or data_root is None:
        return path
    root = Path(data_root).resolve()
    return root.parent / path if path.parts and path.parts[0].casefold() == root.name.casefold() else root / path


__all__ = [
    "NarrationPerformanceUnit", "VoiceGenerationSpan", "VoicePerformancePlanningError", "VoicePerformanceRenderPlan",
    "VoicePerformanceComposer", "VoicePerformanceComposition", "VoiceReferenceWindow", "pause_duration_ms",
    "plan_voice_generation_spans", "plan_voice_performance_units", "select_voice_reference_windows",
]
