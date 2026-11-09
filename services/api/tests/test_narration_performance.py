from __future__ import annotations

from array import array
from datetime import datetime, timedelta, timezone
import hashlib
import math
from pathlib import Path
import wave

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.budget import ProviderCallLedger
from app.db import AssetRepository, AudioAssetRepository, ClipRepository, Database, IPProfileRepository, JobRepository, ProjectRepository, ProviderCallRepository, VoiceProfileRepository
from app.domain.models import (
    Asset,
    AudioAsset,
    Clip,
    ConsentRecord,
    IPProfile,
    Job,
    JobStatus,
    JobType,
    NarrationEmphasis,
    NarrationPace,
    NarrationPause,
    NarrationPerformanceCue,
    NarrationPerformanceCueKind,
    NarrationPerformancePlanSource,
    NarrationPerformanceSuggestionPattern,
    NarrationRhythm,
    Project,
    RationalFps,
    SourceKind,
    TranscriptSegment,
    VoiceGenerationJobPayload,
    VoiceProfile,
)
from app.jobs.handlers import VoiceGenerationJobHandler
from app.jobs.runner import JobRunner
from app.jobs.store import JobStore
from app.main import create_app
from app.narration_performance import (
    NarrationPerformancePlanError,
    build_narration_performance_plan,
    compile_narration_delivery_plan,
    narration_copy_fingerprint,
    narration_performance_plan_fingerprint,
    suggest_narration_performance,
    validate_narration_performance_plan,
)
from app.providers.voice import NarrationPerformanceCoverage, NarrationPerformancePreflight, VoiceSynthesisResult
from app.routing import FeatureSupport
from app.voice_emphasis import automatic_emphasis_plan


COPY = "Start with the point, then prove it."
REVIEWED_COPY = "选题没有判断、文案没有结构。哪些问题要先看，哪些证据要后看。结论要让观众记住。"
TURN_COPY = "先明确问题。但是，别急着给答案。最后留下结论。"


def _cues() -> list[NarrationPerformanceCue]:
    point_start = COPY.index("point")
    prove_start = COPY.index("prove")
    return [
        NarrationPerformanceCue(
            kind=NarrationPerformanceCueKind.EMPHASIS,
            start_char=point_start,
            end_char=point_start + len("point"),
            emphasis=NarrationEmphasis.STRONG,
        ),
        NarrationPerformanceCue(
            kind=NarrationPerformanceCueKind.PAUSE,
            start_char=COPY.index(",") + 1,
            end_char=COPY.index(",") + 1,
            pause=NarrationPause.BEAT,
        ),
        NarrationPerformanceCue(
            kind=NarrationPerformanceCueKind.PACE,
            start_char=prove_start,
            end_char=len(COPY) - 1,
            pace=NarrationPace.MEASURED,
        ),
        NarrationPerformanceCue(
            kind=NarrationPerformanceCueKind.RHYTHM,
            start_char=prove_start,
            end_char=len(COPY) - 1,
            rhythm=NarrationRhythm.LAND,
        ),
    ]


def _plan():
    return build_narration_performance_plan(
        COPY,
        delivery_goal="Land the point before the proof.",
        overall_pace=NarrationPace.CONVERSATIONAL,
        cues=_cues(),
        source=NarrationPerformancePlanSource.USER,
        evidence_refs=["u-voice:delivery-review"],
    )


