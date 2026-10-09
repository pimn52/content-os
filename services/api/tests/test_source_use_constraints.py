"""Temporary byte-identity fixtures, not runtime QA or creator approval.

The assisted label exercises the normal explicit intake contract only.
No media generator/provider is called and no real user database is opened.
"""
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.assembly.edit_plan import _default_edit_plan
from app.db import AssetRepository, ClipRepository, Database, IPProfileRepository, ProjectRepository
from app.domain.models import Asset, Clip, IPProfile, JobStatus, SourceKind, SubtitleTreatment, VideoScene, VideoSpec, VideoVisual, VisualStyleTokens
from app.main import create_app, _scene_planning_context
from app.production_preflight import PreflightInputError, build_production_preflight
from app.source_use_constraints import SourceUseConstraintService, RetainedRejectionRequest, UseConstraintStateRequest, presentation
from test_production_preflight import _create_draft


def retained(client, path, *, talking=False, admit=None):
    project_id, scene = _create_draft(client, talking=talking)
    source = path.parent / "source.media"
    source.write_bytes(b"synthetic horizontal source identity; not media QA")
    media = path.parent / "rejected.media"
    media.write_bytes(b"synthetic retained output identity; not media QA")
    with Database(path) as db:
        project = ProjectRepository(db).get(UUID(project_id))
        asset = AssetRepository(db).create(Asset(source_kind=SourceKind.AI_VIDEO if admit else SourceKind.USER_ASSET,
            source_file=source.name, content_hash=hashlib.sha256(source.read_bytes()).hexdigest(),
            duration_ms=5000, width=1920, height=1080, fps=project.fps, has_audio=True,
            authorization_reference="fixture:retained-rights", imported_at=datetime.now(timezone.utc),
            metadata={"burned_in_subtitles": "present", "subtitle_evidence_reference": "fixture:source-text"}))
        clip = ClipRepository(db).create(Clip(asset_id=asset.id, start_ms=0, end_ms=5000,
            asset_duration_ms=5000, transcript=scene.voice_text,
            visual_description="creator showing a workflow", talking_candidate=talking))
        if admit:
            asset, _, _ = admit(db, UUID(project_id), asset, clip, scene.voice_text, transcript_end_ms=3000)
    plan = client.post(f"/projects/{project_id}/production-preflight", json={}).json()
    candidate = plan["scenes"][0]["selected_candidate"]
    assert candidate["clip_id"] == str(clip.id), plan
    from app.domain.models import CandidateAsset
    candidate = CandidateAsset.model_validate(candidate)
    entry = _default_edit_plan(project, [scene], {scene.id: candidate}).scenes[0].model_copy(update={
        "subtitle_treatment": SubtitleTreatment.NONE, "burned_in_subtitles": "present"})
    # Validate copy updates into enum fields; historical spec itself is typed.
    from app.domain.models import EditPlanScene, EditPlan
    entry = EditPlanScene.model_validate(entry.model_dump(mode="json"))
    edit = EditPlan(project_id=project.id, scenes=[entry])
    spec = VideoSpec(project_id=project.id, format=project.format, width=1080, height=1920,
        fps=project.fps, edit_plan=edit, scenes=[VideoScene(scene_id=scene.scene_id,
            start_frame=0, duration_frames=90, subtitle_treatment=entry.subtitle_treatment,
            visual_role=entry.visual_role, graphic_treatment=entry.graphic_treatment,
            visual=VideoVisual(source_kind=asset.source_kind, asset_id=asset.id, clip_id=clip.id,
                clip_start_ms=0, clip_end_ms=5000, authorization_reference=asset.authorization_reference))])
    raw = {"project_id": project_id, "media": media.name,
        "sha256": hashlib.sha256(media.read_bytes()).hexdigest(), "video_spec": spec.model_dump(mode="json"),
        "edit_plan": edit.model_dump(mode="json"), "human_review": {
            "approved": False, "reviewed_sha256": hashlib.sha256(media.read_bytes()).hexdigest(),
            "evidence_reference": "fixture:retained-whole-render-rejection",
            "reviewed_at": datetime.now(timezone.utc).isoformat(), "scope": "exact whole render only",
            "findings": ["Horizontal composition and original source text conflict; no per-scene judgment."]}}
    manifest = path.parent / "retained.json"
    manifest.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    request = {"manifest_path": manifest.name, "expected_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "evidence_class": "assisted_test", "confirmed_source": True,
        "reason": "explicit synthetic retained-source confirmation", "idempotency_key": "intake"}
    return project_id, scene, asset, clip, spec, raw, request, plan


