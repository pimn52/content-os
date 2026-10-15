"""Read-only audio audition boundary; scope is never a quality/dispatch grant."""
from pathlib import Path
import hashlib
from uuid import UUID

from app.db.repositories import JobRepository, AssetRepository, ImageAssetRepository
from app.execution_admission import UseAdmissionError
from app.execution_scope import ExecutionScopeService, read_job_scope
from app.media.local_paths import existing_local_media_file


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def require_ordinary_media(db, root, path, *, kind, subject_id, content_hash=None):
    """Aliases cannot turn known evaluation bytes into ordinary media."""
    service = ExecutionScopeService(db, root)
    scopes = list(service.scopes_for_hash(file_digest(path)))
    if content_hash:
        scopes.extend(service.scopes_for_hash(content_hash))
    subject = service.get(kind, subject_id)
    if subject:
        scopes.append(subject)
    media = {"asset": AssetRepository, "image": ImageAssetRepository}.get(kind)
    value = media(db).get(subject_id) if media else None
    for marker in ("voice_generation", "talking_generation", "talking_run_preview"):
        provenance = value.metadata.get(marker) if value else None
        if isinstance(provenance, dict) and provenance.get("job_id"):
            try:
                job = JobRepository(db).get(UUID(provenance["job_id"]))
            except (ValueError, TypeError):
                raise UseAdmissionError("execution_use_origin_invalid") from None
            if job and (producer := read_job_scope(db, job)):
                scopes.append(producer)
    if kind == "render":
        from app.evaluation_composition import is_composition
        if is_composition(db,render_id=subject_id):
            raise UseAdmissionError("evaluation_scope_not_commercial")
        job = JobRepository(db).get(subject_id)
        if job and (producer := read_job_scope(db, job)):
            scopes.append(producer)
    if any(scope.purpose == "internal_evaluation" for scope in scopes):
        raise UseAdmissionError("evaluation_scope_not_commercial")


class AudioAccess:
    def __init__(self, db, root):
        self.db, self.root = db, Path(root).resolve()
        self.service = ExecutionScopeService(db, root)

    def inspect(self, audio, *, project_id=None, purpose=None, operation="review"):
        result = {"audio_id": str(audio.id), "state": "blocked", "reasons": [],
            "operation": "review", "purpose": purpose, "project_id": str(project_id) if project_id else None,
            "quality_authorized": False, "download_authorized": False,
            "dispatch_authorized": False, "manifest": {"lineage": "unknown", "scopes": [],
                "commercial_authorized": False, "execution_authorized": False}}
        try:
            result["manifest"] = self.service.manifest("audio", audio.id, content_hash=audio.content_hash)
            from app.voice_evaluation_qa import has_qa_reference, qa_review_state
            qa_policy, producer_audio = None, audio
            if has_qa_reference(self.db, audio):
                qa_policy, producer_audio = qa_review_state(self.db, self.root, audio,
                    project_id=project_id, purpose=purpose)
            if operation != "review":
                raise UseAdmissionError("provider_use_operation_unsupported")
            subject = self.service.get("audio", audio.id)
            scopes = list(self.service.scopes_for_hash(audio.content_hash))
            if subject:
                scopes.append(subject)
                origin = self.db.connection.execute(
                    "SELECT content_hash FROM execution_use_content_origins WHERE subject_kind='audio' AND subject_id=?",
                    (str(audio.id),)).fetchall()
                if not origin or any(row[0] != audio.content_hash for row in origin):
                    raise UseAdmissionError("execution_use_origin_missing")
            job = producer = None
            generation = audio.metadata.get("voice_generation")
            if isinstance(generation, dict) and generation.get("job_id"):
                try:
                    job = JobRepository(self.db).get(UUID(generation["job_id"]))
                except (ValueError, TypeError):
                    raise UseAdmissionError("execution_use_origin_invalid") from None
                producer = read_job_scope(self.db, job) if job else None
                if subject and subject.purpose == "internal_evaluation" and producer is None:
                    raise UseAdmissionError("execution_use_origin_missing")
                if producer:
                    if producer.model_dump(mode="json") not in result["manifest"]["scopes"]:
                        result["manifest"]["scopes"].append(producer.model_dump(mode="json"))
                    result["manifest"]["lineage"] = "known"
                    if job.type.value != "generate_voice" or generation.get("project_id") not in (None, str(job.project_id)):
                        raise UseAdmissionError("execution_use_origin_invalid")
                    if subject is None:
                        raise UseAdmissionError("execution_use_origin_missing")
                    if producer.project_id != subject.project_id or any(binding not in subject.bindings for binding in producer.bindings):
                        raise UseAdmissionError("execution_use_ancestor_dropped")
                    from app.voice_calibration import require_output
                    require_output(self.db, producer_audio, job, producer)
                    scopes.append(producer)
            source = existing_local_media_file(audio.source_file, self.root)
            actual = file_digest(source)
            scopes.extend(self.service.scopes_for_hash(actual))
            for scope in scopes:
                value = scope.model_dump(mode="json")
                if value not in result["manifest"]["scopes"]:
                    result["manifest"]["scopes"].append(value)
            if scopes:
                result["manifest"]["lineage"] = "known"
            restricted = any(scope.purpose == "internal_evaluation" for scope in scopes)
            if scopes and actual != audio.content_hash:
                raise UseAdmissionError("execution_use_content_changed")
            if restricted:
                if not source.is_relative_to(self.root):
                    raise UseAdmissionError("execution_use_path_escape")
                if project_id is None or purpose is None:
                    raise UseAdmissionError("execution_use_review_context_required")
                from app.voice_calibration import calibration_receipts, require_completed_review
                if any(calibration_receipts(self.db, scope) for scope in scopes):
                    if job is None or producer is None:
                        raise UseAdmissionError("execution_use_origin_missing")
                    result["review_policy"] = require_completed_review(self.db, self.root, producer_audio, job, producer,
                        scopes, project_id=project_id, purpose=purpose)
                    if qa_policy is not None:
                        result["review_policy"] = qa_policy
                else:
                    for scope in scopes:
                        self.service.require_current(scope, project_id=project_id, purpose=purpose, operation="review")
            elif purpose not in (None, "commercial_production"):
                raise UseAdmissionError("execution_use_lineage_unknown")
            result["state"] = "review_available" if restricted else "legacy_available"
            result["restricted"] = restricted
            return result, source
        except UseAdmissionError as exc:
            result["reasons"] = [str(exc)]
        except FileNotFoundError:
            result["reasons"] = ["audio_source_missing"]
        except (ValueError, OSError):
            result["reasons"] = ["audio_source_invalid"]
        return result, None
