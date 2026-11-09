"""Product wrapper around the local face-motion probe child runtime."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import subprocess


class LocalTalkingMotionProbe:
    """Run bounded face-motion observations in the configured local runtime."""

    def __init__(
        self,
        python_executable: str | Path,
        ffmpeg: str | Path,
        data_root: str | Path,
        *,
        timeout_seconds: int = 180,
    ) -> None:
        self.python_executable = Path(python_executable).resolve()
        self.ffmpeg = Path(ffmpeg).resolve()
        self.data_root = Path(data_root).resolve()
        if (not self.python_executable.is_file() or not self.ffmpeg.is_file()
                or type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 600):
            raise ValueError("Talking motion probe runtime is invalid")
        self.timeout_seconds = timeout_seconds
        self._cache: dict[tuple[object, ...], dict[str, object]] = {}

    @classmethod
    def from_maintained_provider(cls, provider, *, ffmpeg: str | Path, data_root: str | Path):
        options = getattr(provider, "options", None)
        engine = options.get("engine") if isinstance(options, dict) else None
        if engine is None:
            return None
        python = Path(engine) / "python.exe"
        if not python.is_file():
            return None
        return cls(python, ffmpeg, data_root)

    def measure_talking(
        self,
        video: str | Path,
        *,
        word_timings: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        source = Path(video).resolve()
        words = None if word_timings is None else [
            {
                "text": str(item["text"]),
                "start_ms": int(item["start_ms"]),
                "end_ms": int(item["end_ms"]),
            }
            for item in word_timings
        ]
        word_key = "none" if words is None else hashlib.sha256(
            json.dumps(words, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        key = (str(source), "talking", None, None, word_key)
        if key not in self._cache:
            self._cache[key] = self._run("talking", source, word_timings=words)
        return dict(self._cache[key])

    def measure_reference(self, video: str | Path, start_ms: int, end_ms: int) -> dict[str, object]:
        if type(start_ms) is not int or type(end_ms) is not int or start_ms < 0 or end_ms <= start_ms:
            raise ValueError("Talking reference probe interval is invalid")
        source = Path(video).resolve()
        key = (str(source), "reference", start_ms, end_ms, "none")
        if key not in self._cache:
            self._cache[key] = self._run("reference", source, start_ms=start_ms, end_ms=end_ms)
        return dict(self._cache[key])

    def _run(
        self,
        mode: str,
        video: str | Path,
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
        word_timings: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        source = Path(video).resolve()
        if not source.is_relative_to(self.data_root) or not source.is_file():
            return _unavailable("video_outside_data_root")
        script = Path(__file__).with_name("sync_probe_child.py")
        if not script.is_file():
            return _unavailable("probe_child_missing")
        cache = self.data_root / "generated" / "talking-probe-cache"
        cache.mkdir(parents=True, exist_ok=True)
        environment = dict(os.environ)
        environment["MPLCONFIGDIR"] = str(cache)
        command = [
            str(self.python_executable),
            str(script),
            "--mode",
            mode,
            "--video",
            str(source),
            "--ffmpeg",
            str(self.ffmpeg),
        ]
        if start_ms is not None and end_ms is not None:
            command.extend(("--start-ms", str(start_ms), "--end-ms", str(end_ms)))
        if word_timings is not None:
            command.extend((
                "--word-timings-json",
                json.dumps(word_timings, ensure_ascii=False, separators=(",", ":")),
            ))
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                env=environment,
            )
        except (OSError, subprocess.TimeoutExpired):
            return _unavailable("probe_execution_failed")
        if completed.returncode != 0:
            return _unavailable("probe_execution_failed")
        lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        if not lines:
            return _unavailable("probe_output_missing")
        try:
            payload = json.loads(lines[-1])
        except json.JSONDecodeError:
            return _unavailable("probe_output_invalid")
        if (not isinstance(payload, dict)
                or payload.get("method") not in {
                    "mediapipe_face_mesh_motion_envelope_v1",
                    "mediapipe_face_mesh_reference_window_v1",
                    "mediapipe_face_mesh_reference_window_v2",
                }
                or payload.get("state") not in {"measured", "unavailable"}):
            return _unavailable("probe_output_invalid")
        return payload


def _unavailable(reason: str) -> dict[str, object]:
    return {
        "state": "unavailable",
        "reason": reason,
        "method": "local_talking_motion_probe_v1",
        "claim": "observation_unavailable_not_a_quality_pass",
    }


def talking_output_word_timings(master, generation, *, limit: int = 3) -> list[dict[str, object]]:
    """Project verified master words into output-relative final word intervals."""
    if type(limit) is not int or limit < 1 or limit > 10:
        raise ValueError("Talking word timing limit must be between 1 and 10")
    metadata = getattr(master, "metadata", None)
    timing = metadata.get("voice_word_timing") if isinstance(metadata, dict) else None
    if not isinstance(timing, dict) or timing.get("source_sha256") != getattr(master, "content_hash", None):
        return []
    words = timing.get("words")
    if not isinstance(words, list):
        return []
    if not isinstance(generation, dict):
        return []
    start_ms = generation.get("master_slice_start_ms")
    end_ms = generation.get("master_slice_end_ms")
    if type(start_ms) is not int or type(end_ms) is not int or start_ms < 0 or end_ms <= start_ms:
        start_ms, end_ms = 0, int(getattr(master, "duration_ms", 0))
    selected = []
    for word in words:
        if not isinstance(word, dict):
            continue
        text = word.get("text")
        word_start = word.get("start_ms")
        word_end = word.get("end_ms")
        if (
            not isinstance(text, str) or not text.strip()
            or type(word_start) is not int or type(word_end) is not int
            or word_start < start_ms or word_end > end_ms or word_end <= word_start
        ):
            continue
        selected.append({
            "text": text.strip(),
            "start_ms": word_start - start_ms,
            "end_ms": word_end - start_ms,
        })
    selected.sort(key=lambda item: (int(item["start_ms"]), int(item["end_ms"])))
    return selected[-limit:]


__all__ = ["LocalTalkingMotionProbe", "talking_output_word_timings"]