def intake(client, project, request):
    response = client.post(f"/projects/{project}/source-use-constraints", json=request)
    assert response.status_code == 201, response.text
    return response.json()


def state(client, project, record, *, enabled=True, key="adopt", use_ids=None):
    return client.put(f"/projects/{project}/source-use-constraints/{record['id']}", json={
        "expected_version": record["version"], "enabled": enabled,
        "use_ids": ([record["uses"][0]["use_id"]] if use_ids is None else use_ids) if enabled else [],
        "reason": "explicit future-use policy, not a new scene review", "idempotency_key": key})


def preview(client, project):
    response = client.post(f"/projects/{project}/production-preflight", json={})
    assert response.status_code == 200, response.text
    return response.json()


def test_normal_intake_adopt_choice_disable_history_restart(tmp_path):
    path = tmp_path / "rules.sqlite"
    with TestClient(create_app(path)) as client:
        project, scene, asset, clip, spec, raw, request, before = retained(client, path)
        record = intake(client, project, request)
        assert record["state"] == "candidate" and record["adopted_use_ids"] == []
        assert record["original_review"] == raw["human_review"]
        assert json.loads(record["original_manifest"]) == raw
        assert record["uses"][0]["scope_authority"] == "proposed_future_use_not_independent_scene_rejection"
        assert intake(client, project, request) == record
        assert preview(client, project)["scenes"][0]["selected_candidate"]["clip_id"] == str(clip.id)
        adopted = state(client, project, record).json()
        assert adopted["state"] == "enabled", adopted
        after = preview(client, project)
        decision = after["scenes"][0]
        assert decision["selected_candidate"]["source_kind"] == SourceKind.TYPOGRAPHY.value
        assert decision["excluded_uses"][0]["state"] == "excluded"
        assert decision["excluded_uses"][0]["candidate"]["clip_id"] == str(clip.id)
        assert after["evidence_level"] == "metadata_and_adopted_use_constraint"
        assert after["fingerprint"] != before["fingerprint"]
        assert f"source_suitability_unknown:{scene.scene_id}" not in after["stop_reasons"]
        # Other hard gates remain in effect: changing choice is not dispatch/QA.
        assert after["status"] == "blocked" and not after["dispatch_performed"]
        assert "voice_capability_not_verified" in after["stop_reasons"]
        assert client.get(f"/projects/{project}/provider-calls").json() == []
        with Database(path) as db:
            for table in ("jobs", "production_runs", "presentation_observations", "planning_preferences"):
                assert db.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
            context, refs = _scene_planning_context(db, ProjectRepository(db).get(UUID(project)))
            assert context["source_use_constraints"][0]["id"] == record["id"]
            assert any(f"source_use_constraint:{record['id']}:v2" in ref for ref in refs)
        assert state(client, project, record).json() == adopted  # exact replay
        assert state(client, project, record, key="stale").status_code == 409
        disabled = state(client, project, adopted, enabled=False, key="disable").json()
        assert disabled["version"] == 3
        assert preview(client, project)["scenes"][0]["selected_candidate"]["clip_id"] == str(clip.id)
        assert state(client, project, record).json() == adopted  # replay cannot revive
        assert client.get(f"/projects/{project}/source-use-constraints").json()[0]["state"] == "disabled"
        assert len(client.get(f"/projects/{project}/source-use-constraints/{record['id']}/versions").json()) == 3
    with TestClient(create_app(path)) as client:
        assert preview(client, project)["scenes"][0]["selected_candidate"]["clip_id"] == str(clip.id)
        assert state(client, project, disabled, key="reenable").status_code == 200


