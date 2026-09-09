"""Local-only Codex-authored fixture smoke test.

The injected planner and embedding classes are test doubles, not Content OS
providers and not representations of Codex runtime APIs.  The video sources
are actual locally encoded MP4s; no transcript or vision observation is
invented for the fixture.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from app.db import AssetRepository, ClipRepository, Database, IPProfileRepository, ProjectRepository
from app.domain.models import Clip, IPProfile, Project, RationalFps, ScenePlan, SourceKind, VideoSpec, VisualIntent
from app.main import create_app
from app.media import FFProbeAdapter, MediaImporter
from app.providers.embedding import EmbeddingBatch
from app.providers.scene_planner import ScenePlanResult
from app.renderer import RemotionRenderer
from app.search import ClipEmbeddingIndexer


class LocalFixtureScenePlanner:
    """Test-only deterministic ScenePlan source, explicitly injected into create_app."""

    def plan(self, project: Project, *, script: str | None = None, topic: str | None = None) -> ScenePlanResult:
        assert script == "Show a local editing workflow."
        assert topic == "Local fixture workflow"
        scenes = tuple(
            ScenePlan(
                project_id=project.id,
                scene_id=f"fixture-{order}",
                order=order,
                purpose="hook" if order == 0 else "explain",
                voice_text=f"Fixture sequence {order + 1}.",
                duration_target_ms=400,
                visual_intent=VisualIntent(subject="creator", action="editing locally", framing="close"),
                preferred_sources=[SourceKind.USER_ASSET],
                fallback_sources=[SourceKind.CAPTURE],
                caption_emphasis=[f"fixture-sequence-{order + 1}"],
            )
            for order in range(2)
        )
        return ScenePlanResult(project.id, scenes)


class DeterministicFixtureEmbedding:
    """Test-only vector source; it does not call or impersonate a provider.

    The ScenePlan's ``fixture-sequence-*`` labels are test routing controls,
    not observations about the source clips.  Indexed Clip vectors are ordered
    by the local fixture's explicit source order, so no transcript or vision
    metadata is fabricated to influence selection.
    """

    def embed(self, texts: tuple[str, ...]) -> EmbeddingBatch:
        if len(texts) > 1:
            return EmbeddingBatch(tuple((1.0, 0.0) if index == 0 else (0.0, 1.0) for index, _ in enumerate(texts)))
        query = texts[0].lower()
        if "fixture-sequence-1" in query:
            return EmbeddingBatch(((1.0, 0.0),))
        if "fixture-sequence-2" in query:
            return EmbeddingBatch(((0.0, 1.0),))
        raise AssertionError(f"unexpected local fixture query: {texts[0]}")


def _binaries() -> tuple[str, str]:
    # The persistent-runner contract intentionally does not discover tools from
    # PATH: a report must say exactly which local binaries were injected.
    ffmpeg = os.environ.get("CONTENT_OS_FFMPEG")
    ffprobe = os.environ.get("CONTENT_OS_FFPROBE")
    if not ffmpeg or not ffprobe:
        pytest.skip("set CONTENT_OS_FFMPEG and CONTENT_OS_FFPROBE to run this real-media fixture")
    return ffmpeg, ffprobe


def _make_source(ffmpeg: str, output: Path, color: str, frequency: int) -> None:
    subprocess.run(
        [ffmpeg, "-y", "-f", "lavfi", "-i", f"color=c={color}:size=360x640:rate=30", "-f", "lavfi", "-i",
         f"sine=frequency={frequency}:sample_rate=48000", "-t", "0.8", "-c:v", "mpeg4", "-q:v", "2",
         "-c:a", "aac", "-shortest", str(output)],
        check=True, capture_output=True,
    )


def _artifact_dir() -> Path | None:
    """Return the runner's staging directory, if this is a persisted run."""
    configured = os.environ.get("CONTENT_OS_LOCAL_FIXTURE_ARTIFACT_DIR")
    if not configured:
        return None
    directory = Path(configured).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_local_codex_fixture_runs_plan_route_spec_and_remotion_render(tmp_path: Path) -> None:
    ffmpeg, ffprobe = _binaries()
    artifact_dir = _artifact_dir()
    database_path = tmp_path / "fixture.sqlite"
    sources = [tmp_path / "fixture red.mp4", tmp_path / "fixture blue 中文.mp4"]
    _make_source(ffmpeg, sources[0], "red", 440)
    _make_source(ffmpeg, sources[1], "blue", 660)
    if artifact_dir:
        script_copy = artifact_dir / "script" / Path(__file__).name
        script_copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(__file__), script_copy)
        for source in sources:
            destination = artifact_dir / "fixture-input" / source.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        _write_json(artifact_dir / "fixture-input.json", {
            "schema_version": 1,
            "kind": "deterministic_local_fixture",
            "script": "Show a local editing workflow.",
            "topic": "Local fixture workflow",
            "sources": [
                {"filename": source.name, "color": color, "sine_frequency_hz": frequency, "rights_ref": f"fixture-rights-{index}"}
                for index, (source, color, frequency) in enumerate(zip(sources, ("red", "blue"), (440, 660), strict=True))
            ],
            "binaries": {"ffmpeg": ffmpeg, "ffprobe": ffprobe},
        })

    db = Database(database_path)
    try:
        profile = IPProfile(creator_name="Local fixture creator")
        project = Project(
            ip_profile_id=profile.id, title="Local Codex fixture", topic="Local fixture workflow",
            resolution_width=360, resolution_height=640, fps=RationalFps(numerator=30, denominator=1),
            created_at=datetime.now(timezone.utc),
        )
        with db.transaction():
            IPProfileRepository(db).create(profile)
            ProjectRepository(db).create(project)
        importer = MediaImporter(db, tmp_path / "fixture data", FFProbeAdapter(ffprobe))
        assets = [importer.import_path(source, f"fixture-rights-{index}") for index, source in enumerate(sources)]
        clips = [Clip(asset_id=asset.id, start_ms=100, end_ms=500, asset_duration_ms=asset.duration_ms) for asset in assets]
        clip_repository = ClipRepository(db)
        for clip in clips:
            clip_repository.create(clip)
        embedding = DeterministicFixtureEmbedding()
        ClipEmbeddingIndexer(db, embedding).index_clips(clips)
    finally:
        db.close()

    with TestClient(create_app(database_path, scene_planner=LocalFixtureScenePlanner(), embedding_provider=embedding)) as client:
        plan_response = client.post(f"/projects/{project.id}/scene-plan", json={"script": "Show a local editing workflow.", "topic": project.topic})
        assert plan_response.status_code == 200
        scenes = plan_response.json()["scenes"]
        assert [scene["order"] for scene in scenes] == [0, 1]
        if artifact_dir:
            _write_json(artifact_dir / "scene-plan.json", plan_response.json())

        route_response = client.post(f"/projects/{project.id}/asset-routes", json={"scenes": scenes, "max_candidates": 1})
        assert route_response.status_code == 200
        selections = [result["candidates"][0] for result in route_response.json()]
        assert {selection["clip_id"] for selection in selections} == {str(clip.id) for clip in clips}
        assert all(selection["source_kind"] == "user_asset" and not selection["requires_capture"] for selection in selections)
        if artifact_dir:
            _write_json(artifact_dir / "route-candidates.json", route_response.json())

        spec_response = client.post(f"/projects/{project.id}/video-spec", json={"scenes": scenes, "selections": selections})
        assert spec_response.status_code == 200
        spec = spec_response.json()
        assert [scene["start_frame"] for scene in spec["scenes"]] == [0, 12]
        assert [scene["duration_frames"] for scene in spec["scenes"]] == [12, 12]
        if artifact_dir:
            _write_json(artifact_dir / "video-spec.json", spec)

    output = artifact_dir / "rendered" / "local-codex-fixture.mp4" if artifact_dir else tmp_path / "local codex fixture.mp4"
    output.parent.mkdir(parents=True, exist_ok=True)
    renderer_db = Database(database_path)
    try:
        rendered = RemotionRenderer(AssetRepository(renderer_db), ClipRepository(renderer_db), renderer_dir=Path(__file__).parents[3] / "apps" / "renderer", timeout_seconds=120).render(
            VideoSpec.model_validate(spec), output
        )
    finally:
        renderer_db.close()
    assert rendered == output.resolve() and output.is_file() and output.stat().st_size > 0
    probe = subprocess.run([ffprobe, "-v", "error", "-count_frames", "-show_entries", "stream=codec_type,nb_read_frames", "-of", "json", str(output)], check=True, capture_output=True, text=True)
    streams = json.loads(probe.stdout)["streams"]
    assert next(stream for stream in streams if stream["codec_type"] == "video")["nb_read_frames"] == "24"
    assert any(stream["codec_type"] == "audio" for stream in streams)
    if artifact_dir:
        _write_json(artifact_dir / "ffprobe.json", json.loads(probe.stdout))
