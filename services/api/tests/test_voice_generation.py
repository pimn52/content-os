from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from app.assembly.video_spec import GeneratedNarrationQaPending, _require_generated_voice_qa
from app.budget import ProviderCallLedger
from app.db import AudioAssetRepository, BudgetPolicyRepository, ClipRepository, Database, IPProfileRepository, ProjectRepository, ProviderCallRepository, VoiceProfileRepository
from app.domain.models import AudioAsset, BudgetPolicy, Clip, ConsentRecord, IPProfile, Job, JobStatus, JobType, Project, RationalFps, SourceKind, VoiceGenerationJobPayload, VoiceProfile, VoiceQaJobPayload
from app.jobs import JobRunner, JobStore
from app.jobs.handlers import VoiceGenerationJobHandler, VoiceQaJobHandler
from app.main import create_app
from app.providers.voice import VoiceSynthesisResult
from app.providers.asr import TranscriptionResult, TranscriptionSegment
from app.voice_qa import apply_voice_qa, verify_generated_voice


def _project_and_profile(db: Database, root: Path) -> tuple[Project, VoiceProfile]:
    ip = IPProfile(creator_name="Creator")
    IPProfileRepository(db).create(ip)
    project = Project(ip_profile_id=ip.id, title="Voice", topic="New narration", fps=RationalFps(numerator=25, denominator=1), created_at=datetime.now(timezone.utc))
    ProjectRepository(db).create(project)
    clip = Clip(asset_id=uuid4(), start_ms=0, end_ms=1_000, asset_duration_ms=1_000, voice_candidate=True)
    # VoiceProfileRepository deliberately records consent provenance; the
    # profile endpoint additionally validates real reference clips.
    profile = VoiceProfile(
        name="Creator voice", provider="test-voice", provider_profile_id="creator-v1", reference_clip_ids=[clip.id],
        consent=ConsentRecord(subject_name="Creator", basis="self", confirmed=True, confirmed_at=datetime.now(timezone.utc)),
        language="zh", created_at=datetime.now(timezone.utc),
    )
    VoiceProfileRepository(db).create(profile)
    return project, profile


def test_voice_job_imports_provenance_and_reserves_local_tts(tmp_path: Path) -> None:
    db = Database(tmp_path / "voice.sqlite")
    try:
        project, profile = _project_and_profile(db, tmp_path)
        with db.transaction():
            BudgetPolicyRepository(db).save(BudgetPolicy(project_id=project.id, allow_unknown_cost=False, updated_at=datetime.now(timezone.utc)))
        payload = VoiceGenerationJobPayload(project_id=project.id, voice_profile_id=profile.id, text="这是新的旁白。", authorization_reference="voice-consent-1", language="zh")
        now = datetime.now(timezone.utc)
        job = Job(project_id=project.id, type=JobType.GENERATE_VOICE, idempotency_key="voice-one", created_at=now, updated_at=now, payload=payload)
        JobStore(db).enqueue(job)

        class Provider:
            provider_name = "test-voice"
            model = "test-model"
            is_local = True

            def synthesize(self, received: VoiceProfile, text: str, output_path: Path, *, language: str | None = None) -> VoiceSynthesisResult:
                assert received.id == profile.id and text == payload.text and language == "zh"
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(b"generated-audio")
                return VoiceSynthesisResult(output_path, provider_version="test-version")

        class Importer:
            def import_path(self, source: Path, authorization_reference: str, *, language: str | None = None) -> AudioAsset:
                assert source.is_file() and authorization_reference == "voice-consent-1"
                value = AudioAsset(
                    source_file=str(source), content_hash="a" * 64, duration_ms=1_200, sample_rate=24_000, channels=1,
                    language=language, authorization_reference=authorization_reference, imported_at=datetime.now(timezone.utc),
                )
                AudioAssetRepository(db).create(value)
                return value

        handler = VoiceGenerationJobHandler(
            VoiceProfileRepository(db), AudioAssetRepository(db), Importer(), Provider(), tmp_path / "generated", ProviderCallLedger(db)  # type: ignore[arg-type]
        )
        runner = JobRunner(JobStore(db), {JobType.GENERATE_VOICE: handler}, worker_id="voice-worker", lease_duration=timedelta(minutes=1), max_attempts=2)
        result = runner.run_once()

        assert result is not None and result.status is JobStatus.COMPLETED
        audio = AudioAssetRepository(db).list()[0]
        assert audio.metadata["voice_generation"] == {
            "provider": "test-voice", "model": "test-model", "provider_version": "test-version",
            "voice_profile_id": str(profile.id), "job_id": str(job.id), "attempt": 1,
            "target_text": payload.text, "qa_state": "pending",
        }
        call = ProviderCallRepository(db).list_for_project(project.id)[0]
        assert call.operation == "tts" and call.status == "completed"
        assert call.estimated_cost.amount == Decimal("0")
    finally:
        db.close()


