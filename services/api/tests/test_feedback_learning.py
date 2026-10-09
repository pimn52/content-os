"""Retained synthetic layout evidence; no provider calls or quality approval."""
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.db import Database, IPProfileRepository, ProjectRepository
from app.domain.models import IPProfile, Project, ScenePlan, SourceKind, VisualIntent
from app.main import create_app
from app.providers.scene_planner import ScenePlanResult
from test_presentation_repair import setup


class DuplicatePlanner:
    def plan(self, project, *, script=None, topic=None):
        voice = script or "A genuinely new topic."
        scene = ScenePlan(project_id=project.id, scene_id="hook", order=0, purpose="New topic takeaway",
                          voice_text=voice, duration_target_ms=3000, visual_intent=VisualIntent(subject="idea"),
                          preferred_sources=[SourceKind.TYPOGRAPHY], caption_emphasis=[voice, "New topic takeaway"])
        return ScenePlanResult(project.id, (scene,))


def candidate(client, project, observation, classification="creator_preference"):
    response = client.post(f"/projects/{project}/planning-preferences", json={
        "observation_id": observation["id"], "classification": classification})
    assert response.status_code == 201, response.text
    return response.json()


def change(client, project, rule, enabled=True):
    return client.put(f"/projects/{project}/planning-preferences/{rule['id']}", json={
        "expected_version": rule["version"], "enabled": enabled, "reason": "explicit scoped test adoption"})


def plan(client, project):
    response = client.post(f"/projects/{project}/scene-plan", json={"topic": "Different topic"})
    assert response.status_code == 200, response.text
    return response.json()["scenes"][0]


def test_candidate_adoption_new_topic_disable_reenable_restart(tmp_path):
    path = tmp_path / "learning.sqlite"
    with TestClient(create_app(path, scene_planner=DuplicatePlanner())) as client:
        project, _, _, _, _, obs = setup(client, path)
        rules = f"/projects/{project}/planning-preferences"
        before = plan(client, project)
        assert before["caption_emphasis"][0] == before["voice_text"]
        rule = candidate(client, project, obs)
        assert candidate(client, project, obs) == rule
        assert rule["retained_case"]["passed"] and not rule["stop_reasons"]
        assert plan(client, project)["caption_emphasis"] == before["caption_emphasis"]
        response = change(client, project, rule)
        assert response.status_code == 200, response.text
        enabled = response.json()
        after = plan(client, project)
        assert after["caption_emphasis"] == ["New topic takeaway"]
        assert after["voice_text"] == before["voice_text"]
        assert after["preferred_sources"] == before["preferred_sources"]
        assert f"planning_preference:{rule['id']}:v2" in after["evidence_refs"]
        from app.assembly.edit_plan import resolve_edit_plan, EditPlanPreflightError
        from app.domain.models import CandidateAsset, CostCategory, UsageCost
        with Database(path) as db:
            owner = ProjectRepository(db).get(UUID(project))
        first_scene, next_scene = ScenePlan.model_validate(before), ScenePlan.model_validate(after)
        def selection(scene):
            return {scene.id: CandidateAsset(scene_plan_id=scene.id, source_kind=SourceKind.TYPOGRAPHY,
                    match_score=1, why=["synthetic regression"], estimated_cost=UsageCost(category=CostCategory.TYPOGRAPHY))}
        with pytest.raises(EditPlanPreflightError, match="duplicates"):
            resolve_edit_plan(owner, [first_scene], selection(first_scene), clip_intervals={}, supplied=None)
        resolved = resolve_edit_plan(owner, [next_scene], selection(next_scene), clip_intervals={}, supplied=None)
        assert resolved.scenes[0].graphic_text == "New topic takeaway"
        assert change(client, project, rule).status_code == 409
        draft_before = client.get(f"/projects/{project}/draft").json()
        disabled = change(client, project, enabled, False).json()
        assert client.get(f"/projects/{project}/draft").json() == draft_before
        assert plan(client, project)["caption_emphasis"] == before["caption_emphasis"]
        assert change(client, project, disabled).status_code == 200
        assert len(client.get(rules + f"/{rule['id']}/versions").json()) == 4
    with TestClient(create_app(path, scene_planner=DuplicatePlanner())) as client:
        assert plan(client, project)["caption_emphasis"] == ["New topic takeaway"]


@pytest.mark.parametrize("classification", ["asset_defect", "configuration_limit"])
def test_asset_and_configuration_evidence_not_creator_default(tmp_path, classification):
    path = tmp_path / "classification.sqlite"
    with TestClient(create_app(path, scene_planner=DuplicatePlanner())) as client:
        project, _, _, _, _, obs = setup(client, path)
        rule = candidate(client, project, obs, classification)
        assert "asset_or_configuration_finding_not_creator_default" in rule["stop_reasons"]
        assert change(client, project, rule).status_code == 409
        assert plan(client, project)["caption_emphasis"][0] == "A genuinely new topic."


