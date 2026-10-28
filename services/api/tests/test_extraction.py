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


def test_real_video_analysis_proxy_normalizes_vfr_and_stream_offset_without_source_change(tmp_path):
    ffmpeg=os.environ.get('CONTENT_OS_FFMPEG') or shutil.which('ffmpeg')
    ffprobe=os.environ.get('CONTENT_OS_FFPROBE') or shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:pytest.skip('local ffmpeg/ffprobe unavailable')
    source=tmp_path/'variable-clock.mp4'
    subprocess.run([ffmpeg,'-y','-v','error','-itsoffset','0.108','-f','lavfi','-i',
        'testsrc2=duration=3:size=320x180:rate=30','-f','lavfi','-i','sine=duration=3.4',
        '-map','0:v','-map','1:a','-vf',"select='not(eq(mod(n,13),0))'",'-fps_mode:v','vfr',
        '-c:v','libx264','-c:a','aac',str(source)],check=True,capture_output=True)
    before=hashlib.sha256(source.read_bytes()).hexdigest()
    probe=FFProbeAdapter(ffprobe)
    raw=probe.probe(source)
    assert float(raw.metadata['streams'][0]['start_time']) > 0
    asset=Asset(source_file=str(source),content_hash=before,duration_ms=raw.duration_ms,width=raw.width,height=raw.height,
        fps=RationalFps(numerator=30,denominator=1),authorization_reference='fixture',imported_at=datetime.now(timezone.utc))
    extractor=MediaExtractor(tmp_path/'data',command=ffmpeg)
    legacy=tmp_path/'data'/'assets'/'analysis-video'/before/'730-1759-30-1.mp4'
    legacy.parent.mkdir(parents=True,exist_ok=True)
    legacy.write_bytes(b'outdated video clock cache')
    output=extractor.extract_video_interval(asset,730,1759)
    assert output != legacy and legacy.read_bytes() == b'outdated video clock cache'
    video=probe.probe(output)
    assert abs(video.duration_ms-1029) <= 34 and video.fps.numerator/video.fps.denominator == 30
    assert video.has_audio is False
    assert abs(float(video.metadata['streams'][0]['start_time'])) < .001
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert extractor.extract_video_interval(asset,730,1759) == output


@pytest.mark.parametrize('size,expected', [('1080x1920',(540,960)),('1920x1080',(960,540)),('320x180',(320,180))])
def test_real_analysis_proxy_bounds_both_axes_without_cropping_or_changing_time(tmp_path, size, expected):
    ffmpeg=os.environ.get('CONTENT_OS_FFMPEG') or shutil.which('ffmpeg')
    ffprobe=os.environ.get('CONTENT_OS_FFPROBE') or shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:pytest.skip('local ffmpeg/ffprobe unavailable')
    source=tmp_path/'source.mp4'
    subprocess.run([ffmpeg,'-y','-v','error','-f','lavfi','-i',f'testsrc2=duration=1:size={size}:rate=30',
        '-c:v','libx264','-preset','ultrafast',str(source)],check=True,capture_output=True)
    source_hash=hashlib.sha256(source.read_bytes()).hexdigest()
    probe=FFProbeAdapter(ffprobe); raw=probe.probe(source)
    asset=Asset(source_file=str(source),content_hash=source_hash,duration_ms=raw.duration_ms,width=raw.width,height=raw.height,
        fps=raw.fps,authorization_reference='fixture',imported_at=datetime.now(timezone.utc))
    extractor=MediaExtractor(tmp_path/'data',command=ffmpeg)
    old=tmp_path/'data'/'assets'/'analysis-video'/source_hash/'0-1000-30-1-cfr.mp4'
    old.parent.mkdir(parents=True,exist_ok=True);old.write_bytes(b'oversized legacy proxy')
    output=extractor.extract_video_interval(asset,0,1000)
    video=probe.probe(output)
    assert (video.width,video.height)==expected
    assert abs(video.width*raw.height-video.height*raw.width)<=2*raw.width
    assert abs(video.duration_ms-1000)<=34 and video.fps==raw.fps and not video.has_audio
    assert output!=old and old.read_bytes()==b'oversized legacy proxy'
    assert hashlib.sha256(source.read_bytes()).hexdigest()==source_hash


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


def test_bounded_source_audio_interval_is_validated_and_idempotent(monkeypatch, tmp_path: Path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    asset = _asset(source, duration_ms=10_000)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        Path(argv[-1]).write_bytes(b"RIFF interval")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    extractor = MediaExtractor(tmp_path / "data", command=("ffmpeg-custom",), sample_rate=48_000, channels=2)
    first = extractor.extract_audio_interval(asset, 2_500, 8_000)
    second = extractor.extract_audio_interval(asset, 2_500, 8_000)
    assert first.path == second.path and len(calls) == 1
    assert calls[0][calls[0].index("-ss") + 1] == "2.500"
    assert calls[0][calls[0].index("-t") + 1] == "5.500"
    assert calls[0][calls[0].index("-ar") + 1] == "48000"
    assert calls[0][calls[0].index("-ac") + 1] == "2"
    with pytest.raises(ExtractionValidationError):
        extractor.extract_audio_interval(asset, 8_001, 10_001)
    with pytest.raises(ExtractionValidationError):
        extractor.extract_audio_interval(asset, True, 2_000)


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
