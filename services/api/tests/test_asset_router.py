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
from app.search.index import ClipSearchHit


NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)


def test_explicit_planned_clip_survives_weak_retrieval_without_claiming_visual_qa(tmp_path):
    with Database(tmp_path/"planned-source.sqlite") as db:
        asset=AssetRepository(db).create(_asset(SourceKind.USER_ASSET,7))
        clip=ClipRepository(db).create(_clip(asset,0))
        class Search:
            def search(self,*args,**kwargs):raise AssertionError("已选来源不应重新检索")
        scene=_scene().model_copy(update={"preferred_clip_id":clip.id})
        router=AssetRouter(Search(),AssetRepository(db),now=lambda:NOW)
        chosen=next(c for c in router.route_all([scene])[0].candidates if c.recommended)
        assert chosen.clip_id==clip.id and not chosen.requires_capture
        assert any("not visual QA" in reason for reason in chosen.why)
        missing=scene.model_copy(update={"preferred_clip_id":uuid4()})
        assert next(c for c in router.route(missing).candidates if c.recommended).requires_capture


def test_scene_relevance_precedes_ownership_quality_and_freshness(tmp_path):
    db = Database(tmp_path / "intent.sqlite")
    try:
        own, external = _asset(SourceKind.USER_ASSET, 7), _asset(SourceKind.STOCK, 8)
        for asset in (own, external):
            AssetRepository(db).create(asset)
        own_clip = _clip(own, 0, quality=1)
        documentary = _clip(external, 0, quality=0.5, used_count=2, last_used_at=NOW)
        class Search:
            def search(self, query, **kwargs):
                return [ClipSearchHit(own_clip, 0.75), ClipSearchHit(documentary, 0.9)]
        scene = _scene().model_copy(update={"preferred_sources": [SourceKind.STOCK, SourceKind.USER_ASSET]})
        result = AssetRouter(Search(), AssetRepository(db), now=lambda: NOW).route(scene)
        assert result.candidates[0].asset_id == external.id
        assert result.candidates[0].recommended
        assert result.candidates[0].estimated_cost.category.value == "stock"
        speaking = scene.model_copy(update={"visual_requirement": "creator_speaking"})
        assert all(c.source_kind is not SourceKind.STOCK for c in AssetRouter(Search(), AssetRepository(db)).route(speaking).candidates)
    finally:
        db.close()


@pytest.mark.parametrize("creator_source", [SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET])
def test_comparable_footage_prefers_creator_without_inflating_match_score(tmp_path, creator_source):
    db = Database(tmp_path / "creator-preference.sqlite")
    try:
        own, external = _asset(creator_source, 7), _asset(SourceKind.STOCK, 8)
        for asset in (own, external):
            AssetRepository(db).create(asset)
        own_clip, stock_clip = _clip(own, 0), _clip(external, 0)
        class Search:
            def search(self, query, **kwargs):
                return [ClipSearchHit(stock_clip, 0.90), ClipSearchHit(own_clip, 0.88)]
        scene = _scene().model_copy(update={"preferred_sources": [SourceKind.STOCK, creator_source]})
        result = AssetRouter(Search(), AssetRepository(db), now=lambda: NOW).route(scene)
        chosen = next(item for item in result.candidates if item.recommended)
        stock = next(item for item in result.candidates if item.asset_id == external.id)
        assert chosen.asset_id == own.id
        assert chosen.match_score < stock.match_score
        assert any("优先使用创作者素材" in reason for reason in chosen.why)
    finally:
        db.close()


@pytest.mark.parametrize("quality,used_count,last_used_at", [(0.1, 0, None), (0.9, 5, NOW)])
def test_creator_preference_respects_quality_but_legacy_counts_do_not_impose_occupancy(tmp_path, quality, used_count, last_used_at):
    db = Database(tmp_path / "creator-quality.sqlite")
    try:
        own, external = _asset(SourceKind.USER_ASSET, 7), _asset(SourceKind.STOCK, 8)
        for asset in (own, external):
            AssetRepository(db).create(asset)
        own_clip = _clip(own, 0, quality=quality, used_count=used_count, last_used_at=last_used_at)
        stock_clip = _clip(external, 0)
        class Search:
            def search(self, query, **kwargs):
                return [ClipSearchHit(own_clip, 0.90), ClipSearchHit(stock_clip, 0.89)]
        scene = _scene().model_copy(update={"preferred_sources": [SourceKind.USER_ASSET, SourceKind.STOCK]})
        result = AssetRouter(Search(), AssetRepository(db), now=lambda: NOW).route(scene)
        assert next(item for item in result.candidates if item.recommended).asset_id == (external.id if quality == .1 else own.id)
    finally:
        db.close()


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
            assert "creator" in texts[0] and "operating software" in texts[0]
            return EmbeddingBatch(((1.0, 0.0),))
        # The source-kind-ineligible AI Clip is semantically perfect too; the
        # router must filter it after local search using its stored Asset.
        vectors = []
        for text in texts:
            if "quality_score:" not in text:
                vectors.append((1.0, 0.0))
                continue
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
    primary = _clip(primary_asset, 0, quality=0.95)
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
        assert any("source occupancy" in why for why in first.candidates[0].why)
        assert ClipRepository(db).get(clips[0].id).used_count == 0
    finally:
        db.close()


