from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from app.db import AssetRepository, AudioAssetRepository, Database, IPProfileRepository, JobRepository, ProjectRepository, TalkingSliceSeriesRepository
from app.domain.models import Asset, AudioAsset, IPProfile, Job, JobStatus, JobType, Project, RationalFps, SourceKind, TalkingGenerationJobPayload, TalkingSliceSeries
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


def test_failed_child_recovery_is_bounded_and_preserves_original_job(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "series-recovery.sqlite"
    with Database(path) as db:
        profile = IPProfile(creator_name="Creator")
        IPProfileRepository(db).create(profile)
        project = Project(
            ip_profile_id=profile.id, title="Series", topic="Recovery",
            fps=RationalFps(numerator=25, denominator=1), created_at=NOW,
        )
        ProjectRepository(db).create(project)
        series = _series().model_copy(update={"project_id": project.id})
        first = _child(series, 0)
        failed = _child(series, 1, status=JobStatus.FAILED).model_copy(update={
            "attempt": 1, "error_code": "talking_invalid_request",
        })
        TalkingSliceSeriesRepository(db).create(series)
        JobRepository(db).create(first)
        JobRepository(db).create(failed)
        AssetRepository(db).create(_output(first, human=None))
        AudioAssetRepository(db).create(AudioAsset(
            id=series.narration_audio_id, source_file="master.wav", content_hash="a" * 64,
            duration_ms=2_000, sample_rate=24_000, channels=1,
            authorization_reference="consent", imported_at=NOW,
            metadata={"voice_generation": {"qa_state": "verified"}},
        ))
    monkeypatch.setattr("app.main.voice_human_review_status", lambda _audio: "approved")
    url = f"/projects/{project.id}/talking-slice-series/{series.id}/recover-failed-child"
    request = {"failed_job_id": str(failed.id), "evidence_reference": "v62:staging-validated"}
    with TestClient(create_app(path)) as client:
        assert client.post(url, json={**request, "failed_job_id": str(first.id)}).status_code == 422
        accepted = client.post(url, json=request)
        assert accepted.status_code == 201, accepted.text
        replacement_id = UUID(accepted.json()["id"])
        assert replacement_id != failed.id
        assert client.post(url, json=request).json()["id"] == str(replacement_id)
        assert client.post(url, json={**request, "evidence_reference": "conflict"}).status_code == 409
    with Database(path) as db:
        saved = TalkingSliceSeriesRepository(db).get(series.id)
        assert saved.child_job_ids == [first.id, replacement_id]
        assert saved.recovery_history[0].failed_job_id == failed.id
        assert saved.recovery_history[0].replacement_job_id == replacement_id
        assert JobRepository(db).get(failed.id) == failed
        assert JobRepository(db).get(replacement_id).payload == failed.payload