def test_required_talking_not_silently_replaced_by_typography(tmp_path):
    path = tmp_path / "talking.sqlite"
    with TestClient(create_app(path)) as client:
        project, scene, _, clip, _, _, request, _ = retained(client, path, talking=True)
        record = intake(client, project, request)
        assert state(client, project, record).status_code == 200
        result = preview(client, project)
        assert result["scenes"][0]["selected_candidate"] is None
        assert result["scenes"][0]["production_need"] == "capture"
        assert f"source_use_replan_required:{scene.scene_id}" in result["stop_reasons"]
        assert client.get(f"/projects/{project}/draft").json()["scenes"][0]["preferred_sources"] == [SourceKind.USER_ASSET.value, SourceKind.AI_VIDEO.value]


def test_bound_admitted_same_use_cannot_bypass_policy(tmp_path, admit_talking_run):
    from app.db import ProjectDraftRepository
    path = tmp_path / "pin.sqlite"
    with TestClient(create_app(path)) as client:
        project, scene, _, clip, _, _, request, before = retained(client, path, talking=True, admit=admit_talking_run)
        assert before["scenes"][0]["selected_candidate"]["source_kind"] == SourceKind.AI_VIDEO.value
        record = intake(client, project, request)
        assert state(client, project, record).status_code == 200
        with Database(path) as db:
            with pytest.raises(PreflightInputError, match="bound_source_use_excluded"):
                build_production_preflight(db, ProjectRepository(db).get(UUID(project)),
                    ProjectDraftRepository(db).get(UUID(project)), style_tokens=VisualStyleTokens(),
                    bound_talking_clips={scene.id: clip.id})


def test_no_explicit_graphic_alternative_returns_unmet_original_requirement(tmp_path):
    path = tmp_path / "unmet.sqlite"
    with TestClient(create_app(path)) as client:
        project, scene, _, _, _, _, request, _ = retained(client, path)
        saved = client.put(f"/projects/{project}/draft", json={"script": scene.voice_text, "topic": "New topic",
            "scenes": [scene.model_copy(update={"fallback_sources": []}).model_dump(mode="json")]})
        assert saved.status_code == 200, saved.text
        record = intake(client, project, request)
        assert state(client, project, record).status_code == 200
        result = preview(client, project)
        assert result["scenes"][0]["selected_candidate"] is None
        assert f"source_use_replan_required:{scene.scene_id}" in result["stop_reasons"]


def test_stale_preflight_and_late_planning_results_cannot_commit(tmp_path):
    from app.providers.scene_planner import ScenePlanResult
    path = tmp_path / "late.sqlite"
    class LatePlanner:
        def plan(self, project, *, script=None, topic=None):
            with Database(path) as db:
                SourceUseConstraintService(db).state(project.id, UUID(adopted["id"]), UseConstraintStateRequest(
                    expected_version=adopted["version"], enabled=False,
                    reason="synthetic late policy change", idempotency_key="during-planning"))
            return ScenePlanResult(project.id, (scene,))
    with TestClient(create_app(path, scene_planner=LatePlanner())) as client:
        project, scene, _, _, _, _, request, before = retained(client, path)
        record = intake(client, project, request)
        adopted = state(client, project, record).json()
        assert client.post(f"/projects/{project}/production-runs", json={
            "expected_fingerprint": before["fingerprint"], "idempotency_key": "old-plan"}).status_code == 409
        old_draft = client.get(f"/projects/{project}/draft").json()
        response = client.post(f"/projects/{project}/scene-plan", json={"topic": "next"})
        assert response.status_code == 409, response.text
        assert client.get(f"/projects/{project}/draft").json() == old_draft


