"""One V38-copy OmniVoice punctuation-as-delivery-text evaluation.

Phases: prepare, generate 0|1|2|4, repair3, compose, verify. Each phase is
idempotency-guarded; there are no retries or alternate punctuation variants.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from uuid import UUID
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api"))

import evaluate_v40_phrase_continuity as prior  # noqa: E402
from evaluate_v39_quiet_compaction import compact_pcm  # noqa: E402
from app.budget import ProviderCallLedger, provider_call_input_digest  # noqa: E402
from app.db import AudioAssetRepository, Database, JobRepository  # noqa: E402
from app.domain.models import CostCategory, UsageCost  # noqa: E402
from app.media import AudioImporter, FFProbeAdapter  # noqa: E402
from app.runtime import resolve_local_executable  # noqa: E402
from app.voice_observation import _measure_quiet, observe_voice_performance  # noqa: E402
from app.voice_qa import comparison_tokens  # noqa: E402
from app.voice_takes import VerifiedVoiceTake, VoiceTakeComposer  # noqa: E402

DATA_ROOT = prior.DATA_ROOT
EVIDENCE = DATA_ROOT / "evaluation-evidence" / "v41-punctuation-delivery"
MANIFEST = EVIDENCE / "v41-punctuation-delivery.json"
V40 = prior.MANIFEST
PROJECT_ID = prior.PROJECT_ID
SOURCE_IDS = prior.SOURCE_IDS
AFFECTED = prior.AFFECTED
DELIVERY = {
    0: "一个好选题，不是把信息堆满而是先回答观众最关心的问题。",
    1: "观众刷到视频的前几秒会先问这件事跟我有什么关系？",
    2: "所以写文案之前先说清楚你的判断再用例子和证据把它讲明白。",
    4: "最后，把结论收回来让观众听得懂，也记得住。",
}


def save(manifest: dict) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def validate_delivery(copy: str, delivery: str) -> None:
    if comparison_tokens(copy) != comparison_tokens(delivery):
        raise ValueError("delivery text changes spoken tokens")
    if not copy or not delivery or len(delivery) >= len(copy):
        raise ValueError("V41 must remove only supported punctuation")
    punctuation = frozenset("，：")
    cursor = 0
    for character in copy:
        if cursor < len(delivery) and character == delivery[cursor]:
            cursor += 1
        elif character not in punctuation:
            raise ValueError("V41 delivery changed more than selected comma/colon marks")
    if cursor != len(delivery):
        raise ValueError("V41 delivery has inserted text")


def checked_reference(manifest: dict, window: object) -> Path:
    reference = Path(manifest["reference"]["path"])
    if (not reference.is_file() or prior.sha256(reference) != manifest["reference"]["sha256"]
            or window.transcript != manifest["reference"]["text"]
            or str(window.clip_id) != manifest["reference"]["clip_id"]
            or window.start_ms != manifest["reference"]["start_ms"]
            or window.end_ms != manifest["reference"]["end_ms"]):
        raise ValueError("V41 authorized reference differs from V40/V38")
    return reference


def import_and_queue(db: Database, path: Path, copy: str, metadata: dict, key: str,
                     authorization: str) -> tuple[str, str]:
    imported = AudioImporter(db, DATA_ROOT, FFProbeAdapter(resolve_local_executable("ffprobe"))).import_path(
        path, authorization, language="zh")
    if imported.content_hash != prior.sha256(path) or "voice_generation" in imported.metadata:
        raise ValueError("V41 import is not an independent new AudioAsset")
    fields = dict(imported.metadata)
    fields["voice_generation"] = {
        "project_id": str(PROJECT_ID), "target_text": copy, "qa_state": "pending",
        "human_review_state": "pending", "provider_plan_application": "unverified",
        "evaluation_only": True, **metadata,
    }
    fields["v41_evidence_manifest"] = str(MANIFEST)
    updated = imported.model_copy(update={"metadata": fields})
    with db.transaction():
        AudioAssetRepository(db).update(updated)
        qa_job = prior.queue_qa(db, updated.id, copy, key)
    return str(updated.id), qa_job


def prepare() -> None:
    if MANIFEST.exists():
        raise ValueError("V41 already prepared; no reset")
    with Database(DATA_ROOT / "content-os.sqlite3") as db:
        v38, sources, window = prior.checked_sources(db)
    old = json.loads(V40.read_text(encoding="utf-8"))
    if old["status"] != "blocked" or old["project_id"] != str(PROJECT_ID):
        raise ValueError("V40 unsupported-instruct evidence missing")
    reference = checked_reference(old, window)
    for index, delivery in DELIVERY.items():
        validate_delivery(sources[index].metadata["voice_generation"]["target_text"], delivery)
    manifest = {
        "package": "V41-bounded-punctuation-delivery-text", "status": "prepared",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "project_id": str(PROJECT_ID), "profile_id": str(prior.PROFILE_ID),
        "copy": v38["draft_exact_copy"],
        "source_span_ids": [str(x) for x in SOURCE_IDS],
        "source_span_hashes": [x.content_hash for x in sources],
        "delivery_text": {str(i): text for i, text in DELIVERY.items()},
        "variable": "Remove only selected provider-facing comma/colon marks; spoken tokens and editorial copy stay exact.",
        "reference": {**old["reference"], "path": str(reference)},
        "model": str(prior.MODEL), "runtime": str(prior.RUNTIME),
        "speed": 1.0, "num_step": 32, "device": "cuda", "instruct": None,
        "children": {}, "seam_policy": "measured quiet at known five-span joins only; reduce to at most 120ms without touching speech",
        "limitation": "Evaluation only; no general punctuation renderer or acoustic Performance Plan receipt.",
    }
    save(manifest)
    print(json.dumps({"status": "prepared", "reference_sha256": old["reference"]["sha256"]}))


def generate(index: int) -> None:
    if index not in AFFECTED:
        raise ValueError("V41 may generate only declared affected spans")
    manifest = load()
    if manifest["status"] not in {"prepared", "generating"} or str(index) in manifest["children"]:
        raise ValueError("V41 span already attempted or phase closed; no retry")
    with Database(DATA_ROOT / "content-os.sqlite3") as db:
        _, sources, window = prior.checked_sources(db)
        reference = checked_reference(manifest, window)
        copy = sources[index].metadata["voice_generation"]["target_text"]
        delivery = DELIVERY[index]
        validate_delivery(copy, delivery)
        target = EVIDENCE / f"v41-span-{index}.wav"
        if target.exists():
            raise ValueError("V41 output exists; no overwrite/retry")
        call_input = {
            "project_id": str(PROJECT_ID), "voice_profile_id": str(prior.PROFILE_ID),
            "editorial_copy": copy, "delivery_text": delivery,
            "reference_clip_id": str(window.clip_id),
            "reference_sha256": manifest["reference"]["sha256"],
            "reference_start_ms": window.start_ms, "reference_end_ms": window.end_ms,
            "model": str(prior.MODEL), "speed": 1.0, "num_step": 32,
            "device": "cuda", "instruct": None,
        }
        ledger = ProviderCallLedger(db)
        reservation = ledger.reserve_execution(
            project_id=PROJECT_ID, idempotency_key=f"v41-punctuation-span-{index}-one-attempt",
            operation="tts", mode="runtime", provider="omnivoice", model=str(prior.MODEL),
            input_source=f"v41:profile:{prior.PROFILE_ID}:span:{index}",
            input_digest=provider_call_input_digest(call_input),
            estimated_cost=UsageCost(category=CostCategory.VOICE, amount=Decimal("0"), currency="USD",
                                     provider="omnivoice", note="local non-commercial benchmark; no external provider charge"),
            allow_existing_unknown_cost=True,
        )
        if not reservation.owner:
            raise ValueError("V41 provider call already reserved; no retry")
        manifest["status"] = "generating"
        manifest["children"][str(index)] = {
            "status": "inference_running", "editorial_copy": copy, "delivery_text": delivery,
            "provider_call_id": str(reservation.record.id), "input_digest": provider_call_input_digest(call_input),
        }
        save(manifest)
        command = [str(prior.RUNTIME), "-m", "omnivoice.cli.infer", "--model", str(prior.MODEL),
                   "--text", delivery, "--ref_audio", str(reference), "--ref_text", window.transcript,
                   "--output", str(target), "--language", "Chinese", "--num_step", "32",
                   "--speed", "1.0", "--device", "cuda"]
        try:
            result = subprocess.run(command, capture_output=True, timeout=900, check=False)
            if result.returncode or not target.is_file() or target.stat().st_size == 0:
                raise ValueError(f"V41 OmniVoice produced no output (exit {result.returncode}): "
                                 f"{result.stderr.decode(errors='replace')[-1200:]}")
        except Exception as exc:
            ledger.finish(project_id=PROJECT_ID, call_id=reservation.record.id, status="failed",
                          usage_observable=False, error_code="v41_local_inference_failed")
            manifest["status"] = "blocked"
            manifest["children"][str(index)].update({"status": "inference_failed", "failure_type": type(exc).__name__,
                                                       "failure_detail": str(exc)[-1200:]})
            save(manifest)
            raise
        ledger.finish(project_id=PROJECT_ID, call_id=reservation.record.id, status="completed",
                      usage_observable=False, result_payload={"audio_sha256": prior.sha256(target), "audio_path": str(target)})
        audio_id, qa_job = import_and_queue(
            db, target, copy,
            {"provider": "omnivoice", "model": str(prior.MODEL),
             "voice_profile_id": str(prior.PROFILE_ID), "provider_call_id": str(reservation.record.id),
             "reference_clip_id": str(window.clip_id), "reference_sha256": manifest["reference"]["sha256"],
             "source_audio_asset_id": str(sources[index].id), "generation_text": delivery,
             "punctuation_delivery_variant": "v41-selected-marks-removed"},
            f"v41-span-{index}-fresh-copy-qa", sources[index].authorization_reference)
        manifest["children"][str(index)].update({
            "status": "qa_pending", "path": str(target), "sha256": prior.sha256(target),
            "audio_asset_id": audio_id, "qa_job_id": qa_job,
        })
        save(manifest)
    print(json.dumps({"span": index, "audio_asset_id": audio_id, "qa_job_id": qa_job}))


def repair3() -> None:
    manifest = load()
    if manifest["status"] not in {"prepared", "generating"} or "3" in manifest["children"]:
        raise ValueError("V41 span 3 already prepared or phase closed")
    with Database(DATA_ROOT / "content-os.sqlite3") as db:
        _, sources, _ = prior.checked_sources(db)
        source = sources[3]
        copy = source.metadata["voice_generation"]["target_text"]
        obs = observe_voice_performance(source, copy, data_root=DATA_ROOT)
        quiet = obs["measurement"]["physical_quiet_intervals"]
        if not any(item["start_ms"] <= 5550 and item["end_ms"] >= 6090 for item in quiet):
            raise ValueError("V41 source span 3 quiet evidence changed")
        source_path = DATA_ROOT / "assets" / "audio-originals" / f"{source.content_hash}.wav"
        with wave.open(str(source_path), "rb") as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (1, 2, 24000, "NONE"):
                raise ValueError("V41 span 3 PCM shape changed")
            raw = wav.readframes(wav.getnframes())
        output = compact_pcm(raw, rate=24000, cuts=(("v39_span3_internal_no_break", 5600, 6040),))
        target = EVIDENCE / "v41-span-3-reused-v39-quiet-repair.wav"
        if target.exists():
            raise ValueError("V41 span 3 derived output exists")
        with wave.open(str(target), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(output)
        audio_id, qa_job = import_and_queue(
            db, target, copy,
            {"provider": "derived_audio_quiet_evaluation", "model": "v41-reuse-v39-exact-span3-cut",
             "source_audio_asset_id": str(source.id), "source_sha256": source.content_hash,
             "removed_interval_ms": [5600, 6040], "generation_text": copy},
            "v41-span-3-fresh-copy-qa", source.authorization_reference)
        manifest["children"]["3"] = {"status": "qa_pending", "editorial_copy": copy,
                                       "delivery_text": copy, "source_audio_asset_id": str(source.id),
                                       "path": str(target), "sha256": prior.sha256(target),
                                       "audio_asset_id": audio_id, "qa_job_id": qa_job}
        save(manifest)
    print(json.dumps({"span": 3, "audio_asset_id": audio_id, "qa_job_id": qa_job}))


def seam_cuts(quiet: list[dict], boundaries: list[int]) -> tuple[tuple[str, int, int], ...]:
    cuts = []
    for index, boundary in enumerate(boundaries):
        matches = [item for item in quiet if item["start_ms"] <= boundary <= item["end_ms"]]
        if len(matches) != 1:
            raise ValueError(f"V41 known seam {index} has no single measured quiet interval")
        item = matches[0]
        if item["duration_ms"] > 120:
            start, end = item["start_ms"] + 60, item["end_ms"] - 60
            if start >= end:
                raise ValueError("V41 seam cut is empty")
            cuts.append((f"seam_{index}_{index+1}", start, end))
    return tuple(cuts)


def compose() -> None:
    manifest = load()
    if manifest["status"] != "generating" or set(manifest["children"]) != {str(i) for i in range(5)}:
        raise ValueError("V41 five children are not ready")
    with Database(DATA_ROOT / "content-os.sqlite3") as db:
        _, sources, _ = prior.checked_sources(db)
        audios, jobs = AudioAssetRepository(db), JobRepository(db)
        selected = []
        for i in range(5):
            row = manifest["children"][str(i)]
            asset = audios.get(UUID(row["audio_asset_id"]))
            job = jobs.get(UUID(row["qa_job_id"]))
            if asset is None or job is None or job.status.value != "completed":
                raise ValueError(f"V41 child {i} QA incomplete")
            generation = asset.metadata.get("voice_generation") or {}
            qa = generation.get("qa") or {}
            if (generation.get("qa_state") != "verified" or qa.get("copy_coverage") != 1.0
                    or qa.get("missing_token_count") != 0 or qa.get("duplicate_token_count") != 0
                    or qa.get("substitution_token_count") != 0 or generation.get("target_text") != row["editorial_copy"]
                    or prior.sha256(Path(row["path"])) != row["sha256"]):
                raise ValueError(f"V41 child {i} fresh copy QA failed")
            row["qa"] = qa
            selected.append(asset)
        raw_master = EVIDENCE / "v41-raw-concat.wav"
        final_master = EVIDENCE / "v41-master.wav"
        if raw_master.exists() or final_master.exists():
            raise ValueError("V41 Master output already exists; no overwrite")
        composition = VoiceTakeComposer(resolve_local_executable("ffmpeg"), data_root=DATA_ROOT).compose(
            [VerifiedVoiceTake(scene_id=f"span-{i}", audio=asset) for i, asset in enumerate(selected)], raw_master)
        boundaries = []
        total = 0
        for asset in selected[:-1]:
            total += asset.duration_ms
            boundaries.append(total)
        _, quiet, _, _ = _measure_quiet(raw_master)
        cuts = seam_cuts(quiet, boundaries)
        with wave.open(str(raw_master), "rb") as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (1, 2, 24000, "NONE"):
                raise ValueError("V41 raw Master PCM shape changed")
            raw = wav.readframes(wav.getnframes())
        output = compact_pcm(raw, rate=24000, cuts=cuts)
        with wave.open(str(final_master), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(output)
        audio_id, qa_job = import_and_queue(
            db, final_master, manifest["copy"],
            {"provider": "composed_omnivoice_evaluation", "model": "v41-punctuation-delivery-five-spans",
             "source_take_audio_ids": list(composition.source_asset_ids),
             "raw_master_sha256": prior.sha256(raw_master),
             "quiet_seam_cuts": [{"name": n, "start_ms": s, "end_ms": e} for n, s, e in cuts],
             "generation_text": " ".join(DELIVERY.get(i, sources[i].metadata["voice_generation"]["target_text"])
                                           for i in range(5))},
            "v41-master-fresh-copy-qa", sources[0].authorization_reference)
        manifest["status"] = "master_qa_pending"
        manifest["master"] = {"path": str(final_master), "sha256": prior.sha256(final_master),
                              "raw_path": str(raw_master), "raw_sha256": prior.sha256(raw_master),
                              "audio_asset_id": audio_id, "qa_job_id": qa_job,
                              "source_take_audio_ids": list(composition.source_asset_ids),
                              "raw_boundaries_ms": boundaries,
                              "quiet_seam_cuts": [{"name": n, "start_ms": s, "end_ms": e} for n, s, e in cuts]}
        save(manifest)
    print(json.dumps({"master_audio_asset_id": audio_id, "qa_job_id": qa_job,
                      "path": str(final_master)}))


def verify() -> None:
    manifest = load()
    if manifest["status"] != "master_qa_pending":
        raise ValueError("V41 Master QA is not pending")
    with Database(DATA_ROOT / "content-os.sqlite3") as db:
        row = manifest["master"]
        asset = AudioAssetRepository(db).get(UUID(row["audio_asset_id"]))
        job = JobRepository(db).get(UUID(row["qa_job_id"]))
    if asset is None or job is None or job.status.value != "completed":
        raise ValueError("V41 fresh Master QA is incomplete")
    generation = asset.metadata.get("voice_generation") or {}
    qa = generation.get("qa") or {}
    if (generation.get("qa_state") != "verified" or generation.get("target_text") != manifest["copy"]
            or qa.get("copy_coverage") != 1.0 or qa.get("missing_token_count") != 0
            or qa.get("duplicate_token_count") != 0 or qa.get("substitution_token_count") != 0
            or asset.content_hash != row["sha256"] or prior.sha256(Path(row["path"])) != row["sha256"]):
        raise ValueError("V41 fresh Master copy QA failed")
    row["qa"] = qa
    manifest["status"] = "awaiting_u_review"
    save(manifest)
    print(json.dumps({"audio_asset_id": str(asset.id), "path": row["path"], "qa_state": "verified"}))


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] in {"prepare", "repair3", "compose", "verify"}:
        {"prepare": prepare, "repair3": repair3, "compose": compose, "verify": verify}[sys.argv[1]]()
    elif len(sys.argv) == 3 and sys.argv[1] == "generate" and sys.argv[2] in {"0", "1", "2", "4"}:
        generate(int(sys.argv[2]))
    else:
        raise SystemExit("usage: evaluate_v41_punctuation_delivery.py prepare|generate 0|1|2|4|repair3|compose|verify")