def _project_and_profile(db: Database) -> tuple[Project, VoiceProfile]:
    ip = IPProfile(creator_name="Creator")
    IPProfileRepository(db).create(ip)
    project = Project(
        ip_profile_id=ip.id,
        title="Narration performance",
        topic="New topic",
        fps=RationalFps(numerator=25, denominator=1),
        created_at=datetime.now(timezone.utc),
    )
    ProjectRepository(db).create(project)
    reference = Path(db.path).parent / "voice-reference.bin"
    reference.write_bytes(b"confirmed voice reference")
    asset = Asset(
        source_kind=SourceKind.USER_ASSET,
        source_file=reference.name,
        content_hash=hashlib.sha256(reference.read_bytes()).hexdigest(),
        duration_ms=1_000,
        width=720,
        height=1280,
        fps=RationalFps(numerator=25, denominator=1),
        has_audio=True,
        authorization_reference="creator-voice-reference",
        imported_at=datetime.now(timezone.utc),
    )
    AssetRepository(db).create(asset)
    clip = Clip(
        asset_id=asset.id,
        start_ms=0,
        end_ms=1_000,
        asset_duration_ms=1_000,
        transcript="Creator voice",
        transcript_segments=[TranscriptSegment(start_ms=0, end_ms=1_000, text="Creator voice")],
        transcript_source="owner-confirmed-reference:test",
        voice_candidate=True,
    )
    ClipRepository(db).create(clip)
    profile = VoiceProfile(
        ip_profile_id=ip.id,
        name="Creator voice",
        provider="test-voice",
        provider_profile_id="creator-v1",
        reference_clip_ids=[clip.id],
        consent=ConsentRecord(subject_name="Creator", basis="self", confirmed=True, confirmed_at=datetime.now(timezone.utc)),
        language="en",
        created_at=datetime.now(timezone.utc),
    )
    VoiceProfileRepository(db).create(profile)
    return project, profile


def test_performance_plan_uses_semantic_cues_and_exact_copy_identity() -> None:
    plan = _plan()

    assert plan.copy_fingerprint == narration_copy_fingerprint(COPY)
    assert validate_narration_performance_plan(plan, COPY) == plan
    with pytest.raises(NarrationPerformancePlanError, match="different copy"):
        validate_narration_performance_plan(plan, "Start with another point, then prove it.")

    out_of_range = NarrationPerformanceCue(
        kind=NarrationPerformanceCueKind.EMPHASIS,
        start_char=99,
        end_char=100,
        emphasis=NarrationEmphasis.CLEAR,
    )
    with pytest.raises(NarrationPerformancePlanError, match="exceeds current copy"):
        build_narration_performance_plan(
            COPY,
            delivery_goal="Keep the point clear.",
            overall_pace=NarrationPace.CONVERSATIONAL,
            cues=[out_of_range],
            source=NarrationPerformancePlanSource.USER,
            evidence_refs=[],
        )
    with pytest.raises(ValidationError, match="matching semantic value"):
        NarrationPerformanceCue(
            kind=NarrationPerformanceCueKind.PAUSE,
            start_char=1,
            end_char=1,
            emphasis=NarrationEmphasis.LIGHT,
        )
    with pytest.raises(ValidationError, match="emphasis cues may not overlap"):
        build_narration_performance_plan(
            COPY,
            delivery_goal="Keep the point clear.",
            overall_pace=NarrationPace.CONVERSATIONAL,
            cues=[
                NarrationPerformanceCue(
                    kind=NarrationPerformanceCueKind.EMPHASIS,
                    start_char=0,
                    end_char=8,
                    emphasis=NarrationEmphasis.CLEAR,
                ),
                NarrationPerformanceCue(
                    kind=NarrationPerformanceCueKind.EMPHASIS,
                    start_char=4,
                    end_char=10,
                    emphasis=NarrationEmphasis.STRONG,
                ),
            ],
            source=NarrationPerformancePlanSource.USER,
            evidence_refs=[],
        )


def test_delivery_compiler_preserves_exact_copy_and_exposes_semantic_boundaries() -> None:
    plan = _plan()
    compiled = compile_narration_delivery_plan(plan, COPY)

    assert compiled.copy_fingerprint == narration_copy_fingerprint(COPY)
    assert compiled.performance_plan_fingerprint == narration_performance_plan_fingerprint(plan)
    assert "".join(segment.text for segment in compiled.segments) == COPY
    point = next(segment for segment in compiled.segments if "point" in segment.text)
    assert point.emphasis is NarrationEmphasis.STRONG
    proof = next(segment for segment in compiled.segments if "prove" in segment.text)
    assert proof.pace is NarrationPace.MEASURED and proof.rhythm is NarrationRhythm.LAND
    assert next(segment for segment in compiled.segments if segment.pause_after is NarrationPause.BEAT).text.endswith(",")
    assert "duration_ms" not in compiled.model_dump(mode="json")


