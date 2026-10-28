"""分段语音责任返工：合成 PCM、替身 ASR/Voice，无真实推理。"""
import hashlib
import json
import struct
import wave
from datetime import datetime, timezone, timedelta
from pathlib import Path
from threading import Event, Timer
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.budget import ProviderCallLedger
from app.db import (AudioAssetRepository, Database, JobRepository, ProjectDraftRepository,
    ProviderCallRepository, VoiceProfileRepository)
from app.domain.models import AudioAsset, JobStatus, JobType
from app.jobs import JobRunner, JobStore
from app.jobs.handlers import VoiceGenerationJobHandler, VoiceQaJobHandler
from app.jobs.runner import JobExecutionError
from app.main import create_app
from app.media.audio_importer import AudioImporter
from app.media.ffprobe import AudioProbeMetadata
from app.production_runs import ProductionRunNotReady, ProductionRunService
from app.providers.asr import TranscriptionResult, TranscriptionSegment
from app.providers.voice import VoiceSynthesisResult
from app.repair_allowance import used_repairs
from app.voice_repair import (VoiceRepairService, execution_snapshot, voice_segment_repair_context)
from app.video_production import VideoProductionService
from app.worker_cli import _run_product_queue
from test_voice_repair import setup


def write_pcm(path, value, frames=24_000):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, 24_000, 0, "NONE", "not compressed"))
        output.writeframes(struct.pack("<h", value) * frames)


class Probe:
    def probe_audio(self, path):
        with wave.open(str(path), "rb") as audio:
            return AudioProbeMetadata(audio.getnframes()*1000//audio.getframerate(),
                audio.getframerate(), audio.getnchannels(), "en", {})


def segmented_failure(client, path, *, all_failed=False):
    project, run, root, predecessor = setup(client, path)
    text = "A fresh\n\ncreator point."
    parts = []
    for index, (start, end) in enumerate(((0, 7), (9, len(text)))):
        source = path.parent / f"part-{index}.wav"
        write_pcm(source, 1000+index)
        parts.append(dict(source_file=source.name, sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            start_char=start, end_char=end, start_sample=index*24_000, end_sample=(index+1)*24_000))
    master = path.parent / "master.wav"
    with wave.open(str(master), "wb") as output:
        output.setparams((1, 2, 24_000, 0, "NONE", "not compressed"))
        for part in parts:
            with wave.open(str(path.parent / part["source_file"]), "rb") as source:
                output.writeframes(source.readframes(source.getnframes()))
    with Database(path) as db:
        jobs = JobRepository(db); job = jobs.get(UUID(predecessor))
        payload = job.payload.model_copy(update={"text": text})
        jobs.update(job.model_copy(update={"status": JobStatus.COMPLETED, "error_code": None, "payload": payload}))
        drafts = ProjectDraftRepository(db); draft = drafts.get(UUID(project))
        draft = drafts.save(draft.model_copy(update={"script": text, "version": draft.version+1,
            "script_revision": draft.script_revision+1}))
        db.connection.execute("UPDATE production_runs SET draft_version=?,script_revision=? WHERE id=?",
            (draft.version, draft.script_revision, run["id"]))
        db.connection.execute("UPDATE voice_dispatch_snapshots SET payload=? WHERE job_id=?",
            (json.dumps(execution_snapshot(db, payload)), predecessor))
        db.connection.execute("UPDATE production_runs SET automatic=1 WHERE id=?", (run["id"],))
        audio = AudioAsset(source_file=master.name, content_hash=hashlib.sha256(master.read_bytes()).hexdigest(),
            duration_ms=2000, sample_rate=24_000, channels=1, language="en",
            authorization_reference=payload.authorization_reference, imported_at=datetime.now(timezone.utc),
            metadata={"voice_generation": {"job_id": predecessor, "project_id": project,
                "voice_profile_id": str(payload.voice_profile_id), "provider": payload.expected_provider,
                "model": payload.expected_model, "target_text": text, "qa_state": "pending", "generation_segments": parts}})
        AudioAssetRepository(db).create(audio)
        qa_run = ProductionRunService(db, path.parent).advance_voice_qa(UUID(project), UUID(run["id"]))
        qa = jobs.get(qa_run.voice_qa_job_id)
        class ASR:
            is_local = True
            def transcribe(self, source, **kwargs):
                copy = "A" if all_failed or Path(source).stem.endswith("1") else "A fresh"
                return TranscriptionResult(copy, (TranscriptionSegment(0, 900, copy),), "en")
        handler = VoiceQaJobHandler(AudioAssetRepository(db), ASR(), ProviderCallLedger(db),
            provider_name="fixture-asr", provider_model="fixture-qa", data_root=path.parent,
            repair_execution={"runtime": "fixture", "machine_id": "fixture-host", "parameters": {}})
        with pytest.raises(JobExecutionError, match="did not pass"):
            handler(qa.model_copy(update={"status": JobStatus.RUNNING}))
        jobs.update(qa.model_copy(update={"status": JobStatus.FAILED, "error_code": "voice_qa_failed"}))
    return UUID(project), UUID(run["id"]), root, audio.id, parts


