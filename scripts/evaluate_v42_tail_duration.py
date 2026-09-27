"""One source-bound V41 three-tail pitch-preserving contraction experiment."""

from __future__ import annotations

from array import array
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
import wave
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api"))

import evaluate_v41_punctuation_delivery as prior  # noqa: E402
from app.db import AudioAssetRepository, Database, JobRepository  # noqa: E402
from app.media import AudioImporter, FFProbeAdapter  # noqa: E402
from app.runtime import resolve_local_executable  # noqa: E402

DATA_ROOT = prior.DATA_ROOT
EVIDENCE = DATA_ROOT / "evaluation-evidence" / "v42-tail-duration"
MANIFEST = EVIDENCE / "v42-tail-duration.json"
SOURCE_ID = UUID("0f8346ae-19ae-4f44-a203-ff46876ff30e")
SOURCE_SHA = "b4b2fcbd4a6b1d4953d7af2f203afa5620d299b792190d9d4dd3a04b53c9213c"
PROJECT_ID = prior.PROJECT_ID
TEMPO = 1.8
FADE_MS = 10
# Source-child local PCM windows, hand-bound to the user's three exact marks.
# These are not safe global word boundaries or provider-controllable pauses.
LOCAL_WINDOWS = ((0, "after_而是", 3600, 3940),
                 (1, "after_前几秒", 2600, 3300),
                 (2, "after_先说清楚", 3040, 3400))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pcm(path: Path) -> tuple[int, array]:
    with wave.open(str(path), "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (1, 2, 24000, "NONE"):
            raise ValueError("V42 requires mono 24 kHz PCM16 WAV")
        count = wav.getnframes()
        samples = array("h")
        samples.frombytes(wav.readframes(count))
    if len(samples) != count:
        raise ValueError("V42 PCM frame count changed")
    return 24000, samples


def write_pcm(path: Path, samples: array) -> None:
    if path.exists():
        raise ValueError("V42 output already exists; no overwrite")
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(samples.tobytes())


def boundary_dbfs(samples: array, center_ms: int, *, half_ms: int = 20) -> float:
    start = (center_ms - half_ms) * 24
    end = (center_ms + half_ms) * 24
    if start < 0 or end > len(samples):
        raise ValueError("V42 boundary outside PCM")
    window = samples[start:end]
    rms = math.sqrt(sum(value * value for value in window) / len(window)) / 32768
    return 20 * math.log10(max(rms, 1e-10))


def join_crossfade(left: array, right: array, frames: int) -> array:
    if frames <= 0 or len(left) < frames or len(right) < frames:
        raise ValueError("V42 crossfade requires overlapping PCM")
    output = array("h", left[:-frames])
    for i in range(frames):
        weight = (i + 1) / (frames + 1)
        value = round(left[-frames + i] * (1 - weight) + right[i] * weight)
        output.append(max(-32768, min(32767, value)))
    output.extend(right[frames:])
    return output


def global_windows(v41: dict) -> tuple[tuple[str, int, int], ...]:
    master = v41["master"]
    boundaries = master["raw_boundaries_ms"]
    cuts = master["quiet_seam_cuts"]
    if len(boundaries) != 4 or len(cuts) != 4:
        raise ValueError("V41 five-span seam provenance changed")
    result = []
    for index, label, local_start, local_end in LOCAL_WINDOWS:
        offset = 0 if index == 0 else boundaries[index - 1] - sum(
            cut["end_ms"] - cut["start_ms"] for cut in cuts[:index])
        result.append((label, offset + local_start, offset + local_end))
    if result != [("after_而是", 3600, 3940), ("after_前几秒", 9190, 9890),
                  ("after_先说清楚", 15670, 16030)]:
        raise ValueError("V42 expected source-specific PCM windows changed")
    return tuple(result)


def transform_region(source: array, label: str, start_ms: int, end_ms: int) -> array:
    clip_path = EVIDENCE / f"{label}-source.wav"
    output_path = EVIDENCE / f"{label}-tempo.wav"
    write_pcm(clip_path, source[start_ms * 24:end_ms * 24])
    result = subprocess.run(
        [resolve_local_executable("ffmpeg"), "-nostdin", "-n", "-i", str(clip_path),
         "-af", f"atempo={TEMPO}", "-ar", "24000", "-ac", "1",
         "-c:a", "pcm_s16le", str(output_path)], capture_output=True, timeout=120, check=False)
    if result.returncode or not output_path.is_file():
        raise ValueError(f"V42 FFmpeg atempo failed for {label}")
    _, transformed = pcm(output_path)
    original = (end_ms - start_ms) * 24
    if not original * 0.45 <= len(transformed) <= original * 0.70:
        raise ValueError(f"V42 transformed duration is implausible for {label}")
    return transformed


def assemble(source: array, windows: tuple[tuple[str, int, int], ...], regions: list[array]) -> array:
    if len(windows) != len(regions):
        raise ValueError("V42 transformed region count mismatch")
    pieces = []
    cursor = 0
    for (_, start_ms, end_ms), transformed in zip(windows, regions):
        start, end = start_ms * 24, end_ms * 24
        if start < cursor or start >= end or end > len(source):
            raise ValueError("V42 windows overlap or exceed source")
        pieces.extend((source[cursor:start], transformed))
        cursor = end
    pieces.append(source[cursor:])
    result = array("h", pieces[0])
    fade = FADE_MS * 24
    for piece in pieces[1:]:
        result = join_crossfade(result, piece, fade)
    expected = len(source) - sum((end - start) * 24 - len(region)
                                 for (_, start, end), region in zip(windows, regions)) - fade * 2 * len(windows)
    if len(result) != expected:
        raise ValueError("V42 output frame count mismatch")
    return result


def prepare() -> None:
    if MANIFEST.exists():
        raise ValueError("V42 already prepared; no alternate windows or factors")
    v41 = json.loads(prior.MANIFEST.read_text(encoding="utf-8"))
    if v41["status"] != "u_voice_phrase_continuity_fail":
        raise ValueError("V41 exact U-Voice rejection evidence unavailable")
    windows = global_windows(v41)
    with Database(DATA_ROOT / "content-os.sqlite3") as db:
        source = AudioAssetRepository(db).get(SOURCE_ID)
        if (source is None or source.content_hash != SOURCE_SHA or not source.authorization_reference
                or (source.metadata.get("voice_generation") or {}).get("qa_state") != "verified"
                or source.metadata["voice_generation"].get("target_text") != v41["copy"]):
            raise ValueError("V41 source/copy/QA identity changed")
        source_path = Path(v41["master"]["path"])
        if not source_path.is_file() or sha256(source_path) != SOURCE_SHA:
            raise ValueError("V41 source WAV bytes changed")
        _, samples = pcm(source_path)
        acoustic = []
        for label, start, end in windows:
            start_dbfs, end_dbfs = boundary_dbfs(samples, start), boundary_dbfs(samples, end)
            if start_dbfs > -28 or end_dbfs > -28:
                raise ValueError(f"V42 {label} boundary is too loud for a local join")
            acoustic.append({"label": label, "start_ms": start, "end_ms": end,
                             "start_boundary_dbfs": round(start_dbfs, 2),
                             "end_boundary_dbfs": round(end_dbfs, 2)})
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        candidate = EVIDENCE / "v42-master-three-tail-tempo.wav"
        if candidate.exists():
            raise ValueError("V42 candidate already exists; no overwrite")
        transformed = [transform_region(samples, label, start, end) for label, start, end in windows]
        output = assemble(samples, windows, transformed)
        write_pcm(candidate, output)
        imported = AudioImporter(db, DATA_ROOT, FFProbeAdapter(resolve_local_executable("ffprobe"))).import_path(
            candidate, source.authorization_reference, language=source.language or "zh")
        if imported.content_hash != sha256(candidate) or "voice_generation" in imported.metadata:
            raise ValueError("V42 derived import is not independent")
        metadata = dict(imported.metadata)
        metadata["voice_generation"] = {
            "provider": "derived_audio_local_tempo_evaluation", "model": "v42-three-tail-atempo",
            "project_id": str(PROJECT_ID), "target_text": v41["copy"], "qa_state": "pending",
            "human_review_state": "pending", "source_audio_asset_id": str(SOURCE_ID),
            "source_sha256": SOURCE_SHA, "tempo_factor": TEMPO,
            "crossfade_ms": FADE_MS, "source_bound_windows": acoustic,
            "provider_plan_application": "unverified", "evaluation_only": True,
        }
        metadata["v42_evidence_manifest"] = str(MANIFEST)
        updated = imported.model_copy(update={"metadata": metadata})
        with db.transaction():
            AudioAssetRepository(db).update(updated)
            qa_job = prior.prior.queue_qa(db, updated.id, v41["copy"], "v42-master-fresh-copy-qa")
    manifest = {
        "package": "V42-bounded-three-tail-duration-repair", "status": "qa_pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_audio_asset_id": str(SOURCE_ID), "source_sha256": SOURCE_SHA,
        "source_path": str(source_path), "copy": v41["copy"],
        "windows": acoustic, "tempo_factor": TEMPO, "crossfade_ms": FADE_MS,
        "source_frames": len(samples), "candidate_frames": len(output),
        "candidate_path": str(candidate), "candidate_sha256": sha256(candidate),
        "candidate_audio_asset_id": str(updated.id), "qa_job_id": qa_job,
        "limitations": "Exact user-labelled source only. ASR timing and low-energy PCM are not a general safe word locator, proof of copy or human naturalness.",
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"audio_asset_id": str(updated.id), "qa_job_id": qa_job, "path": str(candidate)}))


