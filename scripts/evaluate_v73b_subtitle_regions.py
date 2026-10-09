"""One bounded local OCR diagnostic; detected text is not subtitle clearance."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", required=True, type=Path)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--frame", required=True, type=Path, action="append")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if not 1 <= len(args.frame) <= 3:
        parser.error("the V73b local experiment permits one to three frames")
    if _sha256(args.source) != args.expected_sha256.lower():
        parser.error("source bytes differ from the authorized Asset")
    if any(frame.resolve() == args.output.resolve() for frame in args.frame):
        parser.error("output must not overwrite an input frame")
    site = args.site.resolve()
    if not site.is_dir():
        parser.error("isolated OCR site is unavailable")
    sys.path.insert(0, str(site))
    from rapidocr import RapidOCR

    model_dir = site / "rapidocr" / "models"
    models = {
        path.name: _sha256(path)
        for path in sorted(model_dir.glob("*.onnx"))
    }
    if len(models) != 3:
        raise RuntimeError("the expected three bundled OCR models were not found")
    started = time.perf_counter()
    engine = RapidOCR()
    observations = []
    for frame in args.frame:
        result = engine(frame.resolve(), use_det=True, use_cls=False, use_rec=True)
        boxes = [] if result.boxes is None else result.boxes.tolist()
        texts = [] if result.txts is None else list(result.txts)
        scores = [] if result.scores is None else list(result.scores)
        observations.append({
            "frame": frame.name, "sha256": _sha256(frame),
            "text_regions": [
                {"polygon": polygon, "text": text, "score": score}
                for polygon, text, score in zip(boxes, texts, scores, strict=True)
            ],
        })
    payload = {
        "evidence_class": "isolated_local_ocr_diagnostic_not_subtitle_clearance",
        "source_sha256": args.expected_sha256.lower(),
        "ocr_package": "rapidocr==3.9.2", "bundled_model_sha256": models,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "observations": observations,
        "decision": "detected_text_is_candidate_negative_evidence; missing_text_is_unknown",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"elapsed_seconds": payload["elapsed_seconds"], "regions_per_frame": [len(item["text_regions"]) for item in observations]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
