"""Ordinary reviewed Talking reuses registered sources without evaluation admission."""
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import pytest

from app.db import (
    AssetRepository, ClipRepository, Database, ProjectDraftRepository, ProviderCallRepository,
    ProviderMachineCapabilityProfileRepository, TalkingProfileRepository,
    TalkingSliceSeriesRepository,
)
from app.main import create_app
from app.production_runs import ProductionRunService
from app.talking.admission import talking_visual_blocker
from app.talking.source_admission import TalkingSourceAdmissionRepository
from test_production_runs import _fixture_reviewable_planned_preview


@pytest.fixture
def admitted(tmp_path, monkeypatch):
    path = tmp_path / "ordinary-admitted.sqlite"
    with TestClient(create_app(path)) as client:
        project, waiting, binding, preview = _fixture_reviewable_planned_preview(client, path, monkeypatch)
        url = f"/projects/{project}/talking-slice-series/{binding['talking_series_id']}"
        reviewed = client.post(url + "/continuity-review", json={
            "approved": True, "evidence_reference": "fixture:exact-preview-review",
            "findings": ["Fixture only, not a real quality judgment"],
            "preview_asset_id": str(preview.id), "preview_sha256": preview.content_hash,
        })
        assert reviewed.status_code == 201, reviewed.text
        promoted = client.post(url + "/talking-run")
        assert promoted.status_code == 201, promoted.text
        record = promoted.json()
    return SimpleNamespace(path=path, project=UUID(project), production_run_id=UUID(waiting["id"]),
        binding=binding, record=record)


def _blocker(db, fixture, *, copy="A fresh creator point."):
    assets = AssetRepository(db)
    asset = assets.get(UUID(fixture.record["assembled_asset_id"]))
    clip = ClipRepository(db).get(UUID(fixture.record["assembled_clip_id"]))
    return talking_visual_blocker(asset, clip, assets, project_id=fixture.project, copy=copy)


def _forbid_evaluation_lookup(self, source_admission_id):
    raise AssertionError("ordinary sources must not consult an evaluation admission")


def test_normal_reviewed_talking_reuses_source_without_evaluation_permission(admitted, monkeypatch):
    monkeypatch.setattr(TalkingSourceAdmissionRepository, "get", _forbid_evaluation_lookup)
    with Database(admitted.path) as db:
        series = TalkingSliceSeriesRepository(db).get(UUID(admitted.binding["talking_series_id"]))
        assert series.planned_origin.source_admission_id is None
        assert _blocker(db, admitted) is None
        assert ProviderCallRepository(db).list() == []
        assert db.connection.execute("SELECT COUNT(*) FROM talking_source_admissions").fetchone()[0] == 0


@pytest.mark.parametrize("change,expected", [
    ("consent", "talking_run_source_context_invalid"),
    ("source_bytes", "talking_reference_bytes_changed"),
    ("license", "planned_talking_execution_authority_changed"),
    ("binding", "planned_talking_execution_authority_changed"),
    ("admitted_origin", "planned_talking_admitted_binding_changed"),
])
def test_normal_reuse_still_rechecks_registered_source_facts(admitted, monkeypatch, change, expected):
    monkeypatch.setattr(TalkingSourceAdmissionRepository, "get", _forbid_evaluation_lookup)
    with Database(admitted.path) as db:
        binding = admitted.binding
        if change == "consent":
            profiles = TalkingProfileRepository(db)
            profile = profiles.get(UUID(binding["talking_profile_id"]))
            updated = profile.model_copy(update={"consent": profile.consent.model_copy(update={"confirmed": False})})
            db.connection.execute("UPDATE talking_profiles SET payload=? WHERE id=?",
                (updated.model_dump_json(), str(profile.id)))
        elif change == "source_bytes":
            source = AssetRepository(db).get(UUID(binding["reference_asset_id"]))
            (admitted.path.parent / source.source_file).write_bytes(b"changed source after review")
        elif change == "license":
            repo = ProviderMachineCapabilityProfileRepository(db)
            cap = repo.get(UUID(binding["capability_profile_id"]))
            repo.save(cap.model_copy(update={"commercial_status": "unknown", "license_evidence_reference": None}))
        elif change == "admitted_origin":
            from app.db import TalkingRunRepository
            repo = TalkingRunRepository(db)
            run = repo.get(UUID(admitted.record["id"]))
            changed = run.model_copy(update={"planned_origin_sha256": "f" * 64})
            db.connection.execute("UPDATE talking_runs SET payload=? WHERE id=?", (changed.model_dump_json(), str(run.id)))
        else:
            row = db.connection.execute("SELECT talking_source_bindings FROM production_runs WHERE id=?",
                (str(admitted.production_run_id),)).fetchone()
            bindings = json.loads(row["talking_source_bindings"])
            bindings[0]["reference_asset_hash"] = "f" * 64
            db.connection.execute("UPDATE production_runs SET talking_source_bindings=? WHERE id=?",
                (json.dumps(bindings), str(admitted.production_run_id)))
        assert _blocker(db, admitted) == expected
        assert ProviderCallRepository(db).list() == []


