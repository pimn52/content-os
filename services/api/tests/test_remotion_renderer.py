from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app.db import AssetRepository, AudioAssetRepository, ClipRepository, Database, ImageAssetRepository
from app.domain.models import Asset, AudioAsset, Clip, ImageAsset, MasterNarration, PortraitPresentation, ProjectFormat, RationalFps, SourceKind, SubtitleTreatment, TranscriptSegment, VideoScene, VideoSpec, VideoVisual
from app.renderer import LocalResourceError, RemotionRenderer, RenderInputError, RenderProcessError, UnauthorizedVisualError


NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)


@pytest.mark.parametrize("frames,accepted", [(80, True), (79, False)])
def test_talking_master_interval_uses_speech_time_across_fractional_frame_boundaries(tmp_path, monkeypatch, frames, accepted):
    db, original, clip, spec = _setup(tmp_path)
    try:
        asset = original.model_copy(update={"source_kind": SourceKind.AI_VIDEO})
        AssetRepository(db).update(asset)
        clip = clip.model_copy(update={"start_ms": 0, "end_ms": 2680})
        ClipRepository(db).update(clip)
        audio_id = uuid4()
        visual = spec.scenes[0].visual.model_copy(update={"source_kind": SourceKind.AI_VIDEO,
            "clip_start_ms": 0, "clip_end_ms": 2680})
        scene = spec.scenes[0].model_copy(update={"duration_frames": frames, "visual": visual,
            "narration_asset_id": audio_id, "narration_start_ms": 8220, "narration_end_ms": 10900})
        observed = []
        monkeypatch.setattr("app.renderer.remotion.talking_visual_blocker", lambda *args, **kwargs: observed.append(kwargs))
        renderer = RemotionRenderer(AssetRepository(db), ClipRepository(db), renderer_dir=tmp_path)
        def validate():
            return renderer._validate_scene(scene, RationalFps(numerator=30, denominator=1),
                project_id=spec.project_id, master_audio_id=audio_id, validate_scene_narration=False)
        if accepted:
            assert validate().trim_after == 81
            assert observed[0]["selected_duration_ms"] == 2680
            assert observed[0]["master_interval"] == (8220, 10900)
        else:
            with pytest.raises(RenderInputError, match="narration interval"):
                validate()
            assert observed == []
    finally:
        db.close()


@pytest.mark.parametrize("changes", [
    {"vertical_reframe_mode": "bottom_crop_contain"},
    {"vertical_reframe_mode": "bottom_crop_contain", "source_bottom_crop_ratio": .15},
    {"vertical_reframe_mode": "bottom_crop_contain", "vertical_reframe_evidence_reference": "source-sheet"},
    {"source_bottom_crop_ratio": .15, "vertical_reframe_evidence_reference": "source-sheet"},
    {"vertical_reframe_mode": "bottom_crop_contain", "source_bottom_crop_ratio": .15, "vertical_reframe_evidence_reference": " "},
])
def test_bottom_crop_requires_explicit_mode_positive_geometry_and_evidence(changes: dict) -> None:
    with pytest.raises(ValueError):
        VideoVisual(source_kind=SourceKind.USER_ASSET, asset_id=uuid4(), authorization_reference="rights", **changes)


@pytest.mark.parametrize("kind", [SourceKind.TYPOGRAPHY, SourceKind.SCREENSHOT, SourceKind.CHART])
def test_bottom_crop_cannot_be_silently_ignored_on_non_video(kind: SourceKind) -> None:
    with pytest.raises(ValueError, match="local video media"):
        VideoVisual(source_kind=kind, asset_id=uuid4(), authorization_reference="rights",
                    vertical_reframe_mode="bottom_crop_contain", source_bottom_crop_ratio=.15,
                    vertical_reframe_evidence_reference="source-sheet")


def test_bottom_crop_panel_cannot_bypass_talking_run_clip_identity(tmp_path: Path, admit_talking_run) -> None:
    db, original, _, spec = _setup(tmp_path)
    runner = _Runner()
    try:
        asset = original.model_copy(update={"source_kind": SourceKind.AI_VIDEO, "metadata": {}})
        AssetRepository(db).update(asset)
        full = ClipRepository(db).create(Clip(asset_id=asset.id, start_ms=0, end_ms=asset.duration_ms, asset_duration_ms=asset.duration_ms))
        asset, _, _ = admit_talking_run(db, spec.project_id, asset, full, "A local caption", transcript_end_ms=1_000)
        sub = ClipRepository(db).create(Clip(asset_id=asset.id, start_ms=0, end_ms=1_001, asset_duration_ms=asset.duration_ms))
        visual = VideoVisual.model_validate({**spec.scenes[0].visual.model_dump(),
            "source_kind": "ai_video", "asset_id": asset.id, "clip_id": sub.id,
            "clip_start_ms": 0, "clip_end_ms": 1_001, "vertical_reframe_mode": "bottom_crop_contain",
            "source_bottom_crop_ratio": .15, "vertical_reframe_evidence_reference": "sampled-source-sheet"})
        scene = spec.scenes[0].model_copy(update={"visual": visual, "portrait_presentation": PortraitPresentation.PORTRAIT_PANEL})
        with pytest.raises(UnauthorizedVisualError, match="talking_run_clip_mismatch"):
            RemotionRenderer(AssetRepository(db), ClipRepository(db), renderer_dir=_renderer_project(tmp_path / "renderer"), runner=runner).render(spec.model_copy(update={"scenes": [scene]}), tmp_path / "blocked.mp4")
        assert runner.calls == []
    finally:
        db.close()


