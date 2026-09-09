from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import AssetRepository, ClipRepository, Database, IPProfileRepository, ProjectRepository
from app.domain.models import Asset, CandidateAsset, Clip, CostCategory, IPProfile, Project, RationalFps, ScenePlan, SourceKind, UsageCost, VideoSpec, VisualIntent
from app.main import create_app
from app.renderer import RemotionRenderer


class _WritingRunner:
    """Local Remotion-process substitute: it writes a small MP4-shaped fixture."""

    def __init__(self) -> None:
        self.calls = 0

    def run(self, argv: list[str], *, cwd: Path, timeout_seconds: float) -> subprocess.CompletedProcess[bytes]:
        self.calls += 1
        Path(argv[7]).write_bytes(b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2")
        return subprocess.CompletedProcess(argv, 0, b"", b"")


def _seed(path: Path, root: Path) -> tuple[Project, ScenePlan, CandidateAsset]:
    db = Database(path)
    try:
        profile = IPProfile(creator_name="Creator")
        project = Project(
            ip_profile_id=profile.id, title="Render", topic="Local clip", fps=RationalFps(numerator=30, denominator=1),
            created_at=datetime.now(timezone.utc),
        )
        source = root / "local source.mp4"
        source.write_bytes(b"source media")
        asset = Asset(
            source_file=str(source), content_hash="9" * 64, duration_ms=2_000, width=1080, height=1920,
            fps=project.fps, authorization_reference="creator-rights", imported_at=datetime.now(timezone.utc),
        )
        clip = Clip(asset_id=asset.id, start_ms=0, end_ms=1_000, asset_duration_ms=asset.duration_ms)
        with db.transaction():
            IPProfileRepository(db).create(profile)
            ProjectRepository(db).create(project)
            AssetRepository(db).create(asset)
            ClipRepository(db).create(clip)
    finally:
        db.close()
    scene = ScenePlan(
        project_id=project.id, scene_id="hook", order=0, purpose="hook", voice_text="Local render",
        duration_target_ms=800, visual_intent=VisualIntent(subject="creator"),
        preferred_sources=[SourceKind.USER_ASSET], fallback_sources=[SourceKind.CAPTURE],
    )
    candidate = CandidateAsset(
        scene_plan_id=scene.id, source_kind=SourceKind.USER_ASSET, asset_id=asset.id, clip_id=clip.id,
        match_score=0.9, why=["authorized local clip"], recommended=True,
        estimated_cost=UsageCost(category=CostCategory.USER_ASSET, amount=Decimal("0"), currency="USD"),
    )
    return project, scene, candidate


def _renderer_factory(renderer_root: Path, runner: _WritingRunner):
    def factory(db: Database) -> RemotionRenderer:
        return RemotionRenderer(AssetRepository(db), ClipRepository(db), renderer_dir=renderer_root, runner=runner)
    return factory


def _renderer_project(root: Path) -> Path:
    (root / "src").mkdir(parents=True)
    (root / "package.json").write_text("{}", encoding="utf-8")
    (root / "src" / "index.ts").write_text("export {};", encoding="utf-8")
    return root


def test_render_api_assembles_and_serves_a_local_mp4_under_owned_root(tmp_path: Path) -> None:
    database_path = tmp_path / "render.sqlite"
    project, scene, candidate = _seed(database_path, tmp_path)
    output_root = tmp_path / "content-os-data" / "test-runs"
    runner = _WritingRunner()
    app = create_app(database_path, renderer_factory=_renderer_factory(_renderer_project(tmp_path / "renderer"), runner), render_output_root=output_root)

    with TestClient(app) as client:
        response = client.post(f"/projects/{project.id}/render", json={
            "scenes": [scene.model_dump(mode="json")], "selections": [candidate.model_dump(mode="json")],
        })
        assert response.status_code == 200
        body = response.json()
        assert body["project_id"] == str(project.id)
        assert body["download_url"] == body["preview_url"]
        assert body["video_spec"]["project_id"] == str(project.id)
        file_response = client.get(body["download_url"])
        assert file_response.status_code == 200
        assert file_response.headers["content-type"].startswith("video/mp4")
        assert file_response.content[4:8] == b"ftyp"
        render_id = body["render_id"]
        assert (output_root / str(project.id) / f"{render_id}.mp4").is_file()
        assert runner.calls == 1


def test_render_api_rejects_foreign_project_unauthorized_spec_and_unknown_files(tmp_path: Path) -> None:
    database_path = tmp_path / "render-errors.sqlite"
    project, scene, candidate = _seed(database_path, tmp_path)
    output_root = tmp_path / "content-os-data" / "test-runs"
    runner = _WritingRunner()
    app = create_app(database_path, renderer_factory=_renderer_factory(_renderer_project(tmp_path / "renderer"), runner), render_output_root=output_root)
    with TestClient(app) as client:
        missing = client.post("/projects/00000000-0000-0000-0000-000000000001/render", json={
            "scenes": [scene.model_dump(mode="json")], "selections": [candidate.model_dump(mode="json")],
        })
        assert missing.status_code == 404

        spec = VideoSpec.model_validate(client.post(f"/projects/{project.id}/video-spec", json={
            "scenes": [scene.model_dump(mode="json")], "selections": [candidate.model_dump(mode="json")],
        }).json())
        foreign = spec.model_copy(update={"project_id": scene.id})
        assert client.post(f"/projects/{project.id}/render", json={"video_spec": foreign.model_dump(mode="json")}).status_code == 422

        bad_visual = spec.model_copy(update={"scenes": [spec.scenes[0].model_copy(update={
            "visual": spec.scenes[0].visual.model_copy(update={"authorization_reference": "not-authorized"}),
        })]})
        rejected = client.post(f"/projects/{project.id}/render", json={"video_spec": bad_visual.model_dump(mode="json")})
        assert rejected.status_code == 422
        assert "authorized stored Clip" in rejected.json()["detail"]
        assert runner.calls == 0

        # UUID route parameters and server-owned output names leave no path
        # segment that a caller can turn into traversal.
        assert client.get(f"/projects/{project.id}/renders/00000000-0000-0000-0000-000000000001").status_code == 404
        assert client.get(f"/projects/{project.id}/renders/%2E%2E%2Fsecret.mp4").status_code == 404
