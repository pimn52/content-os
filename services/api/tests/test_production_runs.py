"""V74a/b: durable render dispatch and approved-Master resume, no provider work."""
from __future__ import annotations

import hashlib
import json
import subprocess
import struct
import wave
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import pytest

from app.db import AssetRepository, AudioAssetRepository, ClipRepository, Database, JobRepository, ProviderCallRepository, ProviderMachineCapabilityProfileRepository, TalkingProfileRepository, VoiceProfileRepository
from app.domain.models import Asset, AudioAsset, Clip, ConsentRecord, JobStatus, JobType, ProviderMachineCapabilityProfile, RationalFps, ScenePlan, SourceKind, TalkingProfile, TalkingReferenceAssessment, TranscriptSegment, VisualIntent, VoiceHumanReview, VoiceProfile
from app.main import create_app
from app.jobs import JobRunner, JobStore
from app.jobs.handlers import TalkingGenerationJobHandler, TalkingQaJobHandler
from app.media.ffprobe import ProbeMetadata
from app.jobs.runner import JobExecutionError
from app.budget import ProviderCallLedger
from app.providers.talking import TalkingSynthesisResult
from app.production_runs import ProductionRunService
from app.talking.planned_preview import TalkingRunPreviewJobHandler
from app.media.ffprobe import FFProbeAdapter
from app.runtime import resolve_local_executable
from app.talking.source_suitability import TalkingSourceSuitabilityAssessment
from app.talking.source_admission import TalkingSourceAdmissionRepository
from app.voice_qa import apply_voice_human_review


def _admit_master(db_path: Path, project_id: str, copy: str, *, job_id: str | None = None) -> Path:
    source = db_path.parent / "approved-master.wav"
    with wave.open(str(source), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24_000)
        stream.writeframes(b"\x00\x00" * 72_000)
    now = datetime.now(timezone.utc)
    with Database(db_path) as db:
        AudioAssetRepository(db).create(AudioAsset(
            source_kind=SourceKind.USER_ASSET, source_file=source.name,
            content_hash=hashlib.sha256(source.read_bytes()).hexdigest(),
            duration_ms=3_000, sample_rate=24_000, channels=1,
            authorization_reference="voice-consent", imported_at=now,
            transcript_source="fixture-asr", transcript_segments=[TranscriptSegment(
                start_ms=0, end_ms=3_000, text=copy,
            )],
            metadata={"voice_generation": {
                "project_id": project_id, "qa_state": "verified", "human_review_state": "approved",
                **({"job_id": job_id} if job_id is not None else {}),
                "human_review": {
                    "approved": True, "evidence_reference": "fixture:voice-review",
                    "findings": ["Exact fixture approved"],
                    "likeness": "pass", "naturalness": "pass", "emphasis": "pass",
                    "pace": "pass", "pauses": "pass", "rhythm": "pass",
                    "reviewed_at": now.isoformat(),
                },
            }},
        ))
    return source


def _generated_voice_output(path: Path, project_id: str, job_id: str, copy: str) -> UUID:
    source = path.parent / "generated-take.wav"
    with wave.open(str(source), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24_000)
        stream.writeframes(b"\x00\x00" * 72_000)
    audio = AudioAsset(
        source_file=source.name, content_hash=hashlib.sha256(source.read_bytes()).hexdigest(),
        duration_ms=3_000, sample_rate=24_000, channels=1,
        authorization_reference="fixture:explicit-dispatch", imported_at=datetime.now(timezone.utc),
        metadata={"voice_generation": {
            "project_id": project_id, "job_id": job_id, "target_text": copy, "qa_state": "pending",
            "provider": "fixture-voice", "model": "fixture-model",
        }},
    )
    with Database(path) as db:
        AudioAssetRepository(db).create(audio)
    return audio.id


def _voice_dispatch_evidence(path: Path, *, license_status: str = "commercial_safe") -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    with Database(path) as db:
        asset = Asset(
            source_file="fixture-reference.mp4", content_hash="a" * 64, duration_ms=1_000,
            width=640, height=360, fps=RationalFps(numerator=25, denominator=1), has_audio=True,
            authorization_reference="fixture:reference-rights", imported_at=now,
        )
        AssetRepository(db).create(asset)
        clip = Clip(asset_id=asset.id, start_ms=0, end_ms=1_000, asset_duration_ms=1_000, voice_candidate=True)
        ClipRepository(db).create(clip)
        profile = VoiceProfile(
            name="Fixture voice", provider="fixture-voice", provider_profile_id="fixture-v1",
            reference_clip_ids=[clip.id],
            consent=ConsentRecord(subject_name="Fixture creator", basis="self", confirmed=True, confirmed_at=now),
            created_at=now,
        )
        VoiceProfileRepository(db).create(profile)
        capability = ProviderMachineCapabilityProfile(
            capability="voice", mode="local", provider="fixture-voice", model="fixture-model",
            runtime="fixture", machine_id="fixture-host", readiness="verified", quality_status="verified",
            provenance_source="fixture", evidence_reference="fixture:voice-capability",
            commercial_status=license_status,
            license_evidence_reference="fixture:license" if license_status == "commercial_safe" else None,
            updated_at=now,
        )
        ProviderMachineCapabilityProfileRepository(db).save(capability)
    return str(profile.id), str(capability.id)


def _fixture_complete_run_voice(client, path, project_id, waiting, texts, *, approved=True, resume=True):
    """Simulated completion/QA/review only; no provider or real human evidence."""
    profile_id, capability_id = _voice_dispatch_evidence(path)
    root = f"/projects/{project_id}/production-runs/{waiting['id']}"
    dispatched = client.post(f"{root}/voice-dispatch", json={
        "voice_profile_id": profile_id, "capability_profile_id": capability_id,
        "authorization_reference": "fixture:explicit-dispatch",
    })
    assert dispatched.status_code == 200, dispatched.text
    voice = dispatched.json()
    assert client.post(f"{root}/resume").status_code == 409
    with Database(path) as db:
        repo = JobRepository(db)
        job = repo.get(UUID(voice["voice_job_id"]))
        repo.update(job.model_copy(update={"status": JobStatus.COMPLETED}))
    audio_id = _generated_voice_output(path, project_id, voice["voice_job_id"], " ".join(texts))
    advanced = client.post(f"{root}/voice-qa-advance")
    assert advanced.status_code == 200, advanced.text
    qa_run = advanced.json()
    assert client.post(f"{root}/resume").status_code == 409
    with Database(path) as db:
        jobs = JobRepository(db)
        qa = jobs.get(UUID(qa_run["voice_qa_job_id"]))
        jobs.update(qa.model_copy(update={"status": JobStatus.COMPLETED}))
        audios = AudioAssetRepository(db)
        audio = audios.get(audio_id)
        metadata = dict(audio.metadata)
        metadata["voice_generation"] = {**metadata["voice_generation"], "qa_state": "verified"}
        audios.update(audio.model_copy(update={
            "metadata": metadata, "transcript_source": "fixture-asr",
            "transcript_segments": [TranscriptSegment(
                start_ms=index * 3000 // len(texts), end_ms=(index + 1) * 3000 // len(texts), text=text,
            ) for index, text in enumerate(texts)],
        }))
    assert client.post(f"{root}/resume").status_code == 409
    with Database(path) as db:
        audios = AudioAssetRepository(db)
        audios.update(apply_voice_human_review(audios.get(audio_id), VoiceHumanReview(
            approved=approved, evidence_reference="fixture:Voice-transition-review", findings=["Fixture only"],
            likeness="pass" if approved else "needs_revision", naturalness="pass", emphasis="pass",
            pace="pass", pauses="pass", rhythm="pass", reviewed_at=datetime.now(timezone.utc),
        )))
    if not resume:
        return client.get(root).json()
    response = client.post(f"{root}/resume")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["id"] == waiting["id"] and result["voice_audio_id"] == str(audio_id)
    assert result["voice_job_id"] == voice["voice_job_id"] and result["voice_qa_job_id"] == qa_run["voice_qa_job_id"]
    assert result["preflight_fingerprint"] == waiting["preflight_fingerprint"]
    return result


def _ready_fixture(client: TestClient, db_path: Path, *, with_master: bool = True) -> tuple[str, dict, Path]:
    created = client.post("/projects", json={"title": "Ready typography", "topic": "New"})
    assert created.status_code == 201, created.text
    project_id = created.json()["id"]
    scene = ScenePlan(
        project_id=project_id, scene_id="hook", order=0, purpose="hook",
        voice_text="A fresh creator point.", duration_target_ms=3_000,
        visual_intent=VisualIntent(subject="idea"),
        preferred_sources=[SourceKind.TYPOGRAPHY], fallback_sources=[],
    )
    saved = client.put(f"/projects/{project_id}/draft", json={
        "script": scene.voice_text, "topic": "New", "scenes": [scene.model_dump(mode="json")],
    })
    assert saved.status_code == 200, saved.text
    source = db_path.parent / "approved-master.wav"
    if with_master:
        _admit_master(db_path, project_id, scene.voice_text)
    budget = client.put(f"/projects/{project_id}/budget", json={
        "currency": "USD", "max_calls": 1, "allow_unknown_cost": True,
    })
    assert budget.status_code == 200, budget.text
    preflight_response = client.post(f"/projects/{project_id}/production-preflight", json={})
    assert preflight_response.status_code == 200, preflight_response.text
    return project_id, preflight_response.json(), source


def _talking_wait_fixture(client: TestClient, db_path: Path, *, with_master: bool = True) -> tuple[str, dict, Path]:
    created = client.post("/projects", json={"title": "Planned Talking", "topic": "New"})
    assert created.status_code == 201
    project_id = created.json()["id"]
    scenes = [
        ScenePlan(
            project_id=project_id, scene_id="hook", order=0, purpose="hook",
            voice_text="A fresh creator point.", duration_target_ms=3_000,
            visual_intent=VisualIntent(subject="creator", action="speaking"),
            preferred_sources=[SourceKind.AI_VIDEO], fallback_sources=[],
        ),
        ScenePlan(
            project_id=project_id, scene_id="summary", order=1, purpose="summary",
            voice_text="A clear takeaway.", duration_target_ms=3_000,
            visual_intent=VisualIntent(subject="idea"),
            preferred_sources=[SourceKind.TYPOGRAPHY], fallback_sources=[],
        ),
    ]
    copy = " ".join(scene.voice_text for scene in scenes)
    saved = client.put(f"/projects/{project_id}/draft", json={
        "script": copy, "topic": "New", "scenes": [scene.model_dump(mode="json") for scene in scenes],
    })
    assert saved.status_code == 200, saved.text
    source = db_path.parent / "approved-master.wav"
    if with_master:
        _admit_master(db_path, project_id, copy)
    budget = client.put(f"/projects/{project_id}/budget", json={
        "currency": "USD", "max_calls": 1, "allow_unknown_cost": True,
    })
    assert budget.status_code == 200, budget.text
    preflight = client.post(f"/projects/{project_id}/production-preflight", json={})
    assert preflight.status_code == 200, preflight.text
    return project_id, preflight.json(), source


def test_new_voice_to_talking_transition_is_atomic_and_restart_safe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "voice-to-talking.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, plan, _ = _talking_wait_fixture(client, path, with_master=False)
        waiting = client.post(f"/projects/{project_id}/production-runs", json={
            "idempotency_key": "same-plan-voice-talking", "expected_fingerprint": plan["fingerprint"],
        }).json()
        assert waiting["status"] == "awaiting_approved_master" and waiting["talking_dependencies"]
        approved = _fixture_complete_run_voice(client, path, project_id, waiting,
                                             ["A fresh creator point.", "A clear takeaway."], resume=False)
        root = f"/projects/{project_id}/production-runs/{waiting['id']}"
        original = ProductionRunService._advance_master_to_talking
        def fail_after_update(service, row, preflight):
            original(service, row, preflight)
            raise RuntimeError("fixture transition commit failure")
        with monkeypatch.context() as patch:
            patch.setattr(ProductionRunService, "_advance_master_to_talking", fail_after_update)
            with pytest.raises(RuntimeError, match="fixture transition commit failure"):
                client.post(f"{root}/resume")
        assert client.get(root).json() == approved
        transitioned = client.post(f"{root}/resume")
        assert transitioned.status_code == 200, transitioned.text
        result = transitioned.json()
        assert result["status"] == "awaiting_talking_dependencies"
        assert result["talking_master_audio_id"] == approved["voice_audio_id"]
        assert result["voice_job_id"] == approved["voice_job_id"]
        assert result["voice_qa_job_id"] == approved["voice_qa_job_id"]
        assert result["preflight_fingerprint"] == waiting["preflight_fingerprint"]
        assert len(result["talking_dependencies"]) == 1
        assert "talking_source_not_admitted:hook" in result["waiting_stop_reasons"]
        assert "talking_capability_not_verified" in result["waiting_stop_reasons"]
        assert result["render_job_id"] is None and result["talking_source_bindings"] == []
        assert client.post(f"{root}/resume").status_code == 409  # No second transition or inference.
    with TestClient(create_app(path)) as client:
        assert client.get(root).json() == result
        with Database(path) as db:
            assert db.connection.execute("SELECT count(*) FROM production_runs").fetchone()[0] == 1
            assert len(JobRepository(db).list()) == 2  # Voice and independent QA only.
            assert ProviderCallRepository(db).list_for_project(UUID(project_id)) == []


@pytest.mark.parametrize("failure", ["rejected", "qa_reopened", "wrong_master", "changed_bytes", "changed_draft", "cancelled", "voice_consent_revoked", "voice_license_changed"])
def test_new_voice_talking_bridge_blocks_changed_or_missing_authority(tmp_path: Path, failure: str) -> None:
    path = tmp_path / f"voice-bridge-{failure}.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, plan, _ = _talking_wait_fixture(client, path, with_master=False)
        waiting = client.post(f"/projects/{project_id}/production-runs", json={
            "idempotency_key": "bridge-failure", "expected_fingerprint": plan["fingerprint"],
        }).json()
        approved = _fixture_complete_run_voice(client, path, project_id, waiting,
                                             ["A fresh creator point.", "A clear takeaway."],
                                             approved=failure != "rejected", resume=False)
        root = f"/projects/{project_id}/production-runs/{waiting['id']}"
        with Database(path) as db:
            if failure in {"voice_consent_revoked", "voice_license_changed"}:
                voice = JobRepository(db).get(UUID(approved["voice_job_id"]))
                if failure == "voice_consent_revoked":
                    profiles = VoiceProfileRepository(db)
                    profile = profiles.get(voice.payload.voice_profile_id)
                    revoked = profile.model_copy(update={"consent": profile.consent.model_copy(update={"confirmed": False})})
                    db.connection.execute("UPDATE voice_profiles SET payload = ? WHERE id = ?",
                                          (revoked.model_dump_json(), str(profile.id)))
                else:
                    capabilities = ProviderMachineCapabilityProfileRepository(db)
                    capability = capabilities.get(voice.payload.execution_capability_profile_id)
                    capabilities.save(capability.model_copy(update={"commercial_status": "unknown", "license_evidence_reference": None}))
            if failure == "qa_reopened":
                jobs = JobRepository(db)
                qa = jobs.get(UUID(approved["voice_qa_job_id"]))
                jobs.update(qa.model_copy(update={"status": JobStatus.PENDING}))
            if failure == "wrong_master":
                audios = AudioAssetRepository(db)
                audio = audios.get(UUID(approved["voice_audio_id"]))
                metadata = dict(audio.metadata)
                metadata["voice_generation"] = {**metadata["voice_generation"], "job_id": str(uuid4())}
                audios.update(audio.model_copy(update={"metadata": metadata}))
            if failure == "changed_bytes":
                (tmp_path / "generated-take.wav").write_bytes(b"changed voice")
        if failure == "changed_draft":
            draft = client.get(f"/projects/{project_id}/draft").json()
            assert client.put(f"/projects/{project_id}/draft", json={
                "script": draft["script"] + " change", "topic": "New", "scenes": draft["scenes"],
            }).status_code == 200
        if failure == "cancelled":
            assert client.post(f"{root}/cancel").status_code == 200
        assert client.post(f"{root}/resume").status_code == 409
        with Database(path) as db:
            assert len(JobRepository(db).list()) == 2
            row = db.connection.execute("SELECT * FROM production_runs WHERE id = ?", (waiting["id"],)).fetchone()
            assert row["stage"] != "awaiting_talking" and row["talking_master_audio_id"] is None


