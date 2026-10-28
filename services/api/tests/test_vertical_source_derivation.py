from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from decimal import Decimal
import os
from pathlib import Path
import shutil
import subprocess
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.assembly import VideoSpecAssembler
from app.db import AssetRepository, ClipRepository, Database, IPProfileRepository, ProjectRepository
from app.domain.models import (
    Asset,
    CandidateAsset,
    Clip,
    CostCategory,
    IPProfile,
    Project,
    RationalFps,
    ScenePlan,
    SourceCropWindow,
    SourceKind,
    UsageCost,
    VerifiedVerticalDerivationEvidence,
    VerifiedVerticalDerivationRequest,
    VisualIntent,
)
from app.main import create_app
from app.media import FFProbeAdapter, MediaImporter, ProbeMetadata, VerticalDerivationConflict, VerticalDerivationProcessError, VerticalDerivationValidationError, VerticalSourceDerivationService
from app.media.crop_assessment import CropAssessment, CropAssessmentRepository, CropProposal, FaceBoxObservation
from app.runtime import resolve_local_executable


class _OutputProbe:
    def __init__(self, height: int = 1920) -> None:
        self.height = height

    def probe(self, path: Path) -> ProbeMetadata:
        return ProbeMetadata(
            duration_ms=15_000,
            width=1080,
            height=self.height,
            fps=RationalFps(numerator=30, denominator=1),
            has_audio=False,
            metadata={"format": {"format_name": "mp4"}, "streams": [{"codec_type": "video"}]},
        )


class _WritingRunner:
    def __init__(self) -> None:
        self.calls = 0
        self.argv: list[str] | None = None

    def __call__(self, argv: list[str]) -> None:
        self.calls += 1
        self.argv = argv
        Path(argv[-1]).write_bytes(b"vertical derivative bytes")


def _source(
    db: Database,
    root: Path,
    *,
    source_kind: SourceKind = SourceKind.USER_ASSET,
    metadata: dict | None = None,
) -> tuple[Asset, Clip]:
    source_path = root / "original horizontal.mp4"
    source_path.write_bytes(b"original horizontal source")
    asset = Asset(
        source_kind=source_kind, source_file=str(source_path), content_hash=hashlib.sha256(source_path.read_bytes()).hexdigest(), duration_ms=60_000,
        width=1920, height=1080, fps=RationalFps(numerator=30, denominator=1),
        authorization_reference="creator-source-rights", imported_at=datetime.now(timezone.utc), metadata=metadata or {},
    )
    clip = Clip(asset_id=asset.id, start_ms=0, end_ms=60_000, asset_duration_ms=asset.duration_ms)
    with db.transaction():
        AssetRepository(db).create(asset)
        ClipRepository(db).create(clip)
    return asset, clip


def _request(asset: Asset, clip: Clip, *, start_ms: int = 10_000, end_ms: int = 25_000) -> VerifiedVerticalDerivationRequest:
    return VerifiedVerticalDerivationRequest(
        idempotency_key="vertical-source-15s-v1",
        source_asset_id=asset.id,
        source_clip_id=clip.id,
        start_ms=start_ms,
        end_ms=end_ms,
        crop_window=SourceCropWindow(x=600, y=0, width=594, height=1056),
        source_subtitle_state="known_burned_in",
        known_subtitle_regions=[SourceCropWindow(x=0, y=1056, width=1920, height=24)],
        evidence=VerifiedVerticalDerivationEvidence(
            source_asset_id=asset.id, source_clip_id=clip.id,
            source_content_hash=asset.content_hash, crop_window=SourceCropWindow(x=600, y=0, width=594, height=1056),
            covered_start_ms=start_ms, covered_end_ms=end_ms,
            method="human_full_interval_review", method_version="v1", coverage="full_interval_continuous", confidence="reviewed",
            face_head_clearance_verified=True, crop_stability_verified=True,
            source_subtitle_clearance_verified=True, evidence_reference="review://source/10s-25s/v1",
        ),
    )


