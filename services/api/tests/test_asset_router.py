from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app.db import AssetRepository, ClipRepository, Database, ImageAssetRepository
from app.domain.models import Asset, Clip, ImageAsset, ProjectFormat, RationalFps, ScenePlan, SourceKind, VisualIntent
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


def test_router_talking_requires_persisted_same_project_exact_copy_admission(tmp_path: Path, admit_talking_run) -> None:
    db = Database(tmp_path / "talking-route.sqlite")
    try:
        scene = _scene().model_copy(update={
            "preferred_sources": [SourceKind.AI_VIDEO], "fallback_sources": [SourceKind.CAPTURE],
            "duration_target_ms": 4_000,
        })
        asset = _asset(SourceKind.AI_VIDEO, 8).model_copy(update={
            "duration_ms": 4_000,
            "metadata": {"talking_generation": {"qa_state": "verified", "human_review_state": "approved"}},
        })
        AssetRepository(db).create(asset)
        clip = ClipRepository(db).create(Clip(asset_id=asset.id, start_ms=0, end_ms=4_000, asset_duration_ms=4_000))

        class EmptySearch:
            def search(self, query, *, top_k):
                return []

        router = AssetRouter(EmptySearch(), AssetRepository(db))
        def routed(s):
            return {candidate.asset_id for candidate in router.route(s).candidates if candidate.asset_id}

        assert asset.id not in routed(scene)  # approved child, no Run
        stored, _, _ = admit_talking_run(db, scene.project_id, asset, clip, scene.voice_text)
        assert asset.id in routed(scene)
        assert asset.id not in routed(scene.model_copy(update={"duration_target_ms": 3_000}))
        assert asset.id not in routed(scene.model_copy(update={"voice_text": "Unreviewed different copy"}))
        assert asset.id not in routed(scene.model_copy(update={"project_id": uuid4()}))
        pending = dict(stored.metadata)
        pending["talking_run"] = {**pending["talking_run"], "admission_state": "pending"}
        AssetRepository(db).update(stored.model_copy(update={"metadata": pending}))
        assert asset.id not in routed(scene)
        rejected = dict(stored.metadata)
        rejected["talking_run"] = {**rejected["talking_run"], "continuity_review_state": "rejected"}
        AssetRepository(db).update(stored.model_copy(update={"metadata": rejected}))
        assert asset.id not in routed(scene)
    finally:
        db.close()


def test_router_does_not_publish_assets_marked_reference_or_unknown(tmp_path: Path) -> None:
    db, router, clips, primary_asset = _router(tmp_path)
    try:
        AssetRepository(db).update(primary_asset.model_copy(update={"metadata": {"r1_usage": "reference"}}))
        ids = [candidate.clip_id for candidate in router.route(_scene()).candidates if candidate.clip_id is not None]
        assert clips[0].id not in ids
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
        typography_result = router.route(typography_only).candidates
        assert typography_result[0].source_kind == SourceKind.TYPOGRAPHY
        assert typography_result[0].recommended is True and typography_result[0].requires_capture is False
        assert typography_result[-1].source_kind == SourceKind.CAPTURE
    finally:
        db.close()


def test_router_surfaces_imported_static_visual_before_capture_gap(tmp_path: Path) -> None:
    db, _, _, _ = _router(tmp_path)
    try:
        image = ImageAsset(
            source_kind=SourceKind.SCREENSHOT,
            source_file="C:/media/screen.png",
            content_hash="e" * 64,
            width=800,
            height=600,
            authorization_reference="creator-screen",
            imported_at=NOW,
        )
        image_repo = ImageAssetRepository(db)
        image_repo.create(image)
        scene = _scene().model_copy(update={"preferred_sources": [SourceKind.SCREENSHOT], "fallback_sources": [SourceKind.CAPTURE]})
        router = AssetRouter(ClipEmbeddingIndexer(db, _EmbeddingProvider()), AssetRepository(db), images=image_repo, capture_gap_threshold=0.99, now=lambda: NOW)
        candidates = router.route(scene).candidates
        assert candidates[0].source_kind is SourceKind.SCREENSHOT
        assert candidates[0].recommended is True and candidates[0].asset_id == image.id
        assert candidates[-1].source_kind is SourceKind.CAPTURE
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


