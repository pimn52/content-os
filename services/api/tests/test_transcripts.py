from dataclasses import dataclass
from uuid import uuid4

import pytest

from app.db import AssetRepository, ClipRepository, Database
from app.domain.models import Asset, Clip, RationalFps
from app.media.transcripts import ClipTranscriptPersistence, NoClipsForAsset, TranscriptMapper, TranscriptSegment, map_transcript_to_clips
from app.providers.asr import TranscriptionSegment


def _clips() -> list[Clip]:
    return [
        Clip(asset_id=uuid4(), start_ms=0, end_ms=1_000, asset_duration_ms=2_000),
        Clip(asset_id=uuid4(), start_ms=1_000, end_ms=2_000, asset_duration_ms=2_000),
    ]


def test_overlap_mapping_preserves_clip_and_segment_order():
    clips = _clips()
    segments = [
        TranscriptSegment(100, 400, " first   words "),
        TranscriptSegment(900, 1_100, "crosses boundary"),
        TranscriptSegment(1_200, 1_500, "last words"),
    ]
    mapped = map_transcript_to_clips(clips, segments)
    assert [clip.id for clip in mapped] == [clip.id for clip in clips]
    assert [clip.transcript for clip in mapped] == ["first words crosses boundary", "crosses boundary last words"]


def test_empty_and_duplicate_segments_are_stable_and_stale_text_is_cleared():
    clips = _clips()
    clips[1] = clips[1].model_copy(update={"transcript": "already present"})
    segments = [TranscriptSegment(0, 100, "  hello "), TranscriptSegment(0, 100, "hello"), TranscriptSegment(0, 100, "   ")]
    first = map_transcript_to_clips(clips, segments)
    second = map_transcript_to_clips(first, segments)
    assert first == second
    assert first[0].transcript == "hello"
    assert first[1].transcript is None


@dataclass
class ProviderSegment:
    start_ms: int
    end_ms: int
    text: str


def test_mapping_accepts_provider_attributes_and_mappings():
    clips = _clips()
    mapped = TranscriptMapper().map(clips, [ProviderSegment(10, 20, "object"), {"start_ms": 20, "end_ms": 30, "text": "mapping"}])
    assert mapped[0].transcript == "object mapping"
    assert TranscriptSegment is TranscriptionSegment


def test_invalid_segments_are_rejected():
    clips = _clips()
    with pytest.raises(ValueError):
        map_transcript_to_clips(clips, [{"start_ms": 5, "end_ms": 5, "text": "bad"}])


def test_transcripts_persist_repeatably_and_in_stable_order(tmp_path):
    db = Database(tmp_path / "transcripts.sqlite")
    try:
        source = tmp_path / "asset.mp4"
        source.write_bytes(b"asset")
        asset = Asset(source_file=str(source), content_hash="a" * 64, duration_ms=2_000, width=10, height=10, fps=RationalFps(numerator=25, denominator=1), authorization_reference="rights", imported_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc))
        AssetRepository(db).create(asset)
        clips = [Clip(asset_id=asset.id, start_ms=0, end_ms=1_000, asset_duration_ms=2_000), Clip(asset_id=asset.id, start_ms=1_000, end_ms=2_000, asset_duration_ms=2_000)]
        repository = ClipRepository(db)
        for clip in clips:
            repository.create(clip)
        segments = [TranscriptSegment(100, 1_100, "cross"), TranscriptSegment(1_200, 1_500, "second")]
        service = ClipTranscriptPersistence(db)
        first = service.apply(asset.id, segments)
        second = service.apply(asset.id, segments)
        assert [clip.id for clip in first] == [clip.id for clip in second]
        assert [clip.transcript for clip in second] == ["cross", "cross second"]
        assert repository.list_by_asset(asset.id) == second
    finally:
        db.close()


def test_transcript_persistence_rolls_back_all_updates_on_failure(tmp_path, monkeypatch):
    db = Database(tmp_path / "rollback.sqlite")
    try:
        source = tmp_path / "asset.mp4"
        source.write_bytes(b"asset")
        from datetime import datetime, timezone
        asset = Asset(source_file=str(source), content_hash="b" * 64, duration_ms=2_000, width=10, height=10, fps=RationalFps(numerator=25, denominator=1), authorization_reference="rights", imported_at=datetime.now(timezone.utc))
        AssetRepository(db).create(asset)
        repository = ClipRepository(db)
        clips = [Clip(asset_id=asset.id, start_ms=0, end_ms=1_000, asset_duration_ms=2_000), Clip(asset_id=asset.id, start_ms=1_000, end_ms=2_000, asset_duration_ms=2_000)]
        for clip in clips:
            repository.create(clip)
        original_update = repository.update
        calls = 0
        def fail_on_second(clip):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("injected update failure")
            return original_update(clip)
        service = ClipTranscriptPersistence(db)
        monkeypatch.setattr(service.repository, "update", fail_on_second)
        with pytest.raises(RuntimeError, match="injected"):
            service.apply(asset.id, [TranscriptSegment(0, 500, "new")])
        assert repository.list_by_asset(asset.id) == clips
    finally:
        db.close()


def test_transcript_persistence_requires_existing_clips(tmp_path):
    db = Database(tmp_path / "empty.sqlite")
    try:
        with pytest.raises(NoClipsForAsset):
            ClipTranscriptPersistence(db).apply(uuid4(), [])
    finally:
        db.close()