def test_reviewed_source_can_be_reused_after_draft_changes_but_copy_must_still_match(admitted, monkeypatch):
    monkeypatch.setattr(TalkingSourceAdmissionRepository, "get", _forbid_evaluation_lookup)
    with Database(admitted.path) as db:
        drafts = ProjectDraftRepository(db)
        draft = drafts.get(admitted.project)
        drafts.save(draft.model_copy(update={"version": draft.version + 1, "script_revision": draft.script_revision + 1}))
        assert _blocker(db, admitted) is None
        assert _blocker(db, admitted, copy="An unrelated fresh claim.") == "talking_run_copy_mismatch"
        series = TalkingSliceSeriesRepository(db).get(UUID(admitted.binding["talking_series_id"]))
        # Preview/review preparation retains the current-plan requirement.
        with pytest.raises(ValueError, match="planned_talking_plan_changed"):
            ProductionRunService(db, admitted.path.parent).require_current_planned_run_origin(
                admitted.project, admitted.production_run_id, series.planned_origin.scene_plan_id,
                expected=series.planned_origin,
            )


def test_cleared_old_run_association_cannot_reenter_dispatch_or_revoke_reviewed_result(admitted, monkeypatch):
    monkeypatch.setattr(TalkingSourceAdmissionRepository, "get", _forbid_evaluation_lookup)
    def no_dispatch(*args, **kwargs):
        raise AssertionError("reviewed result consumption must not reenter dispatch/preflight")
    monkeypatch.setattr(ProductionRunService, "require_talking_job_admission", no_dispatch)
    with Database(admitted.path) as db:
        row = db.connection.execute("SELECT talking_source_bindings FROM production_runs WHERE id=?",
            (str(admitted.production_run_id),)).fetchone()
        bindings = json.loads(row["talking_source_bindings"])
        bindings[0]["talking_run_id"] = None
        db.connection.execute("UPDATE production_runs SET talking_source_bindings=? WHERE id=?",
            (json.dumps(bindings), str(admitted.production_run_id)))
        assert _blocker(db, admitted) is None
        source = AssetRepository(db).get(UUID(admitted.binding["reference_asset_id"]))
        (admitted.path.parent / source.source_file).write_bytes(b"changed after old association cleared")
        assert _blocker(db, admitted) == "talking_reference_bytes_changed"


@pytest.mark.parametrize("active", [None, False, True])
def test_legacy_explicit_admission_keeps_its_revocation_check(admitted, monkeypatch, active):
    admission_id, seen = uuid4(), []
    def lookup(self, value):
        seen.append(value)
        return None if active is None else SimpleNamespace(active=active)
    monkeypatch.setattr(TalkingSourceAdmissionRepository, "get", lookup)
    with Database(admitted.path) as db:
        repo = TalkingSliceSeriesRepository(db)
        series = repo.get(UUID(admitted.binding["talking_series_id"]))
        raw = series.model_dump(mode="json")
        raw["planned_origin"]["source_admission_id"] = str(admission_id)
        # Legacy fixtures retain their exact prior origin; no new receipt is created.
        db.connection.execute("UPDATE talking_slice_series SET payload=? WHERE id=?",
            (json.dumps(raw), str(series.id)))
        assert _blocker(db, admitted) == (None if active else "talking_source_permission_revoked_or_missing")
        assert seen == [admission_id]
