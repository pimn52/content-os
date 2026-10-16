"""Normal repository objects + owned prepared child; fixture synthesis only."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
from types import SimpleNamespace
from uuid import uuid4
import wave

import pytest

from app.db import AssetRepository, AudioAssetRepository, ClipRepository, Database, JobRepository, ProviderCallRepository, ProviderMachineCapabilityProfileRepository, VoiceProfileRepository
from app.domain.models import Asset, Clip, ConsentRecord, Job, JobStatus, JobType, RationalFps, TranscriptSegment, VoiceGenerationJobPayload, VoiceProfile
from app.execution_admission import UseAdmissionError, configuration_digest
from app.execution_scope import ExecutionScopeService, commercial_content_blocker, job_use_identity, require_evaluation_lane_closed
from app.maintained_voice_job import execute_configured_maintained_voice_job, execute_maintained_voice_job
from app.media.audio_importer import AudioImporter
from app.media.voice_reference import prepare_voice_reference
from app.media.ffprobe import AudioProbeMetadata
from app.provider_execution import ProviderExecutionIdempotencyConflict, ProviderExecutionInProgress, ProviderExecutionReplayUnavailable, ProviderExecutionRecordedFailure
from app.providers.maintained_voice_execution import prepare_maintained_voice_child
from app.runtime_inventory import RuntimeInventoryStore
from app.voice_performance import authorized_voice_reference_choices, resolve_authorized_voice_reference_choice
from test_execution_admission import setup, OPERATOR_KEY
from test_execution_attestation import adopt_v2
from test_maintained_runtime import KEY, reference, candidate
from test_maintained_voice_execution import child_case, command_options
from app.providers.maintained_voice_prepared import _observe
from test_provider_execution import Cost


@pytest.fixture
def bound_job(setup, child_case, monkeypatch, request):
    client, path, project, *_ = setup
    root = path.parent
    options = dict(child_case.options)
    model, work, outputs = root / "model", root / "work", root / "generated"
    shutil.copytree(options["model_root"], model)
    work.mkdir(); (outputs / "voice").mkdir(parents=True)
    for source in (child_case.origin_path, child_case.candidate["source"]):
        (root / source.name).write_bytes(source.read_bytes())
    source = root / "reference.wav"
    with wave.open(str(source), "wb") as audio:
        audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        audio.writeframes(struct.pack("<h", 1000) * 72000)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    source_media = source
    if getattr(request, "param", None) == "video":
        source_media = root / "reference.mp4"
        source_media.write_bytes(b"fixture encoded stereo video; decoded by mocked FFmpeg")
    options.update(data_root=root, model_root=model, staging_parent=work,
        origin_reference=reference(root / child_case.origin_path.name, root))
    now = datetime.now(timezone.utc)
    with Database(path) as db:
        db.data_root = root
        asset = Asset(source_file=str(source_media), content_hash=hashlib.sha256(source_media.read_bytes()).hexdigest(), duration_ms=3000, width=1080, height=1920,
            fps=RationalFps(numerator=25, denominator=1), has_audio=True, authorization_reference="synthetic-source", imported_at=now)
        AssetRepository(db).create(asset)
        clip = Clip(asset_id=asset.id, start_ms=0, end_ms=3000, asset_duration_ms=3000,
            transcript_segments=[TranscriptSegment(start_ms=0, end_ms=3000, text="synthetic reference")])
        ClipRepository(db).create(clip)
        voice = VoiceProfile(name="synthetic", provider="omnivoice", provider_profile_id="synthetic",
            reference_clip_ids=[clip.id], consent=ConsentRecord(confirmed=True, subject_name="synthetic",
                basis="self", confirmed_at=now, authorization_reference="synthetic-consent"),
            language="zh", created_at=now)
        VoiceProfileRepository(db).create(voice)
        selection = authorized_voice_reference_choices(voice, [clip], {asset.id: asset}, data_root=root)[0][0]
        job = Job(project_id=project, type=JobType.GENERATE_VOICE, status=JobStatus.RUNNING, attempt=1,
            idempotency_key="maintained-job", created_at=now, updated_at=now)
        options["inputs"] = options["inputs"].model_copy(update=dict(reference_audio=str(source),
            reference_sha256=digest, reference_start_frame=0, reference_end_frame=72000,
            reference_max_frames=72000, reference_max_bytes=200000,
            output_path=str(outputs / "voice" / f"{job.id}-attempt-1.wav")))
    monkeypatch.setenv("CONTENT_OS_EXECUTION_ADMISSION_KEY_HASHES", json.dumps({
        "operator": hashlib.sha256(OPERATOR_KEY.encode()).hexdigest(), "test-operator": hashlib.sha256(KEY.encode()).hexdigest()}))
    spec = _observe(dict(options, gpu_uuid=options.get("gpu_uuid")), packaged=True, timeout_seconds=options["timeout_seconds"])
    with Database(path) as db:
        capability = setup[3].model_copy(update=dict(id=uuid4(), **{key:getattr(spec,key) for key in ("provider","model","runtime","machine_id")}))
        ProviderMachineCapabilityProfileRepository(db).save(capability)
    case = list(setup); case[3] = capability
    case[5] = setup[5].model_copy(update=dict(capability_profile_id=capability.id, configuration_sha256=configuration_digest(capability)))
    RuntimeInventoryStore(root).install_v2(child_case.inventory)
    receipt, _, _ = adopt_v2(case, spec, key="maintained-job-report")
    class Probe:
        def probe_audio(self, path):
            with wave.open(str(path), "rb") as audio:
                return AudioProbeMetadata(max(1, audio.getnframes()*1000//audio.getframerate()), audio.getframerate(), audio.getnchannels(), None, {})
    with Database(path) as db:
        db.data_root = root
        job = job.model_copy(update={"payload": VoiceGenerationJobPayload(project_id=project, voice_profile_id=voice.id,
            text=options["inputs"].text, language="zh", authorization_reference="synthetic-consent",
            reference_window=selection, expected_provider=spec.provider, expected_model=spec.model,
            execution_capability_profile_id=capability.id)})
        JobRepository(db).create(job)
        scopes = ExecutionScopeService(db, root)
        scope = scopes.prepare(project, purpose="internal_evaluation", admission_ids=(receipt,))
        scopes.pin("job", job.id, scope, operation="generation", subject_sha256=job_use_identity(job))
        importer = AudioImporter(db, root, Probe())
        calls = []
        class Child:
            def wait(self, timeout):
                assert not db.connection.in_transaction
                rows = ProviderCallRepository(db).list_for_project(project)
                assert len(rows) == 1 and rows[0].status == "running"
                calls.append(1)
                with wave.open(options["inputs"].output_path, "wb") as audio:
                    audio.setparams((1,2,24000,0,"NONE","not compressed"))
                    audio.writeframes(struct.pack("<4h", 1000, -1000, 2000, -2000))
                return 0
            def poll(self): return 0
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: Child())
        yield SimpleNamespace(db=db, root=root, outputs=outputs, job=job, options=options, receipt=receipt,
            importer=importer, calls=calls, scope=scope, voice=voice, asset=asset, clip=clip, cost=Cost())


def execute(c, *, replay=False):
    with prepare_maintained_voice_child(**command_options(c.options), replay_only=replay) as prepared:
        return execute_maintained_voice_job(db=c.db, data_root=c.root, output_root=c.outputs, job=c.job,
            prepared=prepared, execution_admission_id=c.receipt, importer=c.importer, cost_estimator=c.cost)


def test_owned_job_result_is_atomic_scoped_pending_and_replayed_without_generation(bound_job, monkeypatch):
    c = bound_job
    original = c.importer._place
    def place(temp, destination):
        assert c.db.connection.in_transaction
        rows = c.db.connection.execute("SELECT payload FROM audio_assets").fetchall()
        assert len(rows) == 1
        metadata = json.loads(rows[0][0])["metadata"]
        assert metadata["voice_generation"]["job_id"] == str(c.job.id)
        assert metadata["voice_generation"]["qa_state"] == "pending"
        assert c.db.connection.execute("SELECT COUNT(*) FROM execution_use_content_origins WHERE subject_kind='audio'").fetchone()[0] == 1
        return original(temp, destination)
    monkeypatch.setattr(c.importer, "_place", place)
    audio = execute(c)
    assert commercial_content_blocker(c.db, audio.content_hash) == "evaluation_scope_not_commercial"
    assert execute(c, replay=True).id == audio.id and c.calls == [1]
    assert ProviderCallRepository(c.db).list_for_project(c.job.project_id)[0].status == "completed"
    with pytest.raises(UseAdmissionError, match="evaluation_execution_not_integrated"):
        require_evaluation_lane_closed(c.db, c.job)


@pytest.mark.parametrize("change", ["text", "interval", "reference_text", "output"])
def test_child_request_must_match_persisted_job_before_call(bound_job, change):
    c = bound_job
    updates = {"text":dict(text="different copy"), "interval":dict(reference_start_frame=1),
        "reference_text":dict(reference_text="different reference"), "output":dict(output_path=str(c.outputs / "voice/other.wav"))}[change]
    c.options["inputs"] = c.options["inputs"].model_copy(update=updates)
    with pytest.raises(UseAdmissionError): execute(c)
    assert c.calls == [] and ProviderCallRepository(c.db).list_for_project(c.job.project_id) == []


@pytest.mark.parametrize("table,id_field", [("voice_profiles","voice"),("clips","clip"),("assets","asset")])
def test_source_change_during_estimate_cannot_reserve_or_generate(bound_job, table, id_field):
    c = bound_job
    c.cost.change = lambda: c.db.connection.execute(f"UPDATE {table} SET payload=payload || ' ' WHERE id=?", (str(getattr(c,id_field).id),))
    with pytest.raises(UseAdmissionError, match="source_changed"): execute(c)
    assert c.calls == [] and ProviderCallRepository(c.db).list_for_project(c.job.project_id) == []


def test_completed_replay_rejects_changed_bytes_without_second_call(bound_job):
    c = bound_job; audio = execute(c)
    Path(audio.source_file).write_bytes(b"changed")
    with pytest.raises(ProviderExecutionReplayUnavailable): execute(c, replay=True)
    assert c.calls == [1] and len(ProviderCallRepository(c.db).list_for_project(c.job.project_id)) == 1


def test_import_failure_rolls_back_visibility_and_marks_call_failed(bound_job, monkeypatch):
    c = bound_job
    monkeypatch.setattr(c.importer, "_place", lambda *a, **kw: (_ for _ in ()).throw(OSError("synthetic failed placement")))
    with pytest.raises(OSError): execute(c)
    assert c.db.connection.execute("SELECT COUNT(*) FROM audio_assets").fetchone()[0] == 0
    assert c.db.connection.execute("SELECT COUNT(*) FROM execution_use_content_origins WHERE subject_kind='audio'").fetchone()[0] == 0
    with pytest.raises(ProviderExecutionRecordedFailure): execute(c, replay=True)
    assert c.calls == [1]


def test_readonly_replay_preparation_cannot_launch(bound_job):
    c = bound_job
    with prepare_maintained_voice_child(**command_options(c.options), replay_only=True) as prepared:
        from app.providers.prepared import ExecutionPreparationError
        with pytest.raises(ExecutionPreparationError, match="replay_only"): prepared.execute()
    assert c.calls == []


@pytest.mark.parametrize("change", ["job", "project", "qa", "copy"])
def test_atomic_import_cannot_attach_wrong_job_or_quality_grant(bound_job, change):
    c = bound_job
    event = dict(job_id=str(c.job.id), project_id=str(c.job.project_id), qa_state="pending",
        voice_profile_id=str(c.voice.id), target_text=c.job.payload.text)
    key, value = {"job":("job_id",str(uuid4())), "project":("project_id",str(uuid4())),
        "qa":("qa_state","verified"), "copy":("target_text","unrelated")}[change]
    event[key] = value
    from app.media.audio_importer import AudioImportError
    with pytest.raises(AudioImportError):
        c.importer.import_path(c.options["inputs"].reference_audio,"synthetic", generated_job=c.job,
            generated_metadata={"voice_generation":event})
    assert c.db.connection.execute("SELECT COUNT(*) FROM audio_assets").fetchone()[0] == 0


def test_replay_revocation_is_not_a_generation_or_visibility_bypass(bound_job):
    c = bound_job; audio = execute(c)
    row = c.db.connection.execute("SELECT payload FROM provider_use_admissions WHERE id=?",(str(c.receipt),)).fetchone()
    record = json.loads(row[0]); record.update(revoked_at="2026-10-10T00:00:00Z",revoked_by="operator",revocation_reason="fixture revoke")
    c.db.connection.execute("UPDATE provider_use_admissions SET payload=? WHERE id=?",(json.dumps(record),str(c.receipt)))
    with pytest.raises(UseAdmissionError): execute(c,replay=True)
    assert c.calls == [1] and commercial_content_blocker(c.db,audio.content_hash) == "evaluation_scope_not_commercial"


def derived_reference(c, monkeypatch):
    tool = c.root / "ffmpeg-fixture.exe"
    if not tool.exists(): tool.write_bytes(b"synthetic tool")
    def run(command, **kwargs):
        assert not c.db.connection.in_transaction
        with wave.open(command[-1], "wb") as audio:
            audio.setparams((1,2,24000,0,"NONE","not compressed"))
            audio.writeframes(struct.pack("<h",1500)*72000)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(subprocess,"run",run)
    window = resolve_authorized_voice_reference_choice(c.job.payload.reference_window,
        authorized_voice_reference_choices(c.voice,[c.clip],{c.asset.id:c.asset},data_root=c.root))
    return prepare_voice_reference(data_root=c.root,staging_parent=c.options["staging_parent"],ffmpeg=tool,
        window=window,selection=c.job.payload.reference_window,inputs=c.options["inputs"])


@pytest.mark.parametrize("bound_job",["video"],indirect=True)
def test_derived_video_reference_commits_original_and_pcm_provenance_and_replays(bound_job,monkeypatch):
    c = bound_job
    def call(replay):
        with derived_reference(c,monkeypatch) as reference:
            inputs = reference.inputs_snapshot()
            with prepare_maintained_voice_child(**command_options(dict(c.options,inputs=inputs)),replay_only=replay) as prepared:
                return execute_maintained_voice_job(db=c.db,data_root=c.root,output_root=c.outputs,job=c.job,
                    prepared=prepared,execution_admission_id=c.receipt,importer=c.importer,cost_estimator=c.cost,
                    reference=reference)
    audio = call(False)
    event = audio.metadata["voice_generation"]
    assert event["reference_window"] == c.job.payload.reference_window.model_dump(mode="json")
    derivation = event["reference_derivation"]
    assert derivation["source_sha256"] == c.asset.content_hash
    assert derivation["source_sha256"] != derivation["derived_sha256"]
    assert derivation["frames"] == 72000 and "reference_audio" not in derivation
    assert call(True).id == audio.id and c.calls == [1]
    assert list(c.options["staging_parent"].iterdir()) == []
    assert commercial_content_blocker(c.db,audio.content_hash) == "evaluation_scope_not_commercial"


@pytest.mark.parametrize("bound_job",["video"],indirect=True)
@pytest.mark.parametrize("change",["tool","source"])
def test_derived_source_or_tool_drift_before_call_fails_without_generation(bound_job,monkeypatch,change):
    c = bound_job
    with derived_reference(c,monkeypatch) as reference:
        inputs = reference.inputs_snapshot()
        with prepare_maintained_voice_child(**command_options(dict(c.options,inputs=inputs))) as prepared:
            path = c.root / "ffmpeg-fixture.exe" if change == "tool" else Path(c.asset.source_file)
            c.cost.change = lambda: path.write_bytes(b"changed")
            from app.providers.prepared import ExecutionPreparationError
            with pytest.raises((UseAdmissionError,ExecutionPreparationError)):
                execute_maintained_voice_job(db=c.db,data_root=c.root,output_root=c.outputs,job=c.job,
                    prepared=prepared,execution_admission_id=c.receipt,importer=c.importer,cost_estimator=c.cost,
                    reference=reference)
    assert c.calls == [] and c.db.connection.execute("SELECT COUNT(*) FROM audio_assets").fetchone()[0] == 0


@pytest.mark.parametrize("bound_job",["video"],indirect=True)
def test_derived_reference_cannot_select_a_second_interval(bound_job,monkeypatch):
    c = bound_job
    with derived_reference(c,monkeypatch) as reference:
        inputs = reference.inputs_snapshot().model_copy(update=dict(reference_start_frame=1))
        with prepare_maintained_voice_child(**command_options(dict(c.options,inputs=inputs))) as prepared:
            from app.providers.prepared import ExecutionPreparationError
            with pytest.raises(ExecutionPreparationError,match="binding_changed"):
                execute_maintained_voice_job(db=c.db,data_root=c.root,output_root=c.outputs,job=c.job,
                    prepared=prepared,execution_admission_id=c.receipt,importer=c.importer,cost_estimator=c.cost,
                    reference=reference)
    assert c.calls == [] and ProviderCallRepository(c.db).list_for_project(c.job.project_id) == []


def configured(c,monkeypatch,**updates):
    tool = c.root / "configured-ffmpeg.exe"
    if not tool.exists(): tool.write_bytes(b"synthetic configured tool")
    def run(command,**kwargs):
        assert not c.db.connection.in_transaction
        with wave.open(command[-1],"wb") as audio:
            audio.setparams((1,2,24000,0,"NONE","not compressed"))
            audio.writeframes(struct.pack("<h",1500)*72000)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(subprocess,"run",run)
    options = {key:value for key,value in c.options.items() if key not in ("data_root","staging_parent","inputs","replay_only")}
    kwargs = dict(db=c.db,data_root=c.root,output_root=c.outputs,staging_parent=c.options["staging_parent"],
        ffmpeg=tool,job=c.job,execution_admission_id=c.receipt,importer=c.importer,cost_estimator=c.cost,
        runtime_options=options)
    kwargs.update(updates)
    return execute_configured_maintained_voice_job(**kwargs)


@pytest.mark.parametrize("bound_job",["video"],indirect=True)
def test_configured_entry_builds_job_inputs_and_ledger_driven_replay(bound_job,monkeypatch):
    c = bound_job
    def forbidden(*a, **kw): pytest.fail("full installation scan in normal Voice Job")
    monkeypatch.setattr("app.providers.maintained_voice_prepared._observe", forbidden)
    monkeypatch.setattr("app.providers.maintained_runtime._observe_inventory", forbidden)
    monkeypatch.setattr("app.providers.maintained_runtime._model_artifacts", forbidden)
    audio = configured(c,monkeypatch)
    assert "execution_sha256" not in audio.metadata["voice_generation"]
    assert audio.metadata["voice_generation"]["configuration_observation"] == "configured_local_process"
    assert audio.metadata["voice_generation"]["target_text"] == c.job.payload.text
    assert audio.metadata["voice_generation"]["reference_derivation"]["source_sha256"] == c.asset.content_hash
    assert configured(c,monkeypatch).id == audio.id and c.calls == [1]
    assert list(c.options["staging_parent"].iterdir()) == []
    with pytest.raises(UseAdmissionError,match="evaluation_execution_not_integrated"):
        require_evaluation_lane_closed(c.db,c.job)


@pytest.mark.parametrize("override",["inputs","data_root","replay_only","command"])
def test_configured_entry_rejects_caller_input_overrides_before_extraction(bound_job,monkeypatch,override):
    c = bound_job
    options = {key:value for key,value in c.options.items() if key not in ("data_root","staging_parent","inputs")}
    options[override] = "caller override"
    with pytest.raises(UseAdmissionError,match="configuration_invalid"):
        configured(c,monkeypatch,runtime_options=options)
    assert c.calls == [] and list(c.options["staging_parent"].iterdir()) == []


@pytest.mark.parametrize("change",["job","consent","admission","binding"])
def test_configured_entry_rechecks_current_authority_before_extraction(bound_job,monkeypatch,change):
    c = bound_job; updates = {}
    if change == "job": updates["job"] = c.job.model_copy(update=dict(status=JobStatus.PENDING))
    if change == "binding": updates["execution_admission_id"] = uuid4()
    if change == "consent":
        row = c.db.connection.execute("SELECT payload FROM voice_profiles WHERE id=?",(str(c.voice.id),)).fetchone()
        payload = json.loads(row[0]); payload["consent"]["confirmed"] = False
        c.db.connection.execute("UPDATE voice_profiles SET payload=? WHERE id=?",(json.dumps(payload),str(c.voice.id)))
    if change == "admission":
        row = c.db.connection.execute("SELECT payload FROM provider_use_admissions WHERE id=?",(str(c.receipt),)).fetchone()
        payload = json.loads(row[0]); payload.update(revoked_at="2026-10-10T00:00:00Z",revoked_by="operator",revocation_reason="fixture revoke")
        c.db.connection.execute("UPDATE provider_use_admissions SET payload=? WHERE id=?",(json.dumps(payload),str(c.receipt)))
    with pytest.raises(UseAdmissionError): configured(c,monkeypatch,**updates)
    assert c.calls == [] and list(c.options["staging_parent"].iterdir()) == []


@pytest.mark.parametrize("status",["reserved","running","failed"])
def test_configured_entry_stops_recorded_running_failure_before_preparation(bound_job,monkeypatch,status):
    c = bound_job; configured(c,monkeypatch)
    repository = ProviderCallRepository(c.db)
    record = repository.list_for_project(c.job.project_id)[0]
    repository.update(record.model_copy(update=dict(status=status,result_payload=None,completed_at=None)))
    error = ProviderExecutionInProgress if status in {"reserved","running"} else ProviderExecutionRecordedFailure
    with pytest.raises(error): configured(c,monkeypatch)
    assert c.calls == [1] and list(c.options["staging_parent"].iterdir()) == []


def test_configured_entry_cleans_reference_when_child_preparation_fails(bound_job,monkeypatch):
    c = bound_job
    options = {key:value for key,value in c.options.items() if key not in ("data_root","staging_parent","inputs")}
    options["operator_key"] = "invalid fixture operator"
    from app.providers.prepared import ExecutionPreparationError
    with pytest.raises(UseAdmissionError, match="operator_required"): configured(c,monkeypatch,runtime_options=options)
    assert c.calls == [] and list(c.options["staging_parent"].iterdir()) == []
