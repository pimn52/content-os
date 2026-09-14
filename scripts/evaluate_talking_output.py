"""Record evidence for one real local Talking/lip-sync output.

This evaluation bridge is deliberately separate from the optional provider
runtime.  It checks the produced container with FFprobe, extracts its actual
audio track, and sends that track through the existing local-ASR Voice QA
bridge.  It does not assert visual likeness or semantic lip-sync from a
fixture: those remain an explicit human U-Talking gate.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile


REPO_ROOT = Path(__file__).resolve().parents[1]
VOICE_EVALUATOR = REPO_ROOT / "scripts" / "evaluate_omnivoice_output.py"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--target-audio", required=True, type=Path)
    parser.add_argument("--target-text", required=True)
    parser.add_argument("--reference-audio", required=True, type=Path)
    parser.add_argument("--reference-text", required=True)
    parser.add_argument("--authorization-reference", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--provider-model", required=True)
    parser.add_argument("--ffmpeg", required=True, type=Path)
    parser.add_argument("--ffprobe", required=True, type=Path)
    parser.add_argument("--qa-output", type=Path)
    parser.add_argument("--language", default="zh")
    parser.add_argument("--max-duration-delta-ms", type=int, default=500)
    parser.add_argument("--database", type=Path, help="Optional Content OS SQLite database to update after this real evaluation.")
    parser.add_argument("--asset-id", help="Generated Talking asset ID to update; requires --database.")
    return parser


def _existing_file(value: Path, label: str) -> Path:
    path = value.resolve()
    if not path.is_file():
        raise SystemExit(f"{label} does not exist: {path}")
    return path


def _probe(ffprobe: Path, video: Path) -> dict[str, object]:
    completed = subprocess.run(
        [
            str(ffprobe), "-v", "error", "-show_entries",
            "format=duration:stream=codec_type,codec_name,width,height,r_frame_rate",
            "-of", "json", str(video),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(f"FFprobe could not read Talking output: {completed.stderr.strip()}")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit("FFprobe returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise SystemExit("FFprobe result is invalid")
    return result


def _duration_ms(probe: dict[str, object]) -> int | None:
    container = probe.get("format")
    if not isinstance(container, dict):
        return None
    value = container.get("duration")
    try:
        milliseconds = round(float(value) * 1_000)
    except (TypeError, ValueError):
        return None
    return milliseconds if milliseconds > 0 else None


def _streams(probe: dict[str, object]) -> tuple[dict[str, object], ...]:
    values = probe.get("streams")
    if not isinstance(values, list):
        return ()
    return tuple(item for item in values if isinstance(item, dict))


def _extract_audio(ffmpeg: Path, video: Path, output: Path) -> None:
    completed = subprocess.run(
        [
            str(ffmpeg), "-hide_banner", "-loglevel", "error", "-i", str(video),
            "-map", "0:a:0", "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", "-y", str(output),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
        raise SystemExit(f"Talking output audio extraction failed: {completed.stderr.strip()}")


def main() -> int:
    args = _parser().parse_args()
    if args.max_duration_delta_ms < 0:
        raise SystemExit("max-duration-delta-ms must be non-negative")
    video = _existing_file(args.video, "Talking video")
    target_audio = _existing_file(args.target_audio, "target audio")
    reference_audio = _existing_file(args.reference_audio, "reference audio")
    ffmpeg = _existing_file(args.ffmpeg, "FFmpeg")
    ffprobe = _existing_file(args.ffprobe, "FFprobe")
    if not args.target_text.strip() or not args.reference_text.strip():
        raise SystemExit("target-text and reference-text must not be empty")

    probe = _probe(ffprobe, video)
    target_probe = _probe(ffprobe, target_audio)
    streams = _streams(probe)
    video_streams = [item for item in streams if item.get("codec_type") == "video"]
    audio_streams = [item for item in streams if item.get("codec_type") == "audio"]
    duration_ms = _duration_ms(probe)
    target_duration_ms = _duration_ms(target_probe)
    output_path = (args.qa_output or video.with_suffix(".talking-qa.json")).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="content-os-talking-qa-", dir=output_path.parent) as raw_temp:
        extracted_audio = Path(raw_temp) / "observed-output.wav"
        if audio_streams:
            _extract_audio(ffmpeg, video, extracted_audio)
            voice_qa_path = Path(raw_temp) / "voice-qa.json"
            command = [
                sys.executable, str(VOICE_EVALUATOR),
                "--audio", str(extracted_audio),
                "--target-text", args.target_text,
                "--reference-audio", str(reference_audio),
                "--reference-text", args.reference_text,
                "--authorization-reference", args.authorization_reference,
                "--provider", args.provider,
                "--provider-model", args.provider_model,
                "--language", args.language,
                "--qa-output", str(voice_qa_path),
            ]
            completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
            if not voice_qa_path.is_file():
                raise SystemExit(f"local ASR Voice QA did not produce evidence: {(completed.stderr or completed.stdout).strip()[-1500:]}")
            voice_qa = json.loads(voice_qa_path.read_text(encoding="utf-8"))
            voice_verified = completed.returncode == 0 and bool(voice_qa.get("qa", {}).get("verified"))
            observed_duration_ms = voice_qa.get("audio", {}).get("duration_ms")
        else:
            voice_qa = None
            voice_verified = False
            observed_duration_ms = None

    checks: list[str] = []
    if not video_streams:
        checks.append("video_stream_missing")
    elif not all(isinstance(item.get("width"), int) and item["width"] > 0 and isinstance(item.get("height"), int) and item["height"] > 0 for item in video_streams):
        checks.append("video_dimensions_invalid")
    if not audio_streams:
        checks.append("audio_stream_missing")
    if duration_ms is None:
        checks.append("container_duration_invalid")
    if target_duration_ms is None:
        checks.append("driving_audio_duration_invalid")
    if not voice_verified:
        checks.append("new_word_audio_qa_failed")
    if isinstance(target_duration_ms, int) and duration_ms is not None and abs(target_duration_ms - duration_ms) > args.max_duration_delta_ms:
        checks.append("driving_audio_video_duration_mismatch")
    if not checks:
        checks.append("automated_checks_passed_human_likeness_and_sync_review_required")
    evidence = {
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "provider": args.provider,
        "provider_model": args.provider_model,
        "authorization_reference": args.authorization_reference,
        "video": {"path": str(video), "duration_ms": duration_ms, "probe": probe},
        "target_audio": {"path": str(target_audio), "duration_ms": target_duration_ms, "probe": target_probe},
        "observed_output_audio_duration_ms": observed_duration_ms,
        "new_word_audio_qa": voice_qa,
        "automated_verified": checks == ["automated_checks_passed_human_likeness_and_sync_review_required"],
        "checks": checks,
        "human_gate": "U-Talking likeness, naturalness, and visible lip-sync judgment required",
    }
    output_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if (args.database is None) != (args.asset_id is None):
        raise SystemExit("--database and --asset-id must be supplied together")
    if args.database is not None and args.asset_id is not None:
        from uuid import UUID

        from app.db import AssetRepository, Database
        from app.talking_qa import TalkingQaReport, apply_talking_qa

        database = Database(args.database)
        try:
            assets = AssetRepository(database)
            asset = assets.get(UUID(args.asset_id))
            if asset is None:
                raise SystemExit("Talking asset is unavailable in the supplied database")
            assets.update(apply_talking_qa(asset, TalkingQaReport(
                automated_verified=bool(evidence["automated_verified"]),
                checks=tuple(str(item) for item in checks),
                evidence_reference=str(output_path),
            )))
        finally:
            database.close()
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0 if evidence["automated_verified"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
