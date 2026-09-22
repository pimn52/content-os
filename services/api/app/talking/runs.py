"""Application service that admits reviewed Talking slice series as product assets."""
from __future__ import annotations

import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from app.db import AssetRepository, AudioAssetRepository, ClipRepository, JobRepository, TalkingRunRepository, TalkingSliceSeriesContinuityReviewRepository
from app.domain.models import Asset, AudioAsset, Clip, Job, JobStatus, SourceKind, TalkingGenerationJobPayload, TalkingRun, TalkingRunChildEvidence, TalkingSliceSeries
from app.media import MediaImporter
from app.talking.series_review import assess_talking_slice_series


class TalkingRunAssemblyError(ValueError):
    """The reviewed series cannot safely become one product-level visual."""


class TalkingRunAssembler:
    """Join visuals and remux one authoritative MasterNarration interval.

    Provider child audio is deliberately never an input to the product output.
    The result is imported through the ordinary asset library and receives one
    whole-duration Clip, so Router and VideoSpec do not need child-job knowledge.
    """

    def __init__(self, *, assets: AssetRepository, audios: AudioAssetRepository, clips: ClipRepository,
                 jobs: JobRepository, reviews: TalkingSliceSeriesContinuityReviewRepository,
                 runs: TalkingRunRepository, importer: MediaImporter, output_root: str | Path,
                 ffmpeg_command: str | Path = "ffmpeg") -> None:
        self.assets, self.audios, self.clips, self.jobs = assets, audios, clips, jobs
        self.reviews, self.runs, self.importer = reviews, runs, importer
        self.output_root = Path(output_root)
        self.ffmpeg_command = str(ffmpeg_command)

    def assemble(self, series: TalkingSliceSeries) -> TalkingRun:
        existing = self.runs.get_by_series_id(series.id)
        if existing is not None:
            return existing
        master = self.audios.get(series.narration_audio_id)
        review = self.reviews.get_by_series_id(series.id)
        if master is None or review is None or not review.approved:
            raise TalkingRunAssemblyError("TalkingRun requires an approved series continuity review and available MasterNarration")
        children = [self.jobs.get(job_id) for job_id in series.child_job_ids]
        assessment = assess_talking_slice_series(series, children, self.assets.list())
        if not assessment.ready_for_human_continuity_review:
            raise TalkingRunAssemblyError("TalkingRun requires every child output, automated QA and child human review")
        if review.child_job_ids != series.child_job_ids:
            raise TalkingRunAssemblyError("TalkingRun continuity review does not cover this exact child order")
        generation = master.metadata.get("voice_generation")
        if not isinstance(generation, dict) or generation.get("qa_state") != "verified":
            raise TalkingRunAssemblyError("TalkingRun requires verified MasterNarration")
        evidence = self._evidence(children, assessment, master)
        reference_clip_id = self._reference_clip_id(children)
        self._require_source_forward(children, reference_clip_id)
        start_ms, end_ms = evidence[0].master_start_ms, evidence[-1].master_end_ms
        if end_ms > master.duration_ms:
            raise TalkingRunAssemblyError("TalkingRun child evidence exceeds MasterNarration duration")
        output = self._assemble_media([self.assets.get(child.output_asset_id) for child in evidence], master, start_ms, end_ms, series.id)
        try:
            asset = self.importer.import_path(output, self._authorization(children), source_kind=SourceKind.AI_VIDEO)
        finally:
            output.unlink(missing_ok=True)
        if not asset.has_audio or asset.duration_ms <= 0:
            raise TalkingRunAssemblyError("assembled TalkingRun output is not a playable video with authoritative audio")
        metadata = dict(asset.metadata)
        metadata["talking_run"] = {
            "run_id": None, "series_id": str(series.id), "master_narration_audio_id": str(master.id),
            "master_start_ms": start_ms, "master_end_ms": end_ms,
            "authorized_reference_clip_id": str(reference_clip_id), "automated_qa_state": "verified",
            "continuity_review_id": str(review.id), "continuity_review_state": "approved",
            "admission_state": "admitted", "child_job_ids": [str(child.job_id) for child in evidence],
        }
        asset = self.assets.update(asset.model_copy(update={"metadata": metadata}))
        clip = self.clips.create(Clip(asset_id=asset.id, start_ms=0, end_ms=asset.duration_ms,
                                      asset_duration_ms=asset.duration_ms, talking_candidate=True))
        run = TalkingRun(project_id=series.project_id, series_id=series.id, master_narration_audio_id=master.id,
                         master_start_ms=start_ms, master_end_ms=end_ms, authorized_reference_clip_id=reference_clip_id,
                         child_evidence=evidence, continuity_review_id=review.id, assembled_asset_id=asset.id,
                         assembled_clip_id=clip.id, automated_qa_state="verified", admission_state="admitted",
                         created_at=datetime.now(timezone.utc))
        metadata["talking_run"]["run_id"] = str(run.id)
        self.assets.update(asset.model_copy(update={"metadata": metadata}))
        persisted = self.runs.create(run)
        if persisted.id != run.id:
            raise TalkingRunAssemblyError("TalkingRun admission conflicted with another result")
        return persisted

    def _evidence(self, jobs: list[Job | None], assessment: object, master: AudioAsset) -> list[TalkingRunChildEvidence]:
        statuses = getattr(assessment, "children")
        evidence: list[TalkingRunChildEvidence] = []
        for index, (job, status) in enumerate(zip(jobs, statuses, strict=True)):
            if job is None or job.status is not JobStatus.COMPLETED or not isinstance(job.payload, TalkingGenerationJobPayload) or status.output_asset_id is None:
                raise TalkingRunAssemblyError("TalkingRun child evidence is incomplete")
            output = self.assets.get(status.output_asset_id)
            generation = None if output is None else output.metadata.get("talking_generation")
            if not isinstance(generation, dict):
                raise TalkingRunAssemblyError("TalkingRun child output has no Talking provenance")
            start, end = generation.get("master_slice_start_ms"), generation.get("master_slice_end_ms")
            window_start, window_end = generation.get("reference_window_start_ms"), generation.get("reference_window_end_ms")
            provider, model = generation.get("provider"), generation.get("model")
            if any(isinstance(value, bool) or not isinstance(value, int) for value in (start, end, window_start, window_end)) or not isinstance(provider, str) or not isinstance(model, str):
                raise TalkingRunAssemblyError("TalkingRun child provenance is incomplete")
            evidence.append(TalkingRunChildEvidence(series_index=index, job_id=job.id, output_asset_id=output.id,
                master_start_ms=start, master_end_ms=end, reference_window_start_ms=window_start,
                reference_window_end_ms=window_end, provider=provider, model=model,
                provider_version=generation.get("provider_version") if isinstance(generation.get("provider_version"), str) else None))
        return evidence

    @staticmethod
    def _reference_clip_id(jobs: list[Job | None]) -> UUID:
        values = {job.payload.reference_clip_id for job in jobs if job is not None and isinstance(job.payload, TalkingGenerationJobPayload)}
        if len(values) != 1:
            raise TalkingRunAssemblyError("TalkingRun children must use one authorized reference Clip")
        return values.pop()

    @staticmethod
    def _require_source_forward(jobs: list[Job | None], reference_clip_id: UUID) -> None:
        previous_start = -1
        for index, job in enumerate(jobs):
            if job is None or not isinstance(job.payload, TalkingGenerationJobPayload):
                raise TalkingRunAssemblyError("TalkingRun child job is unavailable")
            payload = job.payload
            if payload.reference_clip_id != reference_clip_id or payload.slice_series_index != index or payload.reference_window_start_ms is None:
                raise TalkingRunAssemblyError("TalkingRun source-forward child ordering is not preserved")
            if payload.reference_window_start_ms < previous_start:
                raise TalkingRunAssemblyError("TalkingRun children restart the source reference instead of moving forward")
            previous_start = payload.reference_window_start_ms

    @staticmethod
    def _authorization(jobs: list[Job | None]) -> str:
        values = {job.payload.authorization_reference for job in jobs if job is not None and isinstance(job.payload, TalkingGenerationJobPayload)}
        if len(values) != 1:
            raise TalkingRunAssemblyError("TalkingRun children must share an authorization record")
        return values.pop()

    def _assemble_media(self, assets: list[Asset | None], master: AudioAsset, start_ms: int, end_ms: int, series_id: UUID) -> Path:
        child_paths = [None if asset is None else self._resolve_asset_path(asset.source_file) for asset in assets]
        master_path = self._resolve_asset_path(master.source_file)
        if any(path is None or not path.is_file() for path in child_paths) or not master_path.is_file():
            raise TalkingRunAssemblyError("TalkingRun assembly media is unavailable locally")
        self.output_root.mkdir(parents=True, exist_ok=True)
        duration = (end_ms - start_ms) / 1000
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", dir=self.output_root, delete=False) as listing:
            list_path = Path(listing.name)
            for path in child_paths:
                assert path is not None
                listing.write("file '" + str(path.resolve()).replace("'", "'\\\\''") + "'\n")
        output = self.output_root / f"talking-run-{series_id}.mp4"
        command = [self.ffmpeg_command, "-y", "-f", "concat", "-safe", "0", "-i", str(list_path),
                   "-ss", f"{start_ms / 1000:.3f}", "-t", f"{duration:.3f}", "-i", str(master_path.resolve()),
                   "-map", "0:v:0", "-map", "1:a:0", "-t", f"{duration:.3f}", "-c:v", "libx264", "-c:a", "aac", str(output)]
        try:
            completed = subprocess.run(command, shell=False, capture_output=True, text=True, timeout=300, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TalkingRunAssemblyError("TalkingRun FFmpeg assembly failed") from exc
        finally:
            list_path.unlink(missing_ok=True)
        if completed.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
            output.unlink(missing_ok=True)
            raise TalkingRunAssemblyError("TalkingRun FFmpeg assembly did not produce output")
        return output

    def _resolve_asset_path(self, source_file: str) -> Path:
        """Resolve current and legacy portable asset paths below the data root.

        Earlier local databases stored paths relative to ``services/api`` as
        ``../../content-os-data/...``.  Keep those retained originals usable
        after a checkout moves, while never accepting a path outside the
        application-owned data root for a relative asset reference.
        """

        path = Path(source_file)
        if path.is_absolute():
            return path
        data_root = Path(getattr(self.importer, "data_root", self.output_root)).resolve()
        parts = path.parts
        for index, part in enumerate(parts):
            if part.casefold() == data_root.name.casefold():
                return data_root.joinpath(*parts[index + 1:])
        return data_root / path


__all__ = ["TalkingRunAssembler", "TalkingRunAssemblyError"]
