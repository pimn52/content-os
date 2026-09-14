"""Run one explicitly requested local OmniVoice benchmark take.

This optional bridge exposes only the controls offered by the installed
OmniVoice CLI.  It uses an already-local model snapshot, makes no network
request, and writes no credentials.  The output still needs independent ASR
QA before it can be joined into a MasterNarration.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--reference-audio", required=True, type=Path)
    parser.add_argument("--reference-text", required=True)
    parser.add_argument("--text", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--language", default="zh")
    parser.add_argument("--num-step", type=int, default=32)
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--duration", type=float)
    parser.add_argument("--instruct")
    args = parser.parse_args()
    required = ((args.runtime, "runtime"), (args.model, "model"), (args.reference_audio, "reference audio"))
    for value, label in required:
        if not value.resolve().exists():
            raise SystemExit(f"{label} is unavailable: {value}")
    if args.num_step < 1 or args.speed <= 0 or args.duration is not None and args.duration <= 0:
        raise SystemExit("num-step, speed, and duration must be positive")
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(args.runtime.resolve()), "-m", "omnivoice.cli.infer",
        "--model", str(args.model.resolve()), "--text", args.text,
        "--ref_audio", str(args.reference_audio.resolve()), "--ref_text", args.reference_text,
        "--output", str(output), "--language", args.language,
        "--num_step", str(args.num_step), "--speed", str(args.speed), "--device", "cpu",
    ]
    if args.duration is not None:
        command.extend(("--duration", str(args.duration)))
    if args.instruct:
        command.extend(("--instruct", args.instruct))
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
        raise SystemExit("local OmniVoice did not produce a playable WAV")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
