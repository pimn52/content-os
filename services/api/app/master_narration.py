"""Application service for composing QA-verified Voice takes into a master candidate."""
from __future__ import annotations

import tempfile
import hashlib
from pathlib import Path
from uuid import UUID

from app.db import AudioAssetRepository, JobRepository
from app.domain.models import AudioAsset, JobType, SourceKind, TranscriptSegment
from app.media import AudioImportError, AudioImporter
from app.voice_takes import VerifiedVoiceTake, VoiceTakeComposer, VoiceTakeCompositionError
from app.voice_seam import VoiceSeamCompactionError, compact_measured_seam_quiet


class MasterNarrationCompositionError(ValueError):
    pass


class MasterNarrationComposer:
    """Persist a QA-pending composed candidate, never a silently approved master."""

    def __init__(self, *, audios: AudioAssetRepository, jobs: JobRepository, importer: AudioImporter,
                 composer: VoiceTakeComposer, output_root: str | Path) -> None:
        self.audios, self.jobs, self.importer, self.composer = audios, jobs, importer, composer
        self.output_root = Path(output_root)

    def compose(self, project_id: UUID, take_audio_ids: list[UUID], *, compact_quiet_seams: bool = False) -> AudioAsset:
        if not take_audio_ids or len(take_audio_ids) > 100 or len(set(take_audio_ids)) != len(take_audio_ids):
            raise MasterNarrationCompositionError("MasterNarration composition requires 1–100 unique take audio IDs")
        takes = [self._take(project_id, audio_id, index) for index, audio_id in enumerate(take_audio_ids)]
        authorizations = {take.audio.authorization_reference for take in takes}
        if len(authorizations) != 1:
            raise MasterNarrationCompositionError("MasterNarration takes must share one authorization record")
        self.output_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="content-os-master-", dir=str(self.output_root)) as temporary:
            output = Path(temporary) / "raw-master.wav"
            try:
                provisional = self.composer.compose(takes, output)
                source_to_import = provisional.output_path
                segments = list(provisional.transcript_segments)
                expected_duration_ms = provisional.expected_duration_ms
                seam_evidence = None
                if compact_quiet_seams:
                    boundaries = []
                    elapsed = 0
                    for take in takes[:-1]:
                        elapsed += take.audio.duration_ms
                        boundaries.append(elapsed)
                    seam_evidence = compact_measured_seam_quiet(
                        provisional.output_path, Path(temporary) / "compacted-master.wav", boundaries,
                    )
                    source_to_import = seam_evidence.output_path
                    expected_duration_ms = seam_evidence.final_duration_ms
                    segments = [TranscriptSegment(
                        start_ms=self._shift_after_cuts(segment.start_ms, seam_evidence.cuts),
                        end_ms=self._shift_after_cuts(segment.end_ms, seam_evidence.cuts),
                        text=segment.text,
                    ) for segment in segments]
                output_hash = hashlib.sha256(source_to_import.read_bytes()).hexdigest()
                if self.audios.get_by_content_hash(output_hash) is not None:
                    raise MasterNarrationCompositionError("identical Master audio is already registered; refusing to overwrite it")
                imported = self.importer.import_path(
                    source_to_import, authorizations.pop(), source_kind=SourceKind.USER_ASSET,
                )
                if "voice_generation" in imported.metadata:
                    raise MasterNarrationCompositionError("identical Master audio already has provenance; refusing to overwrite it")
                if seam_evidence is not None and imported.content_hash != seam_evidence.final_sha256:
                    raise MasterNarrationCompositionError("imported compacted Master does not match the measured output")
            except (VoiceTakeCompositionError, VoiceSeamCompactionError, AudioImportError, FileNotFoundError, OSError, ValueError) as exc:
                raise MasterNarrationCompositionError(str(exc)) from exc
        metadata = dict(imported.metadata)
        metadata["voice_generation"] = {
            "provider": "composed_voice_takes",
            "model": "provider_neutral_composition",
            "project_id": str(project_id),
            "target_text": " ".join(self._target_text(take.audio) for take in takes),
            "qa_state": "pending",
            "composition": {
                "source_take_audio_ids": list(provisional.source_asset_ids),
                "source_take_content_hashes": [take.audio.content_hash for take in takes],
                "expected_duration_ms": expected_duration_ms,
                "timing_state": "provisional_pending_master_voice_qa",
            },
        }
        if seam_evidence is not None:
            metadata["voice_generation"]["composition"]["measured_seam_compaction"] = {
                "method": "pcm16_mono_24khz_10ms_rms_below_minus45dbfs",
                "margin_ms_each_side": 60, "max_cut_ms": 300,
                "raw_sha256": seam_evidence.raw_sha256,
                "final_sha256": seam_evidence.final_sha256,
                "raw_duration_ms": seam_evidence.raw_duration_ms,
                "cuts": [{"seam_index": index, "start_ms": start, "end_ms": end}
                         for index, start, end in seam_evidence.cuts],
            }
        candidate = imported.model_copy(update={
            "metadata": metadata,
            "transcript_segments": segments,
            "transcript_source": "voice-take-composition:provisional-pending-master-qa",
        })
        return self.audios.update(candidate)

    @staticmethod
    def _shift_after_cuts(time_ms: int, cuts: tuple[tuple[int, int, int], ...]) -> int:
        return time_ms - sum(max(0, min(time_ms, end) - start) for _, start, end in cuts)

    def _take(self, project_id: UUID, audio_id: UUID, index: int) -> VerifiedVoiceTake:
        audio = self.audios.get(audio_id)
        generation = None if audio is None else audio.metadata.get("voice_generation")
        if audio is None or not isinstance(generation, dict) or generation.get("qa_state") != "verified":
            raise MasterNarrationCompositionError("every MasterNarration take must be a QA-verified generated Voice asset")
        job_id = generation.get("job_id")
        try:
            job = self.jobs.get(UUID(job_id)) if isinstance(job_id, str) else None
        except ValueError:
            job = None
        if job is None or job.type is not JobType.GENERATE_VOICE or job.project_id != project_id:
            raise MasterNarrationCompositionError("MasterNarration takes must be generated for the selected project")
        return VerifiedVoiceTake(scene_id=f"take-{index}", audio=audio)

    @staticmethod
    def _target_text(audio: AudioAsset) -> str:
        generation = audio.metadata.get("voice_generation")
        text = generation.get("target_text") if isinstance(generation, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise MasterNarrationCompositionError("every MasterNarration take requires persisted requested copy")
        return text.strip()


__all__ = ["MasterNarrationComposer", "MasterNarrationCompositionError"]
