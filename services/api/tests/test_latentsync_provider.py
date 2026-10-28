from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import subprocess
import json
import os
import sys
import wave
import shutil
from uuid import uuid4

import pytest

from app.domain.models import AudioAsset, ConsentRecord, TalkingProfile, TranscriptSegment
from app.providers.latentsync import LatentSyncProvider
from app.providers.talking import TalkingExecutionOptions, TalkingInputError, TalkingProviderResponseError, TalkingReference


def _probe(frames: int) -> str:
    return json.dumps({"streams": [{"nb_read_frames": str(frames),
        "avg_frame_rate": "25/1", "duration": f"{frames / 25:.3f}"}],
        "format": {"duration": "99.0"}})


def _provider(tmp_path: Path, command_runner):
    repo = tmp_path / "latentsync"
    (repo / "scripts").mkdir(parents=True)
    (repo / "configs" / "unet").mkdir(parents=True)
    runner = repo / "scripts" / "inference.py"
    config = repo / "configs" / "unet" / "stage2.yaml"
    checkpoint = tmp_path / "latentsync_unet.pt"
    runner.write_text("# test runner\n", encoding="utf-8")
    config.write_text("model: {}\n", encoding="utf-8")
    checkpoint.write_bytes(b"checkpoint")
    return LatentSyncProvider(
        sys.executable,
        repo,
        checkpoint,
        ffmpeg_command="test-ffmpeg",
        command_runner=command_runner,
    )


def _inputs(tmp_path: Path) -> tuple[TalkingProfile, AudioAsset, TalkingReference]:
    reference_path = tmp_path / "ordinary-reference.mp4"
    narration_path = tmp_path / "verified-narration.wav"
    reference_path.write_bytes(b"ordinary reference")
    narration_path.write_bytes(b"verified narration")
    clip_id = uuid4()
    profile = TalkingProfile(
        name="Creator",
        provider="latentsync",
        reference_clip_ids=[clip_id],
        consent=ConsentRecord(
            subject_name="Creator",
            basis="self",
            confirmed=True,
            confirmed_at=datetime.now(timezone.utc),
        ),
        created_at=datetime.now(timezone.utc),
    )
    narration = AudioAsset(
        source_file=str(narration_path),
        content_hash="a" * 64,
        duration_ms=1_200,
        sample_rate=24_000,
        channels=1,
        authorization_reference="voice-rights",
        imported_at=datetime.now(timezone.utc),
        transcript_segments=[TranscriptSegment(start_ms=0, end_ms=1_000, text="verified speech")],
        transcript_source="voice-qa:test-asr",
    )
    reference = TalkingReference(
        clip_id=clip_id,
        source_path=reference_path,
        start_ms=1_250,
        end_ms=4_750,
    )
    return profile, narration, reference


def test_latentsync_stages_clip_and_invokes_official_cli_contract(tmp_path: Path) -> None:
    calls: list[tuple[list[str], Path, float]] = []

    def command_runner(argv, cwd, timeout):
        args = list(argv)
        calls.append((args, cwd, timeout))
        if "--video_out_path" in args:
            output = Path(args[args.index("--video_out_path") + 1])
            if not output.is_absolute():
                output = cwd / output
            output.write_bytes(b"generated video")
        elif "-show_entries" in args:
            return subprocess.CompletedProcess(args, 0, _probe(32), "")
        else:
            Path(args[-1]).write_bytes(b"staged reference")
        return subprocess.CompletedProcess(args, 0, "", "")

    provider = _provider(tmp_path, command_runner)
    profile, narration, reference = _inputs(tmp_path)
    output = tmp_path / "generated" / "talking.mp4"

    result = provider.synthesize(profile, narration, reference, output)

    assert result.video_path == output.resolve()
    assert result.provider_version == "1.5"
    assert len(calls) == 6
    ffmpeg, cwd, timeout = calls[0]
    assert cwd == provider.repo_root
    assert timeout == 900.0
    assert ffmpeg[0] == "test-ffmpeg"
    assert ffmpeg[ffmpeg.index("-ss") + 1] == "1.250"
    assert ffmpeg[ffmpeg.index("-t") + 1] == "1.280"
    assert "-an" in ffmpeg
    assert ffmpeg[ffmpeg.index("-r") + 1] == "25"
    assert "-count_frames" in calls[1][0]
    audio_prepare = calls[2][0]
    assert audio_prepare[0] == "test-ffmpeg"
    assert audio_prepare[audio_prepare.index("-ar") + 1] == "16000"
    inference = calls[3][0]
    assert inference[:2] == [sys.executable, str(provider.runner_path)]
    assert inference[inference.index("--inference_ckpt_path") + 1] == str(provider.checkpoint_path)
    assert inference[inference.index("--inference_steps") + 1] == "20"
    assert inference[inference.index("--guidance_scale") + 1] == "1.5"
    assert inference[inference.index("--seed") + 1] == "1247"
    raw_probe = calls[4][0]
    assert raw_probe[0] == "ffprobe"
    assert raw_probe[raw_probe.index("-show_entries") + 1] == "stream=nb_read_frames,avg_frame_rate,duration"
    normalize = calls[5][0]
    assert "-filter_complex" in normalize
    assert "tpad" not in normalize[normalize.index("-filter_complex") + 1]
    metadata = provider.runtime_metadata
    assert metadata.processing_resolution_px == 256
    assert metadata.estimated_cost.amount == 0
    assert metadata.estimated_cost.provider == "latentsync"