@pytest.mark.parametrize("crop_failure", [False, True])
def test_bottom_crop_stages_separate_complete_width_media_and_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crop_failure: bool) -> None:
    db, asset, _, spec = _setup(tmp_path)
    source = Path(asset.source_file)
    original = source.read_bytes()

    class CropRunner(_Runner):
        crop_calls: list[list[str]] = []

        def run(self, argv, *, cwd, timeout_seconds):
            if "-vf" in argv:
                self.crop_calls.append(argv)
                if not crop_failure:
                    Path(argv[-1]).write_bytes(b"cropped-full-width")
                return subprocess.CompletedProcess(argv, 1 if crop_failure else 0, b"", b"")
            return super().run(argv, cwd=cwd, timeout_seconds=timeout_seconds)

    runner = CropRunner()
    monkeypatch.setenv("CONTENT_OS_FFMPEG", "configured-ffmpeg")
    cropped = spec.scenes[0].model_copy(update={"visual": VideoVisual.model_validate({
        **spec.scenes[0].visual.model_dump(), "vertical_reframe_mode": "bottom_crop_contain",
        "source_bottom_crop_ratio": .15, "vertical_reframe_evidence_reference": "exact-source-sheet",
    })})
    # Same original with two treatments must not share the transformed bytes.
    plain = spec.scenes[0].model_copy(update={"scene_id": "plain", "start_frame": 30})
    spec = spec.model_copy(update={"scenes": [cropped, plain]})
    try:
        renderer_root = _renderer_project(tmp_path / "renderer")
        renderer = RemotionRenderer(AssetRepository(db), ClipRepository(db), renderer_dir=renderer_root, runner=runner)
        if crop_failure:
            with pytest.raises(RenderProcessError):
                renderer.render(spec, tmp_path / "output.mp4")
            assert not runner.calls
        else:
            renderer.render(spec, tmp_path / "output.mp4")
            sources = runner.calls[0][3]["sceneSources"]
            assert sources[cropped.scene_id]["src"] != sources[plain.scene_id]["src"]
            assert sources[cropped.scene_id]["sourceBottomCropRatio"] == 0
            assert runner.staged_bytes[sources[cropped.scene_id]["src"]] == b"cropped-full-width"
            assert runner.staged_bytes[sources[plain.scene_id]["src"]] == original
            assert sources[cropped.scene_id]["trimBefore"] == sources[plain.scene_id]["trimBefore"]
            assert sources[cropped.scene_id]["trimAfter"] == sources[plain.scene_id]["trimAfter"]
        assert len(runner.crop_calls) == 1
        argv = runner.crop_calls[0]
        assert argv[0] == "configured-ffmpeg"
        assert argv[argv.index("-vf") + 1] == "crop=iw:trunc(ih*(1-0.15)/2)*2:0:0"
        assert argv[argv.index("-i") + 1] == str(source)
        assert argv[argv.index("-c:a") + 1] == "copy"
        assert source.read_bytes() == original
        assert not list((renderer_root / "public" / "content-os-renders").iterdir())
    finally:
        db.close()


class _Runner:
    def __init__(self, *, returncode: int = 0) -> None:
        self.returncode = returncode
        self.calls: list[tuple[list[str], Path, float, dict[str, object]]] = []
        self.staged_bytes: dict[str, bytes] = {}

    def run(self, argv: list[str], *, cwd: Path, timeout_seconds: float) -> subprocess.CompletedProcess[bytes]:
        if "-map" in argv:
            self.calls.append((argv, cwd, timeout_seconds, {}))
            if self.returncode == 0:
                Path(argv[-1]).write_bytes(b"minimal-muxed-mp4")
            return subprocess.CompletedProcess(argv, self.returncode, b"", b"")
        props_path = Path(next(value.split("=", 1)[1] for value in argv if value.startswith("--props=")))
        props = json.loads(props_path.read_text(encoding="utf-8"))
        self.calls.append((argv, cwd, timeout_seconds, props))
        for scene in props["sceneSources"].values():
            if scene["kind"] in {"video", "image"}:
                self.staged_bytes[scene["src"]] = (cwd / "public" / scene["src"]).read_bytes()
            if "narrationSrc" in scene:
                self.staged_bytes[scene["narrationSrc"]] = (cwd / "public" / scene["narrationSrc"]).read_bytes()
        if props.get("masterNarration"):
            master = props["masterNarration"]
            self.staged_bytes[master["src"]] = (cwd / "public" / master["src"]).read_bytes()
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


def test_remotion_portable_media_uses_configured_root_not_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db, asset, _, spec = _setup(tmp_path)
    runner = _Runner()
    try:
        db.data_root = tmp_path
        AssetRepository(db).update(asset.model_copy(update={"source_file": Path(asset.source_file).name}))
        renderer_root = _renderer_project(tmp_path / "renderer")
        monkeypatch.chdir(renderer_root)
        renderer = RemotionRenderer(AssetRepository(db), ClipRepository(db), renderer_dir=renderer_root, runner=runner)
        renderer.render(spec, tmp_path / "portable.mp4")
        assert b"original-media-bytes" in runner.staged_bytes.values()
        AssetRepository(db).update(asset.model_copy(update={"source_file": "../outside.mp4"}))
        with pytest.raises(LocalResourceError):
            renderer.render(spec, tmp_path / "escape.mp4")
        assert len(runner.calls) == 1
    finally:
        db.close()


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
        assert source_props["portraitPresentation"] == "full_canvas"
        assert runner.staged_bytes[source_props["src"]] == source.read_bytes()
        assert hashlib.sha256(source.read_bytes()).hexdigest() == before
        assert list((renderer_root / "public" / "content-os-renders").iterdir()) == []
    finally:
        db.close()