def test_voice_job_api_is_typed_and_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "voice-api.sqlite"
    db = Database(path)
    project, profile = _project_and_profile(db, tmp_path)
    db.close()
    request = {
        "idempotency_key": "voice-api", "voice_profile_id": str(profile.id), "text": "新的文本", "authorization_reference": "voice-consent-1", "language": "zh",
    }
    with TestClient(create_app(path)) as client:
        first = client.post(f"/projects/{project.id}/voice-jobs", json=request)
        assert first.status_code == 201
        repeated = client.post(f"/projects/{project.id}/voice-jobs", json=request)
        assert repeated.status_code == 201 and repeated.json()["id"] == first.json()["id"]
        assert client.post(f"/projects/{project.id}/voice-jobs", json={**request, "text": "different"}).status_code == 409
    reopened = Database(path)
    try:
        job = JobStore(reopened).get(uuid4())
        assert job is None
        stored = reopened.connection.execute("SELECT payload FROM jobs").fetchone()["payload"]
        assert '"type":"generate_voice"' in stored and "voice-consent-1" in stored
    finally:
        reopened.close()


def test_voice_qa_job_uses_real_asr_and_persists_verified_evidence(tmp_path: Path) -> None:
    db = Database(tmp_path / "voice-qa.sqlite")
    try:
        project, _ = _project_and_profile(db, tmp_path)
        source = tmp_path / "generated.wav"
        source.write_bytes(b"playable")
        audio = AudioAsset(
            source_file=str(source), content_hash="f" * 64, duration_ms=2_000, sample_rate=24_000, channels=1,
            language="zh", authorization_reference="voice-consent-1", imported_at=datetime.now(timezone.utc),
            metadata={"voice_generation": {"provider": "test-voice", "model": "test-model", "target_text": "你好世界", "qa_state": "pending"}},
        )
        AudioAssetRepository(db).create(audio)
        with db.transaction():
            BudgetPolicyRepository(db).save(BudgetPolicy(project_id=project.id, allow_unknown_cost=False, updated_at=datetime.now(timezone.utc)))
        payload = VoiceQaJobPayload(project_id=project.id, narration_audio_id=audio.id, target_text="你好世界")
        now = datetime.now(timezone.utc)
        job = Job(project_id=project.id, type=JobType.VERIFY_VOICE, idempotency_key="voice-qa-one", created_at=now, updated_at=now, payload=payload)
        JobStore(db).enqueue(job)

        class ASR:
            model = "qa-model"
            is_local = True

            def transcribe(self, audio_path: Path, *, language: str | None = None, prompt: str | None = None) -> TranscriptionResult:
                assert Path(audio_path) == source and language == "zh" and prompt is None
                return TranscriptionResult("你好世界", (TranscriptionSegment(0, 1_000, "你好世界"),), "zh")

        handler = VoiceQaJobHandler(
            AudioAssetRepository(db), ASR(), ProviderCallLedger(db),
            provider_name="faster-whisper", provider_model="qa-model",
        )
        result = JobRunner(JobStore(db), {JobType.VERIFY_VOICE: handler}, worker_id="voice-qa-worker", lease_duration=timedelta(minutes=1), max_attempts=2).run_once()

        assert result is not None and result.status is JobStatus.COMPLETED
        updated = AudioAssetRepository(db).get(audio.id)
        assert updated is not None
        generation = updated.metadata["voice_generation"]
        assert generation["qa_state"] == "verified"
        assert generation["qa"]["copy_coverage"] == 1.0
        assert updated.transcript_source == "faster-whisper:qa-model"
        call = ProviderCallRepository(db).list_for_project(project.id)[0]
        assert call.operation == "asr" and call.status == "completed" and call.estimated_cost.amount == Decimal("0")
    finally:
        db.close()


