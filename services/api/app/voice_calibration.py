"""Immutable once-only application claim; no runtime or quality grant."""
from app.domain.models import ProviderUseAdmission, ProviderUseLicenseReviewV3, JobType, execution_canonical_sha256
from app.execution_admission import UseAdmissionError

POLICY = "voice-calibration-v1"
REVIEW_POLICY = "completed-voice-calibration-review-v1"


def request_digest(payload):
    return execution_canonical_sha256({"request_version": 1, "project_id": str(payload.project_id),
        "payload": payload.model_dump(mode="json")})


def calibration_receipts(db, scope):
    values = []
    for binding in scope.bindings:
        row = db.connection.execute("SELECT payload FROM provider_use_admissions WHERE id=? AND project_id=?",
            (str(binding.admission_id), str(scope.project_id))).fetchone()
        if row is None:
            raise UseAdmissionError("provider_use_admission_missing")
        try:
            receipt = ProviderUseAdmission.model_validate_json(row[0])
        except ValueError:
            raise UseAdmissionError("provider_use_binding_changed") from None
        if type(receipt.review) is ProviderUseLicenseReviewV3:
            values.append(receipt)
    return values


def require_claim(db, job, scope):
    receipts = calibration_receipts(db, scope)
    if not receipts:
        return ()
    if len(receipts) != 1 or job.type != JobType.GENERATE_VOICE or scope.purpose != "internal_evaluation":
        raise UseAdmissionError("voice_calibration_scope_unsupported")
    receipt = receipts[0]
    from app.execution_scope import job_use_identity
    marker = db.connection.execute("SELECT * FROM voice_draft_jobs WHERE job_id=?", (str(job.id),)).fetchone()
    claim = db.connection.execute("SELECT * FROM voice_calibration_claims WHERE admission_id=?", (str(receipt.id),)).fetchone()
    if (marker is None or marker["policy"] != POLICY or claim is None
        or claim["job_id"] != str(job.id) or claim["request_sha256"] != request_digest(job.payload)
        or claim["request_sha256"] != receipt.review.request_sha256
        or claim["job_sha256"] != job_use_identity(job) or claim["scope_fingerprint"] != scope.fingerprint
        or marker["project_id"] != str(job.project_id) or marker["job_sha256"] != claim["job_sha256"]
        or marker["scope_fingerprint"] != scope.fingerprint
        or receipt.review.capability_profile_id != job.payload.execution_capability_profile_id):
        raise UseAdmissionError("voice_calibration_claim_invalid")
    if db.connection.execute("SELECT 1 FROM production_runs WHERE voice_job_id=?", (str(job.id),)).fetchone():
        raise UseAdmissionError("voice_draft_production_unsupported")
    return (tuple(claim),)


def require_output(db, audio, job, scope):
    if not calibration_receipts(db, scope):
        return
    require_claim(db, job, scope)
    from app.db import ProviderCallRepository
    call = ProviderCallRepository(db).get_by_idempotency_key(f"maintained-voice:{job.id}")
    value = (call.result_payload or {}).get("result") if call and call.status == "completed" else None
    generation = audio.metadata.get("voice_generation", {})
    if (not isinstance(value, dict) or value.get("job_id") != str(job.id) or value.get("audio_id") != str(audio.id)
        or value.get("content_hash") != audio.content_hash or generation.get("admission_policy") != POLICY
        or generation.get("qa_state") != "pending"
        or generation.get("bound_input_sha256") != value.get("bound_input_sha256")):
        raise UseAdmissionError("voice_calibration_output_unbound")


def _retained_completed_report(service, receipt):
    """Private completed-output evidence reader; never adoption/dispatch eligibility."""
    import hashlib
    from app.domain.models import ExecutionSpecificationV6
    from app.domain.maintained_runtime import MaintainedGenerationOrigin
    from app.providers.maintained_runtime import _require_origin
    from app.providers.maintained_voice_prepared import require_voice_specification
    from app.providers.maintained_voice_bundle import BUNDLE_SLOT, BUNDLE_MAX_BYTES
    from app.runtime_inventory import RuntimeInventoryStore
    raw = service._file(receipt.review_reference, maximum=100_000)
    if hashlib.sha256(raw).hexdigest() != receipt.review_sha256:
        raise UseAdmissionError("provider_use_review_hash_changed")
    report = ProviderUseLicenseReviewV3.model_validate_json(raw)
    if report != receipt.review or type(report.execution_specification) is not ExecutionSpecificationV6:
        raise UseAdmissionError("provider_use_binding_changed")
    spec = report.execution_specification
    require_voice_specification(spec)
    inventory = RuntimeInventoryStore(service.root).read_v2(spec.runtime_inventory)
    _require_origin(service.root, spec.runtime_origin, inventory, MaintainedGenerationOrigin)
    entries = [entry for entry in inventory.files if entry.role == "entrypoint"]
    if (len(entries) != 1 or entries[0].name != BUNDLE_SLOT
            or not 0 < entries[0].size_bytes <= BUNDLE_MAX_BYTES):
        raise UseAdmissionError("maintained_voice_child_evidence_invalid")
    terms = service._file(report.license_terms_reference)
    if not terms or hashlib.sha256(terms).hexdigest() != report.license_terms_sha256:
        raise UseAdmissionError("provider_use_terms_hash_changed")
    return report