def test_imported_stock_uses_existing_renderer_and_exports_exact_source_credit(tmp_path):
    db, asset, _, spec = _setup(tmp_path)
    try:
        credit = "Galaxy — NASA · CC BY 4.0 · https://commons.wikimedia.org/wiki/File:Galaxy.webm"
        stock = asset.model_copy(update={"source_kind": SourceKind.STOCK,
            "metadata": {"external_sources": [{"candidate": {"attribution": credit}}]}})
        AssetRepository(db).update(stock)
        visual = spec.scenes[0].visual.model_copy(update={"source_kind": SourceKind.STOCK})
        scene = spec.scenes[0].model_copy(update={"visual": visual})
        runner = _Runner()
        output = RemotionRenderer(AssetRepository(db), ClipRepository(db),
            renderer_dir=_renderer_project(tmp_path / "renderer"), runner=runner).render(
                spec.model_copy(update={"scenes": [scene]}), tmp_path / "stock.mp4")
        assert len(runner.calls) == 1
        assert Path(asset.source_file).read_bytes() == b"original-media-bytes"
        credits = output.with_suffix(".credits.txt").read_text(encoding="utf-8")
        assert credit in credits and "剪辑" in credits
    finally:
        db.close()


def test_remotion_adapter_accepts_authorized_subinterval_inside_clip(tmp_path: Path) -> None:
    db, asset, clip, spec = _setup(tmp_path)
    runner = _Runner()
    try:
        renderer_root = _renderer_project(tmp_path / "renderer")
        sub_visual = spec.scenes[0].visual.model_copy(update={"clip_start_ms": 1_200, "clip_end_ms": 2_000})
        sub_scene = spec.scenes[0].model_copy(update={"duration_frames": 18, "visual": sub_visual})
        sub_spec = spec.model_copy(update={"scenes": [sub_scene]})
        RemotionRenderer(AssetRepository(db), ClipRepository(db), renderer_dir=renderer_root, runner=runner).render(
            sub_spec, tmp_path / "output" / "subinterval.mp4"
        )
        source_props = runner.calls[0][3]["sceneSources"]["scene_01"]
        assert source_props["trimBefore"] == 36
        assert source_props["trimAfter"] == 60
    finally:
        db.close()


def test_remotion_adapter_allows_frame_rate_conversion_within_authorized_duration(tmp_path: Path) -> None:
    db, asset, clip, spec = _setup(tmp_path)
    runner = _Runner()
    try:
        converted = asset.model_copy(update={"id": uuid4(), "content_hash": "b" * 64, "fps": RationalFps(numerator=25, denominator=1), "duration_ms": 4_000})
        AssetRepository(db).create(converted)
        updated_clip = Clip(asset_id=converted.id, start_ms=0, end_ms=3_480, asset_duration_ms=4_000)
        ClipRepository(db).create(updated_clip)
        visual = spec.scenes[0].visual.model_copy(update={"asset_id": converted.id, "clip_id": updated_clip.id, "clip_start_ms": 0, "clip_end_ms": 3_480, "source_duration_ms": 4_000})
        scene = spec.scenes[0].model_copy(update={"duration_frames": 105, "visual": visual})
        RemotionRenderer(AssetRepository(db), ClipRepository(db), renderer_dir=_renderer_project(tmp_path / "renderer"), runner=runner).render(
            spec.model_copy(update={"scenes": [scene]}), tmp_path / "converted.mp4"
        )
        assert runner.calls
    finally:
        db.close()


def test_remotion_adapter_rejects_a_human_rejected_talking_asset(tmp_path: Path) -> None:
    db, asset, _, spec = _setup(tmp_path)
    try:
        rejected = asset.model_copy(update={
            "id": uuid4(), "content_hash": "c" * 64, "source_kind": SourceKind.AI_VIDEO,
            "metadata": {"talking_generation": {"qa_state": "verified", "human_review_state": "rejected"}},
        })
        AssetRepository(db).create(rejected)
        clip = Clip(asset_id=rejected.id, start_ms=0, end_ms=1_001, asset_duration_ms=rejected.duration_ms)
        ClipRepository(db).create(clip)
        visual = spec.scenes[0].visual.model_copy(update={
            "source_kind": SourceKind.AI_VIDEO, "asset_id": rejected.id, "clip_id": clip.id,
            "clip_start_ms": 0, "clip_end_ms": 1_001,
        })
        blocked = spec.model_copy(update={"scenes": [spec.scenes[0].model_copy(update={"visual": visual})]})
        with pytest.raises(UnauthorizedVisualError, match="generated_talking_human_review_not_approved"):
            RemotionRenderer(AssetRepository(db), ClipRepository(db), renderer_dir=_renderer_project(tmp_path / "renderer"), runner=_Runner()).render(
                blocked, tmp_path / "blocked.mp4"
            )
    finally:
        db.close()


def test_remotion_talking_rechecks_admission_project_and_copy(tmp_path: Path, admit_talking_run) -> None:
    db, original, _, spec = _setup(tmp_path)
    runner = _Runner()
    try:
        asset = original.model_copy(update={
            "id": uuid4(), "source_kind": SourceKind.AI_VIDEO, "content_hash": "b" * 64,
            "duration_ms": 3_003, "metadata": {"talking_generation": {
                "qa_state": "verified", "human_review_state": "approved",
            }},
        })
        AssetRepository(db).create(asset)
        clip = ClipRepository(db).create(Clip(asset_id=asset.id, start_ms=0, end_ms=3_003, asset_duration_ms=3_003))
        visual = spec.scenes[0].visual.model_copy(update={
            "source_kind": SourceKind.AI_VIDEO, "asset_id": asset.id, "clip_id": clip.id,
            "clip_start_ms": 0, "clip_end_ms": 3_003, "source_duration_ms": 3_003,
        })
        talking_spec = spec.model_copy(update={
            "scenes": [spec.scenes[0].model_copy(update={"visual": visual})],
        })
        renderer = RemotionRenderer(
            AssetRepository(db), ClipRepository(db), renderer_dir=_renderer_project(tmp_path / "renderer"), runner=runner,
        )
        with pytest.raises(UnauthorizedVisualError, match="generated_talking_run_admission_required"):
            renderer.render(talking_spec, tmp_path / "unadmitted.mp4")
        stored, _, _ = admit_talking_run(
            db, spec.project_id, asset, clip, "A local caption", transcript_end_ms=1_000,
        )
        renderer.render(talking_spec, tmp_path / "admitted.mp4")
        assert len(runner.calls) == 1
        cut_scene = talking_spec.scenes[0].model_copy(update={"duration_frames": 15})
        with pytest.raises(UnauthorizedVisualError, match="talking_run_speech_would_be_cut"):
            renderer.render(talking_spec.model_copy(update={"scenes": [cut_scene]}), tmp_path / "cut-speech.mp4")
        wrong_copy = talking_spec.model_copy(update={
            "scenes": [talking_spec.scenes[0].model_copy(update={"caption": "Other copy"})],
        })
        with pytest.raises(UnauthorizedVisualError, match="talking_run_copy_mismatch"):
            renderer.render(wrong_copy, tmp_path / "wrong-copy.mp4")
        with pytest.raises(UnauthorizedVisualError, match="talking_run_project_mismatch"):
            renderer.render(talking_spec.model_copy(update={"project_id": uuid4()}), tmp_path / "wrong-project.mp4")
        pending = dict(stored.metadata)
        pending["talking_run"] = {**pending["talking_run"], "admission_state": "pending"}
        AssetRepository(db).update(stored.model_copy(update={"metadata": pending}))
        with pytest.raises(UnauthorizedVisualError, match="talking_run_not_admitted"):
            renderer.render(talking_spec, tmp_path / "pending.mp4")
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
        horizontal = spec.model_copy(update={"format": ProjectFormat.SQUARE, "width": 1080, "height": 1080})
        with pytest.raises(RenderInputError, match="9:16"):
            renderer.render(horizontal, tmp_path / "bad.mp4")
        assert runner.calls == []
    finally:
        db.close()


