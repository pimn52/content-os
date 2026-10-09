"""V73c: negative crop observations cannot become positive suitability."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import AssetRepository, ClipRepository, Database
from app.domain.models import Asset, Clip, RationalFps, ScenePlan, SourceCropWindow, SourceKind, VisualIntent
from app.main import create_app
from app.media.crop_assessment import CropAssessment, CropAssessmentRepository, CropProposal, FaceBoxObservation


def _source(db: Database, *, content_hash: str = "a" * 64) -> tuple[Asset, Clip]:
    asset = Asset(
        source_kind=SourceKind.USER_ASSET, source_file="fixture-horizontal.mp4",
        content_hash=content_hash, duration_ms=5_000, width=1080, height=640,
        fps=RationalFps(numerator=25, denominator=1),
        authorization_reference="authorized-fixture", imported_at=datetime.now(timezone.utc),
    )
    clip = Clip(
        asset_id=asset.id, start_ms=0, end_ms=5_000, asset_duration_ms=5_000,
        transcript="A fresh creator point.", visual_description="creator showing a workflow",
    )
    AssetRepository(db).create(asset)
    ClipRepository(db).create(clip)
    return asset, clip


def _proposal(asset: Asset, clip: Clip, *, scene_id=None, x: int = 460, end_ms: int = 5_000) -> CropProposal:
    return CropProposal(
        scene_plan_id=scene_id, source_asset_id=asset.id, source_clip_id=clip.id,
        start_ms=0, end_ms=end_ms, crop_window=SourceCropWindow(x=x, y=0, width=360, height=640),
    )


def _assessment(proposal: CropProposal, *, face_x: int = 457) -> CropAssessment:
    return CropAssessment(
        proposal=proposal, source_content_hash="a" * 64,
        evidence_class="assisted_test",
        method="mediapipe_face_detection", method_version="0.10.10",
        coverage="full_frame_scan", evidence_reference="local:v73a/face-coverage.json#frame55",
        observations=(FaceBoxObservation(
            time_ms=2_200, face_box=SourceCropWindow(x=face_x, y=268, width=207, height=207),
            confidence=0.9,
        ),),
    )


def test_persisted_negative_is_exact_and_unknown_never_overrides_it(tmp_path: Path) -> None:
    path = tmp_path / "crop.sqlite"
    with Database(path) as db:
        asset, clip = _source(db)
        proposal = _proposal(asset, clip)
        repo = CropAssessmentRepository(db)
        assert repo.decision(proposal) == "unknown"
        inside = _assessment(proposal, face_x=500)
        assert inside.decision == "unknown"
        repo.create(inside)
        assert repo.decision(proposal) == "unknown"
        fixture_only = _assessment(proposal).model_copy(update={"evidence_class": "fixture"})
        repo.create(fixture_only)
        assert repo.decision(proposal) == "unknown"
        outside = _assessment(proposal)
        assert outside.decision == "unusable"
        repo.create(outside)
        assert repo.decision(proposal) == "unusable"
        assert repo.decision(_proposal(asset, clip, x=400)) == "unknown"
        assert repo.decision(_proposal(asset, clip, end_ms=4_000)) == "unknown"
        different_clip = ClipRepository(db).create(Clip(
            asset_id=asset.id, start_ms=0, end_ms=5_000, asset_duration_ms=5_000,
        ))
        assert repo.decision(_proposal(asset, different_clip)) == "unknown"
        # AssetRepository forbids hash mutation; simulate a corrupted/legacy row
        # to prove the assessment still fails closed if its bound hash diverges.
        changed = asset.model_copy(update={"content_hash": "b" * 64})
        db.connection.execute(
            "UPDATE assets SET content_hash = ?, payload = ? WHERE id = ?",
            (changed.content_hash, changed.model_dump_json(), str(asset.id)),
        )
        assert repo.decision(proposal) == "unknown"
    with Database(path) as db:
        assert CropAssessmentRepository(db).decision(proposal) == "unknown"


def test_crop_assessment_rejects_stale_or_out_of_clip_observations(tmp_path: Path) -> None:
    with Database(tmp_path / "bounds.sqlite") as db:
        asset, clip = _source(db)
        repo = CropAssessmentRepository(db)
        bad_hash = _assessment(_proposal(asset, clip)).model_copy(update={"source_content_hash": "b" * 64})
        try:
            repo.create(bad_hash)
            assert False, "stale hash must fail"
        except ValueError as exc:
            assert "stale" in str(exc)
        bad_interval = _assessment(_proposal(asset, clip, end_ms=6_000))
        try:
            repo.create(bad_interval)
            assert False, "out-of-Clip interval must fail"
        except ValueError as exc:
            assert "Clip" in str(exc)


def test_preflight_and_router_exclude_only_the_proposed_bad_crop(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "preflight.sqlite"
    monkeypatch.setenv("CONTENT_OS_RETRIEVAL_MODE", "lexical")
    with TestClient(create_app(path)) as client:
        project_id = client.post("/projects", json={"title": "Crop boundary", "topic": "New"}).json()["id"]
        scene = ScenePlan(
            project_id=project_id, scene_id="hook", order=0, purpose="hook",
            voice_text="A fresh creator point.", duration_target_ms=3_000,
            visual_intent=VisualIntent(subject="creator", action="showing a workflow"),
            preferred_sources=[SourceKind.USER_ASSET], fallback_sources=[SourceKind.TYPOGRAPHY],
        )
        saved = client.put(f"/projects/{project_id}/draft", json={
            "script": scene.voice_text, "topic": "New", "scenes": [scene.model_dump(mode="json")],
        })
        assert saved.status_code == 200, saved.text
        with Database(path) as db:
            asset, clip = _source(db)
        proposal = _proposal(asset, clip, scene_id=scene.id)
        no_evidence = client.post(f"/projects/{project_id}/production-preflight", json={
            "crop_proposals": [proposal.model_dump(mode="json")],
        }).json()
        assert no_evidence["scenes"][0]["selected_candidate"]["clip_id"] == str(clip.id)
        recorded = client.post(f"/assets/{asset.id}/crop-assessments", json=_assessment(proposal).model_dump(mode="json"))
        assert recorded.status_code == 201, recorded.text
        assert CropAssessment.model_validate(recorded.json()).decision == "unusable"
        forged_runtime = _assessment(proposal).model_copy(update={"evidence_class": "runtime"})
        assert client.post(
            f"/assets/{asset.id}/crop-assessments", json=forged_runtime.model_dump(mode="json"),
        ).status_code == 422
        blocked = client.post(f"/projects/{project_id}/production-preflight", json={
            "crop_proposals": [proposal.model_dump(mode="json")],
            "expected_fingerprint": no_evidence["fingerprint"],
        })
        assert blocked.status_code == 409
        latest = client.post(f"/projects/{project_id}/production-preflight", json={
            "crop_proposals": [proposal.model_dump(mode="json")],
        }).json()
        assert latest["evidence_level"] == "metadata_and_persisted_negative_assessment"
        assert latest["scenes"][0]["selected_candidate"]["source_kind"] == "typography"
        assert any("face-boundary failure" in reason for reason in latest["scenes"][0]["reasons"])
        route = client.post(f"/projects/{project_id}/asset-routes", json={
            "scenes": [scene.model_dump(mode="json")],
            "crop_proposals": [proposal.model_dump(mode="json")],
        })
        assert route.status_code == 200, route.text
        assert str(clip.id) not in {candidate["clip_id"] for candidate in route.json()[0]["candidates"] if candidate["clip_id"]}
        unchanged = client.post(f"/projects/{project_id}/production-preflight", json={}).json()
        assert unchanged["scenes"][0]["selected_candidate"]["clip_id"] == str(clip.id)
        capture_scene = scene.model_copy(update={"fallback_sources": [SourceKind.CAPTURE]})
        revised = client.put(f"/projects/{project_id}/draft", json={
            "script": scene.voice_text, "topic": "New", "scenes": [capture_scene.model_dump(mode="json")],
        })
        assert revised.status_code == 200, revised.text
        capture = client.post(f"/projects/{project_id}/production-preflight", json={
            "crop_proposals": [proposal.model_dump(mode="json")],
        }).json()
        assert capture["scenes"][0]["production_need"] == "capture"
        assert "capture_required:hook" in capture["stop_reasons"]
        with Database(path) as db:
            _, alternative = _source(db, content_hash="b" * 64)
        fallback = client.post(f"/projects/{project_id}/production-preflight", json={
            "crop_proposals": [proposal.model_dump(mode="json")],
        }).json()
        assert fallback["scenes"][0]["selected_candidate"]["clip_id"] == str(alternative.id)
