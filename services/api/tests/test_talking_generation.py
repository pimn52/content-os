from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import pytest

from app.budget import ProviderCallLedger
from app.db import AssetRepository, AudioAssetRepository, BudgetPolicyRepository, Database, IPProfileRepository, JobRepository, ProjectRepository, ProviderCallRepository, TalkingProfileRepository
from app.domain.models import Asset, AudioAsset, BudgetPolicy, Clip, ConsentRecord, GazeDirection, IPProfile, Job, JobStatus, JobType, Project, RationalFps, SourceKind, TalkingGenerationJobPayload, TalkingPerformanceBrief, TalkingProfile, TalkingReferenceAssessment, TranscriptSegment
from app.jobs import JobRunner, JobStore
from app.jobs.handlers import TalkingGenerationJobHandler, _resolve_local_asset_source_path
from app.main import create_app
from app.media.ffprobe import ProbeMetadata
from app.providers.talking import TalkingExecutionOptions, TalkingReference, TalkingSynthesisResult
from app.talking import select_talking_reference
from app.talking_qa import TalkingHumanReview, TalkingQaReport, apply_talking_human_review, apply_talking_qa, verify_talking_output


def test_talking_job_requires_verified_new_narration_and_persists_video_provenance(tmp_path: Path) -> None:
    db = Database(tmp_path / "talking.sqlite")
    try:
        ip = IPProfile(creator_name="Creator")
        IPProfileRepository(db).create(ip)
        project = Project(ip_profile_id=ip.id, title="Talking", topic="New words", fps=RationalFps(numerator=25, denominator=1), created_at=datetime.now(timezone.utc))
        ProjectRepository(db).create(project)
        reference_path = tmp_path / "reference.mp4"
        reference_path.write_bytes(b"authorized reference")
        reference_asset = Asset(
            source_file=str(reference_path), content_hash="r" * 64, duration_ms=5_000,
            width=720, height=1_280, fps=RationalFps(numerator=25, denominator=1), has_audio=True,
            authorization_reference="talking-consent-1", imported_at=datetime.now(timezone.utc),
        )
        AssetRepository(db).create(reference_asset)
        from app.db import ClipRepository
        reference_clip = ClipRepository(db).create(Clip(
            asset_id=reference_asset.id, start_ms=0, end_ms=5_000, asset_duration_ms=5_000,
            talking_candidate=True, face_visibility=0.9, mouth_visibility=0.9,
        ))
        profile = TalkingProfile(
            name="Creator talking", provider="test-talking", reference_clip_ids=[reference_clip.id],
            consent=ConsentRecord(subject_name="Creator", basis="self", confirmed=True, confirmed_at=datetime.now(timezone.utc)),
            created_at=datetime.now(timezone.utc),
        )
        TalkingProfileRepository(db).create(profile)
        data_root = tmp_path / "content-os-data"
        narration_path = data_root / "assets" / "audio-originals" / "verified.wav"
        narration_path.parent.mkdir(parents=True)
        narration_path.write_bytes(b"verified narration")
        narration = AudioAsset(
            source_file="content-os-data/assets/audio-originals/verified.wav", content_hash="a" * 64, duration_ms=1_000, sample_rate=24_000, channels=1,
            authorization_reference="voice-consent-1", imported_at=datetime.now(timezone.utc),
            metadata={"voice_generation": {"provider": "test-voice", "qa_state": "verified"}},
        )
        AudioAssetRepository(db).create(narration)
        with db.transaction():
            BudgetPolicyRepository(db).save(BudgetPolicy(project_id=project.id, allow_unknown_cost=False, updated_at=datetime.now(timezone.utc)))
        payload = TalkingGenerationJobPayload(
            project_id=project.id, talking_profile_id=profile.id, reference_clip_id=reference_clip.id,
            narration_audio_id=narration.id, authorization_reference="talking-consent-1",
            terminal_face_closeout=True,
            terminal_delivery_end_ms=1_000,
            execution_parameters={"provider_tail_context": 600},
            execution_parameter_sources={"provider_tail_context": "local_verified"},
            execution_profile_reference="talking:local:test-talking:test-model:test-runtime:test-machine",
            reference_window_start_ms=500,
            reference_window_end_ms=1_500,
        )
        now = datetime.now(timezone.utc)
        job = Job(project_id=project.id, type=JobType.GENERATE_TALKING, idempotency_key="talking-one", created_at=now, updated_at=now, payload=payload)
        JobStore(db).enqueue(job)

        class Provider:
            provider_name = "test-talking"
            model = "test-model"
            is_local = True

            def synthesize(self, received: TalkingProfile, received_narration: AudioAsset, reference: TalkingReference, output_path: Path, options: TalkingExecutionOptions) -> TalkingSynthesisResult:
                assert received.id == profile.id and received_narration.id == narration.id
                assert Path(received_narration.source_file) == narration_path
                assert reference.clip_id == reference_clip.id and reference.source_path == reference_path
                assert (reference.start_ms, reference.end_ms) == (500, 1_500)
                assert options.terminal_face_closeout is True
                assert options.terminal_delivery_end_ms == 1_000
                assert options.provider_parameters == {"provider_tail_context": 600}
                assert options.parameter_sources == {"provider_tail_context": "local_verified"}
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(b"generated-video")
                return TalkingSynthesisResult(output_path, provider_version="test-version")

        class Importer:
            def __init__(self, root: Path) -> None:
                self.data_root = root

            def import_path(self, source: Path, authorization_reference: str, *, source_kind: SourceKind) -> Asset:
                assert source.is_file() and authorization_reference == "talking-consent-1" and source_kind is SourceKind.AI_VIDEO
                value = Asset(
                    source_kind=source_kind, source_file=str(source), content_hash="b" * 64, duration_ms=1_000,
                    width=720, height=1_280, fps=RationalFps(numerator=25, denominator=1), has_audio=True,
                    authorization_reference=authorization_reference, imported_at=datetime.now(timezone.utc),
                )
                AssetRepository(db).create(value)
                return value

        handler = TalkingGenerationJobHandler(
            TalkingProfileRepository(db), AudioAssetRepository(db), AssetRepository(db), Importer(data_root), Provider(),
            tmp_path / "generated", ProviderCallLedger(db)  # type: ignore[arg-type]
        )
        result = JobRunner(JobStore(db), {JobType.GENERATE_TALKING: handler}, worker_id="talking-worker", lease_duration=timedelta(minutes=1), max_attempts=2).run_once()
        assert result is not None and result.status is JobStatus.COMPLETED
        video = next(value for value in AssetRepository(db).list() if value.source_kind is SourceKind.AI_VIDEO)
        assert video.metadata["talking_generation"]["provider"] == "test-talking"
        assert video.metadata["talking_generation"]["reference_clip_id"] == str(reference_clip.id)
        assert video.metadata["talking_generation"]["qa_state"] == "pending"
        assert video.metadata["talking_generation"]["terminal_face_closeout"] is True
        assert video.metadata["talking_generation"]["terminal_delivery_end_ms"] == 1_000
        assert video.metadata["talking_generation"]["execution_parameters"] == {"provider_tail_context": 600}
        call = ProviderCallRepository(db).list_for_project(project.id)[0]
        assert call.operation == "talking" and call.status == "completed" and call.estimated_cost.amount == Decimal("0")
    finally:
        db.close()


