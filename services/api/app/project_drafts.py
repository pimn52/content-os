"""Server-enforced draft revisioning and downstream invalidation.

The browser may clear stale UI state optimistically, but it is not the source
of truth.  This module makes a changed script, scene copy, topic, or IP
revision invalidate derived routes, selections, and render specifications even
when an old client retries a previously assembled payload.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Iterable, Sequence

from app.db import IPProfileRepository, ProjectDraftRepository
from app.db.database import Database
from app.domain.models import CandidateAsset, DraftRoute, Project, ProjectDraft, ScenePlan, VideoSpec


def save_project_draft(
    db: Database,
    project: Project,
    *,
    script: str | None,
    topic: str | None,
    scenes: Sequence[ScenePlan],
    routes: Sequence[DraftRoute],
    confirmed: Sequence[CandidateAsset],
    video_spec: VideoSpec | None,
    evidence_refs: Iterable[str] = (),
    accept_scene_plan_with_new_script: bool = False,
    now: datetime | None = None,
) -> ProjectDraft:
    """Persist one draft update and discard artifacts derived from old inputs.

    ``accept_scene_plan_with_new_script`` is only for a scene-plan response
    produced in the same server request.  A normal script/topic edit cannot
    smuggle its prior ScenePlan, routes, or render back into the current
    revision by re-sending browser state.
    """

    if not isinstance(project, Project):
        raise TypeError("draft persistence requires a Project contract")
    selected_now = now or datetime.now(timezone.utc)
    if selected_now.tzinfo is None or selected_now.utcoffset() is None:
        raise ValueError("draft persistence timestamps must be timezone-aware")

    stored_scenes = list(scenes)
    stored_routes = list(routes)
    stored_confirmed = list(confirmed)
    refs = list(dict.fromkeys(str(value) for value in evidence_refs if str(value)))
    repository = ProjectDraftRepository(db)
    previous = repository.get(project.id)
    profile_version = _ip_profile_version(db, project)
    script_changed = previous is None or previous.script != script
    topic_changed = previous is None or previous.topic != topic
    scene_copy_changed = previous is None or _scene_copy_fingerprint(previous.scenes) != _scene_copy_fingerprint(stored_scenes)
    ip_profile_changed = previous is not None and previous.ip_profile_version != profile_version
    upstream_changed = script_changed or topic_changed or scene_copy_changed or ip_profile_changed

    reasons: list[str] = []
    if script_changed:
        reasons.append("script_changed")
    if topic_changed:
        reasons.append("topic_changed")
    if scene_copy_changed:
        reasons.append("scene_copy_changed")
    if ip_profile_changed:
        reasons.append("ip_profile_changed")

    if upstream_changed and previous is not None:
        # A user edit to the prose or topic cannot retain an old plan. A
        # freshly returned server-side ScenePlan is the one narrow exception;
        # it is still paired with empty downstream artifacts.
        if (script_changed or topic_changed or ip_profile_changed) and not accept_scene_plan_with_new_script:
            stored_scenes = []
        stored_routes = []
        stored_confirmed = []
        video_spec = None
        invalidation_reasons = reasons
    elif previous is None:
        # The initial project draft has no earlier output to invalidate. It
        # may legitimately arrive with a user-authored ScenePlan and routes.
        invalidation_reasons = []
    elif stored_routes or stored_confirmed or video_spec is not None:
        # The caller rebuilt a derived stage against the unchanged current
        # input, so the stale marker is no longer a current-state warning.
        invalidation_reasons = []
    else:
        invalidation_reasons = previous.invalidation_reasons if previous is not None else []

    fingerprint = draft_input_fingerprint(script=script, topic=topic, scenes=stored_scenes)

    script_revision = (
        1 if previous is None and (script is not None or topic is not None or stored_scenes)
        else 0 if previous is None
        else previous.script_revision + 1 if upstream_changed
        else previous.script_revision
    )
    value = ProjectDraft(
        project_id=project.id,
        version=1 if previous is None else previous.version + 1,
        script_revision=script_revision,
        script=script,
        topic=topic,
        scenes=stored_scenes,
        routes=stored_routes,
        confirmed=stored_confirmed,
        video_spec=video_spec,
        ip_profile_version=profile_version,
        evidence_refs=refs,
        input_fingerprint=fingerprint,
        invalidation_reasons=invalidation_reasons,
        updated_at=selected_now,
    )
    if previous is not None:
        comparable = value.model_copy(update={"version": previous.version, "updated_at": previous.updated_at})
        if comparable == previous:
            return previous
    return repository.save(value)


def draft_input_fingerprint(*, script: str | None, topic: str | None, scenes: Sequence[ScenePlan]) -> str:
    """Stable identity for the text and ordered scene copy that drives output."""

    document = {
        "script": script,
        "topic": topic,
        "scenes": [scene.model_dump(mode="json") for scene in scenes],
    }
    encoded = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _scene_copy_fingerprint(scenes: Sequence[ScenePlan]) -> str:
    return draft_input_fingerprint(script=None, topic=None, scenes=scenes)


def _ip_profile_version(db: Database, project: Project) -> int | None:
    revisions = IPProfileRepository(db).revisions(project.ip_profile_id)
    return revisions[-1][0] if revisions else None