def test_new_voice_talking_plan_retains_budget_stop_without_dispatch(tmp_path: Path) -> None:
    path = tmp_path / "voice-talking-budget.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, _, _ = _talking_wait_fixture(client, path, with_master=False)
        profile_id, capability_id = _voice_dispatch_evidence(path)
        assert client.put(f"/projects/{project_id}/budget", json={
            "currency": "USD", "max_calls": 0, "allow_unknown_cost": True,
        }).status_code == 200
        plan = client.post(f"/projects/{project_id}/production-preflight", json={}).json()
        started = client.post(f"/projects/{project_id}/production-runs", json={
            "idempotency_key": "zero-budget-voice-talking", "expected_fingerprint": plan["fingerprint"],
        })
        assert started.status_code == 201, started.text
        waiting = started.json()
        assert "budget_call_limit" in waiting["waiting_stop_reasons"]
        response = client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/voice-dispatch", json={
            "voice_profile_id": profile_id, "capability_profile_id": capability_id,
            "authorization_reference": "fixture:budget-blocked",
        })
        assert response.status_code == 409 and "voice_budget_not_authorized" in response.text
        with Database(path) as db:
            assert JobRepository(db).list() == [] and ProviderCallRepository(db).list() == []


def test_new_talking_plan_waits_durably_without_selection_or_jobs(tmp_path: Path) -> None:
    path = tmp_path / "talking-wait.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, plan, _ = _talking_wait_fixture(client, path)
        scene = plan["scenes"][0]
        assert scene["production_need"] == "new_talking" and scene["selected_candidate"] is None
        assert plan["preliminary_edit_plan"] is None
        payload = {"idempotency_key": "talking-plan", "expected_fingerprint": plan["fingerprint"]}
        first = client.post(f"/projects/{project_id}/production-runs", json=payload)
        assert first.status_code == 201, first.text
        run = first.json()
        assert run["status"] == "awaiting_talking_dependencies"
        assert run["render_job_id"] is None and run["voice_job_id"] is None
        assert run["talking_master_audio_id"] is not None
        assert run["talking_dependencies"] == [{
            "scene_plan_id": scene["scene_plan_id"], "scene_id": "hook",
            "visual_dependency_fingerprint": scene["visual_dependency_fingerprint"],
        }]
        assert "talking_source_not_admitted:hook" in run["waiting_stop_reasons"]
        assert "provider_license_scope_unverified" in run["waiting_stop_reasons"]
        assert client.post(f"/projects/{project_id}/production-runs", json=payload).json() == run
        duplicate = client.post(f"/projects/{project_id}/production-runs", json={
            **payload, "idempotency_key": "other-click",
        })
        assert duplicate.status_code == 201 and duplicate.json()["id"] == run["id"]
        early = client.post(f"/projects/{project_id}/production-runs/{run['id']}/resume")
        assert early.status_code == 409 and "talking_dependencies_not_admitted" in early.json()["detail"]["reasons"]
        with Database(path) as db:
            assert JobRepository(db).list() == []
            assert ProviderCallRepository(db).list_for_project(UUID(project_id)) == []
    with TestClient(create_app(path)) as client:
        assert client.get(f"/projects/{project_id}/production-runs/{run['id']}").json() == run
        cancelled = client.post(f"/projects/{project_id}/production-runs/{run['id']}/cancel")
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
        assert cancelled.json()["talking_dependencies"] == run["talking_dependencies"]
        assert client.post(f"/projects/{project_id}/production-runs/{run['id']}/resume").status_code == 409


def test_new_talking_wait_rejects_unapproved_or_changed_master_and_stale_plan(tmp_path: Path) -> None:
    path = tmp_path / "talking-blocked.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, plan, source = _talking_wait_fixture(client, path, with_master=False)
        missing = client.post(f"/projects/{project_id}/production-runs", json={
            "idempotency_key": "missing-master", "expected_fingerprint": plan["fingerprint"],
        })
        assert missing.status_code == 201 and missing.json()["status"] == "awaiting_approved_master"
        assert client.post(f"/projects/{project_id}/production-runs/{missing.json()['id']}/resume").status_code == 409
        _admit_master(path, project_id, "A fresh creator point. A clear takeaway.")
        approved = client.post(f"/projects/{project_id}/production-preflight", json={}).json()
        stale = client.post(f"/projects/{project_id}/production-runs", json={
            "idempotency_key": "stale", "expected_fingerprint": plan["fingerprint"],
        })
        assert stale.status_code == 409 and stale.json()["detail"] == "stale_production_preflight"
        source.write_bytes(b"changed after approval")
        changed = client.post(f"/projects/{project_id}/production-runs", json={
            "idempotency_key": "changed", "expected_fingerprint": approved["fingerprint"],
        })
        assert changed.status_code == 409
        assert "master_narration_bytes_changed" in changed.json()["detail"]["reasons"]
        with Database(path) as db:
            assert JobRepository(db).list() == []
            assert ProviderCallRepository(db).list_for_project(UUID(project_id)) == []


def test_talking_wait_keeps_budget_block_and_rejects_capture_or_changed_plan(tmp_path: Path) -> None:
    path = tmp_path / "talking-boundaries.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, plan, _ = _talking_wait_fixture(client, path)
        budget = client.put(f"/projects/{project_id}/budget", json={
            "currency": "USD", "max_calls": 0, "allow_unknown_cost": True,
        })
        assert budget.status_code == 200
        budget_plan = client.post(f"/projects/{project_id}/production-preflight", json={}).json()
        assert "budget_call_limit" in budget_plan["stop_reasons"]
        waiting = client.post(f"/projects/{project_id}/production-runs", json={
            "idempotency_key": "budget-wait", "expected_fingerprint": budget_plan["fingerprint"],
        })
        assert waiting.status_code == 201, waiting.text
        assert "budget_call_limit" in waiting.json()["waiting_stop_reasons"]
        assert waiting.json()["render_job_id"] is None
        stale = client.post(f"/projects/{project_id}/production-runs", json={
            "idempotency_key": "old-plan", "expected_fingerprint": plan["fingerprint"],
        })
        assert stale.status_code == 409
        capture = ScenePlan(
            project_id=project_id, scene_id="capture", order=1, purpose="summary",
            voice_text="A clear takeaway.", duration_target_ms=3_000,
            visual_intent=VisualIntent(subject="creator footage"),
            preferred_sources=[SourceKind.CAPTURE], fallback_sources=[],
        )
        old = client.get(f"/projects/{project_id}/draft").json()
        saved = client.put(f"/projects/{project_id}/draft", json={
            "script": old["script"], "topic": old["topic"], "scenes": [old["scenes"][0], capture.model_dump(mode="json")],
        })
        assert saved.status_code == 200, saved.text
        capture_plan = client.post(f"/projects/{project_id}/production-preflight", json={}).json()
        assert "capture_required:capture" in capture_plan["stop_reasons"]
        rejected = client.post(f"/projects/{project_id}/production-runs", json={
            "idempotency_key": "capture-plan", "expected_fingerprint": capture_plan["fingerprint"],
        })
        assert rejected.status_code == 409
        conflicting = client.post(f"/projects/{project_id}/production-runs", json={
            "idempotency_key": "budget-wait", "expected_fingerprint": capture_plan["fingerprint"],
        })
        assert conflicting.status_code == 409
        with Database(path) as db:
            assert JobRepository(db).list() == []
            assert ProviderCallRepository(db).list_for_project(UUID(project_id)) == []


def _talking_source_fixture(client: TestClient, path: Path, *, target_duration_ms: int = 3000, new_voice: bool = False, policy_version: int = 1) -> tuple[str, dict, dict, Path]:
    project_id = client.post("/projects", json={"title": "Talking source", "topic": "New"}).json()["id"]
    scene = ScenePlan(
        project_id=project_id, scene_id="hook", order=0, purpose="hook",
        voice_text="A fresh creator point.", duration_target_ms=target_duration_ms,
        visual_intent=VisualIntent(subject="creator", action="speaking"),
        preferred_sources=[SourceKind.AI_VIDEO], fallback_sources=[],
    )
    saved = client.put(f"/projects/{project_id}/draft", json={
        "script": scene.voice_text, "topic": "New", "scenes": [scene.model_dump(mode="json")],
    })
    assert saved.status_code == 200, saved.text
    if not new_voice:
        _admit_master(path, project_id, scene.voice_text)
    source = path.parent / "talking-reference.mp4"
    source.write_bytes(b"fixture reference bytes")
    now = datetime.now(timezone.utc)
    with Database(path) as db:
        asset = Asset(
            source_file=source.name, content_hash=hashlib.sha256(source.read_bytes()).hexdigest(),
            duration_ms=5_000, width=1080, height=1920,
            fps=RationalFps(numerator=25, denominator=1), has_audio=True,
            authorization_reference="fixture:source-rights", imported_at=now,
        )
        AssetRepository(db).create(asset)
        clip = Clip(
            asset_id=asset.id, start_ms=0, end_ms=5_000, asset_duration_ms=5_000,
            talking_candidate=True, face_visibility=0.9, mouth_visibility=0.9,
            talking_reference_assessment=TalkingReferenceAssessment(
                burned_in_subtitles=False, evidence_reference="fixture:reference-observation",
            ),
        )
        ClipRepository(db).create(clip)
        profile = TalkingProfile(
            name="Fixture talking", provider="fixture-talking", reference_clip_ids=[clip.id],
            consent=ConsentRecord(subject_name="Creator", basis="self", confirmed=True, confirmed_at=now),
            created_at=now,
        )
        TalkingProfileRepository(db).create(profile)
        capability = ProviderMachineCapabilityProfile(
            capability="talking", mode="local", provider="fixture-talking", model="fixture-model",
            runtime="fixture", machine_id="fixture-host", readiness="verified", quality_status="verified",
            verified_parameters={"fresh_voice_talking_duration_ms": 4_460},
            provenance_source="fixture", evidence_reference="fixture:talking-capability",
            commercial_status="commercial_safe", license_evidence_reference="fixture:license", updated_at=now,
        )
        ProviderMachineCapabilityProfileRepository(db).save(capability)
    assert client.put(f"/projects/{project_id}/budget", json={
        "currency": "USD", "max_calls": 1, "allow_unknown_cost": True,
    }).status_code == 200
    plan = client.post(f"/projects/{project_id}/production-preflight", json={}).json()
    started = client.post(f"/projects/{project_id}/production-runs", json={
        "idempotency_key": "source-binding", "expected_fingerprint": plan["fingerprint"],
        "talking_review_policy_version": policy_version,
    })
    assert started.status_code == 201, started.text
    payload = {
        "scene_plan_id": str(scene.id), "talking_profile_id": str(profile.id),
        "reference_clip_id": str(clip.id), "capability_profile_id": str(capability.id),
        "master_start_ms": 0, "master_end_ms": 3_000,
        "authorization_reference": "fixture:explicit-source-choice",
    }
    waiting = started.json()
    if new_voice:
        waiting = _fixture_complete_run_voice(client, path, project_id, waiting, [scene.voice_text])
    return project_id, waiting, payload, source


def test_talking_reference_candidate_binding_is_durable_but_not_dispatch_ready(tmp_path: Path) -> None:
    path = tmp_path / "bind.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, waiting, payload, _ = _talking_source_fixture(client, path)
        url = f"/projects/{project_id}/production-runs/{waiting['id']}/talking-source-bind"
        response = client.post(url, json=payload)
        assert response.status_code == 200, response.text
        bound = response.json()
        assert bound["status"] == "awaiting_talking_dependencies"
        assert bound["talking_source_bindings"][0]["suitability"] == "full_interval_unknown"
        assert bound["talking_source_bindings"][0]["reference_clip_id"] == payload["reference_clip_id"]
        assert bound["talking_source_bindings"][0]["reference_assessment_reference"] == "fixture:reference-observation"
        assert bound["talking_source_bindings"][0]["license_evidence_reference"] == "fixture:license"
        assert "talking_full_interval_suitability_unverified:hook" in bound["waiting_stop_reasons"]
        assert client.post(url, json=payload).json() == bound
        changed_brief = client.post(url, json={**payload, "brief": {"require_no_burned_subtitles": False}})
        assert changed_brief.status_code == 409
        conflict = client.post(url, json={**payload, "authorization_reference": "different"})
        assert conflict.status_code == 409
        assert client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/resume").status_code == 409
        with Database(path) as db:
            assert JobRepository(db).list() == []
            assert ProviderCallRepository(db).list_for_project(UUID(project_id)) == []
    with TestClient(create_app(path)) as client:
        assert client.get(f"/projects/{project_id}/production-runs/{waiting['id']}").json() == bound


def test_talking_source_bind_rejects_unverified_license_and_stale_draft(tmp_path: Path) -> None:
    path = tmp_path / "bind-license.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, waiting, payload, _ = _talking_source_fixture(client, path)
        url = f"/projects/{project_id}/production-runs/{waiting['id']}/talking-source-bind"
        with Database(path) as db:
            capability = ProviderMachineCapabilityProfileRepository(db).get(UUID(payload["capability_profile_id"]))
            ProviderMachineCapabilityProfileRepository(db).save(capability.model_copy(update={
                "commercial_status": "non_commercial_only", "license_evidence_reference": None,
            }))
        rejected = client.post(url, json=payload)
        assert rejected.status_code == 409
        assert "talking_provider_license_scope_unverified" in rejected.json()["detail"]["reasons"]
        with Database(path) as db:
            capability = ProviderMachineCapabilityProfileRepository(db).get(UUID(payload["capability_profile_id"]))
            ProviderMachineCapabilityProfileRepository(db).save(capability.model_copy(update={
                "commercial_status": "commercial_safe", "license_evidence_reference": "fixture:license",
            }))
        draft = client.get(f"/projects/{project_id}/draft").json()
        saved = client.put(f"/projects/{project_id}/draft", json={
            "script": draft["script"], "topic": "changed", "scenes": draft["scenes"],
        })
        assert saved.status_code == 200, saved.text
        stale = client.post(url, json=payload)
        assert stale.status_code == 409 and "draft changed" in stale.json()["detail"]
        with Database(path) as db:
            assert JobRepository(db).list() == []
            assert ProviderCallRepository(db).list_for_project(UUID(project_id)) == []