def test_talking_job_rejects_unverified_narration_before_provider_call(tmp_path: Path) -> None:
    db = Database(tmp_path / "talking-unverified.sqlite")
    try:
        profile = TalkingProfile(
            name="Creator talking", provider="test-talking", reference_clip_ids=[uuid4()],
            consent=ConsentRecord(subject_name="Creator", basis="self", confirmed=True, confirmed_at=datetime.now(timezone.utc)),
            created_at=datetime.now(timezone.utc),
        )
        TalkingProfileRepository(db).create(profile)
        narration = AudioAsset(
            source_file=str(tmp_path / "pending.wav"), content_hash="c" * 64, duration_ms=1_000, sample_rate=24_000, channels=1,
            authorization_reference="voice-consent-1", imported_at=datetime.now(timezone.utc),
            metadata={"voice_generation": {"provider": "test-voice", "qa_state": "pending"}},
        )
        AudioAssetRepository(db).create(narration)
        payload = TalkingGenerationJobPayload(project_id=uuid4(), talking_profile_id=profile.id, reference_clip_id=profile.reference_clip_ids[0], narration_audio_id=narration.id, authorization_reference="talking-consent-1")
        job = Job(project_id=payload.project_id, type=JobType.GENERATE_TALKING, status=JobStatus.RUNNING, idempotency_key="talking-pending", created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc), payload=payload)

        class Provider:
            provider_name = "test-talking"
            model = "test-model"
            def synthesize(self, *_: object) -> TalkingSynthesisResult:
                raise AssertionError("provider must not run")

        handler = TalkingGenerationJobHandler(TalkingProfileRepository(db), AudioAssetRepository(db), AssetRepository(db), object(), Provider(), tmp_path / "generated")  # type: ignore[arg-type]
        from app.jobs.runner import JobExecutionError
        try:
            handler(job)
        except JobExecutionError as exc:
            assert exc.code == "talking_narration_not_verified"
        else:
            raise AssertionError("unverified narration must be rejected")
    finally:
        db.close()