def require_completed_review(db, root, audio, job, producer, scopes, *, project_id, purpose):
    """Exact completed calibration output only; all current review restrictions apply."""
    from pathlib import Path
    from app.domain.models import JobStatus, VoiceGenerationJobPayload
    from app.db import ProviderCallRepository, VoiceProfileRepository, ClipRepository, AssetRepository, ProviderMachineCapabilityProfileRepository
    from app.execution_scope import ExecutionScopeService, ExecutionUseBindingV2, _make_scope
    from app.execution_admission import resolve_provider_use
    from app.voice_performance import authorized_voice_reference_choices, resolve_authorized_voice_reference_choice
    from app.media.local_paths import existing_local_media_file
    from app.media.audio_access import file_digest
    from app.providers.prepared import ExecutionPreparationError
    from app.runtime_inventory import RuntimeInventoryError
    if (job.status != JobStatus.COMPLETED or job.type != JobType.GENERATE_VOICE
            or not isinstance(job.payload, VoiceGenerationJobPayload)
            or project_id != job.project_id or job.payload.project_id != project_id
            or purpose != "internal_evaluation" or producer.policy_version != 2):
        raise UseAdmissionError("voice_completed_review_unsupported")
    receipts = calibration_receipts(db, producer)
    if len(receipts) != 1:
        raise UseAdmissionError("voice_completed_review_unsupported")
    receipt = receipts[0]
    matching = [binding for binding in producer.bindings if binding.admission_id == receipt.id]
    if len(matching) != 1:
        raise UseAdmissionError("provider_use_binding_changed")
    binding = matching[0]
    if (not isinstance(binding, ExecutionUseBindingV2)
            or binding.review_sha256 != receipt.review_sha256
            or binding.configuration_sha256 != receipt.review.configuration_sha256
            or binding.identity != receipt.review.identity
            or binding.execution_specification != receipt.review.execution_specification
            or binding.execution_sha256 != receipt.review.execution_sha256):
        raise UseAdmissionError("provider_use_binding_changed")
    require_output(db, audio, job, producer)
    spec = receipt.review.execution_specification
    call = ProviderCallRepository(db).get_by_idempotency_key(f"maintained-voice:{job.id}")
    generation = audio.metadata.get("voice_generation", {})
    expected = dict(job_id=str(job.id), project_id=str(project_id), attempt=job.attempt,
        provider=spec.provider, model=spec.model, execution_sha256=spec.execution_sha256,
        voice_profile_id=str(job.payload.voice_profile_id), target_text=job.payload.text,
        reference_window=job.payload.reference_window.model_dump(mode="json") if job.payload.reference_window else None,
        configuration_observation="execution_observed")
    if (call.project_id != project_id or call.mode != "runtime" or call.operation != "tts"
            or call.input_source != "maintained-voice-job"
            or call.provider != spec.provider or call.model != spec.model
            or any(generation.get(key) != value for key, value in expected.items())
            or not generation.get("bound_input_sha256")
            or audio.authorization_reference != job.payload.authorization_reference):
        raise UseAdmissionError("voice_calibration_output_unbound")
    service = ExecutionScopeService(db, root)
    try:
        _retained_completed_report(service.admissions, receipt)
    except (ValueError, RuntimeInventoryError, ExecutionPreparationError) as exc:
        if isinstance(exc, UseAdmissionError):
            raise
        raise UseAdmissionError("voice_completed_review_evidence_invalid") from None
    # Only this exact binding receives historical execution validation. Every
    # other binding still resolves through the default current-evidence path.
    for scope in scopes:
        if scope.project_id != project_id or scope.purpose != purpose:
            raise UseAdmissionError("provider_use_project_mismatch")
        for value in scope.bindings:
            if value.admission_id == receipt.id:
                if value != binding:
                    raise UseAdmissionError("provider_use_binding_changed")
                profile = ProviderMachineCapabilityProfileRepository(db).get(value.capability_profile_id)
                decision = resolve_provider_use(project_id=project_id, capability=profile,
                    configuration_sha256=None if profile is None else service.admissions._configuration(profile),
                    identity=value.identity, purpose=purpose, operation="review", admission=receipt, evidence_current=True)
                if decision.state != "allowed_for_scope":
                    raise UseAdmissionError(decision.reasons[0])
            else:
                service.require_current(_make_scope(scope.project_id, scope.purpose, (value,)),
                    project_id=project_id, purpose=purpose, operation="review")
    profile = VoiceProfileRepository(db).get(job.payload.voice_profile_id)
    if profile is None or not profile.consent.confirmed or profile.provider != spec.provider:
        raise UseAdmissionError("voice_profile_not_authorized")
    clips = [clip for identity in profile.reference_clip_ids if (clip := ClipRepository(db).get(identity))]
    assets = {asset.id: asset for clip in clips if (asset := AssetRepository(db).get(clip.asset_id))}
    try:
        window = resolve_authorized_voice_reference_choice(job.payload.reference_window,
            authorized_voice_reference_choices(profile, clips, assets, data_root=root))
        source = existing_local_media_file(window.source_file, Path(root))
        if not source.is_relative_to(Path(root).resolve()) or file_digest(source) != job.payload.reference_window.source_content_hash:
            raise ValueError()
    except (ValueError, OSError):
        raise UseAdmissionError("maintained_voice_reference_changed") from None
    service.require_content(job.payload.reference_window.source_content_hash,
        project_id=project_id, purpose=purpose, operation="derivation")
    return REVIEW_POLICY