def test_talking_source_bind_rejects_invalid_interval_source_and_budget(tmp_path: Path) -> None:
    path = tmp_path / "bind-blocked.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, waiting, payload, source = _talking_source_fixture(client, path)
        url = f"/projects/{project_id}/production-runs/{waiting['id']}/talking-source-bind"
        wrong = client.post(url, json={**payload, "master_start_ms": 100})
        assert wrong.status_code == 409 and "talking_master_interval_not_exact_scene_speech" in wrong.json()["detail"]["reasons"]
        original_source = source.read_bytes()
        source.write_bytes(b"changed source bytes")
        changed = client.post(url, json=payload)
        assert changed.status_code == 409 and "talking_reference_bytes_changed" in changed.json()["detail"]["reasons"]
        source.write_bytes(original_source)
        with Database(path) as db:
            clip = ClipRepository(db).get(UUID(payload["reference_clip_id"]))
            ClipRepository(db).update(clip.model_copy(update={"talking_reference_assessment": None}))
        unassessed = client.post(url, json=payload)
        assert unassessed.status_code == 409 and "talking_reference_fit_not_verified" in unassessed.json()["detail"]["reasons"]
        with Database(path) as db:
            clip = ClipRepository(db).get(UUID(payload["reference_clip_id"]))
            ClipRepository(db).update(clip.model_copy(update={"talking_reference_assessment": TalkingReferenceAssessment(
                burned_in_subtitles=False, evidence_reference="fixture:reassessment",
            )}))
        assert client.put(f"/projects/{project_id}/budget", json={
            "currency": "USD", "max_calls": 0, "allow_unknown_cost": True,
        }).status_code == 200
        blocked = client.post(url, json=payload)
        assert blocked.status_code == 409 and "talking_budget_not_authorized" in blocked.json()["detail"]["reasons"]
        with Database(path) as db:
            assert JobRepository(db).list() == []
            assert ProviderCallRepository(db).list_for_project(UUID(project_id)) == []


def _talking_assessment_payload(binding: dict, *, key: str, evidence_class: str = "assisted_test") -> dict:
    return {
        "idempotency_key": key,
        "source_asset_id": binding["reference_asset_id"],
        "source_clip_id": binding["reference_clip_id"],
        "source_content_hash": binding["reference_asset_hash"],
        "start_ms": binding["reference_start_ms"],
        "end_ms": binding["reference_end_ms"],
        "presentation": "source_native_portrait",
        "evidence_class": evidence_class,
        "method": "human_full_interval_review",
        "method_version": "fixture-v1",
        "coverage": "full_interval_continuous",
        "face_head_clearance": "pass", "motion_continuity": "pass",
        "subtitle_clearance": "pass", "effective_quality": "pass",
        "reviewer_reference": "fixture:reviewer",
        "evidence_reference": "fixture:full-interval-review",
    }


def test_talking_full_interval_review_claim_is_exact_durable_and_not_dispatch(tmp_path: Path) -> None:
    path = tmp_path / "suitability.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, waiting, binding_request, _ = _talking_source_fixture(client, path)
        bind = client.post(
            f"/projects/{project_id}/production-runs/{waiting['id']}/talking-source-bind",
            json=binding_request,
        )
        assert bind.status_code == 200, bind.text
        binding = bind.json()["talking_source_bindings"][0]
        payload = _talking_assessment_payload(binding, key="review-1")
        # These references are supplied by the caller, not authenticated review evidence.
        payload["reviewer_reference"] = "caller-asserted-reviewer"
        payload["evidence_reference"] = "caller-asserted-evidence"
        record = client.post("/talking-reference-suitability-assessments", json=payload)
        assert record.status_code == 201, record.text
        assessment = record.json()
        assert assessment["reviewer_reference"] == "caller-asserted-reviewer"
        assert assessment["evidence_reference"] == "caller-asserted-evidence"
        assert client.post("/talking-reference-suitability-assessments", json=payload).json() == assessment
        changed = client.post("/talking-reference-suitability-assessments", json={
            **payload, "subtitle_clearance": "unknown",
        })
        assert changed.status_code == 409
        apply_url = f"/projects/{project_id}/production-runs/{waiting['id']}/talking-suitability-apply"
        request = {"scene_plan_id": binding["scene_plan_id"], "assessment_id": assessment["id"]}
        applied = client.post(apply_url, json=request)
        assert applied.status_code == 200, applied.text
        run = applied.json()
        assert run["status"] == "awaiting_talking_dependencies"
        assert run["talking_source_bindings"][0]["suitability"] == "review_claimed_suitable"
        assert run["talking_source_bindings"][0]["suitability_assessment_id"] == assessment["id"]
        assert "talking_dispatch_not_implemented" in run["waiting_stop_reasons"]
        assert client.post(apply_url, json=request).json() == run
        assert client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/resume").status_code == 409
        with Database(path) as db:
            assert JobRepository(db).list() == []
            assert ProviderCallRepository(db).list_for_project(UUID(project_id)) == []
    with TestClient(create_app(path)) as client:
        assert client.get(f"/projects/{project_id}/production-runs/{waiting['id']}").json() == run
        assert client.get(f"/talking-reference-suitability-assessments/{assessment['id']}").json() == assessment


def test_talking_source_admission_requires_trusted_actor_and_exact_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "review-admission.sqlite"
    secret = "local-review-key-with-at-least-32-random-characters"
    monkeypatch.setenv("CONTENT_OS_TALKING_REVIEW_KEY_HASHES", json.dumps({
        "local-reviewer": hashlib.sha256(secret.encode()).hexdigest(),
    }))
    evidence = tmp_path / "review-evidence.json"
    evidence.write_bytes(b'{"reviewed_source":"fixture","coverage":"full"}')
    with TestClient(create_app(path)) as client:
        project_id, waiting, binding_request, source = _talking_source_fixture(client, path)
        bound = client.post(
            f"/projects/{project_id}/production-runs/{waiting['id']}/talking-source-bind",
            json=binding_request,
        ).json()
        binding = bound["talking_source_bindings"][0]
        claim = client.post("/talking-reference-suitability-assessments", json={
            **_talking_assessment_payload(binding, key="adoptable-review"),
            "evidence_reference": evidence.name,
        })
        assert claim.status_code == 201, claim.text
        claim_id = claim.json()["id"]
        url = f"/talking-reference-suitability-assessments/{claim_id}/admissions"
        body = {"idempotency_key": "adopt-once", "evidence_sha256": hashlib.sha256(evidence.read_bytes()).hexdigest()}
        assert client.post(url, json=body).status_code == 403
        assert client.post(url, json=body, headers={"x-content-os-talking-review-key": "forged"}).status_code == 403
        headers = {"x-content-os-talking-review-key": secret}
        admitted = client.post(url, json=body, headers=headers)
        assert admitted.status_code == 201, admitted.text
        receipt = admitted.json()
        assert receipt["actor_id"] == "local-reviewer"
        assert client.post(url, json=body, headers=headers).json() == receipt
        with Database(path) as db:
            repository = TalkingSourceAdmissionRepository(db, tmp_path)
            assert repository.require_current(repository.get(UUID(receipt["id"]))).id == UUID(claim_id)
            assert JobRepository(db).list() == []
            assert ProviderCallRepository(db).list_for_project(UUID(project_id)) == []
        evidence.write_bytes(b"changed evidence")
        assert client.post(url, json=body, headers=headers).status_code == 409
        with Database(path) as db:
            repository = TalkingSourceAdmissionRepository(db, tmp_path)
            with pytest.raises(ValueError, match="evidence bytes changed"):
                repository.require_current(repository.get(UUID(receipt["id"])))
        evidence.write_bytes(b'{"reviewed_source":"fixture","coverage":"full"}')
        source.write_bytes(b"changed source")
        with Database(path) as db:
            repository = TalkingSourceAdmissionRepository(db, tmp_path)
            with pytest.raises(ValueError, match="source bytes changed"):
                repository.require_current(repository.get(UUID(receipt["id"])))
        source.write_bytes(b"fixture reference bytes")
        revoked = client.post(f"/talking-source-admissions/{receipt['id']}/revoke", json={
            "reason": "Evidence withdrawn",
        }, headers=headers)
        assert revoked.status_code == 200 and revoked.json()["revoked_by"] == "local-reviewer"
        assert client.post(url, json=body, headers=headers).status_code == 409
    with Database(path) as db:
        repository = TalkingSourceAdmissionRepository(db, tmp_path)
        with pytest.raises(ValueError, match="revoked"):
            repository.require_current(repository.get(UUID(receipt["id"])))


def test_talking_source_admission_rejects_nonpositive_and_uninspectable_claims(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "review-admission-negative.sqlite"
    monkeypatch.delenv("CONTENT_OS_ALLOW_LEGACY_TALKING_EVALUATION", raising=False)
    secret = "another-local-review-key-with-at-least-32-characters"
    monkeypatch.setenv("CONTENT_OS_TALKING_REVIEW_KEY_HASHES", json.dumps({
        "reviewer": hashlib.sha256(secret.encode()).hexdigest(),
    }))
    headers = {"x-content-os-talking-review-key": secret}
    with TestClient(create_app(path)) as client:
        project_id, waiting, binding_request, _ = _talking_source_fixture(client, path)
        binding = client.post(
            f"/projects/{project_id}/production-runs/{waiting['id']}/talking-source-bind",
            json=binding_request,
        ).json()["talking_source_bindings"][0]
        for suffix, changes in (
            ("fixture", {"evidence_class": "fixture"}),
            ("unknown", {"subtitle_clearance": "unknown"}),
            ("negative", {"face_head_clearance": "fail"}),
            ("outside", {"evidence_reference": "../outside-evidence.json"}),
            ("missing", {"evidence_reference": "missing-evidence.json"}),
        ):
            claim = client.post("/talking-reference-suitability-assessments", json={
                **_talking_assessment_payload(binding, key=f"claim-{suffix}"), **changes,
            })
            assert claim.status_code == 201, claim.text
            attempt = client.post(f"/talking-reference-suitability-assessments/{claim.json()['id']}/admissions", json={
                "idempotency_key": f"admit-{suffix}", "evidence_sha256": "0" * 64,
            }, headers=headers)
            assert attempt.status_code == 409, (suffix, attempt.text)
        direct = {
            "idempotency_key": "legacy-disabled", "talking_profile_id": binding["talking_profile_id"],
            "reference_clip_id": binding["reference_clip_id"],
            "narration_audio_id": binding["master_audio_id"],
            "authorization_reference": "caller-asserted",
        }
        assert client.post(f"/projects/{project_id}/talking-jobs", json=direct).status_code == 403
        assert client.post(f"/projects/{project_id}/talking-slice-series-jobs", json={
            **direct, "execution_machine_id": "fixture-host",
        }).status_code == 403
        assert client.post(f"/projects/{project_id}/talking-slice-series/{uuid4()}/recover-failed-child", json={
            "failed_job_id": str(uuid4()), "evidence_reference": "fixture:failure",
        }).status_code == 403
        with Database(path) as db:
            assert JobRepository(db).list() == []


def _ready_talking_dispatch_fixture(client: TestClient, path: Path, monkeypatch: pytest.MonkeyPatch, *, target_duration_ms: int = 3000, new_voice: bool = False, policy_version: int = 1) -> tuple[str, dict, dict, Path]:
    secret = "planned-talking-review-key-with-adequate-entropy-0001"
    monkeypatch.setenv("CONTENT_OS_TALKING_REVIEW_KEY_HASHES", json.dumps({
        "reviewer": hashlib.sha256(secret.encode()).hexdigest(),
    }))
    project_id, waiting, binding_request, source = _talking_source_fixture(client, path, target_duration_ms=target_duration_ms, new_voice=new_voice, policy_version=policy_version)
    bound = client.post(
        f"/projects/{project_id}/production-runs/{waiting['id']}/talking-source-bind",
        json=binding_request,
    )
    assert bound.status_code == 200, bound.text
    binding = bound.json()["talking_source_bindings"][0]
    evidence = path.parent / "planned-review-evidence.json"
    evidence.write_bytes(b'{"source":"portrait","review":"pass"}')
    claim = client.post("/talking-reference-suitability-assessments", json={
        **_talking_assessment_payload(binding, key="planned-dispatch-review"),
        "evidence_reference": evidence.name,
    })
    assert claim.status_code == 201, claim.text
    applied = client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/talking-suitability-apply", json={
        "scene_plan_id": binding["scene_plan_id"], "assessment_id": claim.json()["id"],
    })
    assert applied.status_code == 200, applied.text
    receipt = client.post(f"/talking-reference-suitability-assessments/{claim.json()['id']}/admissions", json={
        "idempotency_key": "planned-dispatch-adoption",
        "evidence_sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
    }, headers={"x-content-os-talking-review-key": secret})
    assert receipt.status_code == 201, receipt.text
    return project_id, waiting, binding, evidence


def test_planned_talking_dispatch_is_exact_idempotent_and_stays_awaiting_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "planned-talking.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, waiting, binding, _ = _ready_talking_dispatch_fixture(client, path, monkeypatch)
        with Database(path) as db:
            admission = TalkingSourceAdmissionRepository(db, tmp_path).get_for_assessment(UUID(
                client.get(f"/projects/{project_id}/production-runs/{waiting['id']}").json()["talking_source_bindings"][0]["suitability_assessment_id"]
            ))
        assert admission is not None
        url = f"/projects/{project_id}/production-runs/{waiting['id']}/talking-dispatch"
        payload = {"scene_plan_id": binding["scene_plan_id"], "source_admission_id": str(admission.id)}
        assert client.post(url, json={**payload, "source_admission_id": str(uuid4())}).status_code == 409
        with Database(path) as db:
            assert JobRepository(db).list() == []
        first = client.post(url, json=payload)
        assert first.status_code == 200, first.text
        run = first.json()
        job_id = run["talking_source_bindings"][0]["talking_job_id"]
        assert job_id and run["status"] == "awaiting_talking_dependencies"
        assert "talking_dispatch_not_implemented" not in run["waiting_stop_reasons"]
        assert client.post(url, json=payload).json()["talking_source_bindings"][0]["talking_job_id"] == job_id
        with Database(path) as db:
            jobs = JobRepository(db).list()
            assert len(jobs) == 1 and str(jobs[0].id) == job_id
            assert jobs[0].payload.planned_context.source_admission_id == admission.id
            assert (jobs[0].payload.slice_start_segment_index, jobs[0].payload.slice_end_segment_index) == (0, 1)
            assert ProviderCallRepository(db).list_for_project(UUID(project_id)) == []
        assert client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/resume").status_code == 409
    with TestClient(create_app(path)) as client:
        assert client.post(url, json=payload).json()["talking_source_bindings"][0]["talking_job_id"] == job_id
        cancelled = client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/cancel")
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
        with Database(path) as db:
            assert JobRepository(db).get(UUID(job_id)).status is JobStatus.CANCELLED