def test_remotion_adapter_preserves_footage_and_stages_horizontal_canvas(tmp_path):
    db,asset,clip,spec=_setup(tmp_path)
    runner=_Runner()
    try:
        AssetRepository(db).update(asset.model_copy(update={"width":1920,"height":1080}))
        horizontal=spec.model_copy(update={"format":ProjectFormat.HORIZONTAL,"width":1920,"height":1080})
        RemotionRenderer(AssetRepository(db),ClipRepository(db),renderer_dir=_renderer_project(tmp_path/"renderer"),runner=runner).render(horizontal,tmp_path/"horizontal.mp4")
        props=runner.calls[0][3]
        assert (props["videoSpec"]["width"],props["videoSpec"]["height"])==(1920,1080)
        visual=props["sceneSources"]["scene_01"]
        assert visual["verticalReframeMode"]=="contain" and visual["portraitPresentation"]=="full_canvas"
        assert runner.staged_bytes[visual["src"]]==Path(asset.source_file).read_bytes()
    finally:
        db.close()


def test_remotion_adapter_renders_authorized_footage_in_a_portrait_panel_without_cropping(tmp_path: Path) -> None:
    db, _, _, spec = _setup(tmp_path)
    runner = _Runner()
    try:
        asset = AssetRepository(db).get(spec.scenes[0].visual.asset_id)
        asset=AssetRepository(db).update(asset.model_copy(update={"width": 1920, "height": 1080}))
        panel_scene = spec.scenes[0].model_copy(update={"portrait_presentation": PortraitPresentation.PORTRAIT_PANEL})
        panel = spec.model_copy(update={"scenes": [panel_scene]})
        RemotionRenderer(AssetRepository(db), ClipRepository(db), renderer_dir=_renderer_project(tmp_path / "renderer"), runner=runner).render(
            panel, tmp_path / "panel.mp4"
        )
        props = runner.calls[0][3]["sceneSources"]["scene_01"]
        assert props["portraitPresentation"] == "portrait_panel"
        assert props["verticalReframeMode"] == "contain"
        assert runner.staged_bytes[props["src"]] == Path(asset.source_file).read_bytes()

        asset=AssetRepository(db).update(asset.model_copy(update={"metadata":{**asset.metadata,'visual_analysis':{
            str(spec.scenes[0].visual.clip_id):dict(source_content_hash=asset.content_hash,
                start_ms=spec.scenes[0].visual.clip_start_ms,end_ms=spec.scenes[0].visual.clip_end_ms,
                comparison_layout='side_by_side_equal_halves',subtitle_regions=[],evidence_reference='synthetic-geometry',
                coverage='full_interval_continuous',covered_start_ms=spec.scenes[0].visual.clip_start_ms,
                covered_end_ms=spec.scenes[0].visual.clip_end_ms)}}}))
        stacked_scene = panel_scene.model_copy(update={
            "portrait_presentation": PortraitPresentation.STACKED_COMPARISON,
            "subtitle_treatment": SubtitleTreatment.TIMED_CAPTIONS,
        })
        stacked_runner = _Runner()
        RemotionRenderer(AssetRepository(db), ClipRepository(db), renderer_dir=_renderer_project(tmp_path / "stacked-renderer"), runner=stacked_runner).render(
            panel.model_copy(update={"scenes": [stacked_scene]}), tmp_path / "stacked.mp4"
        )
        stacked_props = stacked_runner.calls[0][3]["sceneSources"]["scene_01"]
        assert stacked_props["portraitPresentation"] == "stacked_comparison"
        assert stacked_props["verticalReframeMode"] == "contain"
        assert stacked_runner.staged_bytes[stacked_props["src"]] == Path(asset.source_file).read_bytes()
        assert stacked_props['sourceAspectRatio']==16/9
        facts=asset.metadata['visual_analysis'][str(spec.scenes[0].visual.clip_id)]
        facts['subtitle_regions']=[[.3,.88,.4,.08]]
        AssetRepository(db).update(asset)
        from app.assembly.edit_plan import comparison_clearance
        ratio,reference=comparison_clearance(asset,ClipRepository(db).get(spec.scenes[0].visual.clip_id))
        cropped=stacked_scene.model_copy(update={'visual':VideoVisual.model_validate({**stacked_scene.visual.model_dump(),
            'vertical_reframe_mode':'bottom_crop_contain','source_bottom_crop_ratio':ratio,'vertical_reframe_evidence_reference':reference})})
        cropped_runner=_Runner()
        RemotionRenderer(AssetRepository(db),ClipRepository(db),renderer_dir=_renderer_project(tmp_path/'cropped-stack'),runner=cropped_runner).render(
            panel.model_copy(update={'scenes':[cropped]}),tmp_path/'cropped-stack.mp4')
        assert len(cropped_runner.calls)==1  # CSS clipping, no whole-asset FFmpeg transcode.
        cropped_props=cropped_runner.calls[0][3]['sceneSources']['scene_01']
        assert cropped_props['sourceBottomCropRatio']==ratio and cropped_props['sourceAspectRatio']==16/9
        facts['coverage']='sampled_keyframe';AssetRepository(db).update(asset)
        rejected_runner=_Runner()
        with pytest.raises(RenderInputError,match='full_interval_clearance_required'):
            RemotionRenderer(AssetRepository(db),ClipRepository(db),renderer_dir=_renderer_project(tmp_path/'sampled-stack'),
                runner=rejected_runner).render(panel.model_copy(update={'scenes':[cropped]}),tmp_path/'sampled-stack.mp4')
        assert rejected_runner.calls==[]
        facts['coverage']='full_interval_continuous'
        facts['subtitle_regions']=[];AssetRepository(db).update(asset)
        AssetRepository(db).update(asset.model_copy(update={"width": 1280, "height": 800}))
        with pytest.raises(RenderInputError, match="requires a 16:9 source"):
            RemotionRenderer(AssetRepository(db), ClipRepository(db), renderer_dir=_renderer_project(tmp_path / "non-16-9-renderer"), runner=_Runner()).render(
                panel.model_copy(update={"scenes": [stacked_scene]}), tmp_path / "non-16-9.mp4"
            )

        unsafe_scene = panel_scene.model_copy(update={"visual": panel_scene.visual.model_copy(update={
            "vertical_reframe_mode": "bottom_crop_contain",
            "source_bottom_crop_ratio": 0.1,
            "vertical_reframe_evidence_reference": "old-sampled-sheet",
        })})
        with pytest.raises(RenderInputError, match="portrait_panel"):
            RemotionRenderer(AssetRepository(db), ClipRepository(db), renderer_dir=_renderer_project(tmp_path / "unsafe-renderer"), runner=_Runner()).render(
                panel.model_copy(update={"scenes": [unsafe_scene]}), tmp_path / "unsafe.mp4"
            )
    finally:
        db.close()