def test_latentsync_rejects_truncated_raw_output_before_duration_normalization(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def command_runner(argv, cwd, timeout):
        args = list(argv)
        calls.append(args)
        if "--video_out_path" in args:
            output = Path(args[args.index("--video_out_path") + 1])
            if not output.is_absolute():
                output = cwd / output
            output.write_bytes(b"truncated generated video")
            return subprocess.CompletedProcess(args, 0, "", "")
        if "-show_entries" in args:
            # The format/audio duration is long; a one-frame-short video must fail.
            return subprocess.CompletedProcess(args, 0, _probe(32 if Path(args[-1]).name == 'reference.mp4' else 29), "")
        Path(args[-1]).write_bytes(b"staged input")
        return subprocess.CompletedProcess(args, 0, "", "")

    provider = _provider(tmp_path, command_runner)
    profile, narration, reference = _inputs(tmp_path)

    with pytest.raises(TalkingProviderResponseError, match="raw output video is shorter"):
        provider.synthesize(profile, narration, reference, tmp_path / "generated" / "talking.mp4")

    assert any("-show_entries" in call for call in calls)
    assert not any("-filter_complex" in call for call in calls)


@pytest.mark.parametrize("probe_data", [
    '{"streams": [{"avg_frame_rate": "25/1", "duration": "1.280"}]}',
    _probe(31),
])
def test_latentsync_rejects_unproven_or_short_staged_frames_before_inference(tmp_path, probe_data):
    calls = []
    def runner(argv, cwd, timeout):
        args = list(argv)
        calls.append(args)
        if "-count_frames" in args:
            return subprocess.CompletedProcess(args, 0, probe_data, "")
        Path(args[-1]).write_bytes(b"reference")
        return subprocess.CompletedProcess(args, 0, "", "")
    provider = _provider(tmp_path, runner)
    profile, narration, reference = _inputs(tmp_path)
    with pytest.raises(TalkingProviderResponseError):
        provider.synthesize(profile, narration, reference, tmp_path / "output.mp4")
    assert not any("--video_out_path" in call for call in calls)


def test_latentsync_rejects_missing_raw_frame_evidence(tmp_path):
    def runner(argv, cwd, timeout):
        args = list(argv)
        if "--video_out_path" in args:
            output = Path(args[args.index("--video_out_path") + 1])
            (output if output.is_absolute() else cwd / output).write_bytes(b"generated")
        elif "-count_frames" in args:
            data = _probe(32) if Path(args[-1]).name == "reference.mp4" else '{"streams": [], "format": {"duration": "99"}}'
            return subprocess.CompletedProcess(args, 0, data, "")
        else:
            Path(args[-1]).write_bytes(b"input")
        return subprocess.CompletedProcess(args, 0, "", "")
    provider = _provider(tmp_path, runner)
    profile, narration, reference = _inputs(tmp_path)
    with pytest.raises(TalkingProviderResponseError, match="raw output video frame evidence"):
        provider.synthesize(profile, narration, reference, tmp_path / "output.mp4")


def test_latentsync_terminal_closeout_trims_model_only_silence_and_keeps_original_narration(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def command_runner(argv, cwd, timeout):
        args = list(argv)
        calls.append(args)
        if "--video_out_path" in args:
            output = Path(args[args.index("--video_out_path") + 1])
            if not output.is_absolute():
                output = cwd / output
            output.write_bytes(b"generated video with model context")
        elif "-show_entries" in args:
            # 1000ms narration + 175ms leading context + 600ms look-ahead,
            # rounded to the model's next 16-frame window = 1920ms.
            return subprocess.CompletedProcess(args, 0, _probe(48), "")
        else:
            Path(args[-1]).write_bytes(b"staged input")
        return subprocess.CompletedProcess(args, 0, "", "")

    provider = _provider(tmp_path, command_runner)
    profile, narration, reference = _inputs(tmp_path)
    result = provider.synthesize(
        profile,
        narration,
        reference,
        tmp_path / "generated" / "terminal-closeout.mp4",
        TalkingExecutionOptions(
            terminal_face_closeout=True,
            terminal_delivery_end_ms=1_000,
            provider_parameters={"trailing_silence_lookahead_ms": 600},
            parameter_sources={"trailing_silence_lookahead_ms": "provider_default"},
            profile_reference="talking:local:latentsync:LatentSync-1.5:local-compatibility:test-machine",
        ),
    )

    assert result.video_path.is_file()
    reference_stage, _reference_probe, model_audio, _inference, _raw_probe, normalize = calls
    assert reference_stage[reference_stage.index("-t") + 1] == "1.920"
    assert "anullsrc=r=16000:cl=mono" in model_audio
    assert model_audio[model_audio.index("-t") + 1] == "0.175"
    model_filter = model_audio[model_audio.index("-filter_complex") + 1]
    assert "concat=n=2:v=0:a=1" in model_filter
    assert "apad=pad_dur=0.745" in model_filter
    normalized_filter = normalize[normalize.index("-filter_complex") + 1]
    assert "trim=start=0.175:duration=1.000" in normalized_filter
    assert "tpad=stop_mode=clone" not in normalized_filter
    assert normalize[normalize.index("-map") + 3] == "1:a:0"


def test_latentsync_terminal_closeout_rejects_insufficient_reference_before_inference(tmp_path: Path) -> None:
    provider = _provider(
        tmp_path,
        lambda argv, cwd, timeout: subprocess.CompletedProcess(list(argv), 0, "", ""),
    )
    profile, narration, reference = _inputs(tmp_path)
    short_reference = TalkingReference(
        clip_id=reference.clip_id,
        source_path=reference.source_path,
        start_ms=reference.start_ms,
        end_ms=reference.start_ms + 1_000,
    )

    with pytest.raises(TalkingInputError, match="continuous reference clip"):
        provider.synthesize(
            profile,
            narration,
            short_reference,
            tmp_path / "generated" / "terminal-closeout.mp4",
            TalkingExecutionOptions(
                terminal_face_closeout=True,
                terminal_delivery_end_ms=1_000,
                provider_parameters={"trailing_silence_lookahead_ms": 600},
            ),
        )


def test_latentsync_rejects_non_mp4_output_and_missing_result(tmp_path: Path) -> None:
    provider = _provider(
        tmp_path,
        lambda argv, cwd, timeout: subprocess.CompletedProcess(list(argv), 0, "", ""),
    )
    profile, narration, reference = _inputs(tmp_path)

    with pytest.raises(TalkingInputError, match=r"\.mp4"):
        provider.synthesize(profile, narration, reference, tmp_path / "generated" / "talking.mov")

    with pytest.raises(TalkingProviderResponseError, match="produced no"):
        provider.synthesize(profile, narration, reference, tmp_path / "generated" / "talking.mp4")


def test_latentsync_resolves_core_relative_media_paths_from_repo_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _provider(
        tmp_path,
        lambda argv, cwd, timeout: subprocess.CompletedProcess(list(argv), 1, "", ""),
    )
    relative = Path("content-os-data") / "assets" / "ordinary-reference.mp4"
    expected = (tmp_path / relative).resolve()
    expected.parent.mkdir(parents=True)
    expected.write_bytes(b"ordinary reference")
    monkeypatch.chdir(tmp_path)

    assert provider._resolve_input_path(relative) == expected


@pytest.mark.parametrize("duration_ms", [1400, 2820])
def test_real_ffmpeg_frame_and_audio_handoff_without_model(tmp_path, duration_ms):
    from app.runtime import resolve_local_executable
    if not os.environ.get("CONTENT_OS_FFMPEG"):
        pytest.skip("set CONTENT_OS_FFMPEG to the runtime used by the worker")
    ffmpeg = resolve_local_executable("ffmpeg")
    ffprobe = resolve_local_executable("ffprobe")
    if not (Path(ffmpeg).is_file() or shutil.which(ffmpeg)) or not (Path(ffprobe).is_file() or shutil.which(ffprobe)):
        pytest.skip("local FFmpeg and FFprobe required")
    class CopyStaged(LatentSyncProvider):
        def _run_inference(self, video_path, audio_path, target):
            shutil.copyfile(video_path, target)

    provider = _provider(tmp_path, lambda *args: None)
    provider.__class__ = CopyStaged
    provider.ffmpeg_command = (ffmpeg,)
    provider.ffprobe_command = (ffprobe,)
    provider._command_runner = provider._run_command
    profile, narration, reference = _inputs(tmp_path)
    subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
        "testsrc2=size=64x112:rate=25:duration=4", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        str(reference.source_path)], check=True, capture_output=True)
    with wave.open(narration.source_file, "wb") as pcm:
        pcm.setnchannels(1); pcm.setsampwidth(2); pcm.setframerate(24000)
        pcm.writeframes(b"\x01\x00" * (duration_ms * 24))
    narration = narration.model_copy(update={"duration_ms": duration_ms})
    reference = TalkingReference(clip_id=reference.clip_id, source_path=reference.source_path,
                                 start_ms=0, end_ms=4000)
    output = tmp_path / "output.mp4"
    provider.synthesize(profile, narration, reference, output)
    result = subprocess.run([ffprobe, "-v", "error", "-count_frames", "-show_entries",
        "stream=codec_type,nb_read_frames,duration,avg_frame_rate", "-of", "json", str(output)],
        check=True, capture_output=True, text=True)
    streams = {item["codec_type"]: item for item in json.loads(result.stdout)["streams"]}
    assert int(streams["video"]["nb_read_frames"]) == (duration_ms * 25 + 999) // 1000
    assert streams["video"]["avg_frame_rate"] == "25/1"
    assert float(streams["video"]["duration"]) * 1000 >= duration_ms
    assert abs(float(streams["audio"]["duration"]) * 1000 - duration_ms) <= 50