def test_changed_bytes_profile_revision_and_creator_isolation(tmp_path):
    path = tmp_path / "isolation.sqlite"
    with TestClient(create_app(path, scene_planner=DuplicatePlanner())) as client:
        project, _, _, _, output, obs = setup(client, path)
        rule = candidate(client, project, obs)
        assert change(client, project, rule).status_code == 200
        with Database(path) as db:
            original = ProjectRepository(db).get(UUID(project))
            second = original.model_copy(update={"id": uuid4(), "topic": "second topic"})
            ProjectRepository(db).create(second)
            other_profile = IPProfile(creator_name="Other creator")
            IPProfileRepository(db).create(other_profile)
            other = Project(ip_profile_id=other_profile.id, title="other", topic="other", fps=original.fps, created_at=datetime.now(timezone.utc))
            ProjectRepository(db).create(other)
        assert plan(client, str(second.id))["caption_emphasis"] == ["New topic takeaway"]
        assert plan(client, str(other.id))["caption_emphasis"][0] == "A genuinely new topic."
        assert client.get(f"/projects/{other.id}/planning-preferences").json() == []
        assert change(client, str(other.id), rule).status_code == 404
        output.write_bytes(b"changed synthetic bytes")
        assert plan(client, project)["caption_emphasis"][0] == "A genuinely new topic."
        listed = client.get(f"/projects/{project}/planning-preferences").json()[0]
        assert "preference_render_evidence_changed" in listed["current_stop_reasons"]
        assert change(client, project, {**rule, "version": 2}).status_code == 409
        assert change(client, project, {**rule, "version": 2}, False).status_code == 200
        with Database(path) as db:
            profile = IPProfileRepository(db).get(original.ip_profile_id)
            IPProfileRepository(db).update(profile.model_copy(update={"audience": "Changed audience"}))
        listed = client.get(f"/projects/{project}/planning-preferences").json()[0]
        assert "preference_profile_revision_changed" in listed["current_stop_reasons"]


def test_no_safe_semantic_alternative_does_not_rewrite_speech():
    from app.feedback_learning import concise_emphasis
    scene = ScenePlan(project_id=uuid4(), scene_id="a", order=0, purpose="Full copy",
                      voice_text="Full copy", duration_target_ms=1000, visual_intent=VisualIntent(subject="idea"),
                      preferred_sources=[SourceKind.TYPOGRAPHY], caption_emphasis=["Full copy"])
    assert concise_emphasis(scene) == scene


def test_changed_preference_during_planning_stops_stale_persistence(tmp_path):
    path = tmp_path / "late-policy.sqlite"
    pending = []
    class ChangingPlanner(DuplicatePlanner):
        def plan(self, project, **kwargs):
            if pending:
                from app.feedback_learning import FeedbackLearningService, PreferenceStateRequest
                with Database(path) as db:
                    rule = pending.pop()
                    FeedbackLearningService(db, path.parent / "renders").set_state(project.id, UUID(rule["id"]),
                        PreferenceStateRequest(expected_version=rule["version"], enabled=False, reason="changed while planning"))
            return super().plan(project, **kwargs)
    with TestClient(create_app(path, scene_planner=ChangingPlanner())) as client:
        project, _, _, _, _, obs = setup(client, path)
        enabled = change(client, project, candidate(client, project, obs)).json()
        original = client.get(f"/projects/{project}/draft").json()
        pending.append(enabled)
        response = client.post(f"/projects/{project}/scene-plan", json={"topic": "next", "persist": True})
        assert response.status_code == 409
        assert response.json()["detail"] == "planning_context_changed_replan"
        assert client.get(f"/projects/{project}/draft").json() == original


def test_rule_conflict_has_one_active_version_and_old_evidence_remains(tmp_path):
    import json
    path = tmp_path / "conflict.sqlite"
    with TestClient(create_app(path, scene_planner=DuplicatePlanner())) as client:
        project, run, _, job, _, obs = setup(client, path)
        first = change(client, project, candidate(client, project, obs)).json()
        other_obs = {**obs, "id": str(uuid4()), "request": {**obs["request"], "idempotency_key": "other-observation"}}
        with Database(path) as db:
            db.connection.execute("INSERT INTO presentation_observations VALUES (?,?,?,?,?,?)", (
                other_obs["id"], project, run["id"], str(job.id), "other-observation", json.dumps(other_obs)))
        second = candidate(client, project, other_obs)
        assert change(client, project, second).status_code == 200
        values = client.get(f"/projects/{project}/planning-preferences").json()
        assert len([v for v in values if v["state"] == "enabled"]) == 1
        superseded = next(v for v in values if v["id"] == first["id"])
        assert superseded["state"] == "disabled" and superseded["version"] == 3
        assert len(client.get(f"/projects/{project}/planning-preferences/{first['id']}/versions").json()) == 3