def test_automatic_segment_repair_preserves_good_pcm_and_rechecks_full_master(tmp_path):
    path = tmp_path / "segmented.sqlite"
    with TestClient(create_app(path)) as client:
        project, run, root, original_id, parts = segmented_failure(client, path)
        with Database(path) as db:
            service = VoiceRepairService(db, tmp_path)
            plan = service.plan(project, run)
            assert plan.action == "regenerate_take", plan
            assert plan.execution_granularity == "segment"
            assert plan.evidence["segment_repair"]["index"] == 1
            original = AudioAssetRepository(db).get(original_id)
            record = service.reconcile_automatic_qa_failure(project, run)
            assert service.reconcile_automatic_qa_failure(project, run) is None
            assert used_repairs(db, run) == 1
            job = JobRepository(db).get(UUID(record["replacement_job_id"]))
            assert job.payload.text == "A fresh\n\ncreator point."
            seen = []
            class Voice:
                provider_name = "fixture-voice"
                model = "fixture-model"
                is_local = True
                repair_execution = {"runtime": "fixture", "machine_id": "fixture-host", "parameters": {}}
                def synthesize(self, profile, text, output, **kwargs):
                    seen.append(text); write_pcm(output, 2000, frames=36_000)
                    return VoiceSynthesisResult(output)
            handler = VoiceGenerationJobHandler(VoiceProfileRepository(db), AudioAssetRepository(db),
                AudioImporter(db, tmp_path, Probe()), Voice(), tmp_path / "generated", ProviderCallLedger(db))
            handler(job.model_copy(update={"status": JobStatus.RUNNING}))
            assert seen == ["creator point."]
            outputs = [a for a in AudioAssetRepository(db).list()
                if a.metadata.get("voice_generation", {}).get("job_id") == str(job.id)]
            assert len(outputs) == 1
            successor = outputs[0]
            assert successor.duration_ms == 2500
            assert successor.metadata["voice_generation"]["qa_state"] == "pending"
            assert successor.metadata["voice_generation"]["generation_segments"][0] == parts[0]
            assert successor.metadata["voice_generation"]["segment_repair"]["predecessor_sha256"] == original.content_hash
            with wave.open(str(successor.source_file), "rb") as audio:
                assert audio.readframes(24_000) == struct.pack("<h", 1000)*24_000
            assert AudioAssetRepository(db).get(original_id) == original
            jobs = JobRepository(db); jobs.update(job.model_copy(update={"status": JobStatus.COMPLETED}))
            current = ProductionRunService(db, tmp_path).advance_voice_qa(project, run)
            qa = jobs.get(current.voice_qa_job_id)
            assert qa.payload.narration_audio_id == successor.id and qa.payload.target_text == job.payload.text
            seen_qa = []
            class ASR:
                is_local = True
                def transcribe(self, source, **kwargs):
                    seen_qa.append(str(source))
                    text = "A fresh" if Path(source).name == "part-0.wav" else "creator point."
                    end = 900 if text == "A fresh" else 1400
                    return TranscriptionResult(text, (TranscriptionSegment(0, end, text),), "en")
            qa_handler = VoiceQaJobHandler(AudioAssetRepository(db), ASR(), ProviderCallLedger(db),
                provider_name="fixture-asr", provider_model="fixture-qa", data_root=tmp_path,
                repair_execution={"runtime": "fixture", "machine_id": "fixture-host", "parameters": {}})
            qa_handler(qa.model_copy(update={"status": JobStatus.RUNNING}))
            assert len(seen_qa) == 2
            assert AudioAssetRepository(db).get(successor.id).metadata["voice_generation"]["qa_state"] == "verified"
            assert len(ProviderCallRepository(db).list_for_project(project)) == 3  # 原QA、局部Voice、新Master QA


