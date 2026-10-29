"""普通 Run 的连续交付区间：真实停顿形状，模型和媒体探测使用替身。"""
from __future__ import annotations

import hashlib
import json
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from test_talking_frame_qa import FrameProbeFixture
from fastapi.testclient import TestClient

from app.db import (
    AssetRepository, AudioAssetRepository, ClipRepository, Database,
    IPProfileRepository, JobRepository, ProjectRepository,
    ProviderCallRepository, ProviderMachineCapabilityProfileRepository,
    TalkingProfileRepository, VoiceProfileRepository,
)
from app.domain.models import (
    Asset, AudioAsset, Clip, ConsentRecord, JobStatus, JobType,
    ProviderMachineCapabilityProfile, RationalFps, ScenePlan, SourceKind,
    TalkingProfile, TalkingReferenceAssessment, TranscriptSegment,
    VisualIntent, VoiceProfile,
)
from app.jobs import JobRunner, JobStore
from app.jobs.handlers import TalkingGenerationJobHandler, TalkingQaJobHandler
from app.main import create_app
from app.media.ffprobe import ProbeMetadata
from app.production_runs import ProductionRunService
from app.video_production import VideoProductionService
from app.providers.talking import TalkingSynthesisResult
from app.talking.slices import extract_talking_audio_slice


def test_generated_master_rejects_partial_or_reordered_scene_copy_before_binding(tmp_path):
    from app.assembly.video_spec import NarrationTimelineError, _master_narration_intervals
    from app.production_runs import _unique_scene_speech_span
    path = tmp_path / "incomplete-copy.sqlite"
    with TestClient(create_app(path)) as client:
        _, _, scenes, master, _, _, _ = _delivery_fixture(client, path, 140)
        variants = [
            [scenes[0].model_copy(update={"voice_text": "画质"}), *scenes[1:]],
            [scenes[1], scenes[0], scenes[2]],
        ]
        for changed in variants:
            assert _unique_scene_speech_span(master, changed, changed[0].id) is None
            with pytest.raises(NarrationTimelineError):
                _master_narration_intervals(master, changed)


