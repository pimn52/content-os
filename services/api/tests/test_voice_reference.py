"""Actual installed FFmpeg on synthetic media; no vendor/model imports."""
import hashlib
from pathlib import Path
import struct
import subprocess
from types import SimpleNamespace
from uuid import uuid4
import wave

import pytest

from app.domain.models import VoiceReferenceWindowSelection
from app.media.voice_reference import prepare_voice_reference, reference_extraction_command
from app.providers.omnivoice_entry_v2 import LocalCloneInputV2
from app.providers.prepared import ExecutionPreparationError
from app.voice_performance import VoiceReferenceWindow


@pytest.fixture
def reference_case(tmp_path):
    source = tmp_path / "source.wav"
    with wave.open(str(source), "wb") as audio:
        audio.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
        audio.writeframes(struct.pack("<hh", 1200, 800) * 96000)
    staging = tmp_path / "staging"; staging.mkdir()
    window = VoiceReferenceWindow(uuid4(), uuid4(), 200, 1700, "synthetic reference", str(source), 1.0)
    selection = VoiceReferenceWindowSelection(clip_id=window.clip_id, asset_id=window.asset_id, start_ms=200,
        end_ms=1700, source_content_hash=hashlib.sha256(source.read_bytes()).hexdigest(),
        transcript_sha256=hashlib.sha256(window.transcript.encode()).hexdigest())
    inputs = LocalCloneInputV2(text="copy", reference_audio=str(source), reference_text=window.transcript,
        language="Chinese", output_path=str(tmp_path / "output.wav"), reference_sha256=selection.source_content_hash,
        reference_start_frame=0, reference_end_frame=36000, reference_max_frames=72000, reference_max_bytes=200000)
    ffmpeg = Path(__file__).parents[3] / "content-os-data/latentsync-runtime310/Library/bin/ffmpeg.exe"
    assert ffmpeg.is_file(), "declared installed FFmpeg prerequisite missing"
    return SimpleNamespace(root=tmp_path, staging=staging, window=window, selection=selection, inputs=inputs, tool=ffmpeg)


def prepare(c):
    return prepare_voice_reference(data_root=c.root, staging_parent=c.staging, ffmpeg=c.tool,
        window=c.window, selection=c.selection, inputs=c.inputs)


def test_real_ffmpeg_stereo_resample_exact_window_and_stable_identity(reference_case):
    c = reference_case
    with prepare(c) as owned:
        inputs = owned.inputs_snapshot(); path = Path(inputs.reference_audio)
        provenance = owned.verify_for(c.window, c.selection, inputs)
        with wave.open(str(path), "rb") as audio:
            assert (audio.getnchannels(), audio.getframerate(), audio.getnframes()) == (1, 24000, 36000)
        assert path.stat().st_size == 44 + 36000 * 2
        assert provenance["source_sha256"] != provenance["derived_sha256"]
    assert not path.exists() and list(c.staging.iterdir()) == []
    with prepare(c) as replay:
        assert replay.provenance_snapshot() == provenance
        assert replay.inputs_snapshot().reference_audio != inputs.reference_audio


def test_analysis_wav_reference_uses_its_own_sample_clock(reference_case):
    from dataclasses import replace
    c = reference_case
    source = c.root / "analysis-16k.wav"
    with wave.open(str(source), "wb") as audio:
        audio.setparams((1, 2, 16_000, 0, "NONE", "not compressed"))
        audio.writeframes(struct.pack("<h", 1200) * (16_000 * 3))
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    parent_asset_hash = "f" * 64
    c.window = replace(c.window, source_file=str(source), source_timebase="analysis_wav",
        source_content_hash=source_hash, asset_content_hash=parent_asset_hash)
    c.selection = c.selection.model_copy(update=dict(source_content_hash=source_hash,
        source_timebase="analysis_wav", asset_content_hash=parent_asset_hash))
    c.inputs = c.inputs.model_copy(update=dict(reference_audio=str(source), reference_sha256=source_hash,
        reference_end_frame=36_000))

    command = reference_extraction_command(c.tool, source, "target.wav", 200, 1700,
        canonical=True, source_timebase="analysis_wav")
    assert "-ss" not in command
    assert "atrim=start_sample=3200:end_sample=27200" in command[command.index("-af") + 1]
    with prepare(c) as owned:
        output = Path(owned.inputs_snapshot().reference_audio)
        with wave.open(str(output), "rb") as audio:
            assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getnframes()) == (1, 2, 24_000, 36_000)
        provenance = owned.verify_for(c.window, c.selection, owned.inputs_snapshot())
        assert provenance["timing"] == "analysis_wav_sample_trim"
        assert provenance["source_sha256"] == source_hash
        assert provenance["asset_sha256"] == parent_asset_hash


def test_analysis_wav_selection_cannot_be_reinterpreted_as_asset_pts(reference_case, monkeypatch):
    from dataclasses import replace
    c = reference_case
    c.window = replace(c.window, source_timebase="analysis_wav", asset_content_hash="f" * 64)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("must reject mismatched timebase"))
    with pytest.raises(ExecutionPreparationError, match="voice_reference_binding_invalid"):
        prepare(c)


