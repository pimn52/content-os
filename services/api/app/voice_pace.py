"""A narrow local pace-candidate worker, separate from Voice provider semantics."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import subprocess
import tempfile
import wave
from uuid import UUID, uuid4

from app.db import AudioAssetRepository, JobRepository
from app.domain.models import Job, JobStatus, JobType, VoicePaceCandidateJobPayload, VoiceQaJobPayload
from app.jobs.runner import JobExecutionError
from app.media.audio_importer import AudioImportError, AudioImporter
from app.voice_boundary_alignment import voice_asset_project_id
from app.voice_observation import VoiceObservationError, _resolve_source_path

GENTLE_SLOWER_TEMPO = 0.94


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class VoicePaceCandidateJobHandler:
    """Create one derived QA-pending asset and enqueue normal independent QA."""

    def __init__(
        self, audios: AudioAssetRepository, jobs: JobRepository,
        importer: AudioImporter, data_root: str | Path, ffmpeg_command: str | Path,
    ) -> None:
        self._audios = audios
        self._jobs = jobs
        self._importer = importer
        self._data_root = Path(data_root).resolve()
        self._ffmpeg = str(ffmpeg_command)

    def __call__(self, job: Job) -> None:
        if job.status is not JobStatus.RUNNING or job.type is not JobType.CREATE_VOICE_PACE_CANDIDATE or not isinstance(job.payload, VoicePaceCandidateJobPayload):
            raise JobExecutionError("voice_pace_job_invalid", "voice pace job must be claimed with its typed payload", retryable=False)
        source = self._audios.get(job.payload.narration_audio_id)
        if source is None or voice_asset_project_id(source, self._jobs) != job.project_id:
            raise JobExecutionError("voice_pace_source_unavailable", "source narration is unavailable in this project", retryable=False)
        generation = source.metadata.get("voice_generation")
        qa = generation.get("qa") if isinstance(generation, dict) else None
        copy = generation.get("target_text") if isinstance(generation, dict) else None
        if (
            not isinstance(generation, dict) or generation.get("qa_state") != "verified"
            or not isinstance(qa, dict) or qa.get("qa_state") != "verified"
            or not isinstance(copy, str) or not copy.strip()
            or generation.get("provider") in {"content_os_local_pace", "derived_audio_tempo_evaluation"}
            or "pace_candidate" in generation
            or not source.authorization_reference
        ):
            raise JobExecutionError("voice_pace_source_ineligible", "pace candidate requires an authorized, independently QA-verified original narration", retryable=False)
        if source.content_hash != job.payload.source_sha256:
            raise JobExecutionError("voice_pace_source_changed", "source narration hash changed after request", retryable=False)
        try:
            source_path = _resolve_source_path(source.source_file, self._data_root)
            if not source_path.is_file() or _sha256(source_path) != source.content_hash:
                raise OSError("source bytes unavailable or changed")
        except (VoiceObservationError, OSError):
            raise JobExecutionError("voice_pace_source_bytes_invalid", "source narration bytes are unavailable or changed", retryable=False) from None

        output_root = self._data_root / "generated" / "voice-pace"
        output_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f"{job.id}-", dir=output_root) as temporary:
            target = Path(temporary) / "candidate.wav"
            try:
                completed = subprocess.run(
                    [self._ffmpeg, "-nostdin", "-n", "-i", str(source_path),
                     "-af", f"atempo={GENTLE_SLOWER_TEMPO:.2f}", "-ar", "24000", "-ac", "1",
                     "-c:a", "pcm_s16le", str(target)],
                    capture_output=True, timeout=120, check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                raise JobExecutionError("voice_pace_transform_unavailable", "local pace transform could not complete", retryable=True) from None
            if completed.returncode != 0 or not target.is_file():
                raise JobExecutionError("voice_pace_transform_failed", "local pace transform did not produce audio", retryable=False)
            try:
                if _sha256(source_path) != source.content_hash:
                    raise OSError("source bytes changed during transform")
            except OSError:
                raise JobExecutionError("voice_pace_source_bytes_invalid", "source narration bytes changed during transform", retryable=False) from None
            try:
                with wave.open(str(target), "rb") as audio:
                    if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth(), audio.getcomptype()) != (24000, 1, 2, "NONE"):
                        raise ValueError("unexpected PCM output")
                    frames = audio.getnframes()
                expected_ms = source.duration_ms / GENTLE_SLOWER_TEMPO
                if abs(frames / 24 - expected_ms) > 100:
                    raise ValueError("unexpected output duration")
                candidate_hash = _sha256(target)
            except (OSError, EOFError, ValueError, wave.Error):
                raise JobExecutionError("voice_pace_output_invalid", "pace candidate PCM or duration is invalid", retryable=False) from None
            try:
                candidate = self._importer.import_path(target, source.authorization_reference, language=source.language)
            except (AudioImportError, OSError, ValueError):
                raise JobExecutionError("voice_pace_import_failed", "pace candidate could not be retained", retryable=True) from None
            if candidate.content_hash != candidate_hash or candidate.authorization_reference != source.authorization_reference:
                raise JobExecutionError("voice_pace_candidate_conflict", "pace candidate identity conflicts with retained audio", retryable=False)
            existing_generation = candidate.metadata.get("voice_generation")
            if existing_generation is not None and (
                not isinstance(existing_generation, dict)
                or existing_generation.get("job_id") != str(job.id)
                or existing_generation.get("source_sha256") != source.content_hash
            ):
                raise JobExecutionError("voice_pace_candidate_conflict", "pace candidate already exists with different provenance", retryable=False)
            if existing_generation is None and set(candidate.metadata) != {"filename"}:
                raise JobExecutionError("voice_pace_candidate_conflict", "retained audio has existing provenance", retryable=False)
            metadata = dict(candidate.metadata)
            metadata["voice_generation"] = existing_generation or {
                "provider": "content_os_local_pace", "model": "ffmpeg-atempo-v1",
                "job_id": str(job.id), "project_id": str(job.project_id),
                "target_text": copy, "qa_state": "pending", "human_review_state": "pending",
                "source_audio_asset_id": str(source.id), "source_sha256": source.content_hash,
                "pace_candidate": {"profile": job.payload.profile, "tempo_factor": GENTLE_SLOWER_TEMPO},
                "provider_plan_application": "not_applicable",
            }
            now = datetime.now(timezone.utc)
            qa_payload = VoiceQaJobPayload(project_id=job.project_id, narration_audio_id=candidate.id, target_text=copy)
            qa_job = Job(
                id=uuid4(), project_id=job.project_id, type=JobType.VERIFY_VOICE,
                idempotency_key=f"voice-pace-qa:{job.id}", created_at=now, updated_at=now,
                payload=qa_payload,
            )
            with self._audios.db.transaction():
                self._audios.update(candidate.model_copy(update={"metadata": metadata}))
                persisted = self._jobs.create(qa_job)
                if persisted.type is not JobType.VERIFY_VOICE or persisted.payload != qa_payload:
                    raise JobExecutionError("voice_pace_qa_conflict", "pace candidate QA job conflicts with another request", retryable=False)
