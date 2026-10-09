"""Shared persisted TalkingRun fixture for production eligibility tests."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.db import AssetRepository, AudioAssetRepository, Database, IPProfileRepository, ProjectRepository, TalkingRunRepository, TalkingSliceSeriesRepository
from app.domain.models import Asset, AudioAsset, Clip, IPProfile, Project, RationalFps, TalkingRun, TalkingRunChildEvidence, TalkingSliceSeries, TranscriptSegment


@pytest.fixture
def admit_talking_run():
    def admit(
        db: Database,
        project_id: UUID,
        asset: Asset,
        clip: Clip,
        copy: str,
        *,
        audio: AudioAsset | None = None,
        transcript_start_ms: int = 0,
        transcript_end_ms: int | None = None,
    ) -> tuple[Asset, TalkingRun, AudioAsset]:
        if ProjectRepository(db).get(project_id) is None:
            profile = IPProfile(creator_name="Fixture creator")
            IPProfileRepository(db).create(profile)
            ProjectRepository(db).create(Project(
                id=project_id, ip_profile_id=profile.id, title="Talking admission fixture",
                topic="Talking", fps=RationalFps(numerator=30, denominator=1),
                created_at=datetime.now(timezone.utc),
            ))
        if audio is None:
            audio = AudioAsset(
                source_file=str(Path(db.path).parent / f"{uuid4()}.wav"), content_hash=uuid4().hex * 2,
                duration_ms=asset.duration_ms, sample_rate=24_000, channels=1,
                authorization_reference="voice-consent", imported_at=datetime.now(timezone.utc),
                transcript_source="fixture-alignment",
                transcript_segments=[TranscriptSegment(
                    start_ms=transcript_start_ms,
                    end_ms=asset.duration_ms if transcript_end_ms is None else transcript_end_ms,
                    text=copy,
                )],
            )
            AudioAssetRepository(db).create(audio)
        series = TalkingSliceSeries(
            project_id=project_id, idempotency_key=f"fixture-{uuid4()}",
            request_fingerprint=uuid4().hex * 2, narration_audio_id=audio.id,
            child_job_ids=[uuid4()], created_at=datetime.now(timezone.utc),
        )
        TalkingSliceSeriesRepository(db).create(series)
        run = TalkingRun(
            project_id=project_id, series_id=series.id, master_narration_audio_id=audio.id,
            master_start_ms=0, master_end_ms=asset.duration_ms, authorized_reference_clip_id=uuid4(),
            child_evidence=[TalkingRunChildEvidence(
                series_index=0, job_id=series.child_job_ids[0], output_asset_id=asset.id,
                master_start_ms=0, master_end_ms=asset.duration_ms,
                reference_window_start_ms=0, reference_window_end_ms=asset.duration_ms,
                provider="fixture", model="fixture",
            )],
            continuity_review_id=uuid4(), assembled_asset_id=asset.id, assembled_clip_id=clip.id,
            automated_qa_state="verified", admission_state="admitted",
            created_at=datetime.now(timezone.utc),
        )
        TalkingRunRepository(db).create(run)
        metadata = dict(asset.metadata)
        metadata["talking_run"] = {
            "run_id": str(run.id), "series_id": str(run.series_id),
            "master_narration_audio_id": str(audio.id), "master_start_ms": 0,
            "master_end_ms": asset.duration_ms, "continuity_review_id": str(run.continuity_review_id),
            "admission_state": "admitted", "automated_qa_state": "verified",
            "continuity_review_state": "approved",
        }
        stored = AssetRepository(db).update(asset.model_copy(update={"metadata": metadata}))
        return stored, run, audio

    return admit
