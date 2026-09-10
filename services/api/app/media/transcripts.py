"""Provider-neutral timestamped transcript mapping for persisted Clips."""
from __future__ import annotations

from typing import Mapping, Protocol, Sequence, TypeVar
from uuid import UUID

from app.db import AudioAssetRepository, ClipRepository, Database
from app.domain.models import AudioAsset, Clip, TranscriptSegment as PersistedTranscriptSegment
from app.providers.asr import TranscriptionSegment


class TimestampedSegment(Protocol):
    start_ms: int
    end_ms: int
    text: str


TranscriptSegment = TranscriptionSegment


SegmentLike = TranscriptSegment | Mapping[str, object] | TimestampedSegment
ClipT = TypeVar("ClipT", bound=Clip)


def map_transcript_to_clips(clips: Sequence[ClipT], segments: Sequence[SegmentLike]) -> list[ClipT]:
    """Assign overlapping segment text to Clips while preserving clip order.

    A segment that crosses a clip boundary is assigned to every overlapping
    clip. Text is normalized and exact duplicate ``(interval, text)`` entries
    are ignored. This call represents one authoritative ASR result, so Clips
    with no matching non-empty segment are explicitly cleared; rebuilding from
    the same inputs is therefore stable and never appends duplicate text.
    """
    normalized = [_coerce_segment(segment) for segment in segments]
    result: list[ClipT] = []
    for clip in clips:
        texts: list[str] = []
        persisted_segments: list[PersistedTranscriptSegment] = []
        seen: set[tuple[int, int, str]] = set()
        for segment in normalized:
            text = " ".join(segment.text.split())
            if not text or segment.end_ms <= clip.start_ms or segment.start_ms >= clip.end_ms:
                continue
            identity = (segment.start_ms, segment.end_ms, text)
            if identity not in seen:
                seen.add(identity)
                texts.append(text)
                persisted_segments.append(PersistedTranscriptSegment(
                    start_ms=segment.start_ms,
                    end_ms=segment.end_ms,
                    text=text,
                ))
        transcript = " ".join(texts) if texts else None
        if transcript is not None and len(transcript) > 100_000:
            raise ValueError("mapped transcript exceeds Clip.transcript limit")
        result.append(clip.model_copy(update={"transcript": transcript, "transcript_segments": persisted_segments, "transcript_source": None}))
    return result


class TranscriptMapper:
    """Small service wrapper useful at the persistence boundary."""

    def map(self, clips: Sequence[ClipT], segments: Sequence[SegmentLike]) -> list[ClipT]:
        return map_transcript_to_clips(clips, segments)


class NoClipsForAsset(RuntimeError):
    """Raised when transcript persistence targets an asset without Clips."""


class AudioAssetNotFound(RuntimeError):
    """Raised when transcript persistence targets an unknown audio asset."""


class ClipTranscriptPersistence:
    """Apply transcript mapping and Clip updates in one short SQLite transaction."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.repository = ClipRepository(db)

    def apply(self, asset_id: UUID, segments: Sequence[SegmentLike], *, source_reference: str | None = None) -> list[Clip]:
        if source_reference is not None and (not isinstance(source_reference, str) or not source_reference.strip()):
            raise ValueError("transcript source_reference must be a non-empty string")
        if source_reference is not None and len(source_reference) > 500:
            raise ValueError("transcript source_reference is too long")
        self.db.connection.execute("BEGIN IMMEDIATE")
        try:
            clips = self.repository.list_by_asset(asset_id)
            if not clips:
                raise NoClipsForAsset(f"asset {asset_id} has no Clips")
            mapped = map_transcript_to_clips(clips, segments)
            if source_reference is not None:
                mapped = [clip.model_copy(update={"transcript_source": source_reference}) for clip in mapped]
            for clip in mapped:
                self.repository.update(clip)
            self.db.connection.commit()
            return mapped
        except BaseException:
            self.db.connection.rollback()
            raise


class AudioTranscriptPersistence:
    """Persist user-provided timestamped transcript intervals on an AudioAsset."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.repository = AudioAssetRepository(db)

    def apply(self, audio_id: UUID, segments: Sequence[SegmentLike], *, source_reference: str) -> AudioAsset:
        if not isinstance(source_reference, str) or not source_reference.strip():
            raise ValueError("transcript source_reference must be a non-empty string")
        if len(source_reference) > 500:
            raise ValueError("transcript source_reference is too long")
        audio = self.repository.get(audio_id)
        if audio is None:
            raise AudioAssetNotFound(f"audio asset {audio_id} not found")
        persisted = [PersistedTranscriptSegment(
            start_ms=segment.start_ms, end_ms=segment.end_ms, text=segment.text,
        ) for segment in _normalize_segments(segments)]
        if any(segment.end_ms > audio.duration_ms for segment in persisted):
            raise ValueError("transcript interval exceeds audio duration")
        updated = audio.model_copy(update={
            "transcript_segments": persisted,
            "transcript_source": source_reference.strip(),
        })
        with self.db.transaction():
            self.repository.update(updated)
        return updated


def _coerce_segment(segment: SegmentLike) -> TranscriptSegment:
    if isinstance(segment, TranscriptSegment):
        return segment
    if isinstance(segment, Mapping):
        start = segment.get("start_ms")
        end = segment.get("end_ms")
        text = segment.get("text")
    else:
        start = getattr(segment, "start_ms", None)
        end = getattr(segment, "end_ms", None)
        text = getattr(segment, "text", None)
    if not isinstance(start, int) or isinstance(start, bool) or not isinstance(end, int) or isinstance(end, bool):
        raise ValueError("transcript segment timestamps must be integers")
    if not isinstance(text, str):
        raise TypeError("transcript text must be a string")
    return TranscriptSegment(start, end, text)


def _normalize_segments(segments: Sequence[SegmentLike]) -> list[TranscriptSegment]:
    return sorted(
        {_coerce_segment(segment) for segment in segments},
        key=lambda segment: (segment.start_ms, segment.end_ms, segment.text),
    )
