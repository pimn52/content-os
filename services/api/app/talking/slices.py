"""Provider-neutral planning of short Talking audio from a verified master."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from uuid import UUID

from app.domain.models import AudioAsset, Clip, TranscriptSegment


class TalkingSlicePlanningError(ValueError):
    """The requested short Talking slice cannot be derived safely."""


class TalkingSliceExtractionError(RuntimeError):
    """The planned bounded audio could not be materialized locally."""


@dataclass(frozen=True)
class TalkingAudioSlicePlan:
    """A whole-speech interval to extract from one verified master narration.

    ``start_ms`` and ``end_ms`` are master-relative. A later extraction layer
    must preserve this provenance and supply only this bounded interval to a
    short-capability Talking provider, never the complete master by default.
    """

    master_audio_id: UUID
    start_ms: int
    end_ms: int
    text: str
    transcript_source: str
    start_segment_index: int
    end_segment_index: int

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


@dataclass(frozen=True)
class TalkingAudioSliceSeriesPlan:
    """An ordered, exhaustive set of provider-bounded slices from one master.

    The series is a planning artifact only. Its boundaries make the visual
    continuity handoff explicit for a later executor; it neither produces nor
    stitches video.
    """

    master_audio_id: UUID
    max_duration_ms: int
    slices: tuple[TalkingAudioSlicePlan, ...]

    @property
    def continuity_boundaries_ms(self) -> tuple[tuple[int, int], ...]:
        """Master-relative gaps or joins that a later visual layer must handle."""

        return tuple(
            (previous.end_ms, following.start_ms)
            for previous, following in zip(self.slices, self.slices[1:])
        )


@dataclass(frozen=True)
class TalkingReferenceWindowPlan:
    """One source-forward reference window for an audio slice.

    ``delivery`` remains the narration slice. ``end_ms`` may extend beyond
    it only for provider-declared frame-alignment context; that context is
    input-only and is never part of the delivered video/audio interval.
    """

    reference_clip_id: UUID
    master_slice_start_ms: int
    master_slice_end_ms: int
    start_ms: int
    end_ms: int


def plan_source_forward_reference_windows(
    reference_clip: Clip,
    audio_series: TalkingAudioSliceSeriesPlan,
    *,
    frame_alignment_context_ms: int,
) -> tuple[TalkingReferenceWindowPlan, ...]:
    """Map a complete audio series to one forward-only continuous source run.

    The same offset in the master narration always maps to the same offset in
    the selected reference Clip. This prevents every independent inference
    from silently restarting at the source Clip's first frame. The small
    adapter-declared context is retained solely for model frame alignment and
    may overlap the next source window; delivery still ends at the audio cut.
    """

    if not isinstance(reference_clip, Clip):
        raise TalkingSlicePlanningError("Talking reference-window planning requires a typed reference Clip")
    if not isinstance(audio_series, TalkingAudioSliceSeriesPlan) or not audio_series.slices:
        raise TalkingSlicePlanningError("Talking reference-window planning requires one or more audio slices")
    if isinstance(frame_alignment_context_ms, bool) or not isinstance(frame_alignment_context_ms, int) or frame_alignment_context_ms < 0:
        raise TalkingSlicePlanningError("Talking reference frame-alignment context must be a non-negative integer")
    origin_ms = audio_series.slices[0].start_ms
    values: list[TalkingReferenceWindowPlan] = []
    for audio_slice in audio_series.slices:
        start_ms = reference_clip.start_ms + audio_slice.start_ms - origin_ms
        end_ms = start_ms + audio_slice.duration_ms + frame_alignment_context_ms
        if end_ms > reference_clip.end_ms:
            raise TalkingSlicePlanningError(
                "Talking reference Clip is too short for the complete source-forward series and its frame-alignment context"
            )
        values.append(TalkingReferenceWindowPlan(
            reference_clip_id=reference_clip.id,
            master_slice_start_ms=audio_slice.start_ms,
            master_slice_end_ms=audio_slice.end_ms,
            start_ms=start_ms,
            end_ms=end_ms,
        ))
    return tuple(values)


def plan_talking_audio_slice(
    master: AudioAsset,
    *,
    start_segment_index: int,
    end_segment_index: int,
    max_duration_ms: int,
) -> TalkingAudioSlicePlan:
    """Plan one bounded slice from complete adjacent verified intervals.

    Indices are zero-based and ``end_segment_index`` is exclusive. Speech
    boundaries never come from caller-supplied milliseconds, preventing
    untraceable cuts inside a phoneme.
    """

    _require_verified_master(master)
    if isinstance(max_duration_ms, bool) or not isinstance(max_duration_ms, int) or max_duration_ms < 1:
        raise TalkingSlicePlanningError("Talking slice maximum duration must be a positive integer")
    if (
        isinstance(start_segment_index, bool)
        or isinstance(end_segment_index, bool)
        or not isinstance(start_segment_index, int)
        or not isinstance(end_segment_index, int)
    ):
        raise TalkingSlicePlanningError("Talking slice transcript indices must be integers")
    segments = tuple(master.transcript_segments)
    if start_segment_index < 0 or end_segment_index <= start_segment_index or end_segment_index > len(segments):
        raise TalkingSlicePlanningError("Talking slice transcript interval is out of range")
    _require_ordered_intervals(segments, master.duration_ms)
    selected = segments[start_segment_index:end_segment_index]
    start_ms, end_ms = selected[0].start_ms, selected[-1].end_ms
    duration_ms = end_ms - start_ms
    if duration_ms > max_duration_ms:
        raise TalkingSlicePlanningError(
            f"Talking slice duration {duration_ms}ms exceeds provider-scoped maximum {max_duration_ms}ms"
        )
    return TalkingAudioSlicePlan(
        master_audio_id=master.id,
        start_ms=start_ms,
        end_ms=end_ms,
        text=" ".join(segment.text for segment in selected),
        transcript_source=master.transcript_source or "",
        start_segment_index=start_segment_index,
        end_segment_index=end_segment_index,
    )


def plan_talking_audio_slice_series(
    master: AudioAsset,
    *,
    max_duration_ms: int | None,
) -> TalkingAudioSliceSeriesPlan:
    """Greedily partition verified transcript sentences into complete slices.

    The maximum must already have been resolved from concrete local capability
    evidence. Keeping resolution outside this provider-neutral planner prevents
    an arbitrary caller value from becoming an automatic routing decision.
    """

    if max_duration_ms is None:
        raise TalkingSlicePlanningError("Talking slice series requires a locally verified maximum duration")
    _require_verified_master(master)
    if isinstance(max_duration_ms, bool) or not isinstance(max_duration_ms, int) or max_duration_ms < 1:
        raise TalkingSlicePlanningError("Talking slice maximum duration must be a positive integer")
    segments = tuple(master.transcript_segments)
    _require_ordered_intervals(segments, master.duration_ms)

    plans: list[TalkingAudioSlicePlan] = []
    start_index = 0
    end_index = 1
    while start_index < len(segments):
        candidate = plan_talking_audio_slice(
            master,
            start_segment_index=start_index,
            end_segment_index=end_index,
            max_duration_ms=max_duration_ms,
        )
        if end_index == len(segments):
            plans.append(candidate)
            break
        next_end_index = end_index + 1
        try:
            plan_talking_audio_slice(
                master,
                start_segment_index=start_index,
                end_segment_index=next_end_index,
                max_duration_ms=max_duration_ms,
            )
        except TalkingSlicePlanningError:
            plans.append(candidate)
            start_index = end_index
            end_index = start_index + 1
        else:
            end_index = next_end_index

    return TalkingAudioSliceSeriesPlan(
        master_audio_id=master.id,
        max_duration_ms=max_duration_ms,
        slices=tuple(plans),
    )


def extract_talking_audio_slice(
    master: AudioAsset,
    plan: TalkingAudioSlicePlan,
    output_path: str | Path,
    *,
    ffmpeg_command: str | Path = "ffmpeg",
    timeout_seconds: float = 120.0,
) -> AudioAsset:
    """Materialize a plan as local PCM without touching the master asset.

    The returned transient AudioAsset deliberately retains generated-Voice QA
    state and rebases only the already-verified transcript intervals. It is
    suitable as a provider input; it is not imported or promoted as a new
    master narration.
    """

    _require_verified_master(master)
    if not isinstance(plan, TalkingAudioSlicePlan) or plan.master_audio_id != master.id:
        raise TalkingSliceExtractionError("Talking slice plan does not belong to the supplied master narration")
    if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
        raise TalkingSliceExtractionError("Talking slice extraction timeout must be positive")
    output = Path(output_path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(ffmpeg_command), "-y", "-ss", f"{plan.start_ms / 1000:.3f}", "-i", master.source_file,
        "-t", f"{plan.duration_ms / 1000:.3f}", "-vn", "-ac", str(master.channels), "-ar", str(master.sample_rate),
        "-c:a", "pcm_s16le", str(output),
    ]
    try:
        completed = subprocess.run(command, shell=False, capture_output=True, text=True, timeout=float(timeout_seconds), check=False)
    except FileNotFoundError as exc:
        raise TalkingSliceExtractionError("Talking slice FFmpeg executable is unavailable") from exc
    except subprocess.TimeoutExpired as exc:
        raise TalkingSliceExtractionError("Talking slice extraction timed out") from exc
    if completed.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
        raise TalkingSliceExtractionError("Talking slice extraction failed")
    segments = [
        TranscriptSegment(
            start_ms=segment.start_ms - plan.start_ms,
            end_ms=segment.end_ms - plan.start_ms,
            text=segment.text,
        )
        for segment in master.transcript_segments[plan.start_segment_index:plan.end_segment_index]
    ]
    metadata = dict(master.metadata)
    metadata["talking_slice"] = {
        "master_audio_id": str(master.id),
        "start_ms": plan.start_ms,
        "end_ms": plan.end_ms,
        "transcript_source": plan.transcript_source,
    }
    return master.model_copy(update={
        "source_file": str(output),
        "duration_ms": plan.duration_ms,
        "metadata": metadata,
        "transcript_segments": segments,
    })


def _require_verified_master(master: AudioAsset) -> None:
    if not isinstance(master, AudioAsset):
        raise TalkingSlicePlanningError("Talking slice requires a typed master narration audio asset")
    generation = master.metadata.get("voice_generation")
    if not isinstance(generation, dict) or generation.get("qa_state") != "verified":
        raise TalkingSlicePlanningError("Talking slice requires QA-verified generated master narration")
    if not master.transcript_source or not master.transcript_segments:
        raise TalkingSlicePlanningError("Talking slice requires persisted verified master transcript timing")


def _require_ordered_intervals(segments: tuple[TranscriptSegment, ...], duration_ms: int) -> None:
    previous_end = 0
    for segment in segments:
        if segment.start_ms < previous_end or segment.end_ms > duration_ms:
            raise TalkingSlicePlanningError("Talking slice master transcript intervals are not ordered within audio duration")
        previous_end = segment.end_ms


__all__ = ["TalkingAudioSlicePlan", "TalkingAudioSliceSeriesPlan", "TalkingReferenceWindowPlan", "TalkingSliceExtractionError", "TalkingSlicePlanningError", "extract_talking_audio_slice", "plan_source_forward_reference_windows", "plan_talking_audio_slice", "plan_talking_audio_slice_series"]
