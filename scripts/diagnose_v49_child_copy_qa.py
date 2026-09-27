"""Bounded source-verified ASR contrast on existing V48/V43 Span-2 takes."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api"))

from app.budget import ProviderCallLedger, provider_call_input_digest  # noqa: E402
from app.db import AudioAssetRepository, Database  # noqa: E402
from app.domain.models import CostCategory, UsageCost  # noqa: E402
from app.providers.asr import FasterWhisperASRProvider  # noqa: E402
from app.voice_qa import comparison_tokens, verify_generated_voice  # noqa: E402

DATA = ROOT / "content-os-data"
DB_PATH = DATA / "content-os.sqlite3"
EVIDENCE = DATA / "evaluation-evidence" / "v49-child-copy-attribution"
MANIFEST = EVIDENCE / "v49-child-copy-attribution.json"
MODEL = DATA / "models" / "models--Systran--faster-whisper-small" / "snapshots" / "536b0662742c02347bc0e980a01041f333bce120"
PROJECT_ID = UUID("c710747f-4742-4a6f-b80d-1d46527f6c90")
ASSETS = (("v48_failed", UUID("78ccd38a-9524-476a-b4b2-e226c2dd7150"), "f0efce960bdf5f6f2741a59a8cf90c7a614844104a91afd10aa28d1cbc10daba"),
          ("v43_control", UUID("ec51e6bb-631a-4f75-aa78-7ac103d1b1de"), "67213e8f2b4093e1ee4db10bada310fbe605fda8a82055eae178d9103a57ff06"))


def save(value: dict) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def main() -> None:
    if MANIFEST.exists():
        raise ValueError("V49 diagnostic already exists; no second ASR run")
    if not MODEL.is_dir():
        raise ValueError("installed local small ASR model unavailable")
    record = {"package": "V49", "status": "running", "created_at": datetime.now(timezone.utc).isoformat(),
              "diagnostic_model": str(MODEL), "method": "faster-whisper local small CPU int8, zh, no prompt, VAD off, word timestamps",
              "source_audio": {}, "limits": "Independent diagnostic ASR only; original QA verdict and AudioAssets remain unchanged."}
    provider = FasterWhisperASRProvider(str(MODEL), device="cpu", compute_type="int8", vad_filter=False, word_timestamps=True)
    with Database(DB_PATH) as db:
        ledger = ProviderCallLedger(db)
        audios = AudioAssetRepository(db)
        for label, asset_id, expected_hash in ASSETS:
            audio = audios.get(asset_id)
            if audio is None or audio.content_hash != expected_hash:
                raise ValueError(f"{label} AudioAsset identity changed")
            path = Path(audio.source_file)
            if not path.is_absolute():
                path = DATA / path
            if not path.is_file() or digest(path) != expected_hash:
                raise ValueError(f"{label} source audio bytes changed")
            generation = audio.metadata.get("voice_generation") or {}
            if generation.get("target_text") != "所以写文案之前，先说清楚你的判断，再用例子和证据把它讲明白。":
                raise ValueError("paired exact editorial copies differ")
            call_input = {"asset_id": str(asset_id), "source_sha256": expected_hash, "model": str(MODEL),
                          "language": "zh", "prompt": None, "vad_filter": False, "word_timestamps": True}
            reservation = ledger.reserve_execution(
                project_id=PROJECT_ID, idempotency_key=f"v49-existing-audio-small-asr-{label}-one",
                operation="asr", mode="runtime", provider="faster-whisper", model=str(MODEL),
                input_source=f"v49:audio-asset:{asset_id}", input_digest=provider_call_input_digest(call_input),
                estimated_cost=UsageCost(category=CostCategory.ASR, amount=Decimal("0"), currency="USD",
                                         provider="faster-whisper", note="local diagnostic; no external charge"),
                allow_existing_unknown_cost=True,
            )
            if not reservation.owner:
                raise ValueError(f"{label} diagnostic ProviderCall already reserved")
            record["source_audio"][label] = {"audio_asset_id": str(asset_id), "source_sha256": expected_hash,
                                             "provider_call_id": str(reservation.record.id), "status": "running",
                                             "saved_qa_state": generation.get("qa_state"),
                                             "saved_medium_asr": " ".join(item.text for item in audio.transcript_segments)}
            save(record)
            try:
                result = provider.transcribe(path, language="zh", prompt=None)
                report = verify_generated_voice(audio, generation["target_text"], result)
            except Exception:
                ledger.finish(project_id=PROJECT_ID, call_id=reservation.record.id, status="failed",
                              usage_observable=False, error_code="v49_local_diagnostic_asr_failed")
                record["status"] = "blocked_diagnostic_runtime"
                record["source_audio"][label]["status"] = "failed"
                save(record)
                raise
            ledger.finish(project_id=PROJECT_ID, call_id=reservation.record.id, status="completed",
                          usage_observable=False, result_payload={"source_sha256": expected_hash,
                                                                  "transcript_sha256": hashlib.sha256(result.text.encode("utf-8")).hexdigest()})
            record["source_audio"][label].update({
                "status": "completed", "diagnostic_transcript": result.text,
                "diagnostic_segments": [{"start_ms": item.start_ms, "end_ms": item.end_ms, "text": item.text}
                                        for item in result.segments],
                "diagnostic_words": [{"start_ms": item.start_ms, "end_ms": item.end_ms, "text": item.text}
                                     for item in result.words],
                "target_token_count": len(comparison_tokens(generation["target_text"])),
                "diagnostic_copy_coverage": report.copy_coverage,
                "diagnostic_missing_tokens": report.missing_token_count,
                "diagnostic_substitutions": report.substitution_token_count,
            })
            save(record)
    record["status"] = "diagnostic_complete_attribution_pending"
    save(record)
    print(json.dumps({"status": record["status"], "transcripts": {
        key: row["diagnostic_transcript"] for key, row in record["source_audio"].items()}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