def test_talking_reference_portable_paths_resolve_from_data_root_not_worker_cwd(tmp_path: Path) -> None:
    data_root = tmp_path / "content-os-data"
    source = data_root / "assets" / "originals" / "reference.media"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"reference")

    resolved = _resolve_local_asset_source_path(
        "content-os-data/assets/originals/reference.media", data_root,
    )

    assert resolved == source
    assert _resolve_local_asset_source_path(str(source), data_root) == source
    assert _resolve_local_asset_source_path(
        "assets/originals/missing.media", data_root,
    ) == data_root / "assets" / "originals" / "missing.media"


def test_talking_slice_payload_requires_a_complete_resolved_execution_plan() -> None:
    common = {
        "project_id": uuid4(), "talking_profile_id": uuid4(), "reference_clip_id": uuid4(),
        "narration_audio_id": uuid4(), "authorization_reference": "talking-consent-1",
    }
    with pytest.raises(ValueError, match="requires indices"):
        TalkingGenerationJobPayload(**common, slice_start_segment_index=0)
    with pytest.raises(ValueError, match="end index"):
        TalkingGenerationJobPayload(
            **common, slice_start_segment_index=1, slice_end_segment_index=1,
            slice_max_duration_ms=4_460, slice_limit_source="local_verified",
        )
    with pytest.raises(ValueError, match="separate execution planning"):
        TalkingGenerationJobPayload(
            **common, slice_start_segment_index=0, slice_end_segment_index=1,
            slice_max_duration_ms=4_460, slice_limit_source="local_verified",
            terminal_face_closeout=True, terminal_delivery_end_ms=1,
        )


