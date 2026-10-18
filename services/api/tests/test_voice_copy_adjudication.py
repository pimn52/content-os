from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException
from starlette.requests import Request
import pytest

from app.db import AudioAssetRepository, Database, IPProfileRepository, JobRepository, ProjectRepository
from app.domain.models import (
    AudioAsset,
    IPProfile,
    Job,
    JobStatus,
    JobType,
    Project,
    RationalFps,
    TranscriptSegment,
    VoiceGenerationJobPayload,
    VoiceReferenceWindowSelection,
)
from app.execution_scope import ExecutionScopeService
from app.main import VoiceCopyAdjudicationRequest, create_app
from app.master_narration import MasterNarrationComposer, MasterNarrationCompositionError
from app.voice_qa import (
    VoiceQaError,
    apply_voice_copy_adjudication,
    voice_copy_adjudication_is_valid,
    voice_take_copy_accepted,
)
from app.voice_takes import VerifiedVoiceTake, VoiceTakeComposer, VoiceTakeCompositionError


NOW = datetime(2026, 10, 18, tzinfo=timezone.utc)
PROJECT_ID = uuid4()
TARGET = "码率是每秒分给画面的数据量，它只是预算，不是清晰度本身。"
SEGMENTS = (
    TranscriptSegment(start_ms=0, end_ms=2_100, text="馬律是每秒分給畫面的數據量"),
    TranscriptSegment(start_ms=2_100, end_ms=3_100, text="它只是預算"),
    TranscriptSegment(start_ms=3_100, end_ms=4_900, text="不是清晰度本身"),
)


def _failed_homophone_audio(path: Path, *, project_id=PROJECT_ID, content_hash: str | None = None) -> AudioAsset:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"frozen synthetic voice bytes")
    reference = VoiceReferenceWindowSelection(
        clip_id=uuid4(), asset_id=uuid4(), source_content_hash="a" * 64,
        start_ms=0, end_ms=7_680, transcript_sha256="b" * 64,
    )
    generation = {
        "provider": "omnivoice", "model": "local-voice", "project_id": str(project_id),
        "job_id": "voice-job", "voice_profile_id": str(uuid4()), "target_text": TARGET,
        "authorization_reference": "owner-authorized-local-evaluation", "reference_window": reference.model_dump(mode="json"),
        "qa_state": "failed",
        "qa": {
            "qa_state": "failed", "playable": True, "copy_coverage": 1.0,
            "missing_token_count": 0, "duplicate_token_count": 0,
            "substitution_token_count": 2, "checks": ["copy_substitution_tokens"],
        },
    }
    return AudioAsset(
        source_file=str(path), content_hash=content_hash or hashlib.sha256(path.read_bytes()).hexdigest(),
        duration_ms=5_000, sample_rate=24_000, channels=1,
        authorization_reference="owner-authorized-local-evaluation", imported_at=NOW,
        transcript_source="faster-whisper:fixture", transcript_segments=list(SEGMENTS),
        metadata={"voice_generation": generation},
    )


def test_copy_adjudication_preserves_failed_qa_and_is_bound_to_exact_bytes_and_copy(tmp_path: Path) -> None:
    audio = _failed_homophone_audio(tmp_path / "voice.wav")
    original_qa = audio.metadata["voice_generation"]["qa"]

    accepted = apply_voice_copy_adjudication(
        audio, project_id=PROJECT_ID, confirmed_target_text=TARGET,
        evidence_reference="Owner confirmed 码率/馬律 are homophones and accepted the audition",
        reviewed_at=NOW,
    )

    assert accepted.metadata["voice_generation"]["qa_state"] == "failed"
    assert accepted.metadata["voice_generation"]["qa"] == original_qa
    assert accepted.metadata["voice_generation"]["copy_adjudication"]["substitutions"] == [
        {"target": "码", "asr": "马"}, {"target": "率", "asr": "律"},
    ]
    assert voice_copy_adjudication_is_valid(accepted, project_id=PROJECT_ID)
    assert voice_take_copy_accepted(accepted, project_id=PROJECT_ID)
    assert VoiceTakeComposer()._validate(VerifiedVoiceTake("what-bitrate-means", accepted)) == Path(accepted.source_file)
    assert not voice_copy_adjudication_is_valid(accepted, project_id=uuid4())
    assert not voice_copy_adjudication_is_valid(accepted.model_copy(update={"content_hash": "c" * 64}))
    with pytest.raises(VoiceQaError, match="exact generated copy"):
        apply_voice_copy_adjudication(
            audio, project_id=PROJECT_ID, confirmed_target_text="改写文案",
            evidence_reference="owner review", reviewed_at=NOW,
        )