def test_comparison_route_prefers_only_approved_playback_interval(tmp_path: Path) -> None:
    db = Database(tmp_path / "reviewed-comparison.sqlite")
    try:
        asset = Asset(source_file=str(tmp_path / "comparison.mp4"), content_hash="e" * 64,
            duration_ms=10_000, width=1280, height=720, fps=RationalFps(numerator=30, denominator=1),
            authorization_reference="creator-rights", imported_at=NOW)
        clip = Clip(asset_id=asset.id, start_ms=0, end_ms=10_000, asset_duration_ms=10_000,
            transcript="同一段画面比较压缩画质")
        review = {str(clip.id): {
            "source_asset_id": str(asset.id), "source_clip_id": str(clip.id), "source_content_hash": asset.content_hash,
            "method": "human_full_interval_review", "decision": "approved", "coverage": "full_interval_continuous",
            "comparison_layout": "side_by_side_equal_halves", "source_subtitle_clearance_verified": True,
            "clip_interval": {"start_ms": 1_000, "end_ms": 6_000},
            "covered_start_ms": 1_000, "covered_end_ms": 6_000,
            "crop_window": {"x": 0, "y": 0, "width": 1280, "height": 650},
            "reviewer_reference": "creator-review", "evidence_reference": "reviewed-interval.mp4", "evidence_sha256": "f" * 64,
        }}
        AssetRepository(db).create(asset.model_copy(update={"metadata": {"source_presentation_reviews": review}}))
        ClipRepository(db).create(clip)
        scene = _scene().model_copy(update={"visual_requirement": "action_evidence",
            "visual_requirement_reason": "同一段画面的实际压缩差异是证据",
            "visual_intent": VisualIntent(subject="同一画面码率对比", action="展示差异"),
            "preferred_sources": [SourceKind.USER_ASSET], "duration_target_ms": 3_000})
        class Searcher:
            def search(self, query, *, top_k=10, **kwargs):
                return [ClipSearchHit(clip, 0.6, "lexical_overlap")]
        router = AssetRouter(Searcher(), AssetRepository(db), now=lambda: NOW)
        selected = next(item for item in router.route(scene).candidates if item.recommended)
        assert (selected.clip_id, selected.playback_start_ms, selected.playback_end_ms) == (clip.id, 1_000, 6_000)
        longer_audio = scene.model_copy(update={"duration_target_ms": 5_001})
        assert next(item for item in router.route(longer_audio).candidates if item.recommended).playback_start_ms is None
        master_scene = scene.model_copy(update={"duration_target_ms": 6_000})
        partial = selected
        with pytest.raises(RoutingInputError, match="explicit_footage_selection_changed_or_not_authorized"):
            router.explicit_footage(master_scene, [partial])
        assert next(item for item in router.route(master_scene).candidates if item.recommended).playback_start_ms is None
        too_short = master_scene.model_copy(update={"duration_target_ms": 6_700})
        assert next(item for item in router.route(too_short).candidates if item.recommended).playback_start_ms is None

        # Revisit a cleared comparison as evidence, within the existing limit
        # of no more than half the vertical video using landscape footage.
        from app.db import IPProfileRepository, ProjectRepository
        from app.domain.models import IPProfile, Project
        profile = IPProfileRepository(db).create(IPProfile(creator_name="Creator"))
        project = ProjectRepository(db).create(Project(ip_profile_id=profile.id, title="Comparison", topic="Bitrate",
            fps=RationalFps(numerator=30, denominator=1), created_at=NOW))
        portrait = AssetRepository(db).create(_asset(SourceKind.HISTORICAL_ASSET, 9))
        alternatives = [_clip(portrait, index) for index in (0, 1)]
        for alternative in alternatives:
            ClipRepository(db).create(alternative)
        class PlanSearch:
            def search(self, query, **kwargs):
                return ([ClipSearchHit(clip, 0.9)] if "码率对比" in query else
                    [ClipSearchHit(alternative, 0.9) for alternative in alternatives])
        plan = [scene.model_copy(update={"id": uuid4(), "project_id": project.id, "order": index}) for index in (0, 1)]
        plan.extend(_scene().model_copy(update={"project_id": project.id, "order": index}) for index in (2, 3))
        plan_router = AssetRouter(PlanSearch(), AssetRepository(db), now=lambda: NOW)
        replay = [next(item for item in result.candidates if item.recommended)
            for result in plan_router.route_all(plan)]
        assert [item.clip_id for item in replay[:2]] == [clip.id, clip.id]
        assert all((item.playback_start_ms, item.playback_end_ms) == (1_000, 6_000) for item in replay[:2])

        # Preserve cleared evidence for the output-format decision instead of
        # replacing it with a card or a new-shoot requirement.
        too_much_landscape = plan[:3]
        preserved = next(item for item in plan_router.route_all(too_much_landscape)[1].candidates if item.recommended)
        assert preserved.asset_id == asset.id and preserved.source_kind is SourceKind.USER_ASSET
        assert any("适合竖屏" in reason for reason in preserved.why)
        assert not any("整片横版" in reason for reason in preserved.why)
        AssetRepository(db).update(asset.model_copy(update={"metadata": {"source_presentation_reviews": {
            str(clip.id): {**review[str(clip.id)], "decision": "rejected"}}}}))
        assert next(item for item in router.route(scene).candidates if item.recommended).playback_start_ms is None
        rejected = next(item for item in plan_router.route_all(plan)[1].candidates if item.recommended)
        assert rejected.playback_start_ms is None  # 不能继承被拒的确切呈现通过。
    finally:
        db.close()


