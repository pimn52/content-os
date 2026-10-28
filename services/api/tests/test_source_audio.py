from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from app.budget import ProviderCallLedger
from app.db import AssetRepository, AudioAssetRepository, BudgetPolicyRepository, ClipRepository, Database, IPProfileRepository, ProjectRepository, ProviderCallRepository
from app.domain.models import Asset, AudioAsset, AudioAssetTranscriptionJobPayload, BudgetPolicy, Clip, IPProfile, Job, JobStatus, JobType, Project, RationalFps, SourceKind, TranscriptSegment
from app.jobs.handlers import AudioAssetTranscriptionJobHandler
from app.jobs.targets import AudioAssetJobTargetStore
from app.media.transcripts import AudioTranscriptPersistence
from app.media.source_audio import SourceAudioDerivationService, admitted_source_audio_binding
from app.production_preflight import _approved_master


def test_source_audio_inherits_authorization_and_keeps_real_transcript(monkeypatch, tmp_path: Path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"authorized source bytes")
    audio_bytes = b"RIFF source excerpt"
    extracted_path = tmp_path / "excerpt.wav"
    extracted_path.write_bytes(audio_bytes)
    now = datetime.now(timezone.utc)

    class FakeExtractor:
        def __init__(self, data_root, command, *, sample_rate, channels):
            assert sample_rate == 48_000 and channels == 2

        def extract_audio_interval(self, asset, start_ms, end_ms):
            assert start_ms == 1_000 and end_ms == 4_000
            return SimpleNamespace(path=extracted_path)

    with Database(tmp_path / "source-audio.sqlite") as db:
        profile = IPProfileRepository(db).create(IPProfile(creator_name="Creator"))
        project = ProjectRepository(db).create(Project(
            ip_profile_id=profile.id, title="P6", topic="bitrate", fps=RationalFps(numerator=30, denominator=1),
            created_at=now,
        ))
        BudgetPolicyRepository(db).save(BudgetPolicy(
            project_id=project.id, currency="CNY", max_amount="7", max_calls=1,
            allow_unknown_cost=False, updated_at=now,
        ))
        asset = AssetRepository(db).create(Asset(
            source_file=str(source), content_hash=hashlib.sha256(source.read_bytes()).hexdigest(),
            duration_ms=5_000, width=1280, height=720,
            fps=RationalFps(numerator=30, denominator=1), has_audio=True,
            authorization_reference="owner-approved-source-use", imported_at=now,
        ))
        clip = ClipRepository(db).create(Clip(
            asset_id=asset.id, start_ms=0, end_ms=5_000, asset_duration_ms=5_000,
            transcript="first phrase second phrase", transcript_source="local-asr",
            transcript_segments=[
                TranscriptSegment(start_ms=1_000, end_ms=2_000, text="first phrase"),
                TranscriptSegment(start_ms=2_000, end_ms=3_500, text="second phrase"),
            ],
        ))

        class FakeImporter:
            def __init__(self, database, data_root, probe):
                self.database = database

            def import_path(self, path, authorization_reference, *, language):
                return AudioAssetRepository(self.database).create(AudioAsset(
                    source_file=str(path), content_hash=hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                    duration_ms=3_000, sample_rate=48_000, channels=2,
                    authorization_reference=authorization_reference, imported_at=now,
                    language=language, metadata={"filename": Path(path).name},
                ))

        monkeypatch.setattr("app.media.source_audio.MediaExtractor", FakeExtractor)
        monkeypatch.setattr("app.media.source_audio.AudioImporter", FakeImporter)
        monkeypatch.setattr("app.media.source_audio.FFProbeAdapter", lambda command: SimpleNamespace(
            probe_audio=lambda path: SimpleNamespace(duration_ms=3_000),
        ))
        audio = SourceAudioDerivationService(db, tmp_path).derive(
            project.id, asset.id, clip.id, 1_000, 4_000,
        )

        assert audio.authorization_reference == asset.authorization_reference
        assert audio.transcript_source and len(audio.transcript_segments) == 2
        assert [(item.start_ms, item.end_ms, item.text) for item in audio.transcript_segments] == [
            (0, 1_000, "first phrase"), (1_000, 2_500, "second phrase"),
        ]
        binding = admitted_source_audio_binding(db, audio, project.id)
        assert binding is not None
        assert binding["source_asset_id"] == str(asset.id)
        assert binding["authorization_reference"] == "owner-approved-source-use"
        assert _approved_master(db, project.id, "first phrase second phrase") == audio
        assert _approved_master(db, project.id, "different copy") is None

        class FakeProvider:
            def transcribe(self, path, *, language=None):
                assert Path(path).read_bytes() == audio_bytes
                return SimpleNamespace(segments=[
                    TranscriptSegment(start_ms=100, end_ms=1_100, text="corrected first phrase"),
                    TranscriptSegment(start_ms=1_100, end_ms=2_600, text="corrected second phrase"),
                ])

        target_store = AudioAssetJobTargetStore(db)
        job = Job(
            project_id=project.id, type=JobType.TRANSCRIBE_AUDIO_ASSET,
            status=JobStatus.RUNNING, idempotency_key="p6-audio-asr",
            created_at=now, updated_at=now,
            payload=AudioAssetTranscriptionJobPayload(audio_asset_id=audio.id, source_sha256=audio.content_hash),
        )
        target_store.enqueue(job, audio.id)
        AudioAssetTranscriptionJobHandler(
            target_store, AudioAssetRepository(db), FakeProvider(), AudioTranscriptPersistence(db),
            ProviderCallLedger(db), provider_name="faster-whisper", provider_model="medium", data_root=tmp_path,
        )(job)
        updated_audio = AudioAssetRepository(db).get(audio.id)
        assert updated_audio is not None
        assert updated_audio.transcript_source == "faster-whisper:medium"
        assert [item.text for item in updated_audio.transcript_segments] == [
            "corrected first phrase", "corrected second phrase",
        ]
        updated_binding = admitted_source_audio_binding(db, updated_audio, project.id)
        assert updated_binding is not None
        assert updated_binding["source_transcript_source"] == "local-asr"
        assert updated_binding["source_transcript_sha256"] != updated_binding["transcript_sha256"]
        provider_calls = ProviderCallRepository(db).list_for_project(project.id)
        assert len(provider_calls) == 1
        assert provider_calls[0].status == "completed"
        assert provider_calls[0].estimated_cost.amount == 0
        assert provider_calls[0].estimated_cost.currency == "CNY"
        assert _approved_master(db, project.id, "corrected first phrase corrected second phrase") == updated_audio
        other_project = ProjectRepository(db).create(Project(
            ip_profile_id=profile.id, title="Other", topic="other", fps=RationalFps(numerator=30, denominator=1),
            created_at=now,
        ))
        assert _approved_master(db, other_project.id, "first phrase second phrase") is None

        changed = AssetRepository(db).update(asset.model_copy(update={"authorization_reference": "different-use"}))
        assert changed.id == asset.id
        assert admitted_source_audio_binding(db, audio, project.id) is None
        assert _approved_master(db, project.id, "first phrase second phrase") is None