def test_talking_job_api_is_typed_idempotent_and_requires_verified_narration(tmp_path: Path) -> None:
    path = tmp_path / "talking-api.sqlite"
    db = Database(path)
    try:
        ip = IPProfile(creator_name="Creator")
        IPProfileRepository(db).create(ip)
        project = Project(ip_profile_id=ip.id, title="Talking", topic="New words", fps=RationalFps(numerator=25, denominator=1), created_at=datetime.now(timezone.utc))
        ProjectRepository(db).create(project)
        reference_path = tmp_path / "api-reference.mp4"
        reference_path.write_bytes(b"authorized reference")
        asset = Asset(source_file=str(reference_path), content_hash="h" * 64, duration_ms=7_000, width=720, height=1_280, fps=RationalFps(numerator=25, denominator=1), has_audio=True, authorization_reference="talking-consent-1", imported_at=datetime.now(timezone.utc))
        AssetRepository(db).create(asset)
        from app.db import ClipRepository
        reference_clip = ClipRepository(db).create(Clip(asset_id=asset.id, start_ms=0, end_ms=7_000, asset_duration_ms=7_000, talking_candidate=True, face_visibility=0.9, mouth_visibility=0.9))
        profile = TalkingProfile(name="Creator talking", provider="latentsync", reference_clip_ids=[reference_clip.id], consent=ConsentRecord(subject_name="Creator", basis="self", confirmed=True, confirmed_at=datetime.now(timezone.utc)), created_at=datetime.now(timezone.utc))
        TalkingProfileRepository(db).create(profile)
        unsupported_profile = profile.model_copy(update={"id": uuid4(), "provider": "not-admitted"})
        TalkingProfileRepository(db).create(unsupported_profile)
        verified = AudioAsset(source_file=str(tmp_path / "verified.wav"), content_hash="d" * 64, duration_ms=1_000, sample_rate=24_000, channels=1, authorization_reference="voice-consent-1", imported_at=datetime.now(timezone.utc), metadata={"voice_generation": {"provider": "test-voice", "qa_state": "verified"}}, transcript_segments=[TranscriptSegment(start_ms=0, end_ms=1_000, text="verified speech")], transcript_source="voice-qa:test-asr")
        pending = verified.model_copy(update={"id": uuid4(), "content_hash": "e" * 64, "metadata": {"voice_generation": {"provider": "test-voice", "qa_state": "pending"}}})
        series_narration = verified.model_copy(update={
            "id": uuid4(),
            "content_hash": "f" * 64,
            "duration_ms": 6_000,
            "transcript_segments": [
                TranscriptSegment(start_ms=0, end_ms=2_000, text="第一句"),
                TranscriptSegment(start_ms=2_100, end_ms=4_000, text="第二句"),
                TranscriptSegment(start_ms=4_100, end_ms=6_000, text="第三句"),
            ],
        })
        AudioAssetRepository(db).create(verified)
        AudioAssetRepository(db).create(pending)
        AudioAssetRepository(db).create(series_narration)
    finally:
        db.close()

    request = {"idempotency_key": "talking-api", "talking_profile_id": str(profile.id), "reference_clip_id": str(reference_clip.id), "narration_audio_id": str(verified.id), "authorization_reference": "talking-consent-1"}
    with TestClient(create_app(path)) as client:
        first = client.post(f"/projects/{project.id}/talking-jobs", json=request)
        assert first.status_code == 201
        repeated = client.post(f"/projects/{project.id}/talking-jobs", json=request)
        assert repeated.status_code == 201 and repeated.json()["id"] == first.json()["id"]
        assert client.post(f"/projects/{project.id}/talking-jobs", json={**request, "authorization_reference": "different"}).status_code == 409
        assert client.post(f"/projects/{project.id}/talking-jobs", json={**request, "idempotency_key": "talking-pending", "narration_audio_id": str(pending.id)}).status_code == 422
        slice_job = client.post(f"/projects/{project.id}/talking-jobs", json={
            **request,
            "idempotency_key": "talking-short-slice",
            "execution_machine_id": "asus-rtx3060-laptop-6gb",
            "slice_start_segment_index": 0,
            "slice_end_segment_index": 1,
        })
        assert slice_job.status_code == 201
        unknown_slice = client.post(f"/projects/{project.id}/talking-jobs", json={
            **request,
            "idempotency_key": "talking-short-slice-unknown",
            "execution_machine_id": "unverified-machine",
            "slice_start_segment_index": 0,
            "slice_end_segment_index": 1,
        })
        assert unknown_slice.status_code == 422
        assert "no verified short-Talking duration bound" in unknown_slice.json()["detail"]
        series_request = {
            "idempotency_key": "talking-series",
            "talking_profile_id": str(profile.id),
            "reference_clip_id": str(reference_clip.id),
            "narration_audio_id": str(series_narration.id),
            "authorization_reference": "talking-consent-1",
            "execution_machine_id": "asus-rtx3060-laptop-6gb",
        }
        series = client.post(f"/projects/{project.id}/talking-slice-series-jobs", json=series_request)
        assert series.status_code == 201
        assert len(series.json()["jobs"]) == 2
        assert series.json()["continuity_boundaries_ms"] == [[4_000, 4_100]]
        review = client.get(f"/projects/{project.id}/talking-slice-series/{series.json()['id']}")
        assert review.status_code == 200
        assert review.json()["readiness"] == "waiting_for_child_jobs"
        assert review.json()["ready_for_human_continuity_review"] is False
        replay = client.post(f"/projects/{project.id}/talking-slice-series-jobs", json=series_request)
        assert replay.status_code == 201
        assert replay.json()["id"] == series.json()["id"]
        assert replay.json()["jobs"] == series.json()["jobs"]
        assert client.post(f"/projects/{project.id}/talking-slice-series-jobs", json={
            **series_request, "authorization_reference": "different",
        }).status_code == 409
        terminal = client.post(f"/projects/{project.id}/talking-jobs", json={
            **request,
            "idempotency_key": "talking-terminal-closeout",
            "terminal_face_closeout": True,
            "execution_machine_id": "test-machine",
        })
        assert terminal.status_code == 201
        rejected = client.post(f"/projects/{project.id}/talking-jobs", json={
            **request,
            "idempotency_key": "talking-terminal-unsupported",
            "talking_profile_id": str(unsupported_profile.id),
            "terminal_face_closeout": True,
            "execution_machine_id": "test-machine",
        })
        assert rejected.status_code == 422
        assert "not yet adapted to Content OS terminal face-closeout protection" in rejected.json()["detail"]

    check_db = Database(path)
    try:
        stored = JobRepository(check_db).get(UUID(terminal.json()["id"]))
        assert stored is not None and isinstance(stored.payload, TalkingGenerationJobPayload)
        assert stored.payload.terminal_face_closeout is True
        assert stored.payload.terminal_delivery_end_ms == 1_000
        assert stored.payload.execution_parameters == {"trailing_silence_lookahead_ms": 600}
        assert stored.payload.execution_parameter_sources == {"trailing_silence_lookahead_ms": "provider_default"}
        assert stored.payload.execution_profile_reference == "talking:local:latentsync:LatentSync-1.5:local-compatibility:test-machine"
        stored_slice = JobRepository(check_db).get(UUID(slice_job.json()["id"]))
        assert stored_slice is not None and isinstance(stored_slice.payload, TalkingGenerationJobPayload)
        assert stored_slice.payload.slice_start_segment_index == 0
        assert stored_slice.payload.slice_end_segment_index == 1
        assert stored_slice.payload.slice_max_duration_ms == 4_460
        assert stored_slice.payload.slice_limit_source == "local_verified"
        series_jobs = [JobRepository(check_db).get(UUID(item["id"])) for item in series.json()["jobs"]]
        assert all(item is not None and isinstance(item.payload, TalkingGenerationJobPayload) for item in series_jobs)
        assert [item.payload.slice_series_index for item in series_jobs if item is not None] == [0, 1]
        assert {item.payload.slice_series_id for item in series_jobs if item is not None} == {UUID(series.json()["id"])}
        assert [(item.payload.reference_window_start_ms, item.payload.reference_window_end_ms) for item in series_jobs if item is not None] == [(0, 4_640), (4_100, 6_640)]
    finally:
        check_db.close()


