"""Server-enforced draft revisioning and downstream invalidation.

The browser may clear stale UI state optimistically, but it is not the source
of truth. This module keeps a user-edited scene plan only when it fully matches
the changed script, and invalidates routes, selections, and render specs.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Iterable, Sequence
from uuid import uuid5

from app.db import IPProfileRepository, ProjectDraftRepository
from app.db.database import Database
from app.domain.models import CandidateAsset, DraftRoute, NarrationPerformancePlan, Project, ProjectDraft, ScenePlan, SourceKind, TalkingPerformanceBrief, VideoSpec
from app.narration_performance import validate_narration_performance_plan


_PERFORMANCE_PLAN_UNSET = object()


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
    narration_performance_plan: NarrationPerformancePlan | None | object = _PERFORMANCE_PLAN_UNSET,
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
    # 未选材草稿使用已有 Master 的真实短句；Run 已锁定的输入不自动重构。
    current_run = previous is not None and db.connection.execute(
        "SELECT 1 FROM production_runs WHERE project_id = ? AND draft_version = ? AND script_revision = ? LIMIT 1",
        (str(project.id), previous.version, previous.script_revision),
    ).fetchone() is not None
    if not stored_routes and not stored_confirmed and video_spec is None and not current_run:
        stored_scenes = _split_talking_master_clauses(db, project, script, stored_scenes)
    profile_version = _ip_profile_version(db, project)
    script_changed = previous is None or previous.script != script
    topic_changed = previous is None or previous.topic != topic
    scene_copy_changed = previous is None or _scene_copy_fingerprint(previous.scenes) != _scene_copy_fingerprint(stored_scenes)
    ip_profile_changed = previous is not None and previous.ip_profile_version != profile_version
    upstream_changed = script_changed or topic_changed or scene_copy_changed or ip_profile_changed

    if narration_performance_plan is _PERFORMANCE_PLAN_UNSET:
        # A normal copy edit never guesses how old character anchors should
        # move. A plan remains only when the exact editable copy did not
        # change; historical revisions retain the prior plan for audit.
        performance_plan = None if script_changed else (previous.narration_performance_plan if previous else None)
    elif narration_performance_plan is None:
        performance_plan = None
    elif isinstance(narration_performance_plan, NarrationPerformancePlan):
        performance_plan = narration_performance_plan
    else:
        raise TypeError("narration_performance_plan must be a NarrationPerformancePlan, None, or omitted")
    if performance_plan is not None:
        validate_narration_performance_plan(performance_plan, script or "")

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
        # A changed script may keep the scenes submitted with it only when
        # their ordered spoken copy exactly covers that script. Topic and IP
        # changes still need a fresh plan because their evidence changed.
        aligned_manual_copy = (
            script_changed and not topic_changed and not ip_profile_changed
            and _scene_copy_matches_script(script, stored_scenes)
        )
        if (topic_changed or ip_profile_changed or (script_changed and not aligned_manual_copy)) and not accept_scene_plan_with_new_script:
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
        narration_performance_plan=performance_plan,
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


def _scene_copy_matches_script(script: str | None, scenes: Sequence[ScenePlan]) -> bool:
    if not script or not scenes:
        return False
    from app.voice_qa import comparison_tokens

    planned = tuple(token for scene in scenes for token in comparison_tokens(scene.voice_text))
    target = comparison_tokens(script)
    return bool(target) and planned == target


def _split_talking_master_clauses(db: Database, project: Project, script: str | None,
                                 scenes: list[ScenePlan], *, master=None, max_segment_ms=None) -> list[ScenePlan]:
    """Split editable Talking copy at admitted Master clauses, never at guessed times."""
    speaking_sources = {SourceKind.AI_VIDEO, SourceKind.TALKING_PROFILE}
    if not _scene_copy_matches_script(script, scenes) or not any(
        scene.visual_requirement == "creator_speaking"
        and speaking_sources.intersection((*scene.preferred_sources, *scene.fallback_sources)) for scene in scenes
    ):
        return scenes
    from app.production_preflight import _approved_master
    from app.voice_qa import comparison_tokens, verified_generated_copy_tokens

    master = master or _approved_master(db, project.id, script)
    if master is None or not master.transcript_source or not master.transcript_segments:
        return scenes
    target = comparison_tokens(script)
    segments = master.transcript_segments
    observed = tuple(token for segment in segments for token in comparison_tokens(segment.text))
    if target != observed and verified_generated_copy_tokens(master) != target:
        return scenes
    edges = {0: (0, 0)}
    offset = previous_end = 0
    for index, segment in enumerate(segments):
        tokens = comparison_tokens(segment.text)
        if (not tokens or segment.start_ms < previous_end or segment.end_ms <= segment.start_ms
            or segment.end_ms > master.duration_ms):
            return scenes
        offset += len(tokens)
        edges[offset] = (index + 1, segment.end_ms)
        previous_end = segment.end_ms

    result = []
    offset = 0
    clause_limit = max_segment_ms if type(max_segment_ms) is int and max_segment_ms>0 else TalkingPerformanceBrief().minimum_reference_duration_ms
    for scene in scenes:
        end_offset = offset + len(comparison_tokens(scene.voice_text))
        eligible = (scene.visual_requirement == "creator_speaking"
            and speaking_sources.intersection((*scene.preferred_sources, *scene.fallback_sources))
            and offset in edges and end_offset in edges)
        parts = []
        if eligible:
            first, start_ms = edges[offset]
            last, _ = edges[end_offset]
            if last - first > 1:
                groups, count, group_start = [], 0, start_ms
                for index in range(first, last):
                    segment = segments[index]
                    end_ms = master.duration_ms if end_offset == len(target) and index == last - 1 else segment.end_ms
                    if index > first and end_ms - group_start > clause_limit:
                        previous_ms = segments[index - 1].end_ms
                        if previous_ms > group_start:
                            groups.append((count, previous_ms))
                            group_start = previous_ms
                    count += len(comparison_tokens(segment.text))
                groups.append((count, end_ms))
                texts = _copy_at_token_boundaries(scene.voice_text, [count for count, _ in groups[:-1]]) if len(groups) > 1 else None
                if texts is not None:
                    for text, (_, end_ms) in zip(texts, groups):
                        # 沿用 Master 的连续时轴，尾段包含真实音频尾部；不缩短来迎合参考片。
                        identity = uuid5(scene.id, f"master-clause:{master.id}:{start_ms}:{end_ms}")
                        parts.append(scene.model_copy(update={
                            "id": identity, "scene_id": f"{scene.scene_id[:70]}--speech-{identity.hex[:12]}",
                            "voice_text": text, "duration_target_ms": end_ms - start_ms,
                        }))
                        start_ms = end_ms
        result.extend(parts or [scene])
        offset = end_offset
    if len(result) > 1_000 or len(result) == len(scenes):
        return scenes
    return [scene.model_copy(update={"order": index}) for index, scene in enumerate(result)]


def _copy_at_token_boundaries(text: str, counts: Sequence[int]) -> list[str] | None:
    """Map ASR positions back to exact editable text, retaining punctuation and whitespace."""
    from app.voice_qa import comparison_tokens

    tokens = comparison_tokens(text)
    cuts = [0]
    for count in counts:
        lower, upper = cuts[-1], len(text)
        # 最右侧同 token 数位置保留分句后的标点，不把英语单词切半。
        while lower < upper:
            middle = (lower + upper + 1) // 2
            if len(comparison_tokens(text[:middle])) <= count:
                lower = middle
            else:
                upper = middle - 1
        if (lower <= cuts[-1] or lower >= len(text)
            or comparison_tokens(text[:lower]) != tokens[:count]
            or comparison_tokens(text[lower:]) != tokens[count:]):
            return None
        cuts.append(lower)
    cuts.append(len(text))
    return [text[left:right] for left, right in zip(cuts, cuts[1:])]


def _ip_profile_version(db: Database, project: Project) -> int | None:
    revisions = IPProfileRepository(db).revisions(project.ip_profile_id)
    return revisions[-1][0] if revisions else None
