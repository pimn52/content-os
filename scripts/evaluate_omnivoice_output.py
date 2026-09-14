"""Evaluate one real OmniVoice output with the project's evidence-based Voice QA.

This is an evaluation-only bridge between the optional OmniVoice runtime and
the provider-neutral Content OS QA contract.  It deliberately transcribes the
generated file with a separate local ASR model; the requested text is recorded
as input evidence, never used as the observed transcript.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import wave


REPO_ROOT = Path(__file__).resolve().parents[1]
API_ROOT = REPO_ROOT / "services" / "api"
DEFAULT_ASR_MODEL = (
    REPO_ROOT
    / "content-os-data"
    / "models"
    / "models--Systran--faster-whisper-small"
    / "snapshots"
    / "536b0662742c02347bc0e980a01041f333bce120"
)

sys.path.insert(0, str(API_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.domain.models import AudioAsset  # noqa: E402
from app.providers.asr import FasterWhisperASRProvider  # noqa: E402
from app.voice_qa import apply_voice_qa, verify_generated_voice  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", required=True, type=Path)
    parser.add_argument("--target-text", required=True)
    parser.add_argument("--reference-audio", required=True, type=Path)
    parser.add_argument("--reference-text", required=True)
    parser.add_argument("--authorization-reference", default="u-voice-latest-four-mp4")
    parser.add_argument("--provider", default="omnivoice")
    parser.add_argument("--provider-model", required=True)
    parser.add_argument("--asr-model", type=Path, default=DEFAULT_ASR_MODEL)
    parser.add_argument("--qa-output", type=Path)
    parser.add_argument("--language", default="zh")
    parser.add_argument("--max-silence-ms", type=int, default=2_000)
    return parser


def _wav_metadata(path: Path) -> tuple[int, int, int]:
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        sample_rate = handle.getframerate()
        frames = handle.getnframes()
    if channels <= 0 or sample_rate <= 0 or frames <= 0:
        raise ValueError("generated WAV has invalid audio metadata")
    duration_ms = round(frames * 1_000 / sample_rate)
    return duration_ms, sample_rate, channels


def _segment_dict(segment: object) -> dict[str, object]:
    return {
        "start_ms": getattr(segment, "start_ms"),
        "end_ms": getattr(segment, "end_ms"),
        "text": getattr(segment, "text"),
    }


def main() -> int:
    args = _parser().parse_args()
    audio_path = args.audio.resolve()
    reference_path = args.reference_audio.resolve()
    if not audio_path.is_file():
        raise SystemExit(f"audio does not exist: {audio_path}")
    if not reference_path.is_file():
        raise SystemExit(f"reference audio does not exist: {reference_path}")
    if not args.asr_model.exists():
        raise SystemExit(f"local ASR model does not exist: {args.asr_model}")

    duration_ms, sample_rate, channels = _wav_metadata(audio_path)
    content_hash = hashlib.sha256(audio_path.read_bytes()).hexdigest()
    audio = AudioAsset(
        source_file=str(audio_path),
        content_hash=content_hash,
        duration_ms=duration_ms,
        sample_rate=sample_rate,
        channels=channels,
        language=args.language,
        authorization_reference=args.authorization_reference,
        imported_at=datetime.now(timezone.utc),
        metadata={
            "voice_generation": {
                "provider": args.provider,
                "model": args.provider_model,
                "reference_audio": str(reference_path),
                "reference_text": args.reference_text,
                "qa_state": "pending",
            }
        },
    )
    asr = FasterWhisperASRProvider(
        model=str(args.asr_model),
        device="cpu",
        compute_type="int8",
        vad_filter=False,
        word_timestamps=True,
    )
    transcription = asr.transcribe(audio_path, language=args.language)
    report = verify_generated_voice(
        audio,
        args.target_text,
        transcription,
        max_silence_ms=args.max_silence_ms,
    )
    updated = apply_voice_qa(
        audio,
        report,
        transcription,
        provider="faster-whisper",
        model=str(args.asr_model),
    )
    evidence = {
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "provider": args.provider,
        "provider_model": args.provider_model,
        "reference_audio": str(reference_path),
        "reference_text": args.reference_text,
        "target_text": args.target_text,
        "audio": {
            "path": str(audio_path),
            "content_hash": content_hash,
            "duration_ms": duration_ms,
            "sample_rate": sample_rate,
            "channels": channels,
        },
        "transcription": {
            "text": transcription.text,
            "language": transcription.language,
            "segments": [_segment_dict(item) for item in transcription.segments],
        },
        "qa": asdict(report),
        "updated_asset_metadata": updated.metadata,
    }
    qa_path = args.qa_output or audio_path.with_suffix(".qa.json")
    qa_path.parent.mkdir(parents=True, exist_ok=True)
    qa_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0 if report.verified else 2


if __name__ == "__main__":
    raise SystemExit(main())
