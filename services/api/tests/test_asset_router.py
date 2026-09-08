from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app.db import AssetRepository, ClipRepository, Database
from app.domain.models import Asset, Clip, ProjectFormat, RationalFps, ScenePlan, SourceKind, VisualIntent
from app.providers.embedding import EmbeddingBatch
from app.routing import AssetRouter, RoutingConfigurationError, RoutingInputError, RoutingWeights
from app.search import ClipEmbeddingIndexer


NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)


def _asset(kind: SourceKind, index: int) -> Asset:
    return Asset(
        source_kind=kind, source_file=f"C:/media/{kind.value}-{index}.mp4", content_hash=(str(index) * 64)[:64],
        duration_ms=16_000, width=1080, height=1920, fps=RationalFps(numerator=30, denominator=1),
        authorization_reference="creator-rights", imported_at=NOW,
    )


def _clip(asset: Asset, index: int, *, quality: float = 0.9, used_count: int = 0, last_used_at: datetime | None = None) -> Clip:
    return Clip(
        asset_id=asset.id, start_ms=index * 4_000, end_ms=(index + 1) * 4_000, asset_duration_ms=asset.duration_ms,
        transcript="本人坐在电脑前操作软件", visual_description="creator at computer desk", action="operating software",
        objects=["computer", "desk"], shot_type="close shot", orientation=ProjectFormat.VERTICAL,
        quality_score=quality, talking_candidate=True, used_count=used_count, last_used_at=last_used_at,
    )


def _scene() -> ScenePlan:
    return ScenePlan(
        project_id=uuid4(), scene_id="scene_01", order=0, purpose="hook", voice_text="本人坐在电脑前操作软件。",
        duration_target_ms=3_000,
        visual_intent=VisualIntent(subject="creator", action="operating software", framing="close", description="computer desk"),
        preferred_sources=[SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET], fallback_sources=[SourceKind.CAPTURE],
        caption_emphasis=["操作软件"],
    )


class _EmbeddingProvider:
    """Deterministic local embedding fake: no network or provider credential."""

    def embed(self, texts):
        if len(texts) == 1:
            assert "本人坐在电脑前操作软件" in texts[0]
            return EmbeddingBatch(((1.0, 0.0),))
        # The source-kind-ineligible AI Clip is semantically perfect too; the
        # router must filter it after local search using its stored Asset.
        vectors = []
        for text in texts:
            if "quality_score: 0.3" in text:
                vectors.append((1.0, 0.0))
            elif "quality_score: 0.9" in text and "used_count" not in text:
                vectors.append((1.0, 0.0))
            else:
                vectors.append((0.96, 0.28))
        return EmbeddingBatch(tuple(vectors))


def _router(tmp_path: Path) -> tuple[Database, AssetRouter, list[Clip], Asset]:
    db = Database(tmp_path / "router.sqlite")
    assets = AssetRepository(db)
    clips = ClipRepository(db)
    primary_asset = _asset(SourceKind.USER_ASSET, 1)
    historical_asset = _asset(SourceKind.HISTORICAL_ASSET, 2)
    reused_asset = _asset(SourceKind.USER_ASSET, 3)
    ignored_ai_asset = _asset(SourceKind.AI_VIDEO, 4)
    for asset in (primary_asset, historical_asset, reused_asset, ignored_ai_asset):
        assets.create(asset)
    primary = _clip(primary_asset, 0, quality=0.9)
    historical = _clip(historical_asset, 1, quality=0.75)
    reused = _clip(reused_asset, 2, quality=0.9, used_count=5, last_used_at=NOW - timedelta(days=1))
    ignored_ai = _clip(ignored_ai_asset, 3, quality=0.3)
    for clip in (primary, historical, reused, ignored_ai):
        clips.create(clip)
    search = ClipEmbeddingIndexer(db, _EmbeddingProvider())
    search.index_clips((primary, historical, reused, ignored_ai))
    return db, AssetRouter(search, assets, now=lambda: NOW), [primary, historical, reused, ignored_ai], primary_asset


