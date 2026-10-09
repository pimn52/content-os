"""Offline single-take repair contracts; synthetic PCM/timing, no inference."""
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.db import (Database, AssetRepository, AudioAssetRepository, JobRepository,
    ProviderMachineCapabilityProfileRepository, ProviderCallRepository, VoiceProfileRepository, ClipRepository)
from app.domain.models import JobStatus, ProviderMachineCapabilityProfile, TranscriptSegment, CostCategory
from app.budget import ProviderCallLedger
from app.jobs.handlers import _reserve_job_provider_call
from app.jobs.runner import JobExecutionError
from app.main import create_app
from app.production_runs import ProductionRunService
from app.repair_allowance import used_repairs
from app.voice_repair import VoiceRepairService, VoiceRepairRequest
from test_production_runs import _ready_fixture, _voice_dispatch_evidence, _generated_voice_output


def setup(client, path, *, code="voice_temporarily_unavailable"):
    project, plan, _ = _ready_fixture(client, path, with_master=False)
    run = client.post(f"/projects/{project}/production-runs", json={"idempotency_key": "voice-repair-test", "expected_fingerprint": plan["fingerprint"]}).json()
    profile, cap = _voice_dispatch_evidence(path)
    with Database(path) as db:
        asset = AssetRepository(db).list()[0]
        reference = path.parent / asset.source_file
        reference.write_bytes(b"synthetic reference bytes, not quality evidence")
        reference_asset = asset.model_copy(update={"id": uuid4(), "content_hash": hashlib.sha256(reference.read_bytes()).hexdigest()})
        AssetRepository(db).create(reference_asset)
        clip = ClipRepository(db).list()[0].model_copy(update={"id": uuid4(), "asset_id": reference_asset.id})
        ClipRepository(db).create(clip)
        profiles = VoiceProfileRepository(db); selected = profiles.get(UUID(profile))
        selected = selected.model_copy(update={"id": uuid4(), "reference_clip_ids": [clip.id]})
        profiles.create(selected); profile = str(selected.id)
        qa_cap = ProviderMachineCapabilityProfile(capability="asr", mode="local", provider="fixture-asr", model="fixture-qa",
            runtime="fixture", machine_id="fixture-host", readiness="verified", quality_status="verified",
            commercial_status="commercial_safe", license_evidence_reference="fixture:qa-license", evidence_reference="fixture:qa-admission",
            provenance_source="fixture only", updated_at=datetime.now(timezone.utc))
        ProviderMachineCapabilityProfileRepository(db).save(qa_cap)
    client.put(f"/projects/{project}/budget", json={"currency": "USD", "max_calls": 10, "allow_unknown_cost": True})
    root = f"/projects/{project}/production-runs/{run['id']}"
    voice = client.post(root + "/voice-dispatch", json={"voice_profile_id": profile, "capability_profile_id": cap,
        "authorization_reference": "fixture:explicit-dispatch"})
    assert voice.status_code == 200, voice.text
    job_id = voice.json()["voice_job_id"]
    with Database(path) as db:
        jobs = JobRepository(db)
        job = jobs.get(UUID(job_id))
        jobs.update(job.model_copy(update={"status": JobStatus.FAILED, "error_code": code}))
        db.connection.execute("INSERT INTO voice_execution_receipts(job_id,payload) VALUES (?,?)", (job_id, json.dumps({"runtime": "fixture", "machine_id": "fixture-host", "parameters": {}})))
    return project, run, root, job_id


def body(client, root, key="repair-one"):
    result = client.get(root + "/voice-repair-plan")
    assert result.status_code == 200, result.text
    plan = result.json()
    return plan, {"expected_fingerprint": plan["fingerprint"], "idempotency_key": key,
                  "confirmed_action": "regenerate_take", "reason": "fixture explicit whole-take replacement"}