def _service(db: Database, root: Path, runner: _WritingRunner) -> VerticalSourceDerivationService:
    return VerticalSourceDerivationService(
        db, root, MediaImporter(db, root, _OutputProbe()), command="fake-ffmpeg", runner=runner,
    )


def _portrait_request(asset: Asset, clip: Clip, height: int = 1440) -> VerifiedVerticalDerivationRequest:
    crop = SourceCropWindow(x=600, y=0, width=720, height=960) if height == 1440 else SourceCropWindow(x=600, y=0, width=630, height=980)
    base = _request(asset, clip)
    return VerifiedVerticalDerivationRequest.model_validate({**base.model_dump(), "output_height": height,
        "crop_window": crop, "evidence": base.evidence.model_copy(update={"crop_window": crop})})


@pytest.mark.parametrize("failure", [None, "output_bytes", "source_bytes", "missing_review", "negative_face", "subtitles"])
def test_talking_binding_reopens_exact_review_without_reclassifying_source(tmp_path: Path, failure: str | None) -> None:
    from app.domain.models import TalkingPerformanceBrief, TalkingReferenceAssessment
    from app.production_runs import _select_bound_talking_reference

    with Database(tmp_path / "talking-source.sqlite") as db:
        asset, clip = _source(db, tmp_path)
        runner = _WritingRunner()
        result = _service(db, tmp_path, runner).derive(_request(asset, clip))
        reference = result.clip.model_copy(update={"talking_reference_assessment": TalkingReferenceAssessment(
            burned_in_subtitles=failure == "subtitles", evidence_reference="review://source/10s-25s/v1",
        ), "face_visibility": 0.2 if failure == "negative_face" else None})
        with db.transaction():
            ClipRepository(db).update(reference)
        derived = result.asset
        if failure == "output_bytes":
            Path(derived.source_file).write_bytes(b"changed output")
        elif failure == "source_bytes":
            Path(asset.source_file).write_bytes(b"changed source")
        elif failure == "missing_review":
            provenance = dict(derived.metadata["source_derivation"])
            provenance.pop("evidence")
            derived = derived.model_copy(update={"metadata": {"source_derivation": provenance}})
        fit = _select_bound_talking_reference(db, tmp_path, derived, reference, TalkingPerformanceBrief())
        assert (fit.selected_clip_id == reference.id) is (failure is None)
        persisted = ClipRepository(db).get(reference.id)
        assert persisted.talking_candidate is False
        assert persisted.mouth_visibility is None
        assert runner.calls == 1


