"""Bind the configured local Voice child to authorized Jobs, media and accounting."""
import hashlib
import json
from pathlib import Path
from uuid import UUID
import wave

from app.db import AssetRepository, AudioAssetRepository, ClipRepository, JobRepository, ProviderCallRepository, VoiceProfileRepository
from app.domain.models import CostCategory, JobStatus, JobType, VoiceGenerationJobPayload
from app.execution_admission import UseAdmissionError, configured_admission_actor
from app.execution_scope import ExecutionScopeService, job_use_identity
from app.frozen_worker import _facts
from app.provider_execution import ProviderExecutionInProgress, ProviderExecutionRecordedFailure, ProviderExecutionService, RuntimeProviderIdentity
from app.providers.maintained_voice_execution import PreparedMaintainedVoiceChild, prepare_maintained_voice_child
from app.providers.omnivoice_entry_v2 import LocalCloneInputV2
from app.voice_delivery import validate_voice_delivery_text
from app.voice_performance import authorized_voice_reference_choices, resolve_authorized_voice_reference_choice
from app.voice_observation import _resolve_source_path
from app.media.voice_reference import OwnedVoiceReference, prepare_voice_reference
from app.providers.runtime_tree import _exact_path


def _source_facts(db, profile_id, selection):
    rows = []
    for table, identity in (("voice_profiles", profile_id), ("clips", selection.clip_id), ("assets", selection.asset_id)):
        row = db.connection.execute(f"SELECT * FROM {table} WHERE id=?", (str(identity),)).fetchone()
        if row is None: raise UseAdmissionError("maintained_voice_source_missing")
        rows.append(tuple(row))
    return tuple(rows)


def _selected_job_source(db, root, job):
    if db.connection.in_transaction:
        raise UseAdmissionError("execution_observation_inside_transaction")
    stored = JobRepository(db).get(job.id)
    if (stored is None or job.status != JobStatus.RUNNING or stored.status != JobStatus.RUNNING
        or stored.attempt != job.attempt or job_use_identity(stored) != job_use_identity(job)
        or job.type != JobType.GENERATE_VOICE or not isinstance(job.payload, VoiceGenerationJobPayload)
        or job.project_id != job.payload.project_id):
        raise UseAdmissionError("maintained_voice_job_changed")
    try:
        profile = VoiceProfileRepository(db).get(job.payload.voice_profile_id)
    except ValueError:
        raise UseAdmissionError("voice_profile_not_authorized") from None
    if profile is None or not profile.consent.confirmed:
        raise UseAdmissionError("voice_profile_not_authorized")
    payload = job.payload
    if (payload.execution_capability_profile_id is None or payload.expected_provider != profile.provider
        or payload.expected_model is None or payload.narration_performance_plan is not None):
        raise UseAdmissionError("maintained_voice_execution_pin_required")
    if payload.delivery_text is not None: validate_voice_delivery_text(payload.text, payload.delivery_text)
    if payload.reference_window is None:
        raise UseAdmissionError("maintained_voice_reference_selection_required")
    clips = [value for identity in profile.reference_clip_ids if (value := ClipRepository(db).get(identity)) is not None]
    assets = {value.id: value for clip in clips if (value := AssetRepository(db).get(clip.asset_id)) is not None}
    try:
        window = resolve_authorized_voice_reference_choice(payload.reference_window,
            authorized_voice_reference_choices(profile, clips, assets, data_root=root))
    except ValueError:
        raise UseAdmissionError("maintained_voice_reference_changed") from None
    return profile, window


