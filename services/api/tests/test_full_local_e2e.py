"""Full local workflow coverage for the currently supported Content OS path.

The media inputs are short, real FFmpeg-encoded files so this test verifies
import/probing, local analysis, timestamped Clip persistence, narration
binding, and the actual Remotion output.  The planner and embedding objects
are explicitly test-only protocol doubles: they exercise orchestration only
and make no semantic claim about the generated content.  Semantic acceptance
continues to require the real assisted-test or local-ASR evidence paths.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.db import AssetRepository, ClipRepository, Database
from app.domain.models import JobType, Project, ScenePlan, SourceKind, VisualIntent
from app.jobs import JobRunner, JobStore
from app.jobs.handlers import AssetAnalysisJobHandler
from app.jobs.targets import AssetJobTargetStore
from app.media import AudioImporter, FFProbeAdapter, FFmpegSceneDetector, MediaAnalysisPipeline, MediaExtractor, MediaImporter
from app.main import create_app
from app.providers.embedding import EmbeddingBatch
from app.providers.scene_planner import ScenePlanResult
from app.runtime import resolve_local_executable
from app.search import ClipEmbeddingIndexer


def _binaries() -> tuple[str, str]:
    ffmpeg_candidates = [os.environ.get("CONTENT_OS_FFMPEG", ""), shutil.which("ffmpeg") or "", resolve_local_executable("ffmpeg")]
    ffmpeg = next((candidate for candidate in ffmpeg_candidates if Path(candidate).is_file() and _supports_encoding(candidate)), None)
    ffprobe = os.environ.get("CONTENT_OS_FFPROBE") or shutil.which("ffprobe") or resolve_local_executable("ffprobe")
    if not ffmpeg or not Path(ffprobe).is_file():
        pytest.skip("real FFmpeg/ffprobe binaries are unavailable")
    return ffmpeg, ffprobe


def _supports_encoding(binary: str) -> bool:
    try:
        completed = subprocess.run(
            [binary, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:size=16x16:rate=1", "-t", "0.1", "-c:v", "mpeg4", "-f", "null", "-"],
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _make_source(ffmpeg: str, output: Path) -> None:
    subprocess.run(
        [
            ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=navy:size=360x640:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "0.8",
            "-c:v", "mpeg4", "-q:v", "2", "-c:a", "aac", "-shortest", str(output),
        ],
        check=True,
        capture_output=True,
    )


def _make_narration(ffmpeg: str, output: Path, frequency: int) -> None:
    subprocess.run(
        [
            ffmpeg, "-y", "-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=48000",
            "-t", "0.4", "-c:a", "pcm_s16le", str(output),
        ],
        check=True,
        capture_output=True,
    )


class _OrchestrationPlanner:
    """Test-only planner; its text is not used as semantic evidence."""

    def plan(self, project: Project, *, script: str | None = None, topic: str | None = None) -> ScenePlanResult:
        assert script == "Local workflow narration."
        assert topic == project.topic
        scenes = tuple(
            ScenePlan(
                project_id=project.id,
                scene_id=f"local-{order + 1}",
                order=order,
                purpose="hook" if order == 0 else "close",
                voice_text=f"Local narration scene {order + 1}",
                duration_target_ms=300,
                visual_intent=VisualIntent(subject="local source", action="shows workflow", framing="close"),
                preferred_sources=[SourceKind.USER_ASSET],
                fallback_sources=[SourceKind.TYPOGRAPHY],
                caption_emphasis=["local workflow"],
            )
            for order in range(2)
        )
        return ScenePlanResult(project.id, scenes)


class _OrchestrationEmbedding:
    """Test-only vector transport for routing mechanics, never semantic output."""

    def embed(self, texts: tuple[str, ...]) -> EmbeddingBatch:
        assert texts
        return EmbeddingBatch(tuple((1.0, 0.0) for _ in texts))


def _run_local_analysis(database_path: Path, data_root: Path, job_id: UUID, ffmpeg: str) -> None:
    # TestClient owns the application connection in its lifespan thread.  A
    # real worker uses its own SQLite connection, so the E2E test does the
    # same instead of reaching across the TestClient thread boundary.
    with Database(database_path) as db:
        target_store = AssetJobTargetStore(db)
        pipeline = MediaAnalysisPipeline(
            db,
            FFmpegSceneDetector(ffmpeg, min_clip_duration_ms=200),
            MediaExtractor(data_root, command=ffmpeg),
        )
        result = JobRunner(
            JobStore(db),
            {JobType.ANALYZE_ASSET: AssetAnalysisJobHandler(
                target_store, AssetRepository(db), pipeline,
            )},
            worker_id="full-local-e2e",
            lease_duration=timedelta(minutes=1),
            max_attempts=1,
        ).run_once()
        assert result is not None and result.id == job_id and result.status.value == "completed"


def _probe(ffprobe: str, path: Path) -> dict[str, object]:
    completed = subprocess.run(
        [ffprobe, "-v", "error", "-count_frames", "-show_entries", "format=duration:stream=codec_type,width,height,nb_read_frames", "-of", "json", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_full_local_e2e_import_analyze_plan_match_voice_gate_talking_and_render(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ffmpeg, ffprobe = _binaries()
    monkeypatch.setenv("CONTENT_OS_RETRIEVAL_MODE", "embedding")
    source = tmp_path / "source with spaces.mp4"
    narration_one = tmp_path / "narration one.wav"
    narration_two = tmp_path / "narration two.wav"
    _make_source(ffmpeg, source)
    _make_narration(ffmpeg, narration_one, 660)
    _make_narration(ffmpeg, narration_two, 880)

    database_path = tmp_path / "full-local-e2e.sqlite"
    embeddings = _OrchestrationEmbedding()
    planner = _OrchestrationPlanner()

    def media_importer_factory(db: Database, root: Path) -> MediaImporter:
        return MediaImporter(db, root, FFProbeAdapter(ffprobe))

    def audio_importer_factory(db: Database, root: Path) -> AudioImporter:
        return AudioImporter(db, root, FFProbeAdapter(ffprobe))

    app = create_app(
        database_path,
        scene_planner=planner,
        embedding_provider=embeddings,
        media_importer_factory=media_importer_factory,
        audio_importer_factory=audio_importer_factory,
        render_output_root=tmp_path / "renders",
    )

    with TestClient(app) as client:
        project_response = client.post("/projects", json={"title": "Full local E2E", "topic": "Local workflow"})
        assert project_response.status_code == 201
        project = Project.model_validate(project_response.json())

        imported = client.post("/imports", json={
            "source_path": str(source),
            "authorization_reference": "e2e-source-rights",
            "source_kind": "user_asset",
            "recursive": False,
        })
        assert imported.status_code == 201
        asset = imported.json()[0]
        asset_id = asset["id"]
        assert asset["duration_ms"] > 0 and asset["has_audio"] is True

        queued = client.post(f"/assets/{asset_id}/jobs/analyze_asset", json={"idempotency_key": "full-e2e-analysis"})
        assert queued.status_code == 201
        _run_local_analysis(database_path, tmp_path, UUID(queued.json()["id"]), ffmpeg)
        with Database(database_path) as db:
            analyzed_asset = AssetRepository(db).get(asset_id)
            clips = ClipRepository(db).list_by_asset(asset_id)
            assert analyzed_asset is not None and clips
            assert clips[0].start_ms == 0 and clips[-1].end_ms == analyzed_asset.duration_ms
            assert all(clip.end_ms > clip.start_ms for clip in clips)
            ClipEmbeddingIndexer(db, embeddings).index_clips(clips)
            planned = client.post(f"/projects/{project.id}/scene-plan", json={
            "script": "Local workflow narration.", "topic": project.topic,
        })
        assert planned.status_code == 200
        scenes = planned.json()["scenes"]
        assert [scene["order"] for scene in scenes] == [0, 1]

        routed = client.post(f"/projects/{project.id}/asset-routes", json={"scenes": scenes, "max_candidates": 1})
        assert routed.status_code == 200
        route_rows = routed.json()
        selections = [row["candidates"][0] for row in route_rows]
        assert len(selections) == 2
        assert all(candidate["source_kind"] == "user_asset" and candidate["requires_capture"] is False for candidate in selections)

        imported_audio = []
        for narration in (narration_one, narration_two):
            response = client.post("/audio-imports", json={
                "source_path": str(narration),
                "authorization_reference": "e2e-narration-rights",
                "source_kind": "user_asset",
                "language": "en",
                "recursive": False,
            })
            assert response.status_code == 201
            imported_audio.append(response.json()[0])
        assert all(item["duration_ms"] > 0 for item in imported_audio)

        narration_ids = {str(scene["id"]): audio["id"] for scene, audio in zip(scenes, imported_audio, strict=True)}
        spec_response = client.post(f"/projects/{project.id}/video-spec", json={
            "scenes": scenes,
            "selections": selections,
            "narration_asset_ids": narration_ids,
            "narration_required": True,
        })
        assert spec_response.status_code == 200
        spec = spec_response.json()
        assert [scene["narration_asset_id"] for scene in spec["scenes"]] == [audio["id"] for audio in imported_audio]
        assert all(scene["visual"]["clip_end_ms"] - scene["visual"]["clip_start_ms"] == audio["duration_ms"] for scene, audio in zip(spec["scenes"], imported_audio, strict=True))

        rendered = client.post(f"/projects/{project.id}/render", json={"video_spec": spec})
        assert rendered.status_code == 200, rendered.text
        download = client.get(rendered.json()["download_url"])
        assert download.status_code == 200
        assert download.headers["content-type"].startswith("video/mp4")
        output = tmp_path / "renders" / str(project.id) / f"{rendered.json()['render_id']}.mp4"
        assert output.is_file() and output.stat().st_size > 0
        metadata = _probe(ffprobe, output)
        video = next(stream for stream in metadata["streams"] if stream["codec_type"] == "video")
        audio = next(stream for stream in metadata["streams"] if stream["codec_type"] == "audio")
        assert (video["width"], video["height"]) == (1080, 1920)
        assert int(video["nb_read_frames"]) > 0
        assert audio["codec_type"] == "audio"

        readiness = client.get("/runtime/readiness")
        assert readiness.status_code == 200
        capabilities = {item["key"]: item for item in readiness.json()["capabilities"]}
        assert capabilities["tts"]["status"] == "not_developed"
        assert capabilities["talking"]["status"] == "not_developed"
