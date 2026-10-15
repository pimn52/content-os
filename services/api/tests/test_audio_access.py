"""Synthetic normal API consumption; no provider/runtime or quality proof."""
from datetime import datetime, timezone
import hashlib
from uuid import uuid4

import pytest

from app.db import AudioAssetRepository, AssetRepository, ClipRepository, Database, ImageAssetRepository, JobRepository, ProjectRepository
from app.domain.models import AudioAsset, Asset, Clip, ImageAsset, RationalFps, Job, JobType
from app.execution_scope import ExecutionScopeService, ExecutionUseScope, job_use_identity
from test_execution_scope import scoped, admission_setup
from test_execution_admission import headers


@pytest.fixture
def audio_case(scoped):
    case, receipt, scope = scoped
    client, path, project, *_ = case
    source = path.parent / "review-only.wav"
    source.write_bytes(b"synthetic media bytes; never a provider output")
    audio = AudioAsset(source_file=source.name, content_hash=hashlib.sha256(source.read_bytes()).hexdigest(),
        duration_ms=1000, sample_rate=24000, channels=1, authorization_reference="synthetic-only",
        imported_at=datetime.now(timezone.utc))
    with Database(path) as db:
        AudioAssetRepository(db).create(audio)
        ExecutionScopeService(db, path.parent).pin("audio", audio.id, scope, operation="generation", content_hash=audio.content_hash)
    return client, path, project, receipt, scope, audio, source


def context(project):
    return {"project_id": str(project), "purpose": "internal_evaluation"}


@pytest.mark.parametrize("revoked", [False, True])
def test_reproduced_generic_bypass_closed_and_historical_metadata_readable(audio_case, revoked):
    client, path, project, receipt, scope, audio, source = audio_case
    if revoked:
        assert client.post(f"/projects/{project}/provider-use-admissions/{receipt}/revoke",
            json={"reason": "synthetic revoke"}, headers=headers()).status_code == 200
    assert client.get(f"/audio-assets/{audio.id}/media").status_code == 403
    assert client.get(f"/audio-assets/{audio.id}").status_code == 200
    status = client.get(f"/audio-assets/{audio.id}/access", params=context(project)).json()
    assert status["manifest"]["scopes"][0]["purpose"] == "internal_evaluation"
    assert status["download_authorized"] is status["quality_authorized"] is status["dispatch_authorized"] is False
    response = client.get(f"/audio-assets/{audio.id}/media", params=context(project))
    assert response.status_code == (403 if revoked else 200)
    if not revoked:
        assert response.content == source.read_bytes()
        assert response.headers["cache-control"] == "no-store"
        partial = client.get(f"/audio-assets/{audio.id}/media", params=context(project), headers={"Range": "bytes=0-8"})
        assert partial.status_code == 206 and partial.content == source.read_bytes()[:9]
    else:
        assert status["state"] == "blocked" and "revoked" in status["reasons"][0]
        for extra in ({"Range": "bytes=0-8"}, {"If-None-Match": '"cached"'}, {"If-Range": '"cached"', "Range": "bytes=0-8"}):
            assert client.get(f"/audio-assets/{audio.id}/media", params=context(project), headers=extra).status_code == 403


@pytest.mark.parametrize("mutation", ["project", "purpose", "unknown", "operation", "terms", "bytes", "path", "origin", "sidecar", "producer"])
def test_context_current_evidence_and_lineage_fail_closed(audio_case, mutation, tmp_path):
    client, path, project, receipt, scope, audio, source = audio_case
    params = context(project)
    with Database(path) as db:
        db.data_root = path.parent
        if mutation == "project": params["project_id"] = str(uuid4())
        if mutation == "purpose": params["purpose"] = "commercial_production"
        if mutation == "unknown": params["purpose"] = "download"
        if mutation == "operation": params["operation"] = "download"
        if mutation == "terms": (path.parent / "scope-review.json").write_text("{}")
        if mutation == "bytes": source.write_bytes(b"changed bytes")
        if mutation == "path":
            outside = tmp_path.parent / (str(uuid4()) + ".wav")
            outside.write_bytes(source.read_bytes())
            AudioAssetRepository(db).update(audio.model_copy(update={"source_file": str(outside)}))
        if mutation == "origin": db.connection.execute("DELETE FROM execution_use_content_origins WHERE subject_id=?", (str(audio.id),))
        if mutation == "sidecar": db.connection.execute("UPDATE execution_use_scopes SET fingerprint=? WHERE subject_id=?", ("0" * 64, str(audio.id)))
        if mutation == "producer":
            now = datetime.now(timezone.utc)
            job = Job(project_id=project, type=JobType.GENERATE_VOICE, idempotency_key="producer", created_at=now, updated_at=now)
            JobRepository(db).create(job)
            ExecutionScopeService(db, path.parent).pin("job", job.id, scope, operation="generation", subject_sha256=job_use_identity(job))
            AudioAssetRepository(db).update(audio.model_copy(update={"metadata": {"voice_generation": {"job_id": str(job.id)}}}))
            db.connection.execute("DELETE FROM execution_use_content_origins WHERE subject_id=?", (str(audio.id),))
            db.connection.execute("DELETE FROM execution_use_scopes WHERE subject_id=?", (str(audio.id),))
    assert client.get(f"/audio-assets/{audio.id}/media", params=params).status_code == 403
    assert client.get(f"/audio-assets/{audio.id}/access", params=params).json()["state"] == "blocked"


