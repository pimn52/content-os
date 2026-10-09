"""Evidence-bound local derivation of requested vertical source Clips.

The service makes a new ordinary Asset/Clip so routing and VideoSpec keep
their existing contracts.  The original Asset is never changed; all claims
about crop safety stay scoped to the requested source interval and crop.
"""
from __future__ import annotations

import os
import hashlib
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from app.db import AssetRepository, ClipRepository, Database
from app.domain.models import Asset, Clip, SourceCropWindow, SourceKind, VerifiedVerticalDerivationRequest
from .crop_assessment import CropAssessmentRepository, CropProposal

from .ffprobe import ProbeError
from .importer import MediaImportError, MediaImporter


class VerticalDerivationError(RuntimeError):
    """Base class for bounded source-derivation failures."""


class VerticalDerivationValidationError(VerticalDerivationError):
    pass


class VerticalDerivationConflict(VerticalDerivationError):
    pass


class VerticalDerivationProcessError(VerticalDerivationError):
    pass


@dataclass(frozen=True)
class VerticalDerivationResult:
    asset: Asset
    clip: Clip


Runner = Callable[[list[str]], None]


class VerticalSourceDerivationService:
    """Run a fixed, reviewed crop and persist its portable provenance."""

    def __init__(
        self,
        db: Database,
        data_root: str | Path,
        importer: MediaImporter,
        *,
        command: str | Sequence[str] = "ffmpeg",
        timeout_seconds: float = 120.0,
        runner: Runner | None = None,
    ) -> None:
        if isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.db = db
        self.assets = AssetRepository(db)
        self.clips = ClipRepository(db)
        self.importer = importer
        self.data_root = Path(data_root)
        self.output_root = self.data_root / "generated" / "vertical-derivatives"
        self.command = (command,) if isinstance(command, str) else tuple(command)
        if not self.command or any(not item for item in self.command):
            raise ValueError("ffmpeg command must not be empty")
        self.timeout_seconds = float(timeout_seconds)
        self.runner = runner

    def derive(self, request: VerifiedVerticalDerivationRequest) -> VerticalDerivationResult:
        source, source_clip, source_path = self._validate_request(request)
        existing = self._existing_idempotent_result(request, source)
        if existing is not None:
            return existing
        self.output_root.mkdir(parents=True, exist_ok=True)
        temp = self._temporary_output()
        try:
            self._run(self._command(source_path, request, temp))
            if not temp.is_file() or temp.stat().st_size <= 0:
                raise VerticalDerivationProcessError("ffmpeg did not produce a vertical derivative")
            try:
                measured = self.importer.probe.probe(temp)
            except (ProbeError, OSError) as exc:
                raise VerticalDerivationProcessError(f"derived media probe failed: {exc}") from exc
            if (measured.width, measured.height) != (1080, 1920):
                raise VerticalDerivationProcessError("derived media is not 1080x1920")
            if measured.duration_ms < request.end_ms - request.start_ms:
                raise VerticalDerivationProcessError("derived media is shorter than the requested source interval")
            try:
                derived = self.importer.import_path(
                    temp,
                    source.authorization_reference,
                    source_kind=source.source_kind,
                )
            except (FileNotFoundError, MediaImportError, ProbeError, OSError) as exc:
                raise VerticalDerivationProcessError(f"derived media could not be imported: {exc}") from exc
            provenance = self._provenance(source, source_clip, request)
            stored = self.assets.get(derived.id)
            if stored is None:
                raise VerticalDerivationProcessError("derived Asset was not persisted")
            current = stored.metadata.get("source_derivation")
            if current is not None:
                if current != provenance:
                    raise VerticalDerivationConflict("derived media bytes already belong to a different source derivation")
            else:
                stored = stored.model_copy(update={"metadata": {
                    **stored.metadata,
                    **self._inherited_admission_metadata(source),
                    "source_derivation": provenance,
                }})
                with self.db.transaction():
                    self.assets.update(stored)
            return self._ensure_output_clip(stored, request)
        finally:
            temp.unlink(missing_ok=True)

    def _existing_idempotent_result(self, request: VerifiedVerticalDerivationRequest, source: Asset) -> VerticalDerivationResult | None:
        for asset in self.assets.list():
            provenance = asset.metadata.get("source_derivation")
            if not isinstance(provenance, dict) or provenance.get("idempotency_key") != request.idempotency_key:
                continue
            expected = self._request_identity(request)
            expected["source_content_hash"] = source.content_hash
            if {key: provenance.get(key) for key in expected} != expected:
                raise VerticalDerivationConflict("idempotency key is already bound to a different vertical derivation")
            output_path = self._resolve_source_path(asset.source_file)
            if not output_path.is_file() or self._sha256(output_path) != asset.content_hash:
                raise VerticalDerivationConflict("idempotent derived media is missing or its bytes changed")
            return self._ensure_output_clip(asset, request, create=False)
        return None

    def _validate_request(self, request: VerifiedVerticalDerivationRequest) -> tuple[Asset, Clip, Path]:
        asset = self.assets.get(request.source_asset_id)
        clip = self.clips.get(request.source_clip_id)
        if asset is None or clip is None:
            raise VerticalDerivationValidationError("source Asset or Clip is unavailable")
        if clip.asset_id != asset.id or clip.asset_duration_ms != asset.duration_ms:
            raise VerticalDerivationValidationError("source Clip does not match source Asset")
        if asset.width <= asset.height:
            raise VerticalDerivationValidationError("vertical derivation requires a horizontal source Asset")
        if request.start_ms < clip.start_ms or request.end_ms > clip.end_ms:
            raise VerticalDerivationValidationError("requested derivation interval must stay within the source Clip")
        if request.evidence.source_asset_id != asset.id or request.evidence.source_clip_id != clip.id or request.evidence.crop_window != request.crop_window:
            raise VerticalDerivationValidationError("derivation evidence does not match source and exact crop")
        if (
            request.evidence.method != "human_full_interval_review"
            or not request.evidence.method_version
            or request.evidence.coverage != "full_interval_continuous"
            or request.evidence.confidence != "reviewed"
        ):
            raise VerticalDerivationValidationError("full-interval review method and coverage are required")
        if request.evidence.covered_start_ms > request.start_ms or request.evidence.covered_end_ms < request.end_ms:
            raise VerticalDerivationValidationError("derivation evidence does not cover the complete requested interval")
        source_path = self._resolve_source_path(asset.source_file)
        if not source_path.is_file():
            raise VerticalDerivationValidationError("source Asset media is unavailable")
        if asset.content_hash != request.evidence.source_content_hash or self._sha256(source_path) != asset.content_hash:
            raise VerticalDerivationValidationError("source media bytes or evidence hash do not match the Asset")
        if CropAssessmentRepository(self.db).decision(CropProposal(
            source_asset_id=asset.id, source_clip_id=clip.id,
            start_ms=request.start_ms, end_ms=request.end_ms, crop_window=request.crop_window,
        )) == "unusable":
            raise VerticalDerivationValidationError("proposed interval/crop has persisted face-boundary failure")
        self._validate_generated_talking_admission(asset)
        self._validate_crop(request.crop_window, asset)
        if any(self._overlap(request.crop_window, subtitle) for subtitle in request.known_subtitle_regions):
            raise VerticalDerivationValidationError("vertical crop intersects known burned-in subtitles")
        return asset, clip, source_path

    def _resolve_source_path(self, source_file: str) -> Path:
        root = self.data_root.resolve()
        value = Path(source_file)
        if value.is_absolute():
            return value.resolve()  # Legacy imported absolute paths remain supported.
        parts = value.parts
        candidate = root.parent / value if parts and parts[0].casefold() == root.name.casefold() else root / value
        candidate = candidate.resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise VerticalDerivationValidationError("portable source path escapes data_root") from exc
        return candidate

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _validate_generated_talking_admission(asset: Asset) -> None:
        """Do not turn an unadmitted generated Talking source into a derivative."""
        if asset.source_kind is not SourceKind.AI_VIDEO:
            return
        run = asset.metadata.get("talking_run")
        if isinstance(run, dict):
            if run.get("admission_state") != "admitted" or run.get("automated_qa_state") != "verified" or run.get("continuity_review_state") != "approved":
                raise VerticalDerivationValidationError("TalkingRun source is not admitted for vertical derivation")
            return
        generation = asset.metadata.get("talking_generation")
        if not isinstance(generation, dict) or generation.get("qa_state") != "verified":
            raise VerticalDerivationValidationError("generated Talking source requires verified automated QA")
        if generation.get("human_review_state") == "rejected":
            raise VerticalDerivationValidationError("rejected generated Talking source cannot be derived")

    @staticmethod
    def _validate_crop(crop: SourceCropWindow, asset: Asset) -> None:
        if crop.x + crop.width > asset.width or crop.y + crop.height > asset.height:
            raise VerticalDerivationValidationError("vertical crop exceeds source dimensions")
        if crop.width * 16 != crop.height * 9:
            raise VerticalDerivationValidationError("vertical crop must have an exact 9:16 aspect ratio")

    @staticmethod
    def _overlap(first: SourceCropWindow, second: SourceCropWindow) -> bool:
        return first.x < second.x + second.width and second.x < first.x + first.width and first.y < second.y + second.height and second.y < first.y + first.height

    def _ensure_output_clip(
        self,
        asset: Asset,
        request: VerifiedVerticalDerivationRequest,
        *,
        create: bool = True,
    ) -> VerticalDerivationResult:
        if (asset.width, asset.height) != (1080, 1920):
            raise VerticalDerivationProcessError("derived Asset is not 1080x1920")
        provenance = asset.metadata.get("source_derivation")
        if not isinstance(provenance, dict):
            raise VerticalDerivationConflict("derived Asset is missing source derivation provenance")
        requested_duration_ms = request.end_ms - request.start_ms
        if asset.duration_ms < requested_duration_ms:
            raise VerticalDerivationProcessError("derived Asset is shorter than the requested source interval")
        candidates = [clip for clip in self.clips.list_by_asset(asset.id) if clip.start_ms == 0 and clip.end_ms == requested_duration_ms]
        if candidates:
            return VerticalDerivationResult(asset=asset, clip=candidates[0])
        if not create:
            raise VerticalDerivationConflict("idempotent derived Asset is missing its full-duration Clip")
        clip = Clip(asset_id=asset.id, start_ms=0, end_ms=requested_duration_ms, asset_duration_ms=asset.duration_ms)
        with self.db.transaction():
            self.clips.create(clip)
        return VerticalDerivationResult(asset=asset, clip=clip)

    def _command(self, source_path: Path, request: VerifiedVerticalDerivationRequest, output: Path) -> list[str]:
        crop = request.crop_window
        start_seconds = f"{request.start_ms / 1000:.3f}"
        duration_seconds = f"{(request.end_ms - request.start_ms) / 1000:.3f}"
        filters = f"crop={crop.width}:{crop.height}:{crop.x}:{crop.y},scale=1080:1920:flags=lanczos"
        return [
            *self.command, "-y", "-v", "error", "-ss", start_seconds, "-i", str(source_path),
            "-t", duration_seconds, "-map", "0:v:0", "-vf", filters, "-an", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output),
        ]

    def _run(self, argv: list[str]) -> None:
        if self.runner is not None:
            self.runner(argv)
            return
        try:
            completed = subprocess.run(argv, shell=False, capture_output=True, text=True, timeout=self.timeout_seconds, check=False)
        except FileNotFoundError as exc:
            raise VerticalDerivationProcessError("ffmpeg binary is unavailable") from exc
        except subprocess.TimeoutExpired as exc:
            raise VerticalDerivationProcessError(f"ffmpeg timed out after {self.timeout_seconds:g}s") from exc
        if completed.returncode != 0:
            raise VerticalDerivationProcessError(f"ffmpeg failed ({completed.returncode}): {completed.stderr.strip()[:500]}")

    def _temporary_output(self) -> Path:
        fd, name = tempfile.mkstemp(prefix=".vertical-", suffix=".mp4", dir=self.output_root)
        os.close(fd)
        path = Path(name)
        path.unlink(missing_ok=True)
        return path

    @staticmethod
    def _request_identity(request: VerifiedVerticalDerivationRequest) -> dict[str, object]:
        return {
            "idempotency_key": request.idempotency_key,
            "source_asset_id": str(request.source_asset_id),
            "source_clip_id": str(request.source_clip_id),
            "source_interval": {"start_ms": request.start_ms, "end_ms": request.end_ms},
            "crop_window": request.crop_window.model_dump(mode="json"),
            "evidence": request.evidence.model_dump(mode="json"),
            "source_subtitle_state": request.source_subtitle_state,
            "known_subtitle_regions": [item.model_dump(mode="json") for item in request.known_subtitle_regions],
        }

    @staticmethod
    def _inherited_admission_metadata(source: Asset) -> dict[str, object]:
        """Keep existing generated-Talking gates intact on a pixel derivative."""
        if source.source_kind is not SourceKind.AI_VIDEO:
            return {}
        return {
            key: value
            for key in ("talking_run", "talking_generation")
            if (value := source.metadata.get(key)) is not None
        }

    def _provenance(self, source: Asset, source_clip: Clip, request: VerifiedVerticalDerivationRequest) -> dict[str, object]:
        return {
            "kind": "verified_vertical_crop_v1",
            **self._request_identity(request),
            "source_content_hash": source.content_hash,
            "source_authorization_reference": source.authorization_reference,
            "source_clip_interval": {"start_ms": source_clip.start_ms, "end_ms": source_clip.end_ms},
            "source_subtitle_state": request.source_subtitle_state,
            "known_subtitle_regions": [item.model_dump(mode="json") for item in request.known_subtitle_regions],
            "evidence": request.evidence.model_dump(mode="json"),
            "transform": {"output_width": 1080, "output_height": 1920, "audio": "excluded", "filter": "fixed_crop_then_scale"},
        }
