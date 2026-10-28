from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import pytest
from test_talking_frame_qa import FrameProbeFixture

from app.budget import ProviderCallLedger
from app.db import AssetRepository, AudioAssetRepository, BudgetPolicyRepository, Database, IPProfileRepository, JobRepository, ProjectRepository, ProviderCallRepository, TalkingProfileRepository
from app.domain.models import Asset, AudioAsset, BudgetPolicy, Clip, ConsentRecord, GazeDirection, IPProfile, Job, JobStatus, JobType, Project, RationalFps, SourceKind, TalkingGenerationJobPayload, TalkingPerformanceBrief, TalkingProfile, TalkingReferenceAssessment, TranscriptSegment
from app.jobs import JobRunner, JobStore
from app.jobs.handlers import TalkingGenerationJobHandler, _resolve_local_asset_source_path
from app.jobs.runner import JobExecutionError
from app.main import create_app
from app.media.ffprobe import ProbeMetadata
from app.providers.talking import TalkingExecutionOptions, TalkingReference, TalkingSynthesisResult
from app.talking import select_talking_reference
from app.talking_qa import TalkingHumanReview, TalkingQaError, TalkingQaReport, apply_talking_human_review, apply_talking_qa, verify_talking_output


@pytest.fixture(autouse=True)
def legacy_talking_evaluation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONTENT_OS_ALLOW_LEGACY_TALKING_EVALUATION", "1")


def test_preexisting_direct_talking_job_requires_real_profile_consent_before_provider_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CONTENT_OS_ALLOW_LEGACY_TALKING_EVALUATION")
    with Database(tmp_path / "legacy-disabled.sqlite") as db:
        now = datetime.now(timezone.utc)
        project_id = uuid4()
        job = Job(
            project_id=project_id, type=JobType.GENERATE_TALKING, status=JobStatus.RUNNING,
            idempotency_key="previously-queued", created_at=now, updated_at=now,
            payload=TalkingGenerationJobPayload(
                project_id=project_id, talking_profile_id=uuid4(), reference_clip_id=uuid4(),
                narration_audio_id=uuid4(), authorization_reference="caller-asserted",
            ),
        )
        handler = TalkingGenerationJobHandler(
            TalkingProfileRepository(db), AudioAssetRepository(db), AssetRepository(db),
            object(), object(), tmp_path / "generated", ProviderCallLedger(db),  # type: ignore[arg-type]
        )
        with pytest.raises(JobExecutionError, match="consented talking profile") as error:
            handler(job)
        assert error.value.code == "talking_profile_not_authorized"
        assert ProviderCallRepository(db).list_for_project(job.project_id) == []


@pytest.mark.parametrize("reference_fits", [True, False])
def test_talking_job_requires_verified_new_narration_and_persists_video_provenance(tmp_path: Path, reference_fits) -> None:
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
            metadata={"voice_generation": {
                "provider": "test-voice",
                "qa_state": "verified",
                "human_review_state": "approved",
                "human_review": {
                    "approved": True,
                    "evidence_reference": "u-voice:talking-handler-fixture",
                    "findings": ["Narration is approved for Talking fixture execution."],
                    "likeness": "pass",
                    "naturalness": "pass",
                    "emphasis": "pass",
                    "pace": "pass",
                    "pauses": "pass",
                    "rhythm": "pass",
                    "reviewed_at": datetime.now(timezone.utc).isoformat(),
                },
            }},
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

            def validate_reference_duration(self, narration_duration_ms, reference_duration_ms):
                assert (narration_duration_ms,reference_duration_ms)==(1000,1000)
                if not reference_fits:
                    from app.providers.talking import TalkingInputError
                    raise TalkingInputError("synthetic frame alignment exceeds reference")

            def synthesize(self, received: TalkingProfile, received_narration: AudioAsset, reference: TalkingReference, output_path: Path, options: TalkingExecutionOptions) -> TalkingSynthesisResult:
                assert reference_fits, "不能在参考覆盖不足时调用提供商"
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
                return TalkingSynthesisResult(output_path, provider_version="test-version", expected_video_fps=RationalFps(numerator=25, denominator=1))

        class Importer:
            def __init__(self, root: Path) -> None:
                self.data_root = root

            def import_path(self, source: Path, authorization_reference: str, *, source_kind: SourceKind, generated_job=None) -> Asset:
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
        if not reference_fits:
            assert result is not None and result.status is JobStatus.FAILED
            assert result.error_code=="talking_reference_alignment_not_covered"
            assert ProviderCallRepository(db).list_for_project(project.id)==[]
            return
        assert result is not None and result.status is JobStatus.COMPLETED
        video = next(value for value in AssetRepository(db).list() if value.source_kind is SourceKind.AI_VIDEO)
        assert video.metadata["talking_generation"]["provider"] == "test-talking"
        assert video.metadata["talking_generation"]["reference_clip_id"] == str(reference_clip.id)
        assert video.metadata["talking_generation"]["qa_state"] == "pending"
        assert RationalFps.model_validate(video.metadata["talking_generation"]["expected_video_fps"]) == RationalFps(numerator=25, denominator=1)
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