def test_talking_slice_series_children_run_only_after_the_predecessor_completes(tmp_path: Path) -> None:
    db = Database(tmp_path / "series-order.sqlite")
    try:
        ip = IPProfile(creator_name="Creator")
        IPProfileRepository(db).create(ip)
        project = Project(ip_profile_id=ip.id, title="Series", topic="Order", fps=RationalFps(numerator=25, denominator=1), created_at=datetime.now(timezone.utc))
        ProjectRepository(db).create(project)
        series_id = uuid4()
        now = datetime.now(timezone.utc)
        children = [
            Job(
                project_id=project.id, type=JobType.GENERATE_TALKING, idempotency_key=f"series-order-{index}",
                created_at=now, updated_at=now,
                payload=TalkingGenerationJobPayload(
                    project_id=project.id, talking_profile_id=uuid4(), reference_clip_id=uuid4(),
                    narration_audio_id=uuid4(), authorization_reference="consent",
                    slice_start_segment_index=index, slice_end_segment_index=index + 1,
                    slice_max_duration_ms=4_460, slice_limit_source="local_verified",
                    slice_series_id=series_id, slice_series_index=index, slice_series_size=2,
                ),
            )
            for index in range(2)
        ]
        store = JobStore(db)
        for child in children:
            store.enqueue(child)
        first = store.claim("worker", timedelta(minutes=2), max_attempts=2, now=now)
        assert first is not None and first.id == children[0].id
        assert store.claim("other-worker", timedelta(minutes=2), max_attempts=2, now=now) is None
        assert store.complete(first.id, "worker", now=now + timedelta(seconds=1)) is not None
        second = store.claim("other-worker", timedelta(minutes=2), max_attempts=2, now=now + timedelta(seconds=2))
        assert second is not None and second.id == children[1].id
    finally:
        db.close()


