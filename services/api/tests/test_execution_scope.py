"""Scope foundations only; no test claims a completed evaluation workflow."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from uuid import UUID, uuid4

import pytest

from app.db import Database, AssetRepository, AudioAssetRepository, ClipRepository, JobRepository, ProviderCallRepository, ProjectRepository
from app.domain.models import Job, JobStatus, JobType
from app.execution_admission import UseAdmissionError
from app.execution_scope import (ExecutionScopeService, ExecutionUseScope, WorkerExecutionUseSnapshot,
    commercial_content_blocker, job_use_identity, require_evaluation_lane_closed)
from app.jobs import JobRunner, JobStore
from test_execution_admission import setup as admission_setup, headers, OPERATOR_KEY


@pytest.fixture
def scoped(admission_setup):
    case = admission_setup
    client, path, project, profile, identity, report, payload = case
    # Synthetic terms explicitly include every operation under test.
    report = report.model_copy(update={"permitted_operations": ["generation", "derivation", "review", "download"]})
    evidence = path.parent / "scope-review.json"
    evidence.write_text(report.model_dump_json(), encoding="utf-8")
    response = client.post(f"/projects/{project}/provider-use-admissions", headers=headers(), json={**payload,
        "review_reference": evidence.name, "review_sha256": hashlib.sha256(evidence.read_bytes()).hexdigest()})
    assert response.status_code == 201, response.text
    receipt = UUID(response.json()["id"])
    with Database(path) as db:
        service = ExecutionScopeService(db, path.parent)
        scope = service.prepare(project, purpose="internal_evaluation", admission_ids=(receipt,))
    return case, receipt, scope


def _job(project):
    now = datetime.now(timezone.utc)
    return Job(project_id=project, type=JobType.GENERATE_VOICE, idempotency_key="scope-job", created_at=now, updated_at=now)


def test_prepare_is_readonly_default_is_not_a_commercial_grant(scoped):
    case, receipt, scope = scoped
    _, path, project, _, _, _, _ = case
    with Database(path) as db:
        service = ExecutionScopeService(db, path.parent)
        before = db.connection.total_changes
        assert service.prepare(project, purpose="internal_evaluation", admission_ids=(receipt,)) == scope
        assert service.prepare(project).purpose == "commercial_production"
        assert service.manifest("asset", uuid4()) == {"policy_version": 1, "lineage": "unknown",
            "commercial_authorized": False, "execution_authorized": False, "scopes": []}
        assert db.connection.total_changes == before
        assert ProviderCallRepository(db).list_for_project(project) == []


@pytest.mark.parametrize("purpose,ids,reason", [
    ("anything", (), "execution_purpose_unsupported"),
    ("internal_evaluation", (), "provider_use_admission_missing"),
    ("commercial_production", "receipt", "evaluation_scope_not_commercial"),
    ("internal_evaluation", "duplicate", "provider_use_admission_duplicate"),
    ("internal_evaluation", "missing", "provider_use_admission_missing"),
])
def test_prepare_rejects_unknown_missing_or_ambiguous_scope(scoped, purpose, ids, reason):
    case, receipt, _ = scoped
    _, path, project, _, _, _, _ = case
    values = (receipt,) if ids == "receipt" else (receipt, receipt) if ids == "duplicate" else (uuid4(),) if ids == "missing" else ids
    with Database(path) as db, pytest.raises(UseAdmissionError, match=reason):
        ExecutionScopeService(db, path.parent).prepare(project, purpose=purpose, admission_ids=values)


def test_immutable_sidecars_do_not_rewrite_job_payload_and_survive_restart(scoped):
    case, _, scope = scoped
    _, path, project, _, _, _, _ = case
    job = _job(project)
    with Database(path) as db:
        JobRepository(db).create(job)
        original = db.connection.execute("SELECT payload FROM jobs WHERE id=?", (str(job.id),)).fetchone()[0]
        service = ExecutionScopeService(db, path.parent)
        for _ in range(2):
            service.pin("job", job.id, scope, operation="generation", subject_sha256=job_use_identity(job))
        assert db.connection.execute("SELECT payload FROM jobs WHERE id=?", (str(job.id),)).fetchone()[0] == original
        assert db.connection.execute("SELECT COUNT(*) FROM execution_use_scopes").fetchone()[0] == 1
    with Database(path) as db:
        service = ExecutionScopeService(db, path.parent)
        assert service.get("job", job.id) == scope
        running = job.model_copy(update={"status": JobStatus.RUNNING, "attempt": 1})
        assert service.require_job_snapshot(running) == scope
        with pytest.raises(UseAdmissionError, match="execution_use_scope_immutable"):
            service.pin("job", job.id, service.prepare(project), operation="generation", subject_sha256=job_use_identity(job))
        with pytest.raises(UseAdmissionError, match="execution_use_job_changed"):
            service.require_job_snapshot(job.model_copy(update={"idempotency_key": "changed"}))


@pytest.mark.parametrize("change,reason", [
    ("revoked", "provider_use_admission_revoked"), ("terms", "provider_use_evidence_not_current"),
    ("report", "provider_use_evidence_not_current"), ("project", "provider_use_project_mismatch"),
    ("commercial", "evaluation_scope_not_commercial"), ("operation", "provider_use_operation_unsupported"),
])
def test_completed_content_rechecks_current_authority(scoped, change, reason):
    case, receipt, scope = scoped
    client, path, project, _, _, _, _ = case
    with Database(path) as db:
        ExecutionScopeService(db, path.parent).pin("audio", uuid4(), scope, operation="generation", content_hash="c" * 64)
    if change == "revoked":
        assert client.post(f"/projects/{project}/provider-use-admissions/{receipt}/revoke", json={"reason": "synthetic revoke"}, headers=headers()).status_code == 200
    if change in ("terms", "report"):
        (path.parent / ("terms.txt" if change == "terms" else "scope-review.json")).write_text("changed", encoding="utf-8")
    with Database(path) as db:
        service = ExecutionScopeService(db, path.parent)
        with pytest.raises(UseAdmissionError, match=reason):
            service.require_content("c" * 64, project_id=uuid4() if change == "project" else project,
                purpose="commercial_production" if change == "commercial" else "internal_evaluation",
                operation="publish" if change == "operation" else "derivation")
        # Retained facts are still inspectable, never a license/publication grant.
        manifest = service.manifest("audio", uuid4(), content_hash="c" * 64)
        assert manifest["scopes"][0]["purpose"] == "internal_evaluation"
        assert manifest["commercial_authorized"] is False


def test_known_hash_cannot_be_laundered_by_copy_or_mixed_ancestors(scoped):
    case, _, scope = scoped
    _, path, project, _, _, _, _ = case
    first, second, final = uuid4(), uuid4(), uuid4()
    with Database(path) as db:
        service = ExecutionScopeService(db, path.parent)
        service.pin("audio", first, scope, operation="generation", content_hash="b" * 64)
        commercial = service.prepare(project)
        with pytest.raises(UseAdmissionError, match="evaluation_scope_not_commercial"):
            service.pin("asset", second, commercial, operation="derivation", content_hash="b" * 64)
        service.pin("asset", second, commercial, operation="derivation", content_hash="d" * 64)
        with pytest.raises(UseAdmissionError, match="evaluation_scope_not_commercial"):
            service.inherit("render", final, project_id=project, purpose="commercial_production",
                parents=(("audio", first), ("asset", second)), operation="derivation", content_hash="e" * 64)
        merged = service.inherit("render", final, project_id=project, purpose="internal_evaluation",
            parents=(("audio", first), ("asset", second)), operation="derivation", content_hash="e" * 64)
        assert merged == scope
        assert commercial_content_blocker(db, "e" * 64) == "evaluation_scope_not_commercial"
        with pytest.raises(UseAdmissionError, match="execution_use_content_hash_immutable"):
            service.pin("render", final, scope, operation="derivation", content_hash="f" * 64)
        assert service.scopes_for_hash("f" * 64) == ()
        with pytest.raises(UseAdmissionError, match="execution_use_lineage_unknown"):
            service.inherit("asset", uuid4(), project_id=project, purpose="internal_evaluation",
                parents=(("audio", uuid4()),), operation="derivation")


def test_current_scope_is_not_silently_changed_or_stripped(scoped):
    case, _, scope = scoped
    _, path, _, _, _, _, _ = case
    subject = uuid4()
    with Database(path) as db:
        service = ExecutionScopeService(db, path.parent)
        service.pin("asset", subject, scope, operation="derivation", content_hash="f" * 64)
        db.connection.execute("UPDATE execution_use_scopes SET payload='{}' WHERE subject_id=?", (str(subject),))
        assert commercial_content_blocker(db, "f" * 64) == "execution_use_scope_invalid"
        with pytest.raises(UseAdmissionError, match="execution_use_scope_invalid"):
            service.get("asset", subject)


def test_job_runner_stops_scoped_work_before_handler_even_with_legacy_flag(scoped, monkeypatch):
    case, _, scope = scoped
    _, path, project, _, _, _, _ = case
    monkeypatch.setenv("CONTENT_OS_ALLOW_EVALUATION_TALKING", "1")
    with Database(path) as db:
        job = JobStore(db).enqueue(_job(project))
        service = ExecutionScopeService(db, path.parent)
        service.pin("job", job.id, scope, operation="generation", subject_sha256=job_use_identity(job))
    with Database(path) as db:
        seen = []
        runner = JobRunner(JobStore(db), {JobType.GENERATE_VOICE: seen.append}, worker_id="scope-test",
            lease_duration=timedelta(minutes=1), max_attempts=2)
        stopped = runner.run_once()
        assert stopped.status is JobStatus.FAILED and stopped.error_code == "evaluation_execution_not_integrated"
        assert seen == [] and stopped.attempt == 1
        assert runner.run_once() is None
        assert ProviderCallRepository(db).list_for_project(project) == []


@pytest.mark.parametrize("change", ["missing", "invalid", "weights", "runtime", "machine", "configuration", "provider", "job"])
def test_v1_worker_snapshot_never_supplies_v2_execution_authority(scoped, change):
    case, _, scope = scoped
    _, path, project, profile, identity, _, _ = case
    job = _job(project)
    class Adapter:
        provider_name = profile.provider
        model = profile.model
        def execution_use_snapshot(self):
            return WorkerExecutionUseSnapshot(identity=identity, configuration_sha256=scope.bindings[0].configuration_sha256)
    provider = Adapter()
    if change == "missing":
        provider.execution_use_snapshot = None
    elif change == "invalid":
        provider.execution_use_snapshot = lambda: {"expected_model": profile.model, "private": "do not expose"}
    elif change == "provider":
        provider.provider_name = "different"
    elif change in ("weights", "runtime", "machine", "configuration"):
        original = provider.execution_use_snapshot()
        new_identity = identity.model_copy(update={
            "model_artifact_sha256" if change == "weights" else "machine_id" if change == "machine" else "runtime": "b" * 64 if change == "weights" else "changed"})
        provider.execution_use_snapshot = lambda: original.model_copy(update={
            "configuration_sha256": "b" * 64} if change == "configuration" else {"identity": new_identity})
    with Database(path) as db:
        JobRepository(db).create(job)
        service = ExecutionScopeService(db, path.parent)
        service.pin("job", job.id, scope, operation="generation", subject_sha256=job_use_identity(job))
        if change == "job":
            job = job.model_copy(update={"payload": None, "idempotency_key": "stripped-and-rekeyed"})
        expected = "execution_use_job_changed" if change == "job" else "execution_attestation_upgrade_required"
        with pytest.raises(UseAdmissionError, match=expected):
            service.require_worker_identity(job, provider, capability_profile_id=profile.id)


@pytest.mark.parametrize("metadata_change", ["record_id", "quality", "license"])
def test_current_evidence_digest_cannot_be_an_independent_runtime_digest(scoped, metadata_change):
    """Retained V77b30 integration conflict, not permission to weaken evidence.

    The pre-migration snapshot target hashed DB evidence unavailable to a
    native adapter. v1 snapshots now require an upgrade rather than echoing
    that digest; v2 independent comparisons are covered in attestation tests.
    """
    from app.execution_admission import configuration_digest
    case, _, scope = scoped
    _, path, project, profile, identity, _, _ = case
    changes = {"record_id": {"id": uuid4()},
               "quality": {"quality_status": "observed"},
               "license": {"license_evidence_reference": "different/terms-attestation.json"}}
    changed_evidence = profile.model_copy(update=changes[metadata_change])
    # Runtime selection and verified parameters are identical, evidence is not.
    fields = ("capability", "mode", "provider", "model", "runtime", "machine_id", "verified_parameters")
    observed = {field: getattr(profile, field) for field in fields}
    assert observed == {field: getattr(changed_evidence, field) for field in fields}
    assert configuration_digest(profile) != configuration_digest(changed_evidence)
    # A digest of only independent runtime observations cannot satisfy today's
    # comparison, even with the exact same model artifact identity.
    runtime_digest = hashlib.sha256(json.dumps(observed, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    class Adapter:
        provider_name = profile.provider
        model = profile.model
        def execution_use_snapshot(self):
            return WorkerExecutionUseSnapshot(identity=identity, configuration_sha256=runtime_digest)
    with Database(path) as db:
        job = JobRepository(db).create(_job(project))
        service = ExecutionScopeService(db, path.parent)
        service.pin("job", job.id, scope, operation="generation", subject_sha256=job_use_identity(job))
        with pytest.raises(UseAdmissionError, match="execution_attestation_upgrade_required"):
            service.require_worker_identity(job, Adapter(), capability_profile_id=profile.id)
        assert ProviderCallRepository(db).list_for_project(project) == []


def test_legacy_snapshot_echo_requires_upgrade_and_cannot_enable_dispatch(scoped):
    case, _, scope = scoped
    _, path, project, profile, identity, _, _ = case
    class Adapter:
        provider_name = profile.provider
        model = profile.model
        def execution_use_snapshot(self):
            return WorkerExecutionUseSnapshot(identity=identity, configuration_sha256=scope.bindings[0].configuration_sha256)
    with Database(path) as db:
        job = JobRepository(db).create(_job(project))
        service = ExecutionScopeService(db, path.parent)
        service.pin("job", job.id, scope, operation="generation", subject_sha256=job_use_identity(job))
        with pytest.raises(UseAdmissionError, match="execution_attestation_upgrade_required"):
            service.require_worker_identity(job, Adapter(), capability_profile_id=profile.id)
        with pytest.raises(UseAdmissionError, match="evaluation_execution_not_integrated"):
            require_evaluation_lane_closed(db, job)


def test_normal_preflight_is_pure_purpose_bound_and_start_fails_closed(scoped):
    case, receipt, _ = scoped
    client, path, project, _, _, _, _ = case
    old = client.post(f"/projects/{project}/production-preflight", json={}).json()
    explicit = client.post(f"/projects/{project}/production-preflight", json={"purpose": "commercial_production"}).json()
    assert old == explicit
    request = {"purpose": "internal_evaluation", "use_admission_ids": [str(receipt)]}
    preview = client.post(f"/projects/{project}/production-preflight", json=request)
    assert preview.status_code == 200, preview.text
    plan = preview.json()
    assert plan["fingerprint"] != old["fingerprint"] and plan["status"] == "blocked"
    assert "evaluation_execution_not_integrated" in plan["stop_reasons"]
    denied = client.post(f"/projects/{project}/production-runs", json={**request,
        "idempotency_key": "scope-normal", "expected_fingerprint": plan["fingerprint"]})
    assert denied.status_code == 409 and denied.json()["detail"]["reasons"] == ["evaluation_execution_not_integrated"]
    assert client.post(f"/projects/{project}/production-preflight", json={"purpose": "unknown"}).status_code == 422
    assert client.post(f"/projects/{project}/production-runs", json={"purpose": "unknown",
        "idempotency_key": "unknown", "expected_fingerprint": old["fingerprint"]}).status_code == 422
    changed_purpose = client.post(f"/projects/{project}/production-runs", json={
        "purpose": "commercial_production", "use_admission_ids": [str(receipt)],
        "idempotency_key": "scope-normal", "expected_fingerprint": old["fingerprint"]})
    assert changed_purpose.status_code == 409 and changed_purpose.json()["detail"]["reasons"] == ["evaluation_scope_not_commercial"]
    with Database(path) as db:
        assert db.connection.execute("SELECT COUNT(*) FROM production_runs").fetchone()[0] == 0
        assert db.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        assert db.connection.execute("SELECT COUNT(*) FROM execution_use_scopes").fetchone()[0] == 0


def test_normal_reimport_new_id_and_stripped_metadata_stay_restricted(scoped):
    from app.media import MediaImporter
    from test_media_import import _StaticProbe
    case, _, scope = scoped
    _, path, _, _, _, _, _ = case
    source = path.parent / "restricted-synthetic.mp4"
    source.write_bytes(b"synthetic restricted bytes, not real media")
    content_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    with Database(path) as db:
        importer = MediaImporter(db, path.parent, _StaticProbe())
        original = importer.import_path(source, "synthetic rights")
        service = ExecutionScopeService(db, path.parent)
        service.pin("asset", original.id, scope, operation="generation", content_hash=content_hash)
        AssetRepository(db).update(original.model_copy(update={"metadata": {}}))
        assert commercial_content_blocker(db, original.content_hash) == "evaluation_scope_not_commercial"
        assert AssetRepository(db).delete(original.id)  # Temporary DB only; history sidecar retained.
        replacement = importer.import_path(source, "new asserted rights")
        assert replacement.id != original.id and replacement.metadata.get("execution_use_scope") is None
        assert replacement.content_hash == original.content_hash
        assert commercial_content_blocker(db, replacement.content_hash) == "evaluation_scope_not_commercial"


def test_known_restricted_media_stops_router_assembly_and_renderer(scoped):
    from app.assembly import VideoSpecAssembler, VideoSpecAssemblyError
    from app.domain.models import Project, SourceKind, VideoSpec, VideoScene, VideoVisual
    from app.renderer import RemotionRenderer, UnauthorizedVisualError
    from app.routing.asset_router import AssetRouter
    from app.search import ClipSearchHit
    from test_video_spec_assembly import _asset, _clip, _scene, _candidate
    from test_remotion_renderer import _renderer_project, _Runner
    case, _, scope = scoped
    client, path, project_id, _, _, _, _ = case
    with Database(path) as db:
        project = ProjectRepository(db).get(project_id)
    asset = _asset(2)
    clip = _clip(asset)
    scene = _scene(project, 0)
    class Search:
        def search(self, query, top_k):
            return [ClipSearchHit(clip=clip, score=1.0)]
    with Database(path) as db:
        assets, clips = AssetRepository(db), ClipRepository(db)
        assets.create(asset)
        clips.create(clip)
        service = ExecutionScopeService(db, path.parent)
        service.pin("asset", asset.id, scope, operation="generation", content_hash=asset.content_hash)
        result = AssetRouter(Search(), assets).route(scene)
        assert not any(value.asset_id == asset.id for value in result.candidates)
        with pytest.raises(VideoSpecAssemblyError, match="evaluation_scope_not_commercial"):
            VideoSpecAssembler(assets, clips).assemble(project, [scene], {scene.id: _candidate(scene, asset, clip)})
        spec = VideoSpec(project_id=project_id, format=project.format, width=1080, height=1920, fps=project.fps,
            scenes=[VideoScene(scene_id=scene.scene_id, start_frame=0, duration_frames=30,
                visual=VideoVisual(source_kind=SourceKind.USER_ASSET, authorization_reference=asset.authorization_reference,
                    asset_id=asset.id, clip_id=clip.id, clip_start_ms=clip.start_ms, clip_end_ms=clip.end_ms,
                    source_duration_ms=asset.duration_ms))])
        runner = _Runner()
        renderer = RemotionRenderer(assets, clips, renderer_dir=_renderer_project(path.parent / "scope-renderer"), runner=runner)
        with pytest.raises(UnauthorizedVisualError, match="evaluation_scope_not_commercial"):
            renderer.render(spec, path.parent / "never-rendered.mp4")
        assert runner.calls == [] and not (path.parent / "never-rendered.mp4").exists()


def test_known_restricted_audio_and_image_stops_consumer_without_metadata(scoped):
    from app.assembly import VideoSpecAssembler, VideoSpecAssemblyError
    from app.db import ImageAssetRepository
    from app.domain.models import AudioAsset, ImageAsset, Project, SourceKind, CandidateAsset, CostCategory, UsageCost
    from app.renderer import RemotionRenderer, UnauthorizedVisualError
    from app.routing.asset_router import _static_fallbacks
    from test_video_spec_assembly import _scene
    case, _, scope = scoped
    client, path, project_id, _, _, _, _ = case
    with Database(path) as db:
        project = ProjectRepository(db).get(project_id)
    now = datetime.now(timezone.utc)
    with Database(path) as db:
        audio = AudioAsset(source_file="synthetic.wav", content_hash="9" * 64, duration_ms=3000,
            sample_rate=24000, channels=1, authorization_reference="synthetic rights", imported_at=now)
        image = ImageAsset(source_kind=SourceKind.SCREENSHOT, source_file="synthetic.png", content_hash="8" * 64,
            width=1080, height=1920, authorization_reference="synthetic rights", imported_at=now)
        AudioAssetRepository(db).create(audio)
        ImageAssetRepository(db).create(image)
        service = ExecutionScopeService(db, path.parent)
        service.pin("audio", audio.id, scope, operation="generation", content_hash=audio.content_hash)
        service.pin("image", image.id, scope, operation="derivation", content_hash=image.content_hash)
        scene = _scene(project, 0).model_copy(update={"preferred_sources": [SourceKind.TYPOGRAPHY]})
        candidate = CandidateAsset(scene_plan_id=scene.id, source_kind=SourceKind.TYPOGRAPHY,
            match_score=0, recommended=True, why=["synthetic deterministic card"], estimated_cost=UsageCost(category=CostCategory.RENDER, amount=0, currency="USD"))
        assembler = VideoSpecAssembler(AssetRepository(db), ClipRepository(db), ImageAssetRepository(db), AudioAssetRepository(db))
        with pytest.raises(VideoSpecAssemblyError, match="evaluation_scope_not_commercial"):
            assembler.assemble(project, [scene], {scene.id: candidate}, master_narration_asset_id=audio.id)
        assert _static_fallbacks(scene, {SourceKind.SCREENSHOT}, ImageAssetRepository(db), recommended=True) == []
        # Renderer refuses the same stripped asset even when called without assembly.
        from test_remotion_renderer import _renderer_project
        renderer = RemotionRenderer(AssetRepository(db), ClipRepository(db), renderer_dir=_renderer_project(path.parent / "audio-renderer"))
        with pytest.raises(UnauthorizedVisualError, match="evaluation_scope_not_commercial"):
            renderer._require_commercial_content(audio)


def test_mixed_admission_ancestry_keeps_every_receipt(scoped):
    case, receipt, scope = scoped
    client, path, project, _, _, report, payload = case
    changed = report.model_copy(update={"permitted_operations": ["generation", "derivation", "review", "download"], "license_version": "synthetic-v2"})
    evidence = path.parent / "second-scope-review.json"
    evidence.write_text(changed.model_dump_json(), encoding="utf-8")
    response = client.post(f"/projects/{project}/provider-use-admissions", headers=headers(), json={**payload,
        "idempotency_key": "second-use", "review_reference": evidence.name, "review_sha256": hashlib.sha256(evidence.read_bytes()).hexdigest()})
    assert response.status_code == 201
    other_receipt = UUID(response.json()["id"])
    first, second, final = uuid4(), uuid4(), uuid4()
    with Database(path) as db:
        service = ExecutionScopeService(db, path.parent)
        other = service.prepare(project, purpose="internal_evaluation", admission_ids=(other_receipt,))
        service.pin("audio", first, scope, operation="generation", content_hash="3" * 64)
        service.pin("asset", second, other, operation="generation", content_hash="4" * 64)
        merged = service.inherit("render", final, project_id=project, purpose="internal_evaluation",
            parents=(("audio", first), ("asset", second)), operation="derivation", content_hash="5" * 64)
        assert {value.admission_id for value in merged.bindings} == {receipt, other_receipt}
        with pytest.raises(UseAdmissionError, match="execution_use_ancestor_dropped"):
            service.pin("render", uuid4(), scope, operation="derivation", content_hash="5" * 64)
    assert client.post(f"/projects/{project}/provider-use-admissions/{other_receipt}/revoke", json={"reason": "synthetic revoke"}, headers=headers()).status_code == 200
    with Database(path) as db, pytest.raises(UseAdmissionError, match="provider_use_admission_revoked"):
        ExecutionScopeService(db, path.parent).require_content("5" * 64, project_id=project, purpose="internal_evaluation")


@pytest.mark.parametrize("kind,marker,job_type", [
    ("audio", "voice_generation", JobType.GENERATE_VOICE),
    ("asset", "talking_generation", JobType.GENERATE_TALKING),
    ("asset", "talking_run_preview", JobType.PREPARE_TALKING_RUN_PREVIEW),
])
def test_normal_provenance_persistence_automatically_inherits_scope(scoped, kind, marker, job_type, monkeypatch):
    from app.domain.models import AudioAsset, TalkingRunPreviewJobPayload
    from test_video_spec_assembly import _asset
    case, _, scope = scoped
    _, path, project, _, _, _, _ = case
    elsewhere = path.parent / "unrelated-cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    with Database(path) as db:
        db.data_root = path.parent
        now = datetime.now(timezone.utc)
        payload = TalkingRunPreviewJobPayload(project_id=project, series_id=uuid4(), origin_sha256="a" * 64) if job_type is JobType.PREPARE_TALKING_RUN_PREVIEW else None
        job = JobRepository(db).create(Job(project_id=project, type=job_type, payload=payload,
            idempotency_key="automatic-output-scope", created_at=now, updated_at=now))
        service = ExecutionScopeService(db, path.parent)
        service.pin("job", job.id, scope, operation="generation", subject_sha256=job_use_identity(job))
        metadata = {marker: {"job_id": str(job.id), "project_id": str(project), "qa_state": "pending"}}
        if kind == "audio":
            audio = AudioAsset(source_file="synthetic.wav", content_hash="6" * 64, duration_ms=1000,
                sample_rate=24000, channels=1, authorization_reference="synthetic", imported_at=now)
            repo = AudioAssetRepository(db)
            repo.create(audio)  # Ordinary import has no generated provenance yet.
            assert service.get("audio", audio.id) is None
            value = repo.update(audio.model_copy(update={"metadata": metadata}))
        else:
            value = _asset(6).model_copy(update={"metadata": metadata})
            repo = AssetRepository(db)
            repo.create(value)
        assert service.get(kind, value.id) == scope
        assert commercial_content_blocker(db, value.content_hash) == "evaluation_scope_not_commercial"
        assert repo.get(value.id).metadata[marker]["qa_state"] == "pending"
        repo.update(value.model_copy(update={"metadata": {}}))
        assert service.get(kind, value.id) == scope
        assert commercial_content_blocker(db, value.content_hash) == "evaluation_scope_not_commercial"
    with Database(path) as db:
        assert ExecutionScopeService(db, path.parent).get(kind, value.id) == scope


@pytest.mark.parametrize("problem,reason", [
    ("root", "execution_scope_data_root_required"),
    ("revoked", "provider_use_admission_revoked"),
    ("project", "execution_use_origin_project_mismatch"),
    ("kind", "execution_use_origin_kind_mismatch"),
])
def test_output_provenance_and_scope_are_atomic_on_rejection(scoped, problem, reason):
    from app.domain.models import AudioAsset
    case, receipt, scope = scoped
    client, path, project, _, _, _, _ = case
    with Database(path) as db:
        job = JobRepository(db).create(_job(project))
        service = ExecutionScopeService(db, path.parent)
        service.pin("job", job.id, scope, operation="generation", subject_sha256=job_use_identity(job))
        audio = AudioAsset(source_file="synthetic.wav", content_hash="7" * 64, duration_ms=1000,
            sample_rate=24000, channels=1, authorization_reference="synthetic", imported_at=job.created_at)
        AudioAssetRepository(db).create(audio)
    if problem == "revoked":
        assert client.post(f"/projects/{project}/provider-use-admissions/{receipt}/revoke", json={"reason": "synthetic revoked-before-persistence"}, headers=headers()).status_code == 200
    marker = "talking_generation" if problem == "kind" else "voice_generation"
    metadata = {marker: {"job_id": str(job.id), "project_id": str(uuid4()) if problem == "project" else str(project)}}
    with Database(path) as db:
        if problem != "root":
            db.data_root = path.parent
        repo = AudioAssetRepository(db)
        with pytest.raises(UseAdmissionError, match=reason):
            repo.update(audio.model_copy(update={"metadata": metadata}))
        assert repo.get(audio.id).metadata == {}
        assert ExecutionScopeService(db, path.parent).get("audio", audio.id) is None
        assert db.connection.execute("SELECT COUNT(*) FROM execution_use_content_origins").fetchone()[0] == 0


def test_missing_job_scope_cannot_fall_back_to_legacy_run_or_repair(scoped):
    case, _, scope = scoped
    client, path, project, _, _, _, _ = case
    plan = client.post(f"/projects/{project}/production-preflight", json={}).json()
    run = client.post(f"/projects/{project}/production-runs", json={"idempotency_key": "scoped-owner-test",
        "expected_fingerprint": plan["fingerprint"]}).json()
    with Database(path) as db:
        job = JobRepository(db).create(_job(project))
        db.connection.execute("UPDATE production_runs SET voice_job_id=? WHERE id=?", (str(job.id), run["id"]))
        service = ExecutionScopeService(db, path.parent)
        # Application-only synthetic binding; normal internal creation is STILL closed.
        service.pin("run", UUID(run["id"]), scope, operation="generation")
        with pytest.raises(UseAdmissionError, match="execution_use_job_scope_missing"):
            require_evaluation_lane_closed(db, job)
        # Same purpose is immutable even when a forged empty-commercial Job scope is supplied.
        with pytest.raises(UseAdmissionError, match="execution_use_ancestor_dropped"):
            service.pin("job", job.id, service.prepare(project), operation="generation", subject_sha256=job_use_identity(job))
        assert service.get("job", job.id) is None


def test_reservation_transaction_rechecks_scope_before_ledger_write(scoped, monkeypatch):
    from app.budget import ProviderCallLedger
    from app.domain.models import CostCategory
    from app.jobs.handlers import _reserve_job_provider_call
    from app.jobs.runner import JobExecutionError
    case, receipt, scope = scoped
    client, path, project, _, _, _, _ = case
    with Database(path) as db:
        db.data_root = path.parent
        job = JobRepository(db).create(_job(project))
        service = ExecutionScopeService(db, path.parent)
        service.pin("job", job.id, scope, operation="generation", subject_sha256=job_use_identity(job))
        ledger = ProviderCallLedger(db)
        reserve = ledger.reserve_execution
        checked = []
        def instrument(**kwargs):
            original = kwargs["reservation_guard"]
            def inside_transaction():
                checked.append(db.connection.in_transaction)
                original()
            kwargs["reservation_guard"] = inside_transaction
            return reserve(**kwargs)
        monkeypatch.setattr(ledger, "reserve_execution", instrument)
        assert client.post(f"/projects/{project}/provider-use-admissions/{receipt}/revoke", json={"reason": "synthetic authority changed before reservation"}, headers=headers()).status_code == 200
        with pytest.raises(JobExecutionError) as error:
            _reserve_job_provider_call(ledger, job, operation="tts", category=CostCategory.VOICE,
                provider="fixture-voice", model="fixture-model", input_source="scope-test", known_local_cost=True)
        assert error.value.code == "provider_use_admission_revoked" and error.value.retryable is False
        assert checked == [True]
        assert ProviderCallRepository(db).list_for_project(project) == []


def test_unscoped_new_voice_job_cannot_use_restricted_reference_bytes(scoped):
    from app.domain.models import VoiceGenerationJobPayload
    from app.db import VoiceProfileRepository
    from app.jobs.handlers import _reserve_job_provider_call
    from app.jobs.runner import JobExecutionError
    from app.budget import ProviderCallLedger
    from app.domain.models import CostCategory
    case, _, scope = scoped
    _, path, project, profile, _, _, _ = case
    with Database(path) as db:
        voice = VoiceProfileRepository(db).list()[0]
        clip = ClipRepository(db).get(voice.reference_clip_ids[0])
        asset = AssetRepository(db).get(clip.asset_id)
        service = ExecutionScopeService(db, path.parent)
        service.pin("asset", asset.id, scope, operation="generation", content_hash=asset.content_hash)
        now = datetime.now(timezone.utc)
        payload = VoiceGenerationJobPayload(project_id=project, voice_profile_id=voice.id,
            text="Synthetic new copy.", authorization_reference="synthetic existing consent")
        job = JobRepository(db).create(Job(project_id=project, type=JobType.GENERATE_VOICE,
            payload=payload, idempotency_key="unscoped-voice-consumer", created_at=now, updated_at=now))
        with pytest.raises(UseAdmissionError, match="evaluation_scope_not_commercial"):
            require_evaluation_lane_closed(db, job)
        with pytest.raises(JobExecutionError) as error:
            _reserve_job_provider_call(ProviderCallLedger(db), job, operation="tts", category=CostCategory.VOICE,
                provider=profile.provider, model=profile.model, input_source="voice-profile", known_local_cost=True)
        assert error.value.code == "evaluation_scope_not_commercial"
        assert ProviderCallRepository(db).list_for_project(project) == []


@pytest.mark.parametrize("kind", ["audio", "asset"])
@pytest.mark.parametrize("dedup", [False, True])
def test_generated_import_restricts_bytes_before_provenance_annotation(scoped, kind, dedup, monkeypatch):
    from app.media.audio_importer import AudioImporter
    from app.media.importer import MediaImporter
    from app.media.ffprobe import AudioProbeMetadata, ProbeMetadata
    from app.domain.models import SourceKind, RationalFps
    case, _, scope = scoped
    _, path, project, _, _, _, _ = case
    class Probe:
        def probe_audio(self, path):
            return AudioProbeMetadata(1000, 24000, 1, None, {})
        def probe(self, path):
            return ProbeMetadata(1000, 1080, 1920, RationalFps(numerator=25, denominator=1), True, {})
    source = path.parent / "synthetic-generated.wav"
    source.write_bytes(b"synthetic bytes, not real runtime or media quality evidence")
    with Database(path) as db:
        db.data_root = path.parent
        job = JobRepository(db).create(_job(project).model_copy(update={
            "type": JobType.GENERATE_VOICE if kind == "audio" else JobType.GENERATE_TALKING}))
        service = ExecutionScopeService(db, path.parent)
        service.pin("job", job.id, scope, operation="generation", subject_sha256=job_use_identity(job))
        importer = (AudioImporter if kind == "audio" else MediaImporter)(db, path.parent, probe=Probe())
        repo = (AudioAssetRepository if kind == "audio" else AssetRepository)(db)
        kwargs = {} if kind == "audio" else {"source_kind": SourceKind.AI_VIDEO}
        if dedup:
            original = importer.import_path(source, "synthetic rights", **kwargs)
            assert service.get(kind, original.id) is None
        else:
            place = importer._place
            def inspect_before_placement(temp, destination):
                import sqlite3
                assert db.connection.in_transaction
                digest = hashlib.sha256(source.read_bytes()).hexdigest()
                assert commercial_content_blocker(db, digest) == "evaluation_scope_not_commercial"
                # Another connection cannot observe a new media row without its scope.
                with sqlite3.connect(path) as observer:
                    table = "audio_assets" if kind == "audio" else "assets"
                    assert observer.execute(f"SELECT count(*) FROM {table} WHERE content_hash=?", (digest,)).fetchone()[0] == 0
                    assert observer.execute("SELECT count(*) FROM execution_use_content_origins WHERE content_hash=?", (digest,)).fetchone()[0] == 0
                return place(temp, destination)
            monkeypatch.setattr(importer, "_place", inspect_before_placement)
        value = importer.import_path(source, "synthetic rights", generated_job=job, **kwargs)
        assert service.get(kind, value.id) == scope
        assert commercial_content_blocker(db, value.content_hash) == "evaluation_scope_not_commercial"
        assert "voice_generation" not in value.metadata and "talking_generation" not in value.metadata
        assert repo.get(value.id).metadata == value.metadata  # No annotation needed for the restriction.
        if dedup:
            assert value.id == original.id
        with pytest.raises(UseAdmissionError, match="execution_use_job_changed"):
            importer.import_path(source, "synthetic rights", generated_job=job.model_copy(update={"idempotency_key": "forged"}), **kwargs)
    with Database(path) as db:
        assert commercial_content_blocker(db, value.content_hash) == "evaluation_scope_not_commercial"


@pytest.mark.parametrize("kind", ["audio", "asset"])
@pytest.mark.parametrize("failure", ["revoked", "placement"])
def test_generated_import_failure_rolls_back_row_hash_origin_and_file(scoped, kind, failure, monkeypatch):
    from app.media.audio_importer import AudioImporter
    from app.media.importer import MediaImporter
    from app.media.ffprobe import AudioProbeMetadata, ProbeMetadata
    from app.domain.models import RationalFps
    case, receipt, scope = scoped
    client, path, project, _, _, _, _ = case
    class Probe:
        def probe_audio(self, path):
            return AudioProbeMetadata(1000, 24000, 1, None, {})
        def probe(self, path):
            return ProbeMetadata(1000, 1080, 1920, RationalFps(numerator=25, denominator=1), True, {})
    source = path.parent / "revoked-generated.wav"
    source.write_bytes(b"synthetic revoked output")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    with Database(path) as db:
        db.data_root = path.parent
        job = JobRepository(db).create(_job(project).model_copy(update={
            "type": JobType.GENERATE_VOICE if kind == "audio" else JobType.GENERATE_TALKING}))
        service = ExecutionScopeService(db, path.parent)
        service.pin("job", job.id, scope, operation="generation", subject_sha256=job_use_identity(job))
        importer = (AudioImporter if kind == "audio" else MediaImporter)(db, path.parent, probe=Probe())
        if failure == "revoked":
            assert client.post(f"/projects/{project}/provider-use-admissions/{receipt}/revoke", json={"reason": "synthetic revoke"}, headers=headers()).status_code == 200
            error, reason = UseAdmissionError, "provider_use_admission_revoked"
        else:
            def fail_placement(temp, destination):
                assert commercial_content_blocker(db, digest) == "evaluation_scope_not_commercial"
                raise OSError("synthetic placement failure")
            monkeypatch.setattr(importer, "_place", fail_placement)
            error, reason = OSError, "synthetic placement failure"
        with pytest.raises(error, match=reason):
            importer.import_path(source, "synthetic rights", generated_job=job)
        repo = (AudioAssetRepository if kind == "audio" else AssetRepository)(db)
        assert repo.get_by_content_hash(digest) is None
        assert service.scopes_for_hash(digest) == ()
        assert list(importer.originals.iterdir()) == []
        assert not db.connection.in_transaction


def test_revoked_profile_domain_failure_is_a_named_private_safe_stop(scoped):
    from app.domain.models import VoiceGenerationJobPayload, CostCategory
    from app.db import VoiceProfileRepository
    from app.budget import ProviderCallLedger
    from app.jobs.handlers import _reserve_job_provider_call
    from app.jobs.runner import JobExecutionError
    case, _, _ = scoped
    _, path, project, profile, _, _, _ = case
    with Database(path) as db:
        voice = VoiceProfileRepository(db).list()[0]
        now = datetime.now(timezone.utc)
        job = JobRepository(db).create(Job(project_id=project, type=JobType.GENERATE_VOICE,
            idempotency_key="revoked-domain-profile", created_at=now, updated_at=now,
            payload=VoiceGenerationJobPayload(project_id=project, voice_profile_id=voice.id,
                text="Synthetic.", authorization_reference="prior synthetic consent")))
        raw = voice.model_dump(mode="json")
        raw["consent"]["confirmed"] = False
        raw["consent"]["subject_name"] = "PRIVATE_SUBJECT_DO_NOT_EXPOSE"
        db.connection.execute("UPDATE voice_profiles SET payload=? WHERE id=?", (json.dumps(raw), str(voice.id)))
        with pytest.raises(JobExecutionError) as error:
            _reserve_job_provider_call(ProviderCallLedger(db), job, operation="tts", category=CostCategory.VOICE,
                provider=profile.provider, model=profile.model, input_source="synthetic", known_local_cost=True)
        assert error.value.code == "execution_use_dependency_invalid" and error.value.retryable is False
        assert str(error.value) == "execution_use_dependency_invalid"
        assert ProviderCallRepository(db).list_for_project(project) == []