def test_voice_qa_job_api_requires_matching_generated_copy(tmp_path: Path) -> None:
    path = tmp_path / "voice-qa-api.sqlite"
    db = Database(path)
    project, _ = _project_and_profile(db, tmp_path)
    source = tmp_path / "generated.wav"
    source.write_bytes(b"playable")
    audio = AudioAsset(
        source_file=str(source), content_hash="g" * 64, duration_ms=1_000, sample_rate=24_000, channels=1,
        authorization_reference="voice-consent-1", imported_at=datetime.now(timezone.utc),
        metadata={"voice_generation": {"target_text": "你好世界", "qa_state": "pending"}},
    )
    AudioAssetRepository(db).create(audio)
    db.close()
    request = {"idempotency_key": "voice-qa-api", "narration_audio_id": str(audio.id), "target_text": "你好世界"}
    with TestClient(create_app(path)) as client:
        first = client.post(f"/projects/{project.id}/voice-qa-jobs", json=request)
        assert first.status_code == 201 and first.json()["type"] == "verify_voice"
        repeated = client.post(f"/projects/{project.id}/voice-qa-jobs", json=request)
        assert repeated.status_code == 201 and repeated.json()["id"] == first.json()["id"]
        assert client.post(f"/projects/{project.id}/voice-qa-jobs", json={**request, "target_text": "别的文本"}).status_code == 422


def test_generated_voice_is_blocked_from_assembly_until_qa_is_verified() -> None:
    audio = AudioAsset(
        source_file="generated.wav", content_hash="b" * 64, duration_ms=1_000, sample_rate=24_000, channels=1,
        authorization_reference="voice-consent-1", imported_at=datetime.now(timezone.utc),
        metadata={"voice_generation": {"provider": "test-voice", "qa_state": "pending"}},
    )
    with pytest.raises(GeneratedNarrationQaPending):
        _require_generated_voice_qa(audio)
    _require_generated_voice_qa(audio.model_copy(update={"metadata": {"voice_generation": {"provider": "test-voice", "qa_state": "verified"}}}))


def test_voice_qa_requires_real_timing_and_records_copy_and_silence_evidence(tmp_path: Path) -> None:
    source = tmp_path / "generated.wav"
    source.write_bytes(b"playable")
    audio = AudioAsset(
        source_file=str(source), content_hash="c" * 64, duration_ms=2_000, sample_rate=24_000, channels=1,
        authorization_reference="voice-consent-1", imported_at=datetime.now(timezone.utc),
        metadata={"voice_generation": {"provider": "test-voice", "qa_state": "pending"}},
    )
    transcript = TranscriptionResult("你好世界", (TranscriptionSegment(0, 1_000, "你好世界"),))
    report = verify_generated_voice(audio, "你好世界", transcript, max_silence_ms=1_100)
    assert report.verified and report.copy_coverage == 1 and report.substitution_token_count == 0 and report.longest_silence_ms == 1_000
    updated = apply_voice_qa(audio, report, transcript, provider="qa-asr", model="qa-model")
    assert updated.metadata["voice_generation"]["qa_state"] == "verified"
    assert updated.transcript_source == "qa-asr:qa-model"
    failed = verify_generated_voice(audio, "你好世界", TranscriptionResult("你好", (TranscriptionSegment(0, 500, "你好"),)))
    assert not failed.verified and failed.missing_token_count == 2 and "copy_missing_tokens" in failed.checks