def test_source_audio_rejects_transcript_past_extracted_audio_duration(monkeypatch, tmp_path: Path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"authorized source bytes")
    extracted_path = tmp_path / "short-excerpt.wav"
    extracted_path.write_bytes(b"RIFF short excerpt")
    now = datetime.now(timezone.utc)

    class FakeExtractor:
        def __init__(self, data_root, command, *, sample_rate, channels):
            pass

        def extract_audio_interval(self, asset, start_ms, end_ms):
            return SimpleNamespace(path=extracted_path)

    with Database(tmp_path / "short-source-audio.sqlite") as db:
        profile = IPProfileRepository(db).create(IPProfile(creator_name="Creator"))
        project = ProjectRepository(db).create(Project(
            ip_profile_id=profile.id, title="P6", topic="bitrate", fps=RationalFps(numerator=30, denominator=1),
            created_at=now,
        ))
        asset = AssetRepository(db).create(Asset(
            source_file=str(source), content_hash=hashlib.sha256(source.read_bytes()).hexdigest(),
            duration_ms=5_000, width=1280, height=720,
            fps=RationalFps(numerator=30, denominator=1), has_audio=True,
            authorization_reference="owner-approved-source-use", imported_at=now,
        ))
        clip = ClipRepository(db).create(Clip(
            asset_id=asset.id, start_ms=0, end_ms=5_000, asset_duration_ms=5_000,
            transcript="first phrase", transcript_source="local-asr",
            transcript_segments=[TranscriptSegment(start_ms=1_000, end_ms=2_000, text="first phrase")],
        ))
        monkeypatch.setattr("app.media.source_audio.MediaExtractor", FakeExtractor)
        monkeypatch.setattr("app.media.source_audio.FFProbeAdapter", lambda command: SimpleNamespace(
            probe_audio=lambda path: SimpleNamespace(duration_ms=800),
        ))
        try:
            SourceAudioDerivationService(db, tmp_path).derive(project.id, asset.id, clip.id, 1_000, 3_000)
        except ValueError as exc:
            assert str(exc) == "source_audio_transcript_exceeds_extracted_duration"
        else:
            raise AssertionError("transcript timestamps past the extracted audio must not become a Master")
        assert AudioAssetRepository(db).list() == []