def test_copy_adjudication_rejects_other_asr_errors(tmp_path: Path) -> None:
    audio = _failed_homophone_audio(tmp_path / "wrong.wav")
    changed = list(audio.transcript_segments)
    changed[1] = changed[1].model_copy(update={"text": "它只是不对"})
    audio = audio.model_copy(update={"transcript_segments": changed})

    with pytest.raises(VoiceQaError, match="only explicitly owner-confirmed"):
        apply_voice_copy_adjudication(
            audio, project_id=PROJECT_ID, confirmed_target_text=TARGET,
            evidence_reference="owner review", reviewed_at=NOW,
        )


def test_copy_adjudication_accepts_only_the_owner_confirmed_yuanli_homophones(tmp_path: Path) -> None:
    target = "源里有一个常见误区。"
    audio = _failed_homophone_audio(tmp_path / "yuanli.wav")
    generation = dict(audio.metadata["voice_generation"])
    generation["target_text"] = target
    qa = dict(generation["qa"])
    qa["substitution_token_count"] = 2
    generation["qa"] = qa
    audio = audio.model_copy(update={
        "transcript_segments": [TranscriptSegment(start_ms=0, end_ms=2_100, text="原理有一个常见误区")],
        "metadata": {"voice_generation": generation},
    })

    accepted = apply_voice_copy_adjudication(
        audio, project_id=PROJECT_ID, confirmed_target_text=target,
        evidence_reference="Owner auditioned this exact generated clip and accepted the sound; ASR substitutions are 源/原 and 里/理",
        reviewed_at=NOW,
    )

    assert accepted.metadata["voice_generation"]["qa_state"] == "failed"
    assert accepted.metadata["voice_generation"]["copy_adjudication"]["substitutions"] == [
        {"target": "源", "asr": "原"}, {"target": "里", "asr": "理"},
    ]
    assert voice_copy_adjudication_is_valid(accepted, project_id=PROJECT_ID)


def test_adjudication_api_checks_project_scope_and_preserves_the_original_qa_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "adjudication.sqlite3"
    db = Database(db_path)
    try:
        ip = IPProfile(creator_name="Creator")
        IPProfileRepository(db).create(ip)
        project = Project(ip_profile_id=ip.id, title="P6", topic="码率", fps=RationalFps(numerator=25, denominator=1), created_at=NOW)
        foreign = Project(ip_profile_id=ip.id, title="Other", topic="Other", fps=RationalFps(numerator=25, denominator=1), created_at=NOW)
        ProjectRepository(db).create(project)
        ProjectRepository(db).create(foreign)
        payload = VoiceGenerationJobPayload(
            project_id=project.id, voice_profile_id=uuid4(), text=TARGET,
            authorization_reference="owner-authorized-local-evaluation",
            reference_window=VoiceReferenceWindowSelection(
                clip_id=uuid4(), asset_id=uuid4(), source_content_hash="d" * 64,
                start_ms=0, end_ms=7_680, transcript_sha256="e" * 64,
            ),
        )
        job = Job(
            project_id=project.id, type=JobType.GENERATE_VOICE, status=JobStatus.COMPLETED,
            idempotency_key="p6-copy-adjudication-job", created_at=NOW, updated_at=NOW, payload=payload,
        )
        JobRepository(db).create(job)
        audio = _failed_homophone_audio(tmp_path / "voice.wav", project_id=project.id)
        generation = dict(audio.metadata["voice_generation"])
        generation["job_id"] = str(job.id)
        generation["voice_profile_id"] = str(payload.voice_profile_id)
        generation["reference_window"] = payload.reference_window.model_dump(mode="json")
        audio = audio.model_copy(update={"metadata": {"voice_generation": generation}})
        AudioAssetRepository(db).create(audio)
    finally:
        db.close()

    # The live P6 call has a real current internal scope; this API contract test
    # isolates the endpoint from provider-admission fixtures while preserving
    # its requirement that the scope be explicitly internal and project-bound.
    monkeypatch.setattr(
        ExecutionScopeService,
        "require_content",
        lambda self, *_args, **_kwargs: (SimpleNamespace(purpose="internal_evaluation"),),
    )
    app = create_app(db_path)
    app.state.database = Database(db_path)
    app.state.data_root = tmp_path.resolve()
    endpoint = next(
        route.endpoint for route in app.routes
        if getattr(route, "path", None) == "/projects/{project_id}/voice-assets/{audio_id}/copy-adjudication"
    )
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": "/copy-adjudication",
        "raw_path": b"/copy-adjudication", "query_string": b"", "headers": [],
        "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 80), "app": app,
    }
    request = Request(scope)

    def invoke(project_id, payload):
        coroutine = endpoint(project_id, audio.id, payload, request)
        try:
            coroutine.send(None)
        except StopIteration as completed:
            return completed.value
        raise AssertionError("copy-adjudication endpoint unexpectedly awaited external work")

    try:
        with pytest.raises(HTTPException) as denied:
            invoke(foreign.id, VoiceCopyAdjudicationRequest(confirmed_target_text=TARGET, evidence_reference="owner review"))
        assert denied.value.status_code == 422
        result = invoke(
            project.id,
            VoiceCopyAdjudicationRequest(
                confirmed_target_text=TARGET,
                evidence_reference="Owner confirmed 码率/馬律 are homophones and accepted the audition",
            ),
        )
        assert result.metadata["voice_generation"]["qa_state"] == "failed"
        assert result.metadata["voice_generation"]["qa"]["checks"] == ["copy_substitution_tokens"]
        assert voice_copy_adjudication_is_valid(result, project_id=project.id)
        with pytest.raises(HTTPException) as repeated:
            invoke(project.id, VoiceCopyAdjudicationRequest(confirmed_target_text=TARGET, evidence_reference="owner review"))
        assert repeated.value.status_code == 409
    finally:
        app.state.database.close()


