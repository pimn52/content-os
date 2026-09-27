"""One authorized-reference-only OmniVoice comparison for three V41 sentences."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sys
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api"))

import evaluate_v40_phrase_continuity as base  # noqa: E402
import evaluate_v41_punctuation_delivery as old  # noqa: E402
from app.budget import ProviderCallLedger, provider_call_input_digest  # noqa: E402
from app.db import AssetRepository, AudioAssetRepository, ClipRepository, Database, JobRepository, VoiceProfileRepository  # noqa: E402
from app.domain.models import CostCategory, UsageCost  # noqa: E402
from app.media import AudioImporter, FFProbeAdapter  # noqa: E402
from app.runtime import resolve_local_executable  # noqa: E402
from app.voice_observation import _measure_quiet  # noqa: E402
from app.voice_performance import select_voice_reference_windows  # noqa: E402
from app.voice_takes import VerifiedVoiceTake, VoiceTakeComposer  # noqa: E402
from evaluate_v39_quiet_compaction import compact_pcm  # noqa: E402
import wave

DATA_ROOT = base.DATA_ROOT
EVIDENCE = DATA_ROOT / "evaluation-evidence" / "v43-reference-continuity"
MANIFEST = EVIDENCE / "v43-reference-continuity.json"
REFERENCE = EVIDENCE / "alternate-reference.wav"
PROJECT_ID = base.PROJECT_ID
REGENERATE = (0, 1, 2)
REUSE = (3, 4)
WINDOW_INDEX = 6


def save(manifest: dict) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def import_and_queue(db: Database, path: Path, copy: str, metadata: dict, key: str,
                     authorization: str) -> tuple[str, str]:
    imported = AudioImporter(db, DATA_ROOT, FFProbeAdapter(resolve_local_executable("ffprobe"))).import_path(
        path, authorization, language="zh")
    if imported.content_hash != base.sha256(path) or "voice_generation" in imported.metadata:
        raise ValueError("V43 import is not an independent AudioAsset")
    fields = dict(imported.metadata)
    fields["voice_generation"] = {
        "project_id": str(PROJECT_ID), "target_text": copy, "qa_state": "pending",
        "human_review_state": "pending", "provider_plan_application": "unverified",
        "evaluation_only": True, **metadata,
    }
    fields["v43_evidence_manifest"] = str(MANIFEST)
    updated = imported.model_copy(update={"metadata": fields})
    with db.transaction():
        AudioAssetRepository(db).update(updated)
        qa_job = base.queue_qa(db, updated.id, copy, key)
    return str(updated.id), qa_job


def selected_window(db: Database):
    profile = VoiceProfileRepository(db).get(base.PROFILE_ID)
    if profile is None or not profile.consent.confirmed:
        raise ValueError("V43 authorized profile unavailable")
    clips = [ClipRepository(db).get(item) for item in profile.reference_clip_ids]
    if not clips or any(item is None for item in clips):
        raise ValueError("V43 authorized reference clips unavailable")
    assets = AssetRepository(db)
    related = {asset.id: asset for clip in clips for asset in [assets.get(clip.asset_id)] if asset is not None}
    windows = select_voice_reference_windows(clips, related, data_root=DATA_ROOT)
    if len(windows) <= WINDOW_INDEX:
        raise ValueError("V43 alternate reference index unavailable")
    selected = windows[WINDOW_INDEX]
    if (selected.start_ms, selected.end_ms, str(selected.clip_id)) != (
            28240, 34240, "e3547414-ae03-5f1d-a2be-1b959fc75a58"):
        raise ValueError("V43 authorized reference window changed")
    return selected


def checked_context(db: Database, manifest: dict):
    v38, sources, _ = base.checked_sources(db)
    v41 = json.loads(old.MANIFEST.read_text(encoding="utf-8"))
    if v41["status"] != "u_voice_phrase_continuity_fail" or v41["copy"] != v38["draft_exact_copy"]:
        raise ValueError("V41 exact copy/review evidence changed")
    window = selected_window(db)
    if not REFERENCE.is_file() or base.sha256(REFERENCE) != manifest["reference"]["sha256"]:
        raise ValueError("V43 extracted authorized reference bytes changed")
    if window.transcript != manifest["reference"]["text"]:
        raise ValueError("V43 reference text changed")
    if [str(x.id) for x in sources] != manifest["source_span_ids"]:
        raise ValueError("V43 source span identity changed")
    for index in REUSE:
        row = v41["children"][str(index)]
        asset = AudioAssetRepository(db).get(UUID(row["audio_asset_id"]))
        if (asset is None or (asset.metadata.get("voice_generation") or {}).get("qa_state") != "verified"
                or asset.content_hash != row["sha256"] or base.sha256(Path(row["path"])) != row["sha256"]):
            raise ValueError(f"V43 reused span {index} is no longer verified")
    return sources, window, v41


def prepare() -> None:
    if MANIFEST.exists():
        raise ValueError("V43 already prepared; no alternate window/retry")
    with Database(DATA_ROOT / "content-os.sqlite3") as db:
        v38, sources, baseline = base.checked_sources(db)
        alternate = selected_window(db)
    v41 = json.loads(old.MANIFEST.read_text(encoding="utf-8"))
    v42 = json.loads((DATA_ROOT / "evaluation-evidence" / "v42-tail-duration" / "v42-tail-duration.json").read_text(encoding="utf-8"))
    if v41["status"] != "u_voice_phrase_continuity_fail" or v42["status"] != "u_voice_continuity_fail":
        raise ValueError("V41/V42 human feedback evidence missing")
    if v38["draft_exact_copy"] != v41["copy"]:
        raise ValueError("V43 editorial copy changed")
    for index in REGENERATE:
        old.validate_delivery(sources[index].metadata["voice_generation"]["target_text"], old.DELIVERY[index])
    if not REFERENCE.is_file():
        raise ValueError("V43 pre-extracted reference missing")
    if not 3000 <= alternate.end_ms - alternate.start_ms <= 10000:
        raise ValueError("V43 authorized reference duration invalid")
    old_ref = Path(json.loads(base.MANIFEST.read_text(encoding="utf-8"))["reference"]["path"])
    _, old_quiet, old_duration, _ = _measure_quiet(old_ref)
    _, new_quiet, new_duration, _ = _measure_quiet(REFERENCE)
    old_count = sum("\u4e00" <= char <= "\u9fff" for char in baseline.transcript)
    new_count = sum("\u4e00" <= char <= "\u9fff" for char in alternate.transcript)
    if (old_duration, new_duration) != (6000, 6000) or old_quiet or new_quiet or new_count <= old_count:
        raise ValueError("V43 alternate reference lacks the declared connected-delivery evidence")
    manifest = {
        "package": "V43-bounded-connected-delivery-reference", "status": "prepared",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "project_id": str(PROJECT_ID), "profile_id": str(base.PROFILE_ID),
        "copy": v41["copy"], "source_span_ids": [str(x.id) for x in sources],
        "source_span_hashes": [x.content_hash for x in sources],
        "reference": {"clip_id": str(alternate.clip_id), "start_ms": alternate.start_ms,
                      "end_ms": alternate.end_ms, "text": alternate.transcript,
                      "path": str(REFERENCE), "sha256": base.sha256(REFERENCE),
                      "source_file": alternate.source_file,
                      "evidence": {"old_6s_transcript_cjk_count": old_count,
                                   "new_6s_transcript_cjk_count": new_count,
                                   "old_minus45_quiet_intervals": len(old_quiet),
                                   "new_minus45_quiet_intervals": len(new_quiet),
                                   "limitation": "Transcript density/PCM quiet are proxies, not proof of connected target delivery."}},
        "reused_v41_span_indexes": list(REUSE),
        "delivery_text": {str(i): old.DELIVERY[i] for i in REGENERATE},
        "model": str(base.MODEL), "speed": 1.0, "num_step": 32,
        "device": "cuda", "instruct": None, "children": {},
        "limitation": "One same-creator authorized reference comparison; no word-level pause control or product capability claim.",
    }
    save(manifest)
    print(json.dumps({"status": "prepared", "reference_sha256": manifest["reference"]["sha256"],
                      "density_counts": [old_count, new_count]}))


def generate(index: int) -> None:
    if index not in REGENERATE:
        raise ValueError("V43 may generate only three declared spans")
    manifest = load()
    if manifest["status"] not in {"prepared", "generating"} or str(index) in manifest["children"]:
        raise ValueError("V43 span already attempted or phase closed; no retry")
    with Database(DATA_ROOT / "content-os.sqlite3") as db:
        sources, window, _ = checked_context(db, manifest)
        copy = sources[index].metadata["voice_generation"]["target_text"]
        delivery = old.DELIVERY[index]
        old.validate_delivery(copy, delivery)
        target = EVIDENCE / f"v43-span-{index}.wav"
        if target.exists():
            raise ValueError("V43 output exists; no overwrite/retry")
        call_input = {
            "project_id": str(PROJECT_ID), "voice_profile_id": str(base.PROFILE_ID),
            "editorial_copy": copy, "delivery_text": delivery,
            "reference_clip_id": str(window.clip_id),
            "reference_sha256": manifest["reference"]["sha256"],
            "reference_start_ms": window.start_ms, "reference_end_ms": window.end_ms,
            "model": str(base.MODEL), "speed": 1.0, "num_step": 32,
            "device": "cuda", "instruct": None,
        }
        ledger = ProviderCallLedger(db)
        reservation = ledger.reserve_execution(
            project_id=PROJECT_ID, idempotency_key=f"v43-reference-span-{index}-one-attempt",
            operation="tts", mode="runtime", provider="omnivoice", model=str(base.MODEL),
            input_source=f"v43:profile:{base.PROFILE_ID}:span:{index}",
            input_digest=provider_call_input_digest(call_input),
            estimated_cost=UsageCost(category=CostCategory.VOICE, amount=Decimal("0"), currency="USD",
                                     provider="omnivoice", note="local non-commercial benchmark; no external provider charge"),
            allow_existing_unknown_cost=True,
        )
        if not reservation.owner:
            raise ValueError("V43 ProviderCall already reserved; no retry")
        manifest["status"] = "generating"
        manifest["children"][str(index)] = {"status": "inference_running", "editorial_copy": copy,
                                               "delivery_text": delivery,
                                               "provider_call_id": str(reservation.record.id),
                                               "input_digest": provider_call_input_digest(call_input)}
        save(manifest)
        command = [str(base.RUNTIME), "-m", "omnivoice.cli.infer", "--model", str(base.MODEL),
                   "--text", delivery, "--ref_audio", str(REFERENCE), "--ref_text", window.transcript,
                   "--output", str(target), "--language", "Chinese", "--num_step", "32",
                   "--speed", "1.0", "--device", "cuda"]
        try:
            result = subprocess.run(command, capture_output=True, timeout=900, check=False)
            if result.returncode or not target.is_file() or target.stat().st_size == 0:
                raise ValueError(f"V43 OmniVoice produced no output (exit {result.returncode}): "
                                 f"{result.stderr.decode(errors='replace')[-1200:]}")
        except Exception as exc:
            ledger.finish(project_id=PROJECT_ID, call_id=reservation.record.id, status="failed",
                          usage_observable=False, error_code="v43_local_inference_failed")
            manifest["status"] = "blocked"
            manifest["children"][str(index)].update({"status": "inference_failed", "failure_type": type(exc).__name__,
                                                       "failure_detail": str(exc)[-1200:]})
            save(manifest)
            raise
        ledger.finish(project_id=PROJECT_ID, call_id=reservation.record.id, status="completed",
                      usage_observable=False, result_payload={"audio_sha256": base.sha256(target),
                                                              "audio_path": str(target)})
        audio_id, qa_job = import_and_queue(
            db, target, copy,
            {"provider": "omnivoice", "model": str(base.MODEL),
             "voice_profile_id": str(base.PROFILE_ID), "provider_call_id": str(reservation.record.id),
             "reference_clip_id": str(window.clip_id), "reference_sha256": manifest["reference"]["sha256"],
             "source_audio_asset_id": str(sources[index].id), "generation_text": delivery,
             "reference_comparison": "v43-authorized-window-6"},
            f"v43-span-{index}-fresh-copy-qa", sources[index].authorization_reference)
        manifest["children"][str(index)].update({"status": "qa_pending", "path": str(target),
                                                  "sha256": base.sha256(target), "audio_asset_id": audio_id,
                                                  "qa_job_id": qa_job})
        save(manifest)
    print(json.dumps({"span": index, "audio_asset_id": audio_id, "qa_job_id": qa_job}))


def compose() -> None:
    manifest = load()
    if manifest["status"] != "generating" or set(manifest["children"]) != {str(i) for i in REGENERATE}:
        raise ValueError("V43 new children not ready")
    with Database(DATA_ROOT / "content-os.sqlite3") as db:
        sources, _, v41 = checked_context(db, manifest)
        audios, jobs = AudioAssetRepository(db), JobRepository(db)
        selected = []
        for index in range(5):
            row = manifest["children"][str(index)] if index in REGENERATE else v41["children"][str(index)]
            asset = audios.get(UUID(row["audio_asset_id"]))
            if asset is None or (asset.metadata.get("voice_generation") or {}).get("qa_state") != "verified":
                raise ValueError(f"V43 child {index} QA unavailable")
            if index in REGENERATE:
                job = jobs.get(UUID(row["qa_job_id"]))
                qa = asset.metadata["voice_generation"].get("qa") or {}
                if (job is None or job.status.value != "completed" or qa.get("copy_coverage") != 1.0
                        or qa.get("missing_token_count") != 0 or qa.get("duplicate_token_count") != 0
                        or qa.get("substitution_token_count") != 0):
                    raise ValueError(f"V43 child {index} fresh copy QA failed")
                row["qa"] = qa
            if base.sha256(Path(row["path"])) != row["sha256"]:
                raise ValueError(f"V43 child {index} bytes changed")
            selected.append(asset)
        raw = EVIDENCE / "v43-raw-concat.wav"
        target = EVIDENCE / "v43-master.wav"
        if raw.exists() or target.exists():
            raise ValueError("V43 Master already exists; no overwrite")
        composition = VoiceTakeComposer(resolve_local_executable("ffmpeg"), data_root=DATA_ROOT).compose(
            [VerifiedVoiceTake(scene_id=f"span-{i}", audio=asset) for i, asset in enumerate(selected)], raw)
        boundaries, offset = [], 0
        for asset in selected[:-1]:
            offset += asset.duration_ms
            boundaries.append(offset)
        _, quiet, _, _ = _measure_quiet(raw)
        cuts = old.seam_cuts(quiet, boundaries)
        with wave.open(str(raw), "rb") as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (1, 2, 24000, "NONE"):
                raise ValueError("V43 raw Master PCM shape changed")
            pcm = wav.readframes(wav.getnframes())
        output = compact_pcm(pcm, rate=24000, cuts=cuts)
        with wave.open(str(target), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(output)
        audio_id, qa_job = import_and_queue(
            db, target, manifest["copy"],
            {"provider": "composed_omnivoice_evaluation", "model": "v43-alternate-reference-five-spans",
             "source_take_audio_ids": list(composition.source_asset_ids),
             "raw_master_sha256": base.sha256(raw),
             "quiet_seam_cuts": [{"name": n, "start_ms": s, "end_ms": e} for n, s, e in cuts],
             "generation_text": " ".join(old.DELIVERY.get(i, sources[i].metadata["voice_generation"]["target_text"])
                                           for i in range(5))},
            "v43-master-fresh-copy-qa", sources[0].authorization_reference)
        manifest["status"] = "master_qa_pending"
        manifest["master"] = {"path": str(target), "sha256": base.sha256(target),
                              "raw_path": str(raw), "raw_sha256": base.sha256(raw),
                              "audio_asset_id": audio_id, "qa_job_id": qa_job,
                              "source_take_audio_ids": list(composition.source_asset_ids),
                              "raw_boundaries_ms": boundaries,
                              "quiet_seam_cuts": [{"name": n, "start_ms": s, "end_ms": e} for n, s, e in cuts]}
        save(manifest)
    print(json.dumps({"master_audio_asset_id": audio_id, "qa_job_id": qa_job,
                      "path": str(target)}))


def verify() -> None:
    manifest = load()
    if manifest["status"] != "master_qa_pending":
        raise ValueError("V43 Master QA is not pending")
    with Database(DATA_ROOT / "content-os.sqlite3") as db:
        row = manifest["master"]
        asset = AudioAssetRepository(db).get(UUID(row["audio_asset_id"]))
        job = JobRepository(db).get(UUID(row["qa_job_id"]))
    if asset is None or job is None or job.status.value != "completed":
        raise ValueError("V43 fresh Master QA incomplete")
    generation = asset.metadata.get("voice_generation") or {}
    qa = generation.get("qa") or {}
    if (generation.get("qa_state") != "verified" or generation.get("target_text") != manifest["copy"]
            or qa.get("copy_coverage") != 1.0 or qa.get("missing_token_count") != 0
            or qa.get("duplicate_token_count") != 0 or qa.get("substitution_token_count") != 0
            or asset.content_hash != row["sha256"] or base.sha256(Path(row["path"])) != row["sha256"]):
        raise ValueError("V43 fresh Master copy QA failed")
    row["qa"] = qa
    manifest["status"] = "awaiting_u_review"
    save(manifest)
    print(json.dumps({"audio_asset_id": str(asset.id), "path": row["path"], "qa_state": "verified"}))


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] in {"prepare", "compose", "verify"}:
        {"prepare": prepare, "compose": compose, "verify": verify}[sys.argv[1]]()
    elif len(sys.argv) == 3 and sys.argv[1] == "generate" and sys.argv[2] in {"0", "1", "2"}:
        generate(int(sys.argv[2]))
    else:
        raise SystemExit("usage: evaluate_v43_reference_continuity.py prepare|generate 0|1|2|compose|verify")