def test_shorter_portrait_viewport_retains_provenance_replay_and_guards(tmp_path: Path) -> None:
    with Database(tmp_path / "portrait.sqlite") as db:
        asset, clip = _source(db, tmp_path)
        runner = _WritingRunner()
        service = VerticalSourceDerivationService(db, tmp_path / "data", MediaImporter(db, tmp_path / "data", _OutputProbe(1440)), command="fake-ffmpeg", runner=runner)
        request = _portrait_request(asset, clip)
        result = service.derive(request)
        assert (result.asset.width, result.asset.height) == (1080, 1440)
        assert runner.argv[runner.argv.index("-vf") + 1].startswith("crop=720:960:600:0,scale=1080:1440:flags=lanczos")
        provenance = result.asset.metadata["source_derivation"]
        assert provenance["kind"] == "verified_portrait_crop_v1"
        assert provenance["presentation_output"] == {"width": 1080, "height": 1440}
        assert provenance["source_content_hash"] == asset.content_hash
        assert provenance["evidence"] == request.evidence.model_dump(mode="json")
        profile = IPProfile(creator_name="Creator")
        project = Project(ip_profile_id=profile.id, title="Flexible source", topic="Aspect", fps=RationalFps(numerator=30, denominator=1), created_at=datetime.now(timezone.utc))
        scene = ScenePlan(project_id=project.id, scene_id="flexible-source", order=0, purpose="point", voice_text="A supported source interval.", duration_target_ms=900,
            visual_intent=VisualIntent(subject="creator"), preferred_sources=[SourceKind.USER_ASSET], fallback_sources=[SourceKind.TYPOGRAPHY])
        candidate = CandidateAsset(scene_plan_id=scene.id, source_kind=SourceKind.USER_ASSET,
            asset_id=result.asset.id, clip_id=result.clip.id, match_score=1.0, why=["evidenced viewport"], recommended=True,
            estimated_cost=UsageCost(category=CostCategory.USER_ASSET, amount=Decimal("0"), currency="USD"))
        with db.transaction():
            IPProfileRepository(db).create(profile)
            ProjectRepository(db).create(project)
        spec = VideoSpecAssembler(AssetRepository(db), ClipRepository(db)).assemble(project, [scene], {scene.id: candidate})
        assert (spec.width, spec.height) == (1080, 1920)
        assert spec.scenes[0].visual.asset_id == result.asset.id
        assert service.derive(request).asset.id == result.asset.id
        with pytest.raises(VerticalDerivationConflict):
            service.derive(_portrait_request(asset, clip, 1680))
        assert runner.calls == 1
        # Geometry is not authority: retain full-interval and subtitle checks.
        with pytest.raises(VerticalDerivationValidationError, match="full-interval"):
            service.derive(request.model_copy(update={"evidence": request.evidence.model_copy(update={"coverage": None})}))
        with pytest.raises(VerticalDerivationValidationError, match="burned-in"):
            service.derive(request.model_copy(update={"known_subtitle_regions": [SourceCropWindow(x=600, y=900, width=720, height=100)]}))
        with pytest.raises(VerticalDerivationValidationError, match="aspect ratio"):
            service.derive(request.model_copy(update={"output_height": 1920}))
        assert runner.calls == 1


def test_shorter_portrait_rejects_incorrect_actual_output_dimensions(tmp_path: Path) -> None:
    with Database(tmp_path / "wrong-size.sqlite") as db:
        asset, clip = _source(db, tmp_path)
        runner = _WritingRunner()
        with pytest.raises(VerticalDerivationProcessError, match="dimensions"):
            _service(db, tmp_path / "data", runner).derive(_portrait_request(asset, clip))
        assert len(AssetRepository(db).list()) == 1


@pytest.mark.parametrize("height", [1078, 1922, 1441, True, "1440"])
def test_portrait_viewport_rejects_out_of_range_odd_or_noninteger_dimensions(tmp_path: Path, height) -> None:
    with Database(tmp_path / "dimensions.sqlite") as db:
        asset, clip = _source(db, tmp_path)
        value = _request(asset, clip).model_dump()
        value["output_height"] = height
        with pytest.raises(ValueError) as caught:
            VerifiedVerticalDerivationRequest.model_validate(value)
        assert all(error["loc"] == ("output_height",) for error in caught.value.errors())


def test_default_portrait_identity_retains_legacy_provenance(tmp_path: Path) -> None:
    with Database(tmp_path / "legacy.sqlite") as db:
        asset, clip = _source(db, tmp_path)
        request = _request(asset, clip)
        identity = VerticalSourceDerivationService._request_identity(request)
        assert request.output_height == 1920
        assert set(identity) == {"idempotency_key", "source_asset_id", "source_clip_id", "source_interval",
            "crop_window", "evidence", "source_subtitle_state", "known_subtitle_regions"}
        # Old clients omitting the optional field retain identical identity.
        value = request.model_dump()
        del value["output_height"]
        assert VerticalSourceDerivationService._request_identity(VerifiedVerticalDerivationRequest.model_validate(value)) == identity


