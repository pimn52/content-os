"""Bounded local face-box coverage probe; never certifies final crop suitability.

Run only with an explicitly scoped, authorized local source. This script uses
the already-installed MediaPipe/OpenCV runtime and makes no network requests.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _status(box: dict[str, int] | None, crop: tuple[int, int, int, int]) -> str:
    if box is None:
        return "unknown_face_detection"
    x, y, width, height = crop
    if box["x"] < x or box["y"] < y or box["x"] + box["width"] > x + width or box["y"] + box["height"] > y + height:
        return "face_outside_crop"
    return "face_box_inside_crop"


def main() -> int:
    import cv2
    import mediapipe as mp

    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--frames", type=int, default=125)
    parser.add_argument("--crop", nargs=4, type=int, metavar=("X", "Y", "WIDTH", "HEIGHT"), default=(460, 0, 360, 640))
    args = parser.parse_args()
    if args.frames < 1 or args.frames > 125:
        parser.error("this experiment is capped at 125 decoded frames")
    if args.source.resolve() == args.output.resolve():
        parser.error("output must not overwrite source")
    source_hash = _sha256(args.source)
    if source_hash != args.expected_sha256.lower():
        parser.error("source hash differs from the declared authorized Asset")
    capture = cv2.VideoCapture(str(args.source))
    if not capture.isOpened():
        raise RuntimeError("OpenCV could not decode the source")
    fps = float(capture.get(cv2.CAP_PROP_FPS))
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if fps <= 0 or width != 1280 or height != 720 or args.frames / fps > 5.001:
        raise RuntimeError("source metadata is outside the declared V73a scope")
    crop = tuple(args.crop)
    if crop[0] < 0 or crop[1] < 0 or crop[2] <= 0 or crop[3] <= 0 or crop[0] + crop[2] > width or crop[1] + crop[3] > height:
        raise RuntimeError("crop is outside the source image")
    observations: list[dict[str, object]] = []
    try:
        with mp.solutions.face_detection.FaceDetection(model_selection=0, min_detection_confidence=0.5) as detector:
            for index in range(args.frames):
                ok, frame = capture.read()
                if not ok:
                    observations.append({"frame_index": index, "time_ms": round(index * 1000 / fps), "state": "unknown_decode_failure"})
                    break
                result = detector.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                faces = result.detections or []
                box = None
                if len(faces) == 1:
                    relative = faces[0].location_data.relative_bounding_box
                    box = {
                        "x": round(relative.xmin * width), "y": round(relative.ymin * height),
                        "width": round(relative.width * width), "height": round(relative.height * height),
                    }
                observations.append({
                    "frame_index": index, "time_ms": round(index * 1000 / fps),
                    "detection_count": len(faces), "face_box": box,
                    "state": _status(box, crop),
                })
    finally:
        capture.release()
    counts = {state: sum(item["state"] == state for item in observations) for state in sorted({str(item["state"]) for item in observations})}
    final = "unusable_crop" if counts.get("face_outside_crop", 0) else "unknown"
    payload = {
        "evidence_class": "local_detector_diagnostic_not_publishability_review",
        "source_sha256": source_hash, "interval_ms": {"start": 0, "end": 5000},
        "crop": dict(zip(("x", "y", "width", "height"), crop, strict=True)),
        "method": {"name": "mediapipe_face_detection", "version": mp.__version__, "model_selection": 0, "minimum_detection_confidence": 0.5},
        "fps": fps, "decoded_frames": len(observations), "requested_frames": args.frames,
        "counts": counts, "face_geometry_decision": final, "subtitle_decision": "unknown",
        "overall_suitability": "unusable" if final == "unusable_crop" else "unknown",
        "limitations": ["face detector misses are unknown, not clear frames", "face boxes do not establish full-head or aesthetic quality", "no burned-in subtitle recognition"],
        "observations": observations,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: payload[key] for key in ("source_sha256", "decoded_frames", "counts", "face_geometry_decision", "subtitle_decision", "overall_suitability")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
