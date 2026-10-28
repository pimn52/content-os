"""普通 Run 技术 QA 自动续跑；不伪造最终人工质量判断。"""
import hashlib
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import pytest

from app.db import AssetRepository, AudioAssetRepository, ClipRepository, Database, JobRepository
from app.domain.models import Asset, JobStatus, JobType, RationalFps
from app.jobs import JobRunner, JobStore
from app.jobs.handlers import TalkingQaJobHandler
from app.main import create_app
from app.media.ffprobe import ProbeMetadata
from app.production_runs import ProductionRunService
from app.talking.admission import talking_visual_blocker
from test_production_runs import _talking_source_fixture


def test_approved_copy_timing_accepts_only_recorded_bounded_asr_substitution(tmp_path):
    from app.domain.models import AudioAsset, TranscriptSegment
    from app.providers.asr import TranscriptionResult, TranscriptionSegment
    from app.talking.admission import _matches_interval_copy
    from app.voice_qa import apply_voice_qa, verify_generated_voice
    source = tmp_path / "fixture.bin"
    source.write_bytes(b"offline fixture")
    audio = AudioAsset(source_file=str(source), content_hash="a" * 64,
        duration_ms=2000, sample_rate=24000, channels=1,
        authorization_reference="fixture", imported_at=datetime.now(timezone.utc),
        transcript_segments=[TranscriptSegment(start_ms=0, end_ms=1000, text="原质量"),
            TranscriptSegment(start_ms=1000, end_ms=2000, text="实际观感")],
        metadata={"voice_generation": {"qa_state": "verified", "target_text": "源质量实际观感",
            "qa": {"missing_token_count": 0, "duplicate_token_count": 0, "substitution_token_count": 1}}})
    transcription = TranscriptionResult("原质量实际观感",
        tuple(TranscriptionSegment(item.start_ms, item.end_ms, item.text) for item in audio.transcript_segments))
    report = verify_generated_voice(audio, audio.metadata["voice_generation"]["target_text"], transcription)
    assert report.verified
    audio = apply_voice_qa(audio, report, transcription, provider="fixture", model="fixture")
    assert _matches_interval_copy("源质量", audio.transcript_segments, 0, 1000, audio=audio)
    assert not _matches_interval_copy("新文案", audio.transcript_segments, 0, 1000, audio=audio)
    assert not _matches_interval_copy("源质量", audio.transcript_segments, 0, 1000)
    audio.metadata["voice_generation"]["qa"]["substitution_token_count"] = 0
    assert not _matches_interval_copy("源质量", audio.transcript_segments, 0, 1000, audio=audio)