def test_remotion_comparison_clearance_is_bound_to_the_played_scene_interval(tmp_path: Path) -> None:
    db, asset, clip, spec = _setup(tmp_path)
    runner = _Runner()
    try:
        asset = AssetRepository(db).update(asset.model_copy(update={"width": 1280, "height": 720}))
        full_clip = ClipRepository(db).create(Clip(
            asset_id=asset.id, start_ms=clip.start_ms, end_ms=asset.duration_ms,
            asset_duration_ms=asset.duration_ms,
        ))
        from app.assembly.edit_plan import comparison_clearance, source_frame_interval_ms
        playback = source_frame_interval_ms(clip.start_ms, spec.scenes[0].duration_frames, spec.fps, full_clip.end_ms)
        assert playback == (1001, 2002)
        asset = AssetRepository(db).update(asset.model_copy(update={"metadata": {
            **asset.metadata, "visual_analysis": {str(full_clip.id): {
                "source_content_hash": asset.content_hash,
                "clip_interval": {"start_ms": playback[0], "end_ms": playback[1]},
                "comparison_layout": "side_by_side_equal_halves", "subtitle_regions": [[.3, .9, .4, .08]],
                "evidence_reference": "synthetic-used-interval", "coverage": "full_interval_continuous",
                "covered_start_ms": playback[0], "covered_end_ms": playback[1],
            }},
        }}))
        with pytest.raises(ValueError, match="comparison_source_layout_or_subtitle_geometry_unknown"):
            comparison_clearance(asset, full_clip)
        ratio, reference = comparison_clearance(asset, full_clip, playback)
        visual = VideoVisual.model_validate({
            **spec.scenes[0].visual.model_dump(), "clip_id": full_clip.id,
            "clip_start_ms": full_clip.start_ms, "clip_end_ms": full_clip.end_ms,
            "vertical_reframe_mode": "bottom_crop_contain", "source_bottom_crop_ratio": ratio,
            "vertical_reframe_evidence_reference": reference,
        })
        scene = spec.scenes[0].model_copy(update={
            "visual": visual, "portrait_presentation": PortraitPresentation.STACKED_COMPARISON,
            "subtitle_treatment": SubtitleTreatment.TIMED_CAPTIONS,
        })
        renderer = RemotionRenderer(AssetRepository(db), ClipRepository(db),
            renderer_dir=_renderer_project(tmp_path / "used-interval-renderer"), runner=runner)
        renderer.render(spec.model_copy(update={"scenes": [scene]}), tmp_path / "used-interval.mp4")
        props = runner.calls[0][3]["sceneSources"][scene.scene_id]
        assert props["sourceBottomCropRatio"] == ratio

        facts = asset.metadata["visual_analysis"][str(full_clip.id)]
        facts["coverage"] = "sampled_keyframe"
        AssetRepository(db).update(asset)
        rejected = _Runner()
        with pytest.raises(RenderInputError, match="full_interval_clearance_required"):
            RemotionRenderer(AssetRepository(db), ClipRepository(db),
                renderer_dir=_renderer_project(tmp_path / "sampled-used-interval-renderer"), runner=rejected).render(
                    spec.model_copy(update={"scenes": [scene]}), tmp_path / "sampled-used-interval.mp4")
        assert rejected.calls == []
    finally:
        db.close()


