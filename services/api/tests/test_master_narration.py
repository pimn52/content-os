from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app.db import AudioAssetRepository, Database, IPProfileRepository, JobRepository, ProjectRepository
from app.domain.models import AudioAsset, IPProfile, Job, JobType, Project, RationalFps, TranscriptSegment
from app.master_narration import MasterNarrationComposer, MasterNarrationCompositionError
from app.voice_takes import ProvisionalMasterNarration


NOW = datetime(2026, 9, 21, tzinfo=timezone.utc)


def test_composition_persists_pending_master_with_ordered_source_provenance(tmp_path: Path) -> None:
    db = Database(tmp_path / "master.sqlite")
    try:
        profile = IPProfile(creator_name="Creator")
        IPProfileRepository(db).create(profile)
        project = Project(ip_profile_id=profile.id, title="Master", topic="New", fps=RationalFps(numerator=25, denominator=1), created_at=NOW)
        ProjectRepository(db).create(project)
        takes: list[AudioAsset] = []
        for index, text in enumerate(("第一句", "第二句")):
            job = Job(project_id=project.id, type=JobType.GENERATE_VOICE, idempotency_key=f"voice-{index}", created_at=NOW, updated_at=NOW)
            JobRepository(db).create(job)
            take = AudioAsset(source_file=str(tmp_path / f"take-{index}.wav"), content_hash=(str(index + 1) * 64), duration_ms=1_000, sample_rate=24_000, channels=1, authorization_reference="voice-consent", imported_at=NOW, transcript_source="voice-qa:asr", transcript_segments=[TranscriptSegment(start_ms=0, end_ms=900, text=text)], metadata={"voice_generation": {"qa_state": "verified", "job_id": str(job.id), "target_text": text}})
            AudioAssetRepository(db).create(take)
            takes.append(take)
        imported = AudioAsset(source_file=str(tmp_path / "master.wav"), content_hash="f" * 64, duration_ms=2_000, sample_rate=24_000, channels=1, authorization_reference="voice-consent", imported_at=NOW)
        AudioAssetRepository(db).create(imported)

        class Composer:
            def compose(self, received: list[object], output_path: Path) -> ProvisionalMasterNarration:
                assert [getattr(item, "audio").id for item in received] == [take.id for take in takes]
                output_path.write_bytes(b"master")
                return ProvisionalMasterNarration(output_path=output_path, expected_duration_ms=2_000, transcript_segments=(TranscriptSegment(start_ms=0, end_ms=900, text="第一句"), TranscriptSegment(start_ms=1_000, end_ms=1_900, text="第二句")), source_asset_ids=tuple(str(take.id) for take in takes))

        class Importer:
            def import_path(self, *_args: object, **_kwargs: object) -> AudioAsset:
                return imported

        result = MasterNarrationComposer(audios=AudioAssetRepository(db), jobs=JobRepository(db), importer=Importer(), composer=Composer(), output_root=tmp_path).compose(project.id, [take.id for take in takes])
        generation = result.metadata["voice_generation"]
        assert generation["qa_state"] == "pending"
        assert generation["target_text"] == "第一句 第二句"
        assert generation["composition"]["source_take_audio_ids"] == [str(take.id) for take in takes]
        assert result.transcript_source == "voice-take-composition:provisional-pending-master-qa"
        assert [(segment.start_ms, segment.end_ms) for segment in result.transcript_segments] == [(0, 900), (1000, 1900)]
    finally:
        db.close()


def test_composition_rejects_a_take_from_another_project(tmp_path: Path) -> None:
    db = Database(tmp_path / "master-project.sqlite")
    try:
        profile = IPProfile(creator_name="Creator")
        IPProfileRepository(db).create(profile)
        projects = [Project(ip_profile_id=profile.id, title=f"P{index}", topic="New", fps=RationalFps(numerator=25, denominator=1), created_at=NOW) for index in range(2)]
        for project in projects:
            ProjectRepository(db).create(project)
        foreign_job = Job(project_id=projects[1].id, type=JobType.GENERATE_VOICE, idempotency_key="foreign-voice", created_at=NOW, updated_at=NOW)
        JobRepository(db).create(foreign_job)
        take = AudioAsset(source_file=str(tmp_path / "foreign.wav"), content_hash="a" * 64, duration_ms=1_000, sample_rate=24_000, channels=1, authorization_reference="voice-consent", imported_at=NOW, transcript_source="voice-qa:asr", transcript_segments=[TranscriptSegment(start_ms=0, end_ms=900, text="第一句")], metadata={"voice_generation": {"qa_state": "verified", "job_id": str(foreign_job.id), "target_text": "第一句"}})
        AudioAssetRepository(db).create(take)
        with pytest.raises(MasterNarrationCompositionError, match="selected project"):
            MasterNarrationComposer(audios=AudioAssetRepository(db), jobs=JobRepository(db), importer=object(), composer=object(), output_root=tmp_path).compose(projects[0].id, [take.id])  # type: ignore[arg-type]
    finally:
        db.close()
