"""One source-bound V38 quiet-only edit; no provider inference or generic locator."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import wave
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api"))

from app.db import AudioAssetRepository, Database, JobRepository  # noqa: E402
from app.domain.models import Job, JobType, VoiceQaJobPayload  # noqa: E402
from app.media import AudioImporter, FFProbeAdapter  # noqa: E402
from app.runtime import resolve_local_executable  # noqa: E402
from app.voice_observation import observe_voice_performance  # noqa: E402

DATA_ROOT = ROOT / "content-os-data"
EVIDENCE = DATA_ROOT / "evaluation-evidence" / "v39-quiet-compaction"
MANIFEST = EVIDENCE / "v39-quiet-compaction.json"
SOURCE_ID = UUID("8c711bcc-d8ae-4de8-aea4-b926a7135c7c")
SOURCE_SHA = "8716a826aeca8402ccd50064370ef3562055a801e82c5bd1547ed5079c666c9b"
PROJECT_ID = UUID("c710747f-4742-4a6f-b80d-1d46527f6c90")
COPY = (
    "一个好选题，不是把信息堆满，而是先回答观众最关心的问题。 "
    "观众刷到视频的前几秒，会先问：这件事跟我有什么关系？ "
    "所以写文案之前，先说清楚你的判断，再用例子和证据把它讲明白。 "
    "不要为了显得专业，把所有信息一次塞进去。每一段只解决一个问题。 "
    "最后，把结论收回来，让观众听得懂，也记得住。"
)

# Exact positions on this source only, milliseconds. Each removed interval must
# lie strictly inside the independently observed -45 dBFS quiet envelope.
CUTS = (
    ("seam_0_1", 6700, 6810),
    ("internal_colon", 9910, 10540),
    ("seam_1_2", 12940, 13050),
    ("seam_2_3", 20180, 20330),
    ("internal_no_punctuation", 25880, 26320),
    ("seam_3_4", 27730, 27850),
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compact_pcm(raw: bytes, *, rate: int, cuts: tuple[tuple[str, int, int], ...]) -> bytes:
    """Remove only specified PCM16 mono frames; untouched frames stay byte-exact."""
    if rate != 24000 or len(raw) % 2:
        raise ValueError("V39 requires complete 24 kHz mono PCM16 frames")
    total = len(raw) // 2
    previous = 0
    parts: list[bytes] = []
    for name, start_ms, end_ms in cuts:
        start = start_ms * rate // 1000
        end = end_ms * rate // 1000
        if not name or start < previous or start >= end or end > total:
            raise ValueError("V39 cuts must be named, ordered, nonoverlapping and in range")
        parts.append(raw[previous * 2:start * 2])
        previous = end
    parts.append(raw[previous * 2:])
    output = b"".join(parts)
    assert len(output) == len(raw) - sum((end - start) * rate // 1000 * 2 for _, start, end in cuts)
    return output


def verify_quiet_envelopes(intervals: list[dict], cuts: tuple[tuple[str, int, int], ...]) -> None:
    for name, start, end in cuts:
        matches = [item for item in intervals if item["start_ms"] + 40 <= start
                   and end <= item["end_ms"] - 40]
        if len(matches) != 1:
            raise ValueError(f"V39 cut {name} is not inside one measured quiet interval with 40ms margins")


def prepare() -> None:
    if MANIFEST.exists():
        raise ValueError("V39 candidate already prepared; no retuning or overwrite")
    with Database(DATA_ROOT / "content-os.sqlite3") as db:
        audios = AudioAssetRepository(db)
        jobs = JobRepository(db)
        source = audios.get(SOURCE_ID)
        if source is None or source.content_hash != SOURCE_SHA or not source.authorization_reference:
            raise ValueError("V38 source identity/authorization changed")
        generation = source.metadata.get("voice_generation") or {}
        if generation.get("qa_state") != "verified" or generation.get("target_text") != COPY:
            raise ValueError("V38 full-copy QA or target changed")
        observation = observe_voice_performance(source, COPY, data_root=DATA_ROOT)
        intervals = observation["measurement"]["physical_quiet_intervals"]
        verify_quiet_envelopes(intervals, CUTS)
        source_path = DATA_ROOT / "assets" / "audio-originals" / f"{SOURCE_SHA}.wav"
        if not source_path.is_file() or sha256(source_path) != SOURCE_SHA:
            raise ValueError("V38 source bytes changed")
        with wave.open(str(source_path), "rb") as wav:
            if (wav.getnchannels(), wav.getframerate(), wav.getsampwidth(), wav.getcomptype()) != (1, 24000, 2, "NONE"):
                raise ValueError("V38 source PCM shape changed")
            frames = wav.getnframes()
            raw = wav.readframes(frames)
        output = compact_pcm(raw, rate=24000, cuts=CUTS)
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        target = EVIDENCE / "v39-v38-quiet-only.wav"
        if target.exists():
            raise ValueError("V39 candidate WAV already exists; no overwrite")
        with wave.open(str(target), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(output)
        candidate_sha = sha256(target)
        imported = AudioImporter(db, DATA_ROOT, FFProbeAdapter(resolve_local_executable("ffprobe"))).import_path(
            target, source.authorization_reference, language=source.language or "zh"
        )
        if imported.content_hash != candidate_sha or "voice_generation" in imported.metadata:
            raise ValueError("V39 import is not independent")
        metadata = dict(imported.metadata)
        metadata["voice_generation"] = {
            "provider": "derived_audio_quiet_evaluation", "model": "v39-pcm16-quiet-only",
            "project_id": str(PROJECT_ID), "target_text": COPY,
            "qa_state": "pending", "human_review_state": "pending",
            "source_audio_asset_id": str(SOURCE_ID), "source_sha256": SOURCE_SHA,
            "source_frames": frames, "output_frames": len(output) // 2,
            "removed_intervals_ms": [{"name": n, "start_ms": s, "end_ms": e} for n, s, e in CUTS],
            "provider_plan_application": "unverified", "evaluation_only": True,
        }
        metadata["v39_evidence_manifest"] = str(MANIFEST)
        updated = imported.model_copy(update={"metadata": metadata})
        now = datetime.now(timezone.utc)
        qa_job = Job(
            id=uuid4(), project_id=PROJECT_ID, type=JobType.VERIFY_VOICE,
            idempotency_key=f"v39-v38-quiet-only-{SOURCE_ID}", created_at=now, updated_at=now,
            payload=VoiceQaJobPayload(project_id=PROJECT_ID, narration_audio_id=updated.id, target_text=COPY),
        )
        with db.transaction():
            audios.update(updated)
            persisted = jobs.create(qa_job)
            if persisted.id != qa_job.id:
                raise ValueError("V39 QA idempotency key already used")
    manifest = {
        "package": "V39-bounded-exact-master-quiet-compaction", "status": "qa_pending",
        "created_at": now.isoformat(), "source_audio_asset_id": str(SOURCE_ID),
        "source_sha256": SOURCE_SHA, "source_path": str(source_path),
        "project_id": str(PROJECT_ID), "copy": COPY,
        "quiet_measurement": {"frame_ms": 10, "threshold_dbfs": -45,
                              "source_sha256": SOURCE_SHA, "intervals": intervals},
        "cuts": [{"name": n, "start_ms": s, "end_ms": e} for n, s, e in CUTS],
        "removed_ms": sum(e - s for _, s, e in CUTS),
        "source_frames": frames, "candidate_frames": len(output) // 2,
        "candidate_path": str(target), "candidate_sha256": candidate_sha,
        "candidate_audio_asset_id": str(updated.id), "qa_job_id": str(qa_job.id),
        "limitations": "Source-specific acoustic quiet edit only; PCM quiet does not prove copy or generalized safe punctuation location. No provider call or human approval.",
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"audio_asset_id": str(updated.id), "qa_job_id": str(qa_job.id), "path": str(target)}))


def verify() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest["status"] not in {"qa_pending", "awaiting_u_review"}:
        raise ValueError("V39 manifest state is not eligible")
    with Database(DATA_ROOT / "content-os.sqlite3") as db:
        audio = AudioAssetRepository(db).get(UUID(manifest["candidate_audio_asset_id"]))
        job = JobRepository(db).get(UUID(manifest["qa_job_id"]))
    if audio is None or job is None or job.status.value != "completed":
        raise ValueError("V39 fresh Voice QA is incomplete")
    generation = audio.metadata.get("voice_generation") or {}
    qa = generation.get("qa") or {}
    if (generation.get("qa_state") != "verified" or generation.get("target_text") != COPY
            or qa.get("copy_coverage") != 1.0 or qa.get("missing_token_count") != 0
            or qa.get("duplicate_token_count") != 0 or qa.get("substitution_token_count") != 0):
        raise ValueError("V39 fresh full-copy Voice QA did not pass")
    if audio.content_hash != manifest["candidate_sha256"] or sha256(Path(manifest["candidate_path"])) != audio.content_hash:
        raise ValueError("V39 candidate bytes changed after QA")
    manifest["status"] = "awaiting_u_review"
    manifest["fresh_qa"] = qa
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"audio_asset_id": str(audio.id), "path": manifest["candidate_path"], "qa_state": "verified"}))


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in {"prepare", "verify"}:
        raise SystemExit("usage: evaluate_v39_quiet_compaction.py prepare|verify")
    {"prepare": prepare, "verify": verify}[sys.argv[1]]()
