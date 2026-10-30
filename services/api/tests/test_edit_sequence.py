"""Local editorial structure facts; these fixtures make no visual-quality claim."""
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.assembly.edit_plan import resolve_edit_plan, review_edit_plan, review_video_spec
from app.domain.models import (CandidateAsset, CostCategory, EditPlan, Project, RationalFps, ScenePlan,
    SourceKind, UsageCost, VideoScene, VideoSpec, VideoVisual, VisualIntent)


def sequence(kinds, intents, durations):
    project = Project(ip_profile_id=uuid4(), title="Sequence fixture", topic="Compression",
        fps=RationalFps(numerator=30, denominator=1), created_at=datetime.now(timezone.utc))
    source_id, source_clip = uuid4(), uuid4()
    scenes, choices, assets, intervals = [], {}, {}, {}
    for index, (kind, intent, duration) in enumerate(zip(kinds, intents, durations, strict=True)):
        scene = ScenePlan(project_id=project.id, scene_id=f"scene-{index}", order=index, purpose=intent,
            viewer_takeaway=intent, voice_text=f"Complete narration {index}.", duration_target_ms=duration,
            visual_intent=VisualIntent(subject="Compression"), preferred_sources=[kind])
        asset_id, clip_id = (uuid4(), uuid4()) if kind is SourceKind.AI_VIDEO else (source_id, source_clip)
        assets[asset_id] = SimpleNamespace(content_hash=str(asset_id), width=1080, height=1920, metadata={})
        choices[scene.id] = CandidateAsset(scene_plan_id=scene.id, source_kind=kind, asset_id=asset_id,
            clip_id=clip_id, match_score=.95, recommended=True, why=["fixture"],
            estimated_cost=UsageCost(category=CostCategory.USER_ASSET, amount=Decimal("0"), currency="USD"))
        intervals[clip_id] = (0, 30000)
        scenes.append(scene)
    plan = resolve_edit_plan(project, scenes, choices, clip_intervals=intervals, supplied=None)
    return project, scenes, choices, assets, intervals, plan


def test_individually_valid_scenes_expose_repeated_information_and_source_intervals():
    _, scenes, choices, assets, intervals, plan = sequence([SourceKind.USER_ASSET] * 3,
        ["Understand compression", "Understand compression", "Choose a bitrate"], [2000] * 3)
    plan = review_edit_plan(plan, scenes, choices, assets=assets, clip_intervals=intervals)
    whole = plan.whole_video
    assert [item.information_change for item in whole.scenes] == ["opening", "declared_repeated", "declared_changed"]
    assert whole.unintentional_repeat_ms == 4000
    assert whole.same_source_on_information_change_scene_ids == ["scene-2"]
    assert whole.stop_reasons == []  # Facts do not invent a universal creative-quality gate.
    assert EditPlan.model_validate_json(plan.model_dump_json()).whole_video == whole


@pytest.mark.parametrize("role", ["reference", "meme", "brand_marker", "comparison"])
def test_intentional_references_keep_repeat_evidence_without_soft_diversity_pressure(role):
    _, scenes, choices, assets, intervals, plan = sequence([SourceKind.USER_ASSET] * 2,
        ["First demonstration", "Revisit the result"], [2000, 2000])
    if role == "reference":
        scenes[1] = scenes[1].model_copy(update={"reuse_intent": "reference"})
    elif role == "comparison":
        scenes[1] = scenes[1].model_copy(update={"purpose": "对比实测", "visual_requirement": "action_evidence",
            "visual_requirement_reason": "保留同一实测比较画面"})
    else:
        assets[choices[scenes[1].id].asset_id].metadata["reuse_role"] = role
    whole = review_edit_plan(plan, scenes, choices, assets=assets, clip_intervals=intervals).whole_video
    assert whole.intentional_repeat_ms == 2000
    assert whole.unintentional_repeat_ms == 0
    assert whole.same_source_on_information_change_ms == 0