@pytest.mark.parametrize("change", ["all_failed", "missing_report", "tampered_report", "unknown_global", "part_changed", "budget", "consumed"])
def test_segment_repair_does_not_guess_or_overwrite_unsupported_failure(tmp_path, change):
    path = tmp_path / "stop.sqlite"
    with TestClient(create_app(path)) as client:
        project, run, root, audio_id, parts = segmented_failure(client, path, all_failed=change=="all_failed")
        with Database(path) as db:
            audios = AudioAssetRepository(db); audio = audios.get(audio_id)
            if change in {"missing_report", "tampered_report", "unknown_global"}:
                metadata = json.loads(audio.model_dump_json())["metadata"]
                qa = metadata["voice_generation"]["qa"]
                if change == "missing_report": qa.pop("generation_part_reports")
                elif change == "tampered_report": qa["generation_part_reports"][0]["report"]["checks"] = ["copy_missing_tokens"]
                else: qa["checks"].append("unknown_quality_finding")
                audios.update(audio.model_copy(update={"metadata": metadata}))
            if change == "part_changed": (tmp_path / parts[0]["source_file"]).write_bytes(b"changed")
            if change == "consumed": db.connection.execute("UPDATE production_runs SET stage='awaiting_talking' WHERE id=?", (str(run),))
        if change == "budget":
            client.put(f"/projects/{project}/budget", json={"currency":"USD", "max_calls":2, "allow_unknown_cost":True})
        with Database(path) as db:
            before = len(ProviderCallRepository(db).list_for_project(project))
            service = VoiceRepairService(db, tmp_path)
            assert service.plan(project, run).action == "stop"
            assert service.reconcile_automatic_qa_failure(project, run) is None
            assert used_repairs(db, run) == 0
            assert len(ProviderCallRepository(db).list_for_project(project)) == before


def test_segment_bytes_changed_after_plan_stop_before_provider(tmp_path):
    path = tmp_path / "changed.sqlite"
    with TestClient(create_app(path)) as client:
        project, run, _, _, parts = segmented_failure(client, path)
        with Database(path) as db:
            record = VoiceRepairService(db, tmp_path).reconcile_automatic_qa_failure(project, run)
            job = JobRepository(db).get(UUID(record["replacement_job_id"]))
            write_pcm(tmp_path / parts[0]["source_file"], 3000)
            with pytest.raises(ProductionRunNotReady, match="segment_sources_changed"):
                voice_segment_repair_context(db, job, tmp_path)
            assert len(ProviderCallRepository(db).list_for_project(project)) == 1


