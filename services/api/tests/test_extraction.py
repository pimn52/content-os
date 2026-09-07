import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app.db import Database
from app.domain.models import Asset, Clip, RationalFps
from app.media.extraction import (
    AudioUnavailableError,
    ExtractionBinaryMissing,
    ExtractionOutputError,
    ExtractionProcessError,
    ExtractionTimeout,
    ExtractionValidationError,
    MediaExtractor,
)
from app.media.ffprobe import FFProbeAdapter
from app.media.importer import MediaImporter


def _asset(path: Path, *, has_audio=True, duration_ms=1000):
    return Asset(source_file=str(path), content_hash="a" * 64, duration_ms=duration_ms, width=320, height=240, fps=RationalFps(numerator=25, denominator=1), has_audio=has_audio, authorization_reference="test", imported_at=datetime.now(timezone.utc))


def test_audio_argv_no_shell_and_idempotent(monkeypatch, tmp_path: Path):
    source = tmp_path / "源 file.mp4"
    source.write_bytes(b"source")
    asset = _asset(source)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        Path(argv[-1]).write_bytes(b"RIFF fake")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    extractor = MediaExtractor(tmp_path / "data", command=("ffmpeg-custom",), sample_rate=22_050, channels=2)
    first = extractor.extract_audio(asset)
    second = extractor.extract_audio(asset)
    assert first.path == second.path and first.path.stat().st_size > 0
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert kwargs["shell"] is False
    assert "-ar" in argv and argv[argv.index("-ar") + 1] == "22050"
    assert "-ac" in argv and argv[argv.index("-ac") + 1] == "2"
    assert not list(first.path.parent.glob("*.tmp"))


def test_keyframe_midpoint_cross_asset_and_audio_validation(monkeypatch, tmp_path: Path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    asset = _asset(source)
    clip = Clip(asset_id=asset.id, start_ms=100, end_ms=701, asset_duration_ms=1000)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        Path(argv[-1]).write_bytes(b"jpeg")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = MediaExtractor(tmp_path / "data").extract_keyframe(asset, clip)
    assert result.timestamp_ms == 400.5
    assert "-ss" in calls[0][0] and calls[0][0][calls[0][0].index("-ss") + 1] == "0.4005"
    assert result.path.is_file() and "400.500ms" in result.path.name
    with pytest.raises(ExtractionValidationError):
        MediaExtractor(tmp_path / "other").extract_keyframe(_asset(source, duration_ms=900), clip)
    with pytest.raises(ExtractionValidationError):
        MediaExtractor(tmp_path / "other").extract_keyframe(_asset(tmp_path / "other.mp4"), clip)
    with pytest.raises(AudioUnavailableError):
        MediaExtractor(tmp_path / "audio").extract_audio(_asset(source, has_audio=False))


def test_config_and_failure_cleanup(monkeypatch, tmp_path: Path):
    with pytest.raises(ValueError):
        MediaExtractor(tmp_path, timeout_seconds=0)
    with pytest.raises(ValueError):
        MediaExtractor(tmp_path, sample_rate=999)
    with pytest.raises(ValueError):
        MediaExtractor(tmp_path, channels=0)
    with pytest.raises(ValueError):
        MediaExtractor(tmp_path, image_width=320)
    with pytest.raises(ValueError):
        MediaExtractor(tmp_path, image_quality=32)
    with pytest.raises(ValueError):
        MediaExtractor(tmp_path, timeout_seconds=float("inf"))
    with pytest.raises(ValueError):
        MediaExtractor(tmp_path, command=[])
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    extractor = MediaExtractor(tmp_path / "data", command="missing-ffmpeg")
    with pytest.raises(ExtractionBinaryMissing):
        extractor.extract_audio(_asset(source))
    assert not list((tmp_path / "data" / "assets" / "audio").glob("*"))

    def fail_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 9, "", "controlled failure")

    monkeypatch.setattr(subprocess, "run", fail_run)
    failing = MediaExtractor(tmp_path / "data2")
    with pytest.raises(ExtractionProcessError):
        failing.extract_audio(_asset(source))
    assert not list((tmp_path / "data2" / "assets" / "audio").glob("*"))

    def timeout_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1)

    monkeypatch.setattr(subprocess, "run", timeout_run)
    timed_out = MediaExtractor(tmp_path / "data3")
    with pytest.raises(ExtractionTimeout):
        timed_out.extract_audio(_asset(source))
    assert not list((tmp_path / "data3" / "assets" / "audio").glob("*"))

    def empty_run(argv, **kwargs):
        Path(argv[-1]).touch()
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", empty_run)
    empty = MediaExtractor(tmp_path / "data4")
    with pytest.raises(ExtractionOutputError):
        empty.extract_audio(_asset(source))
    audio_dir = tmp_path / "data4" / "assets" / "audio"
    assert not list(audio_dir.glob("*"))


def test_real_audio_and_keyframe_outputs(tmp_path: Path):
    ffmpeg = os.environ.get("CONTENT_OS_FFMPEG") or shutil.which("ffmpeg")
    ffprobe = os.environ.get("CONTENT_OS_FFPROBE") or shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("real ffmpeg/ffprobe binaries are unavailable")
    source = tmp_path / "real source 中文.mp4"
    subprocess.run([ffmpeg, "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25", "-f", "lavfi", "-i", "sine=frequency=800", "-t", "1.2", "-c:v", "mpeg4", "-c:a", "aac", str(source)], check=True, capture_output=True)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    db = Database(tmp_path / "store.sqlite")
    asset = MediaImporter(db, tmp_path / "data", FFProbeAdapter(ffprobe)).import_path(source, "rights")
    clip = Clip(asset_id=asset.id, start_ms=100, end_ms=min(asset.duration_ms - 1, 1000), asset_duration_ms=asset.duration_ms)
    extractor = MediaExtractor(tmp_path / "data", command=ffmpeg, sample_rate=16_000, channels=1)
    audio = extractor.extract_audio(asset)
    frame = extractor.extract_keyframe(asset, clip)
    assert audio.path.is_file() and audio.path.stat().st_size > 0
    assert frame.path.is_file() and frame.path.stat().st_size > 0
    audio_probe = json.loads(subprocess.run([ffprobe, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(audio.path)], check=True, capture_output=True, text=True).stdout)
    audio_stream = next(stream for stream in audio_probe["streams"] if stream["codec_type"] == "audio")
    assert int(audio_stream["sample_rate"]) == 16_000 and int(audio_stream["channels"]) == 1
    audio_duration = float(audio_probe["format"]["duration"])
    assert abs(audio_duration - asset.duration_ms / 1000) < 0.1
    assert abs(float(audio_stream.get("start_time", "0"))) < 0.01
    image_probe = json.loads(subprocess.run([ffprobe, "-v", "error", "-print_format", "json", "-show_streams", str(frame.path)], check=True, capture_output=True, text=True).stdout)
    image_stream = next(stream for stream in image_probe["streams"] if stream["codec_type"] == "video")
    assert (int(image_stream["width"]), int(image_stream["height"])) == (320, 240)
    assert clip.start_ms < float(frame.timestamp_ms) < clip.end_ms
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert extractor.extract_audio(asset).path == audio.path
    assert extractor.extract_keyframe(asset, clip).path == frame.path
    assert len(list(audio.path.parent.glob("*"))) == 1
    assert len(list(frame.path.parent.glob("*"))) == 1
    db.close()
