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
from uuid import uuid4

from app.db import AssetRepository, AudioAssetRepository, ClipRepository, ImageAssetRepository
from app.domain.models import Asset, AudioAsset, Clip, ImageAsset, ProjectFormat, RationalFps, SourceKind, VideoScene, VideoSpec


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
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode
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
            raise RenderTimeout("local renderer timed out") from exc
        except OSError as exc:
            raise RenderProcessError(-1) from exc


@dataclass(frozen=True)
class _PreparedVisual:
    source: Path | None
    trim_before: int
    trim_after: int
    kind: str
    narration_source: Path | None = None
    narration_start_frame: int = 0


class RemotionRenderer:
    """Render a validated VideoSpec to a local MP4 through ``npm exec remotion``.

    Source audio is kept for scenes without narration. When a persisted local
    narration asset is attached to a scene, the React composition mutes that
    scene's source audio and overlays the authorized narration interval.
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
        self.renderer_dir = directory
        self.npm_command = _npm_executable(npm_command.strip())
        self.timeout_seconds = float(timeout_seconds)
        self.runner = runner or SubprocessRenderCommandRunner()

    def render(self, spec: VideoSpec, output_path: str | Path) -> Path:
        if not isinstance(spec, VideoSpec):
            raise RenderInputError("renderer requires a VideoSpec contract")
        if spec.format != ProjectFormat.VERTICAL or spec.width * 16 != spec.height * 9:
            raise RenderInputError("minimal renderer accepts only 9:16 vertical VideoSpecs")
        output = _output_path(output_path)
        _renderer_project(self.renderer_dir)
        prepared = {scene.scene_id: self._validate_scene(scene, spec.fps) for scene in spec.scenes}
        if output in {visual.source for visual in prepared.values() if visual.source is not None}:
            raise RenderInputError("render output must not overwrite a source media file")
        output.parent.mkdir(parents=True, exist_ok=True)
        public_root = self.renderer_dir / "public" / "content-os-renders"
        public_root.mkdir(parents=True, exist_ok=True)
        run_id = uuid4().hex
        staging = public_root / run_id
        props_path: Path | None = None
        try:
            staging.mkdir()
            props = self._stage_props(spec, prepared, staging, run_id)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".json", prefix="content-os-remotion-", delete=False) as handle:
                json.dump(props, handle, ensure_ascii=False, separators=(",", ":"))
                props_path = Path(handle.name)
            argv = [
                self.npm_command, "exec", "--", "remotion", "render", "src/index.ts", "ContentOSVideo", str(output),
                f"--props={props_path}",
            ]
            completed = self.runner.run(argv, cwd=self.renderer_dir, timeout_seconds=self.timeout_seconds)
            if completed.returncode != 0:
                raise RenderProcessError(completed.returncode)
            if not output.is_file() or output.stat().st_size == 0:
                raise RenderProcessError(-1)
            return output
        finally:
            if props_path is not None:
                props_path.unlink(missing_ok=True)
            # This directory is generated with a random run ID beneath the
            # fixed renderer public root; no user source path is ever removed.
            if staging.exists():
                shutil.rmtree(staging)

    def _validate_scene(self, scene: VideoScene, composition_fps: RationalFps) -> _PreparedVisual:
        visual = scene.visual
        if scene.transition != "cut":
            raise RenderInputError("minimal renderer supports only cut transitions")
        narration_source, narration_start_frame = self._validate_narration(scene, composition_fps)
        if visual.source_kind == SourceKind.TYPOGRAPHY:
            if any(value is not None for value in (visual.asset_id, visual.clip_id, visual.clip_start_ms, visual.clip_end_ms, visual.source_duration_ms)):
                raise RenderInputError("typography visual must not reference a media Clip")
            return _PreparedVisual(source=None, trim_before=0, trim_after=0, kind="typography", narration_source=narration_source, narration_start_frame=narration_start_frame)
        if visual.source_kind in {SourceKind.SCREENSHOT, SourceKind.CHART}:
            if visual.asset_id is None or visual.clip_id is not None or any(value is not None for value in (visual.clip_start_ms, visual.clip_end_ms, visual.source_duration_ms)):
                raise RenderInputError("static visual must reference an image asset without a Clip")
            if self.images is None:
                raise LocalResourceError("image asset repository is unavailable")
            image = self.images.get(visual.asset_id)
            if image is None or image.source_kind != visual.source_kind or image.authorization_reference != visual.authorization_reference:
                raise UnauthorizedVisualError("VideoSpec static visual does not match the authorized stored image")
            return _PreparedVisual(source=_local_existing_image(image), trim_before=0, trim_after=0, kind="image", narration_source=narration_source, narration_start_frame=narration_start_frame)
        if visual.source_kind not in {SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET}:
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
        source = _local_existing_file(asset)
        # Remotion trimBefore/trimAfter are measured in composition frames,
        # not the source file's native frame rate.
        trim_before = _ceil_frames(visual.clip_start_ms, composition_fps)
        trim_after = _floor_frames(visual.clip_end_ms, composition_fps)
        if trim_after <= trim_before:
            raise RenderInputError("Clip interval cannot be represented as positive source frames")
        # VideoSpec's own validator enforces source duration relative to the
        # project timeline. This checks the adapter's source-frame trim too.
        if scene.duration_frames > trim_after - trim_before:
            raise RenderInputError("Clip source frame range is shorter than the requested scene")
        return _PreparedVisual(source=source, trim_before=trim_before, trim_after=trim_after, kind="video", narration_source=narration_source, narration_start_frame=narration_start_frame)

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
        return _local_existing_audio(audio), _ceil_frames(start_ms, composition_fps)

    def _stage_props(
        self,
        spec: VideoSpec,
        prepared: Mapping[str, _PreparedVisual],
        staging: Path,
        run_id: str,
    ) -> dict[str, object]:
        staged_by_source: dict[Path, str] = {}
        staged_by_audio: dict[Path, str] = {}
        scene_sources: dict[str, dict[str, object]] = {}
        for scene in spec.scenes:
            visual = prepared[scene.scene_id]
            narration_relative = None
            if visual.narration_source is not None:
                narration_relative = staged_by_audio.get(visual.narration_source)
                if narration_relative is None:
                    filename = f"narration-{len(staged_by_audio):03d}-{visual.narration_source.name}"
                    destination = staging / filename
                    shutil.copy2(visual.narration_source, destination)
                    narration_relative = f"content-os-renders/{run_id}/{filename}".replace("\\", "/")
                    staged_by_audio[visual.narration_source] = narration_relative
            if visual.source is None:
                scene_sources[scene.scene_id] = {
                    "kind": "typography",
                    "text": scene.caption or scene.scene_id,
                }
                if narration_relative is not None:
                    scene_sources[scene.scene_id]["narrationSrc"] = narration_relative
                    scene_sources[scene.scene_id]["narrationStartFrame"] = visual.narration_start_frame
                continue
            relative = staged_by_source.get(visual.source)
            if relative is None:
                filename = f"{len(staged_by_source):03d}-{visual.source.name}"
                destination = staging / filename
                shutil.copy2(visual.source, destination)
                relative = f"content-os-renders/{run_id}/{filename}".replace("\\", "/")
                staged_by_source[visual.source] = relative
            scene_sources[scene.scene_id] = {
                "kind": visual.kind,
                "src": relative,
                "trimBefore": visual.trim_before,
                "trimAfter": visual.trim_after,
            }
            if narration_relative is not None:
                scene_sources[scene.scene_id]["narrationSrc"] = narration_relative
                scene_sources[scene.scene_id]["narrationStartFrame"] = visual.narration_start_frame
        return {
            "videoSpec": spec.model_dump(mode="json"),
            "sceneSources": scene_sources,
            "audioMode": "narration" if any(value.narration_source is not None for value in prepared.values()) else "source",
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


def _local_existing_file(asset: Asset) -> Path:
    raw = asset.source_file
    if _is_remote_or_network_path(raw):
        raise LocalResourceError("renderer rejects remote or network media sources")
    path = Path(raw).expanduser().resolve()
    if not path.is_file():
        raise LocalResourceError("referenced local media file does not exist")
    return path


def _local_existing_image(asset: ImageAsset) -> Path:
    raw = asset.source_file
    if _is_remote_or_network_path(raw):
        raise LocalResourceError("renderer rejects remote or network image sources")
    path = Path(raw).expanduser().resolve()
    if not path.is_file():
        raise LocalResourceError("referenced local image file does not exist")
    return path


def _local_existing_audio(asset: AudioAsset) -> Path:
    raw = asset.source_file
    if _is_remote_or_network_path(raw):
        raise LocalResourceError("renderer rejects remote or network audio sources")
    path = Path(raw).expanduser().resolve()
    if not path.is_file():
        raise LocalResourceError("referenced local narration file does not exist")
    return path


def _ceil_frames(milliseconds: int, fps: RationalFps) -> int:
    numerator = milliseconds * fps.numerator
    denominator = 1_000 * fps.denominator
    return (numerator + denominator - 1) // denominator


def _floor_frames(milliseconds: int, fps: RationalFps) -> int:
    return milliseconds * fps.numerator // (1_000 * fps.denominator)


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
