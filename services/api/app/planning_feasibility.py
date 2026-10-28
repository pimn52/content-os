"""Read-only planning hints, never production admission or a cost quotation."""
from __future__ import annotations

import json
from uuid import UUID

from app.budget import snapshot
from app.db import (
    AssetRepository, BudgetPolicyRepository, ClipRepository, Database,
    IPProfileRepository, ProjectRepository, ProviderCallRepository,
    ProviderMachineCapabilityProfileRepository, TalkingProfileRepository,
)
from app.execution_admission import UseAdmissionError
from app.voice_plan import resolve_project_voice_profile


# 兼容旧导入；规划与执行共用同一动作参考池。
from app.talking.pool import (build_talking_pool, configured_talking_choices,
    required_talking_reference_duration_ms, talking_reference_capacity)


def _creator_options(db: Database, project_id: UUID) -> dict[str, object]:
    """Bounded identity hints; neither reference suitability nor execution approval."""
    voice: dict[str, object] = {
        "default_binding": "unknown", "profile_id": None,
        "reference_state": "unknown", "reference_clip_count": 0,
    }
    talking: dict[str, object] = {"state": "unknown", "references": [], "truncated": False}
    project = ProjectRepository(db).get(project_id)
    ip = IPProfileRepository(db).get(project.ip_profile_id) if project else None
    if ip is not None:
        clips, assets = ClipRepository(db), AssetRepository(db)
        try:
            profile = resolve_project_voice_profile(db, project_id)
        except UseAdmissionError:
            pass
        else:
            references = [clips.get(value) for value in profile.reference_clip_ids]
            valid = [clip for clip in references if clip is not None
                     and (asset := assets.get(clip.asset_id)) is not None
                     and asset.metadata.get("ip_profile_id") in (None, str(ip.id))]
            voice = {
                "default_binding": "matched", "profile_id": str(profile.id),
                "reference_state": "present" if len(valid) == len(references) else "incomplete",
                "reference_clip_count": len(valid),
            }
        matches = []
        for profile in sorted(TalkingProfileRepository(db).list(), key=lambda value: str(value.id)):
            if not profile.consent.confirmed:
                continue
            for clip_id in sorted(set(profile.reference_clip_ids), key=str):
                clip = clips.get(clip_id)
                asset = assets.get(clip.asset_id) if clip is not None else None
                # TalkingProfile has no IP binding. Names and consent subjects
                # cannot establish that another person's footage is this IP.
                if asset is None or asset.metadata.get("ip_profile_id") != str(ip.id):
                    continue
                matches.append({
                    "profile_id": str(profile.id), "clip_id": str(clip.id),
                    "duration_ms": clip.end_ms - clip.start_ms,
                    "talking_candidate": clip.talking_candidate,
                    "reference_assessment_present": clip.talking_reference_assessment is not None,
                })
        talking = {
            "state": "matched_references" if matches else "unknown",
            "references": matches[:8], "truncated": len(matches) > 8,
        }
    return {
        "voice": voice, "talking": talking,
        "quality_state": "unverified", "dispatch_authorized": False,
    }


