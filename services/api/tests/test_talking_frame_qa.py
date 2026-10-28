"""独立画轨检查：本地契约夹具与合成视频，不代表口型或自然度。"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from app.domain.models import Asset, AudioAsset, RationalFps, SourceKind
from app.media.ffprobe import FFProbeAdapter, ProbeError, ProbeDecodeError, ProbeInvalid, ProbeMetadata, ProbeTimeout
from app.runtime import resolve_local_executable
from app.talking_qa import apply_talking_qa, verify_talking_output


class FrameProbeFixture:
    """Only a test double: frame facts derive from its synthetic probe value."""
    def probe_video_frames(self, path):
        value = self.probe(path)
        return {"frame_count": (value.duration_ms * value.fps.numerator + 1000 * value.fps.denominator - 1)
                // (1000 * value.fps.denominator), "duration_ms": value.duration_ms, "fps": value.fps}


def qa_assets(tmp_path, duration_ms=1000):
    path = tmp_path / "output.mp4"
    path.write_bytes(b"synthetic frame QA fixture")
    now = datetime.now(timezone.utc)
    asset = Asset(source_kind=SourceKind.AI_VIDEO, source_file=str(path),
        content_hash=hashlib.sha256(path.read_bytes()).hexdigest(), duration_ms=duration_ms,
        width=64, height=112, fps=RationalFps(numerator=25, denominator=1), has_audio=True,
        authorization_reference="fixture consent", imported_at=now,
        metadata={"talking_generation": {"provider": "fixture", "qa_state": "pending",
            "expected_video_fps": {"numerator": 25, "denominator": 1}}})
    audio = AudioAsset(source_file=str(tmp_path / "master.wav"), content_hash="a" * 64,
        duration_ms=duration_ms, sample_rate=24000, channels=1, imported_at=now,
        authorization_reference="fixture consent", metadata={"voice_generation": {"qa_state": "verified",
            "qa": {"copy_coverage": 1.0, "missing_token_count": 0, "duplicate_token_count": 0}}})
    return asset, audio


class ContainerProbe:
    def probe(self, path):
        return ProbeMetadata(duration_ms=1000, width=64, height=112,
            fps=RationalFps(numerator=25, denominator=1), has_audio=True,
            metadata={"streams": [{"codec_type": "video"}, {"codec_type": "audio"}]})


@pytest.mark.parametrize("frames,duration,check", [(24, 960, "output_video_frames_short"),
    (25, 900, "output_video_frames_short"), (28, 1120, "output_video_duration_drift"),
    (25, 1000, "output_video_frames_verified")])
def test_independent_frame_coverage_cannot_be_hidden_by_container(tmp_path, frames, duration, check):
    asset, audio = qa_assets(tmp_path)
    class Probe(ContainerProbe):
        def probe_video_frames(self, path):
            return {"frame_count": frames, "duration_ms": duration, "fps": asset.fps}
    report = verify_talking_output(asset, audio, Probe(), evidence_reference="fixture:independent-frame-check")
    assert check in report.checks
    assert report.automated_verified is (check == "output_video_frames_verified")
    facts = apply_talking_qa(asset, report).metadata["talking_generation"]["qa"]
    assert facts["video_frames"] == frames and facts["expected_video_frames"] == 25
    assert facts["video_duration_ms"] == duration
    assert "human_review_state" not in apply_talking_qa(asset, report).metadata["talking_generation"]


@pytest.mark.parametrize("failure,check", [(ProbeDecodeError("structured invalid data"), "output_video_decode_failed"),
    (ProbeError("unknown tool failure"), "output_video_evidence_unavailable"),
    (ProbeTimeout("local tool timed out"), "output_video_evidence_unavailable"),
    (ProbeInvalid("unknown metadata"), "output_video_evidence_unavailable")])
def test_decode_failure_and_unknown_evidence_are_distinct(tmp_path, failure, check):
    asset, audio = qa_assets(tmp_path)
    class Probe(ContainerProbe):
        def probe_video_frames(self, path): raise failure
    report = verify_talking_output(asset, audio, Probe(), evidence_reference="fixture:decoder")
    assert not report.automated_verified and check in report.checks
    assert "narration_copy_qa_verified" in report.checks


def test_missing_decoder_does_not_promote_container_check_to_frame_pass(tmp_path):
    asset, audio = qa_assets(tmp_path)
    report = verify_talking_output(asset, audio, ContainerProbe(), evidence_reference="fixture:missing-decoder")
    assert not report.automated_verified and "output_video_evidence_unavailable" in report.checks


def test_missing_frame_cannot_lower_its_own_expected_rate(tmp_path):
    asset, audio = qa_assets(tmp_path, duration_ms=1400)
    class Probe(ContainerProbe):
        def probe(self, path):
            return ProbeMetadata(duration_ms=1400, width=64, height=112, fps=RationalFps(numerator=170, denominator=7),
                has_audio=True, metadata={"streams": [{"codec_type": "video"}, {"codec_type": "audio"}]})
        def probe_video_frames(self, path):
            return {"frame_count": 34, "duration_ms": 1400, "fps": RationalFps(numerator=170, denominator=7)}
    report = verify_talking_output(asset, audio, Probe(), evidence_reference="fixture:independent-fps")
    assert not report.automated_verified and "output_video_frames_short" in report.checks
    assert report.expected_video_frames == 35


def test_unknown_provider_frame_rate_cannot_claim_coverage(tmp_path):
    asset, audio = qa_assets(tmp_path)
    asset = asset.model_copy(update={"metadata": {"talking_generation": {"qa_state": "pending"}}})
    class Probe(FrameProbeFixture, ContainerProbe): pass
    report = verify_talking_output(asset, audio, Probe(), evidence_reference="fixture:unknown-provider-fps")
    assert not report.automated_verified and "output_video_evidence_unavailable" in report.checks
    assert report.expected_video_frames is None


def test_decoder_errors_reject_even_with_success_exit_and_complete_header(monkeypatch):
    def run(argv, **kwargs):
        assert "-count_frames" in argv and "-err_detect" in argv
        return subprocess.CompletedProcess(argv, 0, json.dumps({"streams": [{"nb_read_frames": "25",
            "duration": "1.0", "avg_frame_rate": "25/1"}]}), "decoder: corrupt frame\n")
    monkeypatch.setattr("app.media.ffprobe.subprocess.run", run)
    with pytest.raises(ProbeError): FFProbeAdapter().probe_video_frames("fixture.mp4")


@pytest.mark.parametrize("code,exception", [(-1094995529, ProbeDecodeError), (-13, ProbeError)])
def test_only_structured_invalid_data_is_a_media_decode_failure(monkeypatch, code, exception):
    monkeypatch.setattr("app.media.ffprobe.subprocess.run", lambda argv, **kwargs:
        subprocess.CompletedProcess(argv, 1, json.dumps({"error": {"code": code}}), "local tool failure"))
    with pytest.raises(exception) as caught:
        FFProbeAdapter().probe_video_frames("fixture.mp4")
    assert (isinstance(caught.value, ProbeDecodeError)) is (code == -1094995529)


def test_worker_does_not_persist_qa_for_media_changed_during_decode(tmp_path, monkeypatch):
    from datetime import timedelta
    from uuid import UUID
    from fastapi.testclient import TestClient
    from app.db import AssetRepository, Database, ProviderCallRepository
    from app.domain.models import JobStatus, JobType
    from app.jobs import JobRunner, JobStore
    from app.jobs.handlers import TalkingQaJobHandler
    from app.main import create_app
    from test_production_runs import _completed_planned_talking_output

    path = tmp_path / "changed-during-QA.sqlite"
    with TestClient(create_app(path)) as client:
        project, run, binding, source = _completed_planned_talking_output(client, path, monkeypatch)
        advanced = client.post(f"/projects/{project}/production-runs/{run['id']}/talking-qa-advance",
            json={"scene_plan_id": binding["scene_plan_id"]})
        assert advanced.status_code == 200
        qa_id = UUID(advanced.json()["talking_source_bindings"][0]["talking_qa_job_id"])
        class Probe(ContainerProbe):
            def probe(self, path):
                return ProbeMetadata(duration_ms=3000, width=1080, height=1920,
                    fps=RationalFps(numerator=25, denominator=1), has_audio=True,
                    metadata={"streams": [{"codec_type": "video"}, {"codec_type": "audio"}]})
            def probe_video_frames(self, path):
                source.write_bytes(b"fixture replaced during decoded-frame measurement")
                return {"frame_count": 75, "duration_ms": 3000, "fps": RationalFps(numerator=25, denominator=1)}
        with Database(path) as db:
            handler = TalkingQaJobHandler(AssetRepository(db), data_root=tmp_path, probe=Probe())
            finished = JobRunner(JobStore(db), {JobType.VERIFY_TALKING: handler}, worker_id="frame-QA",
                lease_duration=timedelta(minutes=1), max_attempts=1).run_once(job_id=qa_id)
            assert finished.status is JobStatus.FAILED and finished.error_code == "talking_qa_output_changed"
            output = AssetRepository(db).get(finished.payload.output_asset_id)
            assert output.metadata["talking_generation"]["qa_state"] == "pending"
            assert "qa" not in output.metadata["talking_generation"]
            assert ProviderCallRepository(db).list_for_project(UUID(project)) == []


@pytest.mark.parametrize("video_ms", [1000, 2000])
def test_real_video_track_decode_independent_of_longer_audio(tmp_path, video_ms):
    if not os.environ.get("CONTENT_OS_FFMPEG"):
        pytest.skip("use the configured worker FFmpeg")
    asset, audio = qa_assets(tmp_path, duration_ms=2000)
    subprocess.run([resolve_local_executable("ffmpeg"), "-v", "error", "-y", "-f", "lavfi", "-i",
        f"testsrc2=size=64x112:rate=25:duration={video_ms / 1000}", "-f", "lavfi", "-i",
        "sine=frequency=440:sample_rate=24000:duration=2", "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", asset.source_file],
        check=True, capture_output=True, timeout=30)
    probe = FFProbeAdapter(command=resolve_local_executable("ffprobe"))
    report = verify_talking_output(asset, audio, probe, evidence_reference="synthetic:real-FFmpeg")
    assert "output_duration_matches_narration" in report.checks
    assert report.video_frames == video_ms // 40 and report.expected_video_frames == 50
    assert report.automated_verified is (video_ms == 2000)
