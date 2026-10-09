"""Local, review-only Master-audio Talking preview for an existing planned child."""
from __future__ import annotations

import hashlib
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from app.db import AssetRepository, AudioAssetRepository, Database, TalkingSliceSeriesRepository
from app.domain.models import Asset, Job, JobStatus, JobType, SourceKind, TalkingRunPreviewJobPayload
from app.media.ffprobe import FFProbeAdapter
from app.production_runs import ProductionRunService, _snapshot_sha256
from app.talking.review_policy import TalkingReviewPolicyService


class TalkingPreviewError(ValueError):
    pass


def _source_path(value: str, data_root: Path) -> Path:
    path = Path(value)
    resolved = (path if path.is_absolute() else
                data_root.parent / path if path.parts and path.parts[0].casefold() == data_root.name.casefold()
                else data_root / path).resolve()
    if not resolved.is_relative_to(data_root) or not resolved.is_file():
        raise TalkingPreviewError("preview input is unavailable inside the data root")
    return resolved


class TalkingRunPreviewJobHandler:
    def __init__(self, db: Database, data_root: str | Path, *, ffmpeg_command: str | Path, probe: FFProbeAdapter):
        self.db = db
        self.data_root = Path(data_root).resolve()
        self.ffmpeg_command = str(ffmpeg_command)
        self.probe = probe

    def _current(self, job: Job):
        if (job.type is not JobType.PREPARE_TALKING_RUN_PREVIEW or job.status is not JobStatus.RUNNING
            or not isinstance(job.payload, TalkingRunPreviewJobPayload) or job.project_id != job.payload.project_id):
            raise TalkingPreviewError("preview requires a claimed typed Job")
        series = TalkingSliceSeriesRepository(self.db).get(job.payload.series_id)
        if series is None or series.project_id != job.project_id or series.planned_origin is None or len(series.child_job_ids) != 1:
            raise TalkingPreviewError("preview planned collection is missing")
        origin = series.planned_origin
        if (series.child_job_ids != [origin.generation_job_id]
            or _snapshot_sha256(origin.model_dump(mode="json")) != job.payload.origin_sha256):
            raise TalkingPreviewError("preview planned origin snapshot changed")
        run = ProductionRunService(self.db, self.data_root)
        run.require_current_planned_run_origin(job.project_id, origin.run_id, origin.scene_plan_id, expected=origin)
        current = run.get(job.project_id, origin.run_id)
        binding = next((item for item in current.talking_source_bindings if item.scene_plan_id == origin.scene_plan_id), None)
        if binding is None or binding.talking_series_id != series.id or binding.talking_preview_job_id != job.id:
            raise TalkingPreviewError("preview is not bound to the current production run")
        if origin.version == 2:
            for asset in AssetRepository(self.db).list():
                report = asset.metadata.get("talking_run_preview")
                if isinstance(report, dict) and report.get("series_id") == str(series.id):
                    TalkingReviewPolicyService(self.db).require_clear(series.project_id, asset.content_hash, pending=False)
        output = AssetRepository(self.db).get(origin.output_asset_id)
        master = AudioAssetRepository(self.db).get(origin.master_audio_id)
        if output is None or master is None or output.content_hash != origin.output_sha256 or master.content_hash != origin.master_sha256:
            raise TalkingPreviewError("preview media snapshot changed")
        return series, origin, output, master

    def __call__(self, job: Job) -> None:
        from app.jobs.runner import JobExecutionError

        stage: Path | None = None
        final: Path | None = None
        placed = False
        try:
            series, origin, output, master = self._current(job)
            assets = AssetRepository(self.db)
            prior = [asset for asset in assets.list() if isinstance(asset.metadata.get("talking_run_preview"), dict)
                     and asset.metadata["talking_run_preview"].get("job_id") == str(job.id)]
            if prior:
                if len(prior) != 1 or prior[0].metadata["talking_run_preview"].get("origin_sha256") != job.payload.origin_sha256:
                    raise TalkingPreviewError("preview Job has ambiguous existing output")
                path = _source_path(prior[0].source_file, self.data_root)
                with path.open("rb") as stream:
                    if hashlib.file_digest(stream, "sha256").hexdigest() != prior[0].content_hash:
                        raise TalkingPreviewError("preview output bytes changed")
                return
            video_path = _source_path(output.source_file, self.data_root)
            master_path = _source_path(master.source_file, self.data_root)
            folder = self.data_root / "generated" / "talking-previews"
            folder.mkdir(parents=True, exist_ok=True)
            stage = folder / f"{job.id}-{job.attempt}.tmp.mp4"
            duration = (origin.master_end_ms - origin.master_start_ms) / 1000
            command = [self.ffmpeg_command, "-nostdin", "-y", "-i", str(video_path),
                       "-ss", f"{origin.master_start_ms / 1000:.3f}", "-t", f"{duration:.3f}",
                       "-i", str(master_path), "-map", "0:v:0", "-map", "1:a:0",
                       "-t", f"{duration:.3f}", "-c:v", "libx264", "-c:a", "aac", str(stage)]
            completed = subprocess.run(command, shell=False, capture_output=True, text=True, timeout=300, check=False)
            if completed.returncode != 0 or not stage.is_file() or stage.stat().st_size == 0:
                raise TalkingPreviewError("Master-audio preview FFmpeg preparation failed")
            probe = self.probe.probe(stage)
            if not probe.has_audio or abs(probe.duration_ms - round(duration * 1000)) > 80:
                raise TalkingPreviewError("Master-audio preview stream or duration QA failed")
            with stage.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            final = folder / f"{job.id}-{digest}.mp4"
            with self.db.transaction(immediate=True):
                self._current(job)
                existing = assets.get_by_content_hash(digest)
                if existing is not None:
                    raise TalkingPreviewError("preview bytes collide with another asset identity")
                os.link(stage, final)
                placed = True
                metadata = dict(probe.metadata)
                metadata["talking_run_preview"] = {
                    "state": "review_only", "technical_qa_state": "verified", "job_id": str(job.id),
                    "series_id": str(series.id), "origin_sha256": job.payload.origin_sha256,
                    "input_output_sha256": origin.output_sha256, "master_sha256": origin.master_sha256,
                    "master_start_ms": origin.master_start_ms, "master_end_ms": origin.master_end_ms,
                    "duration_ms": probe.duration_ms, "has_audio": probe.has_audio,
                    "video_width": probe.width, "video_height": probe.height,
                }
                if origin.version == 2:
                    metadata["talking_run_preview"]["review_policy_version"] = 2
                asset = Asset(
                    source_kind=SourceKind.AI_VIDEO, source_file=str(final.relative_to(self.data_root)),
                    content_hash=digest, duration_ms=probe.duration_ms, width=probe.width, height=probe.height,
                    fps=probe.fps, has_audio=probe.has_audio,
                    authorization_reference=origin.authorization_reference,
                    imported_at=datetime.now(timezone.utc), metadata=metadata,
                )
                assets.create(asset)
        except Exception as exc:
            if placed and final is not None:
                try:
                    final.unlink(missing_ok=True)
                except OSError:
                    pass
            raise JobExecutionError("talking_preview_invalid", str(exc), retryable=False) from exc
        finally:
            if stage is not None:
                stage.unlink(missing_ok=True)
