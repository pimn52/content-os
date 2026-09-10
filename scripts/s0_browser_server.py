"""Local S0 browser smoke server.

This is an interaction-boundary harness only. It uses a real locally encoded
MP4 and injected test providers so a browser can exercise the buttons without
calling a paid service. Its deterministic labels must not be reported as
assisted-test semantic quality or runtime-provider validation.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import uvicorn

from app.db import AssetRepository, ClipRepository, Database, IPProfileRepository, ProjectRepository
from app.domain.models import Asset, Clip, IPProfile, Project, RationalFps, ScenePlan, SourceKind, VisualIntent
from app.main import create_app
from app.providers.embedding import EmbeddingBatch
from app.providers.scene_planner import ScenePlanResult
from app.search import ClipEmbeddingIndexer


class BrowserScenePlanner:
    def plan(self, project: Project, *, script: str | None = None, topic: str | None = None) -> ScenePlanResult:
        scenes = tuple(
            ScenePlan(
                project_id=project.id,
                scene_id=name,
                order=order,
                purpose="hook" if order == 0 else "explain",
                voice_text=f"Show the {name} desk workflow.",
                duration_target_ms=800,
                visual_intent=VisualIntent(subject="desk", action=f"using the {name} setup", framing="close"),
                preferred_sources=[SourceKind.USER_ASSET],
                fallback_sources=[SourceKind.CAPTURE],
            )
            for order, name in enumerate(("red", "blue"))
        )
        return ScenePlanResult(project.id, scenes)


class BrowserEmbedding:
    def embed(self, texts: tuple[str, ...]) -> EmbeddingBatch:
        vectors = []
        for text in texts:
            lowered = text.lower()
            vectors.append((1.0, 0.0) if "red" in lowered else (0.0, 1.0) if "blue" in lowered else (0.5, 0.5))
        return EmbeddingBatch(tuple(vectors))


def _ffmpeg() -> str:
    configured = os.environ.get("CONTENT_OS_FFMPEG")
    if configured:
        return configured
    found = shutil.which("ffmpeg")
    if found:
        return found
    bundled = Path(tempfile.gettempdir()) / "content-os-ffmpeg-9.0.1-essentials" / "ffmpeg-9.0.1-essentials_build" / "bin" / "ffmpeg.exe"
    if bundled.is_file():
        return str(bundled)
    raise RuntimeError("set CONTENT_OS_FFMPEG or put ffmpeg on PATH")


def _seed(root: Path) -> Path:
    database_path = root / "s0-browser.sqlite"
    source = root / "source.mp4"
    subprocess.run(
        [_ffmpeg(), "-y", "-f", "lavfi", "-i", "color=c=red:size=360x640:rate=30", "-f", "lavfi", "-i",
         "sine=frequency=440:sample_rate=48000", "-t", "3", "-c:v", "mpeg4", "-q:v", "3", "-c:a", "aac",
         "-shortest", str(source)],
        check=True,
        capture_output=True,
    )
    db = Database(database_path)
    try:
        profile = IPProfile(creator_name="Browser smoke creator")
        project = Project(
            ip_profile_id=profile.id, title="S0 browser seed", topic="Interaction smoke",
            resolution_width=360, resolution_height=640, fps=RationalFps(numerator=30, denominator=1),
            created_at=datetime.now(timezone.utc),
        )
        assets = []
        clips = []
        for index, name in enumerate(("red", "blue")):
            asset = Asset(
                source_file=str(source), content_hash=f"{index + 1}" * 64, duration_ms=3_000,
                width=360, height=640, fps=project.fps, has_audio=True,
                authorization_reference=f"browser-smoke-{name}", imported_at=datetime.now(timezone.utc),
            )
            clip = Clip(
                asset_id=asset.id, start_ms=100 + index * 1_000, end_ms=1_900 + index * 1_000,
                asset_duration_ms=asset.duration_ms, visual_description=f"{name} desk workflow",
                action=f"using the {name} setup", quality_score=0.9,
            )
            assets.append(asset)
            clips.append(clip)
        with db.transaction():
            IPProfileRepository(db).create(profile)
            ProjectRepository(db).create(project)
            for asset, clip in zip(assets, clips, strict=True):
                AssetRepository(db).create(asset)
                ClipRepository(db).create(clip)
        ClipEmbeddingIndexer(db, BrowserEmbedding()).index_clips(clips)
    finally:
        db.close()
    return database_path


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="content-os-s0-browser-") as temporary:
        database_path = _seed(Path(temporary))
        application = create_app(database_path, scene_planner=BrowserScenePlanner(), embedding_provider=BrowserEmbedding())
        uvicorn.run(application, host="127.0.0.1", port=8765, log_level="warning")
