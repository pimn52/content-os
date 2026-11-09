from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import math
import os
from pathlib import Path
import struct
from uuid import uuid4
import wave

from fastapi.testclient import TestClient
import pytest

from app.assembly.video_spec import GeneratedNarrationQaPending, _require_generated_voice_qa
from app.budget import ProviderCallLedger
from app.db import AudioAssetRepository, BudgetPolicyRepository, ClipRepository, Database, IPProfileRepository, ProjectRepository, ProviderCallRepository, ProviderMachineCapabilityProfileRepository, VoiceProfileRepository
from app.domain.models import AudioAsset, BudgetPolicy, Clip, ConsentRecord, IPProfile, Job, JobStatus, JobType, Project, ProviderMachineCapabilityProfile, RationalFps, SourceKind, VoiceGenerationJobPayload, VoiceHumanReview, VoiceProfile, VoiceQaJobPayload, VoiceReferenceWindowSelection, VoiceReviewOutcome
from app.jobs import JobRunner, JobStore
from app.jobs.handlers import VoiceGenerationJobHandler, VoiceQaJobHandler
from app.jobs.runner import JobExecutionError
from app.main import create_app
from app.media.audio_importer import AudioImporter
from app.media.ffprobe import AudioProbeMetadata
from app.providers.voice import VoiceSynthesisResult
from app.providers.asr import TranscriptionResult, TranscriptionSegment, TranscriptionWord
from app.voice_qa import VoiceQaError, apply_voice_human_review, apply_voice_qa, comparison_tokens, generated_voice_qa_parts, verify_generated_voice, voice_human_review_status
from app.voice_recovery import VoiceRecoveryRoute, recommend_voice_recovery


def _project_and_profile(db: Database, root: Path) -> tuple[Project, VoiceProfile]:
    ip = IPProfile(creator_name="Creator")
    IPProfileRepository(db).create(ip)
    project = Project(ip_profile_id=ip.id, title="Voice", topic="New narration", fps=RationalFps(numerator=25, denominator=1), created_at=datetime.now(timezone.utc))
    ProjectRepository(db).create(project)
    clip = Clip(asset_id=uuid4(), start_ms=0, end_ms=1_000, asset_duration_ms=1_000, voice_candidate=True)
    # VoiceProfileRepository deliberately records consent provenance; the
    # profile endpoint additionally validates real reference clips.
    profile = VoiceProfile(
        ip_profile_id=ip.id,
        name="Creator voice", provider="test-voice", provider_profile_id="creator-v1", reference_clip_ids=[clip.id],
        consent=ConsentRecord(subject_name="Creator", basis="self", confirmed=True, confirmed_at=datetime.now(timezone.utc)),
        language="zh", created_at=datetime.now(timezone.utc),
    )
    VoiceProfileRepository(db).create(profile)
    return project, profile


def _reviewed_pause_recipe(profile: VoiceProfile):
    return {
        "schema_version": "0.5", "recipe_id": "v60-human-approved", "version": 6,
        "quality_anchor_audio_id": str(uuid4()), "quality_anchor_sha256": "a" * 64,
        "quality_anchor_evidence_reference": "audio:human-approved-v60",
        "human_review": {"approved": True, "likeness": "pass", "naturalness": "pass",
            "emphasis": "pass", "pace": "pass", "pauses": "pass", "rhythm": "pass"},
        "reference_window": {
            "clip_id": str(profile.reference_clip_ids[0]), "asset_id": str(uuid4()),
            "source_content_hash": "b" * 64, "start_ms": 0, "end_ms": 6000,
            "transcript_sha256": "c" * 64,
        },
        "runtime_label": "test-voice-runtime", "runtime_parameters_sha256": "d" * 64,
        "composition_policy": "paragraph_spans",
        "pause_hierarchy": {
            "schema_version": "1.0",
            "levels": {
                "phrase_internal": "continuous_no_cut",
                "comma": "short_continuing_boundary",
                "sentence": "natural_close_longer_than_comma",
                "paragraph": "allow_one_beat",
                "key_point": "allow_one_thinking_beat",
                "ending": "natural_landing",
            },
            "invariants": {
                "sentence_outranks_comma": True,
                "global_speed_is_not_pause_control": True,
                "insert_unmeasured_silence": False,
            },
            "evidence_refs": ["v19b-u-voice-punctuation-hierarchy-review"],
        },
        "seam_policy": {
            "scope": "paragraph_spans",
            "method": "pcm16_mono_24khz_10ms_rms_below_minus45dbfs",
            "eligible_boundary_kinds": ["terminal", "continuing"],
            "margin_ms_each_side": 60, "max_cut_ms": 300,
            "preserve_spoken_frames": True, "insert_silence": False,
            "evidence_reference": "content-os-data/evaluation-evidence/v60-length-completion/manifest.json",
        },
        "delivery_adjustments": {
            "schema_version": "1.4", "review_state": "pending_user_review",
            "syntax_rule_set_version": "zh-clause-binding-v1",
            "punctuation_overrides": [], "pronunciation_overrides": [], "generation_breaks": [],
            "copula_complement_break": {
                "enabled": False, "max_complement_chars": 12,
                "reason": "disabled in fixture", "evidence_reference": "fixture",
            },
            "contextual_pronunciation": {
                "enabled": False, "rule_set_version": "zh-common-polyphones-v1",
                "evidence_reference": "fixture",
            },
            "contrastive_correction_break": {
                "enabled": False, "max_negated_chars": 48,
                "reason": "disabled in fixture", "evidence_reference": "fixture",
            },
            "predicate_completion_break": {
                "enabled": False, "max_clause_chars": 60,
                "reason": "disabled in fixture", "evidence_reference": "fixture",
            },
        },
    }


