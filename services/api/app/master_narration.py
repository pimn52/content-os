"""Application service for composing QA-verified Voice takes into a master candidate."""
from __future__ import annotations

import tempfile
import hashlib
import json
from pathlib import Path
from uuid import UUID

from app.db import AudioAssetRepository, JobRepository
from app.domain.models import (
    AudioAsset,
    JobStatus,
    JobType,
    SourceKind,
    TranscriptSegment,
    VoiceGenerationJobPayload,
    VoiceReferenceWindowSelection,
)
from app.media import AudioImportError, AudioImporter
from app.voice_takes import VerifiedVoiceTake, VoiceTakeComposer, VoiceTakeCompositionError
from app.voice_seam import VoiceSeamCompactionError, compact_measured_seam_quiet, take_boundary_kinds
from app.voice_qa import voice_copy_adjudication_is_valid


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
        if len({self._voice_identity(take.audio) for take in takes}) != 1:
            raise MasterNarrationCompositionError("MasterNarration takes must use one Voice profile, provider, model, and authorized reference")
        self.output_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="content-os-master-", dir=str(self.output_root)) as temporary:
            output = Path(temporary) / "raw-master.wav"
            try:
                provisional = self.composer.compose(takes, output)
                source_to_import = provisional.output_path
                segments = list(provisional.transcript_segments)
                expected_duration_ms = provisional.expected_duration_ms
                seam_evidence = None
                if compact_quiet_seams and len(takes) > 1:
                    boundaries = []
                    elapsed = 0
                    for take in takes[:-1]:
                        elapsed += take.audio.duration_ms
                        boundaries.append(elapsed)
                    seam_evidence = compact_measured_seam_quiet(
                        provisional.output_path, Path(temporary) / "compacted-master.wav", boundaries,
                        boundary_kinds=take_boundary_kinds([self._target_text(take.audio) for take in takes]),
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
            "voice_profile_id": takes[0].audio.metadata["voice_generation"]["voice_profile_id"],
            "target_text": " ".join(self._target_text(take.audio) for take in takes),
            "qa_state": "pending",
            "composition": {
                "source_take_audio_ids": list(provisional.source_asset_ids),
                "source_take_content_hashes": [take.audio.content_hash for take in takes],
                "copy_adjudicated_source_take_audio_ids": [
                    str(take.audio.id) for take in takes
                    if voice_copy_adjudication_is_valid(take.audio, project_id=project_id)
                ],
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
                "boundary_kinds": [kind.value for kind in take_boundary_kinds(
                    [self._target_text(take.audio) for take in takes])],
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
        if audio is None or not isinstance(generation, dict) or not (
            generation.get("qa_state") == "verified"
            or voice_copy_adjudication_is_valid(audio, project_id=project_id)
        ):
            raise MasterNarrationCompositionError("every MasterNarration take must have verified QA or a valid owner copy adjudication")
        job_id = generation.get("job_id")
        try:
            job = self.jobs.get(UUID(job_id)) if isinstance(job_id, str) else None
        except ValueError:
            job = None
        if (job is None or job.type is not JobType.GENERATE_VOICE or job.status is not JobStatus.COMPLETED
            or not isinstance(job.payload, VoiceGenerationJobPayload) or job.project_id != project_id):
            raise MasterNarrationCompositionError("MasterNarration takes must be generated for the selected project")
        try:
            recorded_reference = VoiceReferenceWindowSelection.model_validate(generation.get("reference_window"))
        except ValueError as exc:
            raise MasterNarrationCompositionError("MasterNarration takes require the exact generated authorized reference") from exc
        if (
            generation.get("job_id") != str(job.id)
            or generation.get("project_id") != str(project_id)
            or generation.get("target_text") != job.payload.text
            or generation.get("voice_profile_id") != str(job.payload.voice_profile_id)
            or audio.authorization_reference != job.payload.authorization_reference
            or job.payload.reference_window is None
            or recorded_reference != job.payload.reference_window
            or (job.payload.expected_provider is not None and generation.get("provider") != job.payload.expected_provider)
            or (job.payload.expected_model is not None and generation.get("model") != job.payload.expected_model)
        ):
            raise MasterNarrationCompositionError("MasterNarration take provenance does not match its completed Voice Job")
        return VerifiedVoiceTake(scene_id=f"take-{index}", audio=audio)

    @staticmethod
    def _voice_identity(audio: AudioAsset) -> tuple[str, str, str, str]:
        generation = audio.metadata.get("voice_generation")
        if not isinstance(generation, dict):
            raise MasterNarrationCompositionError("MasterNarration take Voice identity is unavailable")
        profile_id = generation.get("voice_profile_id")
        provider = generation.get("provider")
        model = generation.get("model")
        reference = generation.get("reference_window")
        if not all(isinstance(value, str) and value.strip() for value in (profile_id, provider, model)) or not isinstance(reference, dict):
            raise MasterNarrationCompositionError("MasterNarration take Voice identity is incomplete")
        selected = VoiceReferenceWindowSelection.model_validate(reference)
        # A confirmed source may provide different verified windows (V50 did);
        # each exact window is checked against its own Job above.
        authorized_source = {
            "clip_id": str(selected.clip_id), "asset_id": str(selected.asset_id),
            "source_content_hash": selected.source_content_hash,
            "source_timebase": selected.source_timebase, "asset_content_hash": selected.asset_content_hash,
        }
        return profile_id, provider, model, json.dumps(authorized_source, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _target_text(audio: AudioAsset) -> str:
        generation = audio.metadata.get("voice_generation")
        text = generation.get("target_text") if isinstance(generation, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise MasterNarrationCompositionError("every MasterNarration take requires persisted requested copy")
        return text.strip()


__all__ = ["MasterNarrationComposer", "MasterNarrationCompositionError"]
