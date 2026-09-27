"""One bounded, existing-audio pace comparison; no new Voice inference.

Run ``prepare`` once, run the two ordinary verify_voice jobs with the local
worker, then run ``verify``. The originals and previous QA jobs stay intact.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import wave
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api"))

from app.db import AudioAssetRepository, Database, JobRepository  # noqa: E402
from app.domain.models import Job, JobType, VoiceQaJobPayload  # noqa: E402
from app.media import AudioImporter, FFProbeAdapter  # noqa: E402
from app.runtime import resolve_local_executable  # noqa: E402

EVIDENCE = ROOT / "content-os-data" / "evaluation-evidence" / "v31-pace-candidates"
MANIFEST = EVIDENCE / "v31-pace-candidates.json"
DATA_ROOT = ROOT / "content-os-data"
SOURCE_IDS = (
    UUID("838f6eb9-2eb6-4ac4-b692-5b8c1cc64b0f"),
    UUID("9361fdaf-4040-4f30-b185-a3fa981dfb33"),
)
# One conservative review setting, not a global preferred speaking rate.
TEMPO = 0.94


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def pcm_shape(path: Path) -> tuple[int, int, int]:
    with wave.open(str(path), "rb") as audio:
        if audio.getcomptype() != "NONE" or audio.getsampwidth() != 2:
            raise ValueError("V31 requires uncompressed 16-bit PCM WAV")
        return audio.getframerate(), audio.getnchannels(), audio.getnframes()


def transform_whole_take(source_path: Path, target: Path) -> tuple[int, int]:
    source_rate, source_channels, source_frames = pcm_shape(source_path)
    if source_rate != 24000 or source_channels != 1:
        raise ValueError("V31 source is outside the reviewed PCM profile")
    if target.exists():
        raise ValueError("V31 candidate already exists; no overwrite")
    result = subprocess.run(
        [str(resolve_local_executable("ffmpeg")), "-nostdin", "-n", "-i", str(source_path),
         "-af", f"atempo={TEMPO:.2f}", "-ar", str(source_rate), "-ac", "1",
         "-c:a", "pcm_s16le", str(target)],
        capture_output=True, text=True, timeout=120, check=False,
    )
    if result.returncode != 0 or not target.is_file():
        raise ValueError(f"V31 local tempo transform failed: {result.stderr[-400:]}")
    output_rate, output_channels, output_frames = pcm_shape(target)
    if (output_rate, output_channels) != (source_rate, source_channels):
        raise ValueError("V31 transformed PCM format changed")
    if abs(output_frames - source_frames / TEMPO) > source_rate * 0.06:
        raise ValueError("V31 transformed duration is outside the tempo tolerance")
    return source_frames, output_frames


def prepare() -> None:
    if MANIFEST.exists():
        raise ValueError("V31 already prepared; no retuning or overwrite")
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    rows = []
    with Database(DATA_ROOT / "content-os.sqlite3") as db:
        audios = AudioAssetRepository(db)
        jobs = JobRepository(db)
        for index, source_id in enumerate(SOURCE_IDS):
            source = audios.get(source_id)
            if source is None or not source.authorization_reference:
                raise ValueError(f"V31 source {source_id} is missing or not authorized")
            generation = source.metadata.get("voice_generation") or {}
            copy = generation.get("target_text")
            if generation.get("qa_state") != "verified" or not isinstance(copy, str) or not copy:
                raise ValueError(f"V31 source {source_id} is not independently copy-QA verified")
            source_path = DATA_ROOT / "assets" / "audio-originals" / f"{source.content_hash}.wav"
            if not source_path.is_file() or sha256(source_path) != source.content_hash:
                raise ValueError(f"V31 source {source_id} bytes do not match its AudioAsset")
            original_job = jobs.get(UUID(generation["job_id"]))
            if original_job is None:
                raise ValueError("V31 source generation job is missing")
            target = EVIDENCE / f"v31-source-{index}-tempo-094.wav"
            source_frames, output_frames = transform_whole_take(source_path, target)
            imported = AudioImporter(db, DATA_ROOT, FFProbeAdapter(resolve_local_executable("ffprobe"))).import_path(
                target, source.authorization_reference, language=source.language or "zh"
            )
            if imported.content_hash != sha256(target) or "voice_generation" in imported.metadata:
                raise ValueError("V31 derived import is not independent")
            metadata = dict(imported.metadata)
            metadata["voice_generation"] = {
                "provider": "derived_audio_tempo_evaluation", "model": "v31-ffmpeg-atempo",
                "project_id": str(original_job.project_id), "target_text": copy,
                "qa_state": "pending", "human_review_state": "pending",
                "source_audio_asset_id": str(source.id), "source_sha256": source.content_hash,
                "tempo_factor": TEMPO, "source_frames": source_frames, "output_frames": output_frames,
                "provider_plan_application": "unverified",
            }
            metadata["v31_evidence_manifest"] = str(MANIFEST)
            updated = imported.model_copy(update={"metadata": metadata})
            now = datetime.now(timezone.utc)
            qa_job = Job(
                id=uuid4(), project_id=original_job.project_id, type=JobType.VERIFY_VOICE,
                idempotency_key=f"v31-pace-{source.id}-fresh-voice-qa", created_at=now, updated_at=now,
                payload=VoiceQaJobPayload(project_id=original_job.project_id,
                                           narration_audio_id=updated.id, target_text=copy),
            )
            with db.transaction():
                audios.update(updated)
                persisted = jobs.create(qa_job)
                if persisted.id != qa_job.id:
                    raise ValueError("V31 QA idempotency key already used")
            rows.append({
                "source_audio_asset_id": str(source.id), "source_sha256": source.content_hash,
                "project_id": str(original_job.project_id), "copy": copy,
                "candidate_path": str(target), "candidate_sha256": sha256(target),
                "candidate_audio_asset_id": str(updated.id), "qa_job_id": str(qa_job.id),
                "source_frames": source_frames, "candidate_frames": output_frames,
            })
    MANIFEST.write_text(json.dumps({
        "package": "V31-existing-audio-whole-take-pace", "status": "qa_pending",
        "created_at": datetime.now(timezone.utc).isoformat(), "tempo_factor": TEMPO,
        "candidates": rows,
        "limitation": "One pitch-preserving whole-take transform for listening; not OmniVoice plan application, arbitrary-copy generalization or product admission.",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps([{"audio_asset_id": row["candidate_audio_asset_id"], "qa_job_id": row["qa_job_id"]} for row in rows]))


def verify() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("status") not in {"qa_pending", "awaiting_u_review"} or manifest.get("tempo_factor") != TEMPO:
        raise ValueError("V31 manifest is not eligible for QA verification")
    with Database(DATA_ROOT / "content-os.sqlite3") as db:
        audios = AudioAssetRepository(db)
        jobs = JobRepository(db)
        for row in manifest["candidates"]:
            asset = audios.get(UUID(row["candidate_audio_asset_id"]))
            job = jobs.get(UUID(row["qa_job_id"]))
            if asset is None or job is None or job.status.value != "completed":
                raise ValueError("V31 independent Voice QA job is incomplete")
            generation = asset.metadata.get("voice_generation") or {}
            if generation.get("qa_state") != "verified" or generation.get("target_text") != row["copy"]:
                raise ValueError("V31 independent full-copy Voice QA failed")
            if asset.content_hash != row["candidate_sha256"] or sha256(Path(row["candidate_path"])) != asset.content_hash:
                raise ValueError("V31 candidate bytes changed after QA")
            row["qa"] = generation["qa"]
    if manifest["status"] == "qa_pending":
        manifest["status"] = "awaiting_u_review"
        MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps([{"audio_asset_id": row["candidate_audio_asset_id"], "path": row["candidate_path"]} for row in manifest["candidates"]]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "verify"))
    {"prepare": prepare, "verify": verify}[parser.parse_args().phase]()