def test_known_bad_exact_crop_blocks_derivation_before_ffmpeg(tmp_path: Path) -> None:
    with Database(tmp_path / "negative.sqlite") as db:
        source, clip = _source(db, tmp_path)
        request = _request(source, clip)
        CropAssessmentRepository(db).create(CropAssessment(
            proposal=CropProposal(
                source_asset_id=source.id, source_clip_id=clip.id,
                start_ms=request.start_ms, end_ms=request.end_ms, crop_window=request.crop_window,
            ),
            source_content_hash=source.content_hash, evidence_class="assisted_test", method="face_box_observation",
            method_version="fixture-v1", coverage="sampled_frames",
            observations=(FaceBoxObservation(
                time_ms=12_000, face_box=SourceCropWindow(x=590, y=200, width=180, height=180), confidence=0.9,
            ),), evidence_reference="fixture:known-boundary-crossing",
        ))
        runner = _WritingRunner()
        with pytest.raises(VerticalDerivationValidationError, match="face-boundary failure"):
            _service(db, tmp_path / "data", runner).derive(request)
        assert runner.calls == 0


def test_verified_vertical_derivation_persists_provenance_and_is_consumable(tmp_path: Path) -> None:
    db = Database(tmp_path / "store.sqlite")
    try:
        source, clip = _source(db, tmp_path)
        runner = _WritingRunner()
        result = _service(db, tmp_path / "data", runner).derive(_request(source, clip))

        assert runner.calls == 1
        assert (result.asset.width, result.asset.height, result.clip.start_ms, result.clip.end_ms) == (1080, 1920, 0, 15_000)
        provenance = result.asset.metadata["source_derivation"]
        assert provenance["source_content_hash"] == source.content_hash
        assert provenance["source_interval"] == {"start_ms": 10_000, "end_ms": 25_000}
        assert provenance["known_subtitle_regions"][0]["y"] == 1056
        assert any("crop=594:1056:600:0" in item for item in runner.argv)

        profile = IPProfile(creator_name="Creator")
        project = Project(ip_profile_id=profile.id, title="Vertical source", topic="Crop", fps=RationalFps(numerator=30, denominator=1), created_at=datetime.now(timezone.utc))
        scene = ScenePlan(
            project_id=project.id, scene_id="proof", order=0, purpose="point", voice_text="A short verified interval.",
            duration_target_ms=900, visual_intent=VisualIntent(subject="creator"),
            preferred_sources=[SourceKind.USER_ASSET], fallback_sources=[SourceKind.TYPOGRAPHY],
        )
        candidate = CandidateAsset(
            scene_plan_id=scene.id, source_kind=SourceKind.USER_ASSET, asset_id=result.asset.id, clip_id=result.clip.id,
            match_score=1.0, why=["verified vertical derivative"], recommended=True,
            estimated_cost=UsageCost(category=CostCategory.USER_ASSET, amount=Decimal("0"), currency="USD"),
        )
        with db.transaction():
            IPProfileRepository(db).create(profile)
            ProjectRepository(db).create(project)
        spec = VideoSpecAssembler(AssetRepository(db), ClipRepository(db)).assemble(project, [scene], {scene.id: candidate})
        assert spec.scenes[0].visual.asset_id == result.asset.id

        replay = _service(db, tmp_path / "data", runner).derive(_request(source, clip))
        assert replay.asset.id == result.asset.id
        assert replay.clip.id == result.clip.id
        assert runner.calls == 1
    finally:
        db.close()