def planning_feasibility(db: Database, project_id: UUID, *, talking_provider=None, data_root=None) -> dict[str, object]:
    """Separate stable constraints from live observations for replay identity.

    Usage is sampled before the planning reservation. It may change during this
    call and must be checked again by normal preflight/dispatch, not used as a
    content-evidence version or an idempotency identity component.
    """
    profiles = ProviderMachineCapabilityProfileRepository(db).list()
    capabilities = {}
    for capability in ("voice", "talking"):
        candidates = [p for p in profiles if p.capability == capability]
        capabilities[capability] = {
            "evidence_state": "verified" if any(
                p.readiness == "verified" and p.quality_status == "verified"
                and p.evidence_reference for p in candidates
            ) else "unverified" if candidates else "unknown",
            # Only public, typed status fields: no free-form provenance,
            # parameters, paths, credentials or evidence content.
            "profiles": [{
                "id": str(p.id), "updated_at": p.updated_at.isoformat(),
                "readiness": p.readiness, "quality_status": p.quality_status,
                "commercial_status": p.commercial_status,
                "license_evidence_present": bool(p.license_evidence_reference),
            } for p in sorted(candidates, key=lambda p: str(p.id))],
            "current_execution_license": "unverified",
            "dispatch_authorized": False,
            "estimated_cost": "unknown",
        }
    policies = []
    observations = []
    policy_repo = BudgetPolicyRepository(db)
    calls = ProviderCallRepository(db)
    for scope, scope_id in (("global", None), ("project", project_id)):
        policy = policy_repo.get_by_project(scope_id)
        if policy is None:
            policies.append({"scope": scope, "state": "unknown"})
            continue
        policies.append({
            "scope": scope, "state": "configured", "currency": policy.currency,
            "max_calls": policy.max_calls,
            "max_amount": str(policy.max_amount) if policy.max_amount is not None else None,
            "allow_unknown_cost": policy.allow_unknown_cost,
        })
        spent = snapshot(calls.list() if scope_id is None else calls.list_for_project(project_id), policy.currency)
        observations.append({
            "scope": scope, "calls": spent.calls, "reserved_calls": spent.reserved_calls,
            "remaining_calls": max(0, policy.max_calls - spent.calls) if policy.max_calls is not None else None,
            "known_amount": str(spent.known_amount), "unknown_cost_calls": spent.unknown_cost_calls,
        })
    selected_sources = []
    seen = set()
    for profile, clip, capability in configured_talking_choices(db, project_id):
        asset = AssetRepository(db).get(clip.asset_id)
        if (asset.id, clip.id) in seen:
            continue
        seen.add((asset.id, clip.id))
        provider_matches = (getattr(talking_provider, "provider_name", None) == capability.provider
            and getattr(talking_provider, "model", None) == capability.model)
        selected_sources.append({"profile_id": str(profile.id), "asset_id": str(asset.id),
            "clip_id": str(clip.id), "start_ms": clip.start_ms, "end_ms": clip.end_ms,
            "duration_ms": clip.end_ms - clip.start_ms,
            **talking_reference_capacity(talking_provider if provider_matches else None, clip.end_ms - clip.start_ms)})
    from app.source_usage import SourceUsageService, SourceUsageConflict
    try:
        occupied = SourceUsageService(db).occupied(project_id)
        source_history = {"state":"known", "intervals":occupied[:100], "truncated":len(occupied)>100}
    except (SourceUsageConflict, KeyError, ValueError):
        source_history = {"state":"unresolved", "intervals":[], "truncated":False}
    project = ProjectRepository(db).get(project_id)
    feedback = []
    if project is not None:
        owned = {str(value.id) for value in ProjectRepository(db).list() if value.ip_profile_id == project.ip_profile_id}
        for row in db.connection.execute("SELECT payload FROM presentation_observations ORDER BY rowid DESC"):
            value = json.loads(row[0])
            if value["project_id"] in owned:
                feedback.append({"observation_id":value["id"], "project_id":value["project_id"],
                    "scene_id":value["request"]["scene_id"], "finding":value["request"]["finding"],
                    "reason":value["request"]["reason"][:1000]})
                if len(feedback) == 12:
                    break
    return {
        "authority": "planning_hint_not_admission_or_authorization",
        "capabilities": capabilities, "creator_options": _creator_options(db, project_id),
        "selected_talking_sources": selected_sources[:8],
        **({"talking_motion_pool": build_talking_pool(db, project_id, talking_provider=talking_provider,
            data_root=data_root).summary(talking_provider=talking_provider)} if selected_sources else {}),
        "visible_source_occupancy": source_history,
        "recent_quality_feedback": feedback,
        "budget_policies": policies,
        "live_usage_observation": observations,
        "usage_observation_timing": "before_this_planning_call_reservation",
        "recheck": "normal_production_preflight_and_dispatch",
        "local_compute_time": "unknown", "manual_work_time": "unknown",
    }


def planning_identity_context(context: dict[str, object]) -> dict[str, object]:
    """Exclude only live counters; retain capability and budget policy fences."""
    brief = context.get("production_feasibility")
    if not isinstance(brief, dict):
        return context
    return {**context, "production_feasibility": {
        key: value for key, value in brief.items() if key != "live_usage_observation"
    }}