def test_planned_talking_dispatch_and_worker_fail_closed_on_changed_gates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "planned-talking-stale.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, waiting, binding, evidence = _ready_talking_dispatch_fixture(client, path, monkeypatch)
        with Database(path) as db:
            admission = TalkingSourceAdmissionRepository(db, tmp_path).get_for_assessment(UUID(
                client.get(f"/projects/{project_id}/production-runs/{waiting['id']}").json()["talking_source_bindings"][0]["suitability_assessment_id"]
            ))
        assert admission is not None
        url = f"/projects/{project_id}/production-runs/{waiting['id']}/talking-dispatch"
        payload = {"scene_plan_id": binding["scene_plan_id"], "source_admission_id": str(admission.id)}
        with Database(path) as db:
            capability = ProviderMachineCapabilityProfileRepository(db).get(UUID(binding["capability_profile_id"]))
            ProviderMachineCapabilityProfileRepository(db).save(capability.model_copy(update={
                "verified_parameters": {},
            }))
        missing_bound = client.post(url, json=payload)
        assert missing_bound.status_code == 409
        assert "talking_provider_duration_bound_unverified" in missing_bound.text
        with Database(path) as db:
            ProviderMachineCapabilityProfileRepository(db).save(capability)
        dispatched = client.post(url, json=payload)
        assert dispatched.status_code == 200, dispatched.text
        job_id = UUID(dispatched.json()["talking_source_bindings"][0]["talking_job_id"])
        with Database(path) as db:
            job = JobRepository(db).get(job_id).model_copy(update={"status": JobStatus.RUNNING})
            ProductionRunService(db, tmp_path).require_talking_job_admission(
                job, provider="fixture-talking", model="fixture-model",
                runtime="fixture", machine_id="fixture-host",
            )

            class Provider:
                provider_name = "fixture-talking"
                model = "fixture-model"

                def synthesize(self, *args: object) -> None:
                    raise AssertionError("provider must not be called after admission revocation")

            class Importer:
                data_root = tmp_path

            handler = TalkingGenerationJobHandler(
                TalkingProfileRepository(db), AudioAssetRepository(db), AssetRepository(db),
                Importer(), Provider(), tmp_path / "generated", execution_runtime="fixture",
                execution_machine_id="fixture-host",  # type: ignore[arg-type]
            )
            handler_wrong_host = TalkingGenerationJobHandler(
                TalkingProfileRepository(db), AudioAssetRepository(db), AssetRepository(db),
                Importer(), Provider(), tmp_path / "generated", execution_runtime="fixture",
                execution_machine_id="other-host",  # type: ignore[arg-type]
            )
            with pytest.raises(JobExecutionError) as wrong:
                handler_wrong_host(job)
            assert wrong.value.code == "talking_worker_identity_mismatch"
            assert ProviderCallRepository(db).list_for_project(UUID(project_id)) == []
            capability = ProviderMachineCapabilityProfileRepository(db).get(UUID(binding["capability_profile_id"]))
            ProviderMachineCapabilityProfileRepository(db).save(capability.model_copy(update={
                "commercial_status": "non_commercial_only", "license_evidence_reference": None,
            }))
            with pytest.raises(JobExecutionError) as license_block:
                handler(job)
            assert license_block.value.code == "talking_provider_license_scope_unverified"
            ProviderMachineCapabilityProfileRepository(db).save(capability)
            assert ProviderCallRepository(db).list_for_project(UUID(project_id)) == []
        assert client.put(f"/projects/{project_id}/budget", json={
            "currency": "USD", "max_calls": 0, "allow_unknown_cost": True,
        }).status_code == 200
        with Database(path) as db:
            handler = TalkingGenerationJobHandler(
                TalkingProfileRepository(db), AudioAssetRepository(db), AssetRepository(db),
                Importer(), Provider(), tmp_path / "generated", execution_runtime="fixture",
                execution_machine_id="fixture-host",  # type: ignore[arg-type]
            )
            with pytest.raises(JobExecutionError) as budget_block:
                handler(job)
            assert budget_block.value.code == "talking_budget_not_authorized"
            assert ProviderCallRepository(db).list_for_project(UUID(project_id)) == []
        assert client.put(f"/projects/{project_id}/budget", json={
            "currency": "USD", "max_calls": 1, "allow_unknown_cost": True,
        }).status_code == 200
        evidence.write_bytes(b"review evidence changed")
        with Database(path) as db:
            handler = TalkingGenerationJobHandler(
                TalkingProfileRepository(db), AudioAssetRepository(db), AssetRepository(db),
                Importer(), Provider(), tmp_path / "generated", execution_runtime="fixture",
                execution_machine_id="fixture-host",  # type: ignore[arg-type]
            )
            with pytest.raises(JobExecutionError) as stale:
                handler(job)
            assert stale.value.code == "talking_source_admission_stale_or_revoked"
            assert ProviderCallRepository(db).list_for_project(UUID(project_id)) == []
        evidence.write_bytes(b'{"source":"portrait","review":"pass"}')
        revoked = client.post(f"/talking-source-admissions/{admission.id}/revoke", json={
            "reason": "Reviewer withdrew this source decision",
        }, headers={"x-content-os-talking-review-key": "planned-talking-review-key-with-adequate-entropy-0001"})
        assert revoked.status_code == 200, revoked.text
        with Database(path) as db:
            handler = TalkingGenerationJobHandler(
                TalkingProfileRepository(db), AudioAssetRepository(db), AssetRepository(db),
                Importer(), Provider(), tmp_path / "generated", execution_runtime="fixture",
                execution_machine_id="fixture-host",  # type: ignore[arg-type]
            )
            with pytest.raises(JobExecutionError) as withdrawn:
                handler(job)
            assert withdrawn.value.code == "talking_source_admission_stale_or_revoked"
            assert ProviderCallRepository(db).list_for_project(UUID(project_id)) == []


def test_planned_talking_fake_worker_preserves_qa_pending_and_call_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "planned-talking-fake-worker.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, waiting, binding, _ = _ready_talking_dispatch_fixture(client, path, monkeypatch)
        applied = client.get(f"/projects/{project_id}/production-runs/{waiting['id']}").json()
        with Database(path) as db:
            admission = TalkingSourceAdmissionRepository(db, tmp_path).get_for_assessment(UUID(
                applied["talking_source_bindings"][0]["suitability_assessment_id"]
            ))
        assert admission is not None
        dispatched = client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/talking-dispatch", json={
            "scene_plan_id": binding["scene_plan_id"], "source_admission_id": str(admission.id),
        })
        assert dispatched.status_code == 200, dispatched.text
        job_id = UUID(dispatched.json()["talking_source_bindings"][0]["talking_job_id"])

    def fake_slice(master: AudioAsset, plan: object, destination: Path, **kwargs: object) -> AudioAsset:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"fixture audio slice")
        return master.model_copy(update={"source_file": str(destination)})

    monkeypatch.setattr("app.jobs.handlers.extract_talking_audio_slice", fake_slice)

    class Provider:
        provider_name = "fixture-talking"
        model = "fixture-model"
        is_local = True

        def synthesize(self, profile: TalkingProfile, audio: AudioAsset, reference: object, output: Path) -> TalkingSynthesisResult:
            assert audio.source_file.endswith("narration.wav")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"fixture Talking output")
            return TalkingSynthesisResult(output, provider_version="fixture-v1")

    with Database(path) as db:
        class Importer:
            data_root = tmp_path

            def import_path(self, source: Path, authorization_reference: str, *, source_kind: SourceKind, generated_job=None) -> Asset:
                asset = Asset(
                    source_kind=source_kind, source_file=str(source),
                    content_hash=hashlib.sha256(source.read_bytes()).hexdigest(), duration_ms=3_000,
                    width=1080, height=1920, fps=RationalFps(numerator=25, denominator=1),
                    has_audio=True, authorization_reference=authorization_reference,
                    imported_at=datetime.now(timezone.utc),
                )
                AssetRepository(db).create(asset)
                return asset

        handler = TalkingGenerationJobHandler(
            TalkingProfileRepository(db), AudioAssetRepository(db), AssetRepository(db),
            Importer(), Provider(), tmp_path / "generated", ProviderCallLedger(db),
            execution_runtime="fixture", execution_machine_id="fixture-host",  # type: ignore[arg-type]
        )
        completed = JobRunner(
            JobStore(db), {JobType.GENERATE_TALKING: handler}, worker_id="planned-fixture-worker",
            lease_duration=timedelta(minutes=1), max_attempts=1,
            on_completed=lambda finished: ProductionRunService(db, tmp_path).reconcile_completed_talking_job(
                finished.id, qa_worker_ready=True,
            ),
        ).run_once()
        assert completed is not None and completed.id == job_id and completed.status is JobStatus.COMPLETED
        output = next(asset for asset in AssetRepository(db).list() if asset.source_kind is SourceKind.AI_VIDEO)
        assert output.metadata["talking_generation"]["qa_state"] == "pending"
        assert output.metadata["talking_generation"]["planned_context"]["run_id"] == waiting["id"]
        calls = ProviderCallRepository(db).list_for_project(UUID(project_id))
        assert len(calls) == 1 and calls[0].status == "completed"
        run = ProductionRunService(db, tmp_path).get(UUID(project_id), UUID(waiting["id"]))
        assert run.status == "awaiting_talking_dependencies" and run.render_job_id is None
        assert run.talking_qa_dependencies[0].state == "qa_pending"


def _completed_planned_talking_output(
    client: TestClient, path: Path, monkeypatch: pytest.MonkeyPatch, *, target_duration_ms: int = 3000, new_voice: bool = False, policy_version: int = 1,
) -> tuple[str, dict, dict, Path]:
    project_id, waiting, binding, _ = _ready_talking_dispatch_fixture(client, path, monkeypatch, target_duration_ms=target_duration_ms, new_voice=new_voice, policy_version=policy_version)
    with Database(path) as db:
        admission = TalkingSourceAdmissionRepository(db, path.parent).get_for_assessment(UUID(
            client.get(f"/projects/{project_id}/production-runs/{waiting['id']}").json()["talking_source_bindings"][0]["suitability_assessment_id"]
        ))
    assert admission is not None
    dispatched = client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/talking-dispatch", json={
        "scene_plan_id": binding["scene_plan_id"], "source_admission_id": str(admission.id),
    })
    assert dispatched.status_code == 200, dispatched.text
    job_id = UUID(dispatched.json()["talking_source_bindings"][0]["talking_job_id"])
    with Database(path) as db:
        jobs = JobRepository(db)
        job = jobs.get(job_id)
        assert job is not None
        jobs.update(job.model_copy(update={"status": JobStatus.COMPLETED}))
        master = AudioAssetRepository(db).get(UUID(binding["master_audio_id"]))
        assert master is not None
        metadata = dict(master.metadata)
        generation = dict(metadata["voice_generation"])
        generation["qa"] = {
            "copy_coverage": 1.0, "missing_token_count": 0, "duplicate_token_count": 0,
        }
        metadata["voice_generation"] = generation
        AudioAssetRepository(db).update(master.model_copy(update={"metadata": metadata}))
        source = path.parent / "planned-talking-output.mp4"
        source.write_bytes(b"fixture generated output for QA")
        output = Asset(
            source_kind=SourceKind.AI_VIDEO, source_file=source.name,
            content_hash=hashlib.sha256(source.read_bytes()).hexdigest(),
            duration_ms=3_000, width=1080, height=1920,
            fps=RationalFps(numerator=25, denominator=1), has_audio=True,
            authorization_reference=binding["authorization_reference"],
            imported_at=datetime.now(timezone.utc), metadata={"talking_generation": {
                "job_id": str(job.id), "qa_state": "pending",
                "planned_context": job.payload.planned_context.model_dump(mode="json"),
                "talking_profile_id": binding["talking_profile_id"],
                "reference_clip_id": binding["reference_clip_id"],
                "narration_audio_id": binding["master_audio_id"],
                "provider": job.payload.planned_context.expected_provider,
                "model": job.payload.planned_context.expected_model,
                "master_slice_start_ms": binding["master_start_ms"],
                "master_slice_end_ms": binding["master_end_ms"],
                "reference_window_start_ms": binding["reference_start_ms"],
                "reference_window_end_ms": binding["reference_end_ms"],
            }},
        )
        AssetRepository(db).create(output)
    return project_id, waiting, binding, source


def test_planned_talking_qa_handoff_is_idempotent_and_waits_for_u_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "planned-talking-qa.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, waiting, binding, source = _completed_planned_talking_output(client, path, monkeypatch)
        advance_url = f"/projects/{project_id}/production-runs/{waiting['id']}/talking-qa-advance"
        initial = client.get(f"/projects/{project_id}/production-runs/{waiting['id']}").json()
        assert initial["talking_qa_dependencies"][0]["state"] == "output_awaiting_qa"
        with Database(path) as db:
            recovered = ProductionRunService(db, tmp_path).reconcile_completed_talking_jobs_once()
            assert len(recovered) == 1
        advanced = client.post(advance_url, json={"scene_plan_id": binding["scene_plan_id"]})
        assert advanced.status_code == 200, advanced.text
        qa_run = advanced.json()
        assert qa_run["talking_qa_dependencies"][0]["state"] == "qa_pending"
        assert client.post(advance_url, json={"scene_plan_id": binding["scene_plan_id"]}).json() == qa_run
        with Database(path) as db:
            receipt = TalkingSourceAdmissionRepository(db, tmp_path).get_for_assessment(UUID(
                qa_run["talking_source_bindings"][0]["suitability_assessment_id"]
            ))
        assert receipt is not None
        repeated_dispatch = client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/talking-dispatch", json={
            "scene_plan_id": binding["scene_plan_id"], "source_admission_id": str(receipt.id),
        })
        assert repeated_dispatch.status_code == 200 and repeated_dispatch.json() == qa_run
        assert client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/resume").status_code == 409
        with Database(path) as db:
            assert len(JobRepository(db).list()) == 2
            assert ProviderCallRepository(db).list_for_project(UUID(project_id)) == []
    with TestClient(create_app(path)) as client:
        assert client.get(f"/projects/{project_id}/production-runs/{waiting['id']}").json() == qa_run

        class Probe:
            def probe(self, checked: Path) -> ProbeMetadata:
                assert checked == source
                return ProbeMetadata(
                    duration_ms=3_000, width=1080, height=1920,
                    fps=RationalFps(numerator=25, denominator=1), has_audio=True,
                    metadata={"streams": [{"codec_type": "video"}, {"codec_type": "audio"}]},
                )

        with Database(path) as db:
            handler = TalkingQaJobHandler(AssetRepository(db), data_root=tmp_path, probe=Probe())
            completed = JobRunner(
                JobStore(db), {JobType.VERIFY_TALKING: handler}, worker_id="planned-qa-worker",
                lease_duration=timedelta(minutes=1), max_attempts=1,
            ).run_once(allowed_types={JobType.VERIFY_TALKING})
            assert completed is not None and completed.status is JobStatus.COMPLETED
            run = ProductionRunService(db, tmp_path).get(UUID(project_id), UUID(waiting["id"]))
            assert run is not None and run.talking_qa_dependencies[0].state == "qa_verified_awaiting_u_talking"
            assert run.render_job_id is None and run.status == "awaiting_talking_dependencies"
            assert ProviderCallRepository(db).list_for_project(UUID(project_id)) == []
        assert client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/resume").status_code == 409


def test_planned_talking_qa_blocks_changed_bytes_and_persists_technical_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "planned-talking-qa-fail.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, waiting, binding, source = _completed_planned_talking_output(client, path, monkeypatch)
        url = f"/projects/{project_id}/production-runs/{waiting['id']}/talking-qa-advance"
        source.write_bytes(b"changed generated media")
        stale = client.post(url, json={"scene_plan_id": binding["scene_plan_id"]})
        assert stale.status_code == 409 and "talking_output_bytes_changed" in stale.text
        with Database(path) as db:
            assert len(JobRepository(db).list()) == 1
            asset = next(item for item in AssetRepository(db).list() if item.source_kind is SourceKind.AI_VIDEO)
            source.write_bytes(b"fixture generated output for QA")
        advanced = client.post(url, json={"scene_plan_id": binding["scene_plan_id"]})
        assert advanced.status_code == 200, advanced.text

        class BadProbe:
            def probe(self, checked: Path) -> ProbeMetadata:
                return ProbeMetadata(
                    duration_ms=3_500, width=1080, height=1920,
                    fps=RationalFps(numerator=25, denominator=1), has_audio=False,
                    metadata={"streams": [{"codec_type": "video"}]},
                )

        with Database(path) as db:
            failed = JobRunner(
                JobStore(db), {JobType.VERIFY_TALKING: TalkingQaJobHandler(AssetRepository(db), data_root=tmp_path, probe=BadProbe())},
                worker_id="planned-qa-fail-worker", lease_duration=timedelta(minutes=1), max_attempts=1,
            ).run_once(allowed_types={JobType.VERIFY_TALKING})
            assert failed is not None and failed.status is JobStatus.FAILED and failed.error_code == "talking_qa_failed"
            stored = AssetRepository(db).get(asset.id)
            assert stored is not None and stored.metadata["talking_generation"]["qa_state"] == "failed"
            run = ProductionRunService(db, tmp_path).get(UUID(project_id), UUID(waiting["id"]))
            assert run is not None and run.talking_qa_dependencies[0].state == "qa_failed"
            assert ProviderCallRepository(db).list_for_project(UUID(project_id)) == []


