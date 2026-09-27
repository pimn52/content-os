"""One bounded V21 OmniVoice sentence-contour evaluation, never a general renderer.

Run phases in order: generate, prepare, compose. Generation is one durably
metered local provider call. Each imported child and the final master receives
its own normal Voice QA job; use the ordinary worker between phases.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import struct
import subprocess
import sys
from uuid import UUID, uuid4
import wave


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api"))

from app.budget import ProviderCallLedger, provider_call_input_digest  # noqa: E402
from app.db import AssetRepository, AudioAssetRepository, ClipRepository, Database, JobRepository, VoiceProfileRepository  # noqa: E402
from app.domain.models import CostCategory, Job, JobType, NarrationPause, UsageCost, VoiceQaJobPayload  # noqa: E402
from app.media import AudioImporter, FFProbeAdapter  # noqa: E402
from app.narration_performance import derive_narration_boundary_map  # noqa: E402
from app.runtime import resolve_local_executable  # noqa: E402
from app.voice_performance import VoiceGenerationSpan, VoicePerformanceComposer, _leading_pcm_silence_ms, _trailing_pcm_silence_ms, plan_voice_performance_units, select_voice_reference_windows  # noqa: E402


PROJECT_ID = UUID("161996a7-2607-4a4b-9247-d305537ce839")
PROFILE_ID = UUID("29294db2-d3c6-45c2-b467-6a02e853c826")
ORIGINAL_SPAN_ID = UUID("b3630633-a709-4810-9441-4c8628e68419")
SOURCE_ID = UUID("8c7d47a6-480d-48c6-a912-18559ab6e0c1")
SOURCE_HASH = "0723ff6a1fe3a815b9eee1f5af2d10bb7d03d2cdb1bed8ad305f48752880ae1a"
COPY = "先别急着写文案。选题有没有判断，决定观众会不会继续听。但是，结构决定这句话能不能被听懂。最后，让结论落下。"
PARTS = (COPY[:8], COPY[8:27], COPY[27:])
REFERENCE_START_MS, REFERENCE_END_MS = 240, 6240
FIRST_END_MS, LATER_START_MS = 2200, 5750
MODEL = Path("C:/Users/ASUS/.cache/huggingface/hub/models--k2-fsa--OmniVoice/snapshots/c5fdb5ccb189668d56333f77ba2629f4cd7535f4")
RUNTIME = ROOT / "content-os-data" / "omnivoice-runtime" / "Scripts" / "python.exe"
EVIDENCE = ROOT / "content-os-data" / "evaluation-evidence" / "v21-sentence-contour"
MANIFEST = EVIDENCE / "v21-sentence-contour.json"
CALL_KEY = "v21-causal-sentence-one-inference-cpu"
CUDA_LAUNCH_FAILURE = EVIDENCE / "v21-cuda-pre-inference-failure.json"
RUN_ID = "v21"
TIMEOUT_SECONDS = 900


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def resolve_audio(path: str) -> Path:
    value = Path(path)
    return value if value.is_absolute() else ROOT / value


def write_manifest(value: dict[str, object]) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def checked_sources(db: Database):
    audios = AudioAssetRepository(db)
    original, source = audios.get(ORIGINAL_SPAN_ID), audios.get(SOURCE_ID)
    profile = VoiceProfileRepository(db).get(PROFILE_ID)
    if original is None or source is None or profile is None or not profile.consent.confirmed:
        raise ValueError("V21 source asset or authorized profile unavailable")
    if (original.metadata.get("voice_generation") or {}).get("qa_state") != "verified" or (original.metadata.get("voice_generation") or {}).get("target_text") != "".join(PARTS[:2]):
        raise ValueError("V21 original C first span QA or copy changed")
    if source.content_hash != SOURCE_HASH or (source.metadata.get("voice_generation") or {}).get("qa_state") != "verified" or (source.metadata.get("voice_generation") or {}).get("target_text") != COPY:
        raise ValueError("V21 full-copy V20B source QA or identity changed")
    if sha256(resolve_audio(source.source_file)) != SOURCE_HASH:
        raise ValueError("V21 source WAV hash changed")
    if source.authorization_reference != profile.consent.authorization_reference:
        raise ValueError("V21 authorization provenance mismatch")
    clips = [ClipRepository(db).get(item) for item in profile.reference_clip_ids]
    if any(item is None for item in clips):
        raise ValueError("V21 authorized reference clip unavailable")
    assets = AssetRepository(db)
    related = {asset.id: asset for clip in clips if clip is not None for asset in [assets.get(clip.asset_id)] if asset is not None}
    windows = select_voice_reference_windows(clips, related, data_root=ROOT / "content-os-data")
    if not windows or (windows[0].start_ms, windows[0].end_ms) != (REFERENCE_START_MS, REFERENCE_END_MS):
        raise ValueError("V21 reference window changed")
    if not RUNTIME.is_file() or not MODEL.is_dir():
        raise ValueError("V21 local OmniVoice runtime/model unavailable")
    return source, profile, windows[0]


def extract_part(path: Path, output: Path, start_ms: int, end_ms: int | None) -> None:
    if output.exists():
        raise ValueError(f"V21 excerpt already exists: {output}")
    with wave.open(str(path), "rb") as source:
        if (source.getnchannels(), source.getsampwidth(), source.getframerate(), source.getcomptype()) != (1, 2, 24000, "NONE"):
            raise ValueError("V21 source requires mono PCM16 24 kHz")
        end_frame = source.getnframes() if end_ms is None else end_ms * 24
        if not 0 <= start_ms * 24 < end_frame <= source.getnframes():
            raise ValueError("V21 excerpt bounds invalid")
        source.setpos(start_ms * 24)
        pcm = source.readframes(end_frame - start_ms * 24)
    with output.open("xb") as stream, wave.open(stream, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(24000)
        target.writeframes(pcm)


def assert_quiet_cut(path: Path, cut_ms: int) -> None:
    """Check a fixed seam is actually quiet; never infer spoken copy from PCM."""
    with wave.open(str(path), "rb") as source:
        if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (1, 2, 24000):
            raise ValueError("V21 seam requires mono PCM16 24 kHz")
        if not 20 <= cut_ms <= source.getnframes() // 24 - 20:
            raise ValueError("V21 seam outside source audio")
        source.setpos((cut_ms - 10) * 24)
        pcm = source.readframes(20 * 24)
    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples)) / 32768
    if 20 * math.log10(max(rms, 1e-10)) >= -45:
        raise ValueError("V21 terminal seam contains acoustic content")


def queue_qa(db: Database, audio_id: UUID, target: str, key: str) -> UUID:
    now = datetime.now(timezone.utc)
    job = Job(id=uuid4(), project_id=PROJECT_ID, type=JobType.VERIFY_VOICE,
              idempotency_key=key, created_at=now, updated_at=now,
              payload=VoiceQaJobPayload(project_id=PROJECT_ID, narration_audio_id=audio_id, target_text=target))
    persisted = JobRepository(db).create(job)
    if persisted.id != job.id:
        raise ValueError("V21 Voice QA idempotency key already used")
    return job.id


def import_child(db: Database, path: Path, target: str, metadata: dict[str, object], key: str, authorization: str) -> tuple[str, str]:
    imported = AudioImporter(db, ROOT / "content-os-data", FFProbeAdapter(resolve_local_executable("ffprobe"))).import_path(path, authorization, language="zh")
    if imported.content_hash != sha256(path) or "voice_generation" in imported.metadata:
        raise ValueError("V21 child import was not independent")
    meta = dict(imported.metadata)
    meta["voice_generation"] = {"project_id": str(PROJECT_ID), "target_text": target,
                                "qa_state": "pending", "human_review_state": "pending", **metadata}
    meta["sentence_contour_evidence_manifest"] = str(MANIFEST)
    updated = imported.model_copy(update={"metadata": meta})
    with db.transaction():
        AudioAssetRepository(db).update(updated)
        job_id = queue_qa(db, updated.id, target, key)
    return str(updated.id), str(job_id)


def generate() -> None:
    if MANIFEST.exists():
        previous = json.loads(MANIFEST.read_text(encoding="utf-8"))
        if (RUN_ID != "v21" or previous.get("status") != "inference_failed" or previous.get("device") != "cuda"
                or (EVIDENCE / "causal-sentence.wav").exists() or CUDA_LAUNCH_FAILURE.exists()):
            raise ValueError("V21 generation already attempted; no blind retry")
        # CUDA failed during model load before inference. Preserve that
        # terminal provider-call provenance, then use the original worker's
        # conservative CPU default for the sole actual generation attempt.
        MANIFEST.replace(CUDA_LAUNCH_FAILURE)
    with Database(ROOT / "content-os-data" / "content-os.sqlite3") as db:
        source, profile, window = checked_sources(db)
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        reference = EVIDENCE / "reference-240-6240.wav"
        generated = EVIDENCE / "causal-sentence.wav"
        if generated.exists():
            raise ValueError("V21 generation output already exists")
        if not reference.exists():
            ffmpeg = resolve_local_executable("ffmpeg")
            extraction = subprocess.run([ffmpeg, "-y", "-ss", "0.240", "-i", window.source_file,
                                         "-t", "6.000", "-vn", "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", str(reference)],
                                        check=False, capture_output=True, timeout=120)
            if extraction.returncode or not reference.is_file():
                raise ValueError("V21 authorized reference extraction failed")
        call_input = {"project_id": str(PROJECT_ID), "voice_profile_id": str(PROFILE_ID),
                      "text": PARTS[1], "reference_clip_id": str(window.clip_id),
                      "reference_start_ms": window.start_ms, "reference_end_ms": window.end_ms,
                      "reference_sha256": sha256(reference), "model": str(MODEL),
                      "num_step": 32, "speed": 1.0, "device": "cpu", "instruct": None}
        ledger = ProviderCallLedger(db)
        reservation = ledger.reserve_execution(
            project_id=PROJECT_ID, idempotency_key=CALL_KEY, operation="tts", mode="runtime",
            provider="omnivoice", model=str(MODEL), input_source=f"{RUN_ID}-sentence:profile:{PROFILE_ID}",
            input_digest=provider_call_input_digest(call_input),
            estimated_cost=UsageCost(category=CostCategory.VOICE, amount=Decimal("0"), currency="USD",
                                     provider="omnivoice", note="local non-commercial benchmark; no external provider charge"),
            allow_existing_unknown_cost=True,
        )
        if not reservation.owner:
            raise ValueError("V21 provider call already reserved; inspect provenance, never retry blindly")
        manifest: dict[str, object] = {
            "evaluation": f"{RUN_ID.upper()}-one-complete-causal-sentence-generation", "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "inference_running", "copy": COPY, "generation_text": PARTS[1],
            "source_audio_asset_id": str(source.id), "source_sha256": SOURCE_HASH,
            "authorization_reference": source.authorization_reference, "voice_profile_id": str(profile.id),
            "reference_clip_id": str(window.clip_id), "reference_start_ms": window.start_ms,
            "reference_end_ms": window.end_ms, "reference_text": window.transcript,
            "reference_sha256": sha256(reference), "reference_audio": str(reference),
            "model": str(MODEL), "runtime": str(RUNTIME), "num_step": 32, "speed": 1.0,
            "device": "cpu", "instruct": None, "provider_call_id": str(reservation.record.id),
            "provider_input_digest": provider_call_input_digest(call_input),
            "failed_pre_inference_launch_manifest": str(CUDA_LAUNCH_FAILURE) if CUDA_LAUNCH_FAILURE.exists() else None,
            "first_sentence_cut_ms": FIRST_END_MS, "later_sentences_start_ms": LATER_START_MS,
            "limitation": "Sentence-level generation-boundary experiment; no verified semantic-plan or word-level provider control.",
        }
        write_manifest(manifest)
        # Invoke the runtime directly: on Windows a timeout of the optional
        # bridge kills only its parent Python, leaving OmniVoice orphaned.
        command = [str(RUNTIME), "-m", "omnivoice.cli.infer",
                   "--model", str(MODEL), "--ref_audio", str(reference),
                   "--ref_text", window.transcript, "--text", PARTS[1],
                   "--output", str(generated), "--language", "Chinese",
                   "--num_step", "32", "--speed", "1.0", "--device", "cpu"]
        try:
            completed = subprocess.run(command, check=False, timeout=TIMEOUT_SECONDS)
            if completed.returncode or not generated.is_file() or generated.stat().st_size == 0:
                raise ValueError("V21 OmniVoice produced no usable output")
        except Exception as exc:
            error_code = f"{RUN_ID}_local_inference_timeout" if isinstance(exc, subprocess.TimeoutExpired) else f"{RUN_ID}_local_inference_failed"
            ledger.finish(project_id=PROJECT_ID, call_id=reservation.record.id, status="failed",
                          usage_observable=False, error_code=error_code)
            manifest["status"] = "inference_failed"
            manifest["failure_code"] = error_code
            write_manifest(manifest)
            raise
        ledger.finish(project_id=PROJECT_ID, call_id=reservation.record.id, status="completed", usage_observable=False,
                      result_payload={"audio_sha256": sha256(generated), "audio_path": str(generated)})
        manifest.update({"status": "inference_completed", "generated_audio": str(generated),
                         "generated_sha256": sha256(generated)})
        write_manifest(manifest)
        print(json.dumps({"generated": str(generated), "sha256": sha256(generated),
                          "provider_call_id": str(reservation.record.id)}, ensure_ascii=False))


def prepare() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("status") != "inference_completed":
        raise ValueError("V21 generation not completed or children already prepared")
    generated = Path(manifest["generated_audio"])
    if sha256(generated) != manifest["generated_sha256"]:
        raise ValueError("V21 generated audio hash changed")
    with Database(ROOT / "content-os-data" / "content-os.sqlite3") as db:
        source, _, _ = checked_sources(db)
        assert_quiet_cut(resolve_audio(source.source_file), FIRST_END_MS)
        assert_quiet_cut(resolve_audio(source.source_file), LATER_START_MS)
        first = EVIDENCE / "reused-opening.wav"
        later = EVIDENCE / "reused-later-sentences.wav"
        extract_part(resolve_audio(source.source_file), first, 0, FIRST_END_MS)
        extract_part(resolve_audio(source.source_file), later, LATER_START_MS, None)
        files = (first, generated, later)
        labels = ("reused_opening", "new_causal_sentence", "reused_later_sentences")
        children = []
        for index, (path, target, label) in enumerate(zip(files, PARTS, labels)):
            metadata = {"provider": "omnivoice" if index == 1 else "derived_audio_excerpt",
                        "model": str(MODEL) if index == 1 else f"{RUN_ID}-v20b-unchanged-excerpt",
                        "voice_profile_id": str(PROFILE_ID),
                        "source_audio_asset_id": str(source.id) if index != 1 else None,
                        "source_sha256": SOURCE_HASH if index != 1 else None,
                        "provider_call_id": manifest["provider_call_id"] if index == 1 else None,
                        "reference_clip_id": manifest["reference_clip_id"] if index == 1 else None,
                        "reference_start_ms": REFERENCE_START_MS if index == 1 else None,
                        "reference_end_ms": REFERENCE_END_MS if index == 1 else None}
            audio_id, job_id = import_child(db, path, target, metadata,
                                            f"{RUN_ID}-{label}-independent-copy-qa", source.authorization_reference)
            children.append({"label": label, "target_text": target, "path": str(path),
                             "sha256": sha256(path), "audio_asset_id": audio_id, "qa_job_id": job_id})
        manifest.update({"status": "children_qa_pending", "children": children})
        write_manifest(manifest)
        print(json.dumps(children, ensure_ascii=False))


def compose() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("status") != "children_qa_pending":
        raise ValueError("V21 children not prepared or master already composed")
    with Database(ROOT / "content-os-data" / "content-os.sqlite3") as db:
        source, _, _ = checked_sources(db)
        audios = AudioAssetRepository(db)
        takes = [audios.get(UUID(item["audio_asset_id"])) for item in manifest["children"]]
        if any(take is None or (take.metadata.get("voice_generation") or {}).get("qa_state") != "verified"
               for take in takes):
            raise ValueError("V21 every child must pass independent Voice QA before composition")
        for take, target in zip(takes, PARTS):
            if (take.metadata["voice_generation"]).get("target_text") != target:
                raise ValueError("V21 child target copy mismatch")
        plan_data = source.metadata["voice_generation"]["performance_plan"]
        from app.domain.models import NarrationPerformancePlan
        plan = NarrationPerformancePlan.model_validate(plan_data)
        render = plan_voice_performance_units(COPY, plan)
        units = render.units
        if len(units) != 4 or [unit.end_char for unit in units] != [8, 27, 44, len(COPY)]:
            raise ValueError("V21 expected semantic unit boundaries changed")
        # U-Voice already accepted the shortened terminal joins; the old BEAT
        # at char 27 was editorial intent, not the measured V19E output. Keep
        # the same exact-copy Units, with a local BRIEF execution choice only.
        short_second = replace(units[1], pause_after=NarrationPause.BRIEF)
        spans = (VoiceGenerationSpan(0, 0, 8, PARTS[0], (units[0],)),
                 VoiceGenerationSpan(1, 8, 27, PARTS[1], (short_second,)),
                 VoiceGenerationSpan(2, 27, len(COPY), PARTS[2], units[2:]))
        master_path = EVIDENCE / f"{RUN_ID}-master.wav"
        if master_path.exists():
            raise ValueError("V21 master already exists")
        result = VoicePerformanceComposer(resolve_local_executable("ffmpeg")).compose(
            takes, spans, master_path, boundary_map=derive_narration_boundary_map(COPY))
        total_joins = [
            _trailing_pcm_silence_ms(takes[index]) + result.pause_after_ms[index]
            + _leading_pcm_silence_ms(takes[index + 1])
            for index in range(2)
        ]
        if total_joins[0] > 300 or total_joins[1] > 260:
            raise ValueError("sentence join exceeds the user-reviewed short-pause range")
        imported = AudioImporter(db, ROOT / "content-os-data", FFProbeAdapter(resolve_local_executable("ffprobe"))).import_path(
            master_path, source.authorization_reference, language="zh")
        if imported.content_hash != sha256(master_path) or "voice_generation" in imported.metadata:
            raise ValueError("V21 master import was not independent")
        metadata = dict(imported.metadata)
        metadata["voice_generation"] = {
            "provider": "omnivoice_plus_verified_composition", "model": str(MODEL),
            "project_id": str(PROJECT_ID), "voice_profile_id": str(PROFILE_ID), "target_text": COPY,
            "qa_state": "pending", "human_review_state": "pending",
            "performance_plan": plan.model_dump(mode="json"),
            "composition": {"child_audio_asset_ids": [str(take.id) for take in takes],
                            "span_boundaries": [0, 8, 27, len(COPY)],
                            "effective_pause_after_ms": list(result.pause_after_ms),
                            "measured_total_join_ms": total_joins,
                            "char_27_local_pause_choice": "brief; user-reviewed V19E short join",
                            "provider_plan_application": "unverified"},
        }
        metadata["sentence_contour_evidence_manifest"] = str(MANIFEST)
        updated = imported.model_copy(update={"metadata": metadata})
        with db.transaction():
            audios.update(updated)
            job_id = queue_qa(db, updated.id, COPY, f"{RUN_ID}-fresh-full-master-voice-qa")
        manifest.update({"status": "master_qa_pending", "master": {"path": str(master_path),
                         "sha256": sha256(master_path), "audio_asset_id": str(updated.id),
                         "qa_job_id": str(job_id), "effective_pause_after_ms": list(result.pause_after_ms),
                         "measured_total_join_ms": total_joins}})
        write_manifest(manifest)
        print(json.dumps(manifest["master"], ensure_ascii=False))


def verify_master() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("status") != "master_qa_pending":
        raise ValueError("V21 master QA not pending")
    with Database(ROOT / "content-os-data" / "content-os.sqlite3") as db:
        asset = AudioAssetRepository(db).get(UUID(manifest["master"]["audio_asset_id"]))
        if asset is None or (asset.metadata.get("voice_generation") or {}).get("qa_state") != "verified":
            raise ValueError("V21 fresh full-master Voice QA not verified")
        if sha256(Path(manifest["master"]["path"])) != manifest["master"]["sha256"]:
            raise ValueError("V21 master WAV hash changed")
        manifest["status"] = "awaiting_u_review"
        manifest["master"]["qa"] = asset.metadata["voice_generation"]["qa"]
        write_manifest(manifest)
        print(json.dumps(manifest["master"], ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("generate", "prepare", "compose", "verify-master"))
    phase = parser.parse_args().phase
    {"generate": generate, "prepare": prepare, "compose": compose,
     "verify-master": verify_master}[phase]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