def test_ordinary_queue_leaves_failed_zero_allowance_run_untouched(tmp_path):
    path=tmp_path/'old-failed.sqlite'
    with TestClient(create_app(path)) as client:
        project,run,_,_,_=segmented_failure(client,path)
    with Database(path) as db:
        db.data_root=tmp_path
        db.connection.execute('UPDATE production_runs SET repair_allowance=0 WHERE id=?',(str(run),))
        before=db.connection.execute('SELECT voice_qa_auto_stop_reasons FROM production_runs WHERE id=?',(str(run),)).fetchone()[0]
        calls_before=len(ProviderCallRepository(db).list_for_project(project))
        def forbidden(job):pytest.fail('没有返工额度的旧失败任务不能被自动执行')
        runner=JobRunner(JobStore(db),{JobType.GENERATE_VOICE:forbidden,JobType.VERIFY_VOICE:forbidden},
            worker_id='old-failed',lease_duration=timedelta(minutes=1),max_attempts=1)
        _run_product_queue(SimpleNamespace(once=True,poll_seconds=.01,data_root=tmp_path),db,runner,Event())
        assert db.connection.execute('SELECT voice_qa_auto_stop_reasons FROM production_runs WHERE id=?',(str(run),)).fetchone()[0]==before
        assert used_repairs(db,run)==0 and len(ProviderCallRepository(db).list_for_project(project))==calls_before


@pytest.mark.parametrize("fails_again", [False, True])
def test_ordinary_queue_repairs_one_voice_segment_and_advances_without_manual_resume(tmp_path, fails_again):
    path = tmp_path / "ordinary.sqlite"
    with TestClient(create_app(path)) as client:
        project, run, _, original_id, _ = segmented_failure(client, path)
    with Database(path) as db:
        db.data_root = tmp_path
        stop, steps = Event(), []
        service = VideoProductionService(db, tmp_path)
        class Voice:
            provider_name = "fixture-voice"
            model = "fixture-model"
            is_local = True
            repair_execution = {"runtime": "fixture", "machine_id": "fixture-host", "parameters": {}}
            def synthesize(self, profile, text, output, **kwargs):
                steps.append("voice:"+text); write_pcm(output, 2100, frames=36_000)
                return VoiceSynthesisResult(output)
        class ASR:
            is_local = True
            def transcribe(self, source, **kwargs):
                steps.append("qa:"+Path(source).name)
                first = Path(source).name == "part-0.wav"
                text = "A fresh" if first else "creator" if fails_again else "creator point."
                return TranscriptionResult(text, (TranscriptionSegment(0, 900 if first else 1400, text),), "en")
        voice = VoiceGenerationJobHandler(VoiceProfileRepository(db), AudioAssetRepository(db),
            AudioImporter(db, tmp_path, Probe()), Voice(), tmp_path/"generated", ProviderCallLedger(db))
        qa = VoiceQaJobHandler(AudioAssetRepository(db), ASR(), ProviderCallLedger(db),
            provider_name="fixture-asr", provider_model="fixture-qa", data_root=tmp_path,
            repair_execution={"runtime":"fixture", "machine_id":"fixture-host", "parameters":{}})
        def render(job):
            steps.append("render")
            assert job.payload.video_spec.master_narration.audio_asset_id != original_id
        def completed(job):
            if job.type is JobType.GENERATE_VOICE:
                service.runs.reconcile_completed_voice_job(job.id)
            service.completed(job)
            if job.type is JobType.RENDER:
                stop.set()
        runner = JobRunner(JobStore(db), {JobType.GENERATE_VOICE:voice, JobType.VERIFY_VOICE:qa,
            JobType.RENDER:render}, worker_id="ordinary-voice-repair", lease_duration=timedelta(minutes=1),
            max_attempts=1, on_completed=completed)
        timeout = Timer(2, stop.set); timeout.start()
        try:
            _run_product_queue(SimpleNamespace(once=False,poll_seconds=.01,data_root=tmp_path),db,runner,stop)
        finally:
            timeout.cancel()
        current = service.runs.get(project, run)
        assert len([step for step in steps if step.startswith("voice:")]) == 1
        assert len([step for step in steps if step.startswith("qa:")]) == 2
        assert used_repairs(db, run) == 1
        if fails_again:
            assert "render" not in steps
            assert current.voice_qa_auto_stop_reasons
            assert JobRepository(db).get(current.voice_qa_job_id).status is JobStatus.FAILED
        else:
            assert steps[-1] == "render", (steps, current)
            assert current.status == "render_completed_awaiting_review"
            assert JobRepository(db).get(current.voice_qa_job_id).status is JobStatus.COMPLETED
