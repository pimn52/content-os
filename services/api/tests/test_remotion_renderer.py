from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app.db import AssetRepository, ClipRepository, Database
from app.domain.models import Asset, Clip, ProjectFormat, RationalFps, SourceKind, VideoScene, VideoSpec, VideoVisual
from app.renderer import LocalResourceError, RemotionRenderer, RenderInputError, RenderProcessError, UnauthorizedVisualError


NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)


class _Runner:
    def __init__(self, *, returncode: int = 0) -> None:
        self.returncode = returncode
        self.calls: list[tuple[list[str], Path, float, dict[str, object]]] = []
        self.staged_bytes: dict[str, bytes] = {}

    def run(self, argv: list[str], *, cwd: Path, timeout_seconds: float) -> subprocess.CompletedProcess[bytes]:
        props_path = Path(next(value.split("=", 1)[1] for value in argv if value.startswith("--props=")))
        props = json.loads(props_path.read_text(encoding="utf-8"))
        self.calls.append((argv, cwd, timeout_seconds, props))
        for scene in props["sceneSources"].values():
            self.staged_bytes[scene["src"]] = (cwd / "public" / scene["src"]).read_bytes()
        if self.returncode == 0:
            output = Path(argv[7])
            output.write_bytes(b"minimal-mp4")
        return subprocess.CompletedProcess(argv, self.returncode, b"", b"private renderer stderr")


def _renderer_project(root: Path) -> Path:
    (root / "src").mkdir(parents=True)
    (root / "package.json").write_text("{}", encoding="utf-8")
    (root / "src" / "index.ts").write_text("export {};", encoding="utf-8")
    return root


def _setup(tmp_path: Path) -> tuple[Database, Asset, Clip, VideoSpec]:
    source = tmp_path / "源素材 with spaces.mp4"
    source.write_bytes(b"original-media-bytes")
    db = Database(tmp_path / "render.sqlite")
    asset = Asset(
        source_kind=SourceKind.USER_ASSET, source_file=str(source), content_hash="a" * 64, duration_ms=3_003,
        width=1080, height=1920, fps=RationalFps(numerator=60, denominator=1),
        authorization_reference="creator-rights", imported_at=NOW,
    )
    clip = Clip(asset_id=asset.id, start_ms=1_001, end_ms=2_002, asset_duration_ms=asset.duration_ms)
    AssetRepository(db).create(asset)
    ClipRepository(db).create(clip)
    visual = VideoVisual(
        source_kind=asset.source_kind, authorization_reference=asset.authorization_reference, asset_id=asset.id, clip_id=clip.id,
        clip_start_ms=clip.start_ms, clip_end_ms=clip.end_ms, source_duration_ms=asset.duration_ms,
    )
    spec = VideoSpec(
        project_id=uuid4(), format=ProjectFormat.VERTICAL, width=1080, height=1920,
        fps=RationalFps(numerator=30_000, denominator=1_001),
        scenes=[VideoScene(scene_id="scene_01", start_frame=0, duration_frames=30, visual=visual, caption="A local caption")],
    )
    return db, asset, clip, spec


def test_remotion_adapter_stages_local_clip_trims_and_preserves_source(tmp_path: Path) -> None:
    db, asset, _, spec = _setup(tmp_path)
    runner = _Runner()
    source = Path(asset.source_file)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    try:
        renderer_root = _renderer_project(tmp_path / "renderer")
        output = tmp_path / "output" / "vertical.mp4"
        result = RemotionRenderer(AssetRepository(db), ClipRepository(db), renderer_dir=renderer_root, runner=runner).render(spec, output)

        assert result == output.resolve() and result.read_bytes() == b"minimal-mp4"
        argv, cwd, timeout, props = runner.calls[0]
        assert Path(argv[0]).name.lower() in {"npm", "npm.cmd"}
        assert argv[1:6] == ["exec", "--", "remotion", "render", "src/index.ts"]
        assert argv[6] == "ContentOSVideo" and cwd == renderer_root.resolve() and timeout == 600.0
        assert props["audioMode"] == "source"
        source_props = props["sceneSources"]["scene_01"]
        assert source_props["trimBefore"] == 30 and source_props["trimAfter"] == 60
        assert runner.staged_bytes[source_props["src"]] == source.read_bytes()
        assert hashlib.sha256(source.read_bytes()).hexdigest() == before
        assert list((renderer_root / "public" / "content-os-renders").iterdir()) == []
    finally:
        db.close()


def test_remotion_adapter_rejects_remote_unauthorized_and_unsupported_visuals(tmp_path: Path) -> None:
    db, asset, clip, spec = _setup(tmp_path)
    runner = _Runner()
    try:
        root = _renderer_project(tmp_path / "renderer")
        renderer = RemotionRenderer(AssetRepository(db), ClipRepository(db), renderer_dir=root, runner=runner)
        remote_asset = asset.model_copy(update={"source_file": "https://example.invalid/source.mp4"})
        AssetRepository(db).update(remote_asset)
        with pytest.raises(LocalResourceError, match="remote"):
            renderer.render(spec, tmp_path / "bad.mp4")
        AssetRepository(db).update(asset)

        bad_authorization = spec.model_copy(update={"scenes": [spec.scenes[0].model_copy(update={"visual": spec.scenes[0].visual.model_copy(update={"authorization_reference": "wrong"})})]})
        with pytest.raises(UnauthorizedVisualError, match="does not match"):
            renderer.render(bad_authorization, tmp_path / "bad.mp4")
        unsupported = spec.model_copy(update={"scenes": [spec.scenes[0].model_copy(update={"transition": "fade"})]})
        with pytest.raises(RenderInputError, match="cut"):
            renderer.render(unsupported, tmp_path / "bad.mp4")
        horizontal = spec.model_copy(update={"format": ProjectFormat.HORIZONTAL, "width": 1920, "height": 1080})
        with pytest.raises(RenderInputError, match="9:16"):
            renderer.render(horizontal, tmp_path / "bad.mp4")
        assert runner.calls == []
    finally:
        db.close()


def test_remotion_adapter_cleans_generated_staging_after_process_failure(tmp_path: Path) -> None:
    db, _, _, spec = _setup(tmp_path)
    runner = _Runner(returncode=7)
    try:
        root = _renderer_project(tmp_path / "renderer")
        with pytest.raises(RenderProcessError) as raised:
            RemotionRenderer(AssetRepository(db), ClipRepository(db), renderer_dir=root, runner=runner).render(spec, tmp_path / "failed.mp4")
        assert raised.value.returncode == 7
        assert list((root / "public" / "content-os-renders").iterdir()) == []
        with pytest.raises(RenderInputError, match=".mp4"):
            RemotionRenderer(AssetRepository(db), ClipRepository(db), renderer_dir=root, runner=runner).render(spec, tmp_path / "bad.avi")
    finally:
        db.close()


def test_remotion_manifest_is_exact_and_timeline_declares_trim_caption_and_source_audio() -> None:
    root = Path(__file__).parents[3] / "apps" / "renderer"
    manifest = json.loads((root / "package.json").read_text(encoding="utf-8"))
    assert manifest["dependencies"]["remotion"] == "4.0.522"
    assert manifest["dependencies"]["@remotion/cli"] == "4.0.522"
    assert "@remotion/renderer" not in manifest["dependencies"]
    assert all(not value.startswith(("^", "~")) for value in manifest["dependencies"].values())
    timeline = (root / "src" / "video.tsx").read_text(encoding="utf-8")
    assert "trimBefore" in timeline and "trimAfter" in timeline
    assert "scene.caption" in timeline and "audioMode === 'source'" in timeline