def test_remotion_comparison_uses_owner_review_bound_to_asset_clip_and_crop(tmp_path: Path) -> None:
    db, asset, clip, spec = _setup(tmp_path)
    runner = _Runner()
    try:
        asset = AssetRepository(db).update(asset.model_copy(update={"width": 1280, "height": 720}))
        full_clip = ClipRepository(db).create(Clip(
            asset_id=asset.id, start_ms=clip.start_ms, end_ms=asset.duration_ms,
            asset_duration_ms=asset.duration_ms,
        ))
        from app.assembly.edit_plan import comparison_clearance, source_frame_interval_ms
        playback = source_frame_interval_ms(clip.start_ms, spec.scenes[0].duration_frames, spec.fps, full_clip.end_ms)
        assert playback == (1001, 2002)
        review = {
            "source_asset_id": str(asset.id),
            "source_clip_id": str(full_clip.id),
            "source_content_hash": asset.content_hash,
            "clip_interval": {"start_ms": playback[0], "end_ms": playback[1]},
            "covered_start_ms": playback[0], "covered_end_ms": playback[1],
            "coverage": "full_interval_continuous",
            "method": "human_full_interval_review", "decision": "approved",
            "reviewer_reference": "owner-conversation:comparison-2-pass",
            "comparison_layout": "side_by_side_equal_halves",
            "crop_window": {"x": 0, "y": 0, "width": 1280, "height": 650},
            "source_subtitle_clearance_verified": True,
            "evidence_reference": "evaluation-evidence/comparison-2-used-interval.mp4",
            "evidence_sha256": "f" * 64,
        }
        asset = AssetRepository(db).update(asset.model_copy(update={
            "metadata": {**asset.metadata, "source_presentation_reviews": {str(full_clip.id): review}},
        }))
        ratio, reference = comparison_clearance(asset, full_clip, playback)
        assert ratio == pytest.approx(70 / 720)
        assert reference == review["evidence_reference"]
        visual = VideoVisual.model_validate({
            **spec.scenes[0].visual.model_dump(), "clip_id": full_clip.id,
            "clip_start_ms": full_clip.start_ms, "clip_end_ms": full_clip.end_ms,
            "vertical_reframe_mode": "bottom_crop_contain", "source_bottom_crop_ratio": ratio,
            "vertical_reframe_evidence_reference": reference,
        })
        scene = spec.scenes[0].model_copy(update={
            "visual": visual, "portrait_presentation": PortraitPresentation.STACKED_COMPARISON,
            "subtitle_treatment": SubtitleTreatment.TIMED_CAPTIONS,
        })
        renderer = RemotionRenderer(AssetRepository(db), ClipRepository(db),
            renderer_dir=_renderer_project(tmp_path / "human-review-renderer"), runner=runner)
        renderer.render(spec.model_copy(update={"scenes": [scene]}), tmp_path / "human-review.mp4")
        props = runner.calls[0][3]["sceneSources"][scene.scene_id]
        assert props["sourceBottomCropRatio"] == pytest.approx(70 / 720)

        review["source_content_hash"] = "0" * 64
        stale_asset = AssetRepository(db).update(asset.model_copy(update={
            "metadata": {**asset.metadata, "source_presentation_reviews": {str(full_clip.id): review}},
        }))
        rejected = _Runner()
        with pytest.raises(RenderInputError, match="comparison_source_layout_or_subtitle_geometry_unknown"):
            RemotionRenderer(AssetRepository(db), ClipRepository(db),
                renderer_dir=_renderer_project(tmp_path / "stale-human-review-renderer"), runner=rejected).render(
                    spec.model_copy(update={"scenes": [scene]}), tmp_path / "stale-human-review.mp4")
        assert rejected.calls == []
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
        assert raised.value.render_stage == "remotion_video"
        assert raised.value.stderr == b"private renderer stderr"
        assert list((root / "public" / "content-os-renders").iterdir()) == []
        with pytest.raises(RenderInputError, match=".mp4"):
            RemotionRenderer(AssetRepository(db), ClipRepository(db), renderer_dir=root, runner=runner).render(spec, tmp_path / "bad.avi")
    finally:
        db.close()


def test_staging_failure_keeps_stage_without_launching_subprocess(tmp_path: Path, monkeypatch) -> None:
    db, _, _, spec = _setup(tmp_path)
    runner = _Runner()
    def denied_tempfile(**kwargs):
        raise PermissionError("props directory denied")
    monkeypatch.setattr("app.renderer.remotion.tempfile.NamedTemporaryFile", denied_tempfile)
    try:
        renderer = RemotionRenderer(AssetRepository(db), ClipRepository(db),
            renderer_dir=_renderer_project(tmp_path / "renderer"), runner=runner)
        with pytest.raises(PermissionError) as raised:
            renderer.render(spec, tmp_path / "failed.mp4")
        assert raised.value.render_stage == "input_staging"
        assert not runner.calls
    finally:
        db.close()


def test_render_failure_diagnostic_redacts_private_data_and_keeps_first_failure(tmp_path: Path, monkeypatch) -> None:
    from app.domain.models import Job, JobType
    from app.jobs.handlers import _write_render_failure_diagnostic
    job = Job(type=JobType.RENDER, attempt=1, created_at=NOW, updated_at=NOW, idempotency_key="diagnostic")
    monkeypatch.setenv("CONTENT_OS_DIAGNOSTIC_TEST_TOKEN", "environment-secret")
    error = RenderProcessError(9, stderr=(
        "environment-secret\nAuthorization: Bearer other-token\napi_key=unconfigured-secret\n"
        "C:\\Users\\Owner\\private clip.mp4\n/home/owner/private.wav\nhttps://user:pass@private/video\nmux failed"))
    error.render_stage = "ffmpeg_audio_mux"
    assert _write_render_failure_diagnostic(job, tmp_path, "renderer", error)
    path = tmp_path / "render-diagnostics" / str(job.id) / "attempt-1.json"
    raw = path.read_text(encoding="utf-8")
    report = json.loads(raw)
    assert report["stage"] == "ffmpeg_audio_mux"
    assert report["exception_type"] == "RenderProcessError" and report["returncode"] == 9
    for private in ("environment-secret", "other-token", "unconfigured-secret", "C:\\Users", "/home/owner", "user:pass"):
        assert private not in raw
    assert "mux failed" in report["error_summary"]
    assert _write_render_failure_diagnostic(job, tmp_path, "dispatch", ValueError("later failure"))
    assert path.read_text(encoding="utf-8") == raw
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("fixture", encoding="utf-8")
    assert not _write_render_failure_diagnostic(job, blocked, "dispatch", error)


