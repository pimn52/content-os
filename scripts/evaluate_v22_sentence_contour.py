"""V22: one user-approved 45-minute CPU bound for the V21 sentence design.

This wrapper intentionally reuses the exact-copy, source, reference, QA and
composition checks of V21, while keeping a new idempotency/evidence namespace.
"""
from __future__ import annotations

import evaluate_v21_sentence_contour as evaluation


evaluation.RUN_ID = "v22"
evaluation.TIMEOUT_SECONDS = 2700
evaluation.EVIDENCE = evaluation.ROOT / "content-os-data" / "evaluation-evidence" / "v22-sentence-contour"
evaluation.MANIFEST = evaluation.EVIDENCE / "v22-sentence-contour.json"
evaluation.CALL_KEY = "v22-causal-sentence-one-inference-cpu-2700s"
evaluation.CUDA_LAUNCH_FAILURE = evaluation.EVIDENCE / "v22-cuda-pre-inference-failure.json"


if __name__ == "__main__":
    raise SystemExit(evaluation.main())
