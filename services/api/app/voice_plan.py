"""Saved-plan input selection; does not grant execution or bind a ProductionRun."""
from pydantic import BaseModel, ConfigDict, Field
from app.db import ProjectDraftRepository
from app.execution_admission import UseAdmissionError


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