def test_additive_migration_41_preserves_existing_profiles_and_rows(tmp_path):
    path = tmp_path / "migration.sqlite"
    with Database(path) as db:
        profile = IPProfileRepository(db).create(IPProfile(creator_name="retained creator"))
        for table in ("render_use_constraint_snapshots", "source_use_constraint_requests", "source_use_constraint_versions", "source_use_constraints"):
            db.connection.execute(f"DROP TABLE {table}")
        db.connection.execute("DELETE FROM schema_migrations WHERE version=42")
        previous = db.connection.execute("SELECT payload FROM ip_profiles WHERE id=?", (str(profile.id),)).fetchone()[0]
    with Database(path) as db:
        assert db.connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 42
        assert db.connection.execute("SELECT payload FROM ip_profiles WHERE id=?", (str(profile.id),)).fetchone()[0] == previous
        assert IPProfileRepository(db).revisions(profile.id)[-1][0] == 1
        assert db.connection.execute("SELECT COUNT(*) FROM source_use_constraints").fetchone()[0] == 0


def test_readonly_retained_choices_require_confirmation_and_revalidate(tmp_path):
    path = tmp_path / "choices.sqlite"
    with TestClient(create_app(path)) as client:
        project, _, _, _, _, raw, request, _ = retained(client, path)
        directory = tmp_path / "evaluation-evidence" / "old-case"
        directory.mkdir(parents=True)
        manifest = directory / "manifest.json"
        manifest.write_bytes((tmp_path / "retained.json").read_bytes())
        assert client.get(f"/projects/{project}/source-use-constraints").json() == []
        choices = client.get(f"/projects/{project}/retained-render-rejections").json()
        assert choices[0]["manifest_path"] == "evaluation-evidence/old-case/manifest.json"
        assert choices[0]["expected_manifest_sha256"] == request["expected_manifest_sha256"]
        assert choices[0]["evidence_level"] == "unconfirmed_retained_document_not_policy_or_qa"
        assert client.get(f"/projects/{project}/source-use-constraints").json() == []
        request.update(manifest_path=choices[0]["manifest_path"], confirmed_source=False)
        assert client.post(f"/projects/{project}/source-use-constraints", json=request).status_code == 422
        request["confirmed_source"] = True
        manifest.write_bytes(b"changed after selection")
        assert client.post(f"/projects/{project}/source-use-constraints", json=request).status_code == 409
        raw["human_review"]["approved"] = True
        manifest.write_text(json.dumps(raw), encoding="utf-8")
        assert client.get(f"/projects/{project}/retained-render-rejections").json() == []


def test_portable_evidence_uses_configured_root_not_cwd(tmp_path, monkeypatch):
    path = tmp_path / "root.sqlite"
    with TestClient(create_app(path)) as client:
        project, _, _, _, _, _, request, _ = retained(client, path)
        elsewhere = tmp_path / "different-cwd"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        record = intake(client, project, request)
        assert state(client, project, record).status_code == 200
        assert preview(client, project)["scenes"][0]["excluded_uses"][0]["state"] == "excluded"


@pytest.mark.parametrize("change", ["profile", "manifest", "media", "source", "owner"])
def test_changed_authority_and_bytes_hold_enabled_policy(tmp_path, change):
    path = tmp_path / "changed.sqlite"
    with TestClient(create_app(path)) as client:
        project, _, _, _, _, _, request, _ = retained(client, path)
        record = intake(client, project, request)
        adopted = state(client, project, record).json()
        if change in {"manifest", "media", "source"}:
            filename = {"manifest": "retained.json", "media": "rejected.media", "source": "source.media"}[change]
            (tmp_path / filename).write_bytes(b"changed bytes")
        else:
            with Database(path) as db:
                owner = ProjectRepository(db).get(UUID(project))
                if change == "profile":
                    repo = IPProfileRepository(db)
                    profile = repo.get(owner.ip_profile_id)
                    repo.update(profile.model_copy(update={"creator_name": "changed profile"}))
                else:
                    profile = IPProfileRepository(db).create(IPProfile(creator_name="other"))
                    ProjectRepository(db).update(owner.model_copy(update={"ip_profile_id": profile.id}))
        records = client.get(f"/projects/{project}/source-use-constraints").json()
        if change == "owner":
            assert records == []
            assert state(client, project, record).status_code == 404
        else:
            assert records[0]["current_stop_reasons"]
            assert preview(client, project)["scenes"][0]["excluded_uses"][0]["state"] == "unresolved"
            assert state(client, project, adopted, key="new-adopt").status_code == 409
            assert state(client, project, adopted, enabled=False, key="disable").status_code == 200