def _wav_samples(path: Path, samples: list[int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24_000)
        output.writeframes(b"".join(value.to_bytes(2, "little", signed=True) for value in samples))


def test_copula_complement_rule_generalizes_across_subjects_and_short_complements(tmp_path: Path) -> None:
    with Database(tmp_path / "copula-rule.sqlite") as db:
        _, profile = _project_and_profile(db, tmp_path)
        recipe_value = deepcopy(_reviewed_pause_recipe(profile))
        recipe_value["delivery_adjustments"] = {
            "schema_version": "1.4", "review_state": "pending_user_review",
            "syntax_rule_set_version": "zh-clause-binding-v1",
            "punctuation_overrides": [], "pronunciation_overrides": [], "generation_breaks": [],
            "copula_complement_break": {
                "enabled": True, "max_complement_chars": 12,
                "reason": "Copulas bind to a short complement and the sentence closes before the next sentence.",
                "evidence_reference": "user-voice-feedback-2026-11-07-phrase-rhythm-and-cha4",
            },
            "contextual_pronunciation": {
                "enabled": False, "rule_set_version": "zh-common-polyphones-v1",
                "evidence_reference": "fixture",
            },
            "contrastive_correction_break": {
                "enabled": False, "max_negated_chars": 48,
                "reason": "disabled in fixture", "evidence_reference": "fixture",
            },
            "predicate_completion_break": {
                "enabled": False, "max_clause_chars": 60,
                "reason": "disabled in fixture", "evidence_reference": "fixture",
            },
        }
        updated = profile.model_copy(update={"generation_recipe": recipe_value})
        from app.voice_recipe import apply_recipe_delivery_adjustments, parse_generation_recipe
        recipe = parse_generation_recipe(updated)
        assert recipe is not None

        examples = (
            ("最主要的原因，是照明。现在的城市主干道和绿道夜里灯火通明。",
             "最主要的原因是照明。\n\n现在的城市主干道和绿道夜里灯火通明。"),
            ("真正的问题，是成本。团队需要先验证最小方案。",
             "真正的问题是成本。\n\n团队需要先验证最小方案。"),
        )
        for copy, expected in examples:
            application = apply_recipe_delivery_adjustments(copy, recipe)
            assert application.provider_text == expected
            assert application.target_break_positions == (copy.index("。") + 1,)
            assert application.evidence["adjustments"][0]["kind"] == "copula_complement"

        combined = deepcopy(recipe_value)
        combined["delivery_adjustments"]["punctuation_overrides"] = [{
            "copy_text": "这不是跟风，是城市",
            "provider_text": "这不是跟风。是城市",
            "reason": "close the rhetorical first clause",
            "evidence_reference": "fixture",
        }]
        combined["delivery_adjustments"]["generation_breaks"] = [{
            "copy_break_after": "这不是跟风，",
            "provider_break_after": "这不是跟风。",
            "reason": "materialize the rhetorical first clause",
            "evidence_reference": "fixture",
        }]
        combined_recipe = parse_generation_recipe(
            profile.model_copy(update={"generation_recipe": combined})
        )
        copy = (
            "这不是跟风，是城市夜间环境真的变了。\n\n"
            "最主要的原因，是照明。现在的城市主干道和绿道夜里灯火通明。"
        )
        application = apply_recipe_delivery_adjustments(copy, combined_recipe)
        assert "这不是跟风。\n\n是城市夜间环境真的变了。" in application.provider_text
        assert "最主要的原因是照明。\n\n现在的城市主干道和绿道夜里灯火通明。" in application.provider_text
        assert application.evidence["adjustments"][-1]["kind"] == "copula_complement"


def _approved_u_voice_review() -> VoiceHumanReview:
    return VoiceHumanReview(
        approved=True,
        evidence_reference="u-voice:test-approved",
        findings=["Creator likeness, naturalness and delivery are publishable."],
        likeness=VoiceReviewOutcome.PASS,
        naturalness=VoiceReviewOutcome.PASS,
        emphasis=VoiceReviewOutcome.PASS,
        pace=VoiceReviewOutcome.PASS,
        pauses=VoiceReviewOutcome.PASS,
        rhythm=VoiceReviewOutcome.PASS,
        reviewed_at=datetime.now(timezone.utc),
    )


def test_voice_job_rejects_actual_provider_mismatch_but_not_changed_commercial_metadata(tmp_path: Path) -> None:
    with Database(tmp_path / "voice-model.sqlite") as db:
        project, profile = _project_and_profile(db, tmp_path)
        now = datetime.now(timezone.utc)
        capability = ProviderMachineCapabilityProfile(
            capability="voice", mode="local", provider="test-voice", model="admitted-model",
            runtime="fixture", machine_id="fixture", readiness="verified", quality_status="verified",
            commercial_status="commercial_safe", license_evidence_reference="fixture:license",
            provenance_source="fixture", evidence_reference="fixture:capability", updated_at=now,
        )
        ProviderMachineCapabilityProfileRepository(db).save(capability)
        job = Job(
            project_id=project.id, type=JobType.GENERATE_VOICE, status=JobStatus.RUNNING,
            idempotency_key="admitted-model", created_at=now, updated_at=now,
            payload=VoiceGenerationJobPayload(
                project_id=project.id, voice_profile_id=profile.id, text="New words.",
                authorization_reference="fixture:consent", expected_provider="test-voice",
                expected_model="admitted-model", execution_capability_profile_id=capability.id,
            ),
        )

        class WrongModel:
            provider_name = "test-voice"
            model = "different-model"

        handler = VoiceGenerationJobHandler(
            VoiceProfileRepository(db), AudioAssetRepository(db), object(), WrongModel(),
            tmp_path / "generated", ProviderCallLedger(db),
        )  # type: ignore[arg-type]
        with pytest.raises(JobExecutionError, match="configured Voice provider/model differs"):
            handler(job)
        assert ProviderCallRepository(db).list_for_project(project.id) == []
        WrongModel.model = "admitted-model"
        ProviderMachineCapabilityProfileRepository(db).save(capability.model_copy(update={
            "commercial_status": "unknown", "license_evidence_reference": None,
        }))
        with pytest.raises(JobExecutionError) as budget_stop:
            handler(job)
        assert budget_stop.value.code == "provider_budget_blocked"
        assert ProviderCallRepository(db).list_for_project(project.id) == []


@pytest.mark.parametrize("capability_license", [None, "non_commercial_only"])
def test_voice_job_imports_provenance_and_reserves_local_tts(tmp_path: Path, capability_license) -> None:
    db = Database(tmp_path / "voice.sqlite")
    try:
        project, profile = _project_and_profile(db, tmp_path)
        with db.transaction():
            BudgetPolicyRepository(db).save(BudgetPolicy(project_id=project.id, allow_unknown_cost=False, updated_at=datetime.now(timezone.utc)))
        now = datetime.now(timezone.utc)
        capability = None
        if capability_license:
            capability = ProviderMachineCapabilityProfile(capability="voice", mode="local",
                provider="test-voice", model="test-model", runtime="configured-runtime", machine_id="local",
                readiness="configured", quality_status="unknown", commercial_status=capability_license,
                license_evidence_reference="fixture:local-model-license", evidence_reference="fixture:installed-runtime",
                provenance_source="fixture", updated_at=now)
            ProviderMachineCapabilityProfileRepository(db).save(capability)
        payload = VoiceGenerationJobPayload(project_id=project.id, voice_profile_id=profile.id, text="这是新的旁白。", authorization_reference="voice-consent-1", language="zh",
            expected_provider=None if capability is None else capability.provider,
            expected_model=None if capability is None else capability.model,
            execution_capability_profile_id=None if capability is None else capability.id)
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
            def import_path(self, source: Path, authorization_reference: str, *, language: str | None = None, generated_job=None) -> AudioAsset:
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
            "project_id": str(project.id), "target_text": payload.text, "qa_state": "pending",
        }
        call = ProviderCallRepository(db).list_for_project(project.id)[0]
        assert call.operation == "tts" and call.status == "completed"
        assert call.estimated_cost.amount == Decimal("0")
        if capability is not None:
            assert audio.metadata["model_usage"] == [{"provider": "test-voice", "model": "test-model",
                "commercial_status": "non_commercial_only", "license_evidence_reference": "fixture:local-model-license",
                "usage_restrictions": ["non_commercial_only"]}]
            assert ProviderMachineCapabilityProfileRepository(db).get(capability.id).quality_status == "unknown"
            with pytest.raises(GeneratedNarrationQaPending):
                _require_generated_voice_qa(audio)
    finally:
        db.close()