def test_delivery_compiler_keeps_opening_and_closing_pause_as_semantics_not_silence() -> None:
    copy = "Hi"
    plan = build_narration_performance_plan(
        copy,
        delivery_goal="Open and close with space.",
        overall_pace=NarrationPace.MEASURED,
        cues=[
            NarrationPerformanceCue(
                kind=NarrationPerformanceCueKind.PAUSE,
                start_char=0,
                end_char=0,
                pause=NarrationPause.BRIEF,
            ),
            NarrationPerformanceCue(
                kind=NarrationPerformanceCueKind.PAUSE,
                start_char=len(copy),
                end_char=len(copy),
                pause=NarrationPause.LONG,
            ),
        ],
        source=NarrationPerformancePlanSource.USER,
        evidence_refs=[],
    )
    compiled = compile_narration_delivery_plan(plan, copy)

    assert compiled.opening_pause is NarrationPause.BRIEF
    assert compiled.closing_pause is NarrationPause.LONG
    assert [segment.text for segment in compiled.segments] == [copy]
    assert compiled.segments[0].pause_after is None


def test_rhetorical_assistant_suggests_reviewed_patterns_without_acoustic_claims() -> None:
    suggestions = suggest_narration_performance(REVIEWED_COPY)
    repeated = suggest_narration_performance(REVIEWED_COPY)
    plan = suggestions.suggested_plan
    first_boundary = REVIEWED_COPY.index("。") + 1

    assert suggestions.model_dump(mode="json") == repeated.model_dump(mode="json")
    assert plan.source is NarrationPerformancePlanSource.ASSISTED
    assert plan.copy_fingerprint == narration_copy_fingerprint(REVIEWED_COPY)
    assert validate_narration_performance_plan(plan, REVIEWED_COPY) == plan
    strong_concepts = {
        REVIEWED_COPY[cue.start_char:cue.end_char]
        for cue in plan.cues
        if cue.kind is NarrationPerformanceCueKind.EMPHASIS
        and cue.emphasis is NarrationEmphasis.STRONG
    }
    assert {"判断", "结构"} <= strong_concepts
    assert any(
        cue.kind is NarrationPerformanceCueKind.PAUSE
        and cue.pause is NarrationPause.BEAT
        and cue.start_char == cue.end_char == first_boundary
        for cue in plan.cues
    )
    enumeration = next(
        cue for cue in plan.cues
        if cue.kind is NarrationPerformanceCueKind.PACE
        and cue.pace is NarrationPace.DRIVEN
    )
    assert REVIEWED_COPY[enumeration.start_char:enumeration.end_char].startswith("哪些")
    assert {
        NarrationPerformanceSuggestionPattern.PARALLEL_CLAIM,
        NarrationPerformanceSuggestionPattern.ENUMERATION,
        NarrationPerformanceSuggestionPattern.CLAIM_BOUNDARY,
    } <= {entry.pattern for entry in suggestions.suggestions}
    assert {"speed", "duration_ms", "provider"}.isdisjoint(plan.model_dump(mode="json"))


def test_rhetorical_assistant_marks_a_turn_as_a_brief_review_boundary() -> None:
    suggestions = suggest_narration_performance(TURN_COPY)
    turn_end = TURN_COPY.index("。", TURN_COPY.index("但是")) + 1

    turn_pause = next(
        entry for entry in suggestions.suggestions
        if entry.cue.kind is NarrationPerformanceCueKind.PAUSE
        and entry.cue.start_char == entry.cue.end_char == turn_end
    )
    assert turn_pause.cue.pause is NarrationPause.BRIEF
    assert turn_pause.pattern is NarrationPerformanceSuggestionPattern.TURN
    assert "修辞转向" in turn_pause.rationale
    assert {"duration_ms", "f0", "provider"}.isdisjoint(turn_pause.cue.model_dump(mode="json"))


def test_rhetorical_assistant_api_is_ephemeral_and_makes_no_provider_call(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "performance-suggestions-api.sqlite")) as client:
        project = client.post("/projects", json={"title": "Performance", "topic": "New topic"}).json()
        project_id = project["id"]
        assert client.put(
            f"/projects/{project_id}/draft",
            json={"script": REVIEWED_COPY, "topic": "New topic"},
        ).status_code == 200

        first = client.get(f"/projects/{project_id}/narration-performance-suggestions")
        second = client.get(f"/projects/{project_id}/narration-performance-suggestions")

        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert first.json() == second.json()
        assert first.json()["suggested_plan"]["source"] == "assisted"
        assert client.get(f"/projects/{project_id}/draft").json()["narration_performance_plan"] is None
        assert client.get(f"/projects/{project_id}/provider-calls").json() == []


