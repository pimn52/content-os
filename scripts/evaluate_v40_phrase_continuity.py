"""One accounted OmniVoice broad-delivery-cue experiment on V38's affected spans.

Phases: prepare, generate 0|1|2|4, compose, verify. No retries or cue grid.
The exact copy/punctuation and authorized reference are unchanged. This is
evaluation evidence, not a verified Performance Plan application receipt.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api"))

from app.budget import ProviderCallLedger, provider_call_input_digest  # noqa: E402
from app.db import (AssetRepository, AudioAssetRepository, ClipRepository, Database,  # noqa: E402
                    JobRepository, VoiceProfileRepository)
from app.domain.models import CostCategory, Job, JobType, UsageCost, VoiceQaJobPayload  # noqa: E402
from app.media import AudioImporter, FFProbeAdapter  # noqa: E402
from app.runtime import resolve_local_executable  # noqa: E402
from app.voice_performance import select_voice_reference_windows  # noqa: E402
from app.voice_takes import VerifiedVoiceTake, VoiceTakeComposer  # noqa: E402

DATA_ROOT = ROOT / "content-os-data"
V38 = DATA_ROOT / "evaluation-evidence" / "v38-new-copy-master" / "v38-new-copy-master.json"
EVIDENCE = DATA_ROOT / "evaluation-evidence" / "v40-phrase-continuity"
MANIFEST = EVIDENCE / "v40-phrase-continuity.json"
PROJECT_ID = UUID("c710747f-4742-4a6f-b80d-1d46527f6c90")
PROFILE_ID = UUID("29294db2-d3c6-45c2-b467-6a02e853c826")
MODEL = Path("C:/Users/ASUS/.cache/huggingface/hub/models--k2-fsa--OmniVoice/snapshots/c5fdb5ccb189668d56333f77ba2629f4cd7535f4")
RUNTIME = DATA_ROOT / "omnivoice-runtime" / "Scripts" / "python.exe"
AFFECTED = (0, 1, 2, 4)
SOURCE_IDS = (
    UUID("29235e54-2c7e-47ef-a6b7-341047ec2349"),
    UUID("f2451c27-94d2-4ee3-bc4f-cb3711827b82"),
    UUID("1882ee8c-934f-4740-aa54-d3b05506afa3"),
    UUID("830f63c5-9d0b-4ba7-a2ed-f2aa0b17e5be"),
    UUID("60d46d42-1aba-4004-a2c3-29a970664854"),
)
CUE = "短视频口播，语流自然连贯，词组内部不要停顿，逗号轻带过，句号自然收束。"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def save(manifest: dict) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def checked_sources(db: Database) -> tuple[dict, list, object]:
    v38 = json.loads(V38.read_text(encoding="utf-8"))
    if v38["project_id"] != str(PROJECT_ID) or len(v38["spans"]) != 5:
        raise ValueError("V38 project/span contract changed")
    audios = AudioAssetRepository(db)
    sources = []
    for i, source_id in enumerate(SOURCE_IDS):
        source = audios.get(source_id)
        row = v38["spans"][i]
        if (source is None or row["audio_asset_id"] != str(source_id)
                or row["sha256"] != source.content_hash
                or not source.authorization_reference
                or (source.metadata.get("voice_generation") or {}).get("qa_state") != "verified"):
            raise ValueError(f"V38 source span {i} identity/QA changed")
        path = DATA_ROOT / "assets" / "audio-originals" / f"{source.content_hash}.wav"
        if not path.is_file() or sha256(path) != source.content_hash:
            raise ValueError(f"V38 source span {i} bytes changed")
        sources.append(source)
    copy = " ".join(source.metadata["voice_generation"]["target_text"] for source in sources)
    if copy != v38["draft_exact_copy"]:
        raise ValueError("V38 five-span exact copy changed")
    profile = VoiceProfileRepository(db).get(PROFILE_ID)
    if profile is None or not profile.consent.confirmed:
        raise ValueError("authorized VoiceProfile unavailable")
    if any(source.authorization_reference != profile.consent.authorization_reference for source in sources):
        raise ValueError("source/profile authorization provenance mismatch")
    clips = [ClipRepository(db).get(item) for item in profile.reference_clip_ids]
    if not clips or any(item is None for item in clips):
        raise ValueError("authorized reference Clip unavailable")
    assets = AssetRepository(db)
    related = {asset.id: asset for clip in clips for asset in [assets.get(clip.asset_id)] if asset is not None}
    windows = select_voice_reference_windows(clips, related, data_root=DATA_ROOT)
    if not windows or not 3000 <= windows[0].end_ms - windows[0].start_ms <= 10000:
        raise ValueError("V38 best authorized reference window unavailable")
    if not MODEL.is_dir() or not RUNTIME.is_file():
        raise ValueError("local OmniVoice runtime/model unavailable")
    return v38, sources, windows[0]


def queue_qa(db: Database, audio_id: UUID, copy: str, key: str) -> str:
    now = datetime.now(timezone.utc)
    job = Job(id=uuid4(), project_id=PROJECT_ID, type=JobType.VERIFY_VOICE,
              idempotency_key=key, created_at=now, updated_at=now,
              payload=VoiceQaJobPayload(project_id=PROJECT_ID, narration_audio_id=audio_id, target_text=copy))
    persisted = JobRepository(db).create(job)
    if persisted.id != job.id:
        raise ValueError("V40 QA idempotency key already used")
    return str(job.id)


def prepare() -> None:
    if MANIFEST.exists():
        raise ValueError("V40 already prepared; no reset/retune")
    with Database(DATA_ROOT / "content-os.sqlite3") as db:
        v38, sources, window = checked_sources(db)
    reference = EVIDENCE / "authorized-reference.wav"
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    if reference.exists():
        raise ValueError("V40 reference already exists; no overwrite")
    source_file = Path(window.source_file)
    if not source_file.is_file():
        raise ValueError("authorized reference media unavailable")
    command = [resolve_local_executable("ffmpeg"), "-nostdin", "-n", "-ss", f"{window.start_ms / 1000:.3f}",
               "-i", str(source_file), "-t", f"{(window.end_ms - window.start_ms) / 1000:.3f}",
               "-vn", "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", str(reference)]
    result = subprocess.run(command, capture_output=True, timeout=120, check=False)
    if result.returncode or not reference.is_file() or reference.stat().st_size == 0:
        raise ValueError("V40 authorized reference extraction failed")
    manifest = {
        "package": "V40-bounded-in-take-phrase-continuity", "status": "prepared",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "project_id": str(PROJECT_ID), "profile_id": str(PROFILE_ID),
        "copy": v38["draft_exact_copy"], "source_span_ids": [str(x) for x in SOURCE_IDS],
        "source_span_hashes": [x.content_hash for x in sources], "affected_span_indexes": list(AFFECTED),
        "cue": CUE, "cue_scope": "broad delivery/style only; no word-level control or plan receipt",
        "reference": {"clip_id": str(window.clip_id), "start_ms": window.start_ms,
                      "end_ms": window.end_ms, "text": window.transcript,
                      "path": str(reference), "sha256": sha256(reference)},
        "model": str(MODEL), "runtime": str(RUNTIME), "speed": 1.0,
        "num_step": 32, "device": "cuda", "children": {},
        "limitation": "One non-commercial local benchmark; copy QA is separate from human continuity/publishability review.",
    }
    save(manifest)
    print(json.dumps({"status": "prepared", "reference_sha256": manifest["reference"]["sha256"]}))


def generate(index: int) -> None:
    if index not in AFFECTED:
        raise ValueError("only the four declared affected spans may be generated")
    manifest = load()
    if manifest["status"] not in {"prepared", "generating"} or str(index) in manifest["children"]:
        raise ValueError("V40 span already attempted or phase closed; no retry")
    with Database(DATA_ROOT / "content-os.sqlite3") as db:
        _, sources, window = checked_sources(db)
        copy = sources[index].metadata["voice_generation"]["target_text"]
        reference = Path(manifest["reference"]["path"])
        if sha256(reference) != manifest["reference"]["sha256"] or window.transcript != manifest["reference"]["text"]:
            raise ValueError("V40 authorized reference changed")
        target = EVIDENCE / f"v40-span-{index}.wav"
        if target.exists():
            raise ValueError("V40 output exists; no overwrite/retry")
        call_input = {
            "project_id": str(PROJECT_ID), "voice_profile_id": str(PROFILE_ID),
            "text": copy, "reference_clip_id": str(window.clip_id),
            "reference_sha256": manifest["reference"]["sha256"],
            "reference_start_ms": window.start_ms, "reference_end_ms": window.end_ms,
            "model": str(MODEL), "speed": 1.0, "num_step": 32, "device": "cuda", "instruct": CUE,
        }
        ledger = ProviderCallLedger(db)
        reservation = ledger.reserve_execution(
            project_id=PROJECT_ID, idempotency_key=f"v40-phrase-continuity-span-{index}-one-cue",
            operation="tts", mode="runtime", provider="omnivoice", model=str(MODEL),
            input_source=f"v40:profile:{PROFILE_ID}:span:{index}",
            input_digest=provider_call_input_digest(call_input),
            estimated_cost=UsageCost(category=CostCategory.VOICE, amount=Decimal("0"), currency="USD",
                                     provider="omnivoice", note="local non-commercial benchmark; no external provider charge"),
            allow_existing_unknown_cost=True,
        )
        if not reservation.owner:
            raise ValueError("V40 provider call already reserved; no retry")
        manifest["status"] = "generating"
        manifest["children"][str(index)] = {"status": "inference_running", "copy": copy,
                                               "provider_call_id": str(reservation.record.id),
                                               "input_digest": provider_call_input_digest(call_input)}
        save(manifest)
        command = [str(RUNTIME), "-m", "omnivoice.cli.infer", "--model", str(MODEL),
                   "--text", copy, "--ref_audio", str(reference), "--ref_text", window.transcript,
                   "--output", str(target), "--language", "Chinese", "--num_step", "32",
                   "--speed", "1.0", "--device", "cuda", "--instruct", CUE]
        try:
            result = subprocess.run(command, capture_output=True, timeout=900, check=False)
            if result.returncode or not target.is_file() or target.stat().st_size == 0:
                raise ValueError("V40 OmniVoice produced no usable output")
        except Exception as exc:
            ledger.finish(project_id=PROJECT_ID, call_id=reservation.record.id, status="failed",
                          usage_observable=False, error_code="v40_local_inference_failed")
            manifest["status"] = "blocked"
            manifest["children"][str(index)]["status"] = "inference_failed"
            manifest["children"][str(index)]["failure_type"] = type(exc).__name__
            save(manifest)
            raise
        ledger.finish(project_id=PROJECT_ID, call_id=reservation.record.id, status="completed",
                      usage_observable=False, result_payload={"audio_sha256": sha256(target), "audio_path": str(target)})
        imported = AudioImporter(db, DATA_ROOT, FFProbeAdapter(resolve_local_executable("ffprobe"))).import_path(
            target, sources[index].authorization_reference, language="zh")
        if imported.content_hash != sha256(target) or "voice_generation" in imported.metadata:
            raise ValueError("V40 generated import is not independent")
        metadata = dict(imported.metadata)
        metadata["voice_generation"] = {
            "provider": "omnivoice", "model": str(MODEL), "project_id": str(PROJECT_ID),
            "voice_profile_id": str(PROFILE_ID), "target_text": copy, "qa_state": "pending",
            "human_review_state": "pending", "provider_call_id": str(reservation.record.id),
            "reference_clip_id": str(window.clip_id), "reference_sha256": manifest["reference"]["sha256"],
            "source_audio_asset_id": str(sources[index].id), "cue": CUE,
            "provider_plan_application": "unverified", "evaluation_only": True,
        }
        metadata["v40_evidence_manifest"] = str(MANIFEST)
        updated = imported.model_copy(update={"metadata": metadata})
        with db.transaction():
            AudioAssetRepository(db).update(updated)
            qa_job_id = queue_qa(db, updated.id, copy, f"v40-span-{index}-fresh-copy-qa")
        manifest["children"][str(index)].update({
            "status": "qa_pending", "path": str(target), "sha256": sha256(target),
            "audio_asset_id": str(updated.id), "qa_job_id": qa_job_id,
        })
        save(manifest)
    print(json.dumps({"span": index, "audio_asset_id": str(updated.id), "qa_job_id": qa_job_id}))


def compose() -> None:
    manifest = load()
    if manifest["status"] != "generating" or set(manifest["children"]) != {str(i) for i in AFFECTED}:
        raise ValueError("V40 is not ready for one composition")
    with Database(DATA_ROOT / "content-os.sqlite3") as db:
        _, sources, _ = checked_sources(db)
        audios = AudioAssetRepository(db)
        selected = []
        for i in range(5):
            if i in AFFECTED:
                row = manifest["children"][str(i)]
                asset = audios.get(UUID(row["audio_asset_id"]))
                job = JobRepository(db).get(UUID(row["qa_job_id"]))
                if asset is None or job is None or job.status.value != "completed":
                    raise ValueError(f"V40 child {i} fresh QA incomplete")
                generation = asset.metadata.get("voice_generation") or {}
                qa = generation.get("qa") or {}
                if (generation.get("qa_state") != "verified" or qa.get("copy_coverage") != 1.0
                        or qa.get("missing_token_count") != 0 or qa.get("duplicate_token_count") != 0
                        or qa.get("substitution_token_count") != 0 or sha256(Path(row["path"])) != row["sha256"]):
                    raise ValueError(f"V40 child {i} copy QA failed")
                row["qa"] = qa
                selected.append(asset)
            else:
                selected.append(sources[i])
        master_path = EVIDENCE / "v40-phrase-continuity-master.wav"
        if master_path.exists():
            raise ValueError("V40 Master already exists; no overwrite")
        composition = VoiceTakeComposer(resolve_local_executable("ffmpeg"), data_root=DATA_ROOT).compose(
            [VerifiedVoiceTake(scene_id=f"span-{i}", audio=asset) for i, asset in enumerate(selected)], master_path)
        imported = AudioImporter(db, DATA_ROOT, FFProbeAdapter(resolve_local_executable("ffprobe"))).import_path(
            master_path, sources[0].authorization_reference, language="zh")
        if imported.content_hash != sha256(master_path) or "voice_generation" in imported.metadata:
            raise ValueError("V40 Master import is not independent")
        metadata = dict(imported.metadata)
        metadata["voice_generation"] = {
            "provider": "composed_omnivoice_evaluation", "model": "v40-one-broad-cue-five-spans",
            "project_id": str(PROJECT_ID), "target_text": manifest["copy"],
            "qa_state": "pending", "human_review_state": "pending",
            "source_take_audio_ids": composition.source_asset_ids,
            "cue": CUE, "provider_plan_application": "unverified", "evaluation_only": True,
        }
        metadata["v40_evidence_manifest"] = str(MANIFEST)
        updated = imported.model_copy(update={"metadata": metadata})
        with db.transaction():
            audios.update(updated)
            qa_job_id = queue_qa(db, updated.id, manifest["copy"], "v40-master-fresh-copy-qa")
        manifest["status"] = "master_qa_pending"
        manifest["master"] = {"path": str(master_path), "sha256": sha256(master_path),
                              "duration_ms": imported.duration_ms, "audio_asset_id": str(updated.id),
                              "qa_job_id": qa_job_id, "source_take_audio_ids": list(composition.source_asset_ids)}
        save(manifest)
    print(json.dumps({"master_audio_asset_id": str(updated.id), "qa_job_id": qa_job_id,
                      "path": str(master_path)}))


def verify() -> None:
    manifest = load()
    if manifest["status"] != "master_qa_pending":
        raise ValueError("V40 Master QA is not pending")
    with Database(DATA_ROOT / "content-os.sqlite3") as db:
        row = manifest["master"]
        asset = AudioAssetRepository(db).get(UUID(row["audio_asset_id"]))
        job = JobRepository(db).get(UUID(row["qa_job_id"]))
    if asset is None or job is None or job.status.value != "completed":
        raise ValueError("V40 fresh Master QA is incomplete")
    generation = asset.metadata.get("voice_generation") or {}
    qa = generation.get("qa") or {}
    if (generation.get("qa_state") != "verified" or generation.get("target_text") != manifest["copy"]
            or qa.get("copy_coverage") != 1.0 or qa.get("missing_token_count") != 0
            or qa.get("duplicate_token_count") != 0 or qa.get("substitution_token_count") != 0
            or asset.content_hash != row["sha256"] or sha256(Path(row["path"])) != row["sha256"]):
        raise ValueError("V40 fresh Master copy QA failed")
    row["qa"] = qa
    manifest["status"] = "awaiting_u_review"
    save(manifest)
    print(json.dumps({"audio_asset_id": str(asset.id), "path": row["path"], "qa_state": "verified"}))


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] in {"prepare", "compose", "verify"}:
        {"prepare": prepare, "compose": compose, "verify": verify}[sys.argv[1]]()
    elif len(sys.argv) == 3 and sys.argv[1] == "generate" and sys.argv[2] in {"0", "1", "2", "4"}:
        generate(int(sys.argv[2]))
    else:
        raise SystemExit("usage: evaluate_v40_phrase_continuity.py prepare|generate 0|1|2|4|compose|verify")
