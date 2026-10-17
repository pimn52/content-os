"""Saved-plan input selection; does not grant execution or bind a ProductionRun."""
from pydantic import BaseModel, ConfigDict, Field
from app.db import ProjectDraftRepository
from app.execution_admission import UseAdmissionError


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