def test_router_prefers_creator_continuous_clip_and_is_stably_ranked(tmp_path: Path) -> None:
    db, router, clips, primary_asset = _router(tmp_path)
    try:
        scene = _scene()
        first = router.route(scene)
        second = router.route(scene)

        assert [candidate.clip_id for candidate in first.candidates] == [candidate.clip_id for candidate in second.candidates]
        local = [candidate for candidate in first.candidates if candidate.clip_id is not None]
        assert len(local) == 3  # Top-3 are all valid, non-overlapping source Clips.
        assert first.candidates[0].recommended is True
        assert first.candidates[0].clip_id == clips[0].id
        assert first.candidates[0].asset_id == primary_asset.id
        assert all(candidate.source_kind in {SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET} for candidate in local)
        assert clips[3].id not in {candidate.clip_id for candidate in local}
        assert first.candidates[0].estimated_cost.amount == 0
        assert first.candidates[0].estimated_cost.currency == "USD"
        assert any("semantic match" in why for why in first.candidates[0].why)
        assert any("reuse penalty" in why for why in first.candidates[0].why)
        assert ClipRepository(db).get(clips[0].id).used_count == 0
    finally:
        db.close()


def test_router_filters_disallowed_assets_and_penalizes_reuse(tmp_path: Path) -> None:
    db, router, clips, _ = _router(tmp_path)
    try:
        candidates = router.route(_scene()).candidates
        ids = [candidate.clip_id for candidate in candidates if candidate.clip_id is not None]
        assert clips[3].id not in ids  # AI video is a local search hit but not reusable creator media.
        assert ids.index(clips[0].id) < ids.index(clips[2].id)
        assert candidates[0].reuse_count == 0
    finally:
        db.close()


def test_router_surfaces_capture_gap_without_generating_media(tmp_path: Path) -> None:
    db, _, _, _ = _router(tmp_path)
    try:
        router = AssetRouter(
            ClipEmbeddingIndexer(db, _EmbeddingProvider()), AssetRepository(db), capture_gap_threshold=0.99, now=lambda: NOW
        )
        result = router.route(_scene())
        capture = result.candidates[-1]
        assert capture.source_kind == SourceKind.CAPTURE
        assert capture.requires_capture is True and capture.recommended is True
        assert capture.asset_id is None and capture.clip_id is None
        assert capture.estimated_cost.amount is None and capture.estimated_cost.currency is None
        assert "for at least 3 seconds" in capture.why[1]
        assert "no media is generated" in capture.why[2]

        too_long = _scene().model_copy(update={"duration_target_ms": 5_000})
        assert router.route(too_long).candidates[0].source_kind == SourceKind.CAPTURE

        typography_only = _scene().model_copy(update={
            "preferred_sources": [SourceKind.TYPOGRAPHY], "fallback_sources": [SourceKind.CAPTURE],
        })
        assert router.route(typography_only).candidates[0].source_kind == SourceKind.CAPTURE
    finally:
        db.close()


def test_router_validates_configuration_input_and_returns_scene_order(tmp_path: Path) -> None:
    db, router, _, _ = _router(tmp_path)
    try:
        with pytest.raises(RoutingConfigurationError, match="candidate_pool_size"):
            AssetRouter(ClipEmbeddingIndexer(db, _EmbeddingProvider()), AssetRepository(db), candidate_pool_size=True)
        with pytest.raises(RoutingConfigurationError, match="match weight"):
            RoutingWeights(semantic_match=0, visual_quality=0, shot_suitability=0, freshness=0)
        with pytest.raises(RoutingInputError, match="ScenePlan"):
            router.route(object())  # type: ignore[arg-type]
        first_scene = _scene()
        second = _scene().model_copy(update={"order": 1})
        results = router.route_all((second, first_scene))
        assert [result.scene_plan_id for result in results] == [first_scene.id, second.id]
        with pytest.raises(RoutingInputError, match="contiguous"):
            router.route_all((first_scene, second.model_copy(update={"order": 2})))
    finally:
        db.close()
