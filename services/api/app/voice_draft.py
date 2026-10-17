"""Normal standalone evaluation Voice draft, using existing execution contracts."""
from datetime import datetime, timezone
from uuid import uuid4

from app.db import AudioAssetRepository, AssetRepository, BudgetPolicyRepository, ClipRepository, JobRepository, ProviderCallRepository, VoiceProfileRepository
from app.domain.models import ExecutionSpecificationV6, Job, JobType, VoiceGenerationJobPayload
from app.execution_admission import UseAdmissionError
from app.execution_scope import ExecutionScopeService, ExecutionUseBindingV2, job_use_identity
from app.media.audio_access import AudioAccess
from app.media.audio_access import file_digest
from app.media.local_paths import existing_local_media_file
from pathlib import Path
from app.voice_calibration import calibration_receipts, request_digest, require_claim, POLICY as CALIBRATION
from app.voice_plan import require_saved_voice_plan, resolve_project_voice_profile
from app.voice_performance import authorized_voice_reference_choices, resolve_authorized_voice_reference_choice

POLICY = "voice-draft-v1"


class VoiceDraftService:
    def __init__(self, db, root):
        self.db, self.root = db, root
        self.scopes = ExecutionScopeService(db, root)

    def list_saved(self, project_id):
        """Discover historical drafts; current status/access is checked separately."""
        rows = self.db.connection.execute(
            """SELECT jobs.id, voice_draft_jobs.policy FROM voice_draft_jobs
            JOIN jobs ON jobs.id=voice_draft_jobs.job_id
            WHERE voice_draft_jobs.project_id=? AND jobs.project_id=?
            ORDER BY jobs.created_at DESC, jobs.id DESC LIMIT 50""",
            (str(project_id), str(project_id))).fetchall()
        items = []
        for row in rows:
            job = JobRepository(self.db).get(row["id"])
            if (job.type != JobType.GENERATE_VOICE or not isinstance(job.payload, VoiceGenerationJobPayload)
                    or row["policy"] not in (POLICY, CALIBRATION)):
                continue
            items.append({"job_id": str(job.id), "project_id": str(project_id), "policy": row["policy"],
                          "job_status": job.status.value, "created_at": job.created_at.isoformat(),
                          "text_preview": job.payload.text[:120]})
        return items

    def _inputs(self, payload, scope):
        policies = [value for project in (None, scope.project_id)
            if (value := BudgetPolicyRepository(self.db).get_by_project(project)) is not None]
        if (not policies or not any(value.max_calls is not None for value in policies)
            or any(not value.allow_unknown_cost or value.max_calls == 0 for value in policies)):
            raise UseAdmissionError("voice_draft_explicit_budget_required")
        if (not isinstance(payload, VoiceGenerationJobPayload) or payload.reference_window is None
            or payload.narration_performance_plan is not None):
            raise UseAdmissionError("voice_draft_reference_required")
        bindings = [binding for binding in scope.bindings if binding.capability_profile_id == payload.execution_capability_profile_id]
        if len(bindings) != 1 or not isinstance(bindings[0], ExecutionUseBindingV2):
            raise UseAdmissionError("voice_draft_execution_binding_required")
        binding = bindings[0]
        spec = binding.execution_specification
        if (scope.policy_version != 2 or type(spec) is not ExecutionSpecificationV6
            or spec.provider != "omnivoice" or spec.mode != "local"
            or spec.capability != "voice" or payload.expected_provider != spec.provider or payload.expected_model != spec.model):
            raise UseAdmissionError("voice_draft_execution_unsupported")
        profile = resolve_project_voice_profile(self.db,scope.project_id,payload.voice_profile_id,payload.cross_ip_voice_authorization_reference)
        if profile.provider != spec.provider:
            raise UseAdmissionError("voice_profile_not_authorized")
        clips = [value for identity in profile.reference_clip_ids if (value := ClipRepository(self.db).get(identity))]
        assets = {value.id: value for clip in clips if (value := AssetRepository(self.db).get(clip.asset_id))}
        try:
            window = resolve_authorized_voice_reference_choice(payload.reference_window,
                authorized_voice_reference_choices(profile, clips, assets, data_root=self.root))
            source = existing_local_media_file(window.source_file, Path(self.root))
            if not source.is_relative_to(Path(self.root).resolve()) or file_digest(source) != payload.reference_window.source_content_hash:
                raise ValueError()
        except (ValueError, OSError):
            raise UseAdmissionError("maintained_voice_reference_changed") from None
        for dependency in self.scopes.require_content(payload.reference_window.authorization_content_hash,
                project_id=scope.project_id, purpose=scope.purpose, operation="derivation"):
            if any(value not in scope.bindings for value in dependency.bindings):
                raise UseAdmissionError("execution_use_ancestor_dropped")
        return binding

    def enqueue(self, project_id, request):
        source = getattr(request, "saved_plan_source", None)
        if source and request.delivery_text is not None:
            raise UseAdmissionError("voice_saved_plan_transform_unsupported")
        plan_snapshot = require_saved_voice_plan(self.db, project_id, source, request.text) if source else None
        if request.use_draft_performance_plan or request.execution_capability_profile_id is None:
            raise UseAdmissionError("voice_draft_execution_binding_required")
        scope = self.scopes.prepare(project_id, purpose="internal_evaluation", admission_ids=tuple(request.use_admission_ids))
        selected = [b for b in scope.bindings if b.capability_profile_id == request.execution_capability_profile_id]
        if len(selected) != 1:
            raise UseAdmissionError("voice_draft_execution_binding_required")
        identity = selected[0].identity
        payload = VoiceGenerationJobPayload(project_id=project_id, voice_profile_id=request.voice_profile_id,
            cross_ip_voice_authorization_reference=request.cross_ip_voice_authorization_reference,
            text=request.text, delivery_text=request.delivery_text, language=request.language,
            authorization_reference=request.authorization_reference, reference_window=request.reference_window,
            execution_capability_profile_id=request.execution_capability_profile_id,
            expected_provider=identity.provider, expected_model=identity.model)
        self._inputs(payload, scope)
        receipts = calibration_receipts(self.db, scope)
        policy = CALIBRATION if receipts else POLICY
        if request.draft_policy != policy or len(receipts) > 1:
            raise UseAdmissionError("voice_draft_policy_mismatch")
        if receipts and (receipts[0].review.capability_profile_id != payload.execution_capability_profile_id
                or receipts[0].review.request_sha256 != request_digest(payload)):
            raise UseAdmissionError("voice_calibration_request_changed")
        now = datetime.now(timezone.utc)
        job = Job(id=uuid4(), project_id=project_id, type=JobType.GENERATE_VOICE,
            idempotency_key=request.idempotency_key, created_at=now, updated_at=now, payload=payload)
        with self.db.transaction(immediate=True):
            if source and require_saved_voice_plan(self.db, project_id, source, request.text) != plan_snapshot:
                raise UseAdmissionError("voice_saved_plan_changed")
            stored = JobRepository(self.db).create(job)
            marker = self.db.connection.execute("SELECT * FROM voice_draft_jobs WHERE job_id=?", (str(stored.id),)).fetchone()
            if stored.id != job.id:
                if stored.type != job.type or stored.project_id != project_id or stored.payload != payload or marker is None:
                    raise UseAdmissionError("voice_draft_idempotency_conflict")
                if marker["scope_fingerprint"] != scope.fingerprint or marker["policy"] != policy:
                    raise UseAdmissionError("voice_draft_idempotency_conflict")
            else:
                self.db.connection.execute("INSERT INTO voice_draft_jobs VALUES (?,?,?,?,?)",
                    (str(stored.id), str(project_id), policy, job_use_identity(stored), scope.fingerprint))
                if receipts:
                    receipt = receipts[0]
                    if self.db.connection.execute("SELECT 1 FROM voice_calibration_claims WHERE admission_id=?", (str(receipt.id),)).fetchone():
                        raise UseAdmissionError("voice_calibration_already_claimed")
                    self.db.connection.execute("INSERT INTO voice_calibration_claims VALUES (?,?,?,?,?)",
                        (str(receipt.id), str(stored.id), request_digest(payload), job_use_identity(stored), scope.fingerprint))
                self.scopes.pin("job", stored.id, scope, operation="generation", subject_sha256=job_use_identity(stored))
        self.require_job(stored)
        return stored

    def require_job(self, job):
        marker = self.db.connection.execute("SELECT * FROM voice_draft_jobs WHERE job_id=?", (str(job.id),)).fetchone()
        scope = self.scopes.require_job_snapshot(job)
        if (marker is None or scope is None or marker["policy"] not in (POLICY, CALIBRATION)
            or marker["project_id"] != str(job.project_id) or marker["job_sha256"] != job_use_identity(job)
            or marker["scope_fingerprint"] != scope.fingerprint or scope.purpose != "internal_evaluation"
            or job.type != JobType.GENERATE_VOICE):
            raise UseAdmissionError("voice_draft_binding_missing_or_changed")
        # A module draft cannot be used to activate an owning ProductionRun.
        if self.db.connection.execute("SELECT 1 FROM production_runs WHERE voice_job_id=?", (str(job.id),)).fetchone():
            raise UseAdmissionError("voice_draft_production_unsupported")
        expected = CALIBRATION if calibration_receipts(self.db, scope) else POLICY
        if marker["policy"] != expected:
            raise UseAdmissionError("voice_draft_policy_mismatch")
        require_claim(self.db, job, scope)
        self.scopes.require_current(scope, project_id=job.project_id, purpose=scope.purpose, operation="generation")
        return self._inputs(job.payload, scope)

    def status(self, project_id, job):
        if job.project_id != project_id:
            raise UseAdmissionError("provider_use_project_mismatch")
        reasons = []
        try:
            self.require_job(job)
        except UseAdmissionError as exc:
            reasons.append(str(exc))
        generation_reasons = list(reasons)
        call = ProviderCallRepository(self.db).get_by_idempotency_key(f"maintained-voice:{job.id}")
        outputs = []
        for audio in AudioAssetRepository(self.db).list():
            generation = audio.metadata.get("voice_generation", {})
            if isinstance(generation, dict) and generation.get("job_id") == str(job.id):
                access, _ = AudioAccess(self.db, self.root).inspect(audio, project_id=project_id, purpose="internal_evaluation")
                # Mirror the existing normal QA consumer check without dispatch,
                # ASR construction, or treating audition permission as QA authority.
                qa_reasons = []
                try:
                    self.scopes.require_content(audio.content_hash, project_id=project_id, operation="review")
                except UseAdmissionError as exc:
                    qa_reasons.append(str(exc))
                from app.voice_evaluation_qa import VoiceEvaluationQAService
                outputs.append({"evaluation_qa": VoiceEvaluationQAService(self.db, self.root).status(project_id, audio.id), "audio_id": str(audio.id), "content_hash": audio.content_hash, "qa_state": generation.get("qa_state"), "access": access,
                    "qa_consumption": {"state": "blocked" if qa_reasons else "scope_check_passed",
                        "purpose": "commercial_production", "reasons": qa_reasons,
                        "execution_configuration": "not_checked", "dispatch_authorized": False}})
        if call and call.status in ("reserved", "running"):
            reasons.append("provider_call_reconciliation_required")
        elif call and call.status == "failed":
            reasons.append("provider_call_recorded_failure")
        marker = self.db.connection.execute("SELECT policy FROM voice_draft_jobs WHERE job_id=?", (str(job.id),)).fetchone()
        from app.voice_calibration import REVIEW_POLICY
        completed_review = (job.status.value == "completed" and call is not None and call.status == "completed"
            and len(outputs) == 1 and outputs[0]["access"].get("review_policy") in (REVIEW_POLICY, "completed-voice-calibration-review-v2")
            and outputs[0]["access"]["state"] == "review_available")
        if completed_review:
            reasons = []
        return {"target_text": job.payload.text if isinstance(job.payload, VoiceGenerationJobPayload) else None,
            "policy": marker[0] if marker else None, "job_id": str(job.id), "project_id": str(project_id),
            "job_status": job.status.value, "error_code": job.error_code,
            "state": "blocked" if reasons else job.status.value,
            "reasons": reasons, "provider_call_status": call.status if call else None, "outputs": outputs,
            "generation_eligibility": {"state": "blocked" if generation_reasons else "eligible_for_checks",
                "reasons": generation_reasons, "dispatch_authorized": False},
            "quality_authorized": False, "production_authorized": False, "automatic_retry_authorized": False,
            "recovery_action": "reconcile_recorded_call" if call and call.status in ("reserved", "running", "failed") else "inspect_job_error" if job.error_code else None}
