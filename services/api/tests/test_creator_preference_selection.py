"""Creator selection is authority for preference, not media evidence."""
from uuid import UUID

from fastapi.testclient import TestClient

from app.db import Database, IPProfileRepository, ProjectRepository
from app.main import create_app
from test_feedback_learning import DuplicatePlanner, candidate, change, plan
from test_presentation_repair import setup


def select(client, project):
    root = f"/projects/{project}"
    scope = client.get(root + "/planning-preference-selection").json()
    response = client.post(root + "/planning-preference-selection", json={
        "rule": scope["rule"], "expected_profile_version": scope["profile_version"], "reason": "explicit creator choice"})
    assert response.status_code == 201, response.text
    return response.json()


def test_direct_selection_changes_new_planning_without_fabricated_observation(tmp_path):
    path = tmp_path / "selection.sqlite"
    with TestClient(create_app(path, scene_planner=DuplicatePlanner())) as client:
        project = client.post("/projects", json={"title": "new", "topic": "new"}).json()["id"]
        before = plan(client, project)
        value = select(client, project)
        assert value["authority_source"] == "creator_selection"
        assert value["observation_id"] is None and value["retained_case"] is None
        assert select(client, project) == value
        assert plan(client, project)["caption_emphasis"] == before["caption_emphasis"]
        enabled = change(client, project, value).json()
        after = plan(client, project)
        assert after["caption_emphasis"] == ["New topic takeaway"]
        assert f"creator_selection:{value['id']}:v2" in after["evidence_refs"]
        assert not any("presentation_observation:" in ref for ref in after["evidence_refs"])
        assert client.get(f"/projects/{project}/planning-observations").json() == []
        disabled = change(client, project, enabled, False).json()
        assert select(client, project) == disabled
        assert plan(client, project)["caption_emphasis"] == before["caption_emphasis"]
    with TestClient(create_app(path)) as client:
        assert client.get(f"/projects/{project}/planning-preferences").json()[0]["state"] == "disabled"


def test_selection_profile_revision_reset_requires_fresh_adoption(tmp_path):
    path = tmp_path / "revision.sqlite"
    with TestClient(create_app(path, scene_planner=DuplicatePlanner())) as client:
        project = client.post("/projects", json={"title": "new", "topic": "new"}).json()["id"]
        enabled = change(client, project, select(client, project)).json()
        with Database(path) as db:
            owner = ProjectRepository(db).get(UUID(project))
            profile = IPProfileRepository(db).get(owner.ip_profile_id)
            IPProfileRepository(db).update(profile.model_copy(update={"audience": "new audience"}))
        assert plan(client, project)["caption_emphasis"][0] == "A genuinely new topic."
        assert change(client, project, enabled).status_code == 409
        renewed = select(client, project)
        assert renewed["version"] == 3 and renewed["state"] == "candidate" and renewed["profile_version"] == 2
        assert renewed["profile_revision_origin"] == "recorded_profile_revision"
        assert change(client, project, renewed).status_code == 200


def test_direct_selection_wins_and_feedback_cannot_silently_override(tmp_path):
    path = tmp_path / "precedence.sqlite"
    with TestClient(create_app(path, scene_planner=DuplicatePlanner())) as client:
        project, _, _, _, _, obs = setup(client, path)
        observed = candidate(client, project, obs)
        change(client, project, observed)
        direct = change(client, project, select(client, project)).json()
        rules = client.get(f"/projects/{project}/planning-preferences").json()
        old = next(r for r in rules if r["id"] == observed["id"])
        assert old["state"] == "disabled"
        assert change(client, project, old).status_code == 409
        assert change(client, project, direct, False).status_code == 200
        assert plan(client, project)["caption_emphasis"][0] == "A genuinely new topic."
        assert change(client, project, old).status_code == 200


def test_migration_preserves_observed_preferences_and_versions(tmp_path, monkeypatch):
    from app.db import migrations
    path = tmp_path / "migration.sqlite"
    all_migrations = migrations._MIGRATIONS
    with monkeypatch.context() as patch:
        patch.setattr(migrations, "_MIGRATIONS", tuple(item for item in all_migrations if item[0] <= 39))
        # Construct the pre-use-constraint historical fixture without invoking
        # newer hooks against its intentionally old schema. Production still
        # requires its normal migrations; no missing-table fallback is added.
        from app.source_use_constraints import SourceUseConstraintService
        patch.setattr(SourceUseConstraintService, "records", lambda self, project_id: [])
        patch.setattr(SourceUseConstraintService, "snapshot_render", lambda self, project_id, job_id: None)
        with TestClient(create_app(path)) as client:
            project, _, _, _, _, obs = setup(client, path)
            enabled = change(client, project, candidate(client, project, obs)).json()
            history = client.get(f"/projects/{project}/planning-preferences/{enabled['id']}/versions").json()
    with TestClient(create_app(path)) as client:
        assert client.get(f"/projects/{project}/planning-preferences").json()[0]["id"] == enabled["id"]
        assert client.get(f"/projects/{project}/planning-preferences/{enabled['id']}/versions").json() == history
        assert select(client, project)["observation_id"] is None
    with Database(path) as db:
        assert db.connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_legacy_profile_baseline_is_labeled_and_does_not_rewrite_profile(tmp_path):
    path = tmp_path / "legacy-profile.sqlite"
    with TestClient(create_app(path)) as client:
        project = client.post("/projects", json={"title": "old", "topic": "old"}).json()["id"]
    with Database(path) as db:
        owner = ProjectRepository(db).get(UUID(project))
        original = IPProfileRepository(db).get(owner.ip_profile_id)
        db.connection.execute("DELETE FROM ip_profile_revisions WHERE profile_id=?", (str(owner.ip_profile_id),))
        db.connection.execute("DELETE FROM schema_migrations WHERE version=41")
        db.connection.execute("DROP TABLE ip_profile_revision_baselines")
    with TestClient(create_app(path)) as client:
        scope = client.get(f"/projects/{project}/planning-preference-selection").json()
        assert scope["profile_revision_origin"] == "migration_snapshot_not_historical_revision"
        assert scope["profile_version"] == 1
        assert select(client, project)["authority_source"] == "creator_selection"
    with Database(path) as db:
        assert IPProfileRepository(db).get(owner.ip_profile_id) == original
        assert IPProfileRepository(db).revisions(owner.ip_profile_id)[0][2] == original