def _bind(db, root, output_root, job, prepared, reference=None):
    profile, window = _selected_job_source(db, root, job)
    if type(prepared) is not PreparedMaintainedVoiceChild:
        raise UseAdmissionError("maintained_voice_child_required")
    request = prepared.request_snapshot()
    scope = ExecutionScopeService(db, root).require_job_snapshot(job)
    spec = next(b.execution_specification for b in scope.bindings
        if b.capability_profile_id == job.payload.execution_capability_profile_id)
    payload = job.payload
    if (payload.expected_provider != spec.provider or payload.expected_model != spec.model
            or Path(request.model_root).resolve() != Path(spec.model).resolve()):
        raise UseAdmissionError("maintained_voice_execution_pin_required")
    source = Path(window.source_file).resolve()
    target = Path(output_root) / "voice" / f"{job.id}-attempt-{job.attempt}.wav"
    language = payload.language or profile.language
    language = {"zh": "Chinese", "en": "English"}.get(language, language)
    if (Path(request.data_root) != root or not source.is_relative_to(root) or not target.is_relative_to(root)
        or Path(request.inputs.output_path) != target or request.inputs.text != (payload.delivery_text or payload.text).strip()
        or request.inputs.language != language or request.inputs.reference_text != window.transcript.strip()):
        raise UseAdmissionError("maintained_voice_job_input_mismatch")
    derivation = None
    if reference is not None:
        if type(reference) is not OwnedVoiceReference:
            raise UseAdmissionError("voice_reference_owned_required")
        derivation = reference.verify_for(window, payload.reference_window, request.inputs)
    else:
        if (Path(request.inputs.reference_audio) != source
            or request.inputs.reference_sha256 != payload.reference_window.source_content_hash):
            raise UseAdmissionError("maintained_voice_job_input_mismatch")
        try:
            with wave.open(str(source), "rb") as audio:
                rate = audio.getframerate()
                if (audio.getnchannels(), audio.getsampwidth()) != (1, 2): raise ValueError()
        except (OSError, ValueError, wave.Error):
            raise UseAdmissionError("maintained_voice_reference_pcm_required") from None
        if (request.inputs.reference_start_frame != window.start_ms * rate // 1000
            or request.inputs.reference_end_frame != window.end_ms * rate // 1000):
            raise UseAdmissionError("maintained_voice_reference_interval_mismatch")
    facts = _source_facts(db, profile.id, payload.reference_window)
    semantic = request.model_dump(mode="json")
    # Private request paths are execution details; bind source hash/window and
    # Job identity instead, so regenerated private staging does not invent a call.
    semantic["inputs"].pop("reference_audio")
    semantic["inputs"].pop("output_path")
    call = prepared.invocation
    semantic["command"] = dict(python=call.argv[0], timeout_seconds=call.timeout_seconds,
        environment=dict(call.environment))
    # Private temporary/cache paths vary; only stable command settings bind replay.
    for key in ("TEMP", "TMP", "HF_HOME"):
        semantic["command"]["environment"].pop(key, None)
    semantic.update(job_sha256=job_use_identity(job), voice_profile_sha256=hashlib.sha256(profile.model_dump_json().encode()).hexdigest())
    if derivation is not None: semantic["reference_derivation"] = derivation
    return profile, facts, semantic


def execute_maintained_voice_job(*, db, data_root, output_root, job, prepared,
        execution_admission_id, importer, cost_estimator, reference=None):
    root, outputs = Path(data_root).resolve(), Path(output_root).resolve()
    if importer.db is not db or Path(importer.data_root).resolve() != root or getattr(db, "data_root", None) != root:
        raise UseAdmissionError("execution_frozen_database_mismatch")
    profile, facts, semantic = _bind(db, root, outputs, job, prepared, reference)
    service = ExecutionScopeService(db, root)
    scope = service.require_job_snapshot(job)
    binding = next((b for b in scope.bindings if b.admission_id == execution_admission_id), None)
    if binding is None or binding.capability_profile_id != job.payload.execution_capability_profile_id:
        raise UseAdmissionError("execution_admission_binding_mismatch")
    spec = binding.execution_specification
    if prepared.request_snapshot().parameters.model_dump() != spec.parameters:
        raise UseAdmissionError("maintained_voice_configuration_changed")
    service.require_current(scope, project_id=job.project_id, purpose="internal_evaluation", operation="generation")
    authority = _facts(service, job, scope)
    provider = RuntimeProviderIdentity(spec.provider, spec.model)
    def guard():
        if (_source_facts(db, profile.id, job.payload.reference_window) != facts
                or _facts(service, job, scope) != authority):
            raise UseAdmissionError("maintained_voice_source_changed")
    def current():
        guard()
        service.require_current(scope, project_id=job.project_id, purpose="internal_evaluation", operation="generation")
        if _bind(db, root, outputs, job, prepared, reference)[2] != semantic:
            raise UseAdmissionError("maintained_voice_job_input_mismatch")
    input_sha = hashlib.sha256(json.dumps(semantic, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    event = dict(provider=provider.provider, model=provider.model, job_id=str(job.id), project_id=str(job.project_id),
        attempt=job.attempt, voice_profile_id=str(profile.id), target_text=job.payload.text, qa_state="pending",
        bound_input_sha256=input_sha, configuration_observation="configured_local_process",
        reference_window=job.payload.reference_window.model_dump(mode="json"))
    from app.voice_calibration import calibration_receipts, POLICY as CALIBRATION
    if calibration_receipts(db, service.require_job_snapshot(job)):
        event["admission_policy"] = CALIBRATION
    if reference is not None: event["reference_derivation"] = semantic["reference_derivation"]
    def action():
        path = prepared.execute()
        current()
        audio = importer.import_path(path, job.payload.authorization_reference,
            language=job.payload.language or profile.language, generated_job=job,
            generated_metadata={"voice_generation": event})
        if audio.metadata.get("voice_generation") != event:
            raise UseAdmissionError("maintained_voice_output_provenance_conflict")
        return audio
    def decode(value):
        if not isinstance(value, dict) or value.get("job_id") != str(job.id) or value.get("bound_input_sha256") != input_sha:
            raise UseAdmissionError("maintained_voice_replay_mismatch")
        audio = AudioAssetRepository(db).get(UUID(value["audio_id"]))
        if audio is None or audio.content_hash != value.get("content_hash"):
            raise UseAdmissionError("maintained_voice_replay_unavailable")
        provenance = audio.metadata.get("voice_generation", {})
        if any(provenance.get(key) != event[key] for key in ("job_id", "project_id", "configuration_observation", "bound_input_sha256")):
            raise UseAdmissionError("maintained_voice_replay_mismatch")
        scope = service.get("audio", audio.id)
        if scope is None: raise UseAdmissionError("maintained_voice_output_scope_missing")
        service.require_current(scope, project_id=job.project_id, purpose="internal_evaluation", operation="generation")
        path = _resolve_source_path(audio.source_file, root).resolve()
        if (not path.is_relative_to(root) or not path.is_file()
            or path.stat().st_size > spec.parameters["output_max_bytes"]
            or hashlib.sha256(path.read_bytes()).hexdigest() != audio.content_hash):
            raise UseAdmissionError("maintained_voice_replay_bytes_changed")
        return audio
    return ProviderExecutionService(db, cost_estimator).execute(project_id=job.project_id,
        idempotency_key=f"maintained-voice:{job.id}", operation="tts", provider=provider,
        input_source="maintained-voice-job", category=CostCategory.VOICE,
        input_document=semantic, reservation_guard=guard, before_action=current, before_replay=current,
        action=action, encode_result=lambda audio: dict(audio_id=str(audio.id), content_hash=audio.content_hash,
            job_id=str(job.id), bound_input_sha256=input_sha), decode_result=decode,
        error_code=lambda error: str(error) if isinstance(error, UseAdmissionError) else "maintained_voice_job_failed")


def execute_configured_maintained_voice_job(*, db, data_root, output_root, staging_parent, ffmpeg,
        job, execution_admission_id, importer, cost_estimator, runtime_options):
    """Server application entry. Job owns media/text; ledger owns replay mode.

    Runtime options are operator configuration, never a request body.
    Deployment evidence grants use, not a fresh observation of runtime files.
    """
    required = {"environment", "base", "model_root", "origin_reference", "operator_key", "parameters",
        "installation_id", "runtime_label", "machine_id"}
    allowed = required | {"gpu_uuid", "max_bytes", "timeout_seconds"}
    if not isinstance(runtime_options, dict) or not required <= runtime_options.keys() or runtime_options.keys() - allowed:
        raise UseAdmissionError("maintained_voice_configuration_invalid")
    root, outputs, staging = Path(data_root).resolve(), Path(output_root).resolve(), Path(staging_parent)
    if (getattr(db,"data_root",None) != root or importer.db is not db or Path(importer.data_root).resolve() != root
        or not outputs.is_relative_to(root) or not staging.is_relative_to(root)):
        raise UseAdmissionError("execution_frozen_database_mismatch")
    _exact_path(root,directory=True); _exact_path(outputs,directory=True); _exact_path(staging,directory=True)
    profile, window = _selected_job_source(db,root,job)
    scopes = ExecutionScopeService(db,root)
    scope = scopes.require_job_snapshot(job)
    if scope is None or scope.purpose != "internal_evaluation" or scope.policy_version != 2:
        raise UseAdmissionError("execution_use_scope_required")
    binding = next((value for value in scope.bindings if value.admission_id == execution_admission_id),None)
    if binding is None or binding.capability_profile_id != job.payload.execution_capability_profile_id:
        raise UseAdmissionError("execution_admission_binding_mismatch")
    scopes.require_current(scope,project_id=job.project_id,purpose=scope.purpose,operation="generation")
    for dependency in scopes.scopes_for_hash(job.payload.reference_window.source_content_hash):
        scopes.require_current(dependency,project_id=job.project_id,purpose=scope.purpose,operation="derivation")
    existing = ProviderCallRepository(db).get_by_idempotency_key(f"maintained-voice:{job.id}")
    if existing is not None and existing.status in {"reserved", "running"}:
        raise ProviderExecutionInProgress("provider call is already running")
    if existing is not None and existing.status != "completed":
        raise ProviderExecutionRecordedFailure("provider call previously failed")
    output = outputs / "voice" / f"{job.id}-attempt-{job.attempt}.wav"
    output.parent.mkdir(exist_ok=True)
    _exact_path(output.parent,directory=True)
    language = job.payload.language or profile.language
    inputs = LocalCloneInputV2(text=(job.payload.delivery_text or job.payload.text).strip(),
        reference_audio=str(Path(window.source_file).resolve()),reference_text=window.transcript.strip(),
        language={"zh":"Chinese","en":"English"}.get(language,language),output_path=str(output),
        reference_sha256=job.payload.reference_window.source_content_hash,reference_start_frame=0,
        reference_end_frame=(window.end_ms-window.start_ms)*24,reference_max_frames=32*1024**2,
        reference_max_bytes=64*1024**2)
    with prepare_voice_reference(data_root=root,staging_parent=staging,ffmpeg=ffmpeg,window=window,
            selection=job.payload.reference_window,inputs=inputs) as reference:
        # The admission specification is a deployment/license record, not a fresh V6 observation.
        spec = binding.execution_specification
        origin = json.loads(scopes.admissions._file(spec.runtime_origin.reference))
        if configured_admission_actor(runtime_options["operator_key"]) != origin["actor"]:
            raise UseAdmissionError("maintained_generation_operator_required")
        if (Path(runtime_options["model_root"]).resolve() != Path(spec.model).resolve()
                or runtime_options["parameters"] != spec.parameters
                or runtime_options.get("gpu_uuid") != spec.host_runtime.gpu_uuid
                or runtime_options.get("timeout_seconds", 600) != spec.timeout_seconds
                or str(runtime_options["installation_id"]) != str(spec.machine.installation_id)
                or runtime_options["runtime_label"] != spec.runtime or runtime_options["machine_id"] != spec.machine_id):
            raise UseAdmissionError("maintained_voice_configuration_changed")
        command_options = {k: v for k, v in runtime_options.items()
            if k in {"environment", "base", "model_root", "parameters", "timeout_seconds", "gpu_uuid"}}
        with prepare_maintained_voice_child(**command_options,data_root=root,staging_parent=staging,
                inputs=reference.inputs_snapshot(),replay_only=existing is not None) as prepared:
            return execute_maintained_voice_job(db=db,data_root=root,output_root=outputs,job=job,
                prepared=prepared,execution_admission_id=execution_admission_id,importer=importer,
                cost_estimator=cost_estimator,reference=reference)
