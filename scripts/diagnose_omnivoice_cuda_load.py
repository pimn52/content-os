"""One bounded, inference-free OmniVoice CUDA model-load diagnostic.

This does not generate audio or create a provider call. It uses the installed
local model snapshot and preserves the subprocess error for capability review.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import time


CHILD = """import json, time, torch
from omnivoice.models.omnivoice import OmniVoice
start = time.monotonic()
model = OmniVoice.from_pretrained(MODEL_PATH, device_map='cuda', dtype=torch.float16)
print(json.dumps({'loaded': True, 'elapsed_seconds': round(time.monotonic()-start, 3),
                  'model_device': str(model.device),
                  'allocated_bytes': torch.cuda.memory_allocated(),
                  'reserved_bytes': torch.cuda.memory_reserved()}), flush=True)
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    if not args.runtime.is_file() or not (args.model / "model.safetensors").is_file():
        parser.error("the local runtime and model snapshot must already exist")
    if args.evidence.exists():
        parser.error("evidence already exists; do not overwrite a diagnostic")
    code = CHILD.replace("MODEL_PATH", repr(str(args.model)))
    started = time.monotonic()
    record: dict[str, object] = {
        "diagnostic": "omnivoice-cuda-model-load-only",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "runtime": str(args.runtime),
        "model": str(args.model),
        "timeout_seconds": args.timeout,
        "inference_attempted": False,
    }
    try:
        result = subprocess.run(
            [str(args.runtime), "-c", code], capture_output=True, text=True,
            timeout=args.timeout, check=False,
        )
        record.update(status="loaded" if result.returncode == 0 else "load_failed",
                      returncode=result.returncode, stdout=result.stdout[-12000:],
                      stderr=result.stderr[-20000:])
    except subprocess.TimeoutExpired as exc:
        record.update(status="load_timeout", stdout=str(exc.stdout or "")[-12000:],
                      stderr=str(exc.stderr or "")[-20000:])
    record["elapsed_seconds"] = round(time.monotonic() - started, 3)
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": record["status"], "evidence": str(args.evidence),
                      "elapsed_seconds": record["elapsed_seconds"]}))
    return 0 if record["status"] == "loaded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