def _delivery_fixture(client: TestClient, path: Path, gap_ms: int):
    """The middle scene reproduces 11540–12940 vs ASR 11680–12940."""
    project_id = UUID(client.post("/projects", json={
        "title": "连续人物交付", "topic": "码率与画质",
    }).json()["id"])
    texts = ["画质需要结合素材判断。", "码率并非越高越清晰。", "先保留源画面的细节。"]
    scenes = [ScenePlan(
        project_id=project_id, scene_id=f"creator-{index}", order=index,
        purpose="explanation", voice_text=text,
        duration_target_ms=end - start,
        visual_requirement="creator_speaking",
        visual_requirement_reason="本人说明关键判断",
        visual_intent=VisualIntent(subject="creator", action="speaking"),
        preferred_sources=[SourceKind.AI_VIDEO], fallback_sources=[],
    ) for index, (text, start, end) in enumerate(zip(
        texts, [0, 11540, 12940], [11540, 12940, 15140],
    ))]
    saved = client.put(f"/projects/{project_id}/draft", json={
        "script": "".join(texts), "topic": "码率与画质",
        "scenes": [scene.model_dump(mode="json") for scene in scenes],
    })
    assert saved.status_code == 200, saved.text
    now = datetime.now(timezone.utc)
    consent = ConsentRecord(subject_name="Creator", basis="self", confirmed=True, confirmed_at=now)
    master_path = path.parent / "continuous-master.wav"
    with wave.open(str(master_path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24000)
        # Distinct PCM values make extraction boundaries observable to the test.
        stream.writeframes(b"\x01\x00" * (15140 * 24))
    transcript = [TranscriptSegment(start_ms=start, end_ms=end, text=text)
                  for start, end, text in zip(
                      [200, 11540 + gap_ms, 12940 + gap_ms],
                      [11540, 12940, 15000], texts,
                  )]
    reference_path = path.parent / "continuous-motion.mp4"
    reference_path.write_bytes(b"synthetic motion reference, no real model")
    with Database(path) as db:
        project = ProjectRepository(db).get(project_id)
        voice = VoiceProfileRepository(db).create(VoiceProfile(
            ip_profile_id=project.ip_profile_id, name="Fixture voice",
            provider="fixture-voice", provider_profile_id="fixture",
            reference_clip_ids=[uuid4()], consent=consent, created_at=now,
        ))
        ip = IPProfileRepository(db).get(project.ip_profile_id)
        IPProfileRepository(db).update(ip.model_copy(update={"default_voice_profile_id": voice.id}))
        master = AudioAssetRepository(db).create(AudioAsset(
            source_file=master_path.name,
            content_hash=hashlib.sha256(master_path.read_bytes()).hexdigest(),
            duration_ms=15140, sample_rate=24000, channels=1,
            authorization_reference="fixture:voice-consent", imported_at=now,
            transcript_source="fixture:independent-asr", transcript_segments=transcript,
            metadata={"voice_generation": {
                "project_id": str(project_id), "voice_profile_id": str(voice.id),
                "qa_state": "verified", "human_review_state": "approved",
                "target_text": "".join(texts),
                "qa": {"copy_coverage": 1.0, "missing_token_count": 0, "duplicate_token_count": 0},
                "human_review": {
                    "approved": True, "evidence_reference": "fixture:voice-review",
                    "findings": ["Synthetic fixture only"],
                    **{key: "pass" for key in (
                        "likeness", "naturalness", "emphasis", "pace", "pauses", "rhythm",
                    )}, "reviewed_at": now.isoformat(),
                },
            }},
        ))
        reference = AssetRepository(db).create(Asset(
            source_file=reference_path.name,
            content_hash=hashlib.sha256(reference_path.read_bytes()).hexdigest(),
            duration_ms=40000, width=1080, height=1920,
            fps=RationalFps(numerator=25, denominator=1), has_audio=False,
            authorization_reference="fixture:source-rights", imported_at=now,
        ))
        clip = ClipRepository(db).create(Clip(
            asset_id=reference.id, start_ms=0, end_ms=40000, asset_duration_ms=40000,
            talking_candidate=True, face_visibility=0.9, mouth_visibility=0.9,
            talking_reference_assessment=TalkingReferenceAssessment(
                burned_in_subtitles=False, evidence_reference="fixture:motion-observation",
            ),
        ))
        talking = TalkingProfileRepository(db).create(TalkingProfile(
            name="Fixture talking", provider="fixture-talking",
            reference_clip_ids=[clip.id], consent=consent, created_at=now,
        ))
        capability = ProviderMachineCapabilityProfileRepository(db).save(ProviderMachineCapabilityProfile(
            capability="talking", mode="local", provider="fixture-talking", model="fixture-model",
            runtime="fixture", machine_id="fixture-host", readiness="verified", quality_status="verified",
            verified_parameters={"fresh_voice_talking_duration_ms": 20000},
            provenance_source="fixture", evidence_reference="fixture:capability",
            commercial_status="commercial_safe", license_evidence_reference="fixture:license", updated_at=now,
        ))
    assert client.put(f"/projects/{project_id}/budget", json={
        "currency": "USD", "max_calls": 4, "allow_unknown_cost": True,
    }).status_code == 200
    plan = client.post(f"/projects/{project_id}/production-preflight", json={}).json()
    started = client.post(f"/projects/{project_id}/production-runs", json={
        "idempotency_key": "continuous-delivery", "expected_fingerprint": plan["fingerprint"],
    })
    assert started.status_code == 201, started.text
    return project_id, started.json(), scenes, master, talking, clip, capability


@pytest.mark.parametrize("gap_ms", [0, 140])
def test_run_delivery_intervals_reach_audio_slice_qa_and_assembly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gap_ms: int,
) -> None:
    """Exercise persisted context/binding/job/slice/QA/render handoffs, without models."""
    monkeypatch.setenv("CONTENT_OS_DATA_ROOT", str(tmp_path))
    path = tmp_path / "delivery.sqlite"
    expected = [(0, 11540), (11540, 12940), (12940, 15140)]
    extraction_intervals = []
    provider_inputs = []

    def pcm_extractor(command, **kwargs):
        # Execute the actual extraction function, replacing only FFmpeg itself.
        start_ms = round(float(command[command.index("-ss") + 1]) * 1000)
        duration_ms = round(float(command[command.index("-t") + 1]) * 1000)
        extraction_intervals.append((start_ms, start_ms + duration_ms))
        with wave.open(command[command.index("-i") + 1], "rb") as original:
            original.setpos(start_ms * 24)
            pcm = original.readframes(duration_ms * 24)
        with wave.open(command[-1], "wb") as sliced:
            sliced.setnchannels(1)
            sliced.setsampwidth(2)
            sliced.setframerate(24000)
            sliced.writeframes(pcm)
        return SimpleNamespace(returncode=0)

    def extract_with_pcm_runner(*args, **kwargs):
        return extract_talking_audio_slice(*args, **kwargs, command_runner=pcm_extractor)

    monkeypatch.setattr("app.jobs.handlers.extract_talking_audio_slice", extract_with_pcm_runner)

    class Provider:
        provider_name = "fixture-talking"
        model = "fixture-model"
        is_local = True

        def synthesize(self, profile, audio, reference, output):
            with wave.open(audio.source_file, "rb") as sliced:
                assert sliced.getnframes() == audio.duration_ms * 24
                assert sliced.readframes(1) == b"\x01\x00"
            provider_inputs.append(audio)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps({"duration_ms": audio.duration_ms}), encoding="utf-8")
            return TalkingSynthesisResult(output, provider_version="fixture-only", expected_video_fps=RationalFps(numerator=25, denominator=1))

    class Importer:
        data_root = tmp_path

        def __init__(self, db):
            self.db = db

        def import_path(self, source, authorization_reference, *, source_kind):
            duration = json.loads(source.read_text(encoding="utf-8"))["duration_ms"]
            return AssetRepository(self.db).create(Asset(
                source_kind=source_kind, source_file=str(source),
                content_hash=hashlib.sha256(source.read_bytes()).hexdigest(),
                duration_ms=duration, width=1080, height=1920,
                fps=RationalFps(numerator=25, denominator=1), has_audio=True,
                authorization_reference=authorization_reference, imported_at=datetime.now(timezone.utc),
            ))

    class Probe(FrameProbeFixture):
        def probe(self, checked):
            return ProbeMetadata(
                duration_ms=json.loads(checked.read_text(encoding="utf-8"))["duration_ms"],
                width=1080, height=1920, fps=RationalFps(numerator=25, denominator=1), has_audio=True,
                metadata={"streams": [{"codec_type": "video"}, {"codec_type": "audio"}]},
            )

    with TestClient(create_app(path)) as client:
        project_id, run, scenes, master, profile, clip, capability = _delivery_fixture(client, path, gap_ms)
        endpoint = f"/projects/{project_id}/production-runs/{run['id']}"
        for scene, (start, end) in zip(scenes, expected):
            context_response = client.get(f"{endpoint}/talking-source-context", params={"scene_plan_id": str(scene.id)})
            assert context_response.status_code == 200, context_response.text
            context = context_response.json()
            assert context["status"] == "ready"
            assert (context["speech_start_ms"], context["speech_end_ms"]) == (start, end)
            request = {
                "scene_plan_id": str(scene.id), "talking_profile_id": str(profile.id),
                "reference_clip_id": str(clip.id), "capability_profile_id": str(capability.id),
                "master_start_ms": start, "master_end_ms": end,
            }
            # Even a plausible point inside a known gap is not an authorized cut.
            wrong = client.post(f"{endpoint}/talking-source-bind", json={**request, "master_start_ms": start + 70})
            assert wrong.status_code == 409, wrong.text
            assert "talking_master_interval_not_exact_scene_speech" in wrong.text
            bound = client.post(f"{endpoint}/talking-source-bind", json=request)
            assert bound.status_code == 200, bound.text
            dispatched = client.post(f"{endpoint}/talking-dispatch", json={"scene_plan_id": str(scene.id)})
            assert dispatched.status_code == 200, dispatched.text

        with Database(path) as db:
            service = ProductionRunService(db, tmp_path)
            handler = TalkingGenerationJobHandler(
                TalkingProfileRepository(db), AudioAssetRepository(db), AssetRepository(db),
                Importer(db), Provider(), tmp_path / "generated",
                execution_runtime="fixture", execution_machine_id="fixture-host",
            )
            runner = JobRunner(JobStore(db), {JobType.GENERATE_TALKING: handler},
                worker_id="delivery-fixture", lease_duration=timedelta(minutes=1), max_attempts=1,
                on_completed=lambda job: service.reconcile_completed_talking_job(job.id, qa_worker_ready=True))
            for _ in scenes:
                finished = runner.run_once(allowed_types={JobType.GENERATE_TALKING})
                assert finished is not None and finished.status is JobStatus.COMPLETED, finished
            assert extraction_intervals == expected
            assert [(value.metadata["talking_slice"]["start_ms"], value.metadata["talking_slice"]["end_ms"])
                    for value in provider_inputs] == expected
            # ASR speech stays inside the delivery slice; silence is not labeled as speech.
            assert provider_inputs[1].transcript_segments == [TranscriptSegment(
                start_ms=gap_ms, end_ms=1400, text=scenes[1].voice_text,
            )]
            assert provider_inputs[-1].duration_ms == 2200
            assert provider_inputs[-1].transcript_segments[-1].end_ms == 2060
            qa_runner = JobRunner(JobStore(db), {
                JobType.VERIFY_TALKING: TalkingQaJobHandler(AssetRepository(db), data_root=tmp_path, probe=Probe()),
            }, worker_id="delivery-QA-fixture", lease_duration=timedelta(minutes=1), max_attempts=1,
                on_completed=VideoProductionService(db,tmp_path).completed)
            for _ in scenes:
                finished = qa_runner.run_once(allowed_types={JobType.VERIFY_TALKING})
                assert finished is not None and finished.status is JobStatus.COMPLETED, finished
            final = service.get(project_id, UUID(run["id"]))
            assert final.status == "render_pending", final
            render = JobRepository(db).get(final.render_job_id)
            spec = render.payload.video_spec
            assert spec.master_narration.end_ms == 15140
            assert [(scene.narration_start_ms, scene.narration_end_ms) for scene in spec.scenes] == expected
            assert [scene.visual.source_kind for scene in spec.scenes] == [SourceKind.AI_VIDEO] * 3
            assert spec.master_narration.transcript_segments == master.transcript_segments
            retained = AudioAssetRepository(db).get(master.id)
            assert retained.transcript_segments == master.transcript_segments
            assert retained.content_hash == master.content_hash
            assert ProviderCallRepository(db).list_for_project(project_id) == []