def test_person_distribution_and_measured_duration_refresh_without_person_quotas():
    project, scenes, choices, assets, intervals, plan = sequence(
        [SourceKind.USER_ASSET, SourceKind.AI_VIDEO, SourceKind.USER_ASSET, SourceKind.AI_VIDEO],
        ["Question", "Creator position", "Demonstration", "Conclusion"], [1000, 2000, 3000, 1000])
    initial = review_edit_plan(plan, scenes, choices, assets=assets, clip_intervals=intervals).whole_video
    assert initial.talking_scene_ids == ["scene-1", "scene-3"]
    assert (initial.first_talking_start_ms, initial.last_talking_end_ms, initial.longest_without_talking_ms) == (1000, 7000, 3000)
    durations = dict(zip((scene.id for scene in scenes), [2000, 1000, 6000, 3000], strict=True))
    measured = review_edit_plan(plan, scenes, choices, assets=assets, clip_intervals=intervals,
        durations_ms=durations, timing_basis="measured_master")
    assert measured.whole_video.duration_ms == 12000
    assert measured.whole_video.talking_duration_ms == 4000
    assert measured.whole_video.longest_without_talking_ms == 6000
    assert measured.whole_video.timing_basis == "measured_master"
    frames, rendered = 0, []
    for scene in scenes:
        candidate = choices[scene.id]
        duration_frames = durations[scene.id] * 30 // 1000
        rendered.append(VideoScene(scene_id=scene.scene_id, start_frame=frames, duration_frames=duration_frames,
            visual_role="talking" if candidate.source_kind is SourceKind.AI_VIDEO else "footage",
            visual=VideoVisual(source_kind=candidate.source_kind, asset_id=candidate.asset_id, clip_id=candidate.clip_id,
                clip_start_ms=0, clip_end_ms=30000, source_duration_ms=30000, authorization_reference="fixture intake")))
        frames += duration_frames
    spec = VideoSpec(project_id=project.id, format=project.format, width=1080, height=1920, fps=project.fps,
        scenes=rendered, edit_plan=measured)
    output = review_video_spec(spec, assets)
    assert output.edit_plan.whole_video.timing_basis == "rendered_timeline"
    assert output.edit_plan.whole_video.duration_ms == 12000
    assert output.edit_plan.whole_video.talking_scene_ids == ["scene-1", "scene-3"]
    assert output.edit_plan.whole_video.longest_without_talking_ms == 6000


@pytest.mark.parametrize("reference", [False, True])
def test_joint_selection_uses_a_suitable_alternative_when_information_intent_changes(tmp_path, reference):
    from app.db import AssetRepository
    from app.media.visual_facts import full_interval_geometry, scene_visual_key
    from test_material_resolution import material_library
    from test_material_selection import add_source, observed_pool
    db, project, _, _, base, _, resolver = material_library(tmp_path)
    try:
        own = add_source(db, project, tmp_path, SourceKind.USER_ASSET, "creator-demonstration")
        alternative = add_source(db, project, tmp_path, SourceKind.STOCK, "other-demonstration")
        scenes = [base.model_copy(update={"id": uuid4(), "scene_id": str(index), "order": index,
            "duration_target_ms": 2000, "viewer_takeaway": intent,
            "reuse_intent": "reference" if index and reference else "auto"})
            for index, intent in enumerate(["Understand the mechanism", "Choose a practical setting"])]
        windows = observed_pool(resolver, project, [own, alternative], scenes, lambda v, s: (True, .95, "direct"))
        selected = resolver._select(project, scenes, windows, None)
        if reference:
            assert [scene.preferred_clip_id for scene in selected] == [own[1].id, own[1].id]
        else:
            assert {scene.preferred_clip_id for scene in selected} == {own[1].id, alternative[1].id}
        last_source = next(source for source in [own, alternative] if source[1].id == selected[1].preferred_clip_id)
        facts = full_interval_geometry(AssetRepository(db).get(last_source[0].id), last_source[1])
        assert facts["editorial_selections"][scene_visual_key(scenes[1])]["whole_video_same_source_on_information_change_ms"] == 0
    finally:
        db.close()


def test_normal_render_rechecks_whole_video_instead_of_trusting_saved_summary(tmp_path):
    from unittest.mock import Mock
    from app.assembly.video_spec import VideoSpecAssembler
    from app.db import AssetRepository, ClipRepository, Database, ProjectRepository
    from app.domain.models import Job, JobStatus, JobType, RenderVideoJobPayload
    from app.jobs.handlers import RenderVideoJobHandler
    from app.jobs.runner import JobExecutionError
    from test_render_api import _seed
    path = tmp_path / "render-sequence.sqlite3"
    project, scene, candidate = _seed(path, tmp_path)
    with Database(path) as db:
        assets = AssetRepository(db)
        source = assets.get(candidate.asset_id)
        assets.update(source.model_copy(update={"width": 1920, "height": 1080}))
        spec = VideoSpecAssembler(assets, ClipRepository(db)).assemble(project, [scene], {scene.id: candidate})
        assert spec.edit_plan.whole_video.stop_reasons == ["landscape_fallback_dominates_video:hook"]
        # A stale/edited summary cannot waive the actual final scene timeline.
        spec = spec.model_copy(update={"edit_plan": spec.edit_plan.model_copy(update={"whole_video":
            spec.edit_plan.whole_video.model_copy(update={"stop_reasons": []})})})
        now = datetime.now(timezone.utc)
        job = Job(project_id=project.id, type=JobType.RENDER, status=JobStatus.RUNNING,
            idempotency_key="fixture-render-sequence", created_at=now, updated_at=now,
            payload=RenderVideoJobPayload(project_id=project.id, render_id=uuid4(), video_spec=spec))
        renderer = SimpleNamespace(data_root=tmp_path, render=Mock())
        with pytest.raises(JobExecutionError) as failure:
            RenderVideoJobHandler(ProjectRepository(db), renderer, tmp_path / "outputs")(job)
        assert failure.value.code == "landscape_fallback_dominates_video:hook"
        renderer.render.assert_not_called()