def test_transient_repair_replay_restart_and_fresh_qa(tmp_path):
    path = tmp_path / "voice.sqlite"
    with TestClient(create_app(path)) as client:
        project, run, root, predecessor = setup(client, path)
        plan, request = body(client, root)
        assert plan["action"] == "regenerate_take", plan
        assert plan["max_tts_calls"] == plan["max_qa_asr_calls"] == 1
        assert plan["local_compute_cost"] is None
        response = client.post(root + "/voice-repair", json=request)
        assert response.status_code == 201, response.text
        record = response.json()
        assert client.post(root + "/voice-repair", json=request).json() == record
        assert client.post(root + "/voice-repair", json={**request, "reason": "different"}).status_code == 409
        current = client.get(root).json()
        assert current["voice_audio_id"] is None and current["voice_qa_job_id"] is None
        assert current["voice_job_id"] == record["replacement_job_id"] != predecessor
        with Database(path) as db:
            assert used_repairs(db, UUID(run["id"])) == 1
            assert JobRepository(db).get(UUID(predecessor)).error_code == "voice_temporarily_unavailable"
            assert ProductionRunService(db, tmp_path).reconcile_completed_voice_job(UUID(predecessor)) is None
            jobs = JobRepository(db); job = jobs.get(UUID(record["replacement_job_id"]))
            jobs.update(job.model_copy(update={"status": JobStatus.COMPLETED}))
        _generated_voice_output(path, project, record["replacement_job_id"], "A fresh creator point.")
        qa = client.post(root + "/voice-qa-advance")
        assert qa.status_code == 200, qa.text
        assert qa.json()["status"] == "voice_qa_pending"
        assert client.post(root + "/resume").status_code == 409
        assert client.get(root + "/voice-repairs").json()[0]["successor_qa_job_id"] == qa.json()["voice_qa_job_id"]
    with TestClient(create_app(path)) as client:
        assert client.post(root + "/voice-repair", json=request).json() == record
        assert client.get(root).json()["voice_job_id"] == record["replacement_job_id"]


@pytest.mark.parametrize("code", ["voice_invalid_request", "unknown", None])
def test_invalid_unknown_failures_stop(tmp_path, code):
    path = tmp_path / "voice.sqlite"
    with TestClient(create_app(path)) as client:
        _, _, root, _ = setup(client, path, code=code)
        plan, request = body(client, root)
        assert plan["action"] == "stop"
        assert client.post(root + "/voice-repair", json=request).status_code == 409


@pytest.mark.parametrize("change", ["allowance", "legacy", "reference_bytes", "profile", "capability", "draft", "budget", "consumed", "qa_unknown"])
def test_changed_or_unknown_scope_cannot_execute(tmp_path, change):
    path = tmp_path / "voice.sqlite"
    with TestClient(create_app(path)) as client:
        project, run, root, job = setup(client, path)
        _, request = body(client, root)
        with Database(path) as db:
            if change == "allowance": db.connection.execute("UPDATE production_runs SET repair_allowance=0")
            if change == "legacy": db.connection.execute("DELETE FROM voice_dispatch_snapshots")
            if change == "reference_bytes": (tmp_path / "fixture-reference.mp4").write_bytes(b"changed")
            if change == "profile":
                db.connection.execute("UPDATE voice_profiles SET payload=json_set(payload,'$.name','changed')")
            if change == "capability":
                repo = ProviderMachineCapabilityProfileRepository(db); c = next(c for c in repo.list() if c.capability == "voice"); repo.save(c.model_copy(update={"commercial_status": "unknown"}))
            if change == "consumed": db.connection.execute("UPDATE production_runs SET stage='awaiting_master'")
            if change == "qa_unknown": db.connection.execute("DELETE FROM provider_machine_capability_profiles WHERE json_extract(payload,'$.capability')='asr'")
        if change == "draft": client.put(f"/projects/{project}/draft", json={"script": "different", "topic": "changed"})
        if change == "budget": client.put(f"/projects/{project}/budget", json={"currency": "USD", "max_calls": 1, "allow_unknown_cost": True})
        assert body(client, root)[0]["action"] == "stop"
        assert client.post(root + "/voice-repair", json=request).status_code == 409
        assert client.get(root + "/voice-repairs").json() == []


def copy_failure(client, path, root, project, job_id, checks):
    with Database(path) as db:
        jobs = JobRepository(db); job = jobs.get(UUID(job_id)); jobs.update(job.model_copy(update={"status": JobStatus.COMPLETED, "error_code": None}))
    audio_id = _generated_voice_output(path, project, job_id, "A fresh creator point.")
    qa = client.post(root + "/voice-qa-advance")
    assert qa.status_code == 200, qa.text
    with Database(path) as db:
        jobs = JobRepository(db); job = jobs.get(UUID(qa.json()["voice_qa_job_id"])); jobs.update(job.model_copy(update={"status": JobStatus.FAILED, "error_code": "voice_qa_failed"}))
        audios = AudioAssetRepository(db); audio = audios.get(audio_id)
        metadata = audio.metadata.copy(); gen = metadata["voice_generation"].copy()
        gen.update(qa_state="failed", qa={"qa_state": "failed", "provider": "fixture-asr", "model": "fixture-qa", "playable": True, "transcript_segment_count": 1, "checks": checks})
        metadata["voice_generation"] = gen
        audios.update(audio.model_copy(update={"metadata": metadata, "transcript_source": "fixture-asr:fixture-qa",
            "transcript_segments": [TranscriptSegment(start_ms=0, end_ms=3000, text="A point.")]}))
    return audio_id, qa.json()["voice_qa_job_id"]