def test_planned_talking_qa_worker_rechecks_output_bytes_after_enqueue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "planned-talking-qa-stale.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, waiting, binding, source = _completed_planned_talking_output(client, path, monkeypatch)
        advanced = client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/talking-qa-advance", json={
            "scene_plan_id": binding["scene_plan_id"],
        })
        assert advanced.status_code == 200, advanced.text
        source.write_bytes(b"changed after durable QA enqueue")

        class NoProbe:
            def probe(self, checked: Path) -> ProbeMetadata:
                raise AssertionError("changed bytes must block before ffprobe")

        with Database(path) as db:
            failed = JobRunner(
                JobStore(db), {JobType.VERIFY_TALKING: TalkingQaJobHandler(AssetRepository(db), data_root=tmp_path, probe=NoProbe())},
                worker_id="stale-qa-worker", lease_duration=timedelta(minutes=1), max_attempts=1,
            ).run_once(allowed_types={JobType.VERIFY_TALKING})
            assert failed is not None and failed.status is JobStatus.FAILED
            assert failed.error_code == "talking_qa_admission_changed"
            run = ProductionRunService(db, tmp_path).get(UUID(project_id), UUID(waiting["id"]))
            assert run is not None and run.talking_qa_dependencies[0].state == "qa_failed"
            assert ProviderCallRepository(db).list_for_project(UUID(project_id)) == []


def test_planned_preview_preparation_reuses_reviewed_child_without_new_inference_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "planned-preview.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, waiting, binding, _ = _completed_planned_talking_output(client, path, monkeypatch)
        root = f"/projects/{project_id}/production-runs/{waiting['id']}"
        request = {"scene_plan_id": binding["scene_plan_id"]}
        assert client.post(f"{root}/talking-preview-prepare", json=request).status_code == 409
        advanced = client.post(f"{root}/talking-qa-advance", json=request)
        assert advanced.status_code == 200, advanced.text
        bound = advanced.json()["talking_source_bindings"][0]
        with Database(path) as db:
            jobs = JobRepository(db)
            qa = jobs.get(UUID(bound["talking_qa_job_id"]))
            assert qa is not None
            jobs.update(qa.model_copy(update={"status": JobStatus.COMPLETED}))
            asset = AssetRepository(db).get(UUID(bound["talking_output_asset_id"]))
            assert asset is not None
            generation = dict(asset.metadata["talking_generation"])
            generation.update({
                "qa_state": "verified", "qa": {"job_id": str(qa.id), "automated_verified": True,
                                           "evidence_reference": "fixture:technical-qa"},
                "human_review_state": "approved",
                "human_review": {"approved": True, "evidence_reference": "fixture:child-review"},
            })
            AssetRepository(db).update(asset.model_copy(update={"metadata": {"talking_generation": generation}}))
        assert client.put(f"/projects/{project_id}/budget", json={
            "currency": "USD", "max_calls": 0, "allow_unknown_cost": True,
        }).status_code == 200
        first = client.post(f"{root}/talking-preview-prepare", json=request)
        assert first.status_code == 200, first.text
        added = first.json()["talking_source_bindings"][0]
        assert added["talking_series_id"] and added["talking_preview_job_id"]
        assert first.json()["talking_preview_dependencies"][0]["state"] == "pending"
        assert client.post(f"{root}/talking-preview-prepare", json=request).json() == first.json()
        with Database(path) as db:
            from app.db import TalkingSliceSeriesRepository
            series = TalkingSliceSeriesRepository(db).get(UUID(added["talking_series_id"]))
            assert series is not None and series.planned_origin is not None
            assert series.child_job_ids == [UUID(added["talking_job_id"])]
            preview = JobRepository(db).get(UUID(added["talking_preview_job_id"]))
            assert preview is not None and preview.type is JobType.PREPARE_TALKING_RUN_PREVIEW
            assert ProviderCallRepository(db).list_for_project(UUID(project_id)) == []
            output = AssetRepository(db).get(UUID(added["talking_output_asset_id"]))
            changed_generation = dict(output.metadata["talking_generation"])
            changed_generation["human_review_state"] = "rejected"
            changed = output.model_copy(update={"metadata": {"talking_generation": changed_generation}})
            AssetRepository(db).update(changed)
        assert client.post(f"{root}/talking-preview-prepare", json=request).status_code == 409
        with Database(path) as db:
            AssetRepository(db).update(output)
        assert client.post(f"{root}/talking-preview-prepare", json=request).status_code == 200
        review = client.post(f"/projects/{project_id}/talking-slice-series/{series.id}/continuity-review", json={
            "approved": True, "evidence_reference": "fixture:should-not-approve", "findings": [],
        })
        assert review.status_code == 409
        assert client.post(f"/projects/{project_id}/talking-slice-series/{series.id}/talking-run").status_code == 409
        with Database(path) as db:
            from app.db import TalkingSliceSeriesRepository
            series_repo = TalkingSliceSeriesRepository(db)
            with pytest.raises(ValueError, match="immutable"):
                series_repo.update(series.model_copy(update={"planned_origin": None}))
            db.connection.execute(
                "UPDATE talking_slice_series SET payload = ? WHERE id = ?",
                (series.model_copy(update={"planned_origin": None}).model_dump_json(), str(series.id)),
            )
        assert client.post(f"/projects/{project_id}/talking-slice-series/{series.id}/talking-run").status_code == 409
        assert client.post(f"/projects/{project_id}/talking-slice-series/{series.id}/continuity-review", json={
            "approved": True, "evidence_reference": "fixture:marker-stripped", "findings": [],
        }).status_code == 409
    with TestClient(create_app(path)) as client:
        cancelled = client.post(f"{root}/cancel")
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
        with Database(path) as db:
            assert JobRepository(db).get(UUID(added["talking_preview_job_id"])).status is JobStatus.CANCELLED


@pytest.mark.parametrize("policy_version", [1, 2])
def test_planned_preview_worker_remuxes_real_master_and_persists_nonroutable_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy_version: int,
) -> None:
    path = tmp_path / "planned-preview-real.sqlite"
    ffmpeg = resolve_local_executable("ffmpeg")
    ffprobe = resolve_local_executable("ffprobe")
    with TestClient(create_app(path)) as client:
        project_id, waiting, binding, source = _completed_planned_talking_output(client, path, monkeypatch, policy_version=policy_version)
        still = tmp_path / "synthetic.png"
        def chunk(kind: bytes, data: bytes) -> bytes:
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        pixels = (b"\0" + bytes((0, 0, 255)) * 64) * 112
        still.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 112, 8, 2, 0, 0, 0))
                          + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))
        made = subprocess.run([
            ffmpeg, "-nostdin", "-y", "-loop", "1", "-framerate", "25", "-i", str(still),
            "-t", "3", "-c:v", "libx264", str(source),
        ], capture_output=True, text=True, timeout=60, check=False)
        assert made.returncode == 0, made.stderr[-1000:]
        with Database(path) as db:
            assets = AssetRepository(db)
            old = next(item for item in assets.list() if item.source_kind is SourceKind.AI_VIDEO)
            assert assets.delete(old.id)
            assets.create(old.model_copy(update={
                "content_hash": hashlib.sha256(source.read_bytes()).hexdigest(),
                "width": 64, "height": 112, "has_audio": False,
            }))
        root = f"/projects/{project_id}/production-runs/{waiting['id']}"
        request = {"scene_plan_id": binding["scene_plan_id"]}
        advanced = client.post(f"{root}/talking-qa-advance", json=request)
        assert advanced.status_code == 200, advanced.text
        bound = advanced.json()["talking_source_bindings"][0]
        with Database(path) as db:
            jobs, assets = JobRepository(db), AssetRepository(db)
            qa = jobs.get(UUID(bound["talking_qa_job_id"]))
            jobs.update(qa.model_copy(update={"status": JobStatus.COMPLETED}))
            output = assets.get(UUID(bound["talking_output_asset_id"]))
            generation = dict(output.metadata["talking_generation"])
            generation.update({
                "qa_state": "verified", "qa": {"job_id": str(qa.id), "automated_verified": True,
                                           "evidence_reference": "fixture:technical-qa"},
                "human_review_state": "approved",
                "human_review": {"approved": True, "evidence_reference": "fixture:child-review"},
            })
            if policy_version == 2:
                generation.pop("human_review_state")
                generation.pop("human_review")
            assets.update(output.model_copy(update={"metadata": {"talking_generation": generation}}))
        prepared = client.post(f"{root}/talking-preview-prepare", json=request)
        assert prepared.status_code == 200, prepared.text
        preview_id = UUID(prepared.json()["talking_source_bindings"][0]["talking_preview_job_id"])
    with Database(path) as db:
        handler = TalkingRunPreviewJobHandler(db, tmp_path, ffmpeg_command=ffmpeg, probe=FFProbeAdapter(ffprobe))
        completed = JobRunner(
            JobStore(db), {JobType.PREPARE_TALKING_RUN_PREVIEW: handler},
            worker_id="real-preview-worker", lease_duration=timedelta(minutes=1), max_attempts=1,
        ).run_once(allowed_types={JobType.PREPARE_TALKING_RUN_PREVIEW})
        assert completed is not None and completed.id == preview_id and completed.status is JobStatus.COMPLETED, completed
        candidates = [item for item in AssetRepository(db).list() if "talking_run_preview" in item.metadata]
        assert len(candidates) == 1
        candidate = candidates[0]
        assert candidate.has_audio and abs(candidate.duration_ms - 3_000) <= 250
        assert candidate.metadata["talking_run_preview"]["technical_qa_state"] == "verified"
        assert candidate.metadata["talking_run_preview"].get("review_policy_version", 1) == policy_version
        assert "talking_run" not in candidate.metadata
        assert not ClipRepository(db).list_by_asset(candidate.id)
        assert ProviderCallRepository(db).list_for_project(UUID(project_id)) == []
        dependency = ProductionRunService(db, tmp_path).get(UUID(project_id), UUID(waiting["id"])).talking_preview_dependencies[0]
        assert dependency.state == "completed_review_only" and dependency.output_asset_id == candidate.id
        reference = AssetRepository(db).get(UUID(binding["reference_asset_id"]))
        assert reference is not None
        (tmp_path / reference.source_file).write_bytes(b"changed after preview completion")
        with pytest.raises(JobExecutionError) as stale:
            handler(completed.model_copy(update={"status": JobStatus.RUNNING}))
        assert stale.value.code == "talking_preview_invalid"
        assert len([item for item in AssetRepository(db).list() if "talking_run_preview" in item.metadata]) == 1


def _fixture_reviewable_planned_preview(
    client: TestClient, path: Path, monkeypatch: pytest.MonkeyPatch, *, target_duration_ms: int = 3000, new_voice: bool = False, policy_version: int = 1,
) -> tuple[str, dict, dict, Asset]:
    """Admission fixture: never mistaken for actual media QA or a human judgment."""
    project_id, waiting, binding, _ = _completed_planned_talking_output(client, path, monkeypatch, target_duration_ms=target_duration_ms, new_voice=new_voice, policy_version=policy_version)
    root = f"/projects/{project_id}/production-runs/{waiting['id']}"
    request = {"scene_plan_id": binding["scene_plan_id"]}
    advanced = client.post(f"{root}/talking-qa-advance", json=request)
    assert advanced.status_code == 200, advanced.text
    bound = advanced.json()["talking_source_bindings"][0]
    with Database(path) as db:
        jobs, assets = JobRepository(db), AssetRepository(db)
        qa = jobs.get(UUID(bound["talking_qa_job_id"]))
        jobs.update(qa.model_copy(update={"status": JobStatus.COMPLETED}))
        output = assets.get(UUID(bound["talking_output_asset_id"]))
        generation = dict(output.metadata["talking_generation"])
        generation.update({
            "qa_state": "verified", "qa": {"job_id": str(qa.id), "automated_verified": True,
                                       "evidence_reference": "fixture:technical-qa"},
            "human_review_state": "approved",
            "human_review": {"approved": True, "evidence_reference": "fixture:child-u-talking"},
        })
        if policy_version == 2:
            generation.pop("human_review_state")
            generation.pop("human_review")
        assets.update(output.model_copy(update={"metadata": {"talking_generation": generation}}))
    prepared = client.post(f"{root}/talking-preview-prepare", json=request)
    assert prepared.status_code == 200, prepared.text
    bound = prepared.json()["talking_source_bindings"][0]
    with Database(path) as db:
        from app.db import TalkingSliceSeriesRepository
        jobs = JobRepository(db)
        job = jobs.get(UUID(bound["talking_preview_job_id"]))
        jobs.update(job.model_copy(update={"status": JobStatus.COMPLETED}))
        series = TalkingSliceSeriesRepository(db).get(UUID(bound["talking_series_id"]))
        origin = series.planned_origin
        source = path.parent / "fixture-master-preview.mp4"
        source.write_bytes(b"fixture Master-audio candidate, not real encoded media")
        preview = Asset(
            source_kind=SourceKind.AI_VIDEO, source_file=source.name,
            content_hash=hashlib.sha256(source.read_bytes()).hexdigest(),
            duration_ms=origin.master_end_ms - origin.master_start_ms,
            width=1080, height=1920, fps=RationalFps(numerator=25, denominator=1),
            has_audio=True, authorization_reference=origin.authorization_reference,
            imported_at=datetime.now(timezone.utc), metadata={
                "streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
                "talking_run_preview": {
                    "state": "review_only", "technical_qa_state": "verified", "job_id": str(job.id),
                    "series_id": str(series.id), "origin_sha256": job.payload.origin_sha256,
                    "input_output_sha256": origin.output_sha256, "master_sha256": origin.master_sha256,
                    "master_start_ms": origin.master_start_ms, "master_end_ms": origin.master_end_ms,
                    "duration_ms": origin.master_end_ms - origin.master_start_ms, "has_audio": True,
                    "video_width": 1080, "video_height": 1920,
                },
            },
        )
        if policy_version == 2:
            preview.metadata["talking_run_preview"]["review_policy_version"] = 2
        AssetRepository(db).create(preview)
    return project_id, waiting, bound, preview