@pytest.mark.parametrize("offset_ms", [0, 40])
def test_real_derivation_aligns_leading_frame_but_never_pads_missing_end(tmp_path: Path, offset_ms: int) -> None:
    if os.environ.get("CONTENT_OS_REAL_MEDIA_TESTS") != "1":
        pytest.skip("requires explicitly configured local FFmpeg")
    ffmpeg = resolve_local_executable("ffmpeg")
    ffprobe = resolve_local_executable("ffprobe")
    source_path = tmp_path / "offset-source.mp4"
    subprocess.run([ffmpeg, "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=1",
        "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono:d=1.04", "-filter_complex", f"[0:v]setpts=PTS+{offset_ms / 1000}/TB[v]",
        "-map", "[v]", "-map", "1:a", "-fps_mode", "passthrough", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(source_path)],
        check=True, capture_output=True, timeout=30)
    probe = FFProbeAdapter(ffprobe)
    assert probe.probe(source_path).duration_ms == 1040
    with Database(tmp_path / "offset.sqlite") as db:
        asset = Asset(source_file=str(source_path), content_hash=hashlib.sha256(source_path.read_bytes()).hexdigest(),
            duration_ms=1040, width=320, height=180, fps=RationalFps(numerator=25, denominator=1),
            authorization_reference="synthetic-offset-fixture", imported_at=datetime.now(timezone.utc))
        clip = Clip(asset_id=asset.id, start_ms=0, end_ms=1040, asset_duration_ms=1040)
        AssetRepository(db).create(asset); ClipRepository(db).create(clip)
        crop = SourceCropWindow(x=60, y=0, width=180, height=180)
        request = _request(asset, clip, start_ms=0, end_ms=1040)
        request = VerifiedVerticalDerivationRequest.model_validate(request.model_copy(update={"output_height":1080,
            "crop_window":crop, "evidence":request.evidence.model_copy(update={"crop_window":crop})}).model_dump())
        service = VerticalSourceDerivationService(db, tmp_path / "data", MediaImporter(db, tmp_path / "data", probe), command=ffmpeg)
        if offset_ms == 0:
            with pytest.raises(VerticalDerivationProcessError, match="shorter than"):
                service.derive(request)
            assert len(AssetRepository(db).list()) == 1
        else:
            result = service.derive(request)
            measured = probe.probe(result.asset.source_file)
            assert measured.duration_ms == 1040 and measured.has_audio is False
            stream = measured.metadata["streams"][0]
            assert stream["start_time"] == "0.000000" and int(stream["nb_frames"]) == 26
            assert result.asset.metadata["source_derivation"]["transform"]["timing"] == "native_fps_first_frame_to_zero"


def test_vertical_derivation_rejects_subtitle_overlap_before_ffmpeg(tmp_path: Path) -> None:
    db = Database(tmp_path / "store.sqlite")
    try:
        source, clip = _source(db, tmp_path)
        runner = _WritingRunner()
        original = _request(source, clip)
        moved = SourceCropWindow(x=600, y=24, width=594, height=1056)
        request = original.model_copy(update={
            "crop_window": moved,
            "evidence": original.evidence.model_copy(update={"crop_window": moved}),
        })
        with pytest.raises(VerticalDerivationValidationError, match="intersects known burned-in subtitles"):
            _service(db, tmp_path / "data", runner).derive(request)
        assert runner.calls == 0
    finally:
        db.close()


def test_vertical_derivation_rejects_changed_source_bytes_even_on_idempotent_replay(tmp_path: Path) -> None:
    db = Database(tmp_path / "store.sqlite")
    try:
        source, clip = _source(db, tmp_path)
        runner = _WritingRunner()
        service = _service(db, tmp_path / "data", runner)
        request = _request(source, clip)
        service.derive(request)
        Path(source.source_file).write_bytes(b"changed original")
        with pytest.raises(VerticalDerivationValidationError, match="source media bytes"):
            service.derive(request)
        assert runner.calls == 1
    finally:
        db.close()


def test_vertical_derivation_rejects_changed_output_bytes_on_idempotent_replay(tmp_path: Path) -> None:
    db = Database(tmp_path / "store.sqlite")
    try:
        source, clip = _source(db, tmp_path)
        runner = _WritingRunner()
        service = _service(db, tmp_path / "data", runner)
        request = _request(source, clip)
        result = service.derive(request)
        Path(result.asset.source_file).write_bytes(b"changed derivative")
        with pytest.raises(VerticalDerivationConflict, match="bytes changed"):
            service.derive(request)
        assert runner.calls == 1
    finally:
        db.close()