def verify() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest["status"] != "qa_pending":
        raise ValueError("V42 fresh QA is not pending")
    with Database(DATA_ROOT / "content-os.sqlite3") as db:
        asset = AudioAssetRepository(db).get(UUID(manifest["candidate_audio_asset_id"]))
        job = JobRepository(db).get(UUID(manifest["qa_job_id"]))
    if asset is None or job is None or job.status.value != "completed":
        raise ValueError("V42 fresh Voice QA incomplete")
    generation = asset.metadata.get("voice_generation") or {}
    qa = generation.get("qa") or {}
    if (generation.get("qa_state") != "verified" or generation.get("target_text") != manifest["copy"]
            or qa.get("copy_coverage") != 1.0 or qa.get("missing_token_count") != 0
            or qa.get("duplicate_token_count") != 0 or qa.get("substitution_token_count") != 0
            or asset.content_hash != manifest["candidate_sha256"]
            or sha256(Path(manifest["candidate_path"])) != manifest["candidate_sha256"]):
        raise ValueError("V42 fresh whole-copy QA failed")
    manifest["qa"] = qa
    manifest["status"] = "awaiting_u_review"
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"audio_asset_id": str(asset.id), "path": manifest["candidate_path"],
                      "qa_state": "verified"}))


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in {"prepare", "verify"}:
        raise SystemExit("usage: evaluate_v42_tail_duration.py prepare|verify")
    {"prepare": prepare, "verify": verify}[sys.argv[1]]()
