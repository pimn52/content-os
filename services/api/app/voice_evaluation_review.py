"""Normal U-Voice recording without mutating the authenticated ASR projection."""
from datetime import datetime, timezone
import sqlite3
from uuid import UUID
from app.db import AudioAssetRepository, JobRepository, ProjectRepository, ProviderCallRepository
from app.db.voice_evaluation_review import EvaluationVoiceReviewRepository
from app.domain.models import JobStatus, execution_canonical_sha256
from app.domain.voice_evaluation_review import POLICY, EvaluationVoiceReviewRecord
from app.execution_admission import UseAdmissionError
from app.execution_scope import ExecutionScopeService, read_job_scope
from app.media.audio_access import AudioAccess, file_digest
from app.media.local_paths import existing_local_media_file
from app.voice_evaluation_qa import VoiceEvaluationQAService, has_qa_reference, retained_claim, require_completed_qa


def require_legacy_human_review(db, root, audio):
    """Known evaluation provenance cannot reach the old metadata mutation."""
    if has_qa_reference(db, audio):
        raise UseAdmissionError("voice_evaluation_human_review_action_required")
    scopes = ExecutionScopeService(db, root)
    values = list(scopes.scopes_for_hash(audio.content_hash))
    subject = scopes.get("audio", audio.id)
    if subject:
        values.append(subject)
    generation = audio.metadata.get("voice_generation")
    if isinstance(generation, dict) and generation.get("job_id"):
        job = JobRepository(db).get(UUID(generation["job_id"]))
        if job and (scope := read_job_scope(db, job)):
            values.append(scope)
    # Check actual bytes too: changing a declared hash must not strip restrictions.
    try:
        values.extend(scopes.scopes_for_hash(file_digest(existing_local_media_file(audio.source_file, root))))
    except FileNotFoundError:
        pass  # Legacy behavior remains; known subject/producer checks above still apply.
    if any(value.purpose == "internal_evaluation" for value in values):
        raise UseAdmissionError("voice_evaluation_human_review_action_required")


