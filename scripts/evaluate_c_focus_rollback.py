"""V20B: preserve V20A's liked landing without its rejected first-clause gains.

This one candidate derives from the untouched V19E master, never by trying to
reverse V20A's lossy local gain edits. It remains evaluation-only until U-Voice.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
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
from evaluate_c_focus_landing import (  # noqa: E402
    COPY, GAIN_RAMP_MS, SAMPLE_RATE, SOURCE_HASH, SOURCE_ID,
    TAIL_CROSSFADE_MS, TAIL_END_MS, TAIL_START_MS, TAIL_TEMPO,
    apply_gain_windows, plan_with_landing_cues, stretch_final_syllable,
)
from evaluate_c_pause_salvage import PROJECT_ID, sha256  # noqa: E402


LANDING_ONLY_WINDOW = ((8960, 9240, 1.0, "retain the U-Voice-favored 结论 landing"),)


def render_landing_only(pcm: bytes, *, ffmpeg: str) -> tuple[bytes, list[dict[str, object]], dict[str, object]]:
    lifted, gain_audit = apply_gain_windows(pcm, windows=LANDING_ONLY_WINDOW, ramp_ms=GAIN_RAMP_MS)
    rendered, tail_audit = stretch_final_syllable(
        lifted, start_ms=TAIL_START_MS, end_ms=TAIL_END_MS,
        tempo=TAIL_TEMPO, crossfade_ms=TAIL_CROSSFADE_MS, ffmpeg=ffmpeg,
    )
    return rendered, gain_audit, tail_audit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=REPO_ROOT / "content-os-data" / "content-os.sqlite3")
    parser.add_argument("--data-root", type=Path, default=REPO_ROOT / "content-os-data")
    args = parser.parse_args()
    output_dir = args.data_root.resolve() / "evaluation-evidence" / "v20b-c-landing-only"
    output = output_dir / "v20b-c-landing-only.wav"
    manifest_path = output_dir / "v20b-c-landing-only.json"
    if output.exists() or manifest_path.exists():
        raise SystemExit("V20B candidate already exists; no second settings permitted")

    with Database(args.database.resolve()) as db:
        source = AudioAssetRepository(db).get(SOURCE_ID)
        generation = None if source is None else source.metadata.get("voice_generation")
        if source is None or source.content_hash != SOURCE_HASH or not isinstance(generation, dict):
            raise SystemExit("V20B V19E source identity mismatch")
        if generation.get("qa_state") != "verified" or generation.get("target_text") != COPY:
            raise SystemExit("V20B V19E source copy or QA invalid")
        path = Path(source.source_file)
        if not path.is_file() or sha256(path) != SOURCE_HASH:
            raise SystemExit("V20B source file hash mismatch")
        with wave.open(str(path), "rb") as handle:
            if (handle.getnchannels(), handle.getsampwidth(), handle.getframerate(), handle.getcomptype()) != (1, 2, SAMPLE_RATE, "NONE"):
                raise SystemExit("V20B source PCM format mismatch")
            pcm = handle.readframes(handle.getnframes())
        plan = plan_with_landing_cues(generation["performance_plan"])
        rendered, gain_audit, tail_audit = render_landing_only(pcm, ffmpeg=resolve_local_executable("ffmpeg"))
        # The first clause is exactly the accepted V19E source, not merely
        # similar in measured level. Its original 40 ms comma gap is retained.
        assert rendered[:8960 * SAMPLE_RATE // 1000 * 2] == pcm[:8960 * SAMPLE_RATE // 1000 * 2]
        output_dir.mkdir(parents=True, exist_ok=True)
        with output.open("xb") as stream, wave.open(stream, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(SAMPLE_RATE)
            handle.writeframes(rendered)
        output_hash = sha256(output)
        evidence = {
            "evaluation": "V20B-C-single-rejected-focus-rollback",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_audio_asset_id": str(source.id), "source_sha256": SOURCE_HASH,
            "source_qa_job_id": "89b3d9e5-e6c7-4573-b492-498657d9c321",
            "rejected_comparison_audio_asset_id": "03c3cc12-a0a3-4c55-996f-c190babc182a",
            "copy": COPY, "source_frames": len(pcm) // 2,
            "output_frames": len(rendered) // 2, "sample_rate": SAMPLE_RATE,
            "retained_landing_gain": gain_audit, "retained_tail_edit": tail_audit,
            "first_clause_unchanged_through_ms": 8960,
            "performance_plan": plan.model_dump(mode="json"),
            "output_path": str(output.resolve()), "output_sha256": output_hash,
            "limitations": "Evaluation-only gain/tempo; no phrase-level intonation or provider plan application.",
        }
        manifest_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        imported = AudioImporter(db, args.data_root.resolve(), FFProbeAdapter(resolve_local_executable("ffprobe"))).import_path(
            output, source.authorization_reference, language=source.language,
        )
        if imported.content_hash != output_hash or imported.id == source.id:
            raise RuntimeError("V20B derived import is not independent")
        next_generation = {key: value for key, value in generation.items() if key not in {
            "qa", "qa_state", "human_review", "human_review_state", "recovery", "composition",
        }}
        next_generation.update({
            "provider": "derived_audio_edit", "model": "v20b-c-landing-only",
            "qa_state": "pending", "human_review_state": "pending",
            "derived_from_audio_asset_id": str(source.id),
            "performance_plan": plan.model_dump(mode="json"),
        })
        metadata = dict(imported.metadata)
        metadata["voice_generation"] = next_generation
        metadata["derived_audio_edit"] = {
            "manifest": str(manifest_path.resolve()), "source_audio_asset_id": str(source.id),
            "source_sha256": SOURCE_HASH, "output_sha256": output_hash,
            "retained_landing_gain": gain_audit, "retained_tail_edit": tail_audit,
            "first_clause_unchanged_through_ms": 8960,
            "human_quality_status": "pending",
        }
        updated = imported.model_copy(update={"metadata": metadata})
        now = datetime.now(timezone.utc)
        qa_job = Job(
            id=uuid4(), project_id=PROJECT_ID, type=JobType.VERIFY_VOICE,
            idempotency_key="v20b-c-landing-only-fresh-full-master-qa",
            created_at=now, updated_at=now,
            payload=VoiceQaJobPayload(project_id=PROJECT_ID, narration_audio_id=updated.id, target_text=COPY),
        )
        with db.transaction():
            AudioAssetRepository(db).update(updated)
            persisted = JobRepository(db).create(qa_job)
        if persisted.id != qa_job.id:
            raise RuntimeError("V20B QA idempotency key already belongs to another job")
        print(json.dumps({"audio_asset_id": str(updated.id), "audio_path": updated.source_file,
                          "qa_job_id": str(qa_job.id), "manifest": str(manifest_path.resolve()),
                          "output_frames": len(rendered) // 2}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
