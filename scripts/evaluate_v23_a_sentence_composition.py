"""V23: one existing-audio comparison with A's intact causal sentence.

Phases: prepare -> ordinary Voice QA of three children -> compose -> ordinary
full-master Voice QA -> verify-master. No Voice provider inference occurs here.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import struct
import sys
from uuid import UUID, uuid4
import wave


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api"))
sys.path.insert(0, str(ROOT / "scripts"))

from app.db import AudioAssetRepository, Database, JobRepository  # noqa: E402
from app.domain.models import Job, JobType, NarrationPause, NarrationPerformancePlan, VoiceQaJobPayload  # noqa: E402
from app.media import AudioImporter, FFProbeAdapter  # noqa: E402
from app.narration_performance import derive_narration_boundary_map  # noqa: E402
from app.runtime import resolve_local_executable  # noqa: E402
from app.voice_performance import VoiceGenerationSpan, VoicePerformanceComposer, _leading_pcm_silence_ms, _trailing_pcm_silence_ms, plan_voice_performance_units  # noqa: E402
from evaluate_v21_sentence_contour import (  # noqa: E402
    COPY, PARTS, PROJECT_ID, SOURCE_HASH as V20B_HASH, SOURCE_ID as V20B_ID,
    assert_quiet_cut, extract_part, resolve_audio, sha256,
)


A_ID = UUID("aa0c666f-f976-44f5-97bc-ba154e9f6b83")
A_HASH = "e23e0fedba3b7096868182189e669e1d4814e88f401625f288f43eda95715aa1"
A_PATH = ROOT / "content-os-data" / "assets" / "audio-originals" / f"{A_HASH}.wav"
V20B_OPENING_END_MS = 2200
V20B_LATER_START_MS = 5750
A_START_TARGET_MS = 1850
A_END_TARGET_MS = 6090
EVIDENCE = ROOT / "content-os-data" / "evaluation-evidence" / "v23-a-sentence-composition"
MANIFEST = EVIDENCE / "v23-a-sentence-composition.json"


def read_pcm(path: Path) -> tuple[int, bytes]:
    with wave.open(str(path), "rb") as source:
        if (source.getnchannels(), source.getsampwidth(), source.getframerate(), source.getcomptype()) != (1, 2, 24000, "NONE"):
            raise ValueError("V23 requires mono PCM16 24 kHz input")
        return source.getframerate(), source.readframes(source.getnframes())


def choose_low_energy_zero_crossing(pcm: bytes, target_ms: int, *, radius_ms: int = 5) -> int:
    """Choose an acoustic cut, never infer copy correctness from its waveform."""
    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    target = target_ms * 24
    start, end = target - radius_ms * 24, target + radius_ms * 24
    if start < 240 or end > len(samples) - 240:
        raise ValueError("V23 cut window outside source")
    # Require the entire bounded region to be low energy. The A source has a
    # soft native noise floor, not digital silence, so this is deliberately
    # source-specific and stricter than simply finding one zero sample.
    region = samples[start:end]
    rms = math.sqrt(sum(value * value for value in region) / len(region)) / 32768
    if 20 * math.log10(max(rms, 1e-10)) >= -35:
        raise ValueError("V23 A sentence cut region contains acoustic content")
    candidates = [index for index in range(start + 1, end)
                  if samples[index - 1] * samples[index] <= 0]
    if not candidates:
        raise ValueError("V23 A sentence cut has no zero crossing")
    return min(candidates, key=lambda index: (abs(index - target),
                                              abs(samples[index - 1]) + abs(samples[index])))


def extract_sample_interval(source_path: Path, output: Path, start: int, end: int) -> None:
    if output.exists():
        raise ValueError("V23 A sentence excerpt already exists")
    rate, pcm = read_pcm(source_path)
    if not 0 <= start < end <= len(pcm) // 2:
        raise ValueError("V23 sample interval invalid")
    selected = pcm[start * 2:end * 2]
    with output.open("xb") as stream, wave.open(stream, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(selected)


def sources(db: Database):
    audios = AudioAssetRepository(db)
    a, v20b = audios.get(A_ID), audios.get(V20B_ID)
    if a is None or v20b is None or a.content_hash != A_HASH or v20b.content_hash != V20B_HASH:
        raise ValueError("V23 source AudioAsset identity changed")
    if sha256(A_PATH) != A_HASH or sha256(resolve_audio(v20b.source_file)) != V20B_HASH:
        raise ValueError("V23 source file hash mismatch")
    if a.authorization_reference != v20b.authorization_reference:
        raise ValueError("V23 source authorization differs")
    for source in (a, v20b):
        generation = source.metadata.get("voice_generation")
        if not isinstance(generation, dict) or generation.get("qa_state") != "verified" or generation.get("target_text") != COPY:
            raise ValueError("V23 source full-copy Voice QA invalid")
        if "".join(segment.text for segment in source.transcript_segments).strip() == "":
            raise ValueError("V23 source has no independent ASR transcript")
    if [segment.text for segment in a.transcript_segments][:3] != [
        "先別急著寫文案", "選題有沒有判斷", "決定觀眾會不會繼續聽",
    ]:
        raise ValueError("V23 A source sentence order changed")
    return a, v20b


def queue_qa(db: Database, audio_id: UUID, target_text: str, key: str) -> UUID:
    now = datetime.now(timezone.utc)
    job = Job(id=uuid4(), project_id=PROJECT_ID, type=JobType.VERIFY_VOICE,
              idempotency_key=key, created_at=now, updated_at=now,
              payload=VoiceQaJobPayload(project_id=PROJECT_ID, narration_audio_id=audio_id,
                                        target_text=target_text))
    persisted = JobRepository(db).create(job)
    if persisted.id != job.id:
        raise ValueError("V23 Voice QA job key already used")
    return job.id


def import_pending(db: Database, path: Path, target_text: str, source_id: UUID,
                   source_hash: str, cut: dict[str, int], key: str, authorization: str) -> tuple[str, str]:
    imported = AudioImporter(db, ROOT / "content-os-data", FFProbeAdapter(resolve_local_executable("ffprobe"))).import_path(
        path, authorization, language="zh")
    if imported.content_hash != sha256(path) or "voice_generation" in imported.metadata:
        raise ValueError("V23 excerpt import was not independent")
    metadata = dict(imported.metadata)
    metadata["voice_generation"] = {
        "provider": "derived_audio_excerpt", "model": "v23-sample-preserved-source-cut",
        "project_id": str(PROJECT_ID), "target_text": target_text,
        "qa_state": "pending", "human_review_state": "pending",
        "source_audio_asset_id": str(source_id), "source_sha256": source_hash,
        "cut_samples": cut,
    }
    metadata["v23_evidence_manifest"] = str(MANIFEST)
    updated = imported.model_copy(update={"metadata": metadata})
    with db.transaction():
        AudioAssetRepository(db).update(updated)
        qa_job_id = queue_qa(db, updated.id, target_text, key)
    return str(updated.id), str(qa_job_id)


def write_manifest(value: dict[str, object]) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def prepare() -> None:
    if MANIFEST.exists():
        raise ValueError("V23 already prepared; one candidate only")
    with Database(ROOT / "content-os-data" / "content-os.sqlite3") as db:
        a, v20b = sources(db)
        v20b_path = resolve_audio(v20b.source_file)
        assert_quiet_cut(v20b_path, V20B_OPENING_END_MS)
        assert_quiet_cut(v20b_path, V20B_LATER_START_MS)
        _, a_pcm = read_pcm(A_PATH)
        a_start = choose_low_energy_zero_crossing(a_pcm, A_START_TARGET_MS)
        a_end = choose_low_energy_zero_crossing(a_pcm, A_END_TARGET_MS)
        if not 1800 * 24 <= a_start < a_end < 6120 * 24:
            raise ValueError("V23 A sentence cut escaped its terminal interval")
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        outputs = (EVIDENCE / "v20b-opening.wav", EVIDENCE / "a-causal-sentence.wav",
                   EVIDENCE / "v20b-later-sentences.wav")
        if any(path.exists() for path in outputs):
            raise ValueError("V23 child WAV already exists")
        extract_part(v20b_path, outputs[0], 0, V20B_OPENING_END_MS)
        extract_sample_interval(A_PATH, outputs[1], a_start, a_end)
        extract_part(v20b_path, outputs[2], V20B_LATER_START_MS, None)
        _, v20b_pcm = read_pcm(v20b_path)
        cut_data = (
            {"start_sample": 0, "end_sample": V20B_OPENING_END_MS * 24},
            {"start_sample": a_start, "end_sample": a_end},
            {"start_sample": V20B_LATER_START_MS * 24, "end_sample": len(v20b_pcm) // 2},
        )
        children = []
        for index, (path, text, cut) in enumerate(zip(outputs, PARTS, cut_data)):
            parent = a if index == 1 else v20b
            audio_id, job_id = import_pending(db, path, text, parent.id, parent.content_hash,
                                               cut, f"v23-child-{index}-independent-qa",
                                               parent.authorization_reference)
            children.append({"index": index, "source_audio_asset_id": str(parent.id),
                             "target_text": text, "path": str(path), "sha256": sha256(path),
                             "cut_samples": cut, "audio_asset_id": audio_id, "qa_job_id": job_id})
        manifest = {"evaluation": "V23-A-causal-sentence-plus-V20B-joins",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "status": "children_qa_pending", "copy": COPY,
                    "a_source_audio_asset_id": str(a.id), "a_source_sha256": A_HASH,
                    "v20b_source_audio_asset_id": str(v20b.id), "v20b_source_sha256": V20B_HASH,
                    "authorization_reference": a.authorization_reference,
                    "a_cut_targets_ms": [A_START_TARGET_MS, A_END_TARGET_MS],
                    "a_cut_actual_samples": [a_start, a_end],
                    "children": children,
                    "limitation": "Evaluation-only sample-preserving A sentence; no new provider generation or verified in-take plan execution."}
        write_manifest(manifest)
        print(json.dumps({"children": children, "manifest": str(MANIFEST)}, ensure_ascii=False))


def correct_provenance() -> None:
    """Correct a known 3-sample rounded-duration metadata error, auditably."""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("status") != "children_qa_pending":
        raise ValueError("V23 child provenance is no longer pending")
    child = manifest["children"][2]
    with Database(ROOT / "content-os-data" / "content-os.sqlite3") as db:
        _, v20b = sources(db)
        _, pcm = read_pcm(resolve_audio(v20b.source_file))
        actual_end = len(pcm) // 2
        recorded_end = child["cut_samples"]["end_sample"]
        if recorded_end == actual_end:
            print("V23 child 2 cut provenance is already exact")
            return
        if recorded_end - actual_end != 3 or manifest.get("provenance_corrections"):
            raise ValueError("V23 unexpected cut provenance mismatch")
        asset = AudioAssetRepository(db).get(UUID(child["audio_asset_id"]))
        if asset is None or asset.content_hash != child["sha256"]:
            raise ValueError("V23 child 2 audio identity changed")
        generation = dict(asset.metadata["voice_generation"])
        if generation.get("cut_samples") != child["cut_samples"]:
            raise ValueError("V23 child 2 source cut metadata differs from manifest")
        correction = {"child_index": 2, "old_end_sample": recorded_end,
                      "actual_end_sample": actual_end,
                      "reason": "AudioAsset duration_ms rounded 236229 PCM frames to 9843 ms; audio/QA unchanged"}
        generation["cut_samples"] = {**generation["cut_samples"], "end_sample": actual_end}
        metadata = dict(asset.metadata)
        metadata["voice_generation"] = generation
        metadata["v23_provenance_correction"] = correction
        with db.transaction():
            AudioAssetRepository(db).update(asset.model_copy(update={"metadata": metadata}))
        child["cut_samples"]["end_sample"] = actual_end
        manifest["provenance_corrections"] = [correction]
        write_manifest(manifest)
        print(json.dumps(correction, ensure_ascii=False))


def compose() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("status") != "children_qa_pending":
        raise ValueError("V23 children not ready or master already composed")
    with Database(ROOT / "content-os-data" / "content-os.sqlite3") as db:
        _, v20b = sources(db)
        audios = AudioAssetRepository(db)
        takes = [audios.get(UUID(item["audio_asset_id"])) for item in manifest["children"]]
        if any(take is None or (take.metadata.get("voice_generation") or {}).get("qa_state") != "verified"
               for take in takes):
            raise ValueError("V23 every child needs independent verified Voice QA")
        if any(take.metadata["voice_generation"]["target_text"] != text for take, text in zip(takes, PARTS)):
            raise ValueError("V23 child target copy mismatch")
        plan = NarrationPerformancePlan.model_validate(v20b.metadata["voice_generation"]["performance_plan"])
        units = plan_voice_performance_units(COPY, plan).units
        if len(units) != 4 or [unit.end_char for unit in units] != [8, 27, 44, len(COPY)]:
            raise ValueError("V23 semantic unit boundaries changed")
        spans = (VoiceGenerationSpan(0, 0, 8, PARTS[0], (units[0],)),
                 VoiceGenerationSpan(1, 8, 27, PARTS[1], (replace(units[1], pause_after=NarrationPause.BRIEF),)),
                 VoiceGenerationSpan(2, 27, len(COPY), PARTS[2], units[2:]))
        master_path = EVIDENCE / "v23-a-sentence-master.wav"
        if master_path.exists():
            raise ValueError("V23 master WAV already exists")
        composition = VoicePerformanceComposer(resolve_local_executable("ffmpeg")).compose(
            takes, spans, master_path, boundary_map=derive_narration_boundary_map(COPY))
        total_joins = [
            _trailing_pcm_silence_ms(takes[index]) + composition.pause_after_ms[index]
            + _leading_pcm_silence_ms(takes[index + 1])
            for index in range(2)
        ]
        if total_joins[0] > 300 or total_joins[1] > 260:
            raise ValueError("V23 actual terminal join exceeds user-reviewed short-pause range")
        imported = AudioImporter(db, ROOT / "content-os-data", FFProbeAdapter(resolve_local_executable("ffprobe"))).import_path(
            master_path, v20b.authorization_reference, language="zh")
        if imported.content_hash != sha256(master_path) or "voice_generation" in imported.metadata:
            raise ValueError("V23 master import was not independent")
        metadata = dict(imported.metadata)
        metadata["voice_generation"] = {
            "provider": "verified_existing_audio_composition", "model": "v23-a-sentence-v20b-joins",
            "project_id": str(PROJECT_ID), "target_text": COPY,
            "qa_state": "pending", "human_review_state": "pending",
            "performance_plan": plan.model_dump(mode="json"),
            "composition": {"child_audio_asset_ids": [str(take.id) for take in takes],
                            "span_boundaries": [0, 8, 27, len(COPY)],
                            "added_pause_after_ms": list(composition.pause_after_ms),
                            "measured_total_join_ms": total_joins,
                            "char_27_local_pause_choice": "brief; user-reviewed short join",
                            "provider_plan_application": "unverified"},
        }
        metadata["v23_evidence_manifest"] = str(MANIFEST)
        updated = imported.model_copy(update={"metadata": metadata})
        with db.transaction():
            audios.update(updated)
            job_id = queue_qa(db, updated.id, COPY, "v23-fresh-master-voice-qa")
        manifest.update({"status": "master_qa_pending", "master": {"path": str(master_path),
                         "sha256": sha256(master_path), "audio_asset_id": str(updated.id),
                         "qa_job_id": str(job_id), "added_pause_after_ms": list(composition.pause_after_ms),
                         "measured_total_join_ms": total_joins}})
        write_manifest(manifest)
        print(json.dumps(manifest["master"], ensure_ascii=False))


def verify_master() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("status") != "master_qa_pending":
        raise ValueError("V23 master QA not pending")
    with Database(ROOT / "content-os-data" / "content-os.sqlite3") as db:
        master = AudioAssetRepository(db).get(UUID(manifest["master"]["audio_asset_id"]))
        if master is None or (master.metadata.get("voice_generation") or {}).get("qa_state") != "verified":
            raise ValueError("V23 fresh master Voice QA not verified")
        if sha256(Path(manifest["master"]["path"])) != manifest["master"]["sha256"]:
            raise ValueError("V23 master WAV hash changed")
        manifest["status"] = "awaiting_u_review"
        manifest["master"]["qa"] = master.metadata["voice_generation"]["qa"]
        write_manifest(manifest)
        print(json.dumps(manifest["master"], ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "correct-provenance", "compose", "verify-master"))
    {"prepare": prepare, "correct-provenance": correct_provenance,
     "compose": compose, "verify-master": verify_master}[parser.parse_args().phase]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
