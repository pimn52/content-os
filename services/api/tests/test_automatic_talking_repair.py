"""同一普通制作队列的多个人物场景；提供商替身不代表真实口型质量。"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
from threading import Event, Timer
from types import SimpleNamespace
from uuid import UUID
import wave

from fastapi.testclient import TestClient
import pytest

from app.costs import UnknownProviderCostEstimator
from app.db import AssetRepository, AudioAssetRepository, ClipRepository, Database, ProviderCallRepository
from app.domain.models import Asset, AudioAsset, JobType, RationalFps, ScenePlan, TranscriptSegment, VisualIntent
from app.jobs import JobRunner, JobStore
from app.jobs.handlers import TalkingQaJobHandler
from app.jobs.runner import JobExecutionError
from app.main import create_app
from app.media.ffprobe import ProbeDecodeError, ProbeError, ProbeInvalid, ProbeMetadata, ProbeTimeout
from app.video_production import ContentPlanJobHandler, IN_PROGRESS_REASONS, VideoProductionService
from app.worker_cli import _run_product_queue
from test_production_runs import _talking_source_fixture, _voice_dispatch_evidence


@pytest.mark.parametrize("failure", ["technical", "unknown", "other_stop", "frame_short", "decode_failed", "probe_timeout", "probe_invalid", "generic_probe_error", "real_media"])
def test_worker_recovers_diagnosed_talking_qa_after_stop_reason(tmp_path, failure, monkeypatch):
    if failure == "real_media" and not (os.environ.get("CONTENT_OS_FFMPEG") and os.environ.get("CONTENT_OS_FFPROBE")):
        pytest.skip("explicit local FFmpeg and FFprobe paths required")
    path = tmp_path / "two-person-scenes.sqlite"
    with TestClient(create_app(path)) as client:
        pid, old, binding, _ = _talking_source_fixture(client, path)
        with Database(path) as db:
            clip = ClipRepository(db).get(UUID(binding["reference_clip_id"]))
            asset = AssetRepository(db).get(clip.asset_id)
            AssetRepository(db).update(asset.model_copy(update={"has_audio": False}))
        assert client.post(f"/projects/{pid}/production-runs/{old['id']}/talking-source-bind", json=binding).status_code == 200
        _voice_dispatch_evidence(path)
        assert client.put(f"/projects/{pid}/budget", json={"currency": "USD", "max_calls": 12, "allow_unknown_cost": True}).status_code == 200
        response = client.post(f"/projects/{pid}/auto-produce", json={"topic": "连续的本人表达", "idempotency_key": "two-person-input"})
        assert response.status_code == 201, response.text

        with Database(path) as db:
            db.data_root = tmp_path
            service = VideoProductionService(db, tmp_path, voice_provider=SimpleNamespace(model="fixture-model"))
            stop, events, first_qa, unfinished_states = Event(), [], [], []
            copies = ["A fresh creator point.", "Another clear creator judgment."]

            class Planner:
                def plan(self, project, **kwargs):
                    return SimpleNamespace(project_id=project.id, scenes=[ScenePlan(
                        project_id=project.id, scene_id=f"person-{index}", order=index, purpose="本人表达",
                        voice_text=text, duration_target_ms=1200, visual_intent=VisualIntent(subject="创作者本人"),
                        visual_requirement="creator_speaking", visual_requirement_reason="本人说明观点",
                        preferred_sources=["ai_video"], fallback_sources=[],
                    ) for index, text in enumerate(copies)])

            def voice(job):
                events.append("voice")
                output = tmp_path / "voice.wav"
                with wave.open(str(output), "wb") as stream:
                    stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(24000)
                    stream.writeframes(b"\x01\x00" * (24 * 2400))
                AudioAssetRepository(db).create(AudioAsset(
                    source_file=str(output), content_hash=hashlib.sha256(output.read_bytes()).hexdigest(),
                    duration_ms=2400, sample_rate=24000, channels=1,
                    authorization_reference=job.payload.authorization_reference, imported_at=datetime.now(timezone.utc),
                    metadata={"voice_generation": {"job_id": str(job.id), "project_id": pid,
                        "target_text": job.payload.text, "voice_profile_id": str(job.payload.voice_profile_id),
                        "provider": job.payload.expected_provider, "model": job.payload.expected_model, "qa_state": "pending"}},
                ))

            def voice_qa(job):
                events.append("voice_qa")
                repository = AudioAssetRepository(db)
                audio = repository.get(job.payload.narration_audio_id)
                generation = {**audio.metadata["voice_generation"], "qa_state": "verified", "human_review_state": "pending",
                    "qa": {"missing_token_count": 0, "duplicate_token_count": 0, "substitution_token_count": 0, "copy_coverage": 1}}
                repository.update(audio.model_copy(update={"metadata": {"voice_generation": generation},
                    "transcript_source": "fixture independent ASR", "transcript_segments": [
                        TranscriptSegment(start_ms=index * 1200, end_ms=(index + 1) * 1200, text=text)
                        for index, text in enumerate(copies)]}))

            def recheck_first_qa(expected_state):
                assert first_qa
                run = service.runs.reconcile_completed_talking_qa_job(first_qa[0])
                assert run.status == "awaiting_talking_dependencies"
                assert all(reason.startswith(IN_PROGRESS_REASONS) for reason in run.waiting_stop_reasons)
                assert run.talking_qa_dependencies[1].state == expected_state
                unfinished_states.append(expected_state)

            def talking(job):
                index = len([event for event in events if event.startswith("talking-")])
                events.append(f"talking-{index}")
                if index == 1:
                    recheck_first_qa("generation_running")
                    if failure == "generation":
                        stop.set()
                        raise JobExecutionError("fixture_generation_failed", "第二人物生成失败", retryable=False)
                payload, context = job.payload, job.payload.planned_context
                service.runs.require_talking_job_admission(job, provider=context.expected_provider,
                    model=context.expected_model, runtime=context.expected_runtime, machine_id=context.expected_machine_id)
                current_binding = next(b for b in service.runs.get(UUID(pid), context.run_id).talking_source_bindings if b.scene_plan_id == context.scene_plan_id)
                output = tmp_path / f"talking-{index}.mp4"
                if failure == "real_media":
                    subprocess.run([os.environ["CONTENT_OS_FFMPEG"], "-v", "error", "-y",
                        "-f", "lavfi", "-i", f"color=c={['blue', 'red', 'green'][index]}:s=108x192:r=25:d=1.2",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=1.2",
                        "-frames:v", str(29 if index == 1 else 30), "-c:v", "libx264", "-preset", "ultrafast",
                        "-c:a", "aac", str(output)], check=True, capture_output=True)
                else:
                    output.write_bytes(f"fixture talking output {index}".encode())
                AssetRepository(db).create(Asset(source_kind="ai_video", source_file=str(output),
                    content_hash=hashlib.sha256(output.read_bytes()).hexdigest(), duration_ms=1200,
                    width=1080, height=1920, fps=RationalFps(numerator=25, denominator=1), has_audio=True,
                    authorization_reference=payload.authorization_reference, imported_at=datetime.now(timezone.utc),
                    metadata={"talking_generation": {"job_id": str(job.id), "project_id": pid,
                        "planned_context": context.model_dump(mode="json"), "talking_profile_id": str(payload.talking_profile_id),
                        "reference_clip_id": str(payload.reference_clip_id), "narration_audio_id": str(payload.narration_audio_id),
                        "provider": context.expected_provider, "model": context.expected_model,
                        "master_slice_start_ms": current_binding.master_start_ms, "master_slice_end_ms": current_binding.master_end_ms,
                        "reference_window_start_ms": payload.reference_window_start_ms,
                        "reference_window_end_ms": payload.reference_window_end_ms, "expected_video_fps": {"numerator":25, "denominator":1}, "qa_state": "pending"}},
                ))

            class Probe:
                def probe_video_frames(self, path):
                    if path.name == "talking-1.mp4":
                        if failure == "decode_failed":
                            raise ProbeDecodeError("fixture decode failure")
                        if failure == "generic_probe_error":
                            raise ProbeError("fixture unclassified probe failure")
                        if failure == "probe_timeout":
                            raise ProbeTimeout("fixture timeout")
                        if failure == "probe_invalid":
                            raise ProbeInvalid("fixture unavailable evidence")
                        if failure == "frame_short":
                            return {"frame_count": 29, "duration_ms": 1160,
                                "fps": RationalFps(numerator=25, denominator=1)}
                    return {"frame_count": 30, "duration_ms": 1200,
                        "fps": RationalFps(numerator=25, denominator=1)}
                def probe(self, path):
                    return ProbeMetadata(duration_ms=1500 if path.name == "talking-1.mp4" and failure in {"technical", "unknown", "other_stop"} else 1200, width=1080, height=1920,
                        fps=RationalFps(numerator=25, denominator=1), has_audio=True,
                        metadata={"streams": [{"codec_type": "video"}, {"codec_type": "audio"}]})

            from app.media.ffprobe import FFProbeAdapter
            verify = TalkingQaJobHandler(AssetRepository(db), data_root=tmp_path,
                probe=FFProbeAdapter(os.environ["CONTENT_OS_FFPROBE"]) if failure == "real_media" else Probe())
            def talking_qa(job):
                index = len([event for event in events if event.startswith("qa-")])
                events.append(f"qa-{index}")
                if index == 1:
                    recheck_first_qa("qa_running")
                    try:
                        verify(job)
                    except JobExecutionError:
                        if failure == "unknown":
                            repo = AssetRepository(db)
                            output = repo.get(job.payload.output_asset_id)
                            metadata = dict(output.metadata)
                            generation = dict(metadata["talking_generation"])
                            qa = dict(generation["qa"])
                            qa["checks"] = [*qa["checks"], "unknown_defect"]
                            generation["qa"] = qa
                            metadata["talking_generation"] = generation
                            repo.update(output.model_copy(update={"metadata": metadata}))
                        raise
                verify(job)

            if failure == "real_media":
                from app.db import ProjectRepository
                from app.jobs.handlers import RenderVideoJobHandler
                from app.renderer import RemotionRenderer
                from test_remotion_renderer import _renderer_project
                class SyntheticVisualRunner:
                    def run(self, argv, *, cwd, timeout_seconds):
                        if "remotion" not in argv:
                            return subprocess.run(argv, cwd=cwd, timeout=timeout_seconds, capture_output=True)
                        props_path = next(value.split("=", 1)[1] for value in argv if value.startswith("--props="))
                        props = json.loads(Path(props_path).read_text(encoding="utf8"))
                        spec = props["videoSpec"]
                        assert len(props["sceneSources"]) == 2 and props["audioMode"] == "master_narration"
                        assert all((cwd / "public" / source["src"]).is_file() for source in props["sceneSources"].values())
                        frames = max(scene["start_frame"] + scene["duration_frames"] for scene in spec["scenes"])
                        fps = spec["fps"]
                        return subprocess.run([os.environ["CONTENT_OS_FFMPEG"], "-v", "error", "-y",
                            "-f", "lavfi", "-i", f"color=c=blue:s={spec['width']}x{spec['height']}:r={fps['numerator']}/{fps['denominator']}",
                            "-frames:v", str(frames), "-c:v", "libx264", "-preset", "ultrafast", argv[7]],
                            capture_output=True, timeout=timeout_seconds)
                renderer = RemotionRenderer(AssetRepository(db), ClipRepository(db), audios=AudioAssetRepository(db),
                    renderer_dir=_renderer_project(tmp_path / "renderer"), runner=SyntheticVisualRunner())
                actual_render = RenderVideoJobHandler(ProjectRepository(db), renderer, tmp_path / "renders")
            def render(job):
                events.append("render")
                assert [scene.visual_role.value for scene in job.payload.video_spec.scenes] == ["talking", "talking"]
                if failure == "real_media":
                    actual_render(job)
                stop.set()

            def completed(job):
                if job.type is JobType.GENERATE_VOICE:
                    service.runs.reconcile_completed_voice_job(job.id)
                elif job.type is JobType.GENERATE_TALKING:
                    service.runs.reconcile_completed_talking_job(job.id)
                elif job.type is JobType.VERIFY_TALKING:
                    service.runs.reconcile_completed_talking_qa_job(job.id)
                    if not first_qa:
                        first_qa.append(job.id)
                        recheck_first_qa("generation_pending")
                service.completed(job)

            handlers = {JobType.PLAN_CONTENT: ContentPlanJobHandler(service, Planner(), UnknownProviderCostEstimator()),
                JobType.GENERATE_VOICE: voice, JobType.VERIFY_VOICE: voice_qa, JobType.GENERATE_TALKING: talking,
                JobType.VERIFY_TALKING: talking_qa, JobType.RENDER: render}
            runner = JobRunner(JobStore(db), handlers, worker_id="two-person-worker", lease_duration=timedelta(minutes=1),
                max_attempts=1, on_completed=completed)
            run_once = runner.run_once
            def reconcile_after_failure(*args, **kwargs):
                finished = run_once(*args, **kwargs)
                if finished is not None and finished.type is JobType.VERIFY_TALKING and finished.status.value == "failed":
                    current = service.runs.reconcile_completed_talking_qa_job(first_qa[0])
                    assert current.waiting_stop_reasons == ("planned_talking_qa_failed",)
                    if failure == "other_stop":
                        db.connection.execute("UPDATE production_runs SET waiting_stop_reasons=? WHERE id=?",
                            ('["planned_talking_qa_failed", "unrelated_stop"]', str(current.id)))
                return finished
            runner.run_once = reconcile_after_failure
            timeout = Timer(45 if failure == "real_media" else 5, stop.set); timeout.start()
            try:
                _run_product_queue(SimpleNamespace(once=False, poll_seconds=.01, data_root=tmp_path), db, runner, stop)
            finally:
                timeout.cancel()
            result = service.status(UUID(pid))
            from app.db import JobRepository
            from app.talking.repair import TalkingRepairService
            run = service.runs.get(UUID(pid), UUID(result["run"]["id"]))
            records = TalkingRepairService(db, tmp_path).records(UUID(pid), run.id)
            failed_qa = next(job for job in JobRepository(db).list()
                if job.type is JobType.VERIFY_TALKING and job.status.value == "failed")
            failed_output = AssetRepository(db).get(failed_qa.payload.output_asset_id)
            report = failed_output.metadata["talking_generation"]["qa"]
            expected = {"frame_short": "output_video_frames_short", "decode_failed": "output_video_decode_failed",
                "probe_timeout": "output_video_evidence_unavailable", "probe_invalid": "output_video_evidence_unavailable", "generic_probe_error": "output_video_evidence_unavailable", "real_media": "output_video_frames_short"}
            if failure in expected:
                assert expected[failure] in report["checks"]
                assert "output_duration_matches_narration" in report["checks"]
            master_before = AudioAssetRepository(db).get(failed_qa.payload.master_audio_id)
            assert master_before.id == run.voice_audio_id
            assert run.talking_source_bindings[0].talking_qa_job_id == first_qa[0]
            if failure in {"technical", "frame_short", "decode_failed", "real_media"}:
                assert result["status"] == "render_completed_awaiting_review", (result, events, [j.model_dump(mode="json") for j in JobRepository(db).list()])
                assert events == ["voice", "voice_qa", "talking-0", "qa-0", "talking-1", "qa-1", "talking-2", "qa-2", "render"]
                assert len(records) == 1
                assert records[0].reused_master_audio_id == run.voice_audio_id
                assert len(records[0].reused_scene_plan_ids) == 1
                assert run.talking_source_bindings[0].talking_qa_job_id == first_qa[0]
                if failure == "real_media":
                    evidence = json.loads((tmp_path / "render-diagnostics" / str(run.render_job_id) / "output-qa.json").read_text(encoding="utf8"))
                    output = next((tmp_path / "renders").rglob("*.mp4"))
                    assert evidence["state"] == "verified" and evidence["decode"] == "pass"
                    assert evidence["video_frames"] == 72 and evidence["has_audio"] is True
                    assert evidence["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
            else:
                assert "render" not in events and len(records) == 0
                if failure in {"unknown", "probe_timeout", "probe_invalid", "generic_probe_error"}:
                    assert any(reason.startswith("automatic_stop:") for reason in run.waiting_stop_reasons)
                else:
                    assert "unrelated_stop" in run.waiting_stop_reasons
            before = len(JobRepository(db).list())
            for _ in range(2):
                _run_product_queue(SimpleNamespace(once=True, poll_seconds=.01, data_root=tmp_path), db, runner, Event())
            assert len(JobRepository(db).list()) == before
            assert len(TalkingRepairService(db, tmp_path).records(UUID(pid), run.id)) == len(records)
            assert AudioAssetRepository(db).get(master_before.id) == master_before
            assert service.runs.get(UUID(pid), run.id).talking_source_bindings[0].talking_qa_job_id == first_qa[0]
            assert ProviderCallRepository(db).list_for_project(UUID(pid)) == []