@pytest.mark.parametrize("changed_bytes,assembly_fix", [(False,False), (True,False), (False,True)])
def test_same_run_qa_enqueues_review_video_without_child_human_gate(tmp_path, changed_bytes, assembly_fix):
    path = tmp_path / "draft.sqlite"
    with TestClient(create_app(path)) as client:
        pid, waiting, binding, _ = _talking_source_fixture(client, path)
        binding.pop("authorization_reference", None)
        base = f"/projects/{pid}/production-runs/{waiting['id']}"
        bound = client.post(base + "/talking-source-bind", json=binding)
        assert bound.status_code == 200, bound.text
        dispatched = client.post(base + "/talking-dispatch", json={"scene_plan_id": binding["scene_plan_id"]})
        assert dispatched.status_code == 200, dispatched.text
        job_id = UUID(dispatched.json()["talking_source_bindings"][0]["talking_job_id"])
    output_path = tmp_path / "output.mp4"
    output_path.write_bytes(b"synthetic output; not a real inference")
    with Database(path) as db:
        jobs = JobRepository(db)
        job = jobs.get(job_id)
        jobs.update(job.model_copy(update={"status": JobStatus.COMPLETED}))
        payload = job.payload
        master = AudioAssetRepository(db).get(payload.narration_audio_id)
        metadata = dict(master.metadata)
        generation = dict(metadata["voice_generation"])
        generation["qa"] = {"copy_coverage": 1.0, "missing_token_count": 0, "duplicate_token_count": 0}
        metadata["voice_generation"] = generation
        AudioAssetRepository(db).update(master.model_copy(update={"metadata": metadata}))
        output = AssetRepository(db).create(Asset(
            source_kind="ai_video", source_file=output_path.name,
            content_hash=hashlib.sha256(output_path.read_bytes()).hexdigest(),
            width=1080, height=1920, duration_ms=3000, has_audio=True,
            fps=RationalFps(numerator=25, denominator=1), imported_at=datetime.now(timezone.utc),
            authorization_reference=payload.authorization_reference,
            metadata={"talking_generation": {
                "job_id": str(job.id), "qa_state": "pending", "human_review_state": "pending",
                "expected_video_fps": {"numerator":25, "denominator":1},
                "planned_context": payload.planned_context.model_dump(mode="json"),
                "talking_profile_id": str(payload.talking_profile_id),
                "reference_clip_id": str(payload.reference_clip_id),
                "narration_audio_id": str(payload.narration_audio_id),
                "provider": payload.planned_context.expected_provider,
                "model": payload.planned_context.expected_model,
                "master_slice_start_ms": 0, "master_slice_end_ms": 3000,
                "reference_window_start_ms": 0, "reference_window_end_ms": 5000,
            }},
        ))
        service = ProductionRunService(db, tmp_path)
        run_id = UUID(waiting["id"])
        service.reconcile_completed_talking_job(job_id)

        class Probe:
            def probe_video_frames(self, path):
                # Synthetic frame evidence for this local fixture.
                return {"frame_count": 75, "duration_ms": 3000,
                    "fps": RationalFps(numerator=25, denominator=1)}
            def probe(self, path):
                return ProbeMetadata(duration_ms=3000, width=1080, height=1920,
                    fps=RationalFps(numerator=25, denominator=1), has_audio=True,
                    metadata={"streams": [{"codec_type": "video"}, {"codec_type": "audio"}]})

        completed = JobRunner(JobStore(db), {JobType.VERIFY_TALKING:
            TalkingQaJobHandler(AssetRepository(db), data_root=tmp_path, probe=Probe())},
            worker_id="qa", lease_duration=timedelta(minutes=1), max_attempts=1,
        ).run_once(allowed_types={JobType.VERIFY_TALKING})
        assert completed.status is JobStatus.COMPLETED, completed.model_dump_json()
        if changed_bytes:
            output_path.write_bytes(b"changed after QA")
        run = service.reconcile_completed_talking_qa_job(completed.id)
        if changed_bytes:
            assert run.render_job_id is None
            assert run.waiting_stop_reasons
            return
        assert run.status == "render_pending", run.waiting_stop_reasons
        assert service.reconcile_completed_talking_qa_jobs_once() == ()
        asset = AssetRepository(db).get(output.id)
        assert asset.metadata["talking_generation"]["human_review_state"] == "pending"
        clip = ClipRepository(db).list_by_asset(output.id)[0]
        args = dict(project_id=UUID(pid), copy="A fresh creator point.", master_audio_id=payload.narration_audio_id)
        assert talking_visual_blocker(asset, clip, AssetRepository(db), **args) is not None
        assert talking_visual_blocker(asset, clip, AssetRepository(db), production_run_id=uuid4(), **args) is not None
        assert talking_visual_blocker(asset, clip, AssetRepository(db), production_run_id=run_id, **args) is None
        assert len([job for job in jobs.list() if job.type is JobType.RENDER]) == 1
        failed = jobs.get(run.render_job_id)
        expected_spec=failed.payload.video_spec
        if assembly_fix:
            saved_scenes=list(expected_spec.scenes)
            saved_scenes[0]=saved_scenes[0].model_copy(update={"caption":"Previous local assembly defect"})
            failed=failed.model_copy(update={"payload":failed.payload.model_copy(update={
                "video_spec":expected_spec.model_copy(update={"scenes":saved_scenes})})})
        jobs.update(failed.model_copy(update={"status": JobStatus.FAILED,
            "error_code": "render_invalid", "error_message": "retained failure"}))
        # Completion callbacks never retry a failed render. Explicit resume does,
        # preserving the exact approved dependencies and the failed Job.
        assert service.reconcile_completed_talking_qa_job(completed.id).render_job_id == failed.id
        resumed = service.resume(UUID(pid), run_id)
        assert resumed.render_job_id != failed.id and resumed.status == "render_pending"
        replacement = jobs.get(resumed.render_job_id)
        assert replacement.payload.video_spec == expected_spec
        if assembly_fix:assert replacement.payload.video_spec != failed.payload.video_spec
        assert replacement.payload.render_id != failed.payload.render_id
        assert jobs.get(failed.id).status is JobStatus.FAILED
        assert service.resume(UUID(pid), run_id).render_job_id == replacement.id
        assert len([job for job in jobs.list() if job.type is JobType.RENDER]) == 2
        assert len([job for job in jobs.list() if job.type is JobType.GENERATE_TALKING]) == 1
