"""Saved-plan input selection; does not grant execution or bind a ProductionRun."""
from pydantic import BaseModel, ConfigDict, Field
from app.db import ProjectDraftRepository
from app.execution_admission import UseAdmissionError


def is_current_run_voice(db, run_id, audio):
    """技术合格的新旁白只用于自己的待审视频，不继承任何人工通过。"""
    from uuid import UUID
    from app.db import JobRepository
    from app.domain.models import JobStatus,VoiceGenerationJobPayload,VoiceQaJobPayload
    from app.voice_qa import voice_human_review_status
    if db is None or run_id is None:
        return False
    row=db.connection.execute("SELECT * FROM production_runs WHERE id=? AND automatic=1",(str(run_id),)).fetchone()
    generation=audio.metadata.get("voice_generation")
    if (row is None or row["stage"]=="cancelled" or row["voice_audio_id"]!=str(audio.id)
        or not row["voice_job_id"] or not row["voice_qa_job_id"] or not isinstance(generation,dict)
        or generation.get("qa_state")!="verified" or generation.get("human_review_state")=="rejected"
        or voice_human_review_status(audio)=="rejected"):
        return False
    voice=JobRepository(db).get(UUID(row["voice_job_id"]))
    qa=JobRepository(db).get(UUID(row["voice_qa_job_id"]))
    return bool(voice and qa and voice.status is JobStatus.COMPLETED and qa.status is JobStatus.COMPLETED
        and isinstance(voice.payload,VoiceGenerationJobPayload) and isinstance(qa.payload,VoiceQaJobPayload)
        and str(voice.project_id)==row["project_id"] and qa.project_id==voice.project_id
        and qa.payload.narration_audio_id==audio.id and qa.payload.target_text==voice.payload.text
        and generation.get("job_id")==str(voice.id) and generation.get("project_id")==row["project_id"]
        and generation.get("target_text")==voice.payload.text
        and generation.get("voice_profile_id")==str(voice.payload.voice_profile_id)
        and generation.get("provider")==voice.payload.expected_provider and generation.get("model")==voice.payload.expected_model
        and audio.authorization_reference==voice.payload.authorization_reference)


def resolve_project_voice_profile(db, project_id, profile_id=None, cross_ip_authorization_reference=None):
    """One project identity and an explicit, persisted exception for narration."""
    from app.db import ProjectRepository, IPProfileRepository, VoiceProfileRepository
    project = ProjectRepository(db).get(project_id)
    ip = IPProfileRepository(db).get(project.ip_profile_id) if project else None
    if ip is None:
        raise UseAdmissionError("voice_project_ip_required")
    selected = profile_id or ip.default_voice_profile_id
    profile = VoiceProfileRepository(db).get(selected) if selected else None
    if profile is None or not profile.consent.confirmed:
        raise UseAdmissionError("project_authorized_voice_profile_required")
    if profile.ip_profile_id is None or IPProfileRepository(db).get(profile.ip_profile_id) is None:
        raise UseAdmissionError("voice_profile_ip_binding_required")
    if profile.ip_profile_id != ip.id and (profile_id is None or not (cross_ip_authorization_reference or "").strip()):
        raise UseAdmissionError("cross_ip_voice_explicit_authorization_required")
    return profile


def require_project_voice_audio(db, project_id, audio):
    """A good voice take is not automatically the project's voice identity."""
    from uuid import UUID
    from app.db import JobRepository
    generation = audio.metadata.get("voice_generation",{})
    identity = generation.get("voice_profile_id")
    if not identity:
        raise UseAdmissionError("master_voice_identity_required")
    job = JobRepository(db).get(UUID(generation["job_id"])) if generation.get("job_id") else None
    reference = None
    if job and job.project_id == project_id and getattr(job.payload,"voice_profile_id",None) == UUID(identity):
        reference = job.payload.cross_ip_voice_authorization_reference
    return resolve_project_voice_profile(db,project_id,UUID(identity),reference)


class SavedVoicePlanSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: int = Field(ge=0, strict=True)
    script_revision: int = Field(ge=0, strict=True)


def require_saved_voice_plan(db, project_id, source, text):
    draft = ProjectDraftRepository(db).get(project_id)
    if draft is None or not draft.scenes:
        raise UseAdmissionError("voice_saved_plan_required")
    copy = draft.script or "\n\n".join(s.voice_text for s in sorted(draft.scenes, key=lambda s: s.order))
    if (draft.version != source.version or draft.script_revision != source.script_revision
            or not copy.strip() or copy != text):
        raise UseAdmissionError("voice_saved_plan_changed")
    return draft.model_dump_json()