def test_performance_plan_api_versions_cues_and_clears_them_on_copy_edit(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "performance-api.sqlite")) as client:
        project = client.post("/projects", json={"title": "Performance", "topic": "New topic"}).json()
        project_id = project["id"]
        initial = client.put(f"/projects/{project_id}/draft", json={"script": COPY, "topic": "New topic"})
        assert initial.status_code == 200, initial.text

        payload = {
            "delivery_goal": "Land the point before the proof.",
            "overall_pace": "conversational",
            "source": "user",
            "evidence_refs": ["u-voice:delivery-review"],
            "cues": [cue.model_dump(mode="json") for cue in _cues()],
        }
        saved = client.put(f"/projects/{project_id}/narration-performance-plan", json=payload)
        assert saved.status_code == 200, saved.text
        assert saved.json()["copy_fingerprint"] == narration_copy_fingerprint(COPY)
        assert client.get(f"/projects/{project_id}/draft").json()["narration_performance_plan"]["delivery_goal"] == payload["delivery_goal"]
        delivery = client.get(f"/projects/{project_id}/narration-performance-plan/delivery-plan")
        assert delivery.status_code == 200, delivery.text
        assert "".join(item["text"] for item in delivery.json()["segments"]) == COPY
        assert delivery.json()["performance_plan_fingerprint"] != delivery.json()["copy_fingerprint"]
        assert client.get(f"/projects/{project_id}/provider-calls").json() == []

        # Raw provider knobs are not accepted as editorial cues or plan input.
        assert client.put(
            f"/projects/{project_id}/narration-performance-plan",
            json={**payload, "speed": 0.75},
        ).status_code == 422
        bad_anchor = client.put(
            f"/projects/{project_id}/narration-performance-plan",
            json={**payload, "cues": [{
                "kind": "emphasis", "start_char": 999, "end_char": 1000, "emphasis": "strong",
            }]},
        )
        assert bad_anchor.status_code == 422 and "exceeds current copy" in bad_anchor.text

        changed = client.put(f"/projects/{project_id}/draft", json={"script": "A new script needs new delivery direction.", "topic": "New topic"})
        assert changed.status_code == 200, changed.text
        assert changed.json()["narration_performance_plan"] is None
        assert client.get(f"/projects/{project_id}/narration-performance-plan").status_code == 404
        revisions = client.get(f"/projects/{project_id}/draft/revisions").json()
        assert revisions[-2]["narration_performance_plan"]["copy_fingerprint"] == narration_copy_fingerprint(COPY)


def test_performance_plan_api_can_be_explicitly_cleared(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "performance-clear.sqlite")) as client:
        project = client.post("/projects", json={"title": "Performance", "topic": "New topic"}).json()
        project_id = project["id"]
        assert client.put(f"/projects/{project_id}/draft", json={"script": COPY, "topic": "New topic"}).status_code == 200
        saved = client.put(f"/projects/{project_id}/narration-performance-plan", json={
            "delivery_goal": "Land the point.",
            "cues": [cue.model_dump(mode="json") for cue in _cues()],
        })
        assert saved.status_code == 200
        assert client.delete(f"/projects/{project_id}/narration-performance-plan").status_code == 204
        assert client.get(f"/projects/{project_id}/draft").json()["narration_performance_plan"] is None
        assert client.delete(f"/projects/{project_id}/narration-performance-plan").status_code == 204