def test_voice_qa_accepts_traditional_asr_glyphs_but_not_new_words(tmp_path: Path) -> None:
    source = tmp_path / "generated.wav"
    source.write_bytes(b"playable")
    audio = AudioAsset(
        source_file=str(source), content_hash="d" * 64, duration_ms=2_000, sample_rate=24_000, channels=1,
        authorization_reference="voice-consent-1", imported_at=datetime.now(timezone.utc),
        metadata={"voice_generation": {"provider": "test-voice", "qa_state": "pending"}},
    )
    traditional = TranscriptionResult("第一，先寫出一句能被記住的結論", (TranscriptionSegment(0, 1_500, "第一，先寫出一句能被記住的結論"),))
    assert verify_generated_voice(audio, "第一，先写出一句能被记住的结论。", traditional).verified
    progressive_variant = TranscriptionResult("接著，把支撐結論的畫面按順序放進去", (TranscriptionSegment(0, 1_500, "接著，把支撐結論的畫面按順序放進去"),))
    assert verify_generated_voice(audio, "接着，把支撑结论的画面按顺序放进去。", progressive_variant).verified
    extra_word = TranscriptionResult("第一，先寫出一句能被記住的結論和例子", (TranscriptionSegment(0, 1_500, "第一，先寫出一句能被記住的結論和例子"),))
    report = verify_generated_voice(audio, "第一，先写出一句能被记住的结论。", extra_word)
    assert not report.verified and report.duplicate_token_count == 3


def test_voice_qa_keeps_small_asr_substitutions_explicit_and_bounded(tmp_path: Path) -> None:
    source = tmp_path / "generated.wav"
    source.write_bytes(b"playable")
    audio = AudioAsset(
        source_file=str(source), content_hash="s" * 64, duration_ms=2_000, sample_rate=24_000, channels=1,
        authorization_reference="voice-consent-1", imported_at=datetime.now(timezone.utc),
        metadata={"voice_generation": {"provider": "test-voice", "qa_state": "pending"}},
    )
    report = verify_generated_voice(
        audio, "Content OS 可以审核这个新的成品并保留真实素材字幕", TranscriptionResult("ContentOS可以审阅这个新的成品并保留真实素材字幕", (TranscriptionSegment(0, 1_500, "ContentOS可以审阅这个新的成品并保留真实素材字幕"),)),
    )
    assert report.verified
    assert report.missing_token_count == 0 and report.duplicate_token_count == 0
    assert report.substitution_token_count == 1
    assert "copy_substitutions_within_tolerance" in report.checks

    rejected = verify_generated_voice(
        audio, "一二三四五六七八九十", TranscriptionResult("甲乙丙丁戊己庚辛壬癸", (TranscriptionSegment(0, 1_500, "甲乙丙丁戊己庚辛壬癸"),)),
    )
    assert not rejected.verified and "copy_substitution_tokens" in rejected.checks


def test_voice_qa_rejects_unrecognized_leading_audio_gap(tmp_path: Path) -> None:
    source = tmp_path / "generated.wav"
    source.write_bytes(b"playable")
    audio = AudioAsset(
        source_file=str(source), content_hash="e" * 64, duration_ms=2_000, sample_rate=24_000, channels=1,
        authorization_reference="voice-consent-1", imported_at=datetime.now(timezone.utc),
        metadata={"voice_generation": {"provider": "test-voice", "qa_state": "pending"}},
    )
    report = verify_generated_voice(
        audio,
        "你好世界",
        TranscriptionResult("你好世界", (TranscriptionSegment(600, 1_600, "你好世界"),)),
    )
    assert not report.verified
    assert report.leading_silence_ms == 600
    assert "leading_silence_or_unrecognized_audio" in report.checks