@pytest.mark.parametrize("mutation", ["path", "hash", "positive", "wrong_project", "mixed_plan", "fixture", "fixture_document", "no_scope"])
def test_intake_and_adoption_negative_contracts(tmp_path, mutation):
    path = tmp_path / "negative.sqlite"
    with TestClient(create_app(path)) as client:
        project, _, _, _, _, raw, request, _ = retained(client, path)
        if mutation == "path":
            request["manifest_path"] = "../outside.json"
        elif mutation == "hash":
            request["expected_manifest_sha256"] = "0" * 64
        elif mutation in {"positive", "wrong_project", "mixed_plan"}:
            if mutation == "positive":
                raw["human_review"]["approved"] = True
            elif mutation == "wrong_project":
                raw["project_id"] = str(uuid4())
            else:
                raw["edit_plan"]["scenes"][0]["burned_in_subtitles"] = "unknown"
            (tmp_path / "retained.json").write_text(json.dumps(raw), encoding="utf-8")
            request["expected_manifest_sha256"] = hashlib.sha256((tmp_path / "retained.json").read_bytes()).hexdigest()
        elif mutation == "fixture":
            request["evidence_class"] = "fixture"
        elif mutation == "fixture_document":
            raw["evidence_class"] = "fixture"
            (tmp_path / "retained.json").write_text(json.dumps(raw), encoding="utf-8")
            request["expected_manifest_sha256"] = hashlib.sha256((tmp_path / "retained.json").read_bytes()).hexdigest()
        response = client.post(f"/projects/{project}/source-use-constraints", json=request)
        if mutation in {"fixture", "fixture_document", "no_scope"}:
            assert response.status_code == 201, response.text
            assert state(client, project, response.json(), use_ids=[] if mutation == "no_scope" else None).status_code == 409
        else:
            assert response.status_code == 409, response.text
            assert client.get(f"/projects/{project}/source-use-constraints").json() == []


def test_match_exact_bytes_interval_treatment_not_clip_ids_or_ancestors(tmp_path):
    path = tmp_path / "matching.sqlite"
    with TestClient(create_app(path)) as client:
        project, _, asset, clip, spec, _, request, before = retained(client, path)
        record = intake(client, project, request)
        assert state(client, project, record).status_code == 200
        from app.domain.models import CandidateAsset
        candidate = CandidateAsset.model_validate(before["scenes"][0]["selected_candidate"])
        entry = spec.edit_plan.scenes[0]
        treatment = presentation(entry, width=spec.width, height=spec.height, style=spec.scenes[0].style_tokens)
        # Check normal unknown handling before introducing extra candidates
        # into the Router's bounded retrieval pool.
        with Database(path) as db:
            AssetRepository(db).update(asset.model_copy(update={"metadata": {}}))
        assert preview(client, project)["scenes"][0]["excluded_uses"][0]["state"] == "unresolved"
        with Database(path) as db:
            AssetRepository(db).update(asset)
        with Database(path) as db:
            service = SourceUseConstraintService(db)
            records = service.records(UUID(project))
            duplicate = ClipRepository(db).create(clip.model_copy(update={"id": uuid4()}))
            other = candidate.model_copy(update={"clip_id": duplicate.id})
            assert service.evaluate(other, treatment, records)[0].state == "excluded"
            assert service.evaluate(other, None, records)[0].state == "unresolved"
            for changed in ({**treatment, "width": 720}, {**treatment, "portrait_presentation": "portrait_panel"},
                            {**treatment, "graphic_text": "different semantic treatment"}):
                assert not service.evaluate(other, changed, records)
            for start, end in ((0, 4000), (1000, 5000), (1000, 4000)):
                changed = ClipRepository(db).create(clip.model_copy(update={"id": uuid4(), "start_ms": start, "end_ms": end}))
                assert not service.evaluate(candidate.model_copy(update={"clip_id": changed.id}), treatment, records)
            descendant = AssetRepository(db).create(asset.model_copy(update={"id": uuid4(), "content_hash": "d" * 64,
                "metadata": {"parent_asset_id": str(asset.id)}}))
            child = ClipRepository(db).create(clip.model_copy(update={"id": uuid4(), "asset_id": descendant.id}))
            assert not service.evaluate(candidate.model_copy(update={"asset_id": descendant.id, "clip_id": child.id}), treatment, records)


