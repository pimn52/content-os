"""One bounded V19B derived-audio evaluation from the retained C master.

This is not a general prosody renderer.  The fixed cut plan applies only to
the content-addressed V19A C candidate.  It preserves all PCM samples outside
the declared cuts, imports a new asset, and queues fresh full-master Voice QA.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import struct
import sys
from uuid import UUID, uuid4
import wave


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "services" / "api"))

from app.db import AudioAssetRepository, Database, JobRepository  # noqa: E402
from app.domain.models import Job, JobType, VoiceQaJobPayload  # noqa: E402
from app.media import AudioImporter, FFProbeAdapter  # noqa: E402
from app.runtime import resolve_local_executable  # noqa: E402


SOURCE_ID = UUID("b31ee4eb-6c26-48a7-904c-2ccffe75f29e")
SOURCE_HASH = "360f96540855f832dd9f3e1a7a48fa472bc87c1fe49d2ae88eaa344617236e26"
PROJECT_ID = UUID("161996a7-2607-4a4b-9247-d305537ce839")
COPY = "先别急着写文案。选题有没有判断，决定观众会不会继续听。但是，结构决定这句话能不能被听懂。最后，让结论落下。"
EVIDENCE_ID = "V19B-C-source-PCM-and-isolated-ASR-2026-09-23"


@dataclass(frozen=True)
class Cut:
    start_ms: int
    end_ms: int
    kind: str
    rationale: str


# Local evidence-specific numbers, not product-wide pause defaults.
CUTS = (
    Cut(4350, 4500, "quiet", "reduce the comma gap after 判断, retaining 60 ms measured quiet"),
    Cut(6370, 6590, "quiet", "reduce the 继续听。 sentence join without touching speech"),
    Cut(6690, 8560, "noncopy_preroll", "remove unstable non-copy vocalization and following long gap before acoustic 但是 onset"),
    Cut(12270, 12630, "quiet", "reduce the gap before 最后 while preserving its spoken delivery"),
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _rms_dbfs(samples: bytes) -> float:
    if not samples:
        raise ValueError("empty PCM interval")
    count = len(samples) // 2
    values = struct.unpack(f"<{count}h", samples)
    rms = math.sqrt(sum(value * value for value in values) / count) / 32768
    return 20 * math.log10(max(rms, 1e-10))


def edit_pcm(pcm: bytes, *, sample_rate: int, cuts: tuple[Cut, ...]) -> tuple[bytes, list[dict[str, object]]]:
    """Remove only declared sample intervals; reject spoken material in quiet cuts."""
    if sample_rate != 24_000 or len(pcm) % 2:
        raise ValueError("V19B requires mono 24 kHz PCM16")
    frames = len(pcm) // 2
    audit: list[dict[str, object]] = []
    kept: list[bytes] = []
    previous = 0
    for cut in cuts:
        start = cut.start_ms * sample_rate // 1000
        end = cut.end_ms * sample_rate // 1000
        if cut.start_ms * sample_rate % 1000 or cut.end_ms * sample_rate % 1000:
            raise ValueError("cut must fall on an exact sample index")
        if not previous <= start < end <= frames:
            raise ValueError("cuts must be ordered, non-overlapping and inside the source")
        if cut.kind not in {"quiet", "noncopy_preroll"}:
            raise ValueError("unknown cut kind")
        removed = pcm[start * 2:end * 2]
        if cut.kind == "quiet":
            window_frames = sample_rate // 100  # every 10 ms, no averaging away speech
            if any(_rms_dbfs(removed[index * 2:(index + window_frames) * 2]) > -45
                   for index in range(0, end - start, window_frames)):
                raise ValueError(f"declared quiet cut contains voiced audio: {cut.start_ms}-{cut.end_ms} ms")
        else:
            # The attribution is source-specific.  Keep the two join edges in
            # actual quiet PCM; the middle is explicitly *not* called silence.
            edge_bytes = sample_rate // 50 * 2  # 20 ms
            if len(removed) < edge_bytes * 2 or _rms_dbfs(removed[:edge_bytes]) > -45 or _rms_dbfs(removed[-edge_bytes:]) > -45:
                raise ValueError("non-copy pre-roll cut must start and end in measured quiet")
        kept.append(pcm[previous * 2:start * 2])
        audit.append({
            "start_ms": cut.start_ms, "end_ms": cut.end_ms,
            "start_sample": start, "end_sample": end,
            "kind": cut.kind, "rationale": cut.rationale,
            "rms_dbfs": round(_rms_dbfs(removed), 1),
            "evidence_ref": EVIDENCE_ID if cut.kind == "noncopy_preroll" else "source-PCM-10ms-windows-below--45dBFS",
        })
        previous = end
    kept.append(pcm[previous * 2:])
    result = b"".join(kept)
    if len(result) // 2 != frames - sum((cut.end_ms - cut.start_ms) * sample_rate // 1000 for cut in cuts):
        raise AssertionError("edited duration differs from declared cuts")
    return result, audit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=REPO_ROOT / "content-os-data" / "content-os.sqlite3")
    parser.add_argument("--data-root", type=Path, default=REPO_ROOT / "content-os-data")
    args = parser.parse_args()
    output_dir = args.data_root.resolve() / "evaluation-evidence" / "v19b-c-pause-salvage"
    output = output_dir / "v19b-c-pause-salvage.wav"
    manifest_path = output_dir / "v19b-c-pause-salvage.json"
    if output.exists() or manifest_path.exists():
        raise SystemExit("V19B candidate already exists; no second edit permitted")

    with Database(args.database.resolve()) as db:
        source = AudioAssetRepository(db).get(SOURCE_ID)
        generation = None if source is None else source.metadata.get("voice_generation")
        if source is None or source.content_hash != SOURCE_HASH or not isinstance(generation, dict):
            raise SystemExit("V19B source identity/provenance mismatch")
        if generation.get("qa_state") != "verified" or generation.get("target_text") != COPY:
            raise SystemExit("V19B source copy or QA is not valid")
        path = Path(source.source_file)
        if not path.is_file() or sha256(path) != SOURCE_HASH:
            raise SystemExit("V19B source file hash mismatch")
        with wave.open(str(path), "rb") as handle:
            if (handle.getnchannels(), handle.getsampwidth(), handle.getframerate(), handle.getcomptype()) != (1, 2, 24_000, "NONE"):
                raise SystemExit("V19B source PCM format mismatch")
            pcm = handle.readframes(handle.getnframes())
        edited, audit = edit_pcm(pcm, sample_rate=24_000, cuts=CUTS)
        output_dir.mkdir(parents=True, exist_ok=True)
        with output.open("xb") as stream, wave.open(stream, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(24_000)
            handle.writeframes(edited)
        output_hash = sha256(output)
        edit_evidence = {
            "evaluation": "V19B-C-single-derived-candidate",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_audio_asset_id": str(source.id),
            "source_path": str(path.resolve()),
            "source_sha256": SOURCE_HASH,
            "source_qa_state": "verified",
            "source_qa_job_id": "96289b95-96fc-49cb-9091-a2e308a71282",
            "copy": COPY,
            "source_frames": len(pcm) // 2,
            "output_frames": len(edited) // 2,
            "sample_rate": 24_000,
            "cuts": audit,
            "output_path": str(output.resolve()),
            "output_sha256": output_hash,
            "preroll_attribution": {
                "scope": "diagnostic, not word-perfect ASR",
                "pre_gap_medium": "拉 (unstable; prompted variant 吧)",
                "pre_gap_small": "啊 (unstable)",
                "post_gap_medium_and_small": "但是结构决定这句话能不能被听懂",
                "source_full_asr_note": "requested 但是 segment has coarse 8380 ms start; PCM acoustic onset is near 8600 ms",
            },
            "limitations": "No new provider inference, word-level emphasis, time stretch, synthetic breath or human U-Voice decision.",
        }
        manifest_path.write_text(json.dumps(edit_evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        imported = AudioImporter(db, args.data_root.resolve(), FFProbeAdapter(resolve_local_executable("ffprobe"))).import_path(
            output, source.authorization_reference, language=source.language,
        )
        if imported.content_hash != output_hash or imported.id == source.id:
            raise RuntimeError("derived audio import did not create the expected independent asset")
        next_generation = {key: value for key, value in generation.items() if key not in {
            "qa", "qa_state", "human_review", "human_review_state", "recovery", "composition",
        }}
        next_generation.update({
            "provider": "derived_audio_edit",
            "model": "v19b-c-pause-salvage",
            "qa_state": "pending",
            "human_review_state": "pending",
            "derived_from_audio_asset_id": str(source.id),
        })
        metadata = dict(imported.metadata)
        metadata["voice_generation"] = next_generation
        metadata["derived_audio_edit"] = {
            "manifest": str(manifest_path.resolve()),
            "source_audio_asset_id": str(source.id),
            "source_sha256": SOURCE_HASH,
            "output_sha256": output_hash,
            "cuts": audit,
            "human_quality_status": "pending",
        }
        updated = imported.model_copy(update={"metadata": metadata})
        now = datetime.now(timezone.utc)
        qa_job = Job(
            id=uuid4(), project_id=PROJECT_ID, type=JobType.VERIFY_VOICE,
            idempotency_key="v19b-c-pause-salvage-fresh-full-master-qa",
            created_at=now, updated_at=now,
            payload=VoiceQaJobPayload(project_id=PROJECT_ID, narration_audio_id=updated.id, target_text=COPY),
        )
        with db.transaction():
            AudioAssetRepository(db).update(updated)
            persisted = JobRepository(db).create(qa_job)
        if persisted.id != qa_job.id:
            raise RuntimeError("V19B QA idempotency key already belongs to another job")
        print(json.dumps({"audio_asset_id": str(updated.id), "audio_path": updated.source_file,
                          "qa_job_id": str(qa_job.id), "manifest": str(manifest_path.resolve()),
                          "output_frames": len(edited) // 2}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