def test_router_filters_disallowed_assets_and_keeps_usage_count_as_history(tmp_path: Path) -> None:
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


def test_router_uses_imported_assets_without_requiring_a_usage_tag(tmp_path: Path) -> None:
    db, router, clips, primary_asset = _router(tmp_path)
    try:
        AssetRepository(db).update(primary_asset.model_copy(update={"metadata": {"r1_usage": "reference"}}))
        ids = [candidate.clip_id for candidate in router.route(_scene()).candidates if candidate.clip_id is not None]
        assert clips[0].id in ids
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
            RoutingWeights(semantic_match=0, visual_quality=0, shot_suitability=0)
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
    ([SourceKind.TYPOGRAPHY, SourceKind.USER_ASSET], [SourceKind.CAPTURE], SourceKind.TYPOGRAPHY),
    ([SourceKind.USER_ASSET, SourceKind.TYPOGRAPHY], [SourceKind.CAPTURE], SourceKind.CAPTURE),
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
            assert any("Explicit primary typography" in reason for reason in recommended[0].why)
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


def test_explanatory_typography_first_is_selected_even_with_footage(tmp_path):
    from app.domain.models import GraphicCardPlan
    db, router, _, _ = _router(tmp_path)
    try:
        scene = _scene().model_copy(update={
            "visual_requirement": "explanatory", "visual_requirement_reason": "A text card explains the point.",
            "preferred_sources": [SourceKind.TYPOGRAPHY], "fallback_sources": [SourceKind.USER_ASSET],
            "graphic_plan": GraphicCardPlan(kind="text_card", treatment="key_point", points=["关键点"]),
        })
        result = router.route(scene)
        assert next(item for item in result.candidates if item.recommended).source_kind is SourceKind.TYPOGRAPHY
    finally:
        db.close()


