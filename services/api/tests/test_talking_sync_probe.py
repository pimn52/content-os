from pathlib import Path
from types import SimpleNamespace

from app.talking.sync_probe import LocalTalkingMotionProbe, talking_output_word_timings


def test_motion_probe_runs_in_configured_runtime_and_caches_exact_video(tmp_path: Path, monkeypatch) -> None:
    python = tmp_path / "python.exe"
    ffmpeg = tmp_path / "ffmpeg.exe"
    python.write_bytes(b"runtime")
    ffmpeg.write_bytes(b"ffmpeg")
    data_root = tmp_path / "data"
    video = data_root / "assets" / "talking.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    calls = []
    payload = (
        '{"state":"measured","method":"mediapipe_face_mesh_motion_envelope_v1",'
        '"best_video_lag_ms":-80,"correlation":0.31}\n'
    )

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=payload, stderr="")

    monkeypatch.setattr("app.talking.sync_probe.subprocess.run", run)
    probe = LocalTalkingMotionProbe(python, ffmpeg, data_root)

    words = [
        {"text": "来", "start_ms": 3330, "end_ms": 3570},
        {"text": "拆", "start_ms": 3570, "end_ms": 4170},
    ]
    first = probe.measure_talking(video, word_timings=words)
    second = probe.measure_talking(video, word_timings=words)

    assert first == second
    assert first["best_video_lag_ms"] == -80
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[0] == str(python.resolve())
    assert command[command.index("--mode") + 1] == "talking"
    assert command[command.index("--video") + 1] == str(video.resolve())
    assert command[command.index("--word-timings-json") + 1] == (
        '[{"text":"来","start_ms":3330,"end_ms":3570},'
        '{"text":"拆","start_ms":3570,"end_ms":4170}]'
    )
    assert kwargs["env"]["MPLCONFIGDIR"] == str(data_root / "generated" / "talking-probe-cache")


def test_motion_probe_returns_observation_unavailable_outside_data_root(tmp_path: Path) -> None:
    python = tmp_path / "python.exe"
    ffmpeg = tmp_path / "ffmpeg.exe"
    python.write_bytes(b"runtime")
    ffmpeg.write_bytes(b"ffmpeg")
    data_root = tmp_path / "data"
    data_root.mkdir()
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"video")

    result = LocalTalkingMotionProbe(python, ffmpeg, data_root).measure_talking(outside)

    assert result["state"] == "unavailable"
    assert result["reason"] == "video_outside_data_root"
    assert "quality_pass" in result["claim"]


def test_output_word_timings_use_verified_master_and_complete_slice_words() -> None:
    master = SimpleNamespace(
        content_hash="a" * 64,
        duration_ms=10_000,
        metadata={
            "voice_word_timing": {
                "source_sha256": "a" * 64,
                "words": [
                    {"text": "前", "start_ms": 500, "end_ms": 700},
                    {"text": "一", "start_ms": 2_000, "end_ms": 2_200},
                    {"text": "二", "start_ms": 2_300, "end_ms": 2_500},
                    {"text": "三", "start_ms": 2_600, "end_ms": 2_800},
                    {"text": "四", "start_ms": 2_900, "end_ms": 3_100},
                    {"text": "越界", "start_ms": 3_100, "end_ms": 3_300},
                ],
            }
        },
    )
    generation = {"master_slice_start_ms": 2_000, "master_slice_end_ms": 3_200}

    assert talking_output_word_timings(master, generation) == [
        {"text": "二", "start_ms": 300, "end_ms": 500},
        {"text": "三", "start_ms": 600, "end_ms": 800},
        {"text": "四", "start_ms": 900, "end_ms": 1_100},
    ]
    master.metadata["voice_word_timing"]["source_sha256"] = "b" * 64
    assert talking_output_word_timings(master, generation) == []