def test_voice_job_snapshots_only_the_matching_current_draft_plan(tmp_path: Path) -> None:
    path = tmp_path / "performance-voice-api.sqlite"
    db = Database(path)
    project, profile = _project_and_profile(db)
    db.close()
    with TestClient(create_app(path)) as client:
        assert client.put(f"/projects/{project.id}/draft", json={"script": COPY, "topic": project.topic}).status_code == 200
        assert client.put(f"/projects/{project.id}/narration-performance-plan", json={
            "delivery_goal": "Land the point.",
            "cues": [cue.model_dump(mode="json") for cue in _cues()],
        }).status_code == 200
        request = {
            "idempotency_key": "voice-with-performance-plan",
            "voice_profile_id": str(profile.id),
            "text": COPY,
            "language": "en",
            "use_draft_performance_plan": True,
        }
        queued = client.post(f"/projects/{project.id}/voice-jobs", json=request)
        assert queued.status_code == 201, queued.text
        assert client.post(f"/projects/{project.id}/voice-jobs", json={**request, "idempotency_key": "wrong-copy", "text": "Different copy"}).status_code == 422

    reopened = Database(path)
    try:
        job = JobRepository(reopened).get(queued.json()["id"])
        assert job is not None and isinstance(job.payload, VoiceGenerationJobPayload)
        assert job.payload.narration_performance_plan is not None
        assert job.payload.narration_performance_plan.copy_fingerprint == narration_copy_fingerprint(COPY)
    finally:
        reopened.close()


def test_voice_handler_refuses_unapplied_plan_and_records_an_explicit_application_receipt(tmp_path: Path) -> None:
    db = Database(tmp_path / "performance-handler.sqlite")
    try:
        project, profile = _project_and_profile(db)
        plan = _plan()
        now = datetime.now(timezone.utc)
        unsupported_payload = VoiceGenerationJobPayload(
            project_id=project.id,
            voice_profile_id=profile.id,
            text=COPY,
            authorization_reference="voice-consent-1",
            language="en",
            narration_performance_plan=plan,
        )
        unsupported_job = Job(
            project_id=project.id,
            type=JobType.GENERATE_VOICE,
            idempotency_key="performance-unsupported",
            created_at=now,
            updated_at=now,
            payload=unsupported_payload,
        )
        JobStore(db).enqueue(unsupported_job)

        class UnsupportedProvider:
            provider_name = "test-voice"
            model = "test-model"
            is_local = True

            def synthesize(self, *_: object, **__: object) -> VoiceSynthesisResult:
                raise AssertionError("an unsupported provider must not receive semantic delivery intent")

        class Importer:
            def import_path(self, *_: object, **__: object) -> AudioAsset:
                raise AssertionError("unsupported plan must not import an audio asset")

        unsupported_handler = VoiceGenerationJobHandler(
            VoiceProfileRepository(db), AudioAssetRepository(db), Importer(), UnsupportedProvider(), tmp_path / "generated", ProviderCallLedger(db)  # type: ignore[arg-type]
        )
        failed = JobRunner(
            JobStore(db), {JobType.GENERATE_VOICE: unsupported_handler}, worker_id="performance-worker", lease_duration=timedelta(minutes=1), max_attempts=1,
        ).run_once()
        assert failed is not None and failed.status is JobStatus.FAILED
        assert failed.error_code == "voice_performance_intent_unavailable"
        assert ProviderCallRepository(db).list_for_project(project.id) == []

        supported_payload = unsupported_payload.model_copy(update={"text": COPY})
        supported_job = Job(
            project_id=project.id,
            type=JobType.GENERATE_VOICE,
            idempotency_key="performance-supported",
            created_at=now,
            updated_at=now,
            payload=supported_payload,
        )
        JobStore(db).enqueue(supported_job)

        class SupportedProvider:
            provider_name = "test-voice"
            model = "test-model"
            is_local = True
            performance_intent_support = FeatureSupport.AVAILABLE

            def synthesize(self, *_: object, **__: object) -> VoiceSynthesisResult:
                raise AssertionError("a performance-aware job must use the explicit extension")

            def synthesize_with_performance(self, received: VoiceProfile, text: str, output_path: Path, *, language: str | None, performance_plan) -> VoiceSynthesisResult:
                assert received.id == profile.id and text == COPY and language == "en"
                assert performance_plan == plan
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(b"performance-aware-audio")
                return VoiceSynthesisResult(output_path, provider_version="test-version", performance_intent_applied=True)

        class SupportedImporter:
            def import_path(self, source: Path, authorization_reference: str, *, language: str | None = None, generated_job=None) -> AudioAsset:
                assert source.is_file() and authorization_reference == "voice-consent-1" and language == "en"
                value = AudioAsset(
                    source_file=str(source), content_hash="p" * 64, duration_ms=1_200, sample_rate=24_000, channels=1,
                    language=language, authorization_reference=authorization_reference, imported_at=datetime.now(timezone.utc),
                )
                AudioAssetRepository(db).create(value)
                return value

        supported_handler = VoiceGenerationJobHandler(
            VoiceProfileRepository(db), AudioAssetRepository(db), SupportedImporter(), SupportedProvider(), tmp_path / "generated", ProviderCallLedger(db)  # type: ignore[arg-type]
        )
        completed = JobRunner(
            JobStore(db), {JobType.GENERATE_VOICE: supported_handler}, worker_id="performance-worker", lease_duration=timedelta(minutes=1), max_attempts=1,
        ).run_once()
        assert completed is not None and completed.status is JobStatus.COMPLETED
        audio = AudioAssetRepository(db).list()[0]
        receipt = audio.metadata["voice_generation"]["narration_performance"]
        assert receipt["application_state"] == "adapter_applied_pending_quality_review"
        assert receipt["adapter_support"] == "available"
        assert receipt["plan"]["copy_fingerprint"] == narration_copy_fingerprint(COPY)
    finally:
        db.close()