def test_whole_vertical_plan_diversifies_repeated_landscape_fallback(tmp_path):
    from app.db import IPProfileRepository, ProjectRepository
    from app.domain.models import GraphicCardPlan, IPProfile, Project
    db, router, _, source = _router(tmp_path)
    try:
        profile = IPProfile(creator_name="Creator")
        IPProfileRepository(db).create(profile)
        project = Project(ip_profile_id=profile.id, title="Vertical", topic="Topic",
                          fps=RationalFps(numerator=30, denominator=1), created_at=NOW)
        ProjectRepository(db).create(project)
        AssetRepository(db).update(source.model_copy(update={"width": 1280, "height": 720}))
        first = _scene().model_copy(update={"project_id": project.id, "visual_requirement": "action_evidence",
            "visual_requirement_reason": "The action must be visible."})
        second = _scene().model_copy(update={"project_id": project.id, "order": 1,
            "visual_requirement": "explanatory", "visual_requirement_reason": "The card explains the result.",
            "fallback_sources": [SourceKind.TYPOGRAPHY],
            "graphic_plan": GraphicCardPlan(kind="text_card", treatment="key_point", points=["结果"]),
        })
        results = router.route_all((first, second))
        assert next(item for item in results[0].candidates if item.recommended).asset_id == source.id
        chosen = next(item for item in results[1].candidates if item.recommended)
        assert chosen.asset_id != source.id
        assert router.assets.get(chosen.asset_id).height >= router.assets.get(chosen.asset_id).width
    finally:
        db.close()


@pytest.mark.parametrize("graphic_kind,treatment,points", [
    ("text_card", "key_point", ["关键点"]),
    ("diagram", "diagram_flow", ["输入", "输出"]),
])
def test_landscape_dominance_preserves_source_instead_of_promoting_graphic(tmp_path, graphic_kind, treatment, points):
    from app.db import IPProfileRepository, ProjectRepository
    from app.domain.models import GraphicCardPlan, IPProfile, Project

    db, _, clips, source = _router(tmp_path)
    try:
        profile = IPProfile(creator_name="Creator")
        IPProfileRepository(db).create(profile)
        project = Project(ip_profile_id=profile.id, title="Full source", topic="Topic",
                          fps=RationalFps(numerator=30, denominator=1), created_at=NOW)
        ProjectRepository(db).create(project)
        AssetRepository(db).update(source.model_copy(update={"width": 1920, "height": 1080}))
        clip = ClipRepository(db).update(clips[0].model_copy(update={"end_ms": 12_000}))

        class Search:
            def search(self, query, **kwargs):
                return [ClipSearchHit(clip, .95)]

        scenes = tuple(_scene().model_copy(update={"project_id": project.id, "scene_id": f"part-{order}",
            "order": order, "visual_requirement": "explanatory", "visual_requirement_reason": "展示实际操作过程。",
            "fallback_sources": [SourceKind.TYPOGRAPHY],
            "graphic_plan": GraphicCardPlan(kind=graphic_kind, treatment=treatment, points=points),
        }) for order in range(3))
        results = AssetRouter(Search(), AssetRepository(db), now=lambda: NOW).route_all(scenes)
        chosen = [next(item for item in result.candidates if item.recommended) for result in results]
        assert all(item.asset_id == source.id and item.source_kind is SourceKind.USER_ASSET for item in chosen)
        from app.source_usage import SourceUsageService
        usage = SourceUsageService(db)
        used = [usage.candidate_uses(item, scene.scene_id, scene.duration_target_ms)[0]
                for item, scene in zip(chosen, scenes, strict=True)]
        assert [(item["start_ms"], item["end_ms"]) for item in used] == [(0, 3000), (3000, 6000), (6000, 9000)]
        assert any("适合竖屏" in reason for reason in chosen[-1].why)
        assert not any("整片横版" in reason for reason in chosen[-1].why)
    finally:
        db.close()


def test_route_all_avoids_reusing_a_clip_when_an_adequate_alternative_exists(tmp_path):
    from app.db import IPProfileRepository, ProjectRepository
    from app.domain.models import IPProfile, Project
    db, router, _, _ = _router(tmp_path)
    try:
        profile = IPProfile(creator_name="Creator")
        IPProfileRepository(db).create(profile)
        project = Project(ip_profile_id=profile.id, title="Vertical", topic="Topic",
                          fps=RationalFps(numerator=30, denominator=1), created_at=NOW)
        ProjectRepository(db).create(project)
        first = _scene().model_copy(update={"project_id": project.id})
        second = _scene().model_copy(update={"project_id": project.id, "order": 1})

        results = router.route_all((first, second))

        chosen = [next(item for item in result.candidates if item.recommended) for result in results]
        assert chosen[0].clip_id != chosen[1].clip_id
        assert chosen[1].match_score >= 0.45
    finally:
        db.close()


@pytest.mark.parametrize("requirement,expected", [
    ("unknown", SourceKind.CAPTURE), ("creator_speaking", SourceKind.CAPTURE),
    ("action_evidence", SourceKind.CAPTURE), ("explanatory", SourceKind.CAPTURE),
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
        assert chosen.source_kind is not SourceKind.TYPOGRAPHY
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