def test_vertical_derivation_rejects_new_crop_with_old_evidence(tmp_path: Path) -> None:
    db = Database(tmp_path / "store.sqlite")
    try:
        source, clip = _source(db, tmp_path)
        request = _request(source, clip).model_copy(update={
            "crop_window": SourceCropWindow(x=0, y=0, width=594, height=1056),
        })
        runner = _WritingRunner()
        with pytest.raises(VerticalDerivationValidationError, match="exact crop"):
            _service(db, tmp_path / "data", runner).derive(request)
        assert runner.calls == 0
    finally:
        db.close()


def test_legacy_assertion_deserializes_but_cannot_derive_without_bound_evidence(tmp_path: Path) -> None:
    db = Database(tmp_path / "store.sqlite")
    try:
        source, clip = _source(db, tmp_path)
        payload = _request(source, clip).model_dump(mode="python")
        for key in ("source_content_hash", "crop_window", "method", "method_version", "coverage", "confidence"):
            payload["evidence"].pop(key)
        legacy = VerifiedVerticalDerivationRequest.model_validate(payload)
        runner = _WritingRunner()
        with pytest.raises(VerticalDerivationValidationError, match="source and exact crop"):
            _service(db, tmp_path / "data", runner).derive(legacy)
        assert runner.calls == 0
    finally:
        db.close()


def test_admitted_talking_derivative_retains_its_normal_video_spec_gate(tmp_path: Path, admit_talking_run) -> None:
    db = Database(tmp_path / "store.sqlite")
    try:
        source, clip = _source(
            db, tmp_path, source_kind=SourceKind.AI_VIDEO,
            metadata={"talking_run": {
                "admission_state": "admitted", "automated_qa_state": "verified", "continuity_review_state": "approved",
            }},
        )
        project = Project(
            ip_profile_id=UUID("00000000-0000-0000-0000-000000000003"), title="Talking crop", topic="Crop",
            fps=RationalFps(numerator=30, denominator=1), created_at=datetime.now(timezone.utc),
        )
        source, _, _ = admit_talking_run(
            db, project.id, source, clip, "Approved Talking crop.",
            transcript_start_ms=10_000, transcript_end_ms=25_000,
        )
        result = _service(db, tmp_path / "data", _WritingRunner()).derive(_request(source, clip))
        assert result.asset.metadata["talking_run"] == source.metadata["talking_run"]
        scene = ScenePlan(
            project_id=project.id, scene_id="talking", order=0, purpose="point", voice_text="Approved Talking crop.",
            duration_target_ms=15_000, visual_intent=VisualIntent(subject="creator"),
            preferred_sources=[SourceKind.AI_VIDEO], fallback_sources=[SourceKind.TYPOGRAPHY],
        )
        candidate = CandidateAsset(
            scene_plan_id=scene.id, source_kind=SourceKind.AI_VIDEO, asset_id=result.asset.id, clip_id=result.clip.id,
            match_score=1.0, why=["admitted derived Talking"], recommended=True,
            estimated_cost=UsageCost(category=CostCategory.TALKING, amount=Decimal("0"), currency="USD"),
        )
        spec = VideoSpecAssembler(AssetRepository(db), ClipRepository(db)).assemble(project, [scene], {scene.id: candidate})
        assert spec.scenes[0].visual.asset_id == result.asset.id
    finally:
        db.close()


def test_vertical_derivation_contract_rejects_incomplete_temporal_evidence() -> None:
    asset_id = UUID("00000000-0000-0000-0000-000000000001")
    clip_id = UUID("00000000-0000-0000-0000-000000000002")
    with pytest.raises(ValueError, match="cover the complete requested interval"):
        VerifiedVerticalDerivationRequest(
            idempotency_key="short-evidence", source_asset_id=asset_id, source_clip_id=clip_id,
            start_ms=10_000, end_ms=25_000, crop_window=SourceCropWindow(x=0, y=0, width=594, height=1056),
            source_subtitle_state="not_observed",
            evidence=VerifiedVerticalDerivationEvidence(
                source_asset_id=asset_id, source_clip_id=clip_id, source_content_hash="a" * 64,
                crop_window=SourceCropWindow(x=0, y=0, width=594, height=1056),
                covered_start_ms=10_000, covered_end_ms=24_999,
                method="human_full_interval_review", method_version="v1", coverage="full_interval_continuous", confidence="reviewed",
                face_head_clearance_verified=True, crop_stability_verified=True,
                source_subtitle_clearance_verified=True, evidence_reference="review://too-short",
            ),
        )


