"""One V19E final-sentence join candidate from the retained V19D C master.

The long edit is explicitly low-energy *audible* material not aligned by the
current ASR.  It is neither certified silence nor a generic speech editor.
Fresh full-copy QA and human listening remain separate required gates.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import struct
import sys
from uuid import UUID, uuid4
import wave


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "services" / "api"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.db import AudioAssetRepository, Database, JobRepository  # noqa: E402
from app.domain.models import Job, JobType, VoiceQaJobPayload  # noqa: E402
from app.media import AudioImporter, FFProbeAdapter  # noqa: E402
from app.runtime import resolve_local_executable  # noqa: E402
from evaluate_c_pause_salvage import COPY, PROJECT_ID, Cut, edit_pcm, sha256  # noqa: E402


SOURCE_ID = UUID("025f048b-8d40-42ce-934f-a550017b4a15")
SOURCE_HASH = "9744a86a72f95bc5c92beda6e6fc4c4fbcb37a452d58d370d0c0f368f9473bd7"
FINAL_LOW_ENERGY_START_MS = 8210
FINAL_LOW_ENERGY_END_MS = 9380
ENVELOPE_MS = 10
COMMA_QUIET_CUT = Cut(3850, 3870, "quiet", "slightly shorten remaining 判断， quiet without altering accent")


def _rms_dbfs(pcm: bytes) -> float:
    if not pcm or len(pcm) % 2:
        raise ValueError("invalid PCM interval")
    values = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    rms = math.sqrt(sum(value * value for value in values) / len(values)) / 32768
    return 20 * math.log10(max(rms, 1e-10))


def remove_low_energy_join(
    pcm: bytes, *, sample_rate: int, start_ms: int, end_ms: int, envelope_ms: int,
) -> tuple[bytes, dict[str, object]]:
    """Cut one evidence-bounded low-energy interval with explicit edge fades."""
    if sample_rate != 24_000 or len(pcm) % 2 or not 0 < envelope_ms < start_ms < end_ms:
        raise ValueError("invalid V19E PCM edit")
    if any(ms * sample_rate % 1000 for ms in (start_ms, end_ms, envelope_ms)):
        raise ValueError("edit times must map to exact samples")
    start = start_ms * sample_rate // 1000
    end = end_ms * sample_rate // 1000
    fade = envelope_ms * sample_rate // 1000
    if end + fade >= len(pcm) // 2:
        raise ValueError("edit exceeds source")
    edge_frames = sample_rate // 100
    left_dbfs = _rms_dbfs(pcm[(start - edge_frames) * 2:start * 2])
    right_dbfs = _rms_dbfs(pcm[end * 2:(end + edge_frames) * 2])
    if left_dbfs > -32 or right_dbfs > -32:
        raise ValueError("join edge contains strong speech")
    removed = pcm[start * 2:end * 2]
    window_frames = sample_rate // 20  # every 50 ms; reject a hidden strong word body
    for at in range(0, end - start, window_frames):
        window = removed[at * 2:min(at + window_frames, end - start) * 2]
        if _rms_dbfs(window) > -30:
            raise ValueError("declared low-energy interval contains strong speech")
    left = struct.unpack(f"<{fade}h", pcm[(start - fade) * 2:start * 2])
    right = struct.unpack(f"<{fade}h", pcm[end * 2:(end + fade) * 2])
    left_faded = struct.pack(f"<{fade}h", *(
        round(value * (fade - index - 1) / fade) for index, value in enumerate(left)
    ))
    right_faded = struct.pack(f"<{fade}h", *(
        round(value * index / fade) for index, value in enumerate(right)
    ))
    result = b"".join((
        pcm[:(start - fade) * 2], left_faded, right_faded, pcm[(end + fade) * 2:],
    ))
    if len(result) != len(pcm) - (end - start) * 2:
        raise AssertionError("join edit duration differs from declared samples")
    return result, {
        "kind": "asr_unrecognized_low_energy_audio_not_silence",
        "start_ms": start_ms, "end_ms": end_ms,
        "start_sample": start, "end_sample": end,
        "removed_ms": end_ms - start_ms,
        "removed_rms_dbfs": round(_rms_dbfs(removed), 1),
        "left_edge_10ms_dbfs": round(left_dbfs, 1),
        "right_edge_10ms_dbfs": round(right_dbfs, 1),
        "envelope_ms_per_side": envelope_ms,
        "envelope_samples_per_side": fade,
        "asr_attribution": "medium ASR: 懂 ends about 8.14s; 最 starts about 9.36s; interval is not word-perfect proof of non-linguistic content",
        "human_review_warning": "ASR cannot establish that the 懂 ending or final join sounds natural.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=REPO_ROOT / "content-os-data" / "content-os.sqlite3")
    parser.add_argument("--data-root", type=Path, default=REPO_ROOT / "content-os-data")
    args = parser.parse_args()
    output_dir = args.data_root.resolve() / "evaluation-evidence" / "v19e-c-final-join"
    output = output_dir / "v19e-c-final-join.wav"
    manifest_path = output_dir / "v19e-c-final-join.json"
    if output.exists() or manifest_path.exists():
        raise SystemExit("V19E candidate already exists; no alternate edit permitted")

    with Database(args.database.resolve()) as db:
        source = AudioAssetRepository(db).get(SOURCE_ID)
        generation = None if source is None else source.metadata.get("voice_generation")
        if source is None or source.content_hash != SOURCE_HASH or not isinstance(generation, dict):
            raise SystemExit("V19E source identity mismatch")
        if generation.get("qa_state") != "verified" or generation.get("target_text") != COPY:
            raise SystemExit("V19E source copy or QA invalid")
        path = Path(source.source_file)
        if not path.is_file() or sha256(path) != SOURCE_HASH:
            raise SystemExit("V19E source file hash mismatch")
        with wave.open(str(path), "rb") as handle:
            if (handle.getnchannels(), handle.getsampwidth(), handle.getframerate(), handle.getcomptype()) != (1, 2, 24_000, "NONE"):
                raise SystemExit("V19E source PCM format mismatch")
            pcm = handle.readframes(handle.getnframes())
        shortened, join_audit = remove_low_energy_join(
            pcm, sample_rate=24_000, start_ms=FINAL_LOW_ENERGY_START_MS,
            end_ms=FINAL_LOW_ENERGY_END_MS, envelope_ms=ENVELOPE_MS,
        )
        edited, comma_audit = edit_pcm(shortened, sample_rate=24_000, cuts=(COMMA_QUIET_CUT,))
        output_dir.mkdir(parents=True, exist_ok=True)
        with output.open("xb") as stream, wave.open(stream, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(24_000)
            handle.writeframes(edited)
        output_hash = sha256(output)
        evidence = {
            "evaluation": "V19E-C-single-final-join-candidate",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_audio_asset_id": str(source.id), "source_sha256": SOURCE_HASH,
            "source_qa_job_id": "cbdb7d9c-ca55-4f9e-9dfd-e39fc3e34e9b",
            "copy": COPY, "source_frames": len(pcm) // 2,
            "output_frames": len(edited) // 2, "sample_rate": 24_000,
            "low_energy_join_edit": join_audit, "quiet_cuts": comma_audit,
            "performance_plan": generation.get("performance_plan"),
            "output_path": str(output.resolve()), "output_sha256": output_hash,
            "limitations": "Source-specific evaluation; no generic speech trim or U-Voice result.",
        }
        manifest_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        imported = AudioImporter(db, args.data_root.resolve(), FFProbeAdapter(resolve_local_executable("ffprobe"))).import_path(
            output, source.authorization_reference, language=source.language,
        )
        if imported.content_hash != output_hash or imported.id == source.id:
            raise RuntimeError("V19E derived import is not independent")
        next_generation = {key: value for key, value in generation.items() if key not in {
            "qa", "qa_state", "human_review", "human_review_state", "recovery", "composition",
        }}
        next_generation.update({
            "provider": "derived_audio_edit", "model": "v19e-c-final-join",
            "qa_state": "pending", "human_review_state": "pending",
            "derived_from_audio_asset_id": str(source.id),
        })
        metadata = dict(imported.metadata)
        metadata["voice_generation"] = next_generation
        metadata["derived_audio_edit"] = {
            "manifest": str(manifest_path.resolve()), "source_audio_asset_id": str(source.id),
            "source_sha256": SOURCE_HASH, "output_sha256": output_hash,
            "low_energy_join_edit": join_audit, "quiet_cuts": comma_audit,
            "human_quality_status": "pending",
        }
        updated = imported.model_copy(update={"metadata": metadata})
        now = datetime.now(timezone.utc)
        qa_job = Job(
            id=uuid4(), project_id=PROJECT_ID, type=JobType.VERIFY_VOICE,
            idempotency_key="v19e-c-final-join-fresh-full-master-qa",
            created_at=now, updated_at=now,
            payload=VoiceQaJobPayload(project_id=PROJECT_ID, narration_audio_id=updated.id, target_text=COPY),
        )
        with db.transaction():
            AudioAssetRepository(db).update(updated)
            persisted = JobRepository(db).create(qa_job)
        if persisted.id != qa_job.id:
            raise RuntimeError("V19E QA idempotency key already belongs to another job")
        print(json.dumps({"audio_asset_id": str(updated.id), "audio_path": updated.source_file,
                          "qa_job_id": str(qa_job.id), "manifest": str(manifest_path.resolve()),
                          "output_frames": len(edited) // 2}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
