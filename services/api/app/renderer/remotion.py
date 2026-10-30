"""Safe local adapter for the minimal Remotion renderer project.

This boundary does not generate media or invoke providers. It verifies that a
VideoSpec only references authorized, persisted, local continuous Clips, then
copies those source files into a unique temporary Remotion public directory.
The original media files are never modified.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol
from urllib.parse import urlsplit
from app.media.local_paths import resolve_local_media_path
from uuid import UUID, uuid4

from app.db import AssetRepository, AudioAssetRepository, ClipRepository, ImageAssetRepository
from app.domain.models import Asset, AudioAsset, Clip, EditVisualRole, ImageAsset, PortraitPresentation, ProjectFormat, RationalFps, SourceKind, SubtitleTreatment, VerticalReframeMode, VideoScene, VideoSpec
from app.talking.admission import talking_visual_blocker


class RendererError(RuntimeError):
    pass


class RenderInputError(RendererError):
    pass


class LocalResourceError(RendererError):
    pass


class UnauthorizedVisualError(RendererError):
    pass


class RenderTimeout(RendererError):
    pass


class RenderProcessError(RendererError):
    def __init__(self, returncode: int, *, stderr: bytes | str | None = None) -> None:
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"local renderer exited with status {returncode}")


class RenderCommandRunner(Protocol):
    def run(self, argv: list[str], *, cwd: Path, timeout_seconds: float) -> subprocess.CompletedProcess[bytes]: ...


class SubprocessRenderCommandRunner:
    """Explicit argv subprocess runner; no shell or provider credentials."""

    def run(self, argv: list[str], *, cwd: Path, timeout_seconds: float) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(  # noqa: S603 - executable comes from explicit local configuration.
                argv, cwd=cwd, timeout=timeout_seconds, check=False, shell=False,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        except subprocess.TimeoutExpired as exc:
            error = RenderTimeout("local renderer timed out")
            error.stderr = exc.stderr
            raise error from exc
        except OSError as exc:
            raise RenderProcessError(-1, stderr=str(exc)) from exc


@dataclass(frozen=True)
class _PreparedVisual:
    source: Path | None
    trim_before: int
    trim_after: int
    kind: str
    narration_source: Path | None = None
    narration_start_frame: int = 0
    vertical_reframe_mode: str = "contain"
    portrait_presentation: str = "full_canvas"
    source_bottom_crop_ratio: float = 0
    source_width: int = 0
    source_height: int = 0
    graphic_treatment: str = "key_point"
    graphic_text: str | None = None
    style_tokens: dict[str, str] | None = None


class RemotionRenderer:
    """Render a validated VideoSpec to a local MP4 through ``npm exec remotion``.

    Source audio is kept only for source-led scenes. Per-scene legacy
    narration and a single master narration track both mute source audio;
    the master track is staged and mounted exactly once by the composition.
    """

    def __init__(
        self,
        assets: AssetRepository,
        clips: ClipRepository,
        *,
        images: ImageAssetRepository | None = None,
        audios: AudioAssetRepository | None = None,
        renderer_dir: str | Path,
        npm_command: str = "npm",
        timeout_seconds: float = 600.0,
        runner: RenderCommandRunner | None = None,
    ) -> None:
        directory = Path(renderer_dir).resolve()
        if not isinstance(npm_command, str) or not npm_command.strip():
            raise RenderInputError("npm command must be a non-empty string")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds <= 0
        ):
            raise RenderInputError("renderer timeout must be positive")
        self.assets = assets
        self.clips = clips
        self.images = images
        self.audios = audios
        self.data_root = Path(getattr(assets.db, "data_root", Path(assets.db.path).parent)).resolve()
        self.renderer_dir = directory
        self.npm_command = _npm_executable(npm_command.strip())
        self.timeout_seconds = float(timeout_seconds)
        self.runner = runner or SubprocessRenderCommandRunner()

    def render(self, spec: VideoSpec, output_path: str | Path, *, production_run_id: UUID | None = None) -> Path:
        return self._render(spec, output_path, production_run_id=production_run_id)

    def _render(self, spec: VideoSpec, output_path: str | Path, *, production_run_id: UUID | None = None) -> Path:
        stage = "input_validation"
        staging: Path | None = None
        props_path: Path | None = None
        try:
            if not isinstance(spec, VideoSpec):
                raise RenderInputError("renderer requires a VideoSpec contract")
            from app.media.semantic_rejections import require_spec_semantics
            require_spec_semantics(self.assets.db, spec)
            if not (spec.format is ProjectFormat.VERTICAL and spec.width * 16 == spec.height * 9
                    or spec.format is ProjectFormat.HORIZONTAL and spec.width * 9 == spec.height * 16):
                raise RenderInputError("renderer accepts 9:16 vertical or 16:9 horizontal VideoSpecs")
            output = _output_path(output_path)
            _renderer_project(self.renderer_dir)
            master_narration = self._validate_master_narration(spec, spec.fps)
            prepared = {
                scene.scene_id: self._validate_scene(
                    scene, spec.fps, project_id=spec.project_id,
                    master_audio_id=None if spec.master_narration is None else spec.master_narration.audio_asset_id,
                    validate_scene_narration=master_narration is None,
                    production_run_id=production_run_id,
                )
                for scene in spec.scenes
            }
            input_sources = {visual.source for visual in prepared.values() if visual.source is not None}
            if master_narration is not None:
                input_sources.add(master_narration)
            if output in input_sources:
                raise RenderInputError("render output must not overwrite a source media file")
            stage = "input_staging"
            output.parent.mkdir(parents=True, exist_ok=True)
            public_root = self.renderer_dir / "public" / "content-os-renders"
            public_root.mkdir(parents=True, exist_ok=True)
            run_id = uuid4().hex
            staging = public_root / run_id
            staging.mkdir()
            props = self._stage_props(spec, prepared, staging, run_id, master_narration)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".json", prefix="content-os-remotion-", delete=False) as handle:
                json.dump(props, handle, ensure_ascii=False, separators=(",", ":"))
                props_path = Path(handle.name)
            visual_output = staging / "visual.mp4" if master_narration is not None else output
            stage = "remotion_video"
            argv = [
                self.npm_command, "exec", "--", "remotion", "render", "src/index.ts", "ContentOSVideo", str(visual_output),
                f"--props={props_path}",
            ]
            if master_narration is not None:
                argv.append("--muted")
            completed = self.runner.run(argv, cwd=self.renderer_dir, timeout_seconds=self.timeout_seconds)
            if completed.returncode != 0:
                raise RenderProcessError(completed.returncode, stderr=completed.stderr)
            if master_narration is not None:
                stage = "ffmpeg_audio_mux"
                from app.runtime import resolve_local_executable

                # Encode the approved Master once, directly into MP4. Remotion's
                # intermediate AAC/concat path retains encoder priming as speech
                # delay; container-duration tolerance cannot detect that error.
                master = spec.master_narration
                audio_start, audio_end = master.start_ms/1000, master.end_ms/1000
                argv = [resolve_local_executable("ffmpeg"), "-nostdin", "-v", "error",
                    "-i", str(visual_output), "-i", str(master_narration),
                    "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
                    "-af", f"atrim=start={audio_start}:end={audio_end},asetpts=PTS-STARTPTS",
                    "-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart", str(output)]
                completed = self.runner.run(argv, cwd=self.renderer_dir, timeout_seconds=self.timeout_seconds)
                if completed.returncode != 0:
                    raise RenderProcessError(completed.returncode, stderr=completed.stderr)
            stage = "output_validation"
            if not output.is_file() or output.stat().st_size == 0:
                raise RenderProcessError(-1)
            from app.external_media import attribution_text
            source_assets = []
            for scene in spec.scenes:
                clip = self.clips.get(scene.visual.clip_id) if scene.visual.clip_id else None
                asset_id = scene.visual.asset_id or (clip.asset_id if clip else None)
                asset = self.assets.get(asset_id) if asset_id else None
                if asset is not None:
                    source_assets.append(asset)
            credits = attribution_text(source_assets)
            output.with_suffix(".credits.txt").write_text(credits or "本片未使用外部资料库素材。\n", encoding="utf-8")
            return output
        except Exception as exc:
            exc.render_stage = stage
            raise
        finally:
            if props_path is not None:
                props_path.unlink(missing_ok=True)
            # This directory is generated with a random run ID beneath the
            # fixed renderer public root; no user source path is ever removed.
            if staging is not None and staging.exists():
                shutil.rmtree(staging)

    def _validate_scene(
        self,
        scene: VideoScene,
        composition_fps: RationalFps,
        *,
        project_id: UUID,
        master_audio_id: UUID | None,
        validate_scene_narration: bool = True,
        production_run_id: UUID | None = None,
    ) -> _PreparedVisual:
        visual = scene.visual
        if scene.transition != "cut":
            raise RenderInputError("minimal renderer supports only cut transitions")
        if scene.portrait_presentation is PortraitPresentation.PORTRAIT_PANEL:
            talking_panel = (
                visual.source_kind is SourceKind.AI_VIDEO
                and visual.vertical_reframe_mode in {
                    VerticalReframeMode.CONTAIN, VerticalReframeMode.BOTTOM_CROP_CONTAIN,
                }
            )
            footage_panel = (
                visual.source_kind in {SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET, SourceKind.STOCK}
                and visual.vertical_reframe_mode is VerticalReframeMode.CONTAIN
            )
            if not (talking_panel or footage_panel):
                raise RenderInputError("portrait_panel requires complete-frame Talking or authorized footage")
        if scene.portrait_presentation is PortraitPresentation.STACKED_COMPARISON:
            if (visual.source_kind not in {SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET, SourceKind.STOCK}
                or scene.visual_role is not EditVisualRole.FOOTAGE
                or visual.vertical_reframe_mode not in {VerticalReframeMode.CONTAIN,VerticalReframeMode.BOTTOM_CROP_CONTAIN}
                or scene.subtitle_treatment is not SubtitleTreatment.TIMED_CAPTIONS):
                raise RenderInputError("stacked_comparison requires authorized 16:9 comparison footage and timed narration captions")
        narration_source, narration_start_frame = (
            self._validate_narration(scene, composition_fps) if validate_scene_narration else (None, 0)
        )
        if visual.source_kind == SourceKind.TYPOGRAPHY:
            if any(value is not None for value in (visual.asset_id, visual.clip_id, visual.clip_start_ms, visual.clip_end_ms, visual.source_duration_ms)):
                raise RenderInputError("typography visual must not reference a media Clip")
            neutral = (scene.graphic_treatment.value == "none" and not scene.graphic_text
                and not scene.caption and not scene.captions and scene.subtitle_treatment.value == "none")
            return _PreparedVisual(
                source=None, trim_before=0, trim_after=0, kind="typography",
                narration_source=narration_source, narration_start_frame=narration_start_frame,
                graphic_treatment="none" if neutral else "key_point" if scene.graphic_treatment.value == "none" else scene.graphic_treatment.value,
                graphic_text=scene.graphic_text,
                style_tokens=scene.style_tokens.model_dump(),
            )
        if visual.source_kind in {SourceKind.SCREENSHOT, SourceKind.CHART}:
            if visual.asset_id is None or visual.clip_id is not None or any(value is not None for value in (visual.clip_start_ms, visual.clip_end_ms, visual.source_duration_ms)):
                raise RenderInputError("static visual must reference an image asset without a Clip")
            if self.images is None:
                raise LocalResourceError("image asset repository is unavailable")
            image = self.images.get(visual.asset_id)
            if image is None or image.source_kind != visual.source_kind or image.authorization_reference != visual.authorization_reference:
                raise UnauthorizedVisualError("VideoSpec static visual does not match the authorized stored image")
            return _PreparedVisual(source=_local_existing_image(image, self.data_root), trim_before=0, trim_after=0, kind="image", narration_source=narration_source, narration_start_frame=narration_start_frame)
        if visual.source_kind not in {SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET, SourceKind.STOCK, SourceKind.AI_VIDEO, SourceKind.AI_BROLL}:
            raise UnauthorizedVisualError("renderer accepts only authorized user or historical local media")
        if visual.asset_id is None or visual.clip_id is None or visual.clip_start_ms is None or visual.clip_end_ms is None:
            raise RenderInputError("renderer requires a complete local Clip visual reference")
        asset = self.assets.get(visual.asset_id)
        clip = self.clips.get(visual.clip_id)
        if asset is None or clip is None:
            raise LocalResourceError("referenced local media is unavailable")
        if (
            clip.asset_id != asset.id
            or asset.source_kind != visual.source_kind
            or visual.authorization_reference != asset.authorization_reference
            or visual.clip_start_ms < clip.start_ms
            or visual.clip_end_ms > clip.end_ms
            or visual.clip_end_ms <= visual.clip_start_ms
            or clip.asset_duration_ms != asset.duration_ms
            or visual.source_duration_ms != asset.duration_ms
            or clip.end_ms > asset.duration_ms
        ):
            raise UnauthorizedVisualError("VideoSpec visual does not match the authorized stored Clip")
        if scene.portrait_presentation is PortraitPresentation.STACKED_COMPARISON:
            if asset.width * 9 != asset.height * 16:
                raise RenderInputError("stacked_comparison requires a 16:9 source")
            from app.assembly.edit_plan import comparison_clearance, source_frame_interval_ms
            playback_interval_ms = source_frame_interval_ms(
                visual.clip_start_ms, scene.duration_frames, composition_fps, visual.clip_end_ms,
            )
            try:
                ratio,reference=comparison_clearance(asset,clip,playback_interval_ms)
            except ValueError as exc:
                raise RenderInputError(str(exc)) from None
            if (abs(ratio-visual.source_bottom_crop_ratio)>1e-9
                or reference!=visual.vertical_reframe_evidence_reference):
                raise RenderInputError("comparison subtitle geometry changed; replan required")
        if asset.source_kind is SourceKind.AI_VIDEO:
            master_interval = None
            selected_duration_ms = math.ceil(scene.duration_frames * 1000 * composition_fps.denominator / composition_fps.numerator)
            if master_audio_id is not None and scene.narration_start_ms is not None:
                master_interval = (scene.narration_start_ms, scene.narration_end_ms)
                selected_duration_ms = master_interval[1] - master_interval[0]
                frame_units = selected_duration_ms * composition_fps.numerator
                denominator = 1000 * composition_fps.denominator
                if scene.duration_frames not in {frame_units // denominator, (frame_units + denominator - 1) // denominator}:
                    raise RenderInputError("Talking timeline frames do not cover its narration interval")
            blocker = talking_visual_blocker(
                asset, clip, self.assets, project_id=project_id, copy=scene.caption or "",
                master_audio_id=master_audio_id or scene.narration_asset_id,
                production_run_id=production_run_id,
                selected_duration_ms=selected_duration_ms, master_interval=master_interval,
            )
            if blocker is not None:
                raise UnauthorizedVisualError(f"generated Talking visual: {blocker}")
        if asset.source_kind is SourceKind.AI_BROLL:
            from app.media.visual_facts import generated_footage_admitted, full_interval_geometry
            if (not generated_footage_admitted(asset)
                or not full_interval_geometry(asset,clip,(visual.clip_start_ms,visual.clip_end_ms))):
                raise UnauthorizedVisualError("generated illustration model admission or source QA changed")
        source = _local_existing_file(asset, self.data_root)
        # Remotion trimBefore/trimAfter are measured in composition frames,
        # not the source file's native frame rate. Both endpoints use the
        # timeline's ceiling rule; flooring the end drops a boundary frame.
        trim_before = _ceil_frames(visual.clip_start_ms, composition_fps)
        trim_after = _ceil_frames(visual.clip_end_ms, composition_fps)
        if trim_after <= trim_before:
            raise RenderInputError("Clip interval cannot be represented as positive source frames")
        # Source and composition may use different native frame rates (for
        # example a 25fps Talking result in a 29.97fps vertical project).
        # Comparing their frame counts directly rejects valid intervals.  The
        # last displayed composition frame must instead begin within the
        # authorized millisecond interval; the renderer may repeat a source
        # frame during rate conversion, but never plays past the Clip end.
        required_last_frame_ms = ((trim_before + scene.duration_frames - 1) * 1_000 * composition_fps.denominator) / composition_fps.numerator
        if required_last_frame_ms >= visual.clip_end_ms:
            raise RenderInputError("Clip duration is shorter than the requested scene timeline")
        return _PreparedVisual(
            source=source, trim_before=trim_before, trim_after=trim_after, kind="video",
            narration_source=narration_source, narration_start_frame=narration_start_frame,
            vertical_reframe_mode=visual.vertical_reframe_mode.value,
            portrait_presentation=scene.portrait_presentation.value,
            source_bottom_crop_ratio=visual.source_bottom_crop_ratio,
            source_width=asset.width,source_height=asset.height,
            graphic_treatment=scene.graphic_treatment.value,
            graphic_text=scene.graphic_text,
            style_tokens=scene.style_tokens.model_dump(),
        )

    def _validate_narration(self, scene: VideoScene, composition_fps: RationalFps) -> tuple[Path | None, int]:
        if scene.narration_asset_id is None:
            return None, 0
        if self.audios is None:
            raise LocalResourceError("audio asset repository is unavailable")
        audio = self.audios.get(scene.narration_asset_id)
        if audio is None:
            raise LocalResourceError("referenced narration audio is unavailable")
        start_ms = scene.narration_start_ms or 0
        end_ms = scene.narration_end_ms or audio.duration_ms
        if end_ms > audio.duration_ms:
            raise RenderInputError("narration interval exceeds audio duration")
        required_ms = (scene.duration_frames * 1_000 * composition_fps.denominator + composition_fps.numerator - 1) // composition_fps.numerator
        if end_ms - start_ms < required_ms:
            raise RenderInputError("narration interval is shorter than the scene")
        return _local_existing_audio(audio, self.data_root), _ceil_frames(start_ms, composition_fps)

    def _validate_master_narration(self, spec: VideoSpec, composition_fps: RationalFps) -> Path | None:
        master = spec.master_narration
        if master is None:
            return None
        if self.audios is None:
            raise LocalResourceError("audio asset repository is unavailable")
        audio = self.audios.get(master.audio_asset_id)
        if audio is None:
            raise LocalResourceError("referenced master narration audio is unavailable")
        if master.end_ms > audio.duration_ms:
            raise RenderInputError("master narration interval exceeds audio duration")
        return _local_existing_audio(audio, self.data_root)

    def _stage_props(
        self,
        spec: VideoSpec,
        prepared: Mapping[str, _PreparedVisual],
        staging: Path,
        run_id: str,
        master_narration: Path | None,
    ) -> dict[str, object]:
        staged_by_source: dict[tuple[Path, float], str] = {}
        staged_by_audio: dict[Path, str] = {}
        scene_sources: dict[str, dict[str, object]] = {}
        for scene in spec.scenes:
            visual = prepared[scene.scene_id]
            narration_relative = None
            if visual.narration_source is not None:
                narration_relative = staged_by_audio.get(visual.narration_source)
                if narration_relative is None:
                    filename = f"narration-{len(staged_by_audio):03d}{visual.narration_source.suffix}"
                    destination = staging / filename
                    shutil.copy2(visual.narration_source, destination)
                    narration_relative = f"content-os-renders/{run_id}/{filename}".replace("\\", "/")
                    staged_by_audio[visual.narration_source] = narration_relative
            if visual.source is None:
                scene_sources[scene.scene_id] = {
                    "kind": "typography",
                    "text": "" if visual.graphic_treatment == "none" else visual.graphic_text or scene.caption or scene.scene_id,
                    "graphicTreatment": visual.graphic_treatment,
                    "styleTokens": visual.style_tokens or scene.style_tokens.model_dump(),
                }
                if narration_relative is not None:
                    scene_sources[scene.scene_id]["narrationSrc"] = narration_relative
                    scene_sources[scene.scene_id]["narrationStartFrame"] = visual.narration_start_frame
                continue
            bottom_crop = (visual.source_bottom_crop_ratio if visual.vertical_reframe_mode == "bottom_crop_contain"
                and visual.portrait_presentation != "stacked_comparison" else 0)
            source_key = (visual.source, bottom_crop)
            relative = staged_by_source.get(source_key)
            if relative is None:
                filename = f"source-{len(staged_by_source):03d}{visual.source.suffix}"
                destination = staging / filename
                if bottom_crop:
                    from app.runtime import resolve_local_executable

                    destination = destination.with_suffix(".mp4")
                    filename = destination.name
                    # Crop only the evidenced bottom strip; retain all horizontal
                    # motion, timestamps and source audio. Never alter the asset.
                    argv = [resolve_local_executable("ffmpeg"), "-nostdin", "-y", "-v", "error",
                            "-i", str(visual.source), "-vf", f"crop=iw:trunc(ih*(1-{bottom_crop})/2)*2:0:0",
                            "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-c:a", "copy",
                            "-movflags", "+faststart", str(destination)]
                    completed = self.runner.run(argv, cwd=self.renderer_dir, timeout_seconds=self.timeout_seconds)
                    if completed.returncode != 0 or not destination.is_file() or destination.stat().st_size == 0:
                        raise RenderProcessError(completed.returncode or -1, stderr=completed.stderr)
                else:
                    shutil.copy2(visual.source, destination)
                relative = f"content-os-renders/{run_id}/{filename}".replace("\\", "/")
                staged_by_source[source_key] = relative
            scene_sources[scene.scene_id] = {
                "kind": visual.kind,
                "src": relative,
                "trimBefore": visual.trim_before,
                "trimAfter": visual.trim_after,
                "verticalReframeMode": visual.vertical_reframe_mode,
                "portraitPresentation": visual.portrait_presentation,
                "sourceBottomCropRatio": 0 if bottom_crop else visual.source_bottom_crop_ratio,
                "sourceAspectRatio": visual.source_width/(visual.source_height*(1-bottom_crop)) if visual.source_height else None,
                "graphicText": visual.graphic_text,
                "graphicTreatment": visual.graphic_treatment,
                "styleTokens": visual.style_tokens or scene.style_tokens.model_dump(),
            }
            if narration_relative is not None:
                scene_sources[scene.scene_id]["narrationSrc"] = narration_relative
                scene_sources[scene.scene_id]["narrationStartFrame"] = visual.narration_start_frame
        return {
            "videoSpec": spec.model_dump(mode="json"),
            "sceneSources": scene_sources,
            "audioMode": "master_narration" if master_narration is not None else "narration" if any(value.narration_source is not None for value in prepared.values()) else "source",
        }


def _renderer_project(directory: Path) -> None:
    if not (directory / "package.json").is_file() or not (directory / "src" / "index.ts").is_file():
        raise RenderInputError("renderer_dir must contain the Content OS Remotion project")


def _output_path(value: str | Path) -> Path:
    if not isinstance(value, (str, Path)):
        raise RenderInputError("render output path must be local")
    raw = str(value)
    if _is_remote_or_network_path(raw):
        raise RenderInputError("render output path must be local")
    path = Path(value).expanduser().resolve()
    if path.suffix.lower() != ".mp4":
        raise RenderInputError("render output must use the .mp4 extension")
    return path


def _local_existing_file(asset: Asset, data_root: Path | None = None) -> Path:
    raw = asset.source_file
    if _is_remote_or_network_path(raw):
        raise LocalResourceError("renderer rejects remote or network media sources")
    path = _resolve_local_media_path(raw, data_root)
    if not path.is_file():
        raise LocalResourceError("referenced local media file does not exist")
    return path


def _resolve_local_media_path(raw: str, data_root: Path | None) -> Path:
    try:
        return resolve_local_media_path(raw, data_root)
    except ValueError as exc:
        raise LocalResourceError(str(exc)) from exc


def _local_existing_image(asset: ImageAsset, data_root: Path | None = None) -> Path:
    raw = asset.source_file
    if _is_remote_or_network_path(raw):
        raise LocalResourceError("renderer rejects remote or network image sources")
    path = _resolve_local_media_path(raw, data_root)
    if not path.is_file():
        raise LocalResourceError("referenced local image file does not exist")
    return path


def _local_existing_audio(asset: AudioAsset, data_root: Path | None = None) -> Path:
    raw = asset.source_file
    if _is_remote_or_network_path(raw):
        raise LocalResourceError("renderer rejects remote or network audio sources")
    path = _resolve_local_media_path(raw, data_root)
    if not path.is_file():
        raise LocalResourceError("referenced local narration file does not exist")
    return path


def _ceil_frames(milliseconds: int, fps: RationalFps) -> int:
    numerator = milliseconds * fps.numerator
    denominator = 1_000 * fps.denominator
    return (numerator + denominator - 1) // denominator


def _is_remote_or_network_path(raw: str) -> bool:
    # ``urlsplit('C:\\media\\clip.mp4')`` treats ``c`` as a scheme, so honor
    # ordinary Windows drive paths before applying URL rejection.
    if re.match(r"^[A-Za-z]:[\\/]", raw):
        return False
    parsed = urlsplit(raw)
    return bool(parsed.scheme or parsed.netloc or raw.startswith("//") or raw.startswith("\\\\"))


def _npm_executable(command: str) -> str:
    """Resolve the Windows ``npm.cmd`` shim without falling back to a shell."""
    resolved = shutil.which(command)
    if resolved is None and os.name == "nt" and Path(command).suffix == "":
        resolved = shutil.which(f"{command}.cmd")
    return resolved or command