def test_planned_exact_preview_review_and_admission_promote_same_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "planned-admission.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, waiting, bound, preview = _fixture_reviewable_planned_preview(client, path, monkeypatch)
        series_id = bound["talking_series_id"]
        root = f"/projects/{project_id}/talking-slice-series/{series_id}"
        run_root = f"/projects/{project_id}/production-runs/{waiting['id']}"
        readiness = client.get(root)
        assert readiness.status_code == 200, readiness.text
        assert readiness.json()["ready_for_human_continuity_review"] is True
        assert readiness.json()["preview_asset_id"] == str(preview.id)
        assert readiness.json()["preview_sha256"] == preview.content_hash
        assert client.post(f"{root}/talking-run").status_code == 409
        assert client.post(f"{run_root}/talking-run-admit", json={"scene_plan_id": bound["scene_plan_id"]}).status_code == 409
        wrong = client.post(f"{root}/continuity-review", json={
            "approved": True, "evidence_reference": "fixture:whole-preview-review", "findings": [],
            "preview_asset_id": str(preview.id), "preview_sha256": "0" * 64,
        })
        assert wrong.status_code == 409
        reviewed = client.post(f"{root}/continuity-review", json={
            "approved": True, "evidence_reference": "fixture:whole-preview-review", "findings": [],
            "preview_asset_id": str(preview.id), "preview_sha256": preview.content_hash,
        })
        assert reviewed.status_code == 201, reviewed.text
        assert reviewed.json()["preview_asset_id"] == str(preview.id)
        assert client.post(f"{root}/continuity-review", json={
            "approved": True, "evidence_reference": "fixture:whole-preview-review", "findings": [],
            "preview_asset_id": str(preview.id), "preview_sha256": preview.content_hash,
        }).json() == reviewed.json()
        admitted = client.post(f"{run_root}/talking-run-admit", json={"scene_plan_id": bound["scene_plan_id"]})
        assert admitted.status_code == 200, admitted.text
        assert admitted.json()["talking_preview_dependencies"][0]["state"] == "admitted"
        run_id = admitted.json()["talking_source_bindings"][0]["talking_run_id"]
        assert run_id
        direct = client.post(f"{root}/talking-run")
        assert direct.status_code == 201 and direct.json()["id"] == run_id
        assert direct.json()["assembled_asset_id"] == str(preview.id)
        assert direct.json()["reviewed_preview_sha256"] == preview.content_hash
        with Database(path) as db:
            from app.db import TalkingRunRepository
            from app.talking.admission import talking_visual_blocker
            run = TalkingRunRepository(db).get(UUID(run_id))
            assert run is not None
            final = AssetRepository(db).get(preview.id)
            clip = ClipRepository(db).get(run.assembled_clip_id)
            assert final.content_hash == preview.content_hash and final.source_file == preview.source_file
            assert len(ClipRepository(db).list_by_asset(preview.id)) == 1
            master = AudioAssetRepository(db).get(UUID(bound["master_audio_id"]))
            copy = " ".join(segment.text for segment in master.transcript_segments)
            assert talking_visual_blocker(
                final, clip, AssetRepository(db), project_id=UUID(project_id), copy=copy,
                master_audio_id=master.id, selected_duration_ms=preview.duration_ms,
            ) is None
            assert ProviderCallRepository(db).list_for_project(UUID(project_id)) == []
            # A missing product binding must fail before router/origin recursion.
            row = db.connection.execute(
                "SELECT talking_source_bindings FROM production_runs WHERE id = ?", (waiting["id"],),
            ).fetchone()
            bindings = json.loads(row["talking_source_bindings"])
            bindings[0]["talking_run_id"] = None
            db.connection.execute(
                "UPDATE production_runs SET talking_source_bindings = ? WHERE id = ?",
                (json.dumps(bindings), waiting["id"]),
            )
            assert talking_visual_blocker(
                final, clip, AssetRepository(db), project_id=UUID(project_id), copy=copy,
            ) == "talking_run_planned_binding_mismatch"


@pytest.mark.parametrize("new_voice", [False, True])
@pytest.mark.parametrize("policy_version", [1, 2])
def test_planned_run_resumes_exact_measured_timeline_and_rechecks_on_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, new_voice: bool, policy_version: int,
) -> None:
    path = tmp_path / "planned-resume.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, waiting, bound, preview = _fixture_reviewable_planned_preview(client, path, monkeypatch, target_duration_ms=1000, new_voice=new_voice, policy_version=policy_version)
        root = f"/projects/{project_id}/talking-slice-series/{bound['talking_series_id']}"
        resume = f"/projects/{project_id}/production-runs/{waiting['id']}/resume"
        assert client.post(resume).status_code == 409
        assert client.post(f"{root}/continuity-review", json={
            "approved": True, "evidence_reference": "fixture:planned-render-review", "findings": [],
            "preview_asset_id": str(preview.id), "preview_sha256": preview.content_hash,
            **({"review_policy_version": 2, "dimensions": dict.fromkeys(
                ("visible_sync", "identity", "artifacts", "source_performance", "continuity", "publishability"), "pass")}
               if policy_version == 2 else {}),
        }).status_code == 201
        admitted = client.post(f"{root}/talking-run")
        assert admitted.status_code == 201, admitted.text
        preflight = client.post(f"/projects/{project_id}/production-preflight", json={})
        assert preflight.status_code == 200
        assert preflight.json()["scenes"][0]["suitability"] == "reviewed_native_planned_run"
        assert preflight.json()["scenes"][0]["burned_in_subtitles"] == "unknown"
        assert preflight.json()["scenes"][0]["subtitle_treatment"] == "none"
        assert client.put(f"/projects/{project_id}/budget", json={
            "currency": "USD", "max_calls": 0, "allow_unknown_cost": True,
        }).status_code == 200  # Completed inference needs no new-call allowance.
        original_create = JobRepository.create
        def fail_render_create(repo, job):
            if job.type is JobType.RENDER:
                raise RuntimeError("fixture render enqueue failure")
            return original_create(repo, job)
        with monkeypatch.context() as patch:
            patch.setattr(JobRepository, "create", fail_render_create)
            with pytest.raises(RuntimeError, match="fixture render enqueue failure"):
                client.post(resume)
        with Database(path) as db:
            assert ProductionRunService(db, tmp_path).get(UUID(project_id), UUID(waiting["id"])).status == "awaiting_talking_dependencies"
            assert not any(item.type is JobType.RENDER for item in JobRepository(db).list())
        resumed = client.post(resume)
        assert resumed.status_code == 200, resumed.text
        result = resumed.json()
        assert result["status"] == "render_pending"
        assert result["preflight_fingerprint"] == waiting["preflight_fingerprint"]
        assert result["waiting_stop_reasons"] == []
        assert client.post(resume).json() == result
        with Database(path) as db:
            job = JobRepository(db).get(UUID(result["render_job_id"]))
            spec = job.payload.video_spec
            assert spec.master_narration.audio_asset_id == UUID(bound["master_audio_id"])
            assert spec.scenes[0].visual.asset_id == preview.id
            assert spec.scenes[0].visual.clip_id == UUID(admitted.json()["assembled_clip_id"])
            assert spec.scenes[0].duration_frames == 90
            assert spec.scenes[0].narration_start_ms == 0 and spec.scenes[0].narration_end_ms == 3000
            assert len(spec.scenes) == 1
            assert len([item for item in JobRepository(db).list() if item.type is JobType.RENDER]) == 1
            assert ProviderCallRepository(db).list_for_project(UUID(project_id)) == []
            # Real file staging with a fake subprocess, not a real rendered video.
            from app.renderer import RemotionRenderer, UnauthorizedVisualError
            renderer_root = tmp_path / "renderer"
            (renderer_root / "src").mkdir(parents=True)
            (renderer_root / "package.json").write_text("{}", encoding="utf-8")
            (renderer_root / "src" / "index.ts").write_text("export {};", encoding="utf-8")
            class StagingRunner:
                calls = 0
                def run(self, argv, *, cwd, timeout_seconds):
                    self.calls += 1
                    props_path = Path(next(value.split("=", 1)[1] for value in argv if value.startswith("--props=")))
                    props = json.loads(props_path.read_text(encoding="utf-8"))
                    source = props["sceneSources"]["hook"]["src"]
                    assert hashlib.sha256((cwd / "public" / source).read_bytes()).hexdigest() == preview.content_hash
                    Path(argv[7]).write_bytes(b"fixture renderer output")
                    return subprocess.CompletedProcess(argv, 0, b"", b"")
            runner = StagingRunner()
            renderer = RemotionRenderer(AssetRepository(db), ClipRepository(db), audios=AudioAssetRepository(db),
                                       renderer_dir=renderer_root, runner=runner)
            renderer.render(spec, tmp_path / "fixture-render.mp4")
            assert runner.calls == 1
            if policy_version == 2:
                concern = client.post(f"{root}/review-concerns", json={
                    "idempotency_key": "late-render-concern", "preview_asset_id": str(preview.id),
                    "preview_sha256": preview.content_hash, "evidence_reference": "fixture:late-inspection",
                    "finding": {"dimension": "artifacts", "reason": "fixture: inspect mouth", "start_ms": 0, "end_ms": 500},
                })
                assert concern.status_code == 201, concern.text
                assert client.post(resume).status_code == 409
                assert client.post(f"/projects/{project_id}/production-preflight", json={}).json()["scenes"][0]["suitability"] != "reviewed_native_planned_run"
                with pytest.raises(UnauthorizedVisualError):
                    renderer.render(spec, tmp_path / "unresolved-concern-render.mp4")
                assert runner.calls == 1
                resolved = client.post(f"{root}/review-concern-answers", json={
                    "preview_asset_id": str(preview.id), "preview_sha256": preview.content_hash,
                    "answers": [{"concern_id": concern.json()["id"], "approved": True,
                                 "evidence_reference": "fixture:local-judgment", "reason": "fixture: inspected interval"}],
                })
                assert resolved.status_code == 200, resolved.text
            if new_voice:
                voice_job = JobRepository(db).get(UUID(result["voice_job_id"]))
                profile = VoiceProfileRepository(db).get(voice_job.payload.voice_profile_id)
                revoked = profile.model_copy(update={"consent": profile.consent.model_copy(update={"confirmed": False})})
                db.connection.execute("UPDATE voice_profiles SET payload = ? WHERE id = ?",
                                      (revoked.model_dump_json(), str(profile.id)))
                assert client.post(resume).status_code == 409
                with pytest.raises(UnauthorizedVisualError):
                    renderer.render(spec, tmp_path / "revoked-voice-render.mp4")
                assert runner.calls == 1
                db.connection.execute("UPDATE voice_profiles SET payload = ? WHERE id = ?",
                                      (profile.model_dump_json(), str(profile.id)))
            original_preview = (tmp_path / preview.source_file).read_bytes()
            (tmp_path / preview.source_file).write_bytes(b"changed before worker consumes")
            with pytest.raises(UnauthorizedVisualError):
                renderer.render(spec, tmp_path / "blocked-render.mp4")
            assert runner.calls == 1
            (tmp_path / preview.source_file).write_bytes(original_preview)
    with TestClient(create_app(path)) as client:
        assert client.post(resume).json() == result
        (tmp_path / preview.source_file).write_bytes(b"changed after render enqueue")
        assert client.post(resume).status_code == 409
        assert client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/cancel").status_code == 200
        assert client.post(resume).status_code == 409


@pytest.mark.parametrize("failure", ["short_visual", "revoked", "revoked_after_enqueue", "draft_changed"])
def test_planned_render_resume_fails_closed_without_new_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    path = tmp_path / f"planned-resume-{failure}.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, waiting, bound, preview = _fixture_reviewable_planned_preview(client, path, monkeypatch)
        if failure == "short_visual":
            with Database(path) as db:
                metadata = dict(preview.metadata)
                report = dict(metadata["talking_run_preview"])
                report["duration_ms"] = 2980
                metadata["talking_run_preview"] = report
                preview = preview.model_copy(update={"duration_ms": 2980, "metadata": metadata})
                AssetRepository(db).update(preview)
        root = f"/projects/{project_id}/talking-slice-series/{bound['talking_series_id']}"
        assert client.post(f"{root}/continuity-review", json={
            "approved": True, "evidence_reference": "fixture:failure-bound-review", "findings": [],
            "preview_asset_id": str(preview.id), "preview_sha256": preview.content_hash,
        }).status_code == 201
        assert client.post(f"{root}/talking-run").status_code == 201
        if failure == "revoked_after_enqueue":
            assert client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/resume").status_code == 200
        if failure in {"revoked", "revoked_after_enqueue"}:
            from app.db import TalkingSliceSeriesRepository
            with Database(path) as db:
                series = TalkingSliceSeriesRepository(db).get(UUID(bound["talking_series_id"]))
            assert client.post(f"/talking-source-admissions/{series.planned_origin.source_admission_id}/revoke", json={
                "reason": "fixture authority revoked",
            }, headers={"x-content-os-talking-review-key": "planned-talking-review-key-with-adequate-entropy-0001"}).status_code == 200
        if failure == "draft_changed":
            draft = client.get(f"/projects/{project_id}/draft").json()
            assert client.put(f"/projects/{project_id}/draft", json={
                "script": draft["script"] + " changed", "topic": "New", "scenes": draft["scenes"],
            }).status_code == 200
        response = client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/resume")
        assert response.status_code == 409, response.text
        if failure == "short_visual":
            assert "planned_talking_visual_timing_insufficient" in response.text
        with Database(path) as db:
            renders = [item for item in JobRepository(db).list() if item.type is JobType.RENDER]
            assert len(renders) == (1 if failure == "revoked_after_enqueue" else 0)
            assert len(ClipRepository(db).list_by_asset(preview.id)) == 1


def test_planned_preview_rejection_and_changed_bytes_cannot_admit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "planned-rejected.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, waiting, bound, preview = _fixture_reviewable_planned_preview(client, path, monkeypatch)
        root = f"/projects/{project_id}/talking-slice-series/{bound['talking_series_id']}"
        review = client.post(f"{root}/continuity-review", json={
            "approved": False, "evidence_reference": "fixture:whole-result-rejected",
            "findings": ["fixture discontinuity"], "preview_asset_id": str(preview.id),
            "preview_sha256": preview.content_hash,
        })
        assert review.status_code == 201, review.text
        assert client.post(f"{root}/talking-run").status_code == 409
        assert client.post(f"{root}/continuity-review", json={
            "approved": True, "evidence_reference": "fixture:whole-result-rejected",
            "preview_asset_id": str(preview.id), "preview_sha256": preview.content_hash,
        }).status_code == 409
        with Database(path) as db:
            assert ClipRepository(db).list_by_asset(preview.id) == []
            assert AssetRepository(db).get(preview.id).metadata.get("talking_run") is None
    changed = tmp_path / "planned-changed.sqlite"
    with TestClient(create_app(changed)) as client:
        project_id, waiting, bound, preview = _fixture_reviewable_planned_preview(client, changed, monkeypatch)
        root = f"/projects/{project_id}/talking-slice-series/{bound['talking_series_id']}"
        assert client.post(f"{root}/continuity-review", json={
            "approved": True, "evidence_reference": "fixture:whole-result-approved",
            "preview_asset_id": str(preview.id), "preview_sha256": preview.content_hash,
        }).status_code == 201
        (tmp_path / preview.source_file).write_bytes(b"preview changed after review")
        assert client.post(f"{root}/talking-run").status_code == 409
        with Database(changed) as db:
            assert ClipRepository(db).list_by_asset(preview.id) == []
            assert AssetRepository(db).get(preview.id).metadata.get("talking_run") is None