@pytest.mark.parametrize("checks,allowed", [(["copy_missing_tokens"], True), (["copy_duplicate_tokens"], True), (["copy_substitution_tokens"], True), (["copy_missing_tokens", "long_silence"], True), (["leading_silence_or_unrecognized_audio"], False), (["copy_missing_tokens", "timed_transcript_missing"], False)])
def test_timed_copy_report_is_not_asr_failure_or_trimming_authority(tmp_path, checks, allowed):
    path = tmp_path / "voice.sqlite"
    with TestClient(create_app(path)) as client:
        project, _, root, original = setup(client, path)
        audio_id, old_qa = copy_failure(client, path, root, project, original, checks)
        plan, request = body(client, root)
        assert (plan["action"] == "regenerate_take") is allowed, plan
        if allowed:
            with Database(path) as db: before = AudioAssetRepository(db).get(audio_id)
            record = client.post(root + "/voice-repair", json=request).json()
            with Database(path) as db:
                assert AudioAssetRepository(db).get(audio_id) == before
                assert JobRepository(db).get(UUID(old_qa)).status == JobStatus.FAILED
                assert ProductionRunService(db, tmp_path).reconcile_completed_voice_job(UUID(original)) is None
                jobs = JobRepository(db); job = jobs.get(UUID(record["replacement_job_id"])); jobs.update(job.model_copy(update={"status": JobStatus.COMPLETED}))
            # Distinct PCM bytes avoid asset deduplication; exact old audio remains immutable.
            source = tmp_path / "replacement.wav"
            source.write_bytes((tmp_path / "generated-take.wav").read_bytes()[:-2] + b"\x01\x00")
            with Database(path) as db:
                successor = before.model_copy(update={"id": uuid4(), "source_file": source.name,
                    "content_hash": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "metadata": {"voice_generation": {**before.metadata["voice_generation"], "job_id": record["replacement_job_id"], "qa_state": "pending"}}})
                AudioAssetRepository(db).create(successor)
            qa = client.post(root + "/voice-qa-advance")
            assert qa.status_code == 200, qa.text
            assert qa.json()["voice_qa_job_id"] != old_qa


def test_tts_and_asr_reservations_each_once_and_remote_denied(tmp_path):
    path = tmp_path / "voice.sqlite"
    with TestClient(create_app(path)) as client:
        project, run, root, _ = setup(client, path)
        _, request = body(client, root); record = client.post(root + "/voice-repair", json=request).json()
        with Database(path) as db:
            jobs = JobRepository(db); job = jobs.get(UUID(record["replacement_job_id"]))
            args = dict(operation="tts", category=CostCategory.VOICE, provider="fixture-voice", model="fixture-model", input_source="test", repair_data_root=tmp_path, repair_worker_execution={"runtime": "fixture", "machine_id": "fixture-host", "parameters": {}})
            with pytest.raises(JobExecutionError, match="declared admission"):
                _reserve_job_provider_call(ProviderCallLedger(db), job, known_local_cost=False, **args)
            assert ProviderCallRepository(db).list_for_project(UUID(project)) == []
            _reserve_job_provider_call(ProviderCallLedger(db), job, known_local_cost=True, **args)
            with pytest.raises(JobExecutionError):
                _reserve_job_provider_call(ProviderCallLedger(db), job.model_copy(update={"attempt": 2}), known_local_cost=True, **args)
            jobs.update(job.model_copy(update={"status": JobStatus.COMPLETED}))
        _generated_voice_output(path, project, record["replacement_job_id"], "A fresh creator point.")
        qa = client.post(root + "/voice-qa-advance").json()
        with Database(path) as db:
            job = JobRepository(db).get(UUID(qa["voice_qa_job_id"]))
            args = dict(operation="asr", category=CostCategory.ASR, provider="fixture-asr", model="fixture-qa", input_source="test", repair_data_root=tmp_path, known_local_cost=True, repair_worker_execution={"runtime": "fixture", "machine_id": "fixture-host", "parameters": {}})
            for override in ({"repair_local_verified": False}, {"model": "changed"}, {"repair_worker_execution": {"runtime": "fixture", "machine_id": "fixture-host", "parameters": {"vad_filter": True}}}):
                with pytest.raises(JobExecutionError): _reserve_job_provider_call(ProviderCallLedger(db), job, **{**args, **override})
            _reserve_job_provider_call(ProviderCallLedger(db), job, **args)
            with pytest.raises(JobExecutionError): _reserve_job_provider_call(ProviderCallLedger(db), job.model_copy(update={"attempt": 2}), **args)
            assert len(ProviderCallRepository(db).list_for_project(UUID(project))) == 2


