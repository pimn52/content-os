"""Completed-render output binding for publishing and learning."""
from __future__ import annotations

from pathlib import Path
from uuid import UUID

from app.db import FeedbackRepository, JobRepository, PublicationRepository
from app.domain.models import Job, JobStatus, JobType, RenderVideoJobPayload


class PublicationOutputError(ValueError):
    pass


def output_version(job: Job) -> str:
    if job.type is not JobType.RENDER or not isinstance(job.payload, RenderVideoJobPayload):
        raise PublicationOutputError("publication_output_invalid")
    return f"render:{job.payload.render_id}"


def _render_path(root: Path, project_id: UUID, job: Job) -> Path:
    return Path(root) / str(project_id) / f"{job.payload.render_id}.mp4"


def _duration_ms(job: Job) -> int:
    spec = job.payload.video_spec
    return sum(
        (scene.duration_frames * 1000 * spec.fps.denominator + spec.fps.numerator - 1)
        // spec.fps.numerator
        for scene in spec.scenes
    )


def publishable_outputs(db, project_id: UUID, render_root: str | Path) -> list[dict[str, object]]:
    root = Path(render_root).resolve()
    publications = PublicationRepository(db).list_for_project(project_id)
    published_versions = {value.output_version for value in publications}
    feedback_versions = {value.output_version for value in FeedbackRepository(db).list_for_project(project_id)}
    values = []
    for job in reversed(JobRepository(db).list_for_project(project_id)):
        if job.type is not JobType.RENDER or job.status is not JobStatus.COMPLETED:
            continue
        try:
            version = output_version(job)
        except PublicationOutputError:
            continue
        path = _render_path(root, project_id, job).resolve()
        available = path.is_relative_to(root) and path.is_file() and path.stat().st_size > 0
        run = db.connection.execute(
            "SELECT id FROM production_runs WHERE project_id=? AND render_job_id=? ORDER BY rowid DESC LIMIT 1",
            (str(project_id), str(job.id)),
        ).fetchone()
        spec = job.payload.video_spec
        values.append({
            "project_id": project_id,
            "output_version": version,
            "render_id": job.payload.render_id,
            "render_job_id": job.id,
            "run_id": None if run is None else UUID(run["id"]),
            "completed_at": job.updated_at,
            "width": spec.width,
            "height": spec.height,
            "duration_ms": _duration_ms(job),
            "available": available,
            "published": version in published_versions,
            "feedback_recorded": version in feedback_versions,
        })
    return values


def require_publishable_output(
    db, project_id: UUID, version: str, render_root: str | Path, *, require_file: bool = True,
) -> dict[str, object]:
    value = next(
        (item for item in publishable_outputs(db, project_id, render_root) if item["output_version"] == version),
        None,
    )
    if value is None:
        raise PublicationOutputError("publication_output_not_completed_render")
    if require_file and not value["available"]:
        raise PublicationOutputError("publication_output_file_unavailable")
    return value