def test_source_audio_can_be_transcribed_after_interval_extraction(monkeypatch, tmp_path: Path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    now = datetime.now(timezone.utc)
    with Database(tmp_path / "source-audio-invalid.sqlite") as db:
        profile = IPProfileRepository(db).create(IPProfile(creator_name="Creator"))
        project = ProjectRepository(db).create(Project(
            ip_profile_id=profile.id, title="P6", topic="bitrate", fps=RationalFps(numerator=30, denominator=1),
            created_at=now,
        ))
        asset = AssetRepository(db).create(Asset(
            source_file=str(source), content_hash=hashlib.sha256(source.read_bytes()).hexdigest(),
            duration_ms=5_000, width=1280, height=720,
            fps=RationalFps(numerator=30, denominator=1), has_audio=True,
            authorization_reference="owner-approved-source-use", imported_at=now,
        ))
        clip = ClipRepository(db).create(Clip(
            asset_id=asset.id, start_ms=0, end_ms=5_000, asset_duration_ms=5_000,
        ))
        extracted = tmp_path / "excerpt.wav"
        extracted.write_bytes(b"RIFF full excerpt")

        class FakeExtractor:
            def __init__(self, data_root, command, *, sample_rate, channels):
                pass

            def extract_audio_interval(self, asset, start_ms, end_ms):
                return SimpleNamespace(path=extracted)

        class FakeImporter:
            def __init__(self, database, data_root, probe):
                self.database = database

            def import_path(self, path, authorization_reference, *, language):
                return AudioAssetRepository(self.database).create(AudioAsset(
                    source_file=str(path), content_hash=hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                    duration_ms=5_000, sample_rate=48_000, channels=2,
                    authorization_reference=authorization_reference, imported_at=now, language=language,
                    metadata={"filename": Path(path).name},
                ))

        monkeypatch.setattr("app.media.source_audio.MediaExtractor", FakeExtractor)
        monkeypatch.setattr("app.media.source_audio.AudioImporter", FakeImporter)
        monkeypatch.setattr("app.media.source_audio.FFProbeAdapter", lambda command: SimpleNamespace(
            probe_audio=lambda path: SimpleNamespace(duration_ms=5_000),
        ))
        audio = SourceAudioDerivationService(db, tmp_path).derive(project.id, asset.id, clip.id, 0, 5_000)
        assert audio.transcript_segments == [] and audio.transcript_source is None
        assert admitted_source_audio_binding(db, audio, project.id, require_transcript=False) is not None
        assert admitted_source_audio_binding(db, audio, project.id) is None