def test_creator_scope_concurrency_and_idempotency_conflict(tmp_path):
    path = tmp_path / "concurrent.sqlite"
    with TestClient(create_app(path)) as client:
        project, _, _, _, _, _, request, _ = retained(client, path)
        with ThreadPoolExecutor(max_workers=2) as pool:
            records = list(pool.map(lambda _: intake(client, project, request), range(2)))
        assert records[0] == records[1]
        record = records[0]
        assert client.post(f"/projects/{project}/source-use-constraints", json={**request, "reason": "different"}).status_code == 409
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda key: state(client, project, record, key=key), ("one", "two")))
        assert sorted(r.status_code for r in results) == [200, 409]
        with Database(path) as db:
            original = ProjectRepository(db).get(UUID(project))
            second = ProjectRepository(db).create(original.model_copy(update={"id": uuid4()}))
            other_profile = IPProfileRepository(db).create(IPProfile(creator_name="unrelated"))
            other = ProjectRepository(db).create(original.model_copy(update={"id": uuid4(), "ip_profile_id": other_profile.id}))
        assert client.get(f"/projects/{second.id}/source-use-constraints").json()[0]["id"] == record["id"]
        assert client.get(f"/projects/{other.id}/source-use-constraints").json() == []
        assert state(client, str(other.id), record).status_code == 404


def test_render_snapshot_fences_adoption_disable_and_late_changes(tmp_path):
    from app.db import JobRepository
    from app.jobs.handlers import RenderVideoJobHandler
    from app.jobs.runner import JobExecutionError
    from test_production_runs import _ready_fixture
    path = tmp_path / "worker.sqlite"
    with TestClient(create_app(path)) as client:
        project, _, _ = _ready_fixture(client, path)
        plan = preview(client, project)
        response = client.post(f"/projects/{project}/production-runs", json={
            "expected_fingerprint": plan["fingerprint"], "idempotency_key": "render"})
        assert response.status_code == 201, response.text
        with Database(path) as db:
            job = JobRepository(db).get(UUID(response.json()["render_job_id"]))
            assert db.connection.execute("SELECT fingerprint FROM render_use_constraint_snapshots WHERE job_id=?", (str(job.id),)).fetchone()
            service = SourceUseConstraintService(db)
            service.require_render_snapshot(job)
            # Inject only a versioned policy-row fixture to isolate Worker guards.
            owner = ProjectRepository(db).get(UUID(project))
            value = {"id": str(uuid4()), "version": 1, "state": "disabled", "profile_version": 1,
                     "adopted_use_ids": [], "creator_id": str(owner.ip_profile_id)}
            def add_policy():
                db.connection.execute("INSERT INTO source_use_constraints VALUES (?,?,?,?)",
                    (value["id"], value["creator_id"], project, json.dumps(value)))
            class Renderer:
                data_root = tmp_path
                calls = 0
                def render(self, spec, output):
                    self.calls += 1
                    add_policy()
            renderer = Renderer()
            running = job.model_copy(update={"status": JobStatus.RUNNING})
            handler = RenderVideoJobHandler(ProjectRepository(db), renderer, tmp_path / "renders")
            with pytest.raises(JobExecutionError, match="policy or evidence changed"):
                handler(running)  # late mutation while rendering
            assert renderer.calls == 1
            with pytest.raises(JobExecutionError, match="policy or evidence changed"):
                handler(running)  # before rendering
            assert renderer.calls == 1
