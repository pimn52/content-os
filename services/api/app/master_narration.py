"""Application service for composing QA-verified Voice takes into a master candidate."""
from __future__ import annotations

import tempfile
from pathlib import Path
from uuid import UUID

from app.db import AudioAssetRepository, JobRepository
from app.domain.models import AudioAsset, JobType, SourceKind
from app.media import AudioImportError, AudioImporter
from app.voice_takes import VerifiedVoiceTake, VoiceTakeComposer, VoiceTakeCompositionError


class MasterNarrationCompositionError(ValueError):
    pass


class MasterNarrationComposer:
    """Persist a QA-pending composed candidate, never a silently approved master."""

    def __init__(self, *, audios: AudioAssetRepository, jobs: JobRepository, importer: AudioImporter,
                 composer: VoiceTakeComposer, output_root: str | Path) -> None:
        self.audios, self.jobs, self.importer, self.composer = audios, jobs, importer, composer
        self.output_root = Path(output_root)

    def compose(self, project_id: UUID, take_audio_ids: list[UUID]) -> AudioAsset:
        if not take_audio_ids or len(take_audio_ids) > 100 or len(set(take_audio_ids)) != len(take_audio_ids):
            raise MasterNarrationCompositionError("MasterNarration composition requires 1–100 unique take audio IDs")
        takes = [self._take(project_id, audio_id, index) for index, audio_id in enumerate(take_audio_ids)]
        authorizations = {take.audio.authorization_reference for take in takes}
        if len(authorizations) != 1:
            raise MasterNarrationCompositionError("MasterNarration takes must share one authorization record")
        self.output_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="content-os-master-", dir=str(self.output_root)) as temporary:
            output = Path(temporary) / "master.wav"
            try:
                provisional = self.composer.compose(takes, output)
                imported = self.importer.import_path(
                    provisional.output_path, authorizations.pop(), source_kind=SourceKind.USER_ASSET,
                )
            except (VoiceTakeCompositionError, AudioImportError, FileNotFoundError, OSError) as exc:
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
                "expected_duration_ms": provisional.expected_duration_ms,
                "timing_state": "provisional_pending_master_voice_qa",
            },
        }
        candidate = imported.model_copy(update={
            "metadata": metadata,
            "transcript_segments": list(provisional.transcript_segments),
            "transcript_source": "voice-take-composition:provisional-pending-master-qa",
        })
        return self.audios.update(candidate)

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