def test_real_ffmpeg_video_selected_reference(reference_case):
    c = reference_case
    video = c.root / "synthetic.mp4"
    subprocess.run([str(c.tool), "-nostdin", "-v", "error", "-n", "-f", "lavfi", "-i",
        "color=c=blue:s=32x48:r=10:d=2", "-i", c.window.source_file,
        "-c:v", "mpeg4", "-c:a", "aac", "-shortest", str(video)], check=True, capture_output=True, timeout=30)
    from dataclasses import replace
    c.window = replace(c.window, source_file=str(video))
    c.selection = c.selection.model_copy(update=dict(source_content_hash=hashlib.sha256(video.read_bytes()).hexdigest()))
    with prepare(c) as owned:
        assert owned.provenance_snapshot()["frames"] == 36000
        assert owned.verify_for(c.window, c.selection, owned.inputs_snapshot())["source_sha256"] == c.selection.source_content_hash


def test_source_timestamp_offset_is_preserved_before_sample_selection(reference_case):
    from dataclasses import replace
    c = reference_case
    source = c.root / "offset.nut"
    subprocess.run([str(c.tool), "-nostdin", "-v", "error", "-n", "-f", "lavfi", "-i",
        "color=c=blue:s=32x48:r=10:d=2", "-i", c.window.source_file,
        "-af", "asetpts=PTS+0.064/TB", "-c:v", "mpeg4", "-c:a", "pcm_s16le", "-f", "nut", str(source)],
        check=True, capture_output=True, timeout=30)
    c.window = replace(c.window, source_file=str(source), start_ms=0, end_ms=200)
    c.selection = c.selection.model_copy(update=dict(start_ms=0, end_ms=200,
        source_content_hash=hashlib.sha256(source.read_bytes()).hexdigest()))
    with prepare(c) as owned:
        with wave.open(owned.inputs_snapshot().reference_audio, "rb") as audio:
            assert audio.getnframes() == 4800
            samples = struct.unpack("<4800h", audio.readframes(4800))
        assert all(value == 0 for value in samples[:1400])
        assert all(value != 0 for value in samples[-1000:])


def test_actual_short_source_is_not_padded_to_pass(reference_case):
    from dataclasses import replace
    c = reference_case
    c.window = replace(c.window, end_ms=3000)
    c.selection = c.selection.model_copy(update=dict(end_ms=3000))
    with pytest.raises(ExecutionPreparationError, match="voice_reference_output_invalid"):
        prepare(c)
    assert list(c.staging.iterdir()) == []


@pytest.mark.parametrize("change", ["source", "derived", "tool", "interval"])
def test_current_reference_drift_rejected(reference_case, monkeypatch, change):
    c = reference_case
    # Tool fixture avoids altering the actual installed executable.
    fake = c.root / "ffmpeg.exe"; fake.write_bytes(b"synthetic tool")
    def run(command, **kwargs):
        target = Path(command[-1])
        with wave.open(str(target), "wb") as audio:
            audio.setparams((1,2,24000,0,"NONE","not compressed"))
            audio.writeframes(struct.pack("<h",1000)*36000)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(subprocess, "run", run); c.tool = fake
    with prepare(c) as owned:
        inputs = owned.inputs_snapshot()
        if change == "source": Path(c.window.source_file).write_bytes(b"changed")
        if change == "derived": Path(inputs.reference_audio).write_bytes(b"changed")
        if change == "tool": fake.write_bytes(b"changed")
        if change == "interval": inputs = inputs.model_copy(update=dict(reference_start_frame=1))
        with pytest.raises(ExecutionPreparationError): owned.verify_for(c.window,c.selection,inputs)
    assert list(c.staging.iterdir()) == []


@pytest.mark.parametrize("failure", ["timeout", "status", "frames", "silent", "source_drift"])
def test_failed_conversion_cleans_private_attempt(reference_case, monkeypatch, failure):
    c = reference_case
    def run(command, **kwargs):
        assert kwargs["shell"] is False and "HF_TOKEN" not in kwargs["env"]
        assert "-protocol_whitelist" in command and command[command.index("-protocol_whitelist")+1] == "file"
        if failure == "timeout": raise subprocess.TimeoutExpired(command, 120)
        if failure == "status": return SimpleNamespace(returncode=1)
        with wave.open(command[-1], "wb") as audio:
            audio.setparams((1,2,24000,0,"NONE","not compressed"))
            audio.writeframes(struct.pack("<h",0 if failure == "silent" else 1000) * (100 if failure == "frames" else 36000))
        if failure == "source_drift": Path(c.window.source_file).write_bytes(b"changed")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ExecutionPreparationError): prepare(c)
    assert list(c.staging.iterdir()) == []


@pytest.mark.parametrize("change", ["hash", "boundary", "limit", "timeout"])
def test_invalid_selection_stops_before_ffmpeg(reference_case, monkeypatch, change):
    c = reference_case
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("must reject before extraction"))
    if change == "hash": c.selection = c.selection.model_copy(update=dict(source_content_hash="0"*64))
    if change == "boundary": c.staging = c.root.parent
    if change == "limit": c.inputs = c.inputs.model_copy(update=dict(reference_max_bytes=100))
    kwargs = dict(data_root=c.root, staging_parent=c.staging, ffmpeg=c.tool,window=c.window,selection=c.selection,inputs=c.inputs)
    if change == "timeout": kwargs["timeout_seconds"] = True
    with pytest.raises(ExecutionPreparationError): prepare_voice_reference(**kwargs)


def test_legacy_command_is_unchanged():
    assert reference_extraction_command("ffmpeg","source","target",100,1200) == ["ffmpeg","-y","-ss","0.100","-i","source",
        "-t","1.100","-vn","-ac","1","-ar","24000","-c:a","pcm_s16le","target"]