def test_master_composer_consumes_only_the_bound_owner_adjudication(tmp_path: Path) -> None:
    from app.voice_takes import ProvisionalMasterNarration

    db = Database(tmp_path / "master-adjudication.sqlite3")
    try:
        ip = IPProfile(creator_name="Creator")
        IPProfileRepository(db).create(ip)
        project = Project(ip_profile_id=ip.id, title="P6", topic="码率", fps=RationalFps(numerator=25, denominator=1), created_at=NOW)
        ProjectRepository(db).create(project)
        voice_profile_id = uuid4()
        reference = VoiceReferenceWindowSelection(
            clip_id=uuid4(), asset_id=uuid4(), source_content_hash="a" * 64,
            start_ms=0, end_ms=7_680, transcript_sha256="b" * 64,
        )
        payload = VoiceGenerationJobPayload(
            project_id=project.id, voice_profile_id=voice_profile_id, text=TARGET,
            authorization_reference="owner-authorized-local-evaluation", reference_window=reference,
        )
        job = Job(
            project_id=project.id, type=JobType.GENERATE_VOICE, status=JobStatus.COMPLETED,
            idempotency_key="master-adjudication-voice", created_at=NOW, updated_at=NOW, payload=payload,
        )
        JobRepository(db).create(job)
        audio = _failed_homophone_audio(tmp_path / "take.wav", project_id=project.id)
        generation = dict(audio.metadata["voice_generation"])
        generation.update(
            job_id=str(job.id), voice_profile_id=str(voice_profile_id),
            reference_window=reference.model_dump(mode="json"),
        )
        audio = audio.model_copy(update={"metadata": {"voice_generation": generation}})
        AudioAssetRepository(db).create(audio)
        audio = apply_voice_copy_adjudication(
            audio, project_id=project.id, confirmed_target_text=TARGET,
            evidence_reference="owner confirmed the exact homophone on the audition",
            reviewed_at=NOW,
        )
        AudioAssetRepository(db).update(audio)
        imported = AudioAsset(
            source_file=str(tmp_path / "master.wav"), content_hash="f" * 64,
            duration_ms=5_000, sample_rate=24_000, channels=1,
            authorization_reference="owner-authorized-local-evaluation", imported_at=NOW,
        )
        AudioAssetRepository(db).create(imported)

        class Composer:
            def compose(self, takes, output_path):
                assert [take.audio.id for take in takes] == [audio.id]
                output_path.write_bytes(b"composed master bytes")
                return ProvisionalMasterNarration(
                    output_path=output_path, expected_duration_ms=5_000,
                    transcript_segments=(TranscriptSegment(start_ms=0, end_ms=4_900, text=TARGET),),
                    source_asset_ids=(str(audio.id),),
                )

        class Importer:
            def import_path(self, *_args, **_kwargs):
                return imported

        candidate = MasterNarrationComposer(
            audios=AudioAssetRepository(db), jobs=JobRepository(db), importer=Importer(),
            composer=Composer(), output_root=tmp_path,
        ).compose(project.id, [audio.id])
        composition = candidate.metadata["voice_generation"]["composition"]
        assert composition["source_take_audio_ids"] == [str(audio.id)]
        assert composition["copy_adjudicated_source_take_audio_ids"] == [str(audio.id)]
        assert candidate.metadata["voice_generation"]["qa_state"] == "pending"
        assert AudioAssetRepository(db).get(audio.id).metadata["voice_generation"]["qa_state"] == "failed"
    finally:
        db.close()
