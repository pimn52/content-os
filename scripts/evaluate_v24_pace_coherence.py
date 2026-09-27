"""One existing-audio V24 pace-coherence candidate; no provider inference.

Run prepare, the ordinary verify_voice worker twice, compose, the ordinary
verify_voice worker once, then verify-master. Never retune this candidate.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from uuid import UUID, uuid4


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api"))
sys.path.insert(0, str(ROOT / "scripts"))

from app.db import AudioAssetRepository, Database, JobRepository  # noqa: E402
from app.domain.models import Job, JobType, NarrationPause, NarrationPerformancePlan, VoiceQaJobPayload  # noqa: E402
from app.media import AudioImporter, FFProbeAdapter  # noqa: E402
from app.narration_performance import derive_narration_boundary_map  # noqa: E402
from app.runtime import resolve_local_executable  # noqa: E402
from app.voice_performance import (  # noqa: E402
    VoiceGenerationSpan, VoicePerformanceComposer, _leading_pcm_silence_ms,
    _trailing_pcm_silence_ms, plan_voice_performance_units,
)
from evaluate_v21_sentence_contour import COPY, PARTS, PROJECT_ID, SOURCE_ID  # noqa: E402
from evaluate_v23_a_sentence_composition import MANIFEST as V23_MANIFEST, read_pcm, sha256  # noqa: E402


EVIDENCE = ROOT / "content-os-data" / "evaluation-evidence" / "v24-pace-coherence"
MANIFEST = EVIDENCE / "v24-pace-coherence.json"
# One evaluation-only choice from observed A/V23 sentence timings, not a
# universal speech-rate setting or semantic Performance Plan realization.
TEMPO = (1.12, 1.0, 0.88)


def manifest_write(value: dict[str, object]) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def source_children(db: Database) -> tuple[dict, list]:
    v23 = json.loads(V23_MANIFEST.read_text(encoding="utf-8"))
    if v23.get("status") != "awaiting_u_review" or v23.get("copy") != COPY:
        raise ValueError("V23 source evidence is not complete")
    audios = AudioAssetRepository(db)
    children = [audios.get(UUID(item["audio_asset_id"])) for item in v23["children"]]
    master = audios.get(UUID(v23["master"]["audio_asset_id"]))
    if master is None or master.content_hash != v23["master"]["sha256"] or (master.metadata.get("voice_generation") or {}).get("qa_state") != "verified":
        raise ValueError("V23 master source QA/identity changed")
    for item, child, text in zip(v23["children"], children, PARTS, strict=True):
        if child is None or child.content_hash != item["sha256"] or sha256(Path(item["path"])) != item["sha256"]:
            raise ValueError("V23 child source file/hash changed")
        generation = child.metadata.get("voice_generation") or {}
        if generation.get("qa_state") != "verified" or generation.get("target_text") != text:
            raise ValueError("V23 child copy QA changed")
        if child.authorization_reference != master.authorization_reference:
            raise ValueError("V23 child authorization changed")
    return v23, children


def transform_excerpt(source: Path, target: Path, tempo: float) -> tuple[int, int]:
    if target.exists() or tempo == 1:
        raise ValueError("V24 output already exists or requested transform is identity")
    rate, before = read_pcm(source)
    command = [str(resolve_local_executable("ffmpeg")), "-nostdin", "-n", "-i", str(source),
               "-af", f"atempo={tempo:.2f}", "-ar", str(rate), "-ac", "1",
               "-c:a", "pcm_s16le", str(target)]
    result = subprocess.run(command, shell=False, capture_output=True, text=True, timeout=120, check=False)
    if result.returncode != 0 or not target.is_file():
        raise ValueError(f"V24 local tempo transform failed: {result.stderr[-400:]}")
    transformed_rate, after = read_pcm(target)
    before_frames, after_frames = len(before) // 2, len(after) // 2
    expected = before_frames / tempo
    if transformed_rate != rate or abs(after_frames - expected) > rate * 0.06:
        raise ValueError("V24 transformed WAV duration/sample rate out of bounds")
    return before_frames, after_frames


def import_pending(db: Database, path: Path, source, text: str, index: int, frames: tuple[int, int]) -> tuple[str, str]:
    imported = AudioImporter(db, ROOT / "content-os-data", FFProbeAdapter(resolve_local_executable("ffprobe"))).import_path(
        path, source.authorization_reference, language="zh")
    if imported.content_hash != sha256(path) or "voice_generation" in imported.metadata:
        raise ValueError("V24 transformed child import was not independent")
    metadata = dict(imported.metadata)
    metadata["voice_generation"] = {
        "provider": "derived_audio_tempo_evaluation", "model": "v24-ffmpeg-atempo",
        "project_id": str(PROJECT_ID), "target_text": text,
        "qa_state": "pending", "human_review_state": "pending",
        "source_audio_asset_id": str(source.id), "source_sha256": source.content_hash,
        "tempo_factor": TEMPO[index], "source_frames": frames[0], "output_frames": frames[1],
        "provider_plan_application": "unverified",
    }
    metadata["v24_evidence_manifest"] = str(MANIFEST)
    updated = imported.model_copy(update={"metadata": metadata})
    now = datetime.now(timezone.utc)
    job = Job(id=uuid4(), project_id=PROJECT_ID, type=JobType.VERIFY_VOICE,
              idempotency_key=f"v24-child-{index}-fresh-voice-qa", created_at=now, updated_at=now,
              payload=VoiceQaJobPayload(project_id=PROJECT_ID, narration_audio_id=updated.id, target_text=text))
    with db.transaction():
        AudioAssetRepository(db).update(updated)
        persisted = JobRepository(db).create(job)
        if persisted.id != job.id:
            raise ValueError("V24 child QA job key already used")
    return str(updated.id), str(job.id)


def prepare() -> None:
    if MANIFEST.exists():
        raise ValueError("V24 already prepared; one candidate only")
    with Database(ROOT / "content-os-data" / "content-os.sqlite3") as db:
        v23, sources = source_children(db)
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        children = []
        for index in (0, 2):
            source_path = Path(v23["children"][index]["path"])
            path = EVIDENCE / f"v24-child-{index}-tempo.wav"
            frames = transform_excerpt(source_path, path, TEMPO[index])
            audio_id, job_id = import_pending(db, path, sources[index], PARTS[index], index, frames)
            children.append({"index": index, "source_audio_asset_id": str(sources[index].id),
                             "path": str(path), "sha256": sha256(path), "audio_asset_id": audio_id,
                             "qa_job_id": job_id, "source_frames": frames[0], "output_frames": frames[1],
                             "tempo_factor": TEMPO[index]})
        manifest_write({"evaluation": "V24-existing-audio-pace-coherence", "status": "children_qa_pending",
                        "created_at": datetime.now(timezone.utc).isoformat(), "copy": COPY,
                        "tempo_factors": list(TEMPO), "v23_manifest": str(V23_MANIFEST),
                        "v23_master_audio_asset_id": v23["master"]["audio_asset_id"],
                        "unchanged_a_child_audio_asset_id": str(sources[1].id),
                        "unchanged_a_child_sha256": sources[1].content_hash,
                        "children": children,
                        "limitation": "Pitch-preserving time scaling is a local listening probe, not semantic plan execution or productized rhetorical control."})
        print(json.dumps(children, ensure_ascii=False))


def compose() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("status") != "children_qa_pending":
        raise ValueError("V24 children not ready or already composed")
    with Database(ROOT / "content-os-data" / "content-os.sqlite3") as db:
        v23, sources = source_children(db)
        audios = AudioAssetRepository(db)
        transformed = {item["index"]: audios.get(UUID(item["audio_asset_id"])) for item in manifest["children"]}
        takes = [transformed[0], sources[1], transformed[2]]
        if any(take is None or (take.metadata.get("voice_generation") or {}).get("qa_state") != "verified" for take in takes):
            raise ValueError("V24 new children need independent verified Voice QA")
        if any(take.metadata["voice_generation"]["target_text"] != text for take, text in zip(takes, PARTS, strict=True)):
            raise ValueError("V24 child target copy mismatch")
        if takes[1].content_hash != manifest["unchanged_a_child_sha256"]:
            raise ValueError("V24 causal sentence must remain sample-preserved")
        v20b = audios.get(SOURCE_ID)
        plan = NarrationPerformancePlan.model_validate(v20b.metadata["voice_generation"]["performance_plan"])
        units = plan_voice_performance_units(COPY, plan).units
        if len(units) != 4 or [unit.end_char for unit in units] != [8, 27, 44, len(COPY)]:
            raise ValueError("V24 semantic boundaries changed")
        spans = (VoiceGenerationSpan(0, 0, 8, PARTS[0], (units[0],)),
                 VoiceGenerationSpan(1, 8, 27, PARTS[1], (replace(units[1], pause_after=NarrationPause.BRIEF),)),
                 VoiceGenerationSpan(2, 27, len(COPY), PARTS[2], units[2:]))
        path = EVIDENCE / "v24-pace-coherence-master.wav"
        if path.exists():
            raise ValueError("V24 master already exists")
        result = VoicePerformanceComposer(resolve_local_executable("ffmpeg")).compose(
            takes, spans, path, boundary_map=derive_narration_boundary_map(COPY))
        joins = [_trailing_pcm_silence_ms(takes[i]) + result.pause_after_ms[i] + _leading_pcm_silence_ms(takes[i + 1]) for i in range(2)]
        if any(not 160 <= join <= 260 for join in joins):
            raise ValueError("V24 terminal join escaped reviewed short-pause range")
        imported = AudioImporter(db, ROOT / "content-os-data", FFProbeAdapter(resolve_local_executable("ffprobe"))).import_path(
            path, sources[1].authorization_reference, language="zh")
        if imported.content_hash != sha256(path) or "voice_generation" in imported.metadata:
            raise ValueError("V24 master import was not independent")
        metadata = dict(imported.metadata)
        metadata["voice_generation"] = {
            "provider": "verified_existing_audio_composition", "model": "v24-pace-coherence-evaluation",
            "project_id": str(PROJECT_ID), "target_text": COPY,
            "qa_state": "pending", "human_review_state": "pending",
            "performance_plan": plan.model_dump(mode="json"),
            "composition": {"child_audio_asset_ids": [str(take.id) for take in takes],
                            "span_boundaries": [0, 8, 27, len(COPY)],
                            "added_pause_after_ms": list(result.pause_after_ms),
                            "measured_total_join_ms": joins,
                            "provider_plan_application": "unverified"},
        }
        metadata["v24_evidence_manifest"] = str(MANIFEST)
        updated = imported.model_copy(update={"metadata": metadata})
        now = datetime.now(timezone.utc)
        job = Job(id=uuid4(), project_id=PROJECT_ID, type=JobType.VERIFY_VOICE,
                  idempotency_key="v24-fresh-master-voice-qa", created_at=now, updated_at=now,
                  payload=VoiceQaJobPayload(project_id=PROJECT_ID, narration_audio_id=updated.id, target_text=COPY))
        with db.transaction():
            audios.update(updated)
            persisted = JobRepository(db).create(job)
            if persisted.id != job.id:
                raise ValueError("V24 master QA job key already used")
        manifest["status"] = "master_qa_pending"
        manifest["master"] = {"path": str(path), "sha256": sha256(path), "audio_asset_id": str(updated.id),
                              "qa_job_id": str(job.id), "added_pause_after_ms": list(result.pause_after_ms),
                              "measured_total_join_ms": joins}
        manifest_write(manifest)
        print(json.dumps(manifest["master"], ensure_ascii=False))


def verify_master() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("status") != "master_qa_pending":
        raise ValueError("V24 master QA not pending")
    with Database(ROOT / "content-os-data" / "content-os.sqlite3") as db:
        master = AudioAssetRepository(db).get(UUID(manifest["master"]["audio_asset_id"]))
        if master is None or (master.metadata.get("voice_generation") or {}).get("qa_state") != "verified":
            raise ValueError("V24 fresh master Voice QA not verified")
        if master.content_hash != manifest["master"]["sha256"] or sha256(Path(manifest["master"]["path"])) != master.content_hash:
            raise ValueError("V24 master WAV hash changed")
        manifest["status"] = "awaiting_u_review"
        manifest["master"]["qa"] = master.metadata["voice_generation"]["qa"]
        manifest_write(manifest)
        print(json.dumps(manifest["master"], ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "compose", "verify-master"))
    {"prepare": prepare, "compose": compose, "verify-master": verify_master}[parser.parse_args().phase]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
