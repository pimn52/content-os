"""One bounded V20A focus/landing edit of the QA-verified V19E C master.

This evaluation bridge tests a controlled acoustic hypothesis. Gain and a
local pitch-preserving tempo filter are not proof of semantic word-emphasis
support by OmniVoice or of publishable delivery; U-Voice remains decisive.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
from uuid import UUID, uuid4
import wave


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "services" / "api"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.db import AudioAssetRepository, Database, JobRepository  # noqa: E402
from app.domain.models import (  # noqa: E402
    Job, JobType, NarrationEmphasis, NarrationPace,
    NarrationPerformanceCue, NarrationPerformanceCueKind,
    NarrationPerformancePlan, VoiceQaJobPayload,
)
from app.media import AudioImporter, FFProbeAdapter  # noqa: E402
from app.narration_performance import validate_narration_performance_plan  # noqa: E402
from app.runtime import resolve_local_executable  # noqa: E402
from evaluate_c_pause_salvage import COPY, PROJECT_ID, sha256  # noqa: E402


SOURCE_ID = UUID("041ca938-6d0c-4346-8b2c-c1652d326b40")
SOURCE_HASH = "1b154a71df688e2701c491888425d5ee15e0e0fc198f5116c7c5775d0d7408c2"
SAMPLE_RATE = 24_000
GAIN_WINDOWS = (
    (3380, 3550, 3.0, "断 core: reinforce the weak end of 判断"),
    (3960, 4460, -2.0, "first 决定: reduce the accent jump after 判断"),
    (8960, 9240, 1.0, "结论: modest concept landing"),
)
GAIN_RAMP_MS = 20
TAIL_START_MS = 9460  # measured valley before 下 onset; retain all of 落
TAIL_END_MS = 9670
TAIL_TEMPO = 0.65
TAIL_CROSSFADE_MS = 10


def plan_with_landing_cues(raw: dict[str, object]) -> NarrationPerformancePlan:
    source = NarrationPerformancePlan.model_validate(raw)
    validate_narration_performance_plan(source, COPY)
    conclusion = COPY.index("结论")
    landing = COPY.index("落下")
    cues = [
        NarrationPerformanceCue(
            kind=NarrationPerformanceCueKind.EMPHASIS,
            start_char=conclusion, end_char=conclusion + 2,
            emphasis=NarrationEmphasis.STRONG,
            note="U-Voice: let the conclusion carry the final claim",
        ),
        NarrationPerformanceCue(
            kind=NarrationPerformanceCueKind.PACE,
            start_char=landing, end_char=landing + 2,
            pace=NarrationPace.MEASURED,
            note="U-Voice: a controlled falling tail, not an extra silence",
        ),
    ]
    values = source.model_dump(mode="json")
    values["cues"] = [*values["cues"], *(cue.model_dump(mode="json") for cue in cues)]
    values["evidence_refs"] = [*values["evidence_refs"], "v20a-user-focus-and-tail-review"]
    result = NarrationPerformancePlan.model_validate(values)
    validate_narration_performance_plan(result, COPY)
    return result


def apply_gain_windows(
    pcm: bytes, *, windows: tuple[tuple[int, int, float, str], ...], ramp_ms: int,
) -> tuple[bytes, list[dict[str, object]]]:
    """Apply bounded, smooth local gain without clipping or changing timing."""
    if len(pcm) % 2 or ramp_ms <= 0:
        raise ValueError("invalid PCM or gain ramp")
    values = list(struct.unpack(f"<{len(pcm) // 2}h", pcm))
    audit: list[dict[str, object]] = []
    previous = 0
    ramp = ramp_ms * SAMPLE_RATE // 1000
    for start_ms, end_ms, gain_db, rationale in windows:
        start, end = start_ms * SAMPLE_RATE // 1000, end_ms * SAMPLE_RATE // 1000
        if start < previous or end > len(values) or end - start <= 2 * ramp or abs(gain_db) > 4:
            raise ValueError("gain windows must be ordered, separate and conservatively bounded")
        peak_before = max(abs(sample) for sample in values[start:end])
        for index in range(start, end):
            edge = min(1.0, (index - start) / ramp, (end - index - 1) / ramp)
            multiplier = 10 ** ((gain_db * edge) / 20)
            adjusted = round(values[index] * multiplier)
            if abs(adjusted) > 32440:  # ~ -0.09 dBFS hard safety ceiling
                raise ValueError("gain edit would clip a voiced sample")
            values[index] = adjusted
        audit.append({
            "start_ms": start_ms, "end_ms": end_ms,
            "start_sample": start, "end_sample": end,
            "gain_db": gain_db, "ramp_ms": ramp_ms, "rationale": rationale,
            "peak_before": peak_before,
            "peak_after": max(abs(sample) for sample in values[start:end]),
        })
        previous = end
    return struct.pack(f"<{len(values)}h", *values), audit


def stretch_final_syllable(
    pcm: bytes, *, start_ms: int, end_ms: int, tempo: float, crossfade_ms: int,
    ffmpeg: str,
) -> tuple[bytes, dict[str, object]]:
    """Use installed local FFmpeg atempo on only the aligned final syllable."""
    if len(pcm) % 2 or not 0.5 <= tempo < 1 or not 0 < crossfade_ms < end_ms - start_ms:
        raise ValueError("invalid local tail edit")
    start, end = start_ms * SAMPLE_RATE // 1000, end_ms * SAMPLE_RATE // 1000
    fade = crossfade_ms * SAMPLE_RATE // 1000
    if start < fade or end > len(pcm) // 2:
        raise ValueError("tail window exceeds source")
    source_tail = pcm[start * 2:end * 2]
    with tempfile.TemporaryDirectory(prefix="content-os-v20a-tail-") as temp_dir:
        input_path = Path(temp_dir) / "input.wav"
        output_path = Path(temp_dir) / "output.wav"
        with wave.open(str(input_path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(SAMPLE_RATE)
            handle.writeframes(source_tail)
        completed = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(input_path),
             "-af", f"atempo={tempo}", "-ar", str(SAMPLE_RATE), "-ac", "1",
             "-c:a", "pcm_s16le", str(output_path)],
            capture_output=True, text=True, check=False, timeout=120,
        )
        if completed.returncode != 0 or not output_path.is_file():
            raise ValueError("local final-syllable tempo edit failed")
        with wave.open(str(output_path), "rb") as handle:
            if (handle.getnchannels(), handle.getsampwidth(), handle.getframerate()) != (1, 2, SAMPLE_RATE):
                raise ValueError("local tempo output format mismatch")
            transformed = handle.readframes(handle.getnframes())
    if not len(source_tail) < len(transformed) < len(source_tail) * 2 or len(transformed) % 2:
        raise ValueError("local tempo output duration is outside the bounded tail range")
    prefix = list(struct.unpack(f"<{start}h", pcm[:start * 2]))
    slowed = list(struct.unpack(f"<{len(transformed) // 2}h", transformed))
    suffix = list(struct.unpack(f"<{len(pcm) // 2 - end}h", pcm[end * 2:]))
    if len(slowed) <= fade * 2 or len(prefix) <= fade:
        raise ValueError("local tempo output is too short for safe crossfade")
    joined = prefix[:-fade]
    for index in range(fade):
        share = (index + 1) / (fade + 1)
        mixed = round(prefix[-fade + index] * (1 - share) + slowed[index] * share)
        if abs(mixed) > 32440:
            raise ValueError("tail crossfade would clip")
        joined.append(mixed)
    joined.extend(slowed[fade:])
    joined.extend(suffix)
    # The transformed tail itself fades to quiet. Retained source suffix is
    # quiet at this exact window; reject a discontinuity instead of hiding it.
    if abs(joined[len(prefix) - fade + len(slowed) - 1] - suffix[0]) > 2500:
        raise ValueError("tail-to-source join is discontinuous")
    result = struct.pack(f"<{len(joined)}h", *joined)
    return result, {
        "start_ms": start_ms, "end_ms": end_ms,
        "start_sample": start, "end_sample": end,
        "tempo": tempo, "crossfade_ms": crossfade_ms,
        "source_tail_frames": len(source_tail) // 2,
        "transformed_tail_frames": len(transformed) // 2,
        "output_frame_delta": len(joined) - len(pcm) // 2,
        "rationale": "locally measured 下 tail; rhythm LAND and user-directed measured closing pace",
        "limitation": "Pitch-preserving tempo is an evaluation edit, not verified provider semantic emphasis.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=REPO_ROOT / "content-os-data" / "content-os.sqlite3")
    parser.add_argument("--data-root", type=Path, default=REPO_ROOT / "content-os-data")
    args = parser.parse_args()
    output_dir = args.data_root.resolve() / "evaluation-evidence" / "v20a-c-focus-landing"
    output = output_dir / "v20a-c-focus-landing.wav"
    manifest_path = output_dir / "v20a-c-focus-landing.json"
    if output.exists() or manifest_path.exists():
        raise SystemExit("V20A candidate already exists; no second settings permitted")

    with Database(args.database.resolve()) as db:
        source = AudioAssetRepository(db).get(SOURCE_ID)
        generation = None if source is None else source.metadata.get("voice_generation")
        if source is None or source.content_hash != SOURCE_HASH or not isinstance(generation, dict):
            raise SystemExit("V20A source identity mismatch")
        if generation.get("qa_state") != "verified" or generation.get("target_text") != COPY:
            raise SystemExit("V20A source copy or QA invalid")
        path = Path(source.source_file)
        if not path.is_file() or sha256(path) != SOURCE_HASH:
            raise SystemExit("V20A source file hash mismatch")
        with wave.open(str(path), "rb") as handle:
            if (handle.getnchannels(), handle.getsampwidth(), handle.getframerate(), handle.getcomptype()) != (1, 2, SAMPLE_RATE, "NONE"):
                raise SystemExit("V20A source PCM format mismatch")
            pcm = handle.readframes(handle.getnframes())
        plan = plan_with_landing_cues(generation["performance_plan"])
        balanced, gain_audit = apply_gain_windows(pcm, windows=GAIN_WINDOWS, ramp_ms=GAIN_RAMP_MS)
        rendered, tail_audit = stretch_final_syllable(
            balanced, start_ms=TAIL_START_MS, end_ms=TAIL_END_MS,
            tempo=TAIL_TEMPO, crossfade_ms=TAIL_CROSSFADE_MS,
            ffmpeg=resolve_local_executable("ffmpeg"),
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        with output.open("xb") as stream, wave.open(stream, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(SAMPLE_RATE)
            handle.writeframes(rendered)
        output_hash = sha256(output)
        evidence = {
            "evaluation": "V20A-C-single-focus-and-landing-candidate",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_audio_asset_id": str(source.id), "source_sha256": SOURCE_HASH,
            "source_qa_job_id": "89b3d9e5-e6c7-4573-b492-498657d9c321",
            "copy": COPY, "source_frames": len(pcm) // 2,
            "output_frames": len(rendered) // 2, "sample_rate": SAMPLE_RATE,
            "gain_windows": gain_audit, "tail_edit": tail_audit,
            "performance_plan": plan.model_dump(mode="json"),
            "output_path": str(output.resolve()), "output_sha256": output_hash,
            "limitations": "Local evaluation only; no OmniVoice plan-application receipt or verified word-level focus control.",
        }
        manifest_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        imported = AudioImporter(db, args.data_root.resolve(), FFProbeAdapter(resolve_local_executable("ffprobe"))).import_path(
            output, source.authorization_reference, language=source.language,
        )
        if imported.content_hash != output_hash or imported.id == source.id:
            raise RuntimeError("V20A derived import is not independent")
        next_generation = {key: value for key, value in generation.items() if key not in {
            "qa", "qa_state", "human_review", "human_review_state", "recovery", "composition",
        }}
        next_generation.update({
            "provider": "derived_audio_edit", "model": "v20a-c-focus-landing",
            "qa_state": "pending", "human_review_state": "pending",
            "derived_from_audio_asset_id": str(source.id),
            "performance_plan": plan.model_dump(mode="json"),
        })
        metadata = dict(imported.metadata)
        metadata["voice_generation"] = next_generation
        metadata["derived_audio_edit"] = {
            "manifest": str(manifest_path.resolve()), "source_audio_asset_id": str(source.id),
            "source_sha256": SOURCE_HASH, "output_sha256": output_hash,
            "gain_windows": gain_audit, "tail_edit": tail_audit,
            "human_quality_status": "pending",
        }
        updated = imported.model_copy(update={"metadata": metadata})
        now = datetime.now(timezone.utc)
        qa_job = Job(
            id=uuid4(), project_id=PROJECT_ID, type=JobType.VERIFY_VOICE,
            idempotency_key="v20a-c-focus-landing-fresh-full-master-qa",
            created_at=now, updated_at=now,
            payload=VoiceQaJobPayload(project_id=PROJECT_ID, narration_audio_id=updated.id, target_text=COPY),
        )
        with db.transaction():
            AudioAssetRepository(db).update(updated)
            persisted = JobRepository(db).create(qa_job)
        if persisted.id != qa_job.id:
            raise RuntimeError("V20A QA idempotency key already belongs to another job")
        print(json.dumps({"audio_asset_id": str(updated.id), "audio_path": updated.source_file,
                          "qa_job_id": str(qa_job.id), "manifest": str(manifest_path.resolve()),
                          "output_frames": len(rendered) // 2}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
