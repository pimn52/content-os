"""Normal API/owned runner fixtures; no real model, vendor or tool launches."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import struct
import subprocess
from types import SimpleNamespace
from uuid import UUID, uuid4
import wave

import pytest
from fastapi.testclient import TestClient

from app.db import BudgetPolicyRepository, JobRepository, ProviderCallRepository, ProviderMachineCapabilityProfileRepository
from app.domain.models import BudgetPolicy, JobType
from app.main import create_app
from app.jobs.runner import JobRunner
from app.jobs.store import JobStore
from app.jobs.voice_draft_handler import MaintainedVoiceDraftHandler
from app.media.ffprobe import FFProbeAdapter, AudioProbeMetadata
from app.worker_cli import build_runner, parse_config
from app.voice_draft import VoiceDraftService
from app.execution_admission import UseAdmissionError
from app.execution_scope import ExecutionScopeService, ExecutionUseBinding, ExecutionUseScope
from test_maintained_voice_job import bound_job, setup, child_case, candidate


def body(c):
    return {"idempotency_key": "normal-draft", "voice_profile_id": str(c.voice.id),
        "text": c.job.payload.text, "authorization_reference": "synthetic-consent", "language": "zh",
        "reference_window": c.job.payload.reference_window.model_dump(mode="json"),
        "purpose": "internal_evaluation", "use_admission_ids": [str(c.receipt)],
        "execution_capability_profile_id": str(c.job.payload.execution_capability_profile_id)}


def budget(c):
    BudgetPolicyRepository(c.db).save(BudgetPolicy(project_id=c.job.project_id, max_calls=2,
        allow_unknown_cost=True, updated_at=datetime.now(timezone.utc)))


def test_normal_enqueue_is_atomic_idempotent_scoped_and_reloadable(bound_job):
    c = bound_job; budget(c)
    with TestClient(create_app(c.db.path)) as client:
        url = f"/projects/{c.job.project_id}/voice-jobs"
        response = client.post(url, json=body(c))
        assert response.status_code == 201, response.text
        job_id = response.json()["id"]
        assert client.post(url, json=body(c)).json()["id"] == job_id
        changed = {**body(c), "text": "changed copy"}
        assert client.post(url, json=changed).status_code == 409
        status = client.get(f"/projects/{c.job.project_id}/voice-draft-jobs/{job_id}").json()
        assert status["policy"] == "voice-draft-v1" and status["state"] == "pending"
        assert status["outputs"] == [] and status["production_authorized"] is False
        row = c.db.connection.execute("SELECT * FROM voice_draft_jobs WHERE job_id=?", (job_id,)).fetchone()
        assert row and c.db.connection.execute("SELECT 1 FROM execution_use_scopes WHERE subject_id=?", (job_id,)).fetchone()
    assert c.calls == []


def test_saved_drafts_are_bounded_project_local_without_media_authority(bound_job):
    from uuid import uuid4
    c = bound_job; budget(c)
    with TestClient(create_app(c.db.path)) as client:
        project = c.job.project_id
        for index in range(52):
            response = client.post(f"/projects/{project}/voice-jobs", json={**body(c), "idempotency_key": f"saved-{index}"})
            assert response.status_code == 201, response.text
        url = f"/projects/{project}/voice-draft-jobs"
        values = client.get(url).json()
        assert len(values) == 50
        assert all(value["project_id"] == str(project) and value["policy"] == "voice-draft-v1" for value in values)
        assert str(c.job.id) not in {value["job_id"] for value in values}
        assert values == sorted(values, key=lambda value: (value["created_at"], value["job_id"]), reverse=True)
        assert set(values[0]) == {"job_id", "project_id", "policy", "job_status", "created_at", "text_preview"}
        assert client.get(f"/projects/{uuid4()}/voice-draft-jobs").status_code == 404
        assert VoiceDraftService(c.db, c.root).list_saved(uuid4()) == []
        from app.db import ProjectRepository
        other = ProjectRepository(c.db).get(project).model_copy(update={"id": uuid4()})
        ProjectRepository(c.db).create(other)
        c.db.connection.execute("UPDATE voice_draft_jobs SET project_id=? WHERE job_id=?", (str(other.id), values[0]["job_id"]))
        assert values[0]["job_id"] not in {value["job_id"] for value in client.get(url).json()}
    assert c.calls == []


@pytest.mark.parametrize("change", ["budget", "receipt", "capability", "reference", "implicit", "command"])
def test_invalid_normal_request_creates_no_partial_job_or_call(bound_job, change):
    c = bound_job
    if change != "budget": budget(c)
    else: c.db.connection.execute("DELETE FROM budget_policies")
    payload = body(c)
    if change == "receipt": payload["use_admission_ids"] = []
    if change == "capability": payload["execution_capability_profile_id"] = None
    if change == "reference": payload["reference_window"]["clip_id"] = str(uuid4())
    if change == "implicit": payload.pop("purpose")
    if change == "command": payload["command"] = ["caller-program"]
    with TestClient(create_app(c.db.path)) as client:
        response = client.post(f"/projects/{c.job.project_id}/voice-jobs", json=payload)
        assert response.status_code in (409, 422), response.text
    assert c.db.connection.execute("SELECT COUNT(*) FROM voice_draft_jobs").fetchone()[0] == 0
    assert c.calls == [] and ProviderCallRepository(c.db).list_for_project(c.job.project_id) == []


def test_arbitrary_handler_cannot_dispatch_even_a_normal_draft(bound_job):
    c = bound_job; budget(c)
    with TestClient(create_app(c.db.path)) as client:
        response = client.post(f"/projects/{c.job.project_id}/voice-jobs", json=body(c))
        assert response.status_code == 201
    calls = []
    runner = JobRunner(JobStore(c.db), {JobType.GENERATE_VOICE: lambda job: calls.append(job.id)},
        worker_id="fixture", lease_duration=timedelta(seconds=30), max_attempts=1)
    result = runner.run_once()
    assert result.error_code == "evaluation_execution_not_integrated"
    assert calls == [] and c.calls == []


@pytest.mark.parametrize("change", ["marker", "scope", "receipt", "source", "capability"])
def test_fresh_job_status_retains_history_but_denies_changed_authority(bound_job, change):
    c = bound_job; budget(c)
    with TestClient(create_app(c.db.path)) as client:
        response = client.post(f"/projects/{c.job.project_id}/voice-jobs", json=body(c))
        assert response.status_code == 201, response.text
        job_id = response.json()["id"]
        if change == "marker": c.db.connection.execute("DELETE FROM voice_draft_jobs WHERE job_id=?", (job_id,))
        if change == "scope": c.db.connection.execute("DELETE FROM execution_use_scopes WHERE subject_id=?", (job_id,))
        if change == "receipt":
            row = c.db.connection.execute("SELECT payload FROM provider_use_admissions WHERE id=?", (str(c.receipt),)).fetchone()
            receipt = json.loads(row[0]); receipt.update(revoked_at=datetime.now(timezone.utc).isoformat(), revoked_by="fixture", revocation_reason="fixture")
            c.db.connection.execute("UPDATE provider_use_admissions SET payload=? WHERE id=?", (json.dumps(receipt), str(c.receipt)))
        if change == "source": Path(c.asset.source_file).write_bytes(b"changed synthetic reference")
        if change == "capability":
            repo = ProviderMachineCapabilityProfileRepository(c.db)
            profile = repo.get(c.job.payload.execution_capability_profile_id)
            repo.save(profile.model_copy(update={"quality_status": "unknown"}))
        status = client.get(f"/projects/{c.job.project_id}/voice-draft-jobs/{job_id}")
        assert status.status_code == 200 and status.json()["state"] == "blocked"
        assert status.json()["reasons"] and c.calls == []


def test_scope1_and_other_execution_schema_cannot_use_draft_lane(bound_job):
    c = bound_job; budget(c)
    service = VoiceDraftService(c.db, c.root)
    binding = c.scope.bindings[0]
    old = ExecutionUseBinding(**{key: getattr(binding, key) for key in ("admission_id", "capability_profile_id", "review_sha256", "configuration_sha256", "identity")})
    scope1 = ExecutionUseScope(project_id=c.job.project_id, purpose="internal_evaluation", bindings=(old,))
    with pytest.raises(UseAdmissionError, match="binding_required"): service._inputs(c.job.payload, scope1)
    from test_execution_attestation import specification
    capability = ProviderMachineCapabilityProfileRepository(c.db).get(c.job.payload.execution_capability_profile_id)
    mixed = c.scope.model_copy(update={"bindings": (binding.model_copy(update={"execution_specification": specification(capability)}),)})
    with pytest.raises(UseAdmissionError, match="execution_unsupported"): service._inputs(c.job.payload, mixed)
    assert c.calls == []


def test_scope_write_failure_rolls_back_normal_job(bound_job, monkeypatch):
    c = bound_job; budget(c)
    before = len(JobRepository(c.db).list())
    def fail(*args, **kwargs): raise UseAdmissionError("synthetic_pin_failure")
    monkeypatch.setattr(ExecutionScopeService, "pin", fail)
    with TestClient(create_app(c.db.path)) as client:
        assert client.post(f"/projects/{c.job.project_id}/voice-jobs", json=body(c)).status_code == 409
    assert len(JobRepository(c.db).list()) == before
    assert c.db.connection.execute("SELECT COUNT(*) FROM voice_draft_jobs").fetchone()[0] == 0
    assert c.calls == []


def test_normal_factory_runner_output_pending_audition_and_failure_recovery(bound_job, monkeypatch):
    c = bound_job; budget(c)
    with TestClient(create_app(c.db.path)) as client:
        response = client.post(f"/projects/{c.job.project_id}/voice-jobs", json=body(c))
        assert response.status_code == 201, response.text
        job_id = UUID(response.json()["id"])
        c.options["inputs"] = c.options["inputs"].model_copy(update={"output_path": str(c.root / "generated" / "voice" / f"{job_id}-attempt-1.wav")})
        tool = c.root / "fixture-ffmpeg.exe"; tool.write_bytes(b"synthetic FFmpeg substitute")
        def run(command, **kwargs):
            with wave.open(command[-1], "wb") as audio:
                audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                audio.writeframes(struct.pack("<h", 1500) * 72000)
            return SimpleNamespace(returncode=0)
        monkeypatch.setattr(subprocess, "run", run)
        monkeypatch.setattr(FFProbeAdapter, "probe_audio", lambda self, path: AudioProbeMetadata(1, 24000, 1, None, {}))
        options = {key: value for key, value in c.options.items() if key not in ("data_root", "staging_parent", "inputs", "operator_key")}
        config_file = c.root / "worker-config.json"
        config_file.write_text(json.dumps({"config_version": 1, "runtime_options": options},
            default=lambda value: value.model_dump(mode="json") if hasattr(value, "model_dump") else str(value)), encoding="utf-8")
        monkeypatch.setenv("CONTENT_OS_VOICE_PROVIDER", "omnivoice-maintained")
        monkeypatch.setenv("CONTENT_OS_MAINTAINED_VOICE_CONFIG", str(config_file))
        monkeypatch.setenv("CONTENT_OS_MAINTAINED_VOICE_OPERATOR_KEY", c.options["operator_key"])
        config = parse_config(["--db", c.db.path, "--data-root", str(c.root), "--job-type", "generate_voice", "--ffmpeg", str(tool), "--ffprobe", str(tool)])
        runner = build_runner(config, c.db)
        assert type(runner._handlers[JobType.GENERATE_VOICE]) is MaintainedVoiceDraftHandler
        original = MaintainedVoiceDraftHandler.__call__
        def trace(self, job):
            try: return original(self, job)
            except Exception as error:
                pytest.fail("synthetic owned-handler reproduction: " + type(error).__name__)
        monkeypatch.setattr(MaintainedVoiceDraftHandler, "__call__", trace)
        result = runner.run_once()
        assert result.status.value == "completed", result.error_code
        assert c.calls == [1]
        status_url = f"/projects/{c.job.project_id}/voice-draft-jobs/{job_id}"
        status = client.get(status_url).json()
        assert status["state"] == "completed" and status["outputs"][0]["qa_state"] == "pending"
        qa = status["outputs"][0]["qa_consumption"]
        assert qa["state"] == "blocked" and qa["purpose"] == "commercial_production"
        assert qa["dispatch_authorized"] is False and qa["execution_configuration"] == "not_checked"
        audio_id = status["outputs"][0]["audio_id"]
        assert client.get(f"/audio-assets/{audio_id}/media").status_code == 403
        media = client.get(f"/audio-assets/{audio_id}/media", params={"project_id": str(c.job.project_id), "purpose": "internal_evaluation"})
        assert media.status_code == 200 and media.content[:4] == b"RIFF", media.text[:150] if media.status_code != 200 else "invalid WAV"
        assert client.post(f"/projects/{c.job.project_id}/voice-qa-jobs", json={"idempotency_key": "not-admitted", "narration_audio_id": audio_id, "target_text": c.job.payload.text}).status_code == 409
        repository = ProviderCallRepository(c.db)
        record = repository.list_for_project(c.job.project_id)[0]
        repository.update(record.model_copy(update={"status": "running", "result_payload": None, "completed_at": None}))
        blocked = client.get(status_url).json()
        assert blocked["state"] == "blocked" and blocked["recovery_action"] == "reconcile_recorded_call"
        assert blocked["automatic_retry_authorized"] is False and c.calls == [1]