@pytest.mark.parametrize(
    ("coverage", "reason", "expected_error"),
    [
        (NarrationPerformanceCoverage.PARTIAL, "pause_control_only", "voice_performance_intent_partial"),
        (NarrationPerformanceCoverage.UNSUPPORTED, "semantic_cues_unmapped", "voice_performance_intent_unavailable"),
    ],
)
def test_voice_handler_rejects_nonfull_performance_preflight_before_reserving_or_synthesizing(
    tmp_path: Path,
    coverage: NarrationPerformanceCoverage,
    reason: str,
    expected_error: str,
) -> None:
    db = Database(tmp_path / "partial-performance-preflight.sqlite")
    try:
        project, profile = _project_and_profile(db)
        now = datetime.now(timezone.utc)
        job = Job(
            project_id=project.id,
            type=JobType.GENERATE_VOICE,
            idempotency_key="performance-partial-preflight",
            created_at=now,
            updated_at=now,
            payload=VoiceGenerationJobPayload(
                project_id=project.id,
                voice_profile_id=profile.id,
                text=COPY,
                authorization_reference="voice-consent-1",
                language="en",
                narration_performance_plan=_plan(),
            ),
        )
        JobStore(db).enqueue(job)

        class PartialProvider:
            provider_name = "test-voice"
            model = "partial-model"
            is_local = True
            performance_intent_support = FeatureSupport.AVAILABLE

            def synthesize(self, *_: object, **__: object) -> VoiceSynthesisResult:
                raise AssertionError("a planned job must use the performance extension")

            def synthesize_with_performance(self, *_: object, **__: object) -> VoiceSynthesisResult:
                raise AssertionError("a partial preflight must reject before provider synthesis")

            def preflight_narration_performance(self, received: VoiceProfile, text: str, *, language: str | None, performance_plan) -> NarrationPerformancePreflight:
                assert received.id == profile.id and text == COPY and language == "en"
                assert performance_plan.copy_fingerprint == narration_copy_fingerprint(COPY)
                return NarrationPerformancePreflight(
                    coverage=coverage,
                    reasons=(reason,),
                )

        class Importer:
            def import_path(self, *_: object, **__: object) -> AudioAsset:
                raise AssertionError("a partial preflight must not import an audio asset")

        handler = VoiceGenerationJobHandler(
            VoiceProfileRepository(db), AudioAssetRepository(db), Importer(), PartialProvider(), tmp_path / "generated", ProviderCallLedger(db)  # type: ignore[arg-type]
        )
        failed = JobRunner(
            JobStore(db), {JobType.GENERATE_VOICE: handler}, worker_id="performance-worker", lease_duration=timedelta(minutes=1), max_attempts=1,
        ).run_once()
        assert failed is not None and failed.status is JobStatus.FAILED
        assert failed.error_code == expected_error
        assert failed.error_message is not None and reason in failed.error_message
        assert ProviderCallRepository(db).list_for_project(project.id) == []
    finally:
        db.close()


