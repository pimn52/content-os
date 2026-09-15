from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.budget import ProviderCallLedger
from app.db import AssetRepository, AudioAssetRepository, BudgetPolicyRepository, Database, IPProfileRepository, ProjectRepository, ProviderCallRepository, TalkingProfileRepository
from app.domain.models import Asset, AudioAsset, BudgetPolicy, Clip, ConsentRecord, GazeDirection, IPProfile, Job, JobStatus, JobType, Project, RationalFps, SourceKind, TalkingGenerationJobPayload, TalkingPerformanceBrief, TalkingProfile, TalkingReferenceAssessment
from app.jobs import JobRunner, JobStore
from app.jobs.handlers import TalkingGenerationJobHandler
from app.main import create_app
from app.media.ffprobe import ProbeMetadata
from app.providers.talking import TalkingReference, TalkingSynthesisResult
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
        narration_path = tmp_path / "verified.wav"
        narration_path.write_bytes(b"verified narration")
        narration = AudioAsset(
            source_file=str(narration_path), content_hash="a" * 64, duration_ms=1_000, sample_rate=24_000, channels=1,
            authorization_reference="voice-consent-1", imported_at=datetime.now(timezone.utc),
            metadata={"voice_generation": {"provider": "test-voice", "qa_state": "verified"}},
        )
        AudioAssetRepository(db).create(narration)
        with db.transaction():
            BudgetPolicyRepository(db).save(BudgetPolicy(project_id=project.id, allow_unknown_cost=False, updated_at=datetime.now(timezone.utc)))
        payload = TalkingGenerationJobPayload(
            project_id=project.id, talking_profile_id=profile.id, reference_clip_id=reference_clip.id,
            narration_audio_id=narration.id, authorization_reference="talking-consent-1",
        )
        now = datetime.now(timezone.utc)
        job = Job(project_id=project.id, type=JobType.GENERATE_TALKING, idempotency_key="talking-one", created_at=now, updated_at=now, payload=payload)
        JobStore(db).enqueue(job)

        class Provider:
            provider_name = "test-talking"
            model = "test-model"
            is_local = True

            def synthesize(self, received: TalkingProfile, received_narration: AudioAsset, reference: TalkingReference, output_path: Path) -> TalkingSynthesisResult:
                assert received.id == profile.id and received_narration.id == narration.id
                assert reference.clip_id == reference_clip.id and reference.source_path == reference_path
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(b"generated-video")
                return TalkingSynthesisResult(output_path, provider_version="test-version")

        class Importer:
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
            TalkingProfileRepository(db), AudioAssetRepository(db), AssetRepository(db), Importer(), Provider(),
            tmp_path / "generated", ProviderCallLedger(db)  # type: ignore[arg-type]
        )
        result = JobRunner(JobStore(db), {JobType.GENERATE_TALKING: handler}, worker_id="talking-worker", lease_duration=timedelta(minutes=1), max_attempts=2).run_once()
        assert result is not None and result.status is JobStatus.COMPLETED
        video = next(value for value in AssetRepository(db).list() if value.source_kind is SourceKind.AI_VIDEO)
        assert video.metadata["talking_generation"]["provider"] == "test-talking"
        assert video.metadata["talking_generation"]["reference_clip_id"] == str(reference_clip.id)
        assert video.metadata["talking_generation"]["qa_state"] == "pending"
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
        asset = Asset(source_file=str(reference_path), content_hash="h" * 64, duration_ms=5_000, width=720, height=1_280, fps=RationalFps(numerator=25, denominator=1), has_audio=True, authorization_reference="talking-consent-1", imported_at=datetime.now(timezone.utc))
        AssetRepository(db).create(asset)
        from app.db import ClipRepository
        reference_clip = ClipRepository(db).create(Clip(asset_id=asset.id, start_ms=0, end_ms=5_000, asset_duration_ms=5_000, talking_candidate=True, face_visibility=0.9, mouth_visibility=0.9))
        profile = TalkingProfile(name="Creator talking", provider="test-talking", reference_clip_ids=[reference_clip.id], consent=ConsentRecord(subject_name="Creator", basis="self", confirmed=True, confirmed_at=datetime.now(timezone.utc)), created_at=datetime.now(timezone.utc))
        TalkingProfileRepository(db).create(profile)
        verified = AudioAsset(source_file=str(tmp_path / "verified.wav"), content_hash="d" * 64, duration_ms=1_000, sample_rate=24_000, channels=1, authorization_reference="voice-consent-1", imported_at=datetime.now(timezone.utc), metadata={"voice_generation": {"provider": "test-voice", "qa_state": "verified"}})
        pending = verified.model_copy(update={"id": uuid4(), "content_hash": "e" * 64, "metadata": {"voice_generation": {"provider": "test-voice", "qa_state": "pending"}}})
        AudioAssetRepository(db).create(verified)
        AudioAssetRepository(db).create(pending)
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
