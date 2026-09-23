"""One V19C C-derived test of a user-directed punctuation boundary.

Only this retained V19B source is accepted.  The script inserts a brief
plan-backed boundary at the measured 案/选 acoustic transition and shortens
one verified quiet gap.  It does not retime or regenerate spoken phonemes.
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
from app.domain.models import (  # noqa: E402
    Job, JobType, NarrationPause, NarrationPerformanceCue,
    NarrationPerformanceCueKind, NarrationPerformancePlan, VoiceQaJobPayload,
)
from app.media import AudioImporter, FFProbeAdapter  # noqa: E402
from app.narration_performance import validate_narration_performance_plan  # noqa: E402
from app.runtime import resolve_local_executable  # noqa: E402
from evaluate_c_pause_salvage import COPY, PROJECT_ID, Cut, edit_pcm, sha256  # noqa: E402


SOURCE_ID = UUID("11ab577d-7fd0-4abb-92a9-92624458eb52")
SOURCE_HASH = "2c62ed416f7cea877a3846ef292a32024d4fa4dfc2c860f2ff880d0ee42c9eb6"
PERIOD_AT_MS = 2100
PERIOD_INSERT_MS = 180  # Local BRIEF-cue rendering, not a global acoustic rule.
ENVELOPE_MS = 5
FINAL_QUIET_CUT = Cut(9900, 9980, "quiet", "reduce measured 听懂。 / 最后， gap from 200 to 120 ms")


def _rms_dbfs(pcm: bytes) -> float:
    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    rms = math.sqrt(sum(value * value for value in samples) / len(samples)) / 32768
    return 20 * math.log10(max(rms, 1e-10))


def insert_boundary(
    pcm: bytes, *, sample_rate: int, at_ms: int, pause_ms: int, envelope_ms: int,
) -> tuple[bytes, dict[str, object]]:
    """Insert a click-safe pause, altering only the declared adjacent envelope."""
    if sample_rate != 24_000 or len(pcm) % 2 or at_ms <= envelope_ms or pause_ms <= 0 or envelope_ms <= 0:
        raise ValueError("invalid V19C PCM boundary")
    if any(ms * sample_rate % 1000 for ms in (at_ms, pause_ms, envelope_ms)):
        raise ValueError("boundary and envelope must have exact sample indices")
    at = at_ms * sample_rate // 1000
    fade = envelope_ms * sample_rate // 1000
    pause = pause_ms * sample_rate // 1000
    frames = len(pcm) // 2
    if at + fade >= frames:
        raise ValueError("boundary exceeds source")
    before = pcm[(at - sample_rate // 100) * 2:at * 2]
    after = pcm[at * 2:(at + sample_rate // 100) * 2]
    before_dbfs, after_dbfs = _rms_dbfs(before), _rms_dbfs(after)
    if before_dbfs > -30 or after_dbfs > -30:
        raise ValueError("period insertion edge contains strong speech")
    left = list(struct.unpack(f"<{fade}h", pcm[(at - fade) * 2:at * 2]))
    right = list(struct.unpack(f"<{fade}h", pcm[at * 2:(at + fade) * 2]))
    left = [round(value * (fade - index - 1) / fade) for index, value in enumerate(left)]
    right = [round(value * index / fade) for index, value in enumerate(right)]
    result = b"".join((
        pcm[:(at - fade) * 2], struct.pack(f"<{fade}h", *left),
        b"\x00\x00" * pause,
        struct.pack(f"<{fade}h", *right), pcm[(at + fade) * 2:],
    ))
    if len(result) != len(pcm) + pause * 2:
        raise AssertionError("period insertion duration differs from declared samples")
    return result, {
        "at_ms": at_ms, "at_sample": at, "inserted_ms": pause_ms, "inserted_samples": pause,
        "envelope_ms_per_side": envelope_ms, "envelope_samples_per_side": fade,
        "preceding_10ms_dbfs": round(before_dbfs, 1), "following_10ms_dbfs": round(after_dbfs, 1),
        "copy_boundary": "先别急着写文案。|选题有没有判断，决定…",
        "evidence": "medium ASR places 案/选 near 2140 ms; PCM energy valley precedes 2120 ms onset; user review requires a terminal break",
    }


def plan_with_period_cue(raw: dict[str, object]) -> NarrationPerformancePlan:
    plan = NarrationPerformancePlan.model_validate(raw)
    validate_narration_performance_plan(plan, COPY)
    at_char = COPY.index("文案。") + len("文案。")
    cue = NarrationPerformanceCue(
        kind=NarrationPerformanceCueKind.PAUSE,
        start_char=at_char, end_char=at_char, pause=NarrationPause.BRIEF,
        note="U-Voice V19B: sentence close must outrank the continuing 判断， clause",
    )
    values = plan.model_dump(mode="json")
    values["cues"] = [*values["cues"], cue.model_dump(mode="json")]
    values["evidence_refs"] = [*values["evidence_refs"], "v19b-u-voice-punctuation-hierarchy-review"]
    result = NarrationPerformancePlan.model_validate(values)
    validate_narration_performance_plan(result, COPY)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=REPO_ROOT / "content-os-data" / "content-os.sqlite3")
    parser.add_argument("--data-root", type=Path, default=REPO_ROOT / "content-os-data")
    args = parser.parse_args()
    output_dir = args.data_root.resolve() / "evaluation-evidence" / "v19c-c-punctuation-hierarchy"
    output = output_dir / "v19c-c-punctuation-hierarchy.wav"
    manifest_path = output_dir / "v19c-c-punctuation-hierarchy.json"
    if output.exists() or manifest_path.exists():
        raise SystemExit("V19C candidate already exists; no alternate edit permitted")

    with Database(args.database.resolve()) as db:
        source = AudioAssetRepository(db).get(SOURCE_ID)
        generation = None if source is None else source.metadata.get("voice_generation")
        if source is None or source.content_hash != SOURCE_HASH or not isinstance(generation, dict):
            raise SystemExit("V19C source identity mismatch")
        if generation.get("qa_state") != "verified" or generation.get("target_text") != COPY:
            raise SystemExit("V19C source copy or QA invalid")
        path = Path(source.source_file)
        if not path.is_file() or sha256(path) != SOURCE_HASH:
            raise SystemExit("V19C source file hash mismatch")
        with wave.open(str(path), "rb") as handle:
            if (handle.getnchannels(), handle.getsampwidth(), handle.getframerate(), handle.getcomptype()) != (1, 2, 24_000, "NONE"):
                raise SystemExit("V19C source PCM format mismatch")
            pcm = handle.readframes(handle.getnframes())
        plan = plan_with_period_cue(generation["performance_plan"])
        shortened, cut_audit = edit_pcm(pcm, sample_rate=24_000, cuts=(FINAL_QUIET_CUT,))
        edited, insertion_audit = insert_boundary(
            shortened, sample_rate=24_000, at_ms=PERIOD_AT_MS,
            pause_ms=PERIOD_INSERT_MS, envelope_ms=ENVELOPE_MS,
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        with output.open("xb") as stream, wave.open(stream, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(24_000)
            handle.writeframes(edited)
        output_hash = sha256(output)
        evidence = {
            "evaluation": "V19C-C-single-derived-candidate",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_audio_asset_id": str(source.id), "source_sha256": SOURCE_HASH,
            "source_qa_job_id": "440f4a86-4334-4a54-abc1-3d8ec66c9e25",
            "copy": COPY, "source_frames": len(pcm) // 2,
            "output_frames": len(edited) // 2, "sample_rate": 24_000,
            "inserted_boundary": insertion_audit, "quiet_cuts": cut_audit,
            "performance_plan": plan.model_dump(mode="json"),
            "output_path": str(output.resolve()), "output_sha256": output_hash,
            "limitations": "A source-specific punctuation edit, not provider word emphasis or a publishability judgment.",
        }
        manifest_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        imported = AudioImporter(db, args.data_root.resolve(), FFProbeAdapter(resolve_local_executable("ffprobe"))).import_path(
            output, source.authorization_reference, language=source.language,
        )
        if imported.content_hash != output_hash or imported.id == source.id:
            raise RuntimeError("V19C derived import is not independent")
        next_generation = {key: value for key, value in generation.items() if key not in {
            "qa", "qa_state", "human_review", "human_review_state", "recovery", "composition",
        }}
        next_generation.update({
            "provider": "derived_audio_edit", "model": "v19c-c-punctuation-hierarchy",
            "qa_state": "pending", "human_review_state": "pending",
            "derived_from_audio_asset_id": str(source.id),
            "performance_plan": plan.model_dump(mode="json"),
        })
        metadata = dict(imported.metadata)
        metadata["voice_generation"] = next_generation
        metadata["derived_audio_edit"] = {
            "manifest": str(manifest_path.resolve()), "source_audio_asset_id": str(source.id),
            "source_sha256": SOURCE_HASH, "output_sha256": output_hash,
            "inserted_boundary": insertion_audit, "quiet_cuts": cut_audit,
            "human_quality_status": "pending",
        }
        updated = imported.model_copy(update={"metadata": metadata})
        now = datetime.now(timezone.utc)
        qa_job = Job(
            id=uuid4(), project_id=PROJECT_ID, type=JobType.VERIFY_VOICE,
            idempotency_key="v19c-c-punctuation-hierarchy-fresh-full-master-qa",
            created_at=now, updated_at=now,
            payload=VoiceQaJobPayload(project_id=PROJECT_ID, narration_audio_id=updated.id, target_text=COPY),
        )
        with db.transaction():
            AudioAssetRepository(db).update(updated)
            persisted = JobRepository(db).create(qa_job)
        if persisted.id != qa_job.id:
            raise RuntimeError("V19C QA idempotency key already belongs to another job")
        print(json.dumps({"audio_asset_id": str(updated.id), "audio_path": updated.source_file,
                          "qa_job_id": str(qa_job.id), "manifest": str(manifest_path.resolve()),
                          "output_frames": len(edited) // 2}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