def test_remotion_adapter_renders_typography_without_staging_media(tmp_path: Path) -> None:
    db, _, _, spec = _setup(tmp_path)
    runner = _Runner()
    try:
        root = _renderer_project(tmp_path / "renderer")
        typography_visual = VideoVisual(source_kind=SourceKind.TYPOGRAPHY, authorization_reference="local-typography")
        typography_spec = spec.model_copy(update={
            "scenes": [spec.scenes[0].model_copy(update={"visual": typography_visual, "caption": "Use the fallback"})],
        })
        output = tmp_path / "output" / "typography.mp4"
        RemotionRenderer(AssetRepository(db), ClipRepository(db), renderer_dir=root, runner=runner).render(typography_spec, output)
        source_props = runner.calls[0][3]["sceneSources"]["scene_01"]
        assert source_props["kind"] == "typography" and source_props["text"] == "Use the fallback"
        assert source_props["graphicTreatment"] == "key_point"
        assert source_props["styleTokens"]["name"] == "content-os-dark"
        assert runner.staged_bytes == {}
    finally:
        db.close()


def test_remotion_props_keep_all_card_points_and_line_breaks(tmp_path: Path) -> None:
    from app.domain.models import GraphicTreatment, EditVisualRole
    db, _, _, spec = _setup(tmp_path)
    runner = _Runner()
    points = "结论\n条件\n证据\n限制"
    try:
        root = _renderer_project(tmp_path / "renderer")
        entry = spec.scenes[0].model_copy(update={
            "visual": VideoVisual(source_kind=SourceKind.TYPOGRAPHY, authorization_reference="local-typography"),
            "visual_role": EditVisualRole.GRAPHIC, "graphic_treatment": GraphicTreatment.KEY_POINT,
            "graphic_text": points,
        })
        RemotionRenderer(AssetRepository(db), ClipRepository(db), renderer_dir=root, runner=runner).render(
            spec.model_copy(update={"scenes": [entry]}), tmp_path / "output" / "points.mp4")
        props = runner.calls[0][3]["sceneSources"]["scene_01"]
        assert props["text"] == points and props["graphicTreatment"] == "key_point"
    finally:
        db.close()


def test_remotion_adapter_stages_imported_image_visual(tmp_path: Path) -> None:
    db, _, _, spec = _setup(tmp_path)
    image_source = tmp_path / "screen.png"
    image_source.write_bytes(b"png-bytes")
    image = ImageAsset(
        source_kind=SourceKind.SCREENSHOT,
        source_file=str(image_source),
        content_hash="c" * 64,
        width=800,
        height=600,
        authorization_reference="creator-screen",
        imported_at=NOW,
    )
    ImageAssetRepository(db).create(image)
    runner = _Runner()
    try:
        root = _renderer_project(tmp_path / "renderer")
        image_visual = VideoVisual(source_kind=SourceKind.SCREENSHOT, authorization_reference="creator-screen", asset_id=image.id)
        image_spec = spec.model_copy(update={
            "scenes": [spec.scenes[0].model_copy(update={"visual": image_visual, "caption": "Screenshot fallback"})],
        })
        output = tmp_path / "output" / "image.mp4"
        RemotionRenderer(AssetRepository(db), ClipRepository(db), images=ImageAssetRepository(db), renderer_dir=root, runner=runner).render(image_spec, output)
        source_props = runner.calls[0][3]["sceneSources"]["scene_01"]
        assert source_props["kind"] == "image"
        assert runner.staged_bytes[source_props["src"]] == image_source.read_bytes()
    finally:
        db.close()


def test_remotion_adapter_mixes_authorized_narration_audio(tmp_path: Path) -> None:
    db, _, _, spec = _setup(tmp_path)
    audio_source = tmp_path / "voice.wav"
    audio_source.write_bytes(b"voice-bytes")
    audio = AudioAsset(
        source_kind=SourceKind.USER_ASSET,
        source_file=str(audio_source),
        content_hash="d" * 64,
        duration_ms=2_000,
        sample_rate=48_000,
        channels=1,
        authorization_reference="creator-voice",
        imported_at=NOW,
    )
    AudioAssetRepository(db).create(audio)
    runner = _Runner()
    try:
        root = _renderer_project(tmp_path / "renderer")
        narrated_scene = spec.scenes[0].model_copy(update={"narration_asset_id": audio.id, "caption": "Narrated scene"})
        narrated_spec = spec.model_copy(update={"scenes": [narrated_scene]})
        output = tmp_path / "output" / "narrated.mp4"
        RemotionRenderer(AssetRepository(db), ClipRepository(db), audios=AudioAssetRepository(db), renderer_dir=root, runner=runner).render(narrated_spec, output)
        props = runner.calls[0][3]
        source_props = props["sceneSources"]["scene_01"]
        assert props["audioMode"] == "narration"
        assert source_props["narrationStartFrame"] == 0
        assert runner.staged_bytes[source_props["narrationSrc"]] == audio_source.read_bytes()
    finally:
        db.close()