class EvaluationVoiceReviewService:
    def __init__(self, db, root):
        self.db, self.root = db, root
        self.records = EvaluationVoiceReviewRepository(db)
        self.qa = VoiceEvaluationQAService(db, root)

    def _context(self, project_id, audio_id):
        if ProjectRepository(self.db).get(project_id) is None:
            raise UseAdmissionError("voice_human_review_project_missing")
        audio = AudioAssetRepository(self.db).get(audio_id)
        claim = retained_claim(self.db, audio_id)
        if audio is None or claim is None or claim.project_id != project_id:
            raise UseAdmissionError("voice_human_review_audio_unbound")
        return audio, claim

    def _facts(self, audio, claim):
        call = ProviderCallRepository(self.db).get_by_idempotency_key(f"voice-evaluation-qa:{claim.job_id}")
        job = JobRepository(self.db).get(claim.job_id)
        row = self.db.connection.execute("SELECT * FROM voice_evaluation_human_reviews WHERE audio_id=?", (str(audio.id),)).fetchone()
        return (self.qa._facts(claim.original_audio, claim),
            None if (stored := AudioAssetRepository(self.db).get(audio.id)) is None else stored.model_dump_json(),
            None if job is None else job.model_dump_json(), None if call is None else call.model_dump_json(),
            None if row is None else tuple(row),
            tuple(scope.model_dump_json() for scope in self.qa.scopes.scopes_for_hash(audio.content_hash)))

    def _binding(self, project_id, audio, claim):
        require_completed_qa(self.db, self.root, audio, project_id=project_id, purpose="internal_evaluation")
        job = JobRepository(self.db).get(claim.job_id)
        if job is None or job.status != JobStatus.COMPLETED or audio.metadata["voice_generation"]["qa_state"] != "verified":
            raise UseAdmissionError("voice_human_review_verified_terminal_qa_required")
        access, _ = AudioAccess(self.db, self.root).inspect(audio, project_id=project_id, purpose="internal_evaluation")
        if access["state"] != "review_available":
            raise UseAdmissionError(access["reasons"][0])
        call = ProviderCallRepository(self.db).get_by_idempotency_key(f"voice-evaluation-qa:{claim.job_id}")
        value = call.result_payload["result"]
        return dict(policy=POLICY, project_id=project_id, audio_id=audio.id, audio_sha256=audio.content_hash,
            qa_job_id=claim.job_id, qa_claim_sha256=execution_canonical_sha256(claim.model_dump(mode="json")),
            qa_call_id=call.id, qa_result_sha256=execution_canonical_sha256(value), projection_sha256=value["projection_sha256"])

    @staticmethod
    def _fingerprint(binding):
        return execution_canonical_sha256({key: str(value) if isinstance(value, UUID) else value for key, value in binding.items()})

    def _validate_record(self, record, binding):
        if record and record.binding() != {key: str(value) if isinstance(value, UUID) else value for key, value in binding.items()}:
            raise UseAdmissionError("voice_human_review_binding_changed")

    def status(self, project_id, audio_id):
        result = dict(policy=POLICY, purpose="internal_evaluation", project_id=str(project_id), audio_id=str(audio_id),
            state="blocked", current_eligible=False, reasons=[], record=None,
            quality_authorized=False, production_authorized=False, dispatch_authorized=False)
        try:
            audio, claim = self._context(project_id, audio_id)
            record = self.records.get_by_audio(audio_id)
            if record:
                if record.project_id != project_id:
                    raise UseAdmissionError("voice_human_review_binding_changed")
                result["record"] = record.model_dump(mode="json")
            before = self._facts(audio, claim)
            binding = self._binding(project_id, audio, claim)
            self._validate_record(record, binding)
            if before != self._facts(audio, claim):
                raise UseAdmissionError("voice_human_review_authority_changed")
            result.update(state="recorded" if record else "eligible", current_eligible=True,
                expected_qa_fingerprint=self._fingerprint(binding), audio_sha256=audio.content_hash,
                target_text=audio.metadata["voice_generation"]["target_text"])
        except (ValueError, KeyError, TypeError, OSError) as exc:
            result["reasons"] = [str(exc) if isinstance(exc, (UseAdmissionError, ValueError)) else "voice_human_review_evidence_invalid"]
        return result

    def record(self, project_id, audio_id, payload):
        # Typed review consistency must be validated before any persistence.
        review = payload.review(datetime.now(timezone.utc))
        audio, claim = self._context(project_id, audio_id)
        before = self._facts(audio, claim)
        binding = self._binding(project_id, audio, claim)
        if payload.expected_qa_fingerprint != self._fingerprint(binding):
            raise UseAdmissionError("voice_human_review_fingerprint_changed")
        record = self.records.get_by_audio(audio_id)
        self._validate_record(record, binding)
        digest = payload.submission_digest(project_id, audio_id)
        after = self._facts(audio, claim)
        if after[:4] != before[:4] or after[5:] != before[5:]:
            raise UseAdmissionError("voice_human_review_authority_changed")
        with self.db.transaction(immediate=True):
            # A concurrent valid review may be a replay; all other facts stay fixed.
            current = self._facts(audio, claim)
            if current[:4] != before[:4] or current[5:] != before[5:]:
                raise UseAdmissionError("voice_human_review_authority_changed")
            existing = self.records.get_by_audio(audio_id)
            self._validate_record(existing, binding)
            if existing:
                if existing.submission_sha256 != digest:
                    raise UseAdmissionError("voice_human_review_immutable_conflict")
                return existing
            if current[4] != before[4]:
                raise UseAdmissionError("voice_human_review_authority_changed")
            value = EvaluationVoiceReviewRecord(**binding, submission_sha256=digest, review=review)
            try:
                return self.records.create(value)
            except sqlite3.IntegrityError:
                existing = self.records.get_by_audio(audio_id)
                if existing is None or existing.submission_sha256 != digest:
                    raise UseAdmissionError("voice_human_review_immutable_conflict") from None
                self._validate_record(existing, binding)
                return existing
