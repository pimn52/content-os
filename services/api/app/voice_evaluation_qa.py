"""Once-only evaluation QA application service, separate from original Voice grant."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID
from typing import Literal
from pydantic import BaseModel, ConfigDict
from app.db import (AudioAssetRepository, JobRepository, ProviderCallRepository,
    ProviderMachineCapabilityProfileRepository, ProjectRepository)
from app.domain.models import (AudioAsset, Job, JobStatus, JobType, VoiceQaJobPayload,
    ProviderUseLicenseReviewV4, VoiceQAInputBinding, Sha256, VoiceGenerationJobPayload,
    VoiceReferenceWindowSelection, execution_canonical_sha256)
from app.domain.asr_child import ASRChildRequest, ASRChildResult
from app.domain.asr_recipe import ASR_PARAMETERS
from app.execution_admission import UseAdmissionError, resolve_provider_use
from app.execution_scope import ExecutionScopeService, job_use_identity
from app.media.local_paths import existing_local_media_file
from app.media.audio_access import file_digest
from app.voice_qa import verify_generated_voice, apply_voice_qa

POLICY = "voice-evaluation-qa-v1"
REVIEW_POLICY = "completed-voice-calibration-review-v2"
CALL_SOURCE = "voice-evaluation-qa-job"

class QAClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    policy: Literal["voice-evaluation-qa-v1"]
    project_id: UUID
    admission_id: UUID
    job_id: UUID
    job_sha256: Sha256
    scope_fingerprint: Sha256
    request_sha256: Sha256
    input_binding: VoiceQAInputBinding
    original_audio: AudioAsset
    execution_sha256: Sha256

    def canonical_request(self):
        return dict(policy=POLICY, project_id=str(self.project_id), admission_id=str(self.admission_id),
            audio_id=str(self.input_binding.audio_id), target_text=self.original_audio.metadata["voice_generation"]["target_text"])


def retained_claim(db, audio_id):
    row = db.connection.execute("SELECT * FROM voice_evaluation_qa_claims WHERE audio_id=?", (str(audio_id),)).fetchone()
    if row is None:
        return None
    try:
        claim = QAClaim.model_validate_json(row["payload"])
        if (claim.policy != POLICY or str(claim.admission_id) != row["admission_id"]
            or str(claim.job_id) != row["job_id"] or str(claim.input_binding.audio_id) != row["audio_id"]
            or claim.job_sha256 != row["job_sha256"] or claim.scope_fingerprint != row["scope_fingerprint"]
            or claim.request_sha256 != row["request_sha256"] or claim.original_audio.id != claim.input_binding.audio_id
            or claim.original_audio.content_hash != claim.input_binding.audio_sha256
            or claim.request_sha256 != execution_canonical_sha256(claim.canonical_request())):
            raise ValueError()
        return claim
    except (ValueError, KeyError, TypeError):
        raise UseAdmissionError("voice_qa_claim_invalid") from None


def has_qa_reference(db, audio):
    if db.connection.execute("SELECT 1 FROM voice_evaluation_qa_claims WHERE audio_id=?", (str(audio.id),)).fetchone():
        return True
    if "voice_evaluation_qa" in audio.metadata:
        return True
    # A deleted claim or stripped metadata cannot downgrade completed proof to v1.
    for call in ProviderCallRepository(db).list_all():
        if call.input_source == CALL_SOURCE:
            value = (call.result_payload or {}).get("result", {})
            if isinstance(value, dict) and value.get("audio_id") == str(audio.id):
                return True
            try:
                qa_job = JobRepository(db).get(UUID(call.idempotency_key.removeprefix("voice-evaluation-qa:")))
                if qa_job and isinstance(qa_job.payload, VoiceQaJobPayload) and qa_job.payload.narration_audio_id == audio.id:
                    return True
            except ValueError:
                pass
    return False


def _voice_job_original_proof_unchecked(db, root, audio, *, project_id):
    from app.voice_calibration import require_completed_review
    generation = audio.metadata.get("voice_generation", {})
    try:
        producer = JobRepository(db).get(UUID(generation["job_id"]))
    except (KeyError, TypeError, ValueError):
        raise UseAdmissionError("voice_qa_original_producer_missing") from None
    scopes = ExecutionScopeService(db, root)
    source_scope = scopes.get("audio", audio.id)
    producer_scope = None if producer is None else scopes.require_job_snapshot(producer)
    if source_scope is None or producer_scope is None:
        raise UseAdmissionError("execution_use_origin_missing")
    lineage = list(scopes.scopes_for_hash(audio.content_hash))+[source_scope, producer_scope]
    if any(b not in source_scope.bindings for b in producer_scope.bindings):
        raise UseAdmissionError("execution_use_ancestor_dropped")
    rows = db.connection.execute("SELECT content_hash FROM execution_use_content_origins WHERE subject_kind='audio' AND subject_id=?",
        (str(audio.id),)).fetchall()
    if not rows or any(row[0] != audio.content_hash for row in rows):
        raise UseAdmissionError("execution_use_origin_missing")
    require_completed_review(db, root, audio, producer, producer_scope, lineage,
        project_id=project_id, purpose="internal_evaluation")
    source = existing_local_media_file(audio.source_file, Path(root))
    if not source.is_relative_to(Path(root).resolve()) or file_digest(source) != audio.content_hash:
        raise UseAdmissionError("execution_use_content_changed")
    binding = VoiceQAInputBinding(audio_id=audio.id, audio_sha256=audio.content_hash,
        producer_job_id=producer.id, producer_job_sha256=job_use_identity(producer),
        producer_metadata_sha256=execution_canonical_sha256(generation),
        original_scope_fingerprint=source_scope.fingerprint,
        original_admissions=tuple(sorted({(b.admission_id, b.review_sha256) for s in lineage for b in s.bindings}, key=lambda r:str(r[0]))),
        target_text_sha256=hashlib.sha256(producer.payload.text.encode()).hexdigest(),
        language=producer.payload.language or audio.language or "")
    return binding, source


def _composed_source_scope(db, root, project_id, source_ids):
    from app.execution_scope import _make_scope
    service = ExecutionScopeService(db, root)
    bindings = {}
    for audio_id in source_ids:
        scope = service.get("audio", audio_id)
        if scope is None or scope.project_id != project_id or scope.purpose != "internal_evaluation":
            raise UseAdmissionError("voice_qa_composition_source_scope_missing")
        for binding in scope.bindings:
            previous = bindings.get(binding.admission_id)
            if previous is not None and previous != binding:
                raise UseAdmissionError("provider_use_binding_changed")
            bindings[binding.admission_id] = binding
    if not bindings:
        raise UseAdmissionError("voice_qa_composition_source_scope_missing")
    scope = _make_scope(project_id, "internal_evaluation",
        tuple(bindings[key] for key in sorted(bindings, key=str)))
    service.require_current(scope, project_id=project_id, purpose="internal_evaluation", operation="review")
    return scope


def _composed_original_proof_unchecked(db, root, audio, *, project_id):
    from app.voice_qa import voice_copy_adjudication_is_valid, voice_take_copy_accepted
    from app.voice_plan import resolve_project_voice_profile

    generation = audio.metadata.get("voice_generation")
    composition = generation.get("composition") if isinstance(generation, dict) else None
    if (not isinstance(generation, dict) or generation.get("provider") != "composed_voice_takes"
        or generation.get("model") != "provider_neutral_composition"
        or generation.get("project_id") != str(project_id) or generation.get("job_id") is not None
        or not isinstance(composition, dict) or generation.get("qa_state") not in {"pending", "verified", "failed"}):
        raise UseAdmissionError("voice_qa_composition_origin_invalid")
    try:
        source_ids = tuple(UUID(value) for value in composition["source_take_audio_ids"])
        source_hashes = tuple(composition["source_take_content_hashes"])
        target_text = generation["target_text"]
        if (not source_ids or len(source_ids) > 100 or len(source_ids) != len(source_hashes)
            or len(set(source_ids)) != len(source_ids) or not isinstance(target_text, str) or not target_text.strip()
            or composition.get("timing_state") != "provisional_pending_master_voice_qa"
            or composition.get("expected_duration_ms") != audio.duration_ms):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise UseAdmissionError("voice_qa_composition_origin_invalid") from None

    source_repo = AudioAssetRepository(db)
    sources = []
    target_parts = []
    adjudicated_ids = []
    voice_signature = None
    first_job = None
    scopes = ExecutionScopeService(db, root)
    from app.domain.models import VoiceGenerationJobPayload

    for audio_id, content_hash in zip(source_ids, source_hashes):
        if not isinstance(content_hash, str) or len(content_hash) != 64:
            raise UseAdmissionError("voice_qa_composition_source_changed")
        current = source_repo.get(audio_id)
        source_claim = retained_claim(db, audio_id)
        if (current is None or current.content_hash != content_hash or source_claim is None
            or source_claim.project_id != project_id or source_claim.original_audio.id != audio_id
            or source_claim.original_audio.content_hash != content_hash):
            raise UseAdmissionError("voice_qa_composition_source_changed")

        # Verify the producer against its pre-QA output snapshot. Independent
        # QA and owner homophone decisions remain separate immutable overlays.
        source_binding, source_path = _voice_job_original_proof_unchecked(
            db, root, source_claim.original_audio, project_id=project_id)
        source_job = JobRepository(db).get(source_binding.producer_job_id)
        if (source_job is None or not isinstance(source_job.payload, VoiceGenerationJobPayload)
            or current.metadata.get("voice_generation", {}).get("target_text") != source_job.payload.text):
            raise UseAdmissionError("voice_qa_composition_source_job_changed")
        require_completed_qa(db, root, current, project_id=project_id, purpose="internal_evaluation")
        if not voice_take_copy_accepted(current, project_id=project_id):
            raise UseAdmissionError("voice_qa_composition_source_copy_unaccepted")
        if voice_copy_adjudication_is_valid(current, project_id=project_id):
            adjudicated_ids.append(str(audio_id))

        ref = VoiceReferenceWindowSelection.model_validate(source_job.payload.reference_window)
        reference_identity = json.dumps({
            "clip_id": str(ref.clip_id), "asset_id": str(ref.asset_id),
            "source_content_hash": ref.source_content_hash, "source_timebase": ref.source_timebase,
            "asset_content_hash": ref.asset_content_hash,
        }, sort_keys=True, separators=(",", ":"))
        signature = (str(source_job.payload.voice_profile_id), current.metadata["voice_generation"].get("provider"),
            current.metadata["voice_generation"].get("model"), current.authorization_reference, reference_identity)
        if voice_signature is None:
            voice_signature, first_job = signature, source_job
        elif signature != voice_signature:
            raise UseAdmissionError("voice_qa_composition_voice_identity_changed")
        target_parts.append(source_job.payload.text.strip())
        sources.append((current, source_job, source_path))

    if target_text != " ".join(target_parts):
        raise UseAdmissionError("voice_qa_composition_target_changed")
    if composition.get("copy_adjudicated_source_take_audio_ids") != adjudicated_ids:
        raise UseAdmissionError("voice_qa_composition_copy_decision_changed")
    if audio.authorization_reference != sources[0][0].authorization_reference:
        raise UseAdmissionError("voice_qa_composition_authorization_changed")
    if generation.get("voice_profile_id") not in (None, voice_signature[0]):
        raise UseAdmissionError("voice_qa_composition_voice_identity_changed")
    resolve_project_voice_profile(db, project_id, first_job.payload.voice_profile_id,
        getattr(first_job.payload, "cross_ip_voice_authorization_reference", None))

    raw_duration = sum(source.duration_ms for source, _, _ in sources)
    seam = composition.get("measured_seam_compaction")
    if seam is not None:
        try:
            cuts = seam["cuts"]
            if (seam.get("method") != "pcm16_mono_24khz_10ms_rms_below_minus45dbfs"
                or seam.get("margin_ms_each_side") != 60 or seam.get("max_cut_ms") != 300
                or seam.get("raw_duration_ms") != raw_duration
                or seam.get("final_sha256") != audio.content_hash
                or len(cuts) != len(sources) - 1):
                raise ValueError()
            boundaries = []
            elapsed = 0
            for source, _, _ in sources[:-1]:
                elapsed += source.duration_ms
                boundaries.append(elapsed)
            total_cut = 0
            prior_end = -1
            for index, (cut, boundary) in enumerate(zip(cuts, boundaries)):
                start, end = cut["start_ms"], cut["end_ms"]
                if (cut.get("seam_index") != index or type(start) is not int or type(end) is not int
                    or start < 0 or end <= start or end - start > 300 or start >= boundary or end <= boundary
                    or start < prior_end or end > raw_duration):
                    raise ValueError()
                total_cut += end - start
                prior_end = end
            if raw_duration - total_cut != audio.duration_ms:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise UseAdmissionError("voice_qa_composition_seam_evidence_invalid") from None
    elif raw_duration != audio.duration_ms:
        raise UseAdmissionError("voice_qa_composition_duration_changed")

    source = existing_local_media_file(audio.source_file, Path(root))
    if not source.is_relative_to(Path(root).resolve()) or file_digest(source) != audio.content_hash:
        raise UseAdmissionError("execution_use_content_changed")
    scope = _composed_source_scope(db, root, project_id, source_ids)
    saved_scope = scopes.get("audio", audio.id)
    if saved_scope is not None and saved_scope != scope:
        raise UseAdmissionError("execution_use_scope_immutable")
    if saved_scope is not None:
        origins = db.connection.execute("SELECT content_hash FROM execution_use_content_origins WHERE subject_kind='audio' AND subject_id=?",
            (str(audio.id),)).fetchall()
        if len(origins) != 1 or origins[0][0] != audio.content_hash:
            raise UseAdmissionError("execution_use_origin_missing")

    binding = VoiceQAInputBinding(audio_id=audio.id, audio_sha256=audio.content_hash,
        producer_job_id=None, producer_job_sha256=None, composition_source_take_audio_ids=source_ids,
        producer_metadata_sha256=execution_canonical_sha256(generation),
        original_scope_fingerprint=scope.fingerprint,
        original_admissions=tuple((value.admission_id, value.review_sha256) for value in scope.bindings),
        target_text_sha256=hashlib.sha256(target_text.encode()).hexdigest(),
        language=audio.language or first_job.payload.language or "zh")
    return binding, source


def _original_proof_unchecked(db, root, audio, *, project_id):
    generation = audio.metadata.get("voice_generation", {})
    if generation.get("provider") == "composed_voice_takes" or "composition" in generation:
        return _composed_original_proof_unchecked(db, root, audio, project_id=project_id)
    return _voice_job_original_proof_unchecked(db, root, audio, project_id=project_id)


def _original_proof(db, root, audio, *, project_id):
    try:
        return _original_proof_unchecked(db, root, audio, project_id=project_id)
    except UseAdmissionError:
        raise
    except (ValueError, OSError):
        raise UseAdmissionError("voice_qa_original_authority_invalid") from None


def _retained_asr_report(service, receipt):
    from app.domain.maintained_runtime import MaintainedASROrigin
    from app.providers.maintained_runtime import _require_origin
    from app.providers.maintained_voice_bundle import BUNDLE_SLOT, BUNDLE_MAX_BYTES
    from app.runtime_inventory import RuntimeInventoryStore
    raw = service._file(receipt.review_reference, maximum=100000)
    if hashlib.sha256(raw).hexdigest() != receipt.review_sha256:
        raise UseAdmissionError("provider_use_review_hash_changed")
    report = ProviderUseLicenseReviewV4.model_validate_json(raw)
    if report != receipt.review:
        raise UseAdmissionError("provider_use_binding_changed")
    spec = report.execution_specification
    inventory = RuntimeInventoryStore(service.root).read_v2(spec.runtime_inventory)
    _require_origin(service.root, spec.runtime_origin, inventory, MaintainedASROrigin)
    entries = [r for r in inventory.files if r.role == "entrypoint"]
    if len(entries) != 1 or entries[0].name != BUNDLE_SLOT or not 0 < entries[0].size_bytes <= BUNDLE_MAX_BYTES:
        raise UseAdmissionError("maintained_asr_child_evidence_invalid")
    terms = service._file(report.license_terms_reference)
    if not terms or hashlib.sha256(terms).hexdigest() != report.license_terms_sha256:
        raise UseAdmissionError("provider_use_terms_hash_changed")
    return report


def _job_binding(db, root, claim, *, retained=False):
    scopes = ExecutionScopeService(db, root)
    job = JobRepository(db).get(claim.job_id)
    if (job is None or job.type != JobType.VERIFY_VOICE or not isinstance(job.payload, VoiceQaJobPayload)
        or job.project_id != claim.project_id or job.payload.project_id != claim.project_id
        or job.payload.narration_audio_id != claim.input_binding.audio_id
        or hashlib.sha256(job.payload.target_text.encode()).hexdigest() != claim.input_binding.target_text_sha256
        or job_use_identity(job) != claim.job_sha256):
        raise UseAdmissionError("voice_qa_job_changed")
    scope = scopes.require_job_snapshot(job)
    if (scope is None or scope.policy_version != 2 or scope.purpose != "internal_evaluation"
        or scope.fingerprint != claim.scope_fingerprint or len(scope.bindings) != 1
        or scope.bindings[0].admission_id != claim.admission_id
        or scope.bindings[0].execution_sha256 != claim.execution_sha256):
        raise UseAdmissionError("voice_qa_execution_binding_changed")
    receipt = scopes.admissions.get(claim.project_id, claim.admission_id)
    if (receipt is None or type(receipt.review) is not ProviderUseLicenseReviewV4
        or receipt.review.input_binding != claim.input_binding
        or receipt.review.execution_sha256 != claim.execution_sha256
        or scope.bindings[0].review_sha256 != receipt.review_sha256
        or scope.bindings[0].configuration_sha256 != receipt.review.configuration_sha256
        or scope.bindings[0].execution_specification != receipt.review.execution_specification):
        raise UseAdmissionError("voice_qa_admission_changed")
    if retained:
        _retained_asr_report(scopes.admissions, receipt)
        profile = ProviderMachineCapabilityProfileRepository(db).get(receipt.review.capability_profile_id)
        decision = resolve_provider_use(project_id=claim.project_id, capability=profile,
            configuration_sha256=None if profile is None else scopes.admissions._configuration(profile),
            identity=receipt.review.identity, purpose="internal_evaluation", operation="review", admission=receipt, evidence_current=True)
        if decision.state != "allowed_for_scope":
            raise UseAdmissionError(decision.reasons[0])
    else:
        scopes.require_current(scope, project_id=claim.project_id, purpose="internal_evaluation", operation="generation")
    return job, scope, receipt


def _projection(db, root, claim, raw, *, configured=False):
    binding, source = _original_proof(db, root, claim.original_audio, project_id=claim.project_id)
    if binding != claim.input_binding:
        raise UseAdmissionError("voice_qa_original_input_changed")
    request = ASRChildRequest(model_root="unused-parent-only", audio_path=str(source),
        audio_sha256=binding.audio_sha256, language=binding.language)
    transcript = raw.transcription(request.verify_audio())
    scoring_audio = claim.original_audio.model_copy(update={"source_file": str(source)})
    target = claim.original_audio.metadata["voice_generation"]["target_text"]
    report = verify_generated_voice(scoring_audio, target, transcript,
        **{key:ASR_PARAMETERS[key] for key in ("max_silence_ms", "max_leading_silence_ms", "max_substitution_ratio")})
    _, _, receipt = _job_binding(db, root, claim, retained=True)
    spec = receipt.review.execution_specification
    result = apply_voice_qa(claim.original_audio, report, transcript, provider=spec.provider, model=spec.model)
    execution = (dict(deployment_sha256=claim.execution_sha256, configuration_observation="configured_local_process")
        if configured else dict(execution_sha256=claim.execution_sha256))
    result = result.model_copy(update={"metadata": {**result.metadata, "voice_evaluation_qa":
        dict(policy=POLICY, job_id=str(claim.job_id), admission_id=str(claim.admission_id), **execution)}})
    if claim.input_binding.composition_source_take_audio_ids:
        source = AudioAssetRepository(db).get(claim.input_binding.composition_source_take_audio_ids[0])
        source_generation = None if source is None else source.metadata.get("voice_generation")
        profile_id = source_generation.get("voice_profile_id") if isinstance(source_generation, dict) else None
        if not isinstance(profile_id, str) or not profile_id:
            raise UseAdmissionError("voice_qa_composition_voice_identity_changed")
        generation = dict(result.metadata["voice_generation"])
        generation["voice_profile_id"] = profile_id
        result = result.model_copy(update={"metadata": {**result.metadata, "voice_generation": generation}})
    return result, report.metadata(provider=spec.provider, model=spec.model)


def _matches_qa_projection(audio, projected, *, project_id):
    if audio == projected:
        return True
    from app.voice_qa import voice_copy_adjudication_is_valid
    if not voice_copy_adjudication_is_valid(audio, project_id=project_id):
        return False
    metadata = dict(audio.metadata)
    generation = dict(metadata.get("voice_generation", {}))
    generation.pop("copy_adjudication", None)
    metadata["voice_generation"] = generation
    return audio.model_copy(update={"metadata": metadata}) == projected


def qa_input_document(claim, receipt, *, claim_payload=None):
    # Hash the retained claim shape when verifying older completed calls. New
    # optional binding fields must not rewrite the identity of an old claim.
    claim_value = claim.model_dump(mode="json") if claim_payload is None else claim_payload
    return dict(claim=claim_value,
        execution_specification=receipt.review.execution_specification.model_dump(mode="json"),
        execution_sha256=claim.execution_sha256, configuration_sha256=receipt.review.configuration_sha256,
        review_sha256=receipt.review_sha256)


def qa_call_digest(claim, receipt, *, claim_payload=None):
    from app.budget import provider_call_input_digest
    return provider_call_input_digest(dict(project_id=str(claim.project_id), operation="asr",
        provider=receipt.review.identity.provider, model=receipt.review.identity.model,
        input_source=CALL_SOURCE, input=qa_input_document(claim, receipt, claim_payload=claim_payload)))


def require_completed_qa(db, root, audio, *, project_id, purpose):
    """Authenticated completed-result read; only then may historical producer be viewed."""
    if purpose != "internal_evaluation":
        raise UseAdmissionError("evaluation_scope_not_commercial")
    claim = retained_claim(db, audio.id)
    if claim is None or claim.project_id != project_id:
        raise UseAdmissionError("voice_qa_claim_missing")
    claim_row = db.connection.execute("SELECT payload FROM voice_evaluation_qa_claims WHERE audio_id=?",
        (str(audio.id),)).fetchone()
    try:
        claim_payload_json = claim_row["payload"]
        claim_payload = json.loads(claim_payload_json)
        if QAClaim.model_validate_json(claim_payload_json) != claim:
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise UseAdmissionError("voice_qa_claim_invalid") from None
    job, scope, receipt = _job_binding(db, root, claim, retained=True)
    call = ProviderCallRepository(db).get_by_idempotency_key(f"voice-evaluation-qa:{job.id}")
    value = (call.result_payload or {}).get("result") if call and call.status == "completed" else None
    if (not isinstance(value, dict) or call.project_id != project_id or call.operation != "asr"
        or call.mode != "runtime" or call.input_source != CALL_SOURCE
        or call.provider != receipt.review.identity.provider or call.model != receipt.review.identity.model
        or value.get("audio_id") != str(audio.id) or value.get("job_id") != str(job.id)
        or value.get("claim_sha256") != execution_canonical_sha256(claim_payload)
        or value.get("input_digest") != call.input_digest
        or call.input_digest != qa_call_digest(claim, receipt, claim_payload=claim_payload)):
        raise UseAdmissionError("voice_qa_result_unbound")
    try:
        raw = ASRChildResult.model_validate_json(json.dumps(value["transcription"], allow_nan=False))
        projected, report = _projection(db, root, claim, raw,
            configured=value.get("configuration_observation") == "configured_local_process")
        if (value.get("report") != report or value.get("projection_sha256") != execution_canonical_sha256(projected.model_dump(mode="json"))
            or not _matches_qa_projection(audio, projected, project_id=project_id)):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise UseAdmissionError("voice_qa_projection_changed") from None
    return REVIEW_POLICY


class VoiceEvaluationQAService:
    def __init__(self, db, root):
        self.db, self.root = db, Path(root).resolve()
        self.scopes = ExecutionScopeService(db, self.root)

    def enqueue(self, project_id, request):
        if request.qa_policy != POLICY or request.purpose != "internal_evaluation" or request.use_admission_id is None:
            raise UseAdmissionError("voice_qa_policy_required")
        audio = AudioAssetRepository(self.db).get(request.narration_audio_id)
        if audio is None:
            raise UseAdmissionError("voice_qa_audio_missing")
        existing = retained_claim(self.db, audio.id)
        target = request.target_text
        canonical = dict(policy=POLICY, project_id=str(project_id), admission_id=str(request.use_admission_id),
            audio_id=str(audio.id), target_text=target)
        digest = execution_canonical_sha256(canonical)
        if existing is not None:
            job = JobRepository(self.db).get(existing.job_id)
            if job and job.idempotency_key == request.idempotency_key and existing.request_sha256 == digest:
                # Recovery does not reserve or call again; current eligibility is separate.
                _job_binding(self.db, self.root, existing, retained=True)
                if job.project_id != project_id:
                    raise UseAdmissionError("provider_use_project_mismatch")
                return job
            raise UseAdmissionError("voice_qa_already_claimed")
        if has_qa_reference(self.db, audio):
            raise UseAdmissionError("voice_qa_claim_missing")
        binding, _ = _original_proof(self.db, self.root, audio, project_id=project_id)
        origin_scope = (_composed_source_scope(self.db, self.root, project_id,
            binding.composition_source_take_audio_ids) if binding.composition_source_take_audio_ids else None)
        if hashlib.sha256(target.encode()).hexdigest() != binding.target_text_sha256:
            raise UseAdmissionError("voice_qa_target_changed")
        scope = self.scopes.prepare(project_id, purpose="internal_evaluation", admission_ids=(request.use_admission_id,))
        receipt = self.scopes.admissions.get(project_id, request.use_admission_id)
        if type(receipt.review) is not ProviderUseLicenseReviewV4 or receipt.review.input_binding != binding:
            raise UseAdmissionError("voice_qa_admission_changed")
        self.require_budget(project_id)
        now = datetime.now(timezone.utc)
        job = Job(project_id=project_id, type=JobType.VERIFY_VOICE, idempotency_key=request.idempotency_key,
            created_at=now, updated_at=now, payload=VoiceQaJobPayload(project_id=project_id, narration_audio_id=audio.id, target_text=target))
        claim = QAClaim(policy=POLICY, project_id=project_id, admission_id=receipt.id, job_id=job.id,
            job_sha256=job_use_identity(job), scope_fingerprint=scope.fingerprint, request_sha256=digest,
            input_binding=binding, original_audio=audio, execution_sha256=receipt.review.execution_sha256)
        before = self._facts(audio, claim)
        with self.db.transaction(immediate=True):
            if self._facts(audio, claim) != before:
                raise UseAdmissionError("voice_qa_authority_changed")
            if self.db.connection.execute("SELECT 1 FROM voice_evaluation_qa_claims WHERE audio_id=? OR admission_id=?",
                (str(audio.id), str(receipt.id))).fetchone():
                raise UseAdmissionError("voice_qa_already_claimed")
            if origin_scope is not None:
                self.scopes.pin("audio", audio.id, origin_scope, operation="review", content_hash=audio.content_hash)
            stored = JobRepository(self.db).create(job)
            if stored.id != job.id:
                raise UseAdmissionError("voice_qa_idempotency_conflict")
            self.scopes.pin("job", job.id, scope, operation="generation", subject_sha256=claim.job_sha256)
            self.db.connection.execute("INSERT INTO voice_evaluation_qa_claims VALUES (?,?,?,?,?,?,?)",
                (str(receipt.id), str(job.id), str(audio.id), digest, claim.job_sha256, scope.fingerprint, claim.model_dump_json()))
        return job

    def require_budget(self, project_id):
        from app.db import BudgetPolicyRepository
        from app.budget import enforce_reservation, BudgetLimitError
        from app.costs import UnknownProviderCostEstimator
        from app.domain.models import CostCategory
        policies = [(ident, p) for ident in (None, project_id)
            if (p := BudgetPolicyRepository(self.db).get_by_project(ident)) is not None]
        if (not policies or not any(p.max_calls is not None for _, p in policies)
            or any(not p.allow_unknown_cost or p.max_calls == 0 for _, p in policies)):
            raise UseAdmissionError("voice_qa_explicit_budget_required")
        estimate = UnknownProviderCostEstimator().estimate(operation="asr", provider="faster-whisper",
            model="local", input_source=CALL_SOURCE, category=CostCategory.ASR)
        try:
            for ident, policy in policies:
                calls = ProviderCallRepository(self.db).list_all() if ident is None else ProviderCallRepository(self.db).list_for_project(ident)
                enforce_reservation(policy, calls, estimate)
        except BudgetLimitError:
            raise UseAdmissionError("voice_qa_current_budget_blocked") from None

    def _facts(self, audio, claim):
        # Narrow durable composite authority; never read files/native code under lock.
        rows = [audio.model_dump_json()]
        saved = AudioAssetRepository(self.db).get(audio.id)
        rows.append(None if saved is None else saved.model_dump_json())
        from app.db import VoiceProfileRepository
        scopes = []
        source_ids = () if claim is None else claim.input_binding.composition_source_take_audio_ids
        origin_ids = source_ids or (audio.id,)
        producers, hashes = [], {audio.content_hash}
        audios = AudioAssetRepository(self.db)
        for identity in origin_ids:
            source_audio = audios.get(identity)
            source_claim = retained_claim(self.db, identity) if source_ids else None
            producer_audio = source_claim.original_audio if source_claim is not None else (audio if identity == audio.id else source_audio)
            rows.append(None if source_audio is None else source_audio.model_dump_json())
            rows.append(None if source_claim is None else source_claim.model_dump_json())
            if producer_audio is None:
                raise UseAdmissionError("voice_qa_composition_source_changed")
            producer_generation = producer_audio.metadata.get("voice_generation", {})
            try:
                producer = JobRepository(self.db).get(UUID(producer_generation["job_id"]))
            except (KeyError, TypeError, ValueError):
                producer = None
            if producer is None:
                raise UseAdmissionError("voice_qa_original_producer_missing")
            producers.append(producer)
            rows.extend((producer_audio.model_dump_json(), producer.model_dump_json()))
            profile_id = getattr(producer.payload, "voice_profile_id", None)
            profile_row = self.db.connection.execute("SELECT * FROM voice_profiles WHERE id=?",
                (str(profile_id),)).fetchone() if profile_id is not None else None
            rows.append(None if profile_row is None else tuple(profile_row))
            profile = VoiceProfileRepository(self.db).get(profile_id) if profile_id is not None else None
            clip_ids = set([] if profile is None else profile.reference_clip_ids)
            reference = getattr(producer.payload, "reference_window", None)
            if reference is not None:
                clip_ids.add(reference.clip_id)
                hashes.update((reference.source_content_hash, reference.authorization_content_hash))
            for clip_id in sorted(clip_ids, key=str):
                row = self.db.connection.execute("SELECT * FROM clips WHERE id=?", (str(clip_id),)).fetchone()
                rows.append(None if row is None else tuple(row))
                if row:
                    asset_id = json.loads(row["payload"])["asset_id"]
                    asset = self.db.connection.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
                    rows.append(None if asset is None else tuple(asset))
            hashes.add(producer_audio.content_hash)
            if source_claim is not None:
                qa_job = JobRepository(self.db).get(source_claim.job_id)
                qa_scope = self.scopes.get("job", source_claim.job_id)
                qa_call = ProviderCallRepository(self.db).get_by_idempotency_key(f"voice-evaluation-qa:{source_claim.job_id}")
                rows.extend((None if qa_job is None else qa_job.model_dump_json(),
                    None if qa_scope is None else qa_scope.model_dump_json(),
                    None if qa_call is None else qa_call.model_dump_json()))
                if qa_scope is not None:
                    scopes.append(qa_scope)
        for content_hash in sorted(hashes):
            scopes.extend(self.scopes.scopes_for_hash(content_hash))
            rows.append(tuple(tuple(r) for r in self.db.connection.execute(
                "SELECT * FROM execution_use_content_origins WHERE content_hash=? ORDER BY subject_kind,subject_id", (content_hash,))))
        scoped_subjects = [("audio", audio.id)]
        scoped_subjects.extend(("audio", identity) for identity in source_ids)
        scoped_subjects.extend(("job", producer.id) for producer in producers)
        for kind, identity in scoped_subjects:
            scope = self.scopes.get(kind, identity)
            rows.append(None if scope is None else scope.model_dump_json())
            if scope: scopes.append(scope)
        admission_ids = {b.admission_id for s in scopes for b in s.bindings}
        if claim:
            admission_ids.add(claim.admission_id)
            qa_job = JobRepository(self.db).get(claim.job_id)
            rows.append(None if qa_job is None else job_use_identity(qa_job))
            scope = self.scopes.get("job", claim.job_id)
            rows.append(None if scope is None else scope.model_dump_json())
            if scope: admission_ids.update(b.admission_id for b in scope.bindings)
            rows.append(tuple(tuple(r) for r in self.db.connection.execute(
                "SELECT * FROM voice_evaluation_qa_claims WHERE audio_id=? OR admission_id=? ORDER BY admission_id",
                (str(audio.id), str(claim.admission_id)))))
        for identity in sorted(admission_ids, key=str):
            row = self.db.connection.execute("SELECT * FROM provider_use_admissions WHERE id=?", (str(identity),)).fetchone()
            rows.append(None if row is None else tuple(row))
            if row:
                cap_id = json.loads(row["payload"])["review"]["capability_profile_id"]
                cap = self.db.connection.execute("SELECT * FROM provider_machine_capability_profiles WHERE id=?", (cap_id,)).fetchone()
                rows.append(None if cap is None else tuple(cap))
                setting = None if cap is None else self.db.connection.execute("SELECT * FROM provider_machine_settings WHERE scope_key=?", (cap["scope_key"],)).fetchone()
                rows.append(None if setting is None else tuple(setting))
        for producer in producers:
            for table in ("voice_calibration_claims", "voice_draft_jobs"):
                row = self.db.connection.execute(f"SELECT * FROM {table} WHERE job_id=?", (str(producer.id),)).fetchone()
                rows.append(None if row is None else tuple(row))
            call = ProviderCallRepository(self.db).get_by_idempotency_key(f"maintained-voice:{producer.id}")
            rows.append(None if call is None else call.model_dump_json())
        return tuple(rows)

    def require_job(self, job, *, retained=False):
        if (job.type != JobType.VERIFY_VOICE or not isinstance(job.payload, VoiceQaJobPayload)
            or getattr(self.db, "data_root", None) != self.root):
            raise UseAdmissionError("voice_qa_handler_unsupported")
        claim = retained_claim(self.db, job.payload.narration_audio_id)
        if claim is None or claim.job_id != job.id:
            raise UseAdmissionError("voice_qa_claim_missing")
        stored, scope, receipt = _job_binding(self.db, self.root, claim, retained=retained)
        if job_use_identity(stored) != job_use_identity(job):
            raise UseAdmissionError("voice_qa_job_changed")
        binding, source = _original_proof(self.db, self.root, claim.original_audio, project_id=job.project_id)
        if binding != claim.input_binding:
            raise UseAdmissionError("voice_qa_original_input_changed")
        actual = AudioAssetRepository(self.db).get(claim.input_binding.audio_id)
        call = ProviderCallRepository(self.db).get_by_idempotency_key(f"voice-evaluation-qa:{job.id}")
        if call and call.status == "completed":
            require_completed_qa(self.db, self.root, actual, project_id=job.project_id, purpose="internal_evaluation")
        elif actual != claim.original_audio:
            raise UseAdmissionError("voice_qa_original_input_changed")
        return claim, scope, receipt, source

    def status(self, project_id, audio_id):
        result = dict(project_id=str(project_id), audio_id=str(audio_id), policy=POLICY, state="blocked", reasons=[], choices=[], dispatch_authorized=False,
            quality_authorized=False, automatic_retry_authorized=False, technical_ready=False)
        audio = AudioAssetRepository(self.db).get(audio_id)
        try:
            if audio is None:
                raise UseAdmissionError("voice_qa_audio_missing")
            claim = retained_claim(self.db, audio_id)
            if claim:
                if claim.project_id != project_id:
                    raise UseAdmissionError("provider_use_project_mismatch")
                job = JobRepository(self.db).get(claim.job_id)
                self.require_job(job, retained=True)
                call = ProviderCallRepository(self.db).get_by_idempotency_key(f"voice-evaluation-qa:{job.id}")
                result.update(job_id=str(job.id), job_status=job.status.value,
                    call_status=None if call is None else call.status, qa_state=audio.metadata["voice_generation"]["qa_state"],
                    state="completed" if call and call.status == "completed" and job.status == JobStatus.COMPLETED else "reconciliation_required" if call else "queued")
                if call is None and job.status in (JobStatus.FAILED, JobStatus.CANCELLED):
                    result.update(state="blocked", reasons=[job.error_code or "voice_qa_job_terminal_without_call"],
                        recovery_action="correct_preflight_and_reconcile_same_unspent_job")
                result["technical_ready"] = bool(call and call.status == "completed" and job.status == JobStatus.COMPLETED
                    and audio.metadata["voice_generation"]["qa_state"] == "verified")
                return result
            binding, _ = _original_proof(self.db, self.root, audio, project_id=project_id)
            for receipt in self.scopes.admissions.list(project_id):
                if type(receipt.review) is not ProviderUseLicenseReviewV4 or receipt.review.input_binding != binding:
                    continue
                decision = self.scopes.admissions.resolve(project_id, capability_profile_id=receipt.review.capability_profile_id,
                    identity=receipt.review.identity, purpose="internal_evaluation", admission_id=receipt.id)
                if decision.state == "allowed_for_scope":
                    result["choices"].append(dict(admission_id=str(receipt.id), label="本地 FasterWhisper · 内部评估 · 单次 QA"))
            if not result["choices"]:
                raise UseAdmissionError("voice_qa_current_asr_admission_missing")
            self.require_budget(project_id)
            result.update(state="ready", target_text=audio.metadata["voice_generation"]["target_text"])
        except (ValueError, OSError) as exc:
            result["reasons"] = [str(exc) if isinstance(exc, UseAdmissionError) else "voice_qa_evidence_invalid"]
        return result


def execute_evaluation_qa(*, db, root, job, prepared, cost_estimator):
    """Current audio/permission guard and the existing once-only ASR ledger."""
    from app.providers.maintained_asr_execution import PreparedMaintainedASRChild
    from app.provider_execution import ProviderExecutionService, RuntimeProviderIdentity
    from app.domain.models import CostCategory
    service = VoiceEvaluationQAService(db, root)
    claim, scope, receipt, source = service.require_job(job)
    saved = JobRepository(db).get(job.id)
    if saved.status != JobStatus.RUNNING or job.status != JobStatus.RUNNING or saved.attempt != job.attempt:
        raise UseAdmissionError("voice_qa_job_not_running")
    if type(prepared) is not PreparedMaintainedASRChild:
        raise UseAdmissionError("voice_qa_owned_child_required")
    request = prepared.request_snapshot()
    expected = ASRChildRequest(model_root=receipt.review.identity.model, audio_path=str(source),
        audio_sha256=claim.input_binding.audio_sha256, language=claim.input_binding.language)
    if request != expected:
        raise UseAdmissionError("voice_qa_child_input_changed")
    provider = SimpleNamespace(provider_name=receipt.review.identity.provider, model=receipt.review.identity.model)
    if prepared.specification != receipt.review.execution_specification:
        raise UseAdmissionError("voice_qa_configuration_changed")
    prepared.verify()
    facts = service._facts(claim.original_audio, claim)
    def guard():
        active = JobRepository(db).get(job.id)
        if active is None or active.status != JobStatus.RUNNING or active.attempt != job.attempt:
            raise UseAdmissionError("voice_qa_job_not_running")
        if service._facts(claim.original_audio, claim) != facts:
            raise UseAdmissionError("voice_qa_authority_changed")
    def current():
        guard()
        if service.require_job(job)[0] != claim:
            raise UseAdmissionError("voice_qa_claim_invalid")
    def action():
        raw = prepared.execute()
        current()
        projected, report = _projection(db, root, claim, raw, configured=True)
        current()
        value = dict(audio_id=str(projected.id), job_id=str(job.id),
            claim_sha256=execution_canonical_sha256(claim.model_dump(mode="json")),
            input_digest=qa_call_digest(claim, receipt), transcription=raw.model_dump(mode="json"),
            report=report, configuration_observation="configured_local_process", projection_sha256=execution_canonical_sha256(projected.model_dump(mode="json")))
        return projected, value
    def persist(result):
        # The ledger owns this transaction. Original scope/PCM are not rewritten.
        guard()
        projected, _ = result
        cursor = db.connection.execute("UPDATE audio_assets SET payload=? WHERE id=? AND content_hash=?",
            (projected.model_dump_json(), str(projected.id), projected.content_hash))
        if cursor.rowcount != 1:
            raise UseAdmissionError("voice_qa_audio_missing")
    def replay(value):
        audio = AudioAssetRepository(db).get(claim.input_binding.audio_id)
        require_completed_qa(db, root, audio, project_id=job.project_id, purpose="internal_evaluation")
        return audio, value
    def before_replay():
        service.require_job(job, retained=True)
    result = ProviderExecutionService(db, cost_estimator).execute(project_id=job.project_id,
        idempotency_key=f"voice-evaluation-qa:{job.id}", operation="asr",
        provider=RuntimeProviderIdentity(provider.provider_name, provider.model), input_source=CALL_SOURCE,
        category=CostCategory.ASR, input_document=qa_input_document(claim, receipt),
        reservation_guard=guard, before_action=current, before_replay=before_replay,
        action=action, encode_result=lambda r:r[1], decode_result=replay, persist_result=persist,
        error_code=lambda e:str(e) if isinstance(e, UseAdmissionError) else "voice_evaluation_qa_failed")
    return result[0]


def qa_review_state(db, root, audio, *, project_id, purpose):
    claim = retained_claim(db, audio.id)
    if claim is None:
        raise UseAdmissionError("voice_qa_claim_missing")
    if project_id is None or purpose is None:
        raise UseAdmissionError("execution_use_review_context_required")
    if claim.project_id != project_id:
        raise UseAdmissionError("provider_use_project_mismatch")
    if purpose != "internal_evaluation":
        raise UseAdmissionError("evaluation_scope_not_commercial")
    job, _, _ = _job_binding(db, root, claim, retained=True)
    call = ProviderCallRepository(db).get_by_idempotency_key(f"voice-evaluation-qa:{job.id}")
    if call and call.status == "completed":
        policy = require_completed_qa(db, root, audio, project_id=project_id, purpose=purpose)
        return policy, claim.original_audio
    if audio != claim.original_audio:
        raise UseAdmissionError("voice_qa_projection_changed")
    return None, audio
