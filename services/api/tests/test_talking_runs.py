from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.db import AssetRepository, AudioAssetRepository, ClipRepository, Database, IPProfileRepository, JobRepository, ProjectRepository, TalkingRunRepository, TalkingSliceSeriesContinuityReviewRepository, TalkingSliceSeriesRepository
from app.domain.models import Asset, AudioAsset, Clip, IPProfile, Job, JobStatus, JobType, Project, RationalFps, SourceKind, TalkingGenerationJobPayload, TalkingSliceSeries, TalkingSliceSeriesContinuityReview
from app.talking.runs import TalkingRunAssembler


NOW = datetime(2026, 9, 21, tzinfo=timezone.utc)


def test_reviewed_series_becomes_one_admitted_asset_and_clip_without_child_audio(tmp_path: Path) -> None:
    db = Database(tmp_path / "talking-run.sqlite")
    try:
        profile = IPProfile(creator_name="Creator")
        IPProfileRepository(db).create(profile)
        project = Project(ip_profile_id=profile.id, title="Run", topic="New words", fps=RationalFps(numerator=25, denominator=1), created_at=NOW)
        ProjectRepository(db).create(project)
        master = AudioAsset(source_file=str(tmp_path / "master.wav"), content_hash="a" * 64, duration_ms=2_000, sample_rate=24_000, channels=1, authorization_reference="voice-consent", imported_at=NOW, metadata={"voice_generation": {"qa_state": "verified"}})
        AudioAssetRepository(db).create(master)
        series = TalkingSliceSeries(project_id=project.id, idempotency_key="run-series", request_fingerprint="b" * 64, narration_audio_id=master.id, child_job_ids=[uuid4(), uuid4()], created_at=NOW)
        TalkingSliceSeriesRepository(db).create(series)
        outputs: list[Asset] = []
        for index, (start, end, window_start, window_end) in enumerate(((0, 1_000, 0, 1_300), (1_000, 2_000, 1_000, 2_300))):
            job = Job(id=series.child_job_ids[index], project_id=project.id, type=JobType.GENERATE_TALKING, status=JobStatus.COMPLETED, idempotency_key=f"run-child-{index}", created_at=NOW, updated_at=NOW, payload=TalkingGenerationJobPayload(project_id=project.id, talking_profile_id=uuid4(), reference_clip_id=uuid4(), narration_audio_id=master.id, authorization_reference="talking-consent", slice_start_segment_index=index, slice_end_segment_index=index + 1, slice_max_duration_ms=4_460, slice_limit_source="local_verified", reference_window_start_ms=window_start, reference_window_end_ms=window_end, slice_series_id=series.id, slice_series_index=index, slice_series_size=2))
            # The test requires one shared authorized source clip.
            job = job.model_copy(update={"payload": job.payload.model_copy(update={"reference_clip_id": series.id})})
            JobRepository(db).create(job)
            output = Asset(source_kind=SourceKind.AI_VIDEO, source_file=str(tmp_path / f"child-{index}.mp4"), content_hash=(str(index + 1) * 64), duration_ms=1_000, width=720, height=1280, fps=RationalFps(numerator=25, denominator=1), has_audio=True, authorization_reference="talking-consent", imported_at=NOW, metadata={"talking_generation": {"job_id": str(job.id), "qa_state": "verified", "human_review_state": "approved", "master_slice_start_ms": start, "master_slice_end_ms": end, "reference_window_start_ms": window_start, "reference_window_end_ms": window_end, "provider": "latentsync", "model": "LatentSync-1.5"}})
            AssetRepository(db).create(output)
            outputs.append(output)
        review = TalkingSliceSeriesContinuityReview(project_id=project.id, series_id=series.id, approved=True, evidence_reference="human-review:e21", child_job_ids=series.child_job_ids, reviewed_at=NOW)
        TalkingSliceSeriesContinuityReviewRepository(db).create(review)
        assembled = Asset(source_kind=SourceKind.AI_VIDEO, source_file=str(tmp_path / "assembled.mp4"), content_hash="c" * 64, duration_ms=2_000, width=720, height=1280, fps=RationalFps(numerator=25, denominator=1), has_audio=True, authorization_reference="talking-consent", imported_at=NOW)
        AssetRepository(db).create(assembled)

        class Importer:
            def import_path(self, source: Path, authorization_reference: str, *, source_kind: SourceKind) -> Asset:
                assert source_kind is SourceKind.AI_VIDEO and authorization_reference == "talking-consent"
                return assembled

        assembler = TalkingRunAssembler(assets=AssetRepository(db), audios=AudioAssetRepository(db), clips=ClipRepository(db), jobs=JobRepository(db), reviews=TalkingSliceSeriesContinuityReviewRepository(db), runs=TalkingRunRepository(db), importer=Importer(), output_root=tmp_path)
        output = tmp_path / "joined.mp4"
        output.write_bytes(b"assembled")
        assembler._assemble_media = lambda *_args: output  # type: ignore[method-assign]
        run = assembler.assemble(series)

        assert run.assembled_asset_id == assembled.id
        assert run.assembled_clip_id == ClipRepository(db).list_by_asset(assembled.id)[0].id
        assert run.master_narration_audio_id == master.id
        assert [child.output_asset_id for child in run.child_evidence] == [asset.id for asset in outputs]
        metadata = AssetRepository(db).get(assembled.id).metadata["talking_run"]  # type: ignore[union-attr]
        assert metadata["admission_state"] == "admitted"
        assert metadata["master_narration_audio_id"] == str(master.id)
        assert "child_audio" not in metadata
    finally:
        db.close()


def test_assembler_resolves_legacy_portable_paths_below_data_root(tmp_path: Path) -> None:
    class Importer:
        data_root = tmp_path / "content-os-data"

    assembler = TalkingRunAssembler(
        assets=None, audios=None, clips=None, jobs=None, reviews=None, runs=None,  # type: ignore[arg-type]
        importer=Importer(), output_root=tmp_path / "output",
    )

    assert assembler._resolve_asset_path("..\\..\\content-os-data\\assets\\originals\\child.media") == (
        Importer.data_root / "assets" / "originals" / "child.media"
    )
    assert assembler._resolve_asset_path("content-os-data/assets/originals/child.media") == (
        Importer.data_root / "assets" / "originals" / "child.media"
    )
