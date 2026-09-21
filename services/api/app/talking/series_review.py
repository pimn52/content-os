"""Read-only readiness assessment for an ordered Talking slice series."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence
from uuid import UUID

from app.domain.models import Asset, Job, JobStatus, SourceKind, TalkingGenerationJobPayload, TalkingSliceSeries


@dataclass(frozen=True)
class TalkingSliceSeriesChildStatus:
    series_index: int
    job_id: UUID
    job_status: JobStatus | None
    output_asset_id: UUID | None
    automated_qa_state: str | None
    human_review_state: str | None
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class TalkingSliceSeriesReview:
    series_id: UUID
    readiness: str
    ready_for_human_continuity_review: bool
    children: tuple[TalkingSliceSeriesChildStatus, ...]
    blockers: tuple[str, ...]


def assess_talking_slice_series(
    series: TalkingSliceSeries,
    jobs: Sequence[Job | None],
    assets: Sequence[Asset],
) -> TalkingSliceSeriesReview:
    """Report evidence prerequisites without approving or composing anything."""

    if len(jobs) != len(series.child_job_ids):
        raise ValueError("Talking slice series review requires every recorded child job")
    outputs_by_job: dict[str, list[Asset]] = {}
    for asset in assets:
        generation = asset.metadata.get("talking_generation")
        job_id = generation.get("job_id") if isinstance(generation, dict) else None
        if asset.source_kind is SourceKind.AI_VIDEO and isinstance(job_id, str):
            outputs_by_job.setdefault(job_id, []).append(asset)

    children: list[TalkingSliceSeriesChildStatus] = []
    series_blockers: list[str] = []
    for expected_index, (expected_job_id, job) in enumerate(zip(series.child_job_ids, jobs)):
        blockers: list[str] = []
        output: Asset | None = None
        automated_qa_state: str | None = None
        human_review_state: str | None = None
        if job is None:
            blockers.append("child_job_missing")
        elif (
            job.id != expected_job_id
            or not isinstance(job.payload, TalkingGenerationJobPayload)
            or job.payload.slice_series_id != series.id
            or job.payload.slice_series_index != expected_index
        ):
            blockers.append("child_job_series_provenance_mismatch")
        elif job.status is JobStatus.FAILED or job.status is JobStatus.CANCELLED:
            blockers.append(f"child_job_{job.status.value}")
        elif job.status is not JobStatus.COMPLETED:
            blockers.append("child_job_not_completed")
        else:
            outputs = outputs_by_job.get(str(job.id), [])
            if len(outputs) != 1:
                blockers.append("child_output_missing" if not outputs else "child_output_ambiguous")
            else:
                output = outputs[0]
                generation = output.metadata.get("talking_generation")
                if isinstance(generation, dict):
                    automated_qa_state = generation.get("qa_state") if isinstance(generation.get("qa_state"), str) else None
                    human_review_state = generation.get("human_review_state") if isinstance(generation.get("human_review_state"), str) else None
                if automated_qa_state != "verified":
                    blockers.append("child_automated_talking_qa_not_verified")
                elif human_review_state != "approved":
                    blockers.append("child_human_talking_review_not_approved")
        if blockers:
            series_blockers.extend(f"slice_{expected_index}:{item}" for item in blockers)
        children.append(TalkingSliceSeriesChildStatus(
            series_index=expected_index,
            job_id=expected_job_id,
            job_status=None if job is None else job.status,
            output_asset_id=None if output is None else output.id,
            automated_qa_state=automated_qa_state,
            human_review_state=human_review_state,
            blockers=tuple(blockers),
        ))

    readiness = "ready_for_human_continuity_review" if not series_blockers else _readiness_from_blockers(series_blockers)
    return TalkingSliceSeriesReview(
        series_id=series.id,
        readiness=readiness,
        ready_for_human_continuity_review=not series_blockers,
        children=tuple(children),
        blockers=tuple(series_blockers),
    )


def _readiness_from_blockers(blockers: Sequence[str]) -> str:
    if any("failed" in item or "cancelled" in item or "mismatch" in item or "ambiguous" in item for item in blockers):
        return "blocked"
    if any("not_completed" in item or "job_missing" in item for item in blockers):
        return "waiting_for_child_jobs"
    if any("output_missing" in item for item in blockers):
        return "waiting_for_child_outputs"
    if any("automated" in item for item in blockers):
        return "waiting_for_automated_clip_qa"
    return "waiting_for_human_clip_review"


__all__ = [
    "TalkingSliceSeriesChildStatus",
    "TalkingSliceSeriesReview",
    "assess_talking_slice_series",
]