def test_repair_rolls_back_job_record_and_binding(tmp_path, monkeypatch):
    path = tmp_path / "voice.sqlite"
    with TestClient(create_app(path)) as client:
        project, run, root, old = setup(client, path)
        _, request = body(client, root)
        original = JobRepository.create
        def fail_after_create(repo, job):
            original(repo, job)
            raise RuntimeError("test rollback")
        monkeypatch.setattr(JobRepository, "create", fail_after_create)
        with Database(path) as db:
            with pytest.raises(RuntimeError): VoiceRepairService(db, tmp_path).execute(UUID(project), UUID(run["id"]), VoiceRepairRequest(**request))
            assert used_repairs(db, UUID(run["id"])) == 0
            assert len(JobRepository(db).list()) == 1
        assert client.get(root).json()["voice_job_id"] == old


@pytest.mark.parametrize("change", ["approved", "subjective", "composition", "no_timing", "qa_execution_failed", "file_changed"])
def test_review_composition_and_incomplete_evidence_do_not_regenerate(tmp_path, change):
    path = tmp_path / "voice.sqlite"
    with TestClient(create_app(path)) as client:
        project, _, root, original = setup(client, path)
        audio_id, qa_id = copy_failure(client, path, root, project, original, ["copy_missing_tokens"])
        with Database(path) as db:
            audios = AudioAssetRepository(db); audio = audios.get(audio_id); metadata = audio.metadata.copy(); gen = metadata["voice_generation"].copy()
            if change in {"approved", "subjective"}:
                from app.domain.models import VoiceHumanReview
                from app.voice_qa import apply_voice_human_review
                gen["qa_state"] = "verified"; metadata["voice_generation"] = gen
                review = VoiceHumanReview(approved=change == "approved", evidence_reference="fixture:review", findings=["fixture only"],
                    likeness="pass" if change == "approved" else "needs_revision", naturalness="pass", emphasis="pass", pace="pass", pauses="pass", rhythm="pass", reviewed_at=datetime.now(timezone.utc))
                audios.update(apply_voice_human_review(audio.model_copy(update={"metadata": metadata}), review))
            if change == "composition":
                gen["composition"] = {"source_take_audio_ids": [str(audio_id)]}; metadata["voice_generation"] = gen; audios.update(audio.model_copy(update={"metadata": metadata}))
            if change == "no_timing": audios.update(audio.model_copy(update={"transcript_segments": []}))
            if change == "qa_execution_failed":
                jobs = JobRepository(db); qa = jobs.get(UUID(qa_id)); jobs.update(qa.model_copy(update={"error_code": "voice_qa_asr_failed"}))
            if change == "file_changed": (tmp_path / audio.source_file).write_bytes(b"changed")
        plan, request = body(client, root)
        assert plan["action"] == ("replan" if change == "subjective" else "stop"), plan
        assert client.post(root + "/voice-repair", json=request).status_code == 409


def test_worker_configuration_unknown_changed_and_no_accounting_block(tmp_path):
    path = tmp_path / "voice.sqlite"
    with TestClient(create_app(path)) as client:
        project, _, root, _ = setup(client, path)
        _, request = body(client, root); record = client.post(root + "/voice-repair", json=request).json()
        with Database(path) as db:
            job = JobRepository(db).get(UUID(record["replacement_job_id"]))
            for identity in (None, {"runtime": "changed", "machine_id": "fixture-host", "parameters": {}}, {"runtime": "fixture", "machine_id": "fixture-host", "parameters": {"speed": 2}}):
                with pytest.raises(JobExecutionError):
                    _reserve_job_provider_call(ProviderCallLedger(db), job, operation="tts", category=CostCategory.VOICE,
                        provider="fixture-voice", model="fixture-model", input_source="test", known_local_cost=True,
                        repair_data_root=tmp_path, repair_worker_execution=identity)
            from app.jobs.handlers import VoiceGenerationJobHandler
            handler = VoiceGenerationJobHandler(VoiceProfileRepository(db), AudioAssetRepository(db), None, None, tmp_path)
            with pytest.raises(JobExecutionError, match="durable accounting"): handler(job.model_copy(update={"status": JobStatus.RUNNING}))
            assert ProviderCallRepository(db).list_for_project(UUID(project)) == []