def test_removed_direct_talking_endpoints_cannot_bypass_production_run(tmp_path: Path) -> None:
    path=tmp_path / "ordinary-only.sqlite3"
    with TestClient(create_app(path)) as client:
        for route in ("talking-jobs", "talking-slice-series-jobs"):
            response=client.post(f"/projects/{uuid4()}/{route}",json={})
            assert response.status_code==404
    with Database(path) as db:
        assert JobRepository(db).list()==[]
        assert ProviderCallRepository(db).list()==[]


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
    with pytest.raises(TalkingQaError, match="already recorded"):
        apply_talking_human_review(rejected, TalkingHumanReview(
            approved=True, evidence_reference="human-review:overwrite", findings=("must not overwrite",),
        ))


def test_talking_human_review_api_is_project_scoped_qa_gated_and_immutable(tmp_path: Path) -> None:
    path = tmp_path / "talking-review.sqlite"
    db = Database(path)
    try:
        ip = IPProfile(creator_name="Creator")
        IPProfileRepository(db).create(ip)
        now = datetime.now(timezone.utc)
        project = Project(ip_profile_id=ip.id, title="Talking review", topic="New copy", fps=RationalFps(numerator=25, denominator=1), created_at=now)
        other = Project(ip_profile_id=ip.id, title="Other", topic="Other", fps=RationalFps(numerator=25, denominator=1), created_at=now)
        ProjectRepository(db).create(project)
        ProjectRepository(db).create(other)
        narration_id = uuid4()
        job = Job(
            project_id=project.id, type=JobType.GENERATE_TALKING, status=JobStatus.COMPLETED,
            idempotency_key="talking-review-job", created_at=now, updated_at=now,
            payload=TalkingGenerationJobPayload(
                project_id=project.id, talking_profile_id=uuid4(), reference_clip_id=uuid4(),
                narration_audio_id=narration_id, authorization_reference="consent",
            ),
        )
        JobRepository(db).create(job)
        asset = Asset(
            source_kind=SourceKind.AI_VIDEO, source_file=str(tmp_path / "talking.mp4"),
            content_hash="v" * 64, duration_ms=1_000, width=720, height=1280,
            fps=RationalFps(numerator=25, denominator=1), has_audio=True,
            authorization_reference="consent", imported_at=now,
            metadata={"talking_generation": {
                "job_id": str(job.id), "narration_audio_id": str(narration_id), "qa_state": "verified",
            }},
        )
        AssetRepository(db).create(asset)
        pending = asset.model_copy(update={
            "id": uuid4(), "content_hash": "p" * 64,
            "metadata": {"talking_generation": {
                "job_id": str(job.id), "narration_audio_id": str(narration_id), "qa_state": "pending",
            }},
        })
        AssetRepository(db).create(pending)
    finally:
        db.close()

    review = {"approved": True, "evidence_reference": "u-talking:ordered-viewing", "findings": ["Creator and visible lip sync passed"]}
    with TestClient(create_app(path)) as client:
        route = f"/projects/{project.id}/talking-assets/{asset.id}/human-review"
        assert client.post(f"/projects/{other.id}/talking-assets/{asset.id}/human-review", json=review).status_code == 422
        assert client.post(f"/projects/{project.id}/talking-assets/{pending.id}/human-review", json=review).status_code == 422
        accepted = client.post(route, json=review)
        assert accepted.status_code == 200
        assert accepted.json()["metadata"]["talking_generation"]["human_review_state"] == "approved"
        assert client.post(route, json={**review, "approved": False}).status_code == 422
    db = Database(path)
    try:
        saved = AssetRepository(db).get(asset.id)
        assert saved is not None
        assert saved.metadata["talking_generation"]["human_review"]["evidence_reference"] == review["evidence_reference"]
    finally:
        db.close()


def test_talking_output_qa_requires_real_playable_video_audio_and_matching_narration(tmp_path: Path) -> None:
    video = tmp_path / "generated.mp4"
    video.write_bytes(b"generated")
    asset = Asset(
        source_kind=SourceKind.AI_VIDEO, source_file=str(video), content_hash="k" * 64,
        duration_ms=1_000, width=720, height=1_280, fps=RationalFps(numerator=25, denominator=1), has_audio=True,
        authorization_reference="talking-consent-1", imported_at=datetime.now(timezone.utc),
        metadata={"talking_generation": {"provider": "latentsync", "qa_state": "pending",
            "expected_video_fps": {"numerator": 25, "denominator": 1}}},
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

    class Probe(FrameProbeFixture):
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
    longer_master = narration.model_copy(update={"duration_ms": 5_000})
    assert not verify_talking_output(asset, longer_master, Probe(), evidence_reference="qa:full-master").automated_verified
    slice_report = verify_talking_output(
        asset, longer_master, Probe(), evidence_reference="qa:planned-slice", expected_duration_ms=1_000,
    )
    assert slice_report.automated_verified and slice_report.duration_drift_ms == 20

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
