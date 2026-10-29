"""同一普通制作队列的多个人物场景；提供商替身不代表真实口型质量。"""
from datetime import datetime, timedelta, timezone
import hashlib
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
from app.media.ffprobe import ProbeMetadata
from app.video_production import ContentPlanJobHandler, IN_PROGRESS_REASONS, VideoProductionService
from app.worker_cli import _run_product_queue
from test_production_runs import _talking_source_fixture, _voice_dispatch_evidence


@pytest.mark.parametrize("failure", [None, "generation", "qa", "unverified"])
def test_one_input_runs_two_person_scenes_without_stopping_after_first_qa(tmp_path, failure):
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
                output = tmp_path / f"talking-{index}.mp4"
                output.write_bytes(f"fixture talking output {index}".encode())
                AssetRepository(db).create(Asset(source_kind="ai_video", source_file=str(output),
                    content_hash=hashlib.sha256(output.read_bytes()).hexdigest(), duration_ms=1200,
                    width=1080, height=1920, fps=RationalFps(numerator=25, denominator=1), has_audio=True,
                    authorization_reference=payload.authorization_reference, imported_at=datetime.now(timezone.utc),
                    metadata={"talking_generation": {"job_id": str(job.id), "project_id": pid,
                        "planned_context": context.model_dump(mode="json"), "talking_profile_id": str(payload.talking_profile_id),
                        "reference_clip_id": str(payload.reference_clip_id), "narration_audio_id": str(payload.narration_audio_id),
                        "provider": context.expected_provider, "model": context.expected_model,
                        "master_slice_start_ms": index * 1200, "master_slice_end_ms": (index + 1) * 1200,
                        "reference_window_start_ms": payload.reference_window_start_ms,
                        "reference_window_end_ms": payload.reference_window_end_ms, "expected_video_fps": {"numerator":25, "denominator":1}, "qa_state": "pending"}},
                ))

            class Probe:
                def probe_video_frames(self, path):
                    # Synthetic frame evidence for this local fixture.
                    return {"frame_count": 30, "duration_ms": 1200,
                        "fps": RationalFps(numerator=25, denominator=1)}
                def probe(self, path):
                    return ProbeMetadata(duration_ms=1200, width=1080, height=1920,
                        fps=RationalFps(numerator=25, denominator=1), has_audio=True,
                        metadata={"streams": [{"codec_type": "video"}, {"codec_type": "audio"}]})

            verify = TalkingQaJobHandler(AssetRepository(db), data_root=tmp_path, probe=Probe())
            def talking_qa(job):
                index = len([event for event in events if event.startswith("qa-")])
                events.append(f"qa-{index}")
                if index == 1:
                    recheck_first_qa("qa_running")
                    if failure == "qa":
                        stop.set()
                        raise JobExecutionError("fixture_qa_failed", "第二人物质检失败", retryable=False)
                    if failure == "unverified":
                        stop.set()
                        return  # 已完成的 QA Job 若缺合格事实，仍须硬停。
                verify(job)

            def render(job):
                events.append("render")
                assert [scene.visual_role.value for scene in job.payload.video_spec.scenes] == ["talking", "talking"]
                stop.set()

            def completed(job):
                service.completed(job)
                if job.type is JobType.VERIFY_TALKING:
                    if not first_qa:
                        first_qa.append(job.id)
                        recheck_first_qa("generation_pending")

            handlers = {JobType.PLAN_CONTENT: ContentPlanJobHandler(service, Planner(), UnknownProviderCostEstimator()),
                JobType.GENERATE_VOICE: voice, JobType.VERIFY_VOICE: voice_qa, JobType.GENERATE_TALKING: talking,
                JobType.VERIFY_TALKING: talking_qa, JobType.RENDER: render}
            runner = JobRunner(JobStore(db), handlers, worker_id="two-person-worker", lease_duration=timedelta(minutes=1),
                max_attempts=1, on_completed=completed)
            runner.production_service=service
            timeout = Timer(5, stop.set); timeout.start()
            try:
                _run_product_queue(SimpleNamespace(once=False, poll_seconds=.01, data_root=tmp_path), db, runner, stop)
            finally:
                timeout.cancel()
            result = service.status(UUID(pid))
            if failure:
                assert "render" not in events
                run = service.runs.reconcile_completed_talking_qa_job(first_qa[0])
                assert run.waiting_stop_reasons == (("planned_talking_generation_failed",) if failure == "generation" else ("planned_talking_qa_failed",))
                assert service.status(UUID(pid))["status"] == "failed"
                assert len([event for event in events if event == "talking-1"]) == 1
            else:
                assert result["status"] == "render_completed_awaiting_review", result
                assert events == ["voice", "voice_qa", "talking-0", "qa-0", "talking-1", "qa-1", "render"]
                assert unfinished_states == ["generation_pending", "generation_running", "qa_running"]
            assert ProviderCallRepository(db).list_for_project(UUID(pid)) == []
