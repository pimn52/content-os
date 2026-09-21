from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from app.db import AssetRepository, Database, IPProfileRepository, JobRepository, ProjectRepository, TalkingSliceSeriesRepository
from app.domain.models import Asset, IPProfile, Job, JobStatus, JobType, Project, RationalFps, SourceKind, TalkingGenerationJobPayload, TalkingSliceSeries
from app.main import create_app
from app.talking.series_review import assess_talking_slice_series


NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)


def _series() -> TalkingSliceSeries:
    return TalkingSliceSeries(
        project_id=uuid4(),
        idempotency_key="series-review",
        request_fingerprint="a" * 64,
        narration_audio_id=uuid4(),
        child_job_ids=[uuid4(), uuid4()],
        created_at=NOW,
    )


def _child(series: TalkingSliceSeries, index: int, *, status: JobStatus = JobStatus.COMPLETED) -> Job:
    return Job(
        id=series.child_job_ids[index],
        project_id=series.project_id,
        type=JobType.GENERATE_TALKING,
        status=status,
        idempotency_key=f"series-review-{index}",
        created_at=NOW,
        updated_at=NOW,
        payload=TalkingGenerationJobPayload(
            project_id=series.project_id,
            talking_profile_id=uuid4(),
            reference_clip_id=uuid4(),
            narration_audio_id=series.narration_audio_id,
            authorization_reference="consent",
            slice_start_segment_index=index,
            slice_end_segment_index=index + 1,
            slice_max_duration_ms=4_460,
            slice_limit_source="local_verified",
            slice_series_id=series.id,
            slice_series_index=index,
            slice_series_size=2,
        ),
    )


def _output(job: Job, *, automated: str = "verified", human: str | None = "approved") -> Asset:
    generation: dict[str, object] = {"job_id": str(job.id), "qa_state": automated}
    if human is not None:
        generation["human_review_state"] = human
    return Asset(
        source_kind=SourceKind.AI_VIDEO,
        source_file=f"output-{job.id}.mp4",
        content_hash=uuid4().hex * 2,
        duration_ms=1_000,
        width=720,
        height=1_280,
        fps=RationalFps(numerator=25, denominator=1),
        has_audio=True,
        authorization_reference="consent",
        imported_at=NOW,
        metadata={"talking_generation": generation},
    )


def test_series_review_requires_completed_unique_qa_and_individually_approved_children() -> None:
    series = _series()
    first, second = _child(series, 0), _child(series, 1, status=JobStatus.PENDING)

    waiting = assess_talking_slice_series(series, [first, second], [_output(first)])
    assert waiting.readiness == "waiting_for_child_jobs"
    assert waiting.ready_for_human_continuity_review is False
    assert waiting.children[1].blockers == ("child_job_not_completed",)

    second = _child(series, 1)
    automatic_wait = assess_talking_slice_series(series, [first, second], [_output(first), _output(second, human=None)])
    assert automatic_wait.readiness == "waiting_for_human_clip_review"
    assert automatic_wait.children[1].blockers == ("child_human_talking_review_not_approved",)

    ready = assess_talking_slice_series(series, [first, second], [_output(first), _output(second)])
    assert ready.readiness == "ready_for_human_continuity_review"
    assert ready.ready_for_human_continuity_review is True

    ambiguous = assess_talking_slice_series(series, [first, second], [_output(first), _output(first), _output(second)])
    assert ambiguous.readiness == "blocked"
    assert "slice_0:child_output_ambiguous" in ambiguous.blockers


def test_continuity_decision_is_gated_durable_and_immutable(tmp_path: Path) -> None:
    path = tmp_path / "series-continuity.sqlite"
    db = Database(path)
    try:
        profile = IPProfile(creator_name="Creator")
        IPProfileRepository(db).create(profile)
        project = Project(
            ip_profile_id=profile.id, title="Series", topic="Continuity",
            fps=RationalFps(numerator=25, denominator=1), created_at=NOW,
        )
        ProjectRepository(db).create(project)
        series = _series().model_copy(update={"project_id": project.id})
        first, second = _child(series, 0), _child(series, 1)
        TalkingSliceSeriesRepository(db).create(series)
        JobRepository(db).create(first)
        JobRepository(db).create(second)
    finally:
        db.close()

    request = {
        "approved": True,
        "evidence_reference": "human-review:series-continuity-01",
        "findings": ["transitions read as continuous"],
    }
    with TestClient(create_app(path)) as client:
        not_ready = client.post(
            f"/projects/{project.id}/talking-slice-series/{series.id}/continuity-review",
            json=request,
        )
        assert not_ready.status_code == 422

    db = Database(path)
    try:
        AssetRepository(db).create(_output(first))
        AssetRepository(db).create(_output(second))
    finally:
        db.close()

    with TestClient(create_app(path)) as client:
        accepted = client.post(
            f"/projects/{project.id}/talking-slice-series/{series.id}/continuity-review",
            json=request,
        )
        assert accepted.status_code == 201
        assert accepted.json()["approved"] is True
        assert accepted.json()["child_job_ids"] == [str(first.id), str(second.id)]
        fetched = client.get(
            f"/projects/{project.id}/talking-slice-series/{series.id}/continuity-review",
        )
        assert fetched.status_code == 200
        assert fetched.json() == accepted.json()
        listed = client.get(f"/projects/{project.id}/talking-slice-series")
        assert listed.status_code == 200
        assert len(listed.json()) == 1
        assert listed.json()[0]["continuity_review_state"] == "approved"
        assert listed.json()[0]["continuity_review_findings"] == request["findings"]
        replay = client.post(
            f"/projects/{project.id}/talking-slice-series/{series.id}/continuity-review",
            json=request,
        )
        assert replay.status_code == 201
        assert replay.json()["id"] == accepted.json()["id"]
        conflict = client.post(
            f"/projects/{project.id}/talking-slice-series/{series.id}/continuity-review",
            json={**request, "approved": False},
        )
        assert conflict.status_code == 409
