"""Trim a measured leading edge from a generated WAV and apply short fades."""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import soundfile as sf


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--trim-start-ms", required=True, type=int)
    parser.add_argument("--trim-end-ms", type=int)
    parser.add_argument("--fade-in-ms", type=int, default=50)
    parser.add_argument("--fade-out-ms", type=int, default=150)
    args = parser.parse_args()
    if (
        args.trim_start_ms < 0
        or (args.trim_end_ms is not None and args.trim_end_ms < 0)
        or args.fade_in_ms < 0
        or args.fade_out_ms < 0
    ):
        raise SystemExit("edge and fade durations must be non-negative")

    samples, sample_rate = sf.read(str(args.input), always_2d=True, dtype="float32")
    start = round(args.trim_start_ms * sample_rate / 1_000)
    end = (
        len(samples)
        if args.trim_end_ms is None
        else round(args.trim_end_ms * sample_rate / 1_000)
    )
    if start >= len(samples) or end > len(samples) or end <= start:
        raise SystemExit("trim edge is outside the input audio or produces an empty result")
    trimmed = samples[start:end].copy()
    fade_in = min(round(args.fade_in_ms * sample_rate / 1_000), len(trimmed))
    fade_out = min(round(args.fade_out_ms * sample_rate / 1_000), len(trimmed))
    if fade_in:
        trimmed[:fade_in] *= np.linspace(0.0, 1.0, fade_in, dtype=np.float32)[:, None]
    if fade_out:
        trimmed[-fade_out:] *= np.linspace(1.0, 0.0, fade_out, dtype=np.float32)[:, None]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(args.output), trimmed, sample_rate, subtype="PCM_16")
    print(f"wrote {args.output} duration_ms={round(len(trimmed) * 1000 / sample_rate)}")


if __name__ == "__main__":
    main()