@pytest.mark.parametrize("height", [1920, 1440])
def test_vertical_derivation_api_exposes_the_normal_contract(tmp_path: Path, height: int) -> None:
    database_path = tmp_path / "api.sqlite"
    db = Database(database_path)
    source, clip = _source(db, tmp_path)
    db.close()
    runner = _WritingRunner()

    def factory(open_db: Database, root: Path) -> VerticalSourceDerivationService:
        return VerticalSourceDerivationService(open_db, root, MediaImporter(open_db, root, _OutputProbe(height)), command="fake-ffmpeg", runner=runner)

    app = create_app(database_path, vertical_derivation_factory=factory)
    with TestClient(app) as client:
        request = _request(source, clip) if height == 1920 else _portrait_request(source, clip)
        response = client.post("/assets/vertical-derivations", json=request.model_dump(mode="json"))
    assert response.status_code == 201
    body = response.json()
    assert body["asset"]["metadata"]["source_derivation"]["source_clip_id"] == str(clip.id)
    assert body["clip"]["asset_id"] == body["asset"]["id"]
    assert body["asset"]["height"] == height


@pytest.mark.parametrize("height", [1920, 1440])
def test_real_ffmpeg_vertical_derivation_has_a_verified_portrait_output(tmp_path: Path, height: int) -> None:
    if os.environ.get("CONTENT_OS_REAL_MEDIA_TESTS") != "1":
        pytest.skip("set CONTENT_OS_REAL_MEDIA_TESTS=1 to run the 1080x1920 real-media integration test")
    ffmpeg = shutil.which("ffmpeg") or resolve_local_executable("ffmpeg")
    ffprobe = shutil.which("ffprobe") or resolve_local_executable("ffprobe")
    if not Path(ffmpeg).is_file() or not Path(ffprobe).is_file():
        pytest.skip("real ffmpeg/ffprobe binaries are unavailable")
    source_path = tmp_path / "real horizontal.mp4"
    subprocess.run(
        [ffmpeg, "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30", "-frames:v", "30", "-an", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(source_path)],
        check=True, capture_output=True, timeout=30,
    )
    db = Database(tmp_path / "real.sqlite")
    try:
        asset = Asset(
            source_file=str(source_path), content_hash=hashlib.sha256(source_path.read_bytes()).hexdigest(), duration_ms=1_000, width=1920, height=1080,
            fps=RationalFps(numerator=30, denominator=1), authorization_reference="creator-source-rights", imported_at=datetime.now(timezone.utc),
        )
        clip = Clip(asset_id=asset.id, start_ms=0, end_ms=1_000, asset_duration_ms=1_000)
        with db.transaction():
            AssetRepository(db).create(asset)
            ClipRepository(db).create(clip)
        request = _request(asset, clip, start_ms=0, end_ms=500).model_copy(update={"idempotency_key": "real-vertical-source"})
        if height == 1440:
            crop = SourceCropWindow(x=600, y=0, width=720, height=960)
            request = request.model_copy(update={"output_height": height, "crop_window": crop, "evidence": request.evidence.model_copy(update={"crop_window": crop})})
        service = VerticalSourceDerivationService(
            db, tmp_path / "data", MediaImporter(db, tmp_path / "data", FFProbeAdapter(ffprobe)), command=ffmpeg, timeout_seconds=30,
        )
        result = service.derive(request)
        assert (result.asset.width, result.asset.height) == (1080, height)
        assert result.asset.duration_ms == 500
        assert Path(result.asset.source_file).is_file()
    finally:
        db.close()