def test_talking_reference_selection_requires_observed_end_gaze_and_clean_source() -> None:
    clip = Clip(
        start_ms=0, end_ms=5_000, asset_duration_ms=5_000, asset_id=uuid4(), talking_candidate=True,
        quality_score=0.9, face_visibility=0.9, mouth_visibility=0.9,
        talking_reference_assessment=TalkingReferenceAssessment(start_gaze=GazeDirection.CAMERA, end_gaze=GazeDirection.AWAY, expression_labels=["calm"], burned_in_subtitles=True, evidence_reference="human-review:sample"),
    )
    brief = TalkingPerformanceBrief(start_gaze=GazeDirection.CAMERA, end_gaze=GazeDirection.CAMERA, expression_labels=["calm"], require_no_burned_subtitles=True)
    gap = select_talking_reference([clip], brief)
    assert gap.requires_capture is True and gap.selected_clip_id is None
    assert "end gaze" in " ".join(gap.fits[0].reasons)
    assert "burned-in subtitles" in " ".join(gap.fits[0].reasons)
    suitable = clip.model_copy(update={"talking_reference_assessment": TalkingReferenceAssessment(start_gaze=GazeDirection.CAMERA, end_gaze=GazeDirection.CAMERA, expression_labels=["calm"], burned_in_subtitles=False, evidence_reference="human-review:frontal-clean")})
    selected = select_talking_reference([suitable], brief)
    assert selected.selected_clip_id == suitable.id and selected.requires_capture is False


def test_talking_reference_assessment_and_selection_api(tmp_path: Path) -> None:
    path = tmp_path / "reference-selection.sqlite"
    db = Database(path)
    try:
        source = tmp_path / "reference.mp4"
        source.write_bytes(b"reference")
        asset = Asset(source_file=str(source), content_hash="g" * 64, duration_ms=5_000, width=720, height=1_280, fps=RationalFps(numerator=30, denominator=1), authorization_reference="talking-consent-1", imported_at=datetime.now(timezone.utc))
        AssetRepository(db).create(asset)
        clip = Clip(asset_id=asset.id, start_ms=0, end_ms=5_000, asset_duration_ms=5_000, talking_candidate=True, quality_score=0.9, face_visibility=0.9, mouth_visibility=0.9)
        from app.db import ClipRepository
        ClipRepository(db).create(clip)
        profile = TalkingProfile(name="Creator talking", provider="test-talking", reference_clip_ids=[clip.id], consent=ConsentRecord(subject_name="Creator", basis="self", confirmed=True, confirmed_at=datetime.now(timezone.utc)), created_at=datetime.now(timezone.utc))
        TalkingProfileRepository(db).create(profile)
    finally:
        db.close()
    assessment = {"start_gaze": "camera", "end_gaze": "camera", "expression_labels": ["calm"], "burned_in_subtitles": False, "evidence_reference": "human-review:frontal-clean"}
    brief = {"start_gaze": "camera", "end_gaze": "camera", "expression_labels": ["calm"], "require_no_burned_subtitles": True, "minimum_reference_duration_ms": 3000}
    with TestClient(create_app(path)) as client:
        assert client.put(f"/clips/{clip.id}/talking-reference-assessment", json={"assessment": assessment}).status_code == 200
        selected = client.post(f"/talking-profiles/{profile.id}/reference-selection", json={"brief": brief})
        assert selected.status_code == 200
        assert selected.json()["selected_clip_id"] == str(clip.id)