@pytest.mark.parametrize("preferred,fallback,expected", [
    ([SourceKind.USER_ASSET, SourceKind.TYPOGRAPHY], [SourceKind.CAPTURE], SourceKind.TYPOGRAPHY),
    ([SourceKind.USER_ASSET], [SourceKind.TYPOGRAPHY, SourceKind.CAPTURE], SourceKind.CAPTURE),
    ([SourceKind.AI_VIDEO, SourceKind.TYPOGRAPHY], [SourceKind.CAPTURE], SourceKind.CAPTURE),
])
def test_weak_lexical_match_only_selects_explicit_preferred_graphic(tmp_path, preferred, fallback, expected):
    from app.search import ClipTextSearchService
    from app.routing.asset_router import WEAK_LEXICAL_ROUTE_REASON

    db, _, _, _ = _router(tmp_path)
    try:
        scene = _scene().model_copy(update={"preferred_sources": preferred, "fallback_sources": fallback})
        router = AssetRouter(ClipTextSearchService(db), AssetRepository(db), capture_gap_threshold=0.99, now=lambda: NOW)
        first = router.route(scene)
        second = router.route(scene)
        assert first == second
        recommended = [c for c in first.candidates if c.recommended]
        assert len(recommended) == 1 and recommended[0].source_kind is expected
        if expected is SourceKind.TYPOGRAPHY:
            assert any("Explicit preferred typography" in reason for reason in recommended[0].why)
            assert recommended[0].requires_capture is False
        if SourceKind.USER_ASSET in preferred:
            capture = next(c for c in first.candidates if c.source_kind is SourceKind.CAPTURE)
            assert WEAK_LEXICAL_ROUTE_REASON in capture.why
            assert any(c.clip_id is not None and c.match_score < 0.99 for c in first.candidates)
    finally:
        db.close()


def test_strong_local_match_still_precedes_typography(tmp_path):
    db, router, clips, _ = _router(tmp_path)
    try:
        scene = _scene().model_copy(update={"preferred_sources": [SourceKind.USER_ASSET, SourceKind.TYPOGRAPHY]})
        recommended = next(c for c in router.route(scene).candidates if c.recommended)
        assert recommended.clip_id == clips[0].id
    finally:
        db.close()


@pytest.mark.parametrize("requirement,expected", [
    ("unknown", SourceKind.CAPTURE), ("creator_speaking", SourceKind.CAPTURE),
    ("action_evidence", SourceKind.CAPTURE), ("explanatory", SourceKind.TYPOGRAPHY),
])
def test_declared_requirement_controls_fallback_not_retrieval_score(tmp_path, requirement, expected):
    from app.search import ClipTextSearchService
    db, _, _, _ = _router(tmp_path)
    try:
        values = {**_scene().model_dump(), "visual_requirement": requirement,
            "visual_requirement_reason": None if requirement == "unknown" else "This scene expresses the editorial point.",
            "fallback_sources": [SourceKind.TYPOGRAPHY, SourceKind.CAPTURE]}
        scene = ScenePlan.model_validate(values)
        candidates = AssetRouter(ClipTextSearchService(db), AssetRepository(db), capture_gap_threshold=0.99, now=lambda: NOW).route(scene).candidates
        chosen = next(c for c in candidates if c.recommended)
        assert chosen.source_kind is expected
        if requirement == "explanatory":
            assert any(scene.visual_requirement_reason in reason for reason in chosen.why)
        # Changing declaration cannot upgrade any real Clip's score/quality.
        assert all(c.match_score < 0.99 for c in candidates)
    finally:
        db.close()


@pytest.mark.parametrize("requirement", ["creator_speaking", "action_evidence"])
def test_direct_assembly_cannot_replace_required_visual_with_typography(requirement):
    from app.assembly.edit_plan import resolve_edit_plan, EditPlanPreflightError
    from app.domain.models import Project
    from app.routing.asset_router import _typography_fallback
    scene = ScenePlan.model_validate({**_scene().model_dump(), "visual_requirement": requirement,
        "visual_requirement_reason": "The source performance is necessary."})
    project = Project(id=scene.project_id, ip_profile_id=uuid4(), title="Requirement", topic="Requirement", created_at=NOW,
        fps=RationalFps(numerator=30, denominator=1))
    candidate = _typography_fallback(scene, recommended=True)
    with pytest.raises(EditPlanPreflightError, match="cannot replace required"):
        resolve_edit_plan(project, [scene], {scene.id: candidate}, clip_intervals={}, supplied=None)