def test_planned_admission_rolls_back_and_replay_rechecks_revocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "planned-rollback.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, waiting, bound, preview = _fixture_reviewable_planned_preview(client, path, monkeypatch)
        root = f"/projects/{project_id}/talking-slice-series/{bound['talking_series_id']}"
        request = {"approved": True, "evidence_reference": "fixture:whole-result-approved",
                   "preview_asset_id": str(preview.id), "preview_sha256": preview.content_hash}
        assert client.post(f"{root}/continuity-review", json=request).status_code == 201
        from app.db import TalkingRunRepository
        with monkeypatch.context() as fault:
            def fail_create(self: object, value: object) -> None:
                raise RuntimeError("fixture persistence interruption")
            fault.setattr(TalkingRunRepository, "create", fail_create)
            with pytest.raises(RuntimeError, match="fixture persistence interruption"):
                client.post(f"{root}/talking-run")
        with Database(path) as db:
            assert TalkingRunRepository(db).get_by_series_id(UUID(bound["talking_series_id"])) is None
            assert ClipRepository(db).list_by_asset(preview.id) == []
            assert AssetRepository(db).get(preview.id).metadata.get("talking_run") is None
            run = ProductionRunService(db, tmp_path).get(UUID(project_id), UUID(waiting["id"]))
            assert run.talking_source_bindings[0].talking_run_id is None
        admitted = client.post(f"{root}/talking-run")
        assert admitted.status_code == 201, admitted.text
        run_id = admitted.json()["id"]
        assert client.post(f"{root}/talking-run").json()["id"] == run_id
        with Database(path) as db:
            from app.talking.admission import talking_visual_blocker
            final = AssetRepository(db).get(preview.id)
            clip = ClipRepository(db).get(UUID(admitted.json()["assembled_clip_id"]))
            master = AudioAssetRepository(db).get(UUID(bound["master_audio_id"]))
            copy = " ".join(segment.text for segment in master.transcript_segments)
            assert talking_visual_blocker(final, clip, AssetRepository(db), project_id=UUID(project_id), copy=copy) is None
            from app.db import TalkingSliceSeriesRepository
            series = TalkingSliceSeriesRepository(db).get(UUID(bound["talking_series_id"]))
            admission_id = series.planned_origin.source_admission_id
        revoked = client.post(f"/talking-source-admissions/{admission_id}/revoke", json={
            "reason": "fixture reviewer withdrew source approval",
        }, headers={"x-content-os-talking-review-key": "planned-talking-review-key-with-adequate-entropy-0001"})
        assert revoked.status_code == 200, revoked.text
        assert client.post(f"{root}/talking-run").status_code == 409
        with Database(path) as db:
            final = AssetRepository(db).get(preview.id)
            clip = ClipRepository(db).get(UUID(admitted.json()["assembled_clip_id"]))
            assert talking_visual_blocker(final, clip, AssetRepository(db), project_id=UUID(project_id), copy=copy) is not None


def test_talking_suitability_fixture_partial_and_mismatch_remain_blocked(tmp_path: Path) -> None:
    path = tmp_path / "suitability-blocked.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, waiting, binding_request, source = _talking_source_fixture(client, path)
        bind = client.post(
            f"/projects/{project_id}/production-runs/{waiting['id']}/talking-source-bind",
            json=binding_request,
        )
        binding = bind.json()["talking_source_bindings"][0]
        payload = _talking_assessment_payload(binding, key="fixture-review", evidence_class="fixture")
        fixture = client.post("/talking-reference-suitability-assessments", json=payload)
        assert fixture.status_code == 201, fixture.text
        assert TalkingSourceSuitabilityAssessment.model_validate({
            **payload, "face_head_clearance": "fail",
        }).decision == "unknown"
        apply_url = f"/projects/{project_id}/production-runs/{waiting['id']}/talking-suitability-apply"
        applied = client.post(apply_url, json={
            "scene_plan_id": binding["scene_plan_id"], "assessment_id": fixture.json()["id"],
        })
        assert applied.status_code == 200
        assert applied.json()["talking_source_bindings"][0]["suitability"] == "full_interval_unknown"
        assert "talking_full_interval_suitability_unverified:hook" in applied.json()["waiting_stop_reasons"]
        partial = client.post("/talking-reference-suitability-assessments", json={
            **_talking_assessment_payload(binding, key="partial-review"), "coverage": "partial",
        })
        assert partial.status_code == 201
        conflict = client.post(apply_url, json={
            "scene_plan_id": binding["scene_plan_id"], "assessment_id": partial.json()["id"],
        })
        assert conflict.status_code == 409
        wrong_interval = client.post("/talking-reference-suitability-assessments", json={
            **_talking_assessment_payload(binding, key="wrong-interval"), "end_ms": 3_000,
        })
        assert wrong_interval.status_code == 409
        original = source.read_bytes()
        source.write_bytes(b"changed")
        changed_source = client.post("/talking-reference-suitability-assessments", json={
            **_talking_assessment_payload(binding, key="changed-source"),
        })
        assert changed_source.status_code == 409
        source.write_bytes(original)
        unknown_subtitle = client.post("/talking-reference-suitability-assessments", json={
            **_talking_assessment_payload(binding, key="unknown-subtitle"), "subtitle_clearance": "unknown",
        })
        assert unknown_subtitle.status_code == 201
        with Database(path) as db:
            assert JobRepository(db).list() == []
            assert ProviderCallRepository(db).list_for_project(UUID(project_id)) == []


def test_talking_suitability_negative_is_visible_and_horizontal_claim_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "suitability-negative.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, waiting, binding_request, _ = _talking_source_fixture(client, path)
        bound = client.post(
            f"/projects/{project_id}/production-runs/{waiting['id']}/talking-source-bind",
            json=binding_request,
        ).json()
        binding = bound["talking_source_bindings"][0]
        failed = client.post("/talking-reference-suitability-assessments", json={
            **_talking_assessment_payload(binding, key="negative-face"), "face_head_clearance": "fail",
        })
        assert failed.status_code == 201, failed.text
        applied = client.post(
            f"/projects/{project_id}/production-runs/{waiting['id']}/talking-suitability-apply",
            json={"scene_plan_id": binding["scene_plan_id"], "assessment_id": failed.json()["id"]},
        )
        assert applied.status_code == 200, applied.text
        assert applied.json()["talking_source_bindings"][0]["suitability"] == "unusable"
        assert "talking_reference_unusable:hook" in applied.json()["waiting_stop_reasons"]
        with Database(path) as db:
            asset = AssetRepository(db).get(UUID(binding["reference_asset_id"]))
            AssetRepository(db).update(asset.model_copy(update={"width": 1920, "height": 1080}))
        horizontal = client.post("/talking-reference-suitability-assessments", json={
            **_talking_assessment_payload(binding, key="horizontal-portrait-claim"),
        })
        assert horizontal.status_code == 409
        assert "portrait" in horizontal.json()["detail"]
        with Database(path) as db:
            assert JobRepository(db).list() == []
            assert ProviderCallRepository(db).list_for_project(UUID(project_id)) == []


def test_ready_run_is_atomic_idempotent_and_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "runs.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, plan, source = _ready_fixture(client, path)
        assert plan["status"] == "ready_for_authorized_dispatch", plan["stop_reasons"]
        payload = {"idempotency_key": "user-start-1", "expected_fingerprint": plan["fingerprint"]}
        first = client.post(f"/projects/{project_id}/production-runs", json=payload)
        assert first.status_code == 201, first.text
        run = first.json()
        assert run["status"] == "render_pending"
        assert client.post(f"/projects/{project_id}/production-runs", json=payload).json() == run
        duplicate = client.post(f"/projects/{project_id}/production-runs", json={
            **payload, "idempotency_key": "another-click",
        })
        assert duplicate.status_code == 201 and duplicate.json()["id"] == run["id"]
        job = client.get(f"/jobs/{run['render_job_id']}")
        assert job.status_code == 200 and job.json()["type"] == "render"
        with Database(path) as db:
            assert len([value for value in JobRepository(db).list() if value.project_id == UUID(project_id)]) == 1
            assert ProviderCallRepository(db).list_for_project(UUID(project_id)) == []
        assert source.is_file()
    with TestClient(create_app(path)) as client:
        recovered = client.get(f"/projects/{project_id}/production-runs/{run['id']}")
        assert recovered.status_code == 200 and recovered.json() == run
        cancelled = client.post(f"/projects/{project_id}/production-runs/{run['id']}/cancel")
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
        assert client.post(f"/projects/{project_id}/production-runs/{run['id']}/cancel").json() == cancelled.json()
        assert client.get(f"/jobs/{run['render_job_id']}").json()["status"] == "cancelled"


def test_stale_blocked_or_changed_master_never_enqueues(tmp_path: Path) -> None:
    path = tmp_path / "blocked.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, plan, source = _ready_fixture(client, path)
        assert plan["status"] == "ready_for_authorized_dispatch", plan["stop_reasons"]
        payload = {"idempotency_key": "start", "expected_fingerprint": plan["fingerprint"]}
        styled = client.post(f"/projects/{project_id}/production-runs", json={
            **payload, "style_tokens": {"accent_color": "#ff0000"},
        })
        assert styled.status_code == 409 and styled.json()["detail"] == "stale_production_preflight"
        original_bytes = source.read_bytes()
        source.write_bytes(b"changed after approval")
        changed = client.post(f"/projects/{project_id}/production-runs", json=payload)
        assert changed.status_code == 409
        assert "master_narration_bytes_changed" in changed.json()["detail"]["reasons"]
        source.write_bytes(original_bytes)
        with Database(path) as db:
            repo = AudioAssetRepository(db)
            audio = repo.list()[0]
            repo.update(audio.model_copy(update={"source_file": "../outside-master.wav"}))
        escaped = client.post(f"/projects/{project_id}/production-runs", json=payload)
        assert escaped.status_code == 409
        assert "master_narration_path_escapes_data_root" in escaped.json()["detail"]["reasons"]
        with Database(path) as db:
            assert JobRepository(db).list() == []
            assert ProviderCallRepository(db).list_for_project(UUID(project_id)) == []
    with TestClient(create_app(tmp_path / "no-master.sqlite")) as client:
        other_id, blocked_plan, _ = _ready_fixture(client, tmp_path / "no-master.sqlite", with_master=False)
        blocked = client.post(f"/projects/{other_id}/production-runs", json={
            "idempotency_key": "blocked", "expected_fingerprint": blocked_plan["fingerprint"],
        })
        assert blocked.status_code == 201 and blocked.json()["status"] == "awaiting_approved_master"
        assert blocked.json()["render_job_id"] is None


def test_running_render_cannot_be_cancelled_or_marked_product_approved(tmp_path: Path) -> None:
    path = tmp_path / "running.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, plan, _ = _ready_fixture(client, path)
        run = client.post(f"/projects/{project_id}/production-runs", json={
            "idempotency_key": "start", "expected_fingerprint": plan["fingerprint"],
        }).json()
        with Database(path) as db:
            repo = JobRepository(db)
            job = repo.get(UUID(run["render_job_id"]))
            assert job is not None
            repo.update(job.model_copy(update={"status": JobStatus.RUNNING}))
        assert client.post(f"/projects/{project_id}/production-runs/{run['id']}/cancel").status_code == 409
        assert client.get(f"/projects/{project_id}/production-runs/{run['id']}").json()["status"] == "render_running"
        with Database(path) as db:
            repo = JobRepository(db)
            job = repo.get(UUID(run["render_job_id"]))
            assert job is not None
            repo.update(job.model_copy(update={"status": JobStatus.COMPLETED}))
        assert client.get(f"/projects/{project_id}/production-runs/{run['id']}").json()["status"] == "render_completed_awaiting_review"


def test_waiting_run_survives_restart_and_resumes_only_after_master_admission(tmp_path: Path) -> None:
    path = tmp_path / "await-master.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, plan, _ = _ready_fixture(client, path, with_master=False)
        assert plan["status"] == "blocked"
        started = client.post(f"/projects/{project_id}/production-runs", json={
            "idempotency_key": "await-master", "expected_fingerprint": plan["fingerprint"],
        })
        assert started.status_code == 201, started.text
        waiting = started.json()
        assert waiting["status"] == "awaiting_approved_master" and waiting["render_job_id"] is None
        early = client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/resume")
        assert early.status_code == 409
        with Database(path) as db:
            assert JobRepository(db).list() == []
            assert ProviderCallRepository(db).list_for_project(UUID(project_id)) == []
    with TestClient(create_app(path)) as client:
        assert client.get(f"/projects/{project_id}/production-runs/{waiting['id']}").json() == waiting
        _admit_master(path, project_id, "A fresh creator point.")
        budget = client.put(f"/projects/{project_id}/budget", json={
            "currency": "USD", "max_calls": 1, "allow_unknown_cost": False,
        })
        assert budget.status_code == 200, budget.text
        blocked = client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/resume")
        assert blocked.status_code == 409
        assert client.get(f"/projects/{project_id}/production-runs/{waiting['id']}").json() == waiting
        budget = client.put(f"/projects/{project_id}/budget", json={
            "currency": "USD", "max_calls": 1, "allow_unknown_cost": True,
        })
        assert budget.status_code == 200, budget.text
        resumed = client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/resume")
        assert resumed.status_code == 200, resumed.text
        running = resumed.json()
        assert running["id"] == waiting["id"] and running["status"] == "render_pending"
        assert running["render_job_id"] is not None
        assert client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/resume").json() == running
        with Database(path) as db:
            assert len(JobRepository(db).list()) == 1
            assert ProviderCallRepository(db).list_for_project(UUID(project_id)) == []


def test_waiting_run_rejects_changed_draft_and_cancelled_resume(tmp_path: Path) -> None:
    path = tmp_path / "await-stale.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, plan, _ = _ready_fixture(client, path, with_master=False)
        waiting = client.post(f"/projects/{project_id}/production-runs", json={
            "idempotency_key": "await-master", "expected_fingerprint": plan["fingerprint"],
        }).json()
        changed = client.put(f"/projects/{project_id}/draft", json={
            "script": "Changed text.", "topic": "New",
        })
        assert changed.status_code == 200, changed.text
        _admit_master(path, project_id, "A fresh creator point.")
        stale = client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/resume")
        assert stale.status_code == 409 and "draft changed" in stale.json()["detail"]
        cancelled = client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/cancel")
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
        assert client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/resume").status_code == 409
        with Database(path) as db:
            assert JobRepository(db).list() == []


def test_waiting_lane_does_not_bypass_budget_authorization(tmp_path: Path) -> None:
    path = tmp_path / "budget-blocked.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, _, _ = _ready_fixture(client, path, with_master=False)
        budget = client.put(f"/projects/{project_id}/budget", json={
            "currency": "USD", "max_calls": 1, "allow_unknown_cost": False,
        })
        assert budget.status_code == 200, budget.text
        preflight = client.post(f"/projects/{project_id}/production-preflight", json={}).json()
        assert preflight["status"] == "blocked"
        assert any("budget" in reason or "cost" in reason for reason in preflight["stop_reasons"])
        started = client.post(f"/projects/{project_id}/production-runs", json={
            "idempotency_key": "budget-blocked", "expected_fingerprint": preflight["fingerprint"],
        })
        assert started.status_code == 409
        with Database(path) as db:
            assert JobRepository(db).list() == []
            assert ProviderCallRepository(db).list_for_project(UUID(project_id)) == []


