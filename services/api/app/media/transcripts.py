"""Provider-neutral timestamped transcript mapping for persisted Clips."""
from __future__ import annotations

from typing import Mapping, Protocol, Sequence, TypeVar
from uuid import UUID

from app.db import ClipRepository, Database
from app.domain.models import Clip
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
        seen: set[tuple[int, int, str]] = set()
        for segment in normalized:
            text = " ".join(segment.text.split())
            if not text or segment.end_ms <= clip.start_ms or segment.start_ms >= clip.end_ms:
                continue
            identity = (segment.start_ms, segment.end_ms, text)
            if identity not in seen:
                seen.add(identity)
                texts.append(text)
        transcript = " ".join(texts) if texts else None
        if transcript is not None and len(transcript) > 100_000:
            raise ValueError("mapped transcript exceeds Clip.transcript limit")
        result.append(clip.model_copy(update={"transcript": transcript}))
    return result


class TranscriptMapper:
    """Small service wrapper useful at the persistence boundary."""

    def map(self, clips: Sequence[ClipT], segments: Sequence[SegmentLike]) -> list[ClipT]:
        return map_transcript_to_clips(clips, segments)


class NoClipsForAsset(RuntimeError):
    """Raised when transcript persistence targets an asset without Clips."""


class ClipTranscriptPersistence:
    """Apply transcript mapping and Clip updates in one short SQLite transaction."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.repository = ClipRepository(db)

    def apply(self, asset_id: UUID, segments: Sequence[SegmentLike]) -> list[Clip]:
        self.db.connection.execute("BEGIN IMMEDIATE")
        try:
            clips = self.repository.list_by_asset(asset_id)
            if not clips:
                raise NoClipsForAsset(f"asset {asset_id} has no Clips")
            mapped = map_transcript_to_clips(clips, segments)
            for clip in mapped:
                self.repository.update(clip)
            self.db.connection.commit()
            return mapped
        except BaseException:
            self.db.connection.rollback()
            raise


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