def test_voice_job_applies_reviewed_pause_policy_and_preserves_segment_provenance(tmp_path: Path) -> None:
    db = Database(tmp_path / "voice-pause-policy.sqlite")
    try:
        project, profile = _project_and_profile(db, tmp_path)
        profile = VoiceProfileRepository(db).update(
            profile.model_copy(update={"generation_recipe": _reviewed_pause_recipe(profile)})
        )
        with db.transaction():
            BudgetPolicyRepository(db).save(BudgetPolicy(
                project_id=project.id, allow_unknown_cost=False,
                updated_at=datetime.now(timezone.utc),
            ))
        data_root = tmp_path / "data-root"
        data_root.mkdir()
        target_text = "先说明白。\n\n再讲重点。"
        now = datetime.now(timezone.utc)
        job = Job(
            project_id=project.id, type=JobType.GENERATE_VOICE,
            idempotency_key="reviewed-pause-policy", created_at=now, updated_at=now,
            payload=VoiceGenerationJobPayload(
                project_id=project.id, voice_profile_id=profile.id, text=target_text,
                authorization_reference="voice-consent-1", language="zh",
            ),
        )
        JobStore(db).enqueue(job)

        class Provider:
            provider_name = "test-voice"
            model = "test-model"
            is_local = True

            def synthesize(self, _profile: VoiceProfile, text: str, output_path: Path, *, language: str | None = None) -> VoiceSynthesisResult:
                assert text == target_text and language == "zh"
                first = [10_000] * (24_000 * 8 // 10) + [0] * (24_000 * 14 // 100)
                second = [0] * (24_000 * 14 // 100) + [12_000] * (24_000 * 8 // 10)
                parts_dir = output_path.parent / (output_path.stem + ".parts")
                first_path, second_path = parts_dir / "000.wav", parts_dir / "001.wav"
                _wav_samples(first_path, first)
                _wav_samples(second_path, second)
                _wav_samples(output_path, first + second)
                segments = (
                    {
                        "start_char": 0, "end_char": 5, "start_sample": 0,
                        "end_sample": len(first), "source_file": first_path.relative_to(data_root).as_posix(),
                        "sha256": hashlib.sha256(first_path.read_bytes()).hexdigest(),
                    },
                    {
                        "start_char": 7, "end_char": len(target_text),
                        "start_sample": len(first),
                        "end_sample": len(first) + len(second),
                        "source_file": second_path.relative_to(data_root).as_posix(),
                        "sha256": hashlib.sha256(second_path.read_bytes()).hexdigest(),
                    },
                )
                return VoiceSynthesisResult(
                    output_path, provider_version="test-version", generation_segments=segments,
                )

        class Importer:
            def import_path(self, source: Path, authorization_reference: str, *, language: str | None = None, generated_job=None) -> AudioAsset:
                with wave.open(str(source), "rb") as media:
                    duration_ms = round(media.getnframes() * 1000 / media.getframerate())
                value = AudioAsset(
                    source_file=str(source), content_hash=hashlib.sha256(source.read_bytes()).hexdigest(),
                    duration_ms=duration_ms, sample_rate=24_000, channels=1,
                    language=language, authorization_reference=authorization_reference,
                    imported_at=datetime.now(timezone.utc),
                )
                AudioAssetRepository(db).create(value)
                return value

        handler = VoiceGenerationJobHandler(
            VoiceProfileRepository(db), AudioAssetRepository(db), Importer(), Provider(),
            data_root, ProviderCallLedger(db),
        )
        result = JobRunner(
            JobStore(db), {JobType.GENERATE_VOICE: handler}, worker_id="voice-pause-worker",
            lease_duration=timedelta(minutes=1), max_attempts=1,
        ).run_once()

        assert result is not None and result.status is JobStatus.COMPLETED
        audio = AudioAssetRepository(db).list()[0]
        generation = audio.metadata["voice_generation"]
        assert generation["generation_recipe"]["version"] == 6
        assert generation["generation_recipe"]["pause_hierarchy"]["invariants"]["sentence_outranks_comma"] is True
        assert generation["pause_policy"]["state"] == "applied"
        assert generation["pause_policy"]["cuts"][0]["boundary_kind"] == "terminal"
        assert generation["pause_policy"]["global_speed_changed"] is False
        assert generation["generation_segments"][1]["final_start_sample"] == 24_000 * 86 // 100
        parts = generated_voice_qa_parts(audio, data_root)
        assert len(parts) == 2
        assert parts[1][1] == 860
    finally:
        db.close()


def test_voice_job_reuses_byte_identical_generation_and_records_new_recipe(tmp_path: Path) -> None:
    db = Database(tmp_path / "voice-identical-reuse.sqlite")
    try:
        project, profile = _project_and_profile(db, tmp_path)
        profile = VoiceProfileRepository(db).update(
            profile.model_copy(update={"generation_recipe": _reviewed_pause_recipe(profile)})
        )
        with db.transaction():
            BudgetPolicyRepository(db).save(BudgetPolicy(
                project_id=project.id, allow_unknown_cost=False,
                updated_at=datetime.now(timezone.utc),
            ))
        output = tmp_path / "generated" / "voice" / "identical.wav"
        output.parent.mkdir(parents=True)
        output.write_bytes(b"byte-identical-audio")
        digest = hashlib.sha256(output.read_bytes()).hexdigest()
        existing = AudioAsset(
            source_file=str(output), content_hash=digest, duration_ms=1_000,
            sample_rate=24_000, channels=1, language="zh",
            authorization_reference="voice-consent-1", imported_at=datetime.now(timezone.utc),
            metadata={"voice_generation": {
                "provider": "test-voice", "model": "test-model",
                "voice_profile_id": str(profile.id), "job_id": str(uuid4()),
                "project_id": str(project.id), "target_text": "这是同一个声音文件。",
                "qa_state": "verified", "human_review_state": "approved",
            }},
        )
        AudioAssetRepository(db).create(existing)
        now = datetime.now(timezone.utc)
        job = Job(
            project_id=project.id, type=JobType.GENERATE_VOICE,
            idempotency_key="identical-generation", created_at=now, updated_at=now,
            payload=VoiceGenerationJobPayload(
                project_id=project.id, voice_profile_id=profile.id,
                text="这是同一个声音文件。", authorization_reference="voice-consent-1",
                language="zh",
            ),
        )
        JobStore(db).enqueue(job)

        class Provider:
            provider_name = "test-voice"
            model = "test-model"
            is_local = True

            def synthesize(self, _profile: VoiceProfile, _text: str, output_path: Path, *, language: str | None = None) -> VoiceSynthesisResult:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(b"byte-identical-audio")
                return VoiceSynthesisResult(output_path, provider_version="test-version")

        class Importer:
            def import_path(self, _source: Path, _authorization_reference: str, *, language: str | None = None, generated_job=None) -> AudioAsset:
                return existing

        handler = VoiceGenerationJobHandler(
            VoiceProfileRepository(db), AudioAssetRepository(db), Importer(), Provider(),
            tmp_path / "generated", ProviderCallLedger(db),
        )
        result = JobRunner(
            JobStore(db), {JobType.GENERATE_VOICE: handler}, worker_id="identical-voice-worker",
            lease_duration=timedelta(minutes=1), max_attempts=1,
        ).run_once()

        assert result is not None and result.status is JobStatus.COMPLETED
        stored = AudioAssetRepository(db).get(existing.id)
        assert stored is not None
        generation = stored.metadata["voice_generation"]
        assert generation["qa_state"] == "verified"
        assert generation["human_review_state"] == "approved"
        assert generation["byte_identical_reexecutions"][0]["job_id"] == str(job.id)
        assert generation["byte_identical_reexecutions"][0]["generation"]["generation_recipe"]["version"] == 6
        assert ProviderCallRepository(db).list_for_project(project.id)[0].status == "completed"
    finally:
        db.close()


def test_voice_job_uses_recipe_delivery_text_but_keeps_original_copy_and_paragraph_spans(tmp_path: Path) -> None:
    db = Database(tmp_path / "voice-recipe-delivery.sqlite")
    try:
        project, profile = _project_and_profile(db, tmp_path)
        recipe = deepcopy(_reviewed_pause_recipe(profile))
        recipe["delivery_adjustments"] = {
            "schema_version": "1.4", "review_state": "pending_user_review",
            "syntax_rule_set_version": "zh-clause-binding-v1",
            "punctuation_overrides": [{
                "copy_text": "这不是跟风，是城市",
                "provider_text": "这不是跟风。是城市",
                "reason": "close the first claim before 是",
                "evidence_reference": "user-voice-feedback",
            }],
            "pronunciation_overrides": [],
            "generation_breaks": [{
                "copy_break_after": "这不是跟风，",
                "provider_break_after": "这不是跟风。",
                "reason": "materialize the completed first claim",
                "evidence_reference": "user-voice-feedback",
            }],
            "copula_complement_break": {
                "enabled": False, "max_complement_chars": 12,
                "reason": "disabled for explicit break fixture",
                "evidence_reference": "fixture",
            },
            "contextual_pronunciation": {
                "enabled": True, "rule_set_version": "zh-common-polyphones-v1",
                "evidence_reference": "user-voice-feedback",
            },
            "contrastive_correction_break": {
                "enabled": False, "max_negated_chars": 48,
                "reason": "disabled for explicit break fixture",
                "evidence_reference": "fixture",
            },
            "predicate_completion_break": {
                "enabled": False, "max_clause_chars": 60,
                "reason": "disabled for explicit break fixture",
                "evidence_reference": "fixture",
            },
        }
        profile = VoiceProfileRepository(db).update(
            profile.model_copy(update={"generation_recipe": recipe})
        )
        with db.transaction():
            BudgetPolicyRepository(db).save(BudgetPolicy(
                project_id=project.id, allow_unknown_cost=False,
                updated_at=datetime.now(timezone.utc),
            ))
        data_root = tmp_path / "data-root"
        data_root.mkdir()
        target_text = "这不是跟风，是城市夜间环境真的变了。\n\n照明差。"
        provider_text = "这不是跟风。\n\n是城市夜间环境真的变了。\n\n照明CHA4。"
        provider_parts = provider_text.split("\n\n")
        now = datetime.now(timezone.utc)
        job = Job(
            project_id=project.id, type=JobType.GENERATE_VOICE,
            idempotency_key="recipe-delivery", created_at=now, updated_at=now,
            payload=VoiceGenerationJobPayload(
                project_id=project.id, voice_profile_id=profile.id, text=target_text,
                authorization_reference="voice-consent-1", language="zh",
            ),
        )
        JobStore(db).enqueue(job)

        class Provider:
            provider_name = "test-voice"
            model = "test-model"
            is_local = True

            def synthesize(self, _profile: VoiceProfile, text: str, output_path: Path, *, language: str | None = None) -> VoiceSynthesisResult:
                assert text == provider_text and language == "zh"
                parts_dir = output_path.parent / (output_path.stem + ".parts")
                samples = [
                    [10_000] * (24_000 // 2),
                    [11_000] * (24_000 // 2),
                    [12_000] * (24_000 // 2),
                ]
                segments = []
                cursor = sample_cursor = 0
                all_samples = []
                for index, (copy, part_samples) in enumerate(zip(provider_parts, samples)):
                    part_path = parts_dir / f"{index:03}.wav"
                    _wav_samples(part_path, part_samples)
                    segments.append({
                        "start_char": cursor, "end_char": cursor + len(copy),
                        "start_sample": sample_cursor, "end_sample": sample_cursor + len(part_samples),
                        "source_file": part_path.relative_to(data_root).as_posix(),
                        "sha256": hashlib.sha256(part_path.read_bytes()).hexdigest(),
                    })
                    cursor += len(copy) + 2
                    sample_cursor += len(part_samples)
                    all_samples.extend(part_samples)
                _wav_samples(output_path, all_samples)
                return VoiceSynthesisResult(output_path, provider_version="test-version", generation_segments=segments)

        class Importer:
            def import_path(self, source: Path, authorization_reference: str, *, language: str | None = None, generated_job=None) -> AudioAsset:
                with wave.open(str(source), "rb") as media:
                    duration_ms = round(media.getnframes() * 1000 / media.getframerate())
                value = AudioAsset(
                    source_file=str(source), content_hash=hashlib.sha256(source.read_bytes()).hexdigest(),
                    duration_ms=duration_ms, sample_rate=24_000, channels=1,
                    language=language, authorization_reference=authorization_reference,
                    imported_at=datetime.now(timezone.utc),
                )
                AudioAssetRepository(db).create(value)
                return value

        handler = VoiceGenerationJobHandler(
            VoiceProfileRepository(db), AudioAssetRepository(db), Importer(), Provider(),
            data_root, ProviderCallLedger(db),
        )
        result = JobRunner(
            JobStore(db), {JobType.GENERATE_VOICE: handler}, worker_id="voice-delivery-worker",
            lease_duration=timedelta(minutes=1), max_attempts=1,
        ).run_once()

        assert result is not None and result.status is JobStatus.COMPLETED
        audio = AudioAssetRepository(db).list()[0]
        generation = audio.metadata["voice_generation"]
        assert generation["target_text"] == target_text
        assert generation["recipe_provider_text"] == provider_text
        assert generation["recipe_delivery"]["state"] == "applied"
        assert len(generation["recipe_delivery"]["adjustments"]) == 3
        assert [
            target_text[
                segment["start_char"]:
                segment["end_char"]
            ] for segment in generation["generation_segments"]
        ] == ["这不是跟风，", "是城市夜间环境真的变了。", "照明差。"]
        assert len(generated_voice_qa_parts(audio, data_root)) == 3
    finally:
        db.close()


def test_voice_job_import_and_independent_qa_run_offline_with_windows_copy_fallback(tmp_path: Path, monkeypatch) -> None:
    db = Database(tmp_path / "voice-import-qa.sqlite")
    try:
        project, profile = _project_and_profile(db, tmp_path)
        with db.transaction():
            BudgetPolicyRepository(db).save(BudgetPolicy(project_id=project.id, allow_unknown_cost=False, updated_at=datetime.now(timezone.utc)))

        target_text = "这是离线语音生产链路测试。"
        payload = VoiceGenerationJobPayload(
            project_id=project.id, voice_profile_id=profile.id, text=target_text,
            authorization_reference="fixture:voice-consent", language="zh",
        )
        now = datetime.now(timezone.utc)
        voice_job = Job(project_id=project.id, type=JobType.GENERATE_VOICE, idempotency_key="offline-voice-import-qa", created_at=now, updated_at=now, payload=payload)
        JobStore(db).enqueue(voice_job)

        def deny_hardlink(*_args, **_kwargs):
            raise PermissionError(13, "hard links denied by the Windows filesystem")

        monkeypatch.setattr(os, "link", deny_hardlink)

        class Provider:
            provider_name = "test-voice"
            model = "offline-fixture"
            is_local = True

            def synthesize(self, _profile: VoiceProfile, _text: str, output_path: Path, *, language: str | None = None) -> VoiceSynthesisResult:
                assert language == "zh"
                output_path.parent.mkdir(parents=True, exist_ok=True)
                sample_rate = 24_000
                frame_count = sample_rate * 2
                samples = [int(6_000 * math.sin(2 * math.pi * 440 * index / sample_rate)) for index in range(frame_count)]
                with wave.open(str(output_path), "wb") as sink:
                    sink.setnchannels(1)
                    sink.setsampwidth(2)
                    sink.setframerate(sample_rate)
                    sink.writeframes(struct.pack(f"<{frame_count}h", *samples))
                return VoiceSynthesisResult(output_path, provider_version="offline-fixture")

        class Probe:
            def probe_audio(self, _path: str | Path) -> AudioProbeMetadata:
                return AudioProbeMetadata(duration_ms=2_000, sample_rate=24_000, channels=1, language="zh", metadata={})

        data_root = tmp_path / "data-root"
        importer = AudioImporter(db, data_root, probe=Probe())
        voice_handler = VoiceGenerationJobHandler(
            VoiceProfileRepository(db), AudioAssetRepository(db), importer, Provider(),
            tmp_path / "worker-output", ProviderCallLedger(db),
        )
        voice_result = JobRunner(
            JobStore(db), {JobType.GENERATE_VOICE: voice_handler}, worker_id="offline-voice-worker",
            lease_duration=timedelta(minutes=1), max_attempts=1,
        ).run_once()

        assert voice_result is not None and voice_result.status is JobStatus.COMPLETED
        audio = AudioAssetRepository(db).list()[0]
        retained = Path(audio.source_file)
        assert retained.is_relative_to(data_root.resolve())
        assert hashlib.sha256(retained.read_bytes()).hexdigest() == audio.content_hash
        assert audio.metadata["voice_generation"]["qa_state"] == "pending"

        qa_job = Job(
            project_id=project.id, type=JobType.VERIFY_VOICE, idempotency_key="offline-independent-qa",
            created_at=now, updated_at=now,
            payload=VoiceQaJobPayload(project_id=project.id, narration_audio_id=audio.id, target_text=target_text),
        )
        JobStore(db).enqueue(qa_job)

        class ASRFixture:
            model = "offline-fixture"
            is_local = True

            def transcribe(self, audio_path: Path, *, language: str | None = None, prompt: str | None = None) -> TranscriptionResult:
                assert Path(audio_path) == retained and language == "zh" and prompt is None
                return TranscriptionResult(target_text, (TranscriptionSegment(0, 1_900, target_text),), "zh")

        qa_handler = VoiceQaJobHandler(
            AudioAssetRepository(db), ASRFixture(), ProviderCallLedger(db),
            provider_name="offline-fixture-asr", provider_model="offline-fixture", data_root=data_root,
        )
        qa_result = JobRunner(
            JobStore(db), {JobType.VERIFY_VOICE: qa_handler}, worker_id="offline-qa-worker",
            lease_duration=timedelta(minutes=1), max_attempts=1,
        ).run_once()

        assert qa_result is not None and qa_result.status is JobStatus.COMPLETED
        verified = AudioAssetRepository(db).get(audio.id)
        assert verified is not None
        generation = verified.metadata["voice_generation"]
        assert generation["qa_state"] == "verified"
        assert generation["human_review_state"] == "pending"
        assert generation["qa"]["copy_coverage"] == 1.0
        calls = ProviderCallRepository(db).list_for_project(project.id)
        assert [(call.operation, call.status) for call in calls] == [("tts", "completed"), ("asr", "completed")]
    finally:
        db.close()


def test_voice_job_selected_reference_is_applied_and_persisted(tmp_path: Path) -> None:
    db = Database(tmp_path / "selected-reference.sqlite")
    try:
        project, profile = _project_and_profile(db, tmp_path)
        with db.transaction():
            BudgetPolicyRepository(db).save(BudgetPolicy(project_id=project.id, allow_unknown_cost=False, updated_at=datetime.now(timezone.utc)))
        selection = VoiceReferenceWindowSelection(clip_id=profile.reference_clip_ids[0], asset_id=uuid4(), source_content_hash="a" * 64, start_ms=0, end_ms=6_000, transcript_sha256="b" * 64)
        payload = VoiceGenerationJobPayload(project_id=project.id, voice_profile_id=profile.id, text="新的，测试文案", delivery_text="新的测试文案", authorization_reference="voice-consent", reference_window=selection)
        now = datetime.now(timezone.utc)
        job = Job(project_id=project.id, type=JobType.GENERATE_VOICE, idempotency_key="selected-reference", created_at=now, updated_at=now, payload=payload)
        JobStore(db).enqueue(job)

        class Provider:
            provider_name = "test-voice"
            model = "test-model"
            is_local = True

            def synthesize_with_reference(self, received: VoiceProfile, text: str, output_path: Path, *, language: str | None, reference_window: VoiceReferenceWindowSelection) -> VoiceSynthesisResult:
                assert received.id == profile.id and text == payload.delivery_text and reference_window == selection
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(b"generated-audio")
                return VoiceSynthesisResult(output_path)

        class Importer:
            def import_path(self, source: Path, authorization_reference: str, *, language: str | None = None, generated_job=None) -> AudioAsset:
                value = AudioAsset(source_file=str(source), content_hash="c" * 64, duration_ms=1_200, sample_rate=24_000, channels=1, authorization_reference=authorization_reference, imported_at=datetime.now(timezone.utc))
                AudioAssetRepository(db).create(value)
                return value

        handler = VoiceGenerationJobHandler(VoiceProfileRepository(db), AudioAssetRepository(db), Importer(), Provider(), tmp_path / "generated", ProviderCallLedger(db))  # type: ignore[arg-type]
        result = JobRunner(JobStore(db), {JobType.GENERATE_VOICE: handler}, worker_id="selected-worker", lease_duration=timedelta(minutes=1), max_attempts=2).run_once()
        assert result is not None and result.status is JobStatus.COMPLETED
        generated = AudioAssetRepository(db).list()[0].metadata["voice_generation"]
        assert generated["reference_window"] == selection.model_dump(mode="json")
        assert generated["target_text"] == payload.text
        assert generated["provider_delivery_text"] == payload.delivery_text
        audio_id = AudioAssetRepository(db).list()[0].id
        with TestClient(create_app(tmp_path / "selected-reference.sqlite")) as client:
            qa = client.post(f"/projects/{project.id}/voice-qa-jobs", json={
                "idempotency_key": "selected-reference-editorial-qa", "narration_audio_id": str(audio_id),
                "target_text": payload.text,
            })
            assert qa.status_code == 201
            assert client.post(f"/projects/{project.id}/voice-qa-jobs", json={
                "idempotency_key": "selected-reference-delivery-qa", "narration_audio_id": str(audio_id),
                "target_text": payload.delivery_text,
            }).status_code == 422
    finally:
        db.close()


def test_voice_job_api_is_typed_and_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "voice-api.sqlite"
    db = Database(path)
    project, profile = _project_and_profile(db, tmp_path)
    from app.db import AssetRepository
    from app.domain.models import Asset, TranscriptSegment
    source=tmp_path/"confirmed-reference.mp4"
    source.write_bytes(b"synthetic admitted reference")
    asset=AssetRepository(db).create(Asset(source_file=str(source),content_hash=hashlib.sha256(source.read_bytes()).hexdigest(),
        duration_ms=1000,width=1080,height=1920,fps=project.fps,has_audio=True,
        authorization_reference="voice-consent-1",imported_at=project.created_at))
    ClipRepository(db).create(Clip(id=profile.reference_clip_ids[0],asset_id=asset.id,start_ms=0,end_ms=1000,
        asset_duration_ms=1000,voice_candidate=True,transcript="参考",transcript_source="owner-confirmed-reference:fixture",
        transcript_segments=[TranscriptSegment(start_ms=0,end_ms=1000,text="参考")]))
    profile=VoiceProfileRepository(db).update(profile.model_copy(update={"consent":profile.consent.model_copy(update={"authorization_reference":"voice-consent-1"})}))
    db.close()
    request = {
        "idempotency_key": "voice-api", "voice_profile_id": str(profile.id), "text": "新的文本", "language": "zh",
    }
    with TestClient(create_app(path)) as client:
        first = client.post(f"/projects/{project.id}/voice-jobs", json=request)
        assert first.status_code == 201, first.text
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
            source_file=source.name, content_hash=hashlib.sha256(source.read_bytes()).hexdigest(), duration_ms=2_000, sample_rate=24_000, channels=1,
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
            provider_name="faster-whisper", provider_model="qa-model", data_root=tmp_path,
        )
        result = JobRunner(JobStore(db), {JobType.VERIFY_VOICE: handler}, worker_id="voice-qa-worker", lease_duration=timedelta(minutes=1), max_attempts=2).run_once()

        assert result is not None and result.status is JobStatus.COMPLETED
        updated = AudioAssetRepository(db).get(audio.id)
        assert updated is not None
        generation = updated.metadata["voice_generation"]
        assert generation["qa_state"] == "verified"
        assert generation["human_review_state"] == "pending"
        assert generation["qa"]["copy_coverage"] == 1.0
        assert updated.transcript_source == "faster-whisper:qa-model"
        call = ProviderCallRepository(db).list_for_project(project.id)[0]
        assert call.operation == "asr" and call.status == "completed" and call.estimated_cost.amount == Decimal("0")
    finally:
        db.close()


def test_voice_qa_worker_rejects_changed_portable_source_before_asr(tmp_path: Path) -> None:
    with Database(tmp_path / "voice-qa-changed.sqlite") as db:
        project, _ = _project_and_profile(db, tmp_path)
        source = tmp_path / "changed.wav"
        source.write_bytes(b"changed")
        audio = AudioAsset(
            source_file=source.name, content_hash=hashlib.sha256(b"original").hexdigest(),
            duration_ms=1_000, sample_rate=24_000, channels=1,
            authorization_reference="fixture:consent", imported_at=datetime.now(timezone.utc),
            metadata={"voice_generation": {"target_text": "New words.", "qa_state": "pending"}},
        )
        AudioAssetRepository(db).create(audio)

        class ASR:
            is_local = True

            def transcribe(self, *args: object, **kwargs: object) -> None:
                raise AssertionError("changed source must not reach ASR")

        now = datetime.now(timezone.utc)
        job = Job(
            project_id=project.id, type=JobType.VERIFY_VOICE, status=JobStatus.RUNNING,
            idempotency_key="qa-changed", created_at=now, updated_at=now,
            payload=VoiceQaJobPayload(project_id=project.id, narration_audio_id=audio.id, target_text="New words."),
        )
        handler = VoiceQaJobHandler(
            AudioAssetRepository(db), ASR(), ProviderCallLedger(db),
            provider_name="faster-whisper", provider_model="fixture", data_root=tmp_path,
        )  # type: ignore[arg-type]
        with pytest.raises(JobExecutionError, match="source bytes changed"):
            handler(job)
        assert ProviderCallRepository(db).list_for_project(project.id) == []


def test_segmented_voice_qa_resolves_data_root_prefixed_master_path(tmp_path):
    from app.voice_qa import generated_voice_qa_parts
    import struct

    data_root = tmp_path / "content-os-data"
    generated = data_root / "generated" / "voice" / "parts"
    generated.mkdir(parents=True)
    text = "第一段。\n\n第二段。"
    raw = struct.pack("<h", 1000) * 24_000
    parts = []
    for index in range(2):
        source = generated / f"{index}.wav"
        with wave.open(str(source), "wb") as output:
            output.setparams((1, 2, 24_000, 0, "NONE", "not compressed"))
            output.writeframes(raw)
        parts.append(dict(source_file=source.relative_to(data_root).as_posix(),
            sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            start_char=(0, 6)[index], end_char=(4, 10)[index],
            start_sample=index * 24_000, end_sample=(index + 1) * 24_000))
    master = data_root / "master.wav"
    with wave.open(str(master), "wb") as output:
        output.setparams((1, 2, 24_000, 0, "NONE", "not compressed"))
        output.writeframes(raw * 2)
    audio = AudioAsset(source_file="content-os-data/master.wav",
        content_hash=hashlib.sha256(master.read_bytes()).hexdigest(),
        duration_ms=2_000, sample_rate=24_000, channels=1, language="zh",
        authorization_reference="fixture:consent", imported_at=datetime.now(timezone.utc),
        metadata={"voice_generation": {"target_text": text, "qa_state": "pending",
            "generation_segments": parts}})

    assert len(generated_voice_qa_parts(audio, data_root)) == 2


@pytest.mark.parametrize("fault", [None, "part_changed", "master_changed", "copy_missing"])
def test_segmented_voice_qa_checks_exact_pcm_and_never_prompts_or_retries(tmp_path, fault):
    from app.voice_qa import generated_voice_qa_parts
    from app.providers.asr import FasterWhisperASRProvider
    with Database(tmp_path / "parts.sqlite") as db:
        project, _ = _project_and_profile(db, tmp_path)
        parts = []; raw_parts = []; text = "第一段。\n\n第二段。"
        for index, (first, last) in enumerate(((0, 4), (6, 10))):
            source = tmp_path / f"part-{index}.wav"
            raw = struct.pack("<h", 1000 + index) * 24_000
            with wave.open(str(source), "wb") as output:
                output.setparams((1, 2, 24_000, 0, "NONE", "not compressed")); output.writeframes(raw)
            raw_parts.append(raw)
            parts.append(dict(source_file=source.name, sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                start_char=first, end_char=last, start_sample=index*24_000, end_sample=(index+1)*24_000))
        master = tmp_path / "master.wav"
        with wave.open(str(master), "wb") as output:
            output.setparams((1, 2, 24_000, 0, "NONE", "not compressed")); output.writeframes(b"".join(raw_parts))
        audio = AudioAsset(source_file=str(master), content_hash=hashlib.sha256(master.read_bytes()).hexdigest(),
            duration_ms=2000, sample_rate=24_000, channels=1, language="zh", authorization_reference="fixture:consent",
            imported_at=datetime.now(timezone.utc), metadata={"voice_generation":{
                "target_text":text,"qa_state":"pending","generation_segments":parts}})
        assert len(generated_voice_qa_parts(audio, tmp_path)) == 2
        if fault == "part_changed":
            (tmp_path / parts[0]["source_file"]).write_bytes(b"changed")
        if fault == "master_changed":
            # Even metadata naming the current Master cannot substitute different PCM for its parts.
            with wave.open(str(master), "wb") as output:
                output.setparams((1, 2, 24_000, 0, "NONE", "not compressed")); output.writeframes(b"\x01\x00"*48_000)
            audio=audio.model_copy(update={"content_hash":hashlib.sha256(master.read_bytes()).hexdigest()})
        AudioAssetRepository(db).create(audio)
        BudgetPolicyRepository(db).save(BudgetPolicy(project_id=project.id, max_calls=1, updated_at=datetime.now(timezone.utc)))
        seen=[]; loads=[]
        class Model:
            def transcribe(self, path, **kwargs):
                assert kwargs["initial_prompt"] is None
                index=int(Path(path).stem[-1]);seen.append(index)
                copy="一段。" if fault=="copy_missing" and index==0 else ("第一段。","第二段。")[index]
                return iter([type("S",(),dict(start=0,end=.9,text=copy))()]), type("Info",(),{"language":"zh"})()
        def factory(*args,**kwargs):loads.append(1);return Model()
        asr=FasterWhisperASRProvider("fixture",model_factory=factory)
        now=datetime.now(timezone.utc)
        job=Job(project_id=project.id,type=JobType.VERIFY_VOICE,status=JobStatus.RUNNING,
            idempotency_key="segmented-qa",created_at=now,updated_at=now,
            payload=VoiceQaJobPayload(project_id=project.id,narration_audio_id=audio.id,target_text=text))
        handler=VoiceQaJobHandler(AudioAssetRepository(db),asr,ProviderCallLedger(db),
            provider_name="faster-whisper",provider_model="fixture",data_root=tmp_path)
        if fault:
            with pytest.raises(JobExecutionError):handler(job)
        else:handler(job)
        calls=ProviderCallRepository(db).list_for_project(project.id)
        if fault in {"part_changed","master_changed"}:
            assert seen==loads==calls==[]
        else:
            assert seen==[0,1] and loads==[1] and len(calls)==1 and calls[0].status=="completed"
            saved=AudioAssetRepository(db).get(audio.id)
            qa=saved.metadata["voice_generation"]["qa"]
            assert qa["source_part_count"]==2 and qa["transcription_scope"]=="generation_parts"
            assert len(qa["generation_part_reports"]) == 2
            assert [entry["report"]["qa_state"] for entry in qa["generation_part_reports"]] == (
                ["failed", "verified"] if fault == "copy_missing" else ["verified", "verified"])
            assert [entry["source_sha256"] for entry in qa["generation_part_reports"]] == [part["sha256"] for part in parts]
            assert qa["missing_token_count"]==(1 if fault=="copy_missing" else 0)
            assert [(s.start_ms,s.end_ms) for s in saved.transcript_segments]==[(0,900),(1000,1900)]


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
    from app.production_runs import has_approved_voice_quality
    audio = AudioAsset(
        source_file="generated.wav", content_hash="b" * 64, duration_ms=1_000, sample_rate=24_000, channels=1,
        authorization_reference="voice-consent-1", imported_at=datetime.now(timezone.utc),
        metadata={"voice_generation": {"provider": "test-voice", "qa_state": "pending"}},
    )
    with pytest.raises(GeneratedNarrationQaPending):
        _require_generated_voice_qa(audio)
    assert not has_approved_voice_quality(None, audio)
    technical = audio.model_copy(update={"metadata": {"voice_generation": {"provider": "test-voice", "qa_state": "verified"}}})
    with pytest.raises(GeneratedNarrationQaPending, match="U-Voice"):
        _require_generated_voice_qa(technical)
    assert not has_approved_voice_quality(None, technical)
    approved = apply_voice_human_review(technical, _approved_u_voice_review())
    assert voice_human_review_status(approved) == "approved"
    _require_generated_voice_qa(approved)
    assert has_approved_voice_quality(None, approved)


def test_voice_human_review_api_persists_all_dimensions_and_cannot_be_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "voice-human-review.sqlite"
    db = Database(path)
    try:
        project, _ = _project_and_profile(db, tmp_path)
        job = Job(
            project_id=project.id,
            type=JobType.GENERATE_VOICE,
            idempotency_key="voice-human-review-source",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        JobStore(db).enqueue(job)
        source = tmp_path / "reviewed.wav"
        source.write_bytes(b"playable")
        audio = AudioAsset(
            source_file=str(source), content_hash="h" * 64, duration_ms=1_000, sample_rate=24_000, channels=1,
            authorization_reference="voice-consent-1", imported_at=datetime.now(timezone.utc),
            metadata={"voice_generation": {"provider": "test-voice", "job_id": str(job.id), "qa_state": "verified", "human_review_state": "pending"}},
        )
        AudioAssetRepository(db).create(audio)
    finally:
        db.close()

    approved = {
        "approved": True,
        "evidence_reference": "u-voice:reviewed-asset",
        "findings": ["The planned delivery is natural and publishable."],
        "likeness": "pass",
        "naturalness": "pass",
        "emphasis": "pass",
        "pace": "pass",
        "pauses": "pass",
        "rhythm": "pass",
    }
    with TestClient(create_app(path)) as client:
        inconsistent = client.post(
            f"/projects/{project.id}/voice-assets/{audio.id}/human-review",
            json={**approved, "pace": "needs_revision"},
        )
        assert inconsistent.status_code == 422
        response = client.post(f"/projects/{project.id}/voice-assets/{audio.id}/human-review", json=approved)
        assert response.status_code == 200
        review = response.json()["metadata"]["voice_generation"]["human_review"]
        assert response.json()["metadata"]["voice_generation"]["human_review_state"] == "approved"
        assert review["emphasis"] == "pass" and review["rhythm"] == "pass"
        assert review["reviewed_at"]
        repeated = client.post(f"/projects/{project.id}/voice-assets/{audio.id}/human-review", json=approved)
        assert repeated.status_code == 422


def test_voice_human_rejection_remains_ineligible_for_assembly() -> None:
    audio = AudioAsset(
        source_file="rejected.wav", content_hash="j" * 64, duration_ms=1_000, sample_rate=24_000, channels=1,
        authorization_reference="voice-consent-1", imported_at=datetime.now(timezone.utc),
        metadata={"voice_generation": {"provider": "test-voice", "qa_state": "verified", "human_review_state": "pending"}},
    )
    rejected = apply_voice_human_review(
        audio,
        VoiceHumanReview(
            approved=False,
            evidence_reference="u-voice:test-needs-revision",
            findings=["The hook lacks the intended emphasis and rhythmic turn."],
            likeness=VoiceReviewOutcome.PASS,
            naturalness=VoiceReviewOutcome.PASS,
            emphasis=VoiceReviewOutcome.NEEDS_REVISION,
            pace=VoiceReviewOutcome.PASS,
            pauses=VoiceReviewOutcome.PASS,
            rhythm=VoiceReviewOutcome.NEEDS_REVISION,
            reviewed_at=datetime.now(timezone.utc),
        ),
    )
    assert voice_human_review_status(rejected) == "rejected"
    with pytest.raises(GeneratedNarrationQaPending, match="U-Voice"):
        _require_generated_voice_qa(rejected)
    with pytest.raises(VoiceQaError, match="already recorded"):
        apply_voice_human_review(rejected, _approved_u_voice_review())


def test_voice_human_review_api_resolves_a_legacy_composed_master_from_source_takes(tmp_path: Path) -> None:
    path = tmp_path / "legacy-composed-master.sqlite"
    db = Database(path)
    try:
        project, _ = _project_and_profile(db, tmp_path)
        takes: list[AudioAsset] = []
        for index in range(2):
            job = Job(
                project_id=project.id,
                type=JobType.GENERATE_VOICE,
                idempotency_key=f"legacy-source-{index}",
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            JobStore(db).enqueue(job)
            take = AudioAsset(
                source_file=str(tmp_path / f"take-{index}.wav"), content_hash=(str(index + 1) * 64),
                duration_ms=1_000, sample_rate=24_000, channels=1,
                authorization_reference="voice-consent-1", imported_at=datetime.now(timezone.utc),
                metadata={"voice_generation": {"provider": "test-voice", "job_id": str(job.id), "qa_state": "verified"}},
            )
            AudioAssetRepository(db).create(take)
            takes.append(take)
        master = AudioAsset(
            source_file=str(tmp_path / "legacy-master.wav"), content_hash="m" * 64,
            duration_ms=2_000, sample_rate=24_000, channels=1,
            authorization_reference="voice-consent-1", imported_at=datetime.now(timezone.utc),
            metadata={"voice_generation": {
                "provider": "composed_voice_takes",
                "qa_state": "verified",
                "human_review_state": "pending",
                "composition": {"source_take_audio_ids": [str(take.id) for take in takes]},
            }},
        )
        AudioAssetRepository(db).create(master)
    finally:
        db.close()

    request = {
        "approved": True,
        "evidence_reference": "u-voice:legacy-master",
        "findings": ["The composed master is approved after direct review."],
        "likeness": "pass",
        "naturalness": "pass",
        "emphasis": "pass",
        "pace": "pass",
        "pauses": "pass",
        "rhythm": "pass",
    }
    with TestClient(create_app(path)) as client:
        response = client.post(f"/projects/{project.id}/voice-assets/{master.id}/human-review", json=request)
        assert response.status_code == 200
        assert response.json()["metadata"]["voice_generation"]["human_review_state"] == "approved"


def test_voice_qa_requires_real_timing_and_records_copy_and_silence_evidence(tmp_path: Path) -> None:
    source = tmp_path / "generated.wav"
    source.write_bytes(b"playable")
    audio = AudioAsset(
        source_file=str(source), content_hash="c" * 64, duration_ms=2_000, sample_rate=24_000, channels=1,
        authorization_reference="voice-consent-1", imported_at=datetime.now(timezone.utc),
        metadata={"voice_generation": {"provider": "test-voice", "qa_state": "pending"}},
    )
    transcript = TranscriptionResult(
        "你好世界", (TranscriptionSegment(0, 1_000, "你好世界"),),
        words=(TranscriptionWord(0, 400, "你好"), TranscriptionWord(600, 1_000, "世界")),
    )
    report = verify_generated_voice(audio, "你好世界", transcript, max_silence_ms=1_100)
    assert report.verified and report.copy_coverage == 1 and report.substitution_token_count == 0 and report.longest_silence_ms == 1_000
    updated = apply_voice_qa(audio, report, transcript, provider="qa-asr", model="qa-model")
    assert updated.metadata["voice_generation"]["qa_state"] == "verified"
    assert updated.transcript_source == "qa-asr:qa-model"
    assert updated.metadata["voice_word_timing"]["source_sha256"] == audio.content_hash
    assert updated.metadata["voice_word_timing"]["words"][1] == {
        "start_ms": 600, "end_ms": 1000, "text": "世界"}
    failed = verify_generated_voice(audio, "你好世界", TranscriptionResult("你好", (TranscriptionSegment(0, 500, "你好"),)))
    assert not failed.verified and failed.missing_token_count == 2 and "copy_missing_tokens" in failed.checks


def test_voice_qa_clamps_small_asr_segment_overlap_without_dropping_text(tmp_path: Path) -> None:
    source = tmp_path / "overlap.wav"
    source.write_bytes(b"playable")
    audio = AudioAsset(
        source_file=str(source), content_hash="e" * 64, duration_ms=2_000,
        sample_rate=24_000, channels=1, authorization_reference="voice-consent-1",
        imported_at=datetime.now(timezone.utc),
        metadata={"voice_generation": {
            "provider": "test-voice", "qa_state": "pending", "target_text": "你好世界",
        }},
    )
    overlapping = TranscriptionResult(
        "你好世界",
        (
            TranscriptionSegment(0, 1_000, "你好"),
            TranscriptionSegment(950, 2_000, "世界"),
        ),
        words=(
            TranscriptionWord(0, 400, "你好"),
            TranscriptionWord(950, 1_500, "世界"),
        ),
    )
    report = verify_generated_voice(audio, "你好世界", overlapping)
    assert report.verified

    updated = apply_voice_qa(
        audio, report, overlapping, provider="qa-asr", model="qa-model",
    )

    assert [
        (item.start_ms, item.end_ms, item.text)
        for item in updated.transcript_segments
    ] == [(0, 1_000, "你好"), (1_000, 2_000, "世界")]
    assert updated.metadata["voice_generation"]["qa"]["transcript_timing_normalization"] == {
        "method": "clamp_later_segment_start_to_previous_end",
        "max_allowed_overlap_ms": 250,
        "overlap_count": 1,
        "max_overlap_ms": 50,
    }

    excessive = TranscriptionResult(
        "你好世界",
        (
            TranscriptionSegment(0, 1_000, "你好"),
            TranscriptionSegment(700, 2_000, "世界"),
        ),
    )
    with pytest.raises(VoiceQaError, match="segment_timestamps_overlap"):
        apply_voice_qa(
            audio, verify_generated_voice(audio, "你好世界", excessive),
            excessive, provider="qa-asr", model="qa-model",
        )


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


def test_voice_qa_normalizes_only_technical_identifier_punctuation(tmp_path: Path) -> None:
    source = tmp_path / "generated.wav"
    source.write_bytes(b"playable")
    audio = AudioAsset(
        source_file=str(source), content_hash="t" * 64, duration_ms=2_000, sample_rate=24_000, channels=1,
        authorization_reference="voice-consent-1", imported_at=datetime.now(timezone.utc),
        metadata={"voice_generation": {"provider": "test-voice", "qa_state": "pending"}},
    )
    assert comparison_tokens("H.265") == comparison_tokens("H265") == ("h265",)
    # Sentence punctuation remains a boundary; this is not broad punctuation
    # deletion or an ASR-friendly relaxation for ordinary copy.
    assert comparison_tokens("first. second") == ("first", "second")

    report = verify_generated_voice(
        audio,
        "H.265 码率",
        TranscriptionResult("H265 马力", (TranscriptionSegment(0, 1_000, "H265 马力"),)),
    )
    assert report.missing_token_count == 0
    assert report.substitution_token_count == 2
    assert not report.verified and "copy_substitution_tokens" in report.checks


def test_voice_qa_compares_spoken_and_ascii_quantity_styles(tmp_path: Path) -> None:
    source = tmp_path / "generated.wav"
    source.write_bytes(b"playable")
    audio = AudioAsset(
        source_file=str(source), content_hash="q" * 64, duration_ms=2_000, sample_rate=24_000, channels=1,
        authorization_reference="voice-consent-1", imported_at=datetime.now(timezone.utc),
        metadata={"voice_generation": {"provider": "test-voice", "qa_state": "pending"}},
    )
    assert comparison_tokens("两千年前") == comparison_tokens("2000年前")
    assert comparison_tokens("三步") == comparison_tokens("3步")
    # Only explicit counting/time units are normalized; ordinary wording is not.
    assert comparison_tokens("万一") == ("万", "一")
    assert comparison_tokens("一直") == ("一", "直")

    target = "焦虑时代，两千年前的人们仍会面对同样的选择。"
    observed = "焦虑时代2000年前的人们仍会面对同样的选择"
    report = verify_generated_voice(
        audio,
        target,
        TranscriptionResult(observed, (TranscriptionSegment(0, 1_500, observed),)),
    )
    assert report.verified
    assert report.missing_token_count == 0 and report.duplicate_token_count == 0
    assert report.substitution_token_count == 0
    assert report.phonetic_equivalences == ()
    assert report.metadata(provider="qa-asr", model="qa-model")["quantity_style_normalization"] == "spoken_quantity_units_v1"


def test_voice_qa_rejects_unresolved_semantic_substitutions(tmp_path: Path) -> None:
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
    assert not report.verified
    assert report.missing_token_count == 0 and report.duplicate_token_count == 0
    assert report.substitution_token_count == 1
    assert "copy_substitution_tokens" in report.checks

    tone_changed = verify_generated_voice(
        audio,
        "痛苦来自判断",
        TranscriptionResult("痛哭来自判断", (TranscriptionSegment(0, 1_500, "痛哭来自判断"),)),
    )
    assert not tone_changed.verified
    assert "copy_substitution_tokens" in tone_changed.checks

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


def test_voice_qa_measures_pcm_onset_instead_of_late_asr_timestamp(tmp_path: Path) -> None:
    source = tmp_path / "generated.wav"
    with wave.open(str(source), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24_000)
        output.writeframes(b"\0\0" * 2_400 + b"\xff\x1f" * 45_600)
    audio = AudioAsset(
        source_file=str(source), content_hash="o" * 64, duration_ms=2_000, sample_rate=24_000, channels=1,
        authorization_reference="voice-consent-1", imported_at=datetime.now(timezone.utc),
        metadata={"voice_generation": {"provider": "test-voice", "qa_state": "pending"}},
    )
    report = verify_generated_voice(
        audio,
        "你好世界",
        TranscriptionResult("你好世界", (TranscriptionSegment(650, 1_600, "你好世界"),)),
    )
    assert report.verified
    assert report.leading_silence_ms == 100
    assert report.leading_silence_source == "pcm_waveform"


def test_voice_recovery_never_treats_copy_loss_as_a_silence_trim(tmp_path: Path) -> None:
    source = tmp_path / "generated.wav"
    source.write_bytes(b"playable")
    audio = AudioAsset(
        source_file=str(source), content_hash="r" * 64, duration_ms=2_000, sample_rate=24_000, channels=1,
        authorization_reference="voice-consent-1", imported_at=datetime.now(timezone.utc),
        metadata={"voice_generation": {"provider": "test-voice", "qa_state": "pending"}},
    )
    copy_and_silence = verify_generated_voice(
        audio,
        "你好世界",
        TranscriptionResult("你好", (TranscriptionSegment(650, 1_500, "你好"),)),
    )
    recommendation = recommend_voice_recovery(copy_and_silence)
    assert recommendation.route is VoiceRecoveryRoute.REGENERATE_SENTENCE_TAKES
    assert recommendation.requires_new_voice_generation
    assert not recommendation.requires_derived_audio
    assert recommendation.requires_fresh_voice_qa

    updated = apply_voice_qa(audio, copy_and_silence, TranscriptionResult("你好", (TranscriptionSegment(650, 1_500, "你好"),)), provider="qa-asr", model="qa-model")
    assert updated.metadata["voice_generation"]["qa_state"] == "failed"
    assert updated.metadata["voice_generation"]["recovery"] == recommendation.metadata()


def test_voice_recovery_allows_only_pure_leading_silence_to_request_normalization(tmp_path: Path) -> None:
    source = tmp_path / "generated.wav"
    source.write_bytes(b"playable")
    audio = AudioAsset(
        source_file=str(source), content_hash="n" * 64, duration_ms=2_000, sample_rate=24_000, channels=1,
        authorization_reference="voice-consent-1", imported_at=datetime.now(timezone.utc),
        metadata={"voice_generation": {"provider": "test-voice", "qa_state": "pending"}},
    )
    silence_only = verify_generated_voice(
        audio,
        "你好世界",
        TranscriptionResult("你好世界", (TranscriptionSegment(650, 1_500, "你好世界"),)),
    )
    recommendation = recommend_voice_recovery(silence_only)
    assert recommendation.route is VoiceRecoveryRoute.NORMALIZE_LEADING_SILENCE
    assert not recommendation.requires_new_voice_generation
    assert recommendation.requires_derived_audio
    assert recommendation.requires_fresh_voice_qa


def test_voice_recovery_is_visible_from_the_existing_audio_asset_surface(tmp_path: Path) -> None:
    path = tmp_path / "voice-recovery-api.sqlite"
    db = Database(path)
    try:
        source = tmp_path / "generated.wav"
        source.write_bytes(b"playable")
        audio = AudioAsset(
            source_file=str(source), content_hash="v" * 64, duration_ms=2_000, sample_rate=24_000, channels=1,
            authorization_reference="voice-consent-1", imported_at=datetime.now(timezone.utc),
            metadata={"voice_generation": {"provider": "test-voice", "qa_state": "pending"}},
        )
        report = verify_generated_voice(
            audio,
            "你好世界",
            TranscriptionResult("你好世界", (TranscriptionSegment(650, 1_500, "你好世界"),)),
        )
        AudioAssetRepository(db).create(apply_voice_qa(
            audio, report,
            TranscriptionResult("你好世界", (TranscriptionSegment(650, 1_500, "你好世界"),)),
            provider="qa-asr", model="qa-model",
        ))
        audio_id = audio.id
    finally:
        db.close()

    with TestClient(create_app(path)) as client:
        response = client.get(f"/audio-assets/{audio_id}")
        assert response.status_code == 200
        recovery = response.json()["metadata"]["voice_generation"]["recovery"]
        assert recovery["route"] == VoiceRecoveryRoute.NORMALIZE_LEADING_SILENCE.value
        assert recovery["source_audio_preserved"] is True