def test_talking_qa_and_human_rejection_are_provider_neutral(tmp_path: Path) -> None:
    video = tmp_path / "generated.mp4"
    video.write_bytes(b"generated")
    asset = Asset(
        source_kind=SourceKind.AI_VIDEO, source_file=str(video), content_hash="j" * 64,
        duration_ms=1_000, width=720, height=1_280, fps=RationalFps(numerator=25, denominator=1), has_audio=True,
        authorization_reference="talking-consent-1", imported_at=datetime.now(timezone.utc),
        metadata={"talking_generation": {"provider": "test-talking", "qa_state": "pending"}},
    )
    verified = apply_talking_qa(asset, TalkingQaReport(automated_verified=True, checks=("automated_checks_passed_human_likeness_and_sync_review_required",), evidence_reference="local-evidence:talking-output.json"))
    assert verified.metadata["talking_generation"]["qa_state"] == "verified"
    rejected = apply_talking_human_review(verified, TalkingHumanReview(approved=False, evidence_reference="human-review:rejected", findings=("visible lip movement mismatched", "ending expression did not fit delivery")))
    assert rejected.metadata["talking_generation"]["human_review_state"] == "rejected"
    assert rejected.metadata["talking_generation"]["human_review"]["findings"] == ["visible lip movement mismatched", "ending expression did not fit delivery"]


def test_talking_output_qa_requires_real_playable_video_audio_and_matching_narration(tmp_path: Path) -> None:
    video = tmp_path / "generated.mp4"
    video.write_bytes(b"generated")
    asset = Asset(
        source_kind=SourceKind.AI_VIDEO, source_file=str(video), content_hash="k" * 64,
        duration_ms=1_000, width=720, height=1_280, fps=RationalFps(numerator=25, denominator=1), has_audio=True,
        authorization_reference="talking-consent-1", imported_at=datetime.now(timezone.utc),
        metadata={"talking_generation": {"provider": "latentsync", "qa_state": "pending"}},
    )
    narration = AudioAsset(
        source_file=str(tmp_path / "narration.wav"), content_hash="l" * 64, duration_ms=1_000,
        sample_rate=24_000, channels=1, authorization_reference="voice-consent-1",
        imported_at=datetime.now(timezone.utc),
        metadata={"voice_generation": {
            "provider": "omnivoice", "qa_state": "verified",
            "qa": {"qa_state": "verified", "copy_coverage": 1.0, "missing_token_count": 0, "duplicate_token_count": 0},
        }},
    )
    narration_path = Path(narration.source_file)
    narration_path.write_bytes(b"narration")

    class Probe:
        def probe(self, path: Path) -> ProbeMetadata:
            assert path == video
            return ProbeMetadata(
                duration_ms=1_020, width=720, height=1_280,
                fps=RationalFps(numerator=25, denominator=1), has_audio=True,
                metadata={"streams": [{"codec_type": "video"}, {"codec_type": "audio"}]},
            )

    report = verify_talking_output(asset, narration, Probe(), evidence_reference="qa:latentsync-output")
    assert report.automated_verified is True
    assert report.playable is True
    assert report.duration_drift_ms == 20
    assert report.copy_coverage == 1.0
    updated = apply_talking_qa(asset, report)
    assert updated.metadata["talking_generation"]["qa"]["duration_drift_ms"] == 20
    assert updated.metadata["talking_generation"]["qa"]["copy_coverage"] == 1.0
    assert updated.metadata["talking_generation"]["qa"]["missing_token_count"] == 0
    assert updated.metadata["talking_generation"]["qa"]["duplicate_token_count"] == 0

    class BadProbe(Probe):
        def probe(self, path: Path) -> ProbeMetadata:
            return ProbeMetadata(
                duration_ms=1_300, width=720, height=1_280,
                fps=RationalFps(numerator=25, denominator=1), has_audio=False,
                metadata={"streams": [{"codec_type": "video"}]},
            )

    failed = verify_talking_output(asset, narration, BadProbe(), evidence_reference="qa:latentsync-output")
    assert failed.automated_verified is False
    assert "output_not_playable_video_audio" in failed.checks
    assert "output_duration_drift" in failed.checks
