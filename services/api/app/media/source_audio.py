"""Create a traceable narration asset from already-authorized source video audio."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import UUID

from app.db import AssetRepository, AudioAssetRepository, ClipRepository, Database, ProjectRepository
from app.domain.models import Asset, AudioAsset, TranscriptSegment
from app.media.audio_importer import AudioImporter
from app.media.extraction import ExtractionError, MediaExtractor
from app.media.ffprobe import FFProbeAdapter, ProbeError
from app.media.local_paths import existing_local_media_file


class SourceAudioDerivationError(ValueError):
    """Source audio could not be derived from the selected authorized media."""


class SourceAudioDerivationService:
    def __init__(
        self, db: Database, data_root: str | Path, *,
        ffmpeg_command: str | tuple[str, ...] = "ffmpeg",
        ffprobe_command: str | tuple[str, ...] = "ffprobe",
    ) -> None:
        self.db = db
        self.root = Path(data_root).resolve()
        self.ffmpeg_command = ffmpeg_command
        self.ffprobe_command = ffprobe_command

    def derive(
        self, project_id: UUID, asset_id: UUID, clip_id: UUID, start_ms: int, end_ms: int,
    ) -> AudioAsset:
        project = ProjectRepository(self.db).get(project_id)
        asset = AssetRepository(self.db).get(asset_id)
        clip = ClipRepository(self.db).get(clip_id)
        if project is None:
            raise SourceAudioDerivationError("source_audio_project_missing")
        if asset is None or clip is None or clip.asset_id != asset.id:
            raise SourceAudioDerivationError("source_audio_clip_asset_mismatch")
        if not asset.has_audio:
            raise SourceAudioDerivationError("source_asset_has_no_audio")
        if not asset.authorization_reference.strip():
            raise SourceAudioDerivationError("source_asset_authorization_missing")
        if (isinstance(start_ms, bool) or not isinstance(start_ms, int)
            or isinstance(end_ms, bool) or not isinstance(end_ms, int)
            or start_ms < clip.start_ms or end_ms > clip.end_ms or end_ms <= start_ms):
            raise SourceAudioDerivationError("source_audio_interval_outside_selected_clip")

        segments = _source_segments(clip, start_ms, end_ms)
        source = existing_local_media_file(asset.source_file, self.root)
        _verify_asset_bytes(asset, source)
        resolved_asset = asset.model_copy(update={"source_file": str(source)})
        extractor = MediaExtractor(self.root, self.ffmpeg_command, sample_rate=48_000, channels=2)
        try:
            extracted = extractor.extract_audio_interval(resolved_asset, start_ms, end_ms)
            probe = FFProbeAdapter(self.ffprobe_command)
            extracted_metadata = probe.probe_audio(extracted.path)
            shifted = [TranscriptSegment(
                start_ms=item.start_ms - start_ms,
                end_ms=item.end_ms - start_ms,
                text=item.text,
            ) for item in segments]
            if any(item.end_ms > extracted_metadata.duration_ms for item in shifted):
                raise SourceAudioDerivationError("source_audio_transcript_exceeds_extracted_duration")
            importer = AudioImporter(
                self.db, self.root, probe,
            )
            audio = importer.import_path(
                extracted.path, asset.authorization_reference, language="zh-CN",
            )
            if audio.duration_ms != extracted_metadata.duration_ms:
                raise SourceAudioDerivationError("source_audio_probe_duration_changed")
        except (ExtractionError, OSError, ProbeError, ValueError) as exc:
            if isinstance(exc, SourceAudioDerivationError):
                raise
            raise SourceAudioDerivationError("source_audio_extraction_failed") from exc

        source_transcript_source = clip.transcript_source or "persisted-transcript"
        transcript_source = (
            f"source-asset:{asset.id}:{asset.content_hash}:clip:{clip.id}:"
            f"{source_transcript_source}:{start_ms}-{end_ms}"
        )[:500] if shifted else None
        binding: dict[str, Any] = {
            "project_id": str(project_id),
            "source_asset_id": str(asset.id),
            "source_asset_sha256": asset.content_hash,
            "source_clip_id": str(clip.id),
            "source_start_ms": start_ms,
            "source_end_ms": end_ms,
            "authorization_reference": asset.authorization_reference,
            "transcript_source": transcript_source,
            "source_transcript_source": source_transcript_source,
            "transcript_sha256": _digest([item.model_dump(mode="json") for item in shifted]),
            "source_transcript_sha256": _digest([
                item.model_dump(mode="json") for item in clip.transcript_segments
            ]),
        }
        binding["binding_sha256"] = _digest(binding)
        metadata = dict(audio.metadata)
        source_audio = metadata.get("source_audio")
        if source_audio is None:
            source_audio = {"schema_version": 1, "bindings": []}
        if (not isinstance(source_audio, dict) or source_audio.get("schema_version") != 1
            or not isinstance(source_audio.get("bindings"), list)):
            raise SourceAudioDerivationError("source_audio_existing_binding_invalid")
        bindings = [item for item in source_audio["bindings"] if isinstance(item, dict)]
        previous = next((item for item in bindings if (
            item.get("project_id") == str(project_id)
            and item.get("source_asset_id") == str(asset.id)
            and item.get("source_start_ms") == start_ms
            and item.get("source_end_ms") == end_ms
        )), None)
        if previous is not None:
            if previous.get("binding_sha256") != binding["binding_sha256"]:
                raise SourceAudioDerivationError("source_audio_binding_conflict")
            return audio
        if source_audio.get("bindings") and audio.transcript_segments and binding["transcript_sha256"] != _digest([
            item.model_dump(mode="json") for item in audio.transcript_segments
        ]):
            raise SourceAudioDerivationError("source_audio_transcript_conflict")
        bindings.append(binding)
        metadata["source_audio"] = {"schema_version": 1, "bindings": bindings}
        updated = audio.model_copy(update={
            "metadata": metadata,
            "transcript_segments": shifted,
            "transcript_source": transcript_source,
        })
        with self.db.transaction():
            AudioAssetRepository(self.db).update(updated)
        return updated


def admitted_source_audio_binding(
    db: Database, audio: AudioAsset, project_id: UUID, *, require_transcript: bool = True,
) -> dict[str, Any] | None:
    """Return a source-audio binding only while its project and source evidence still match."""
    source_audio = audio.metadata.get("source_audio")
    if not isinstance(source_audio, dict) or source_audio.get("schema_version") != 1:
        return None
    bindings = source_audio.get("bindings")
    if not isinstance(bindings, list):
        return None
    for binding in bindings:
        if not isinstance(binding, dict) or binding.get("project_id") != str(project_id):
            continue
        content = {key: value for key, value in binding.items() if key != "binding_sha256"}
        if binding.get("binding_sha256") != _digest(content):
            continue
        try:
            source_asset = AssetRepository(db).get(UUID(binding.get("source_asset_id", "")))
            source_clip = ClipRepository(db).get(UUID(binding.get("source_clip_id", "")))
            start, end = binding.get("source_start_ms"), binding.get("source_end_ms")
        except (ValueError, TypeError, AttributeError):
            continue
        if (not isinstance(start, int) or isinstance(start, bool)
            or not isinstance(end, int) or isinstance(end, bool) or end <= start
            or source_asset is None or source_clip is None or source_clip.asset_id != source_asset.id
            or source_asset.content_hash != binding.get("source_asset_sha256")
            or source_asset.authorization_reference != binding.get("authorization_reference")
            or audio.authorization_reference != source_asset.authorization_reference
            or source_clip.start_ms > start or source_clip.end_ms < end
            or binding.get("transcript_source") != audio.transcript_source
            or binding.get("transcript_sha256") != _digest([
                item.model_dump(mode="json") for item in audio.transcript_segments
            ])
            or require_transcript and not audio.transcript_segments
            or any(item.start_ms < 0 or item.end_ms > audio.duration_ms for item in audio.transcript_segments)):
            continue
        return binding
    return None


def _source_segments(clip, start_ms: int, end_ms: int) -> list[TranscriptSegment]:
    values = [item for item in clip.transcript_segments if item.start_ms >= start_ms and item.end_ms <= end_ms]
    if not values or any(item.end_ms <= item.start_ms or not item.text.strip() for item in values):
        return []
    values.sort(key=lambda item: (item.start_ms, item.end_ms, item.text))
    if any(current.start_ms < previous.end_ms for previous, current in zip(values, values[1:])):
        return []
    if values[0].start_ms - start_ms > 1_000 or end_ms - values[-1].end_ms > 1_000:
        return []
    return values


def _verify_asset_bytes(asset: Asset, path: Path) -> None:
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != asset.content_hash:
        raise SourceAudioDerivationError("source_asset_bytes_changed")


def _digest(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
