"""Local face-motion probe executed inside the configured LatentSync runtime.

The result measures face visibility, head stability, mouth activity and the
best temporal correlation between mouth motion and the audio envelope. It is
not SyncNet confidence and cannot prove phoneme-level lip sync or likeness.
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import tempfile
from pathlib import Path
import wave

import cv2
import mediapipe as mp
import numpy as np


MOUTH_UPPER = 13
MOUTH_LOWER = 14
MOUTH_LEFT = 61
MOUTH_RIGHT = 291
LEFT_EYE_OUTER = 33
RIGHT_EYE_OUTER = 263
NOSE_TIP = 1
SAMPLE_FPS = 25
MAX_LAG_FRAMES = 10


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("talking", "reference"), required=True)
    parser.add_argument("--video", required=True)
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument("--start-ms", type=int, default=0)
    parser.add_argument("--end-ms", type=int, default=None)
    parser.add_argument("--word-timings-json")
    args = parser.parse_args()
    video = Path(args.video).resolve()
    if not video.is_file():
        raise SystemExit("video_not_found")
    if args.start_ms < 0 or (args.end_ms is not None and args.end_ms <= args.start_ms):
        raise SystemExit("interval_invalid")
    if args.mode == "talking":
        result = measure_talking(
            video,
            Path(args.ffmpeg),
            word_timings=None if not args.word_timings_json else json.loads(args.word_timings_json),
        )
    else:
        result = measure_reference(video, args.start_ms, args.end_ms or args.start_ms + 1_000)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


def measure_talking(
    video: Path,
    ffmpeg: Path,
    *,
    word_timings: object = None,
) -> dict[str, object]:
    landmarks = _landmarks(video, 0, None)
    with tempfile.TemporaryDirectory(prefix="content-os-sync-probe-") as temporary:
        audio_path = Path(temporary) / "audio.wav"
        completed = subprocess.run(
            [
                str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
                "-c:a", "pcm_s16le", str(audio_path),
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
        if completed.returncode != 0 or not audio_path.is_file():
            return _unavailable(landmarks, "audio_decode_unavailable")
        with wave.open(str(audio_path), "rb") as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 16_000):
                return _unavailable(landmarks, "audio_format_invalid")
            pcm = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2").astype(np.float32)
            sample_rate = audio.getframerate()
    audio_envelope = _audio_envelope(pcm, sample_rate, len(landmarks["times_ms"]))
    mouth = np.asarray(landmarks["mouth_opening"], dtype=np.float64)
    mouth_width = np.asarray(landmarks["mouth_width"], dtype=np.float64)
    if len(audio_envelope) != len(mouth) or len(mouth) < 20:
        return _unavailable(landmarks, "aligned_samples_insufficient")
    level_correlation, level_lag = _best_correlation(audio_envelope, mouth)
    motion_correlation, motion_lag = _best_correlation(
        np.abs(np.diff(audio_envelope, prepend=audio_envelope[0])),
        np.abs(np.diff(mouth, prepend=mouth[0])),
    )
    if motion_correlation >= level_correlation:
        correlation, lag, signal = motion_correlation, motion_lag, "mouth_and_audio_motion"
    else:
        correlation, lag, signal = level_correlation, level_lag, "mouth_opening_and_audio_rms"
    if correlation <= 0:
        signal = "inconclusive"
        lag = 0
    result = {
        **_reference_metrics(landmarks),
        "state": "measured",
        "method": "mediapipe_face_mesh_motion_envelope_v1",
        "signal": signal,
        "best_video_lag_ms": round(lag * 1000 / SAMPLE_FPS),
        "correlation": round(correlation, 4),
        "level_correlation": round(level_correlation, 4),
        "motion_correlation": round(motion_correlation, 4),
        "level_best_video_lag_ms": round(level_lag * 1000 / SAMPLE_FPS),
        "motion_best_video_lag_ms": round(motion_lag * 1000 / SAMPLE_FPS),
        "audio_envelope_std": round(float(np.std(audio_envelope)), 6),
        "mouth_signal_std": round(float(np.std(mouth)), 6),
        "limitations": [
            "This is not SyncNet confidence or phoneme-level viseme matching.",
            "Head motion and expression changes can affect the envelope correlation.",
            "A near-zero offset does not prove natural or acceptable visible lip sync.",
        ],
    }
    result["word_timing_alignment"] = _word_timing_alignment(
        landmarks["times_ms"], mouth, mouth_width, audio_envelope, word_timings,
    )
    return result


def _word_timing_alignment(
    raw_times: object,
    mouth: np.ndarray,
    mouth_width: np.ndarray,
    audio_envelope: np.ndarray,
    word_timings: object,
) -> dict[str, object]:
    """Compare persisted word intervals with local mouth/audio peaks."""
    if word_timings is None:
        return {"state": "not_configured", "claim": "word_level_alignment_unknown"}
    if not isinstance(word_timings, list) or not 1 <= len(word_timings) <= 10:
        return {"state": "invalid", "claim": "word_level_alignment_unverified"}
    times = np.asarray(raw_times, dtype=np.int64)
    values = []
    for word in word_timings:
        if not isinstance(word, dict):
            return {"state": "invalid", "claim": "word_level_alignment_unverified"}
        text = word.get("text")
        start_ms = word.get("start_ms")
        end_ms = word.get("end_ms")
        if (
            not isinstance(text, str) or not text.strip()
            or type(start_ms) is not int or type(end_ms) is not int
            or start_ms < 0 or end_ms <= start_ms
        ):
            return {"state": "invalid", "claim": "word_level_alignment_unverified"}
        indexes = np.flatnonzero((times >= start_ms) & (times < end_ms))
        if len(indexes) < 2:
            continue
        word_mouth = mouth[indexes]
        word_width = mouth_width[indexes]
        word_audio = audio_envelope[indexes]
        mouth_peak = int(times[indexes[int(np.argmax(word_mouth))]])
        audio_peak = int(times[indexes[int(np.argmax(word_audio))]])
        values.append({
            "text": text.strip(),
            "start_ms": start_ms,
            "end_ms": end_ms,
            "audio_peak_ms": audio_peak,
            "mouth_peak_ms": mouth_peak,
            "mouth_minus_audio_peak_ms": mouth_peak - audio_peak,
            "mouth_opening_range": round(float(word_mouth.max() - word_mouth.min()), 6),
            "mouth_width_min": round(float(word_width.min()), 6),
            "mouth_width_max": round(float(word_width.max()), 6),
            "mouth_width_at_peak": round(float(word_width[int(np.argmax(word_mouth))]), 6),
            "mouth_width_range": round(float(word_width.max() - word_width.min()), 6),
        })
    if not values:
        return {"state": "unavailable", "claim": "word_level_alignment_unknown"}
    final = values[-1]
    delta = final["mouth_minus_audio_peak_ms"]
    classification = (
        "mouth_after_audio_peak" if delta >= 120
        else "mouth_before_audio_peak" if delta <= -120
        else "peaks_near_each_other"
    )
    return {
        "state": "measured",
        "method": "persisted_word_interval_audio_rms_mouth_opening_and_width_peaks",
        "final_word_classification": classification,
        "words": values,
        "claim": "word_level_observation_only_not_visible_lip_sync_approval",
        "limitations": [
            "A peak comparison is not a phoneme/viseme classifier.",
            "Short syllables at 25 fps resolve only to about 40 ms.",
            "Human visual judgment remains required.",
        ],
    }


def measure_reference(video: Path, start_ms: int, end_ms: int) -> dict[str, object]:
    landmarks = _landmarks(video, start_ms, end_ms)
    return {
        **_reference_metrics(landmarks),
        "state": "measured" if landmarks["detected_ratio"] >= 0.5 else "unavailable",
        "method": "mediapipe_face_mesh_reference_window_v2",
        "limitations": [
            "Face-mesh motion is an observational proxy, not identity or likeness approval.",
            "Head stability and mouth activity do not prove front-facing natural performance.",
        ],
    }


def _landmarks(video: Path, start_ms: int, end_ms: int | None) -> dict[str, object]:
    capture = cv2.VideoCapture(str(video))
    if not capture.isOpened():
        raise SystemExit("video_open_failed")
    if start_ms:
        capture.set(cv2.CAP_PROP_POS_MSEC, float(start_ms))
    frame_rate = capture.get(cv2.CAP_PROP_FPS)
    if not math.isfinite(frame_rate) or frame_rate <= 0:
        frame_rate = 25.0
    step = max(1, round(frame_rate / SAMPLE_FPS))
    values: list[dict[str, float]] = []
    times: list[int] = []
    frame_index = 0
    with mp.solutions.face_mesh.FaceMesh(
        static_image_mode=False,
        max_num_faces=1,
        refine_landmarks=True,
        min_detection_confidence=0.5,
        min_tracking_confidence=0.5,
    ) as mesh:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            time_ms = int(round(capture.get(cv2.CAP_PROP_POS_MSEC)))
            if end_ms is not None and time_ms > end_ms:
                break
            if frame_index % step == 0:
                height, width = frame.shape[:2]
                if max(height, width) > 640:
                    scale = 640 / max(height, width)
                    frame = cv2.resize(frame, (round(width * scale), round(height * scale)))
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                result = mesh.process(rgb)
                if result.multi_face_landmarks:
                    points = result.multi_face_landmarks[0].landmark
                    mouth = _distance(points[MOUTH_UPPER], points[MOUTH_LOWER])
                    mouth_width = _distance(points[MOUTH_LEFT], points[MOUTH_RIGHT])
                    eye = max(1e-6, _distance(points[LEFT_EYE_OUTER], points[RIGHT_EYE_OUTER]))
                    xs = [point.x for point in points]
                    ys = [point.y for point in points]
                    values.append({
                        "mouth": mouth / eye,
                        "mouth_width": mouth_width / eye,
                        "nose_x": points[NOSE_TIP].x,
                        "nose_y": points[NOSE_TIP].y,
                        "face_width": max(xs) - min(xs),
                        "face_height": max(ys) - min(ys),
                    })
                else:
                    values.append({"missing": 1.0})
                times.append(time_ms)
            frame_index += 1
    capture.release()
    mouth = []
    mouth_width = []
    nose_x = []
    nose_y = []
    face_width = []
    face_height = []
    valid = []
    for value in values:
        present = "missing" not in value
        valid.append(present)
        if present:
            mouth.append(value["mouth"])
            mouth_width.append(value["mouth_width"])
            nose_x.append(value["nose_x"])
            nose_y.append(value["nose_y"])
            face_width.append(value["face_width"])
            face_height.append(value["face_height"])
    if valid:
        indexes = np.arange(len(valid), dtype=np.float64)
        for series in (mouth, mouth_width, nose_x, nose_y, face_width, face_height):
            if series:
                values_by_index = np.flatnonzero(valid)
                series[:] = np.interp(indexes, values_by_index, np.asarray(series)).tolist()
            else:
                series[:] = [0.0] * len(valid)
    return {
        "times_ms": times,
        "mouth_opening": mouth,
        "mouth_width": mouth_width,
        "nose_x": nose_x,
        "nose_y": nose_y,
        "face_width": face_width,
        "face_height": face_height,
        "detected_ratio": (sum(valid) / len(valid)) if valid else 0.0,
    }


def _reference_metrics(landmarks: dict[str, object]) -> dict[str, object]:
    mouth = np.asarray(landmarks["mouth_opening"], dtype=np.float64)
    nose_x = np.asarray(landmarks["nose_x"], dtype=np.float64)
    nose_y = np.asarray(landmarks["nose_y"], dtype=np.float64)
    widths = np.asarray(landmarks["face_width"], dtype=np.float64)
    heights = np.asarray(landmarks["face_height"], dtype=np.float64)
    detected = float(landmarks["detected_ratio"])
    if len(mouth) < 5:
        return {
            "detected_ratio": round(detected, 4),
            "face_visibility_score": round(detected, 4),
            "head_stability_score": 0.0,
            "mouth_activity_score": 0.0,
            "reference_quality_score": 0.0,
        }
    displacement = np.sqrt(np.diff(nose_x, prepend=nose_x[0]) ** 2
                           + np.diff(nose_y, prepend=nose_y[0]) ** 2)
    head_stability = max(0.0, min(1.0, 1.0 - float(np.mean(displacement)) / 0.08))
    mouth_activity = max(0.0, min(1.0, float(np.std(mouth)) / 0.08))
    face_size = max(0.0, min(1.0, float(np.median(widths))))
    face_aspect = widths / np.maximum(heights, 1e-6)
    aspect_stability = max(
        0.0,
        min(1.0, 1.0 - float(np.std(face_aspect)) / 0.08),
    )
    score = (0.20 * detected + 0.30 * head_stability
             + 0.20 * aspect_stability + 0.20 * mouth_activity
             + 0.10 * face_size)
    return {
        "detected_ratio": round(detected, 4),
        "face_size_ratio": round(face_size, 4),
        "head_stability_score": round(head_stability, 4),
        "mouth_activity_score": round(mouth_activity, 4),
        "face_aspect_median": round(float(np.median(face_aspect)), 4),
        "face_aspect_stability_score": round(aspect_stability, 4),
        "reference_quality_score": round(score, 4),
    }


def _audio_envelope(samples: np.ndarray, sample_rate: int, count: int) -> np.ndarray:
    window = max(1, sample_rate // SAMPLE_FPS)
    values = []
    for index in range(count):
        start = index * window
        end = min(len(samples), start + window)
        if start >= len(samples):
            values.append(0.0)
            continue
        block = samples[start:end]
        values.append(float(np.sqrt(np.mean(block * block))) if len(block) else 0.0)
    return np.asarray(values, dtype=np.float64)


def _best_correlation(audio: np.ndarray, video: np.ndarray) -> tuple[float, int]:
    best_value, best_lag = -2.0, 0
    for lag in range(-MAX_LAG_FRAMES, MAX_LAG_FRAMES + 1):
        if lag >= 0:
            left, right = audio[lag:], video[:len(video) - lag]
        else:
            left, right = audio[:len(audio) + lag], video[-lag:]
        if len(left) < 20 or not np.isfinite(left).all() or not np.isfinite(right).all():
            continue
        left_centered = left - float(np.mean(left))
        right_centered = right - float(np.mean(right))
        denominator = math.sqrt(
            float(np.dot(left_centered, left_centered))
            * float(np.dot(right_centered, right_centered))
        )
        if denominator == 0:
            continue
        correlation = float(np.dot(left_centered, right_centered)) / denominator
        if correlation > best_value:
            best_value, best_lag = correlation, lag
    return (0.0, 0) if best_value == -2.0 else (best_value, best_lag)


def _distance(left, right) -> float:
    return math.hypot(left.x - right.x, left.y - right.y)


def _unavailable(landmarks: dict[str, object], reason: str) -> dict[str, object]:
    return {
        **_reference_metrics(landmarks),
        "state": "unavailable",
        "reason": reason,
        "method": "mediapipe_face_mesh_motion_envelope_v1",
    }


if __name__ == "__main__":
    raise SystemExit(main())
