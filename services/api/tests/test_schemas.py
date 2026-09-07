from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.domain.models import (
    Asset, CandidateAsset, Clip, ConsentBasis, ConsentRecord, CostCategory, ProjectFormat,
    RationalFps, SourceKind, UsageCost, VideoScene, VideoSpec, VideoVisual,
)

NOW = datetime(2026, 9, 7, tzinfo=timezone.utc)
FPS = RationalFps(numerator=30_000, denominator=1_001)


def test_clip_uses_integer_milliseconds_and_rejects_invalid_intervals() -> None:
    asset_id = uuid4()
    valid = Clip(asset_id=asset_id, start_ms=1_000, end_ms=2_000, asset_duration_ms=3_000)
    assert valid.end_ms - valid.start_ms == 1_000
    for values in (
        {"start_ms": -1, "end_ms": 100, "asset_duration_ms": 1_000},
        {"start_ms": 100, "end_ms": 100, "asset_duration_ms": 1_000},
        {"start_ms": 100, "end_ms": 1_001, "asset_duration_ms": 1_000},
        {"start_ms": 100.5, "end_ms": 200, "asset_duration_ms": 1_000},
        {"start_ms": True, "end_ms": 200, "asset_duration_ms": 1_000},
        {"start_ms": True, "end_ms": 200, "asset_duration_ms": 1_000},
    ):
        with pytest.raises(ValidationError):
            Clip(asset_id=asset_id, **values)


def test_asset_and_clip_duration_mismatch_is_rejected() -> None:
    asset = Asset(source_file="assets/source.mp4", content_hash="a" * 64, duration_ms=2_000,
                  width=1080, height=1920, fps=FPS, authorization_reference="user-owned-media", imported_at=NOW)
    with pytest.raises(ValidationError, match="exceeds asset duration"):
        Clip(asset_id=asset.id, start_ms=1_500, end_ms=2_001, asset_duration_ms=asset.duration_ms)


def test_profile_consent_requires_explicit_confirmation_and_authorization_reference() -> None:
    with pytest.raises(ValidationError, match="explicitly confirmed"):
        ConsentRecord(subject_name="Creator", basis=ConsentBasis.SELF, confirmed=False, confirmed_at=NOW)
    with pytest.raises(ValidationError, match="authorization_reference"):
        ConsentRecord(subject_name="Creator", basis=ConsentBasis.EXPLICIT_AUTHORIZATION,
                      confirmed=True, confirmed_at=NOW)


def test_usage_cost_rejects_nonfinite_decimal_and_serializes_as_string() -> None:
    with pytest.raises(ValidationError, match="finite"):
        UsageCost(category=CostCategory.AI_VIDEO, amount=Decimal("NaN"), currency="USD")
    cost = UsageCost(category=CostCategory.AI_VIDEO, amount=Decimal("1.2500"), currency="USD")
    assert '"amount":"1.2500"' in cost.model_dump_json()


def test_video_spec_uses_integer_frames_with_rational_fps_and_no_overlap() -> None:
    visual = VideoVisual(source_kind=SourceKind.USER_ASSET, authorization_reference="user-owned-media", asset_id=uuid4(), clip_start_ms=0, clip_end_ms=3_000, source_duration_ms=3_000)
    spec = VideoSpec(project_id=uuid4(), format=ProjectFormat.VERTICAL, width=1080, height=1920, fps=FPS,
                     scenes=[VideoScene(scene_id="one", start_frame=0, duration_frames=90, visual=visual),
                             VideoScene(scene_id="two", start_frame=90, duration_frames=30, visual=visual)])
    assert spec.fps.numerator == 30_000
    with pytest.raises(ValidationError, match="must not overlap"):
        VideoSpec(project_id=uuid4(), format=ProjectFormat.VERTICAL, width=1080, height=1920, fps=FPS,
                  scenes=[VideoScene(scene_id="one", start_frame=0, duration_frames=90, visual=visual),
                          VideoScene(scene_id="two", start_frame=89, duration_frames=30, visual=visual)])


def test_candidate_requires_reference_unless_it_is_a_capture() -> None:
    cost = UsageCost(category=CostCategory.CAPTURE, amount=Decimal("0"), currency="USD")
    with pytest.raises(ValidationError, match="existing-media candidates need an asset_id or clip_id"):
        CandidateAsset(scene_plan_id=uuid4(), source_kind=SourceKind.USER_ASSET, match_score=0.8,
                       why=["matches the requested action"], estimated_cost=cost)


def test_candidate_allows_unmaterialized_generation_and_enforces_capture_consistency() -> None:
    unknown = UsageCost(category=CostCategory.AI_VIDEO)
    proposal = CandidateAsset(scene_plan_id=uuid4(), source_kind=SourceKind.AI_VIDEO, match_score=0.5,
                              why=["fallback generation"], estimated_cost=unknown)
    assert proposal.asset_id is None
    with pytest.raises(ValidationError, match="must require_capture"):
        CandidateAsset(scene_plan_id=uuid4(), source_kind=SourceKind.CAPTURE, match_score=0.5,
                       why=["film a new shot"], estimated_cost=UsageCost(category=CostCategory.CAPTURE))


def test_known_cost_requires_currency() -> None:
    with pytest.raises(ValidationError, match="currency is required"):
        UsageCost(category=CostCategory.RENDER, amount=Decimal("1"))


def test_metadata_rejects_non_json_and_nonfinite_values() -> None:
    with pytest.raises(ValidationError, match="non-finite"):
        Asset(source_file="assets/source.mp4", content_hash="a" * 64, duration_ms=2_000, width=1080, height=1920,
              fps=FPS, authorization_reference="user-owned-media", imported_at=NOW, metadata={"score": float("nan")})
    with pytest.raises(ValidationError):
        Asset(source_file="assets/source.mp4", content_hash="a" * 64, duration_ms=2_000, width=1080, height=1920,
              fps=FPS, authorization_reference="user-owned-media", imported_at=NOW, metadata={"bad": object()})


def test_video_spec_rejects_duplicate_scenes_and_short_source_clip() -> None:
    visual = VideoVisual(source_kind=SourceKind.USER_ASSET, authorization_reference="user-owned-media", asset_id=uuid4(),
                         clip_start_ms=0, clip_end_ms=2_000, source_duration_ms=2_000)
    with pytest.raises(ValidationError, match="unique scene_id"):
        VideoSpec(project_id=uuid4(), format=ProjectFormat.VERTICAL, width=1080, height=1920, fps=FPS,
                  scenes=[VideoScene(scene_id="same", start_frame=0, duration_frames=1, visual=visual),
                          VideoScene(scene_id="same", start_frame=1, duration_frames=1, visual=visual)])
    with pytest.raises(ValidationError, match="shorter"):
        VideoSpec(project_id=uuid4(), format=ProjectFormat.VERTICAL, width=1080, height=1920,
                  fps=RationalFps(numerator=30, denominator=1),
                  scenes=[VideoScene(scene_id="one", start_frame=0, duration_frames=62, visual=visual)])