def test_guarded_voice_dispatch_is_durable_and_still_requires_exact_u_voice(tmp_path: Path) -> None:
    path = tmp_path / "voice-dispatch.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, _, _ = _ready_fixture(client, path, with_master=False)
        profile_id, capability_id = _voice_dispatch_evidence(path)
        plan = client.post(f"/projects/{project_id}/production-preflight", json={}).json()
        waiting = client.post(f"/projects/{project_id}/production-runs", json={
            "idempotency_key": "voice-run", "expected_fingerprint": plan["fingerprint"],
        }).json()
        payload = {
            "voice_profile_id": profile_id, "capability_profile_id": capability_id,
            "authorization_reference": "fixture:explicit-dispatch",
        }
        dispatched = client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/voice-dispatch", json=payload)
        assert dispatched.status_code == 200, dispatched.text
        voice_run = dispatched.json()
        assert voice_run["status"] == "voice_pending" and voice_run["voice_job_id"]
        assert voice_run["render_job_id"] is None
        assert client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/voice-dispatch", json=payload).json() == voice_run
        assert client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/resume").status_code == 409
        with Database(path) as db:
            assert len(JobRepository(db).list()) == 1
            assert ProviderCallRepository(db).list_for_project(UUID(project_id)) == []
    with TestClient(create_app(path)) as client:
        assert client.get(f"/projects/{project_id}/production-runs/{waiting['id']}").json() == voice_run
        with Database(path) as db:
            repo = JobRepository(db)
            job = repo.get(UUID(voice_run["voice_job_id"]))
            assert job is not None and job.payload.expected_model == "fixture-model"
            repo.update(job.model_copy(update={"status": JobStatus.COMPLETED}))
        assert client.get(f"/projects/{project_id}/production-runs/{waiting['id']}").json()["status"] == "voice_completed_awaiting_qa"
        assert client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/resume").status_code == 409
        _admit_master(path, project_id, "A fresh creator point.", job_id=voice_run["voice_job_id"])
        resumed = client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/resume")
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["status"] == "render_pending"
        assert resumed.json()["voice_job_id"] == voice_run["voice_job_id"]
        with Database(path) as db:
            assert len(JobRepository(db).list()) == 2
            assert ProviderCallRepository(db).list_for_project(UUID(project_id)) == []


def test_voice_dispatch_rejects_unknown_license_without_creating_job(tmp_path: Path) -> None:
    path = tmp_path / "voice-license.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, _, _ = _ready_fixture(client, path, with_master=False)
        profile_id, capability_id = _voice_dispatch_evidence(path, license_status="unknown")
        plan = client.post(f"/projects/{project_id}/production-preflight", json={}).json()
        waiting = client.post(f"/projects/{project_id}/production-runs", json={
            "idempotency_key": "voice-run", "expected_fingerprint": plan["fingerprint"],
        }).json()
        denied = client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/voice-dispatch", json={
            "voice_profile_id": profile_id, "capability_profile_id": capability_id,
            "authorization_reference": "fixture:explicit-dispatch",
        })
        assert denied.status_code == 409
        assert "voice_provider_license_scope_unverified" in denied.json()["detail"]["reasons"]
        with Database(path) as db:
            repo = ProviderMachineCapabilityProfileRepository(db)
            capability = repo.get(UUID(capability_id))
            assert capability is not None
            repo.save(capability.model_copy(update={"quality_status": "unknown"}))
        capability_denied = client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/voice-dispatch", json={
            "voice_profile_id": profile_id, "capability_profile_id": capability_id,
            "authorization_reference": "fixture:explicit-dispatch",
        })
        assert capability_denied.status_code == 409
        assert "exact_voice_capability_not_verified" in capability_denied.json()["detail"]["reasons"]
        with Database(path) as db:
            assert JobRepository(db).list() == []
            assert ProviderCallRepository(db).list_for_project(UUID(project_id)) == []


def test_voice_dispatch_rejects_stale_draft_and_pending_voice_can_cancel(tmp_path: Path) -> None:
    path = tmp_path / "voice-stale.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, _, _ = _ready_fixture(client, path, with_master=False)
        profile_id, capability_id = _voice_dispatch_evidence(path)
        plan = client.post(f"/projects/{project_id}/production-preflight", json={}).json()
        waiting = client.post(f"/projects/{project_id}/production-runs", json={
            "idempotency_key": "stale-run", "expected_fingerprint": plan["fingerprint"],
        }).json()
        payload = {
            "voice_profile_id": profile_id, "capability_profile_id": capability_id,
            "authorization_reference": "fixture:explicit-dispatch",
        }
        saved_draft = client.get(f"/projects/{project_id}/draft").json()
        scenes = saved_draft["scenes"]
        scenes[0]["voice_text"] = "Changed copy."
        changed = client.put(f"/projects/{project_id}/draft", json={
            "script": "Changed copy.", "topic": "New", "scenes": scenes,
        })
        assert changed.status_code == 200, changed.text
        stale = client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/voice-dispatch", json=payload)
        assert stale.status_code == 409 and "draft changed" in stale.json()["detail"]
        with Database(path) as db:
            assert JobRepository(db).list() == []
        other_project_id, _, _ = _ready_fixture(client, path, with_master=False)
        fresh_plan = client.post(f"/projects/{other_project_id}/production-preflight", json={}).json()
        fresh = client.post(f"/projects/{other_project_id}/production-runs", json={
            "idempotency_key": "fresh-run", "expected_fingerprint": fresh_plan["fingerprint"],
        }).json()
        dispatched = client.post(f"/projects/{other_project_id}/production-runs/{fresh['id']}/voice-dispatch", json=payload)
        assert dispatched.status_code == 200, dispatched.text
        cancelled = client.post(f"/projects/{other_project_id}/production-runs/{fresh['id']}/cancel")
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
        assert client.get(f"/jobs/{dispatched.json()['voice_job_id']}").json()["status"] == "cancelled"
        assert client.post(f"/projects/{other_project_id}/production-runs/{fresh['id']}/resume").status_code == 409


def test_voice_qa_advance_binds_exact_take_and_waits_for_u_voice(tmp_path: Path) -> None:
    path = tmp_path / "voice-qa-advance.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, _, _ = _ready_fixture(client, path, with_master=False)
        profile_id, capability_id = _voice_dispatch_evidence(path)
        plan = client.post(f"/projects/{project_id}/production-preflight", json={}).json()
        waiting = client.post(f"/projects/{project_id}/production-runs", json={
            "idempotency_key": "qa-run", "expected_fingerprint": plan["fingerprint"],
        }).json()
        voice_run = client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/voice-dispatch", json={
            "voice_profile_id": profile_id, "capability_profile_id": capability_id,
            "authorization_reference": "fixture:explicit-dispatch",
        }).json()
        early = client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/voice-qa-advance")
        assert early.status_code == 409
        with Database(path) as db:
            jobs = JobRepository(db)
            voice_job = jobs.get(UUID(voice_run["voice_job_id"]))
            assert voice_job is not None
            jobs.update(voice_job.model_copy(update={"status": JobStatus.COMPLETED}))
        audio_id = _generated_voice_output(path, project_id, voice_run["voice_job_id"], "A fresh creator point.")
        advanced = client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/voice-qa-advance")
        assert advanced.status_code == 200, advanced.text
        qa_run = advanced.json()
        assert qa_run["status"] == "voice_qa_pending" and qa_run["voice_audio_id"] == str(audio_id)
        assert qa_run["voice_qa_job_id"] is not None
        assert client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/voice-qa-advance").json() == qa_run
        assert client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/resume").status_code == 409
        with Database(path) as db:
            assert len(JobRepository(db).list()) == 2
            assert ProviderCallRepository(db).list_for_project(UUID(project_id)) == []
    with TestClient(create_app(path)) as client:
        assert client.get(f"/projects/{project_id}/production-runs/{waiting['id']}").json() == qa_run
        with Database(path) as db:
            jobs = JobRepository(db)
            qa_job = jobs.get(UUID(qa_run["voice_qa_job_id"]))
            assert qa_job is not None and qa_job.payload.narration_audio_id == audio_id
            jobs.update(qa_job.model_copy(update={"status": JobStatus.COMPLETED}))
            audios = AudioAssetRepository(db)
            audio = audios.get(audio_id)
            assert audio is not None
            metadata = dict(audio.metadata)
            metadata["voice_generation"] = {**metadata["voice_generation"], "qa_state": "verified"}
            verified = audio.model_copy(update={
                "metadata": metadata, "transcript_source": "fixture-asr",
                "transcript_segments": [TranscriptSegment(start_ms=0, end_ms=3_000, text="A fresh creator point.")],
            })
            audios.update(verified)
        assert client.get(f"/projects/{project_id}/production-runs/{waiting['id']}").json()["status"] == "awaiting_u_voice_review"
        assert client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/resume").status_code == 409
        with Database(path) as db:
            audios = AudioAssetRepository(db)
            verified = audios.get(audio_id)
            assert verified is not None
            reviewed = apply_voice_human_review(verified, VoiceHumanReview(
                approved=True, evidence_reference="fixture:U-Voice", findings=["Fixture approval"],
                likeness="pass", naturalness="pass", emphasis="pass", pace="pass", pauses="pass", rhythm="pass",
                reviewed_at=datetime.now(timezone.utc),
            ))
            audios.update(reviewed)
        assert client.get(f"/projects/{project_id}/production-runs/{waiting['id']}").json()["status"] == "approved_master_ready_to_resume"
        resumed = client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/resume")
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["status"] == "render_pending"
        assert resumed.json()["voice_audio_id"] == str(audio_id)
        with Database(path) as db:
            assert len(JobRepository(db).list()) == 3
            assert ProviderCallRepository(db).list_for_project(UUID(project_id)) == []


def test_voice_qa_advance_rejects_missing_or_changed_output(tmp_path: Path) -> None:
    path = tmp_path / "voice-qa-bytes.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, _, _ = _ready_fixture(client, path, with_master=False)
        profile_id, capability_id = _voice_dispatch_evidence(path)
        plan = client.post(f"/projects/{project_id}/production-preflight", json={}).json()
        waiting = client.post(f"/projects/{project_id}/production-runs", json={
            "idempotency_key": "qa-bytes", "expected_fingerprint": plan["fingerprint"],
        }).json()
        run = client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/voice-dispatch", json={
            "voice_profile_id": profile_id, "capability_profile_id": capability_id,
            "authorization_reference": "fixture:explicit-dispatch",
        }).json()
        with Database(path) as db:
            jobs = JobRepository(db)
            job = jobs.get(UUID(run["voice_job_id"]))
            assert job is not None
            jobs.update(job.model_copy(update={"status": JobStatus.COMPLETED}))
        missing = client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/voice-qa-advance")
        assert missing.status_code == 409
        assert "voice_output_missing_or_ambiguous" in missing.json()["detail"]["reasons"]
        _generated_voice_output(path, project_id, run["voice_job_id"], "A fresh creator point.")
        source = path.parent / "generated-take.wav"
        source.write_bytes(b"changed after Voice import")
        changed = client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/voice-qa-advance")
        assert changed.status_code == 409
        assert "master_narration_bytes_changed" in changed.json()["detail"]["reasons"]
        with Database(path) as db:
            assert len(JobRepository(db).list()) == 1
            assert ProviderCallRepository(db).list_for_project(UUID(project_id)) == []


def test_worker_completion_callback_advances_voice_once_after_durable_completion(tmp_path: Path) -> None:
    path = tmp_path / "voice-callback.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, _, _ = _ready_fixture(client, path, with_master=False)
        profile_id, capability_id = _voice_dispatch_evidence(path)
        plan = client.post(f"/projects/{project_id}/production-preflight", json={}).json()
        waiting = client.post(f"/projects/{project_id}/production-runs", json={
            "idempotency_key": "callback-run", "expected_fingerprint": plan["fingerprint"],
        }).json()
        run = client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/voice-dispatch", json={
            "voice_profile_id": profile_id, "capability_profile_id": capability_id,
            "authorization_reference": "fixture:explicit-dispatch",
        }).json()
        callback_statuses: list[JobStatus] = []
        with Database(path) as db:
            service = ProductionRunService(db, tmp_path)

            def fake_voice(job) -> None:
                _generated_voice_output(path, project_id, str(job.id), job.payload.text)

            def after_complete(job) -> None:
                persisted = JobRepository(db).get(job.id)
                assert persisted is not None
                callback_statuses.append(persisted.status)
                service.reconcile_completed_voice_job(job.id)

            runner = JobRunner(
                JobStore(db), {JobType.GENERATE_VOICE: fake_voice},
                worker_id="fixture-worker", lease_duration=timedelta(minutes=1), max_attempts=1,
                on_completed=after_complete,
            )
            completed = runner.run_once()
            assert completed is not None and completed.status is JobStatus.COMPLETED
            assert callback_statuses == [JobStatus.COMPLETED]
            service.reconcile_completed_voice_job(UUID(run["voice_job_id"]))
            assert service.reconcile_completed_voice_jobs_once() == ()
            assert len(JobRepository(db).list()) == 2
            assert ProviderCallRepository(db).list_for_project(UUID(project_id)) == []
        recovered = client.get(f"/projects/{project_id}/production-runs/{waiting['id']}").json()
        assert recovered["status"] == "voice_qa_pending"
        assert recovered["voice_qa_auto_stop_reasons"] == []


def test_worker_startup_reconciles_crash_gap_and_persists_blocked_retry(tmp_path: Path) -> None:
    path = tmp_path / "voice-restart.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, _, _ = _ready_fixture(client, path, with_master=False)
        profile_id, capability_id = _voice_dispatch_evidence(path)
        plan = client.post(f"/projects/{project_id}/production-preflight", json={}).json()
        waiting = client.post(f"/projects/{project_id}/production-runs", json={
            "idempotency_key": "restart-run", "expected_fingerprint": plan["fingerprint"],
        }).json()
        run = client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/voice-dispatch", json={
            "voice_profile_id": profile_id, "capability_profile_id": capability_id,
            "authorization_reference": "fixture:explicit-dispatch",
        }).json()
        with Database(path) as db:
            runner = JobRunner(
                JobStore(db), {JobType.GENERATE_VOICE: lambda job: None},
                worker_id="fixture-worker", lease_duration=timedelta(minutes=1), max_attempts=1,
            )
            completed = runner.run_once()
            assert completed is not None and completed.status is JobStatus.COMPLETED
            assert len(JobRepository(db).list()) == 1
    with Database(path) as db:
        service = ProductionRunService(db, tmp_path)
        unready = service.reconcile_completed_voice_jobs_once(qa_worker_ready=False)
        assert len(unready) == 1
        assert unready[0].voice_qa_auto_stop_reasons == ("voice_qa_worker_not_configured",)
        assert len(JobRepository(db).list()) == 1
        reconciled = service.reconcile_completed_voice_jobs_once()
        assert len(reconciled) == 1
        assert reconciled[0].voice_qa_auto_stop_reasons == ("voice_output_missing_or_ambiguous",)
        assert len(JobRepository(db).list()) == 1
    with TestClient(create_app(path)) as client:
        blocked = client.get(f"/projects/{project_id}/production-runs/{waiting['id']}").json()
        assert blocked["status"] == "voice_completed_awaiting_qa"
        assert blocked["voice_qa_auto_stop_reasons"] == ["voice_output_missing_or_ambiguous"]
        _generated_voice_output(path, project_id, run["voice_job_id"], "A fresh creator point.")
        budget = client.put(f"/projects/{project_id}/budget", json={
            "currency": "USD", "max_calls": 0, "allow_unknown_cost": True,
        })
        assert budget.status_code == 200, budget.text
        with Database(path) as db:
            stopped = ProductionRunService(db, tmp_path).reconcile_completed_voice_jobs_once()
            assert len(stopped) == 1
            assert "budget_call_limit" in stopped[0].voice_qa_auto_stop_reasons
            assert len(JobRepository(db).list()) == 1
        budget = client.put(f"/projects/{project_id}/budget", json={
            "currency": "USD", "max_calls": 1, "allow_unknown_cost": True,
        })
        assert budget.status_code == 200, budget.text
        retried = client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/voice-qa-advance")
        assert retried.status_code == 200, retried.text
        assert retried.json()["status"] == "voice_qa_pending"
        assert retried.json()["voice_qa_auto_stop_reasons"] == []
        with Database(path) as db:
            assert len(JobRepository(db).list()) == 2
            assert ProviderCallRepository(db).list_for_project(UUID(project_id)) == []