def test_voice_handler_executes_emphasis_only_plan_through_exact_provider_segments(tmp_path: Path) -> None:
    db = Database(tmp_path / "emphasis-handler.sqlite")
    try:
        project, profile = _project_and_profile(db)
        copy = "结论先行。边界防止误用。最后，镜头要按真实时长来拆。"
        plan = automatic_emphasis_plan(copy)
        assert plan is not None and len(plan.cues) == 1
        now = datetime.now(timezone.utc)
        job = Job(
            project_id=project.id,
            type=JobType.GENERATE_VOICE,
            idempotency_key="emphasis-supported",
            created_at=now,
            updated_at=now,
            payload=VoiceGenerationJobPayload(
                project_id=project.id,
                voice_profile_id=profile.id,
                text=copy,
                authorization_reference="voice-consent-1",
                language="zh",
                narration_performance_plan=plan,
            ),
        )
        JobStore(db).enqueue(job)

        class Provider:
            provider_name = "test-voice"
            model = "test-model"
            is_local = True

            def synthesize(self, received: VoiceProfile, text: str, output_path: Path, *, language: str | None) -> VoiceSynthesisResult:
                assert received.id == profile.id and language == "zh"
                spans = []
                cursor = 0
                for part in text.split("\n\n"):
                    start = text.index(part, cursor)
                    cursor = start + len(part)
                    left = start + len(part) - len(part.lstrip())
                    right = cursor - (len(part) - len(part.rstrip()))
                    spans.append((left, right))
                samples = array("h")
                segments = []
                for index, (start, end) in enumerate(spans):
                    amplitude = 4_000 + index * 2_000
                    first_sample = index * 2_400
                    for sample_index in range(2_400):
                        samples.append(round(
                            amplitude * math.sin(2 * math.pi * 220 * sample_index / 24_000)
                        ))
                    segments.append({
                        "start_char": start,
                        "end_char": end,
                        "start_sample": first_sample,
                        "end_sample": first_sample + 2_400,
                    })
                output_path.parent.mkdir(parents=True, exist_ok=True)
                with wave.open(str(output_path), "wb") as audio:
                    audio.setnchannels(1)
                    audio.setsampwidth(2)
                    audio.setframerate(24_000)
                    audio.writeframes(samples.tobytes())
                return VoiceSynthesisResult(
                    output_path,
                    provider_version="test-version",
                    generation_segments=tuple(segments),
                )

        class Importer:
            def import_path(
                self, source: Path, authorization_reference: str, *,
                language: str | None = None, generated_job=None,
            ) -> AudioAsset:
                assert source.is_file() and authorization_reference == "voice-consent-1"
                value = AudioAsset(
                    source_file=str(source),
                    content_hash="a" * 64,
                    duration_ms=300,
                    sample_rate=24_000,
                    channels=1,
                    language=language,
                    authorization_reference=authorization_reference,
                    imported_at=datetime.now(timezone.utc),
                )
                AudioAssetRepository(db).create(value)
                return value

        handler = VoiceGenerationJobHandler(
            VoiceProfileRepository(db),
            AudioAssetRepository(db),
            Importer(),
            Provider(),
            tmp_path / "generated",
            ProviderCallLedger(db),
        )
        completed = JobRunner(
            JobStore(db),
            {JobType.GENERATE_VOICE: handler},
            worker_id="emphasis-worker",
            lease_duration=timedelta(minutes=1),
            max_attempts=1,
        ).run_once()

        assert completed is not None and completed.status is JobStatus.COMPLETED
        audio = AudioAssetRepository(db).list()[0]
        generation = audio.metadata["voice_generation"]
        assert generation["narration_performance"]["application_state"] == (
            "core_emphasis_applied_pending_quality_review"
        )
        assert generation["focus_delivery"]["applied"][0]["copy_text"] == "最后，"
        assert generation["focus_emphasis"]["state"] == "applied"
        assert generation["focus_emphasis"]["applications"][0]["applied_gain_db"] > 1.0
    finally:
        db.close()
