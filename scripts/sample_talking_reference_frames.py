"""Extract reproducible multi-frame evidence from one local Talking reference.

This helper intentionally records frames and timestamps only.  It does not
invent gaze, expression, or subtitle semantics when no configured vision
provider can support them.  Those observations can then be attached through
the typed Talking-reference assessment endpoint with this JSON as evidence.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--ffmpeg", required=True, type=Path)
    parser.add_argument("--ffprobe", required=True, type=Path)
    parser.add_argument("--frame-count", type=int, default=5)
    return parser


def _file(path: Path, label: str) -> Path:
    resolved = path.resolve()
    if not resolved.is_file():
        raise SystemExit(f"{label} does not exist: {resolved}")
    return resolved


def _duration_ms(ffprobe: Path, video: Path) -> int:
    completed = subprocess.run(
        [str(ffprobe), "-v", "error", "-show_entries", "format=duration", "-of", "json", str(video)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    if completed.returncode != 0:
        raise SystemExit("FFprobe could not read the reference video")
    try:
        seconds = float(json.loads(completed.stdout)["format"]["duration"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit("reference video duration is unavailable") from exc
    milliseconds = round(seconds * 1_000)
    if milliseconds <= 0:
        raise SystemExit("reference video duration is invalid")
    return milliseconds


def main() -> int:
    args = _parser().parse_args()
    if args.frame_count < 2 or args.frame_count > 30:
        raise SystemExit("frame-count must be from 2 through 30")
    video, ffmpeg, ffprobe = _file(args.video, "video"), _file(args.ffmpeg, "FFmpeg"), _file(args.ffprobe, "FFprobe")
    duration_ms = _duration_ms(ffprobe, video)
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    # Seeking to the exact container end has no decodable frame on several
    # otherwise-valid MP4s.  Keep the final sample inside the stream while
    # still representing the ending posture.
    final_sample_ms = max(0, duration_ms - 1_000)
    timestamps = [round(final_sample_ms * index / (args.frame_count - 1)) for index in range(args.frame_count)]
    frames: list[dict[str, object]] = []
    for index, timestamp_ms in enumerate(timestamps):
        frame = output / f"frame-{index + 1:02d}-{timestamp_ms:010d}ms.jpg"
        command = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{timestamp_ms / 1_000:g}", "-i", str(video), "-frames:v", "1", str(frame)]
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        # Some variable-frame-rate MP4s do not yield a frame when a fast seek
        # lands between their indexed packets.  Fall back to output-side seek,
        # which is slower but decodes through the requested timestamp.
        if completed.returncode != 0 or not frame.is_file() or frame.stat().st_size == 0:
            frame.unlink(missing_ok=True)
            completed = subprocess.run(
                [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-i", str(video), "-ss", f"{timestamp_ms / 1_000:g}", "-frames:v", "1", str(frame)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
            )
        if completed.returncode != 0 or not frame.is_file() or frame.stat().st_size == 0:
            raise SystemExit("FFmpeg could not extract reference evidence frames")
        frames.append({"timestamp_ms": timestamp_ms, "path": str(frame)})
    evidence = {
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "video": str(video),
        "duration_ms": duration_ms,
        "frame_count": args.frame_count,
        "frames": frames,
        "semantic_status": "frames_only_no_gaze_expression_or_subtitle_semantics_inferred",
    }
    evidence_path = output / "frames-evidence.json"
    evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sys.stdout.write(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
