"""One accounted independent ASR pass on immutable V57 Span 5."""
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
from app.voice_qa import verify_generated_voice  # noqa: E402

DATA = ROOT / "content-os-data"
SOURCE = DATA / "evaluation-evidence" / "v57-assisted-master" / "manifest.json"
EVIDENCE = DATA / "evaluation-evidence" / "v58-copy-conflict" / "diagnostic.json"
MODEL = DATA / "models" / "models--Systran--faster-whisper-small" / "snapshots" / "536b0662742c02347bc0e980a01041f333bce120"
PROJECT_ID = UUID("d45f42fb-06eb-4ec5-bded-66116ea6a872")


def save(value: dict) -> None:
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    if EVIDENCE.exists() or not MODEL.is_dir():
        raise ValueError("diagnostic already exists or independent local model unavailable")
    original = json.loads(SOURCE.read_text(encoding="utf-8"))
    row = original["children"][5]
    if original["status"] != "blocked_copy_qa" or row["status"] != "blocked_copy_qa":
        raise ValueError("the exact failed V57 QA state changed")
    asset_id = UUID(row["audio_asset_id"])
    with Database(DATA / "content-os.sqlite3") as db:
        audio = AudioAssetRepository(db).get(asset_id)
        if audio is None or audio.content_hash != row["sha256"]:
            raise ValueError("failed AudioAsset identity changed")
        path = Path(audio.source_file)
        if not path.is_absolute():
            path = DATA / path
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
            raise ValueError("failed source WAV bytes changed")
        generation = audio.metadata.get("voice_generation") or {}
        if generation.get("target_text") != row["copy"] or generation.get("qa_state") != "failed":
            raise ValueError("original target/QA provenance differs")
        call_input = {"asset_id": str(asset_id), "sha256": row["sha256"], "model": str(MODEL),
                      "language": "zh", "prompt": None, "vad_filter": False, "word_timestamps": True}
        ledger = ProviderCallLedger(db)
        reservation = ledger.reserve_execution(
            project_id=PROJECT_ID, idempotency_key="v58-span5-small-asr-one",
            operation="asr", mode="runtime", provider="faster-whisper", model=str(MODEL),
            input_source=f"v58:audio-asset:{asset_id}",
            input_digest=provider_call_input_digest(call_input),
            estimated_cost=UsageCost(category=CostCategory.ASR, amount=Decimal("0"), currency="USD",
                                     provider="faster-whisper", note="local diagnostic; no external charge"),
            allow_existing_unknown_cost=True,
        )
        if not reservation.owner:
            raise ValueError("V58 diagnostic ProviderCall is already reserved")
        evidence = {
            "package": "V58", "status": "running", "created_at": datetime.now(timezone.utc).isoformat(),
            "audio_asset_id": str(asset_id), "source_sha256": row["sha256"],
            "original_qa_job_id": row["qa_job_id"], "original_target": row["copy"],
            "original_asr": " ".join(item.text for item in audio.transcript_segments),
            "provider_call_id": str(reservation.record.id), "diagnostic_model": str(MODEL),
            "limits": "one independent local ASR; original failed QA remains unchanged",
        }
        save(evidence)
        provider = FasterWhisperASRProvider(str(MODEL), device="cpu", compute_type="int8",
                                           vad_filter=False, word_timestamps=True)
        try:
            result = provider.transcribe(path, language="zh", prompt=None)
            report = verify_generated_voice(audio, row["copy"], result)
        except Exception:
            ledger.finish(project_id=PROJECT_ID, call_id=reservation.record.id, status="failed",
                          usage_observable=False, error_code="v58_local_diagnostic_asr_failed")
            evidence["status"] = "blocked_diagnostic_runtime"
            save(evidence)
            raise
        ledger.finish(project_id=PROJECT_ID, call_id=reservation.record.id, status="completed",
                      usage_observable=False, result_payload={
                          "source_sha256": row["sha256"],
                          "transcript_sha256": hashlib.sha256(result.text.encode("utf-8")).hexdigest(),
                      })
        evidence.update({
            "status": "diagnostic_complete", "diagnostic_transcript": result.text,
            "diagnostic_segments": [item.__dict__ for item in result.segments],
            "diagnostic_words": [item.__dict__ for item in result.words],
            "copy_coverage": report.copy_coverage,
            "missing_tokens": report.missing_token_count,
            "substitution_tokens": report.substitution_token_count,
            "duplicate_tokens": report.duplicate_token_count,
        })
        save(evidence)
    print(json.dumps({"status": evidence["status"], "transcript": evidence["diagnostic_transcript"],
                      "substitutions": evidence["substitution_tokens"]}))


if __name__ == "__main__":
    main()