@pytest.mark.parametrize("mux_returncode", [0, 9])
def test_remotion_adapter_encodes_master_once_without_intermediate_aac_or_source_audio(tmp_path: Path, mux_returncode: int) -> None:
    db, _, _, spec = _setup(tmp_path)
    audio_source = tmp_path / "full-voice.wav"
    audio_source.write_bytes(b"master-voice-bytes")
    audio = AudioAsset(
        source_kind=SourceKind.USER_ASSET,
        source_file=str(audio_source),
        content_hash="f" * 64,
        duration_ms=2_000,
        sample_rate=48_000,
        channels=1,
        authorization_reference="creator-voice",
        imported_at=NOW,
        transcript_segments=[
            TranscriptSegment(start_ms=0, end_ms=1_001, text="First timed line"),
            TranscriptSegment(start_ms=1_001, end_ms=2_000, text="Second timed line"),
        ],
        transcript_source="provider-alignment:render-test",
    )
    AudioAssetRepository(db).create(audio)
    class MuxRunner(_Runner):
        def run(self, argv, *, cwd, timeout_seconds):
            if "-map" in argv and mux_returncode:
                self.calls.append((argv, cwd, timeout_seconds, {}))
                return subprocess.CompletedProcess(argv, mux_returncode, b"", b"mux rejected input")
            return super().run(argv, cwd=cwd, timeout_seconds=timeout_seconds)

    runner = MuxRunner()
    try:
        root = _renderer_project(tmp_path / "renderer")
        first = spec.scenes[0].model_copy(update={
            "narration_asset_id": audio.id,
            "narration_start_ms": 0,
            "narration_end_ms": 1_001,
        })
        tail = VideoScene(
            scene_id="scene_02",
            start_frame=30,
            duration_frames=30,
            visual=VideoVisual(source_kind=SourceKind.TYPOGRAPHY, authorization_reference="local-typography"),
            narration_asset_id=audio.id,
            narration_start_ms=1_001,
            narration_end_ms=2_000,
            caption="Second timed line",
        )
        master_spec = spec.model_copy(update={
            "scenes": [first, tail],
            "master_narration": MasterNarration(
                audio_asset_id=audio.id,
                start_ms=0,
                end_ms=2_000,
                transcript_source="provider-alignment:render-test",
                transcript_segments=audio.transcript_segments,
            ),
        })
        renderer = RemotionRenderer(AssetRepository(db), ClipRepository(db), audios=AudioAssetRepository(db), renderer_dir=root, runner=runner)
        if mux_returncode:
            with pytest.raises(RenderProcessError) as raised:
                renderer.render(master_spec, tmp_path / "output" / "master.mp4")
            assert raised.value.render_stage == "ffmpeg_audio_mux"
            assert raised.value.returncode == mux_returncode
            assert raised.value.stderr == b"mux rejected input"
        else:
            renderer.render(master_spec, tmp_path / "output" / "master.mp4")
        props = runner.calls[0][3]
        assert props["audioMode"] == "master_narration"
        assert "masterNarration" not in props
        assert "narrationSrc" not in props["sceneSources"]["scene_01"]
        assert "--muted" in runner.calls[0][0]
        assert len(runner.calls) == 2
        mux = runner.calls[1][0]
        assert mux[mux.index("-af") + 1] == "atrim=start=0.0:end=2.0,asetpts=PTS-STARTPTS"
        assert mux[mux.index("-c:v") + 1] == "copy"
        assert mux.count(str(audio_source)) == 1
        assert audio_source.read_bytes() == b"master-voice-bytes"
    finally:
        db.close()


@pytest.mark.parametrize("start,end", [(0,4900),(4900,9340),(9340,14380),(14380,21680),(21680,26300)])
def test_contiguous_source_windows_cover_every_timeline_frame(tmp_path: Path, start: int, end: int) -> None:
    db, asset, _, spec = _setup(tmp_path)
    try:
        asset = asset.model_copy(update={"id":uuid4(),"content_hash":"d"*64,"duration_ms": 26320, "fps": RationalFps(numerator=25,denominator=1)})
        AssetRepository(db).create(asset)
        clip = ClipRepository(db).create(Clip(asset_id=asset.id,start_ms=0,end_ms=26300,asset_duration_ms=26320))
        first,last = (start*30+999)//1000,(end*30+999)//1000
        visual = spec.scenes[0].visual.model_copy(update={"asset_id":asset.id,"clip_id":clip.id,"clip_start_ms":start,"clip_end_ms":end,"source_duration_ms":26320})
        scene = spec.scenes[0].model_copy(update={"start_frame":first,"duration_frames":last-first,"visual":visual})
        renderer = RemotionRenderer(AssetRepository(db),ClipRepository(db),renderer_dir=tmp_path)
        prepared = renderer._validate_scene(scene,RationalFps(numerator=30,denominator=1),project_id=spec.project_id,master_audio_id=None)
        assert prepared.trim_before == first and prepared.trim_after == last
        assert prepared.trim_before + scene.duration_frames == prepared.trim_after
        assert (prepared.trim_after-1)*1000/30 < end
        with pytest.raises(RenderInputError, match="shorter than the requested scene"):
            renderer._validate_scene(scene.model_copy(update={"duration_frames":scene.duration_frames+1}),
                RationalFps(numerator=30,denominator=1),project_id=spec.project_id,master_audio_id=None)
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
    assert "trimBefore" in timeline and "trimAfter" in timeline and "typography" in timeline and "narrationSrc" in timeline
    assert "scene.caption" in timeline and "scene.captions" in timeline and "masterNarration" in timeline
    assert "objectFit: 'contain'" in timeline
    assert "objectFit: 'cover'" not in timeline
    assert "captionPlan.fallbackCaption && audioMode !== 'source'" in timeline
    assert "backgroundColor: '#000000'" in timeline
    assert "captionReserve" not in timeline
    assert "headline" in timeline and "key_point" in timeline and "contrast" in timeline and "GraphicCard" in timeline
    assert "portrait_panel" in timeline and "PortraitPanel" in timeline and "PanelText" in timeline
    assert "stacked_comparison" in timeline and "StackedComparisonPanel" in timeline
    assert "trimBefore={source.trimBefore}" in timeline and "trimAfter={source.trimAfter}" in timeline