def test_concurrent_submits_share_one_allowance(tmp_path):
    path = tmp_path / "voice.sqlite"
    with TestClient(create_app(path)) as client:
        project, run, root, _ = setup(client, path); _, request = body(client, root)
    def submit(key):
        with Database(path) as db:
            try:
                VoiceRepairService(db, tmp_path).execute(UUID(project), UUID(run["id"]), VoiceRepairRequest(**{**request, "idempotency_key": key}))
                return "created"
            except ValueError:
                return "denied"
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(submit, ["one", "two"])) == ["created", "denied"]
    with Database(path) as db:
        assert used_repairs(db, UUID(run["id"])) == 1
        assert len(JobRepository(db).list()) == 2


def test_migration_retains_existing_talking_allowance(tmp_path, monkeypatch):
    from test_talking_repair import setup as talking_setup, request as talking_request
    path = tmp_path / "talking.sqlite"
    with TestClient(create_app(path)) as client:
        project, run, binding, _, root = talking_setup(client, path, monkeypatch)
        _, request = talking_request(client, root, binding)
        assert client.post(root + "/talking-repair", json=request).status_code == 201
    with Database(path) as db:
        for table in ("voice_repairs", "voice_dispatch_snapshots", "voice_execution_receipts"):
            db.connection.execute(f"DROP TABLE {table}")
        db.connection.execute("DELETE FROM schema_migrations WHERE version=37")
    with Database(path) as db:
        assert used_repairs(db, UUID(run["id"])) == 1
        assert len(VoiceRepairService(db, tmp_path).records(UUID(project), UUID(run["id"]))) == 0


def test_voice_usage_counts_against_talking_plan(tmp_path, monkeypatch):
    from test_talking_repair import setup as talking_setup, request as talking_request
    path = tmp_path / "talking.sqlite"
    with TestClient(create_app(path)) as client:
        project, run, binding, _, root = talking_setup(client, path, monkeypatch)
        # A persisted earlier Voice repair from this Run is a historical fact even after stage advancement.
        with Database(path) as db:
            db.connection.execute("INSERT INTO voice_repairs(id,project_id,run_id,idempotency_key,request_fingerprint,replacement_job_id,payload) VALUES (?,?,?,?,?,?,?)",
                (str(uuid4()), project, run["id"], "fixture-prior-voice", "0" * 64, binding["talking_job_id"], "{}"))
        plan, request = talking_request(client, root, binding)
        assert plan["remaining_run_repairs"] == 0 and plan["action"] == "stop"
        assert client.post(root + "/talking-repair", json=request).status_code == 409


def test_identical_failed_bytes_cannot_be_new_candidate(tmp_path):
    from app.jobs.handlers import VoiceGenerationJobHandler
    from app.media.audio_importer import AudioImporter
    from app.providers.voice import VoiceSynthesisResult
    path = tmp_path / "voice.sqlite"
    with TestClient(create_app(path)) as client:
        project, _, root, original = setup(client, path)
        audio_id, _ = copy_failure(client, path, root, project, original, ["copy_missing_tokens"])
        _, request = body(client, root); record = client.post(root + "/voice-repair", json=request).json()
        with Database(path) as db:
            before = AudioAssetRepository(db).get(audio_id)
            class Provider:
                provider_name = "fixture-voice"
                model = "fixture-model"
                is_local = True
                repair_execution = {"runtime": "fixture", "machine_id": "fixture-host", "parameters": {}}
                def synthesize(self, profile, text, output, **kwargs):
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_bytes((tmp_path / before.source_file).read_bytes())
                    return VoiceSynthesisResult(output)
            job = JobRepository(db).get(UUID(record["replacement_job_id"]))
            handler = VoiceGenerationJobHandler(VoiceProfileRepository(db), AudioAssetRepository(db),
                AudioImporter(db, tmp_path), Provider(), tmp_path / "generated", ProviderCallLedger(db))
            with pytest.raises(JobExecutionError): handler(job.model_copy(update={"status": JobStatus.RUNNING}))
            assert AudioAssetRepository(db).get(audio_id) == before
            assert len(AudioAssetRepository(db).list()) == 1
        assert client.get(root).json()["voice_audio_id"] is None
