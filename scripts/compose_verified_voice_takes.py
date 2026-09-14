"""Compose independently QA-verified local voice takes into a provisional WAV.

The script accepts evidence JSON emitted by ``evaluate_omnivoice_output.py``
only as a local evaluation bridge.  It verifies the referenced media hash and
QA state, invokes the provider-neutral take composer, and writes a manifest.
The composed WAV remains provisional until it receives its own independent
ASR QA; no provider's requested text is used as observed master evidence.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "services" / "api"))

from app.domain.models import AudioAsset, TranscriptSegment  # noqa: E402
from app.runtime import resolve_local_executable  # noqa: E402
from app.voice_takes import VerifiedVoiceTake, VoiceTakeComposer  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--take", action="append", required=True, type=Path, help="QA evidence JSON, in requested scene order.")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--manifest", type=Path)
    return parser


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _take(path: Path, index: int) -> VerifiedVoiceTake:
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("qa", {}).get("verified") is not True:
        raise ValueError(f"take evidence is not QA-verified: {path}")
    audio_info = document.get("audio")
    transcription = document.get("transcription")
    generation = document.get("updated_asset_metadata", {}).get("voice_generation")
    if not isinstance(audio_info, dict) or not isinstance(transcription, dict) or not isinstance(generation, dict):
        raise ValueError(f"take evidence is malformed: {path}")
    audio_path = Path(str(audio_info.get("path", ""))).resolve()
    expected_hash = audio_info.get("content_hash")
    if not audio_path.is_file() or not isinstance(expected_hash, str) or _sha256(audio_path) != expected_hash:
        raise ValueError(f"take media does not match evidence: {path}")
    segments = [TranscriptSegment(**item) for item in transcription.get("segments", [])]
    if not segments:
        raise ValueError(f"take evidence has no actual timed transcript: {path}")
    model = str(document.get("provider_model", ""))
    audio = AudioAsset(
        source_file=str(audio_path), content_hash=expected_hash, duration_ms=int(audio_info["duration_ms"]),
        sample_rate=int(audio_info["sample_rate"]), channels=int(audio_info["channels"]),
        language=transcription.get("language"), authorization_reference=str(document.get("authorization_reference") or "local-voice-evaluation"),
        imported_at=datetime.now(timezone.utc), metadata={"voice_generation": generation},
        transcript_segments=segments, transcript_source=f"independent-asr:{model}",
    )
    return VerifiedVoiceTake(scene_id=f"take-{index:02d}", audio=audio)


def main() -> int:
    args = _parser().parse_args()
    takes = [_take(path.resolve(), index) for index, path in enumerate(args.take, start=1)]
    result = VoiceTakeComposer(resolve_local_executable("ffmpeg")).compose(takes, args.output)
    manifest_path = args.manifest or result.output_path.with_suffix(".composition.json")
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "provisional_pending_master_asr_qa",
        "output": str(result.output_path),
        "expected_duration_ms": result.expected_duration_ms,
        "source_asset_ids": list(result.source_asset_ids),
        "source_evidence": [str(path.resolve()) for path in args.take],
        "source_transcript_segments": [item.model_dump(mode="json") for item in result.transcript_segments],
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