def test_reimport_and_generic_aliases_cannot_launder_known_bytes(audio_case):
    client, path, project, receipt, scope, audio, source = audio_case
    with Database(path) as db:
        clone = audio.model_copy(update={"id": uuid4(), "metadata": {}, "content_hash": "d" * 64})
        AudioAssetRepository(db).create(clone)
        asset = Asset(source_file=str(source), content_hash="f" * 64, duration_ms=1000,
            width=32, height=48, fps=RationalFps(numerator=30, denominator=1),
            authorization_reference="synthetic", imported_at=audio.imported_at)
        AssetRepository(db).create(asset)
        clip = Clip(asset_id=asset.id, start_ms=0, end_ms=1000, asset_duration_ms=1000)
        ClipRepository(db).create(clip)
        image = ImageAsset(source_file=str(source), content_hash="e" * 64, width=32, height=48,
            authorization_reference="synthetic", imported_at=audio.imported_at)
        ImageAssetRepository(db).create(image)
    for url in (f"/audio-assets/{clone.id}/media", f"/assets/{asset.id}/media", f"/clips/{clip.id}/media", f"/image-assets/{image.id}/media"):
        assert client.get(url, headers={"Range": "bytes=0-4"}).status_code == 403
    render_id = uuid4()
    rendered = client.app.state.render_output_root / str(project) / f"{render_id}.mp4"
    rendered.parent.mkdir(parents=True, exist_ok=True)
    rendered.write_bytes(source.read_bytes())
    assert client.get(f"/projects/{project}/renders/{render_id}").status_code == 403
    assert client.head(f"/projects/{project}/renders/{render_id}").status_code == 403
    # Inventing a new hash does not restore access even with review context.
    assert client.get(f"/audio-assets/{clone.id}/media", params=context(project)).status_code == 403
    status = client.get(f"/audio-assets/{clone.id}/access", params=context(project)).json()
    assert status["manifest"]["lineage"] == "known"
    assert status["manifest"]["scopes"][0]["purpose"] == "internal_evaluation"


def test_legacy_portable_audio_compatibility_and_no_implicit_grants(audio_case):
    client, path, project, receipt, scope, audio, source = audio_case
    fresh = path.parent / "legacy.wav"
    fresh.write_bytes(b"legacy distinct fixture bytes")
    legacy = audio.model_copy(update={"id": uuid4(), "source_file": fresh.name,
        "content_hash": hashlib.sha256(fresh.read_bytes()).hexdigest()})
    with Database(path) as db: AudioAssetRepository(db).create(legacy)
    assert client.get(f"/audio-assets/{legacy.id}/media").content == fresh.read_bytes()
    status = client.get(f"/audio-assets/{legacy.id}/access").json()
    assert status["state"] == "legacy_available" and status["manifest"]["lineage"] == "unknown"
    assert status["manifest"]["commercial_authorized"] is False


def test_access_token_is_separate_from_current_review_permission(audio_case, monkeypatch):
    client, path, project, receipt, scope, audio, source = audio_case
    monkeypatch.setenv("CONTENT_OS_ACCESS_TOKEN", "synthetic-token")
    url = f"/audio-assets/{audio.id}/media"
    assert client.get(url, params=context(project)).status_code == 401
    authorized = {"Authorization": "Bearer synthetic-token"}
    assert client.get(url, params=context(project), headers=authorized).status_code == 200
    assert client.get(url, headers=authorized).status_code == 403


def test_all_mixed_origins_are_checked_instead_of_selecting_one(audio_case):
    client, path, project, receipt, scope, audio, source = audio_case
    foreign = ExecutionUseScope(project_id=uuid4(), purpose="commercial_production")
    subject_id = uuid4()
    with Database(path) as db:
        projects = ProjectRepository(db)
        projects.create(projects.get(project).model_copy(update={"id": foreign.project_id}))
        # Deliberately corrupted fixture, bypassing the writer which already rejects this mix.
        db.connection.execute("INSERT INTO execution_use_scopes(subject_kind,subject_id,project_id,fingerprint,payload) VALUES (?,?,?,?,?)",
            ("image", str(subject_id), str(foreign.project_id), foreign.fingerprint, foreign.model_dump_json()))
        db.connection.execute("INSERT INTO execution_use_content_origins(content_hash,subject_kind,subject_id) VALUES (?,?,?)",
            (audio.content_hash, "image", str(subject_id)))
    response = client.get(f"/audio-assets/{audio.id}/media", params=context(project))
    assert response.status_code == 403 and response.json()["detail"] == "provider_use_project_mismatch"


def test_generation_permission_does_not_imply_review(admission_setup):
    client, path, project, profile, identity, report, payload = admission_setup
    report = report.model_copy(update={"permitted_operations": ["generation"]})
    evidence = path.parent / "generation-only.json"
    evidence.write_text(report.model_dump_json(), encoding="utf-8")
    from uuid import UUID
    receipt = client.post(f"/projects/{project}/provider-use-admissions", headers=headers(), json={**payload,
        "review_reference": evidence.name, "review_sha256": hashlib.sha256(evidence.read_bytes()).hexdigest()})
    assert receipt.status_code == 201
    source = path.parent / "generation-only.wav"
    source.write_bytes(b"generation only synthetic bytes")
    audio = AudioAsset(source_file=source.name, content_hash=hashlib.sha256(source.read_bytes()).hexdigest(),
        duration_ms=1000, sample_rate=24000, channels=1, authorization_reference="synthetic-only",
        imported_at=datetime.now(timezone.utc))
    with Database(path) as db:
        service = ExecutionScopeService(db, path.parent)
        scope = service.prepare(project, purpose="internal_evaluation", admission_ids=(UUID(receipt.json()["id"]),))
        AudioAssetRepository(db).create(audio)
        service.pin("audio", audio.id, scope, operation="generation", content_hash=audio.content_hash)
    assert client.get(f"/audio-assets/{audio.id}/media", params=context(project)).status_code == 403
