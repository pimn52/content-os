"""V72 read-only planning seam: no provider, media or subjective QA calls."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.db import (
    AssetRepository, AudioAssetRepository, ClipRepository, Database, ProviderCallRepository,
    ProviderMachineSettingRepository,
)
from app.domain.models import (
    Asset, AudioAsset, Clip, ProviderMachineSetting, RationalFps, ScenePlan, SourceKind, TranscriptSegment,
    VisualIntent,
)
from app.main import create_app
from app.assembly.edit_plan import presentation_for_style


def test_subject_first_selects_supported_talking_and_footage_panels_and_preserves_default():
    from app.assembly.edit_plan import _default_edit_plan
    from app.domain.models import CandidateAsset, CostCategory, Project, UsageCost, VisualStyleTokens
    project = Project(title="Style", topic="Style", ip_profile_id=uuid4(), fps=RationalFps(numerator=30, denominator=1), created_at=datetime.now(timezone.utc))
    scene = _scene(str(project.id))
    # This is presentation geometry only, not asset admission.
    for kind, expected in (
        (SourceKind.AI_VIDEO, "portrait_panel"),
        (SourceKind.USER_ASSET, "full_canvas"),
        (SourceKind.HISTORICAL_ASSET, "full_canvas"),
        (SourceKind.TYPOGRAPHY, "full_canvas"),
    ):
        candidate = CandidateAsset(scene_plan_id=scene.id, source_kind=kind,
            asset_id=uuid4() if kind is not SourceKind.TYPOGRAPHY else None,
            clip_id=uuid4() if kind is not SourceKind.TYPOGRAPHY else None,
            match_score=1, why=["fixture"], estimated_cost=UsageCost(category=CostCategory.RENDER, amount=None))
        entry = _default_edit_plan(project, [scene], {scene.id: candidate}).scenes[0]
        assert presentation_for_style(entry, VisualStyleTokens()).model_dump() == entry.model_dump()
        chosen = presentation_for_style(entry, VisualStyleTokens(name="content-os-subject-first"))
        assert chosen.portrait_presentation.value == expected
        assert chosen.selected_asset_id == entry.selected_asset_id
        assert chosen.selected_clip_id == entry.selected_clip_id
        assert chosen.framing_policy == entry.framing_policy
        assert chosen.graphic_text == entry.graphic_text


def test_comparison_stack_requires_an_explicit_comparison_scene_and_16_9_media():
    import pytest
    from types import SimpleNamespace
    from app.assembly.edit_plan import _default_edit_plan
    from app.domain.models import CandidateAsset, CostCategory, Project, UsageCost, VisualStyleTokens

    project = Project(title="Style", topic="Style", ip_profile_id=uuid4(), fps=RationalFps(numerator=30, denominator=1), created_at=datetime.now(timezone.utc))
    scene = _scene(str(project.id)).model_copy(update={"visual_intent": VisualIntent(
        subject="same action bitrate comparison", action="left and right comparison, preserve labels",
        description="two side-by-side views",
    )})
    candidate = CandidateAsset(scene_plan_id=scene.id, source_kind=SourceKind.USER_ASSET,
        asset_id=uuid4(), clip_id=uuid4(), match_score=1, why=["fixture"],
        estimated_cost=UsageCost(category=CostCategory.RENDER, amount=None))
    entry = _default_edit_plan(project, [scene], {scene.id: candidate}).scenes[0]
    style = VisualStyleTokens(name="content-os-portrait-comparison")

    clip=SimpleNamespace(id=candidate.clip_id,start_ms=0,end_ms=5000)
    asset=SimpleNamespace(width=1280,height=720,content_hash='a'*64,metadata={'visual_analysis':{str(clip.id):dict(
        source_content_hash='a'*64,clip_interval={'start_ms':1000,'end_ms':4000},comparison_layout='side_by_side_equal_halves',
        subtitle_regions=[],evidence_reference='synthetic-source-geometry',coverage='full_interval_continuous',
        covered_start_ms=1000,covered_end_ms=4000)}})
    with pytest.raises(ValueError,match='geometry_unknown'):
        presentation_for_style(entry, style, asset, scene_plan=scene,clip=clip)
    used_interval=(1000,4000)
    selected = presentation_for_style(entry, style, asset, scene_plan=scene,clip=clip,playback_interval_ms=used_interval)
    assert selected.portrait_presentation.value == "stacked_comparison"
    assert selected.subtitle_treatment.value == "timed_captions"
    assert selected.selected_asset_id == candidate.asset_id

    non_comparison = scene.model_copy(update={"visual_intent": VisualIntent(subject="camera", action="recording a scene")})
    assert presentation_for_style(entry, style, asset, scene_plan=non_comparison,clip=clip).portrait_presentation.value == "full_canvas"
    with pytest.raises(ValueError, match="comparison_source_layout_or_subtitle_geometry_unknown"):
        presentation_for_style(entry, VisualStyleTokens(), asset, scene_plan=scene,clip=clip)
    with pytest.raises(ValueError,match='geometry_unknown'):
        presentation_for_style(entry,style,SimpleNamespace(width=1280,height=720),scene_plan=scene)
    facts=asset.metadata['visual_analysis'][str(clip.id)]
    for changes in ({'coverage':'sampled_keyframe'}, {'covered_start_ms':1001}, {'covered_end_ms':3999}):
        original=dict(facts);facts.update(changes)
        with pytest.raises(ValueError,match='full_interval_clearance_required'):
            presentation_for_style(entry,style,asset,scene_plan=scene,clip=clip,playback_interval_ms=used_interval)
        facts.clear();facts.update(original)
    facts['subtitle_regions']=[[.3,.88,.4,.08]]
    cropped=presentation_for_style(entry,style,asset,scene_plan=scene,clip=clip,playback_interval_ms=used_interval)
    assert cropped.source_bottom_crop_ratio==pytest.approx((720-(int(.88*720)-2))/720)
    assert cropped.source_subtitle_evidence_reference=='synthetic-source-geometry'
    facts['source_content_hash']='b'*64
    with pytest.raises(ValueError,match='geometry_unknown'):presentation_for_style(entry,style,asset,scene_plan=scene,clip=clip)
    with pytest.raises(ValueError, match="portrait_comparison_requires_16_9_source"):
        facts['source_content_hash']='a'*64
        presentation_for_style(entry, style, SimpleNamespace(width=1280,height=800,content_hash='a'*64,metadata=asset.metadata), scene_plan=scene,clip=clip,playback_interval_ms=used_interval)


def test_source_frame_interval_matches_remotion_scene_duration():
    from app.assembly.edit_plan import source_frame_interval_ms
    from app.domain.models import RationalFps
    assert source_frame_interval_ms(384_329,172,RationalFps(numerator=30,denominator=1),395_964)==(384_333,390_067)


def test_repeated_landscape_fallback_dominating_portrait_plan_requires_replan():
    from types import SimpleNamespace
    from app.domain.models import EditVisualRole, PortraitPresentation
    from app.production_preflight import _dominant_landscape_fallback

    landscape, portrait = uuid4(), uuid4()
    assets = SimpleNamespace(get=lambda value: SimpleNamespace(
        width=1920 if value == landscape else 1080,
        height=1080 if value == landscape else 1920,
    ))
    entry = lambda scene, duration, source: (
        scene, duration, source, EditVisualRole.FOOTAGE, PortraitPresentation.FULL_CANVAS,
    )
    balanced = (entry("camera-1", 2000, landscape), entry("portrait", 6000, portrait), entry("camera-2", 2000, landscape))
    assert _dominant_landscape_fallback(balanced, assets) == ()
    dominant = (entry("camera-1", 5000, landscape), entry("portrait", 2000, portrait), entry("camera-2", 4000, landscape))
    assert _dominant_landscape_fallback(dominant, assets) == (
        "landscape_fallback_dominates_video:camera-1,camera-2",
    )
    assert _dominant_landscape_fallback((entry("one-long-shot", 6000, landscape),
        entry("portrait", 4000, portrait)), assets) == (
        "landscape_fallback_dominates_video:one-long-shot",
    )


@pytest.mark.parametrize("output_format", ["vertical", "horizontal", "square"])
def test_landscape_footage_guard_applies_only_to_portrait_output(output_format):
    from types import SimpleNamespace
    from app.domain.models import EditVisualRole, PortraitPresentation, ProjectFormat
    from app.production_preflight import dominant_landscape_fallback_in_spec

    source_id = uuid4()
    assets = SimpleNamespace(get=lambda value: SimpleNamespace(width=1920, height=1080))
    spec = SimpleNamespace(format=ProjectFormat(output_format),
        edit_plan=SimpleNamespace(scenes=[SimpleNamespace(scene_id="source", portrait_presentation=PortraitPresentation.FULL_CANVAS)]),
        scenes=[SimpleNamespace(scene_id="source", duration_frames=180, visual_role=EditVisualRole.FOOTAGE,
            visual=SimpleNamespace(asset_id=source_id, vertical_reframe_mode=SimpleNamespace(value="contain")))])
    assert dominant_landscape_fallback_in_spec(spec, assets) == (
        ("landscape_fallback_dominates_video:source",) if output_format == "vertical" else ())


def test_source_planning_prefers_existing_portrait_before_centered_landscape(tmp_path):
    from app.production_preflight import _choose_candidate
    from app.domain.models import CandidateAsset,CostCategory,UsageCost
    from test_remotion_renderer import _setup
    db,source,clip,spec=_setup(tmp_path);db.data_root=tmp_path
    try:
        source=AssetRepository(db).update(source.model_copy(update={'width':1920,'height':1080}))
        scene=_scene(str(spec.project_id)).model_copy(update={'duration_target_ms':1000})
        raw=CandidateAsset(scene_plan_id=scene.id,source_kind=SourceKind.USER_ASSET,asset_id=source.id,clip_id=clip.id,
            match_score=.9,recommended=True,why=['same semantic score'],estimated_cost=UsageCost(category=CostCategory.USER_ASSET,amount=None))
        native=AssetRepository(db).create(source.model_copy(update={'id':uuid4(),'width':1080,'height':1920,'content_hash':'b'*64}))
        native_clip=ClipRepository(db).create(clip.model_copy(update={'id':uuid4(),'asset_id':native.id}))
        portrait=raw.model_copy(update={'asset_id':native.id,'clip_id':native_clip.id,'recommended':False})
        selected,need=_choose_candidate(scene,(raw,portrait),ClipRepository(db),AssetRepository(db))
        assert selected.asset_id==native.id and need=='none'
        assert _choose_candidate(scene,(raw,portrait),ClipRepository(db),AssetRepository(db),portrait_output=False)[0].asset_id==source.id
        assert _choose_candidate(scene,(raw,portrait.model_copy(update={'match_score':.1})),ClipRepository(db),AssetRepository(db))[0].asset_id==source.id
        assert _choose_candidate(scene,(raw,),ClipRepository(db),AssetRepository(db))[0].asset_id==source.id
    finally:db.close()


def test_preserved_full_frame_checks_actual_bytes_without_claiming_visual_quality(tmp_path):
    import hashlib
    from app.db import IPProfileRepository,ProjectRepository
    from app.domain.models import IPProfile,Project,Asset,Clip,VisualStyleTokens
    from app.project_drafts import save_project_draft
    from app.production_preflight import build_production_preflight
    source=tmp_path/'source.mp4';source.write_bytes(b'local-source-fixture')
    with Database(tmp_path/'preserved.sqlite3') as db:
        db.data_root=tmp_path
        ip=IPProfileRepository(db).create(IPProfile(creator_name='Creator'))
        project=ProjectRepository(db).create(Project(ip_profile_id=ip.id,title='Source',topic='Source',
            created_at=datetime.now(timezone.utc),fps=RationalFps(numerator=30,denominator=1)))
        scene=_scene(project.id)
        asset=AssetRepository(db).create(Asset(source_file=str(source),content_hash=hashlib.sha256(source.read_bytes()).hexdigest(),
            duration_ms=10000,width=1080,height=1920,fps=project.fps,authorization_reference='registered',imported_at=project.created_at))
        ClipRepository(db).create(Clip(asset_id=asset.id,start_ms=0,end_ms=10000,asset_duration_ms=10000,
            visual_description='creator showing a workflow',quality_score=.9))
        draft=save_project_draft(db,project,script=scene.voice_text,topic=project.topic,scenes=[scene],routes=[],confirmed=[],video_spec=None)
        plan=build_production_preflight(db,project,draft,style_tokens=VisualStyleTokens())
        assert plan.scenes[0].suitability=='preserved_full_frame'
        assert f'source_suitability_unknown:{scene.scene_id}' in plan.stop_reasons
        assert f'source_subtitle_handling_unresolved:{scene.scene_id}' in plan.stop_reasons
        assert any(action.kind == 'analysis' and action.required for action in plan.actions)
        from app.production_runs import ProductionRunService
        assert not ProductionRunService._ready_existing_visual(plan.scenes[0])
        assert plan.scenes[0].subtitle_treatment.value=='none'
        assert any('不是整段内容' in reason for reason in plan.scenes[0].reasons)
        source.write_bytes(b'changed')
        changed=build_production_preflight(db,project,draft,style_tokens=VisualStyleTokens())
        assert changed.scenes[0].suitability=='unknown'
        assert f'source_suitability_unknown:{scene.scene_id}' in changed.stop_reasons


def test_source_planning_reuses_only_verified_portrait_for_the_selected_interval(tmp_path):
    import hashlib
    from app.production_preflight import _choose_candidate
    from app.domain.models import CandidateAsset,CostCategory,UsageCost,SourceCropWindow,VerifiedVerticalDerivationRequest,VerifiedVerticalDerivationEvidence
    from app.media import VerticalSourceDerivationService,MediaImporter,ProbeMetadata
    from test_remotion_renderer import _setup
    db,source,clip,spec=_setup(tmp_path);db.data_root=tmp_path
    try:
        source=AssetRepository(db).create(source.model_copy(update={'id':uuid4(),'width':1920,'height':1080,
            'content_hash':hashlib.sha256(Path(source.source_file).read_bytes()).hexdigest()}))
        clip=ClipRepository(db).create(clip.model_copy(update={'id':uuid4(),'asset_id':source.id}))
        crop=SourceCropWindow(x=420,y=0,width=1080,height=1080)
        evidence=VerifiedVerticalDerivationEvidence(source_asset_id=source.id,source_clip_id=clip.id,source_content_hash=source.content_hash,
            crop_window=crop,covered_start_ms=clip.start_ms,covered_end_ms=clip.end_ms,
            face_head_clearance_verified=True,crop_stability_verified=True,source_subtitle_clearance_verified=True,
            method='human_full_interval_review',method_version='fixture',coverage='full_interval_continuous',confidence='reviewed',
            evidence_reference='Synthetic test only; no actual media quality claim')
        request=VerifiedVerticalDerivationRequest(idempotency_key='fixture-portrait',source_asset_id=source.id,source_clip_id=clip.id,
            start_ms=clip.start_ms,end_ms=clip.end_ms,crop_window=crop,output_height=1080,source_subtitle_state='not_observed',evidence=evidence)
        class Probe:
            def probe(self,path):return ProbeMetadata(duration_ms=1001,width=1080,height=1080,fps=source.fps,has_audio=False,metadata={})
        derived=VerticalSourceDerivationService(db,tmp_path,MediaImporter(db,tmp_path,Probe()),runner=lambda a:Path(a[-1]).write_bytes(b'fixture-derived')).derive(request)
        scene=_scene(str(spec.project_id)).model_copy(update={'duration_target_ms':1000})
        raw=CandidateAsset(scene_plan_id=scene.id,source_kind=source.source_kind,asset_id=source.id,clip_id=clip.id,
            match_score=.9,recommended=True,why=['exact source'],estimated_cost=UsageCost(category=CostCategory.USER_ASSET,amount=None))
        assert _choose_candidate(scene,(raw,),ClipRepository(db),AssetRepository(db))[0].asset_id==derived.asset.id
        bounded = raw.model_copy(update={'playback_start_ms':clip.start_ms + 100,'playback_end_ms':clip.start_ms + 900})
        shorter = scene.model_copy(update={'duration_target_ms':800})
        selected = _choose_candidate(shorter,(bounded,),ClipRepository(db),AssetRepository(db))[0]
        assert (selected.playback_start_ms, selected.playback_end_ms) == (derived.clip.start_ms + 100, derived.clip.start_ms + 900)
        assert _choose_candidate(scene,(raw,),ClipRepository(db),AssetRepository(db),portrait_output=False)[0].asset_id==source.id
        cropped=raw.model_copy(update={'asset_id':derived.asset.id,'clip_id':derived.clip.id})
        assert _choose_candidate(scene,(cropped,raw),ClipRepository(db),AssetRepository(db),portrait_output=False)[0].asset_id==source.id
        assert _choose_candidate(scene,(cropped,),ClipRepository(db),AssetRepository(db),portrait_output=False)==(None,'capture')
        other=ClipRepository(db).create(clip.model_copy(update={'id':uuid4(),'start_ms':0,'end_ms':1001}))
        outside=raw.model_copy(update={'clip_id':other.id})
        assert _choose_candidate(scene,(outside,),ClipRepository(db),AssetRepository(db))[0].asset_id==source.id
        shifted=ClipRepository(db).create(clip.model_copy(update={'id':uuid4(),'start_ms':clip.start_ms+1}))
        # A containing derivative starts at a different source moment; selecting
        # its first frame would silently shift the requested evidence interval.
        assert _choose_candidate(scene,(raw.model_copy(update={'clip_id':shifted.id}),),ClipRepository(db),AssetRepository(db))[0].asset_id==source.id
        Path(derived.asset.source_file).write_bytes(b'changed-derived-bytes')
        assert _choose_candidate(scene,(raw,),ClipRepository(db),AssetRepository(db))[0].asset_id==source.id
    finally:db.close()


def test_editorial_requires_real_bounded_headline_and_safe_margin():
    import pytest
    from types import SimpleNamespace
    from app.assembly.edit_plan import EditPlanPreflightError
    from app.domain.models import EditPlanScene, VisualStyleTokens
    entry = EditPlanScene(scene_plan_id=uuid4(),scene_id="speaker",visual_role="talking",
        selected_source_kind="ai_video",selected_asset_id=uuid4(),selected_clip_id=uuid4(),
        framing_policy="face_safe_contain",fallback={"graphic_treatment":"headline","text":"Context"})
    style = VisualStyleTokens(name="content-os-editorial")
    for text,asset,reason in ((None,None,"editorial_semantic_plan_required"),
        ("x"*33,None,"talking_panel_graphics_exceed_header"),
        ("Context",SimpleNamespace(width=720,height=1280),"portrait_graphic_margin_unavailable")):
        with pytest.raises(EditPlanPreflightError,match=reason):
            presentation_for_style(entry.model_copy(update={"graphic_text":text}),style,asset)


def test_normal_preflight_retains_selected_style_and_invalidates_default_fingerprint(tmp_path):
    with TestClient(create_app(tmp_path / "subject-style.sqlite")) as client:
        project_id, scene = _create_draft(client)
        graphic = scene.model_copy(update={"preferred_sources": [SourceKind.TYPOGRAPHY]})
        client.put(f"/projects/{project_id}/draft", json={"script": scene.voice_text, "topic": "New topic", "scenes": [graphic.model_dump(mode="json")]}).raise_for_status()
        default = client.post(f"/projects/{project_id}/production-preflight", json={}).json()
        selected = client.post(f"/projects/{project_id}/production-preflight", json={"style_tokens": {"name": "content-os-subject-first"}})
        selected.raise_for_status()
        result = selected.json()
        assert result["preliminary_edit_plan"]["style_tokens"]["name"] == "content-os-subject-first"
        whole = result["preliminary_edit_plan"]["whole_video"]
        assert whole["timing_basis"] == "estimated_scene_duration"
        assert whole["duration_ms"] == scene.duration_target_ms
        assert whole["scenes"][0]["information_intent"] == scene.purpose
        assert whole["talking_scene_ids"] == []
        assert result["fingerprint"] != default["fingerprint"]
        assert result["stop_reasons"] == default["stop_reasons"]
        stale = client.post(f"/projects/{project_id}/production-preflight", json={"style_tokens": {"name": "content-os-subject-first"}, "expected_fingerprint": default["fingerprint"]})
        assert stale.status_code == 409
        assert client.get(f"/projects/{project_id}/provider-calls").json() == []


def _scene(project_id, *, talking: bool = False, text: str = "A fresh creator point.") -> ScenePlan:
    return ScenePlan(
        project_id=project_id, scene_id="hook", order=0, purpose="hook",
        voice_text=text, duration_target_ms=3_000,
        visual_intent=VisualIntent(subject="creator", action="speaking" if talking else "showing a workflow"),
        preferred_sources=[SourceKind.USER_ASSET, SourceKind.AI_VIDEO] if talking else [SourceKind.USER_ASSET],
        fallback_sources=[SourceKind.TYPOGRAPHY],
    )


def _presentation_fallback_draft(db):
    from app.db import IPProfileRepository, ProjectRepository
    from app.domain.models import GraphicCardPlan, IPProfile, Project
    from app.project_drafts import save_project_draft

    profile = IPProfile(creator_name="Synthetic creator")
    IPProfileRepository(db).create(profile)
    project = Project(ip_profile_id=profile.id, title="本人讲解", topic="说明与真实证据",
        presentation_style="creator_led", fps=RationalFps(numerator=30, denominator=1),
        created_at=datetime.now(timezone.utc))
    ProjectRepository(db).create(project)
    speaking = _scene(project.id, talking=True, text="本人解释新文案。").model_copy(update={
        "scene_id": "speaking", "visual_requirement": "creator_speaking",
        "visual_requirement_reason": "保存的呈现要求需要本人说出这段新文案。", "fallback_sources": [SourceKind.CAPTURE]})
    evidence = _scene(project.id, text="观察真实动作证据。").model_copy(update={
        "scene_id": "evidence", "order": 1, "visual_requirement": "action_evidence",
        "visual_requirement_reason": "这段主张需要真实动作支持。", "fallback_sources": [SourceKind.CAPTURE]})
    explanation = _scene(project.id, text="这是辅助解释的关键点。").model_copy(update={
        "scene_id": "explanation", "order": 2, "duration_target_ms": 6000,
        "visual_requirement": "explanatory", "visual_requirement_reason": "只解释概念，不取代人物和动作证据。",
        "graphic_plan": GraphicCardPlan(kind="text_card", treatment="headline", points=["辅助说明"])})
    scenes = [speaking, evidence, explanation]
    draft = save_project_draft(db, project, script="".join(scene.voice_text for scene in scenes),
        topic=project.topic, scenes=scenes, routes=[], confirmed=[], video_spec=None)
    return project, draft


def test_creator_led_does_not_replace_missing_footage_with_explanatory_card(tmp_path):
    from app.domain.models import VisualStyleTokens
    from app.planning_input import presentation_plan_gaps
    from app.production_preflight import build_production_preflight

    with Database(tmp_path / "presentation-fallback.sqlite3") as db:
        project, draft = _presentation_fallback_draft(db)
        # A fallback card cannot become the primary just because retrieval failed.
        assert presentation_plan_gaps(project, draft.scenes) == ()
        chosen = build_production_preflight(db, project, draft, style_tokens=VisualStyleTokens(), repair_allowance=0)
        graphic = next(scene for scene in chosen.scenes if scene.scene_id == "explanation")
        assert graphic.planned_source_kind is not SourceKind.TYPOGRAPHY
        assert graphic.selected_candidate is None
        assert "visual_route_unresolved:explanation" in chosen.stop_reasons
        assert "creator_led_typography_share_exceeded" not in chosen.stop_reasons
        assert chosen.status == "blocked" and chosen.dispatch_performed is False
        assert "creator_led_missing_speaking" not in chosen.stop_reasons
        assert "creator_led_missing_real_evidence" not in chosen.stop_reasons
        auto = build_production_preflight(db, project.model_copy(update={"presentation_style": "auto"}),
            draft, style_tokens=VisualStyleTokens(), repair_allowance=0)
        assert "creator_led_typography_share_exceeded" not in auto.stop_reasons
        assert set(auto.stop_reasons) == set(chosen.stop_reasons)
        assert auto.fingerprint != chosen.fingerprint
        assert ProviderCallRepository(db).list_for_project(project.id) == []


def _create_draft(client: TestClient, *, talking: bool = False, text: str = "A fresh creator point.") -> tuple[str, ScenePlan]:
    created = client.post("/projects", json={"title": "Preflight", "topic": "New topic"})
    assert created.status_code == 201
    project_id = created.json()["id"]
    scene = _scene(project_id, talking=talking, text=text)
    saved = client.put(f"/projects/{project_id}/draft", json={
        "script": text, "topic": "New topic", "scenes": [scene.model_dump(mode="json")],
    })
    assert saved.status_code == 200, saved.text
    return project_id, scene


def test_unaligned_approved_master_is_blocked_before_render(tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace
    from app.assembly.video_spec import NarrationTimelineError

    voice = SimpleNamespace(id=uuid4(), content_hash="a" * 64, metadata={}, duration_ms=4_000)
    monkeypatch.setattr("app.production_preflight._approved_master", lambda *args, **kwargs: voice)
    def unaligned(*args):
        raise NarrationTimelineError("timestamps do not contain scene copy")
    monkeypatch.setattr("app.assembly.video_spec._master_narration_intervals", unaligned)
    with TestClient(create_app(tmp_path / "unaligned-master.sqlite")) as client:
        project_id, _ = _create_draft(client)
        response = client.post(f"/projects/{project_id}/production-preflight", json={})
    assert response.status_code == 200
    assert response.json()["status"] == "blocked"
    assert "approved_master_timing_unaligned" in response.json()["stop_reasons"]


def test_preflight_requires_saved_scene_plan_and_never_calls_provider(tmp_path: Path) -> None:
    path = tmp_path / "missing.sqlite"
    with TestClient(create_app(path)) as client:
        project_id = client.post("/projects", json={"title": "Empty", "topic": "New"}).json()["id"]
        response = client.post(f"/projects/{project_id}/production-preflight", json={})
        assert response.status_code == 409
        assert "no ScenePlan" in response.json()["detail"]
        assert client.get(f"/projects/{project_id}/provider-calls").json() == []


def test_weak_text_match_does_not_prove_capture_or_waive_other_gates(tmp_path):
    path = tmp_path / "weak.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, scene = _create_draft(client)
        with Database(path) as db:
            asset = AssetRepository(db).create(Asset(
                source_file=str(tmp_path / "source.mp4"), content_hash="c" * 64,
                duration_ms=5_000, width=1920, height=1080,
                fps=RationalFps(numerator=30, denominator=1), authorization_reference="rights",
                imported_at=datetime.now(timezone.utc),
            ))
            ClipRepository(db).create(Clip(asset_id=asset.id, start_ms=0, end_ms=5_000,
                asset_duration_ms=5_000, transcript="creator"))
        first = client.post(f"/projects/{project_id}/production-preflight", json={}).json()
        assert "visual_route_unresolved:hook" in first["stop_reasons"]
        assert "capture_required:hook" in first["stop_reasons"]  # compatibility, not proof of necessity
        assert first["scenes"][0]["selected_candidate"] is None
        assert any("does not establish" in r for r in first["scenes"][0]["reasons"])
        graphic = scene.model_copy(update={"preferred_sources": [SourceKind.TYPOGRAPHY, SourceKind.USER_ASSET]})
        client.put(f"/projects/{project_id}/draft", json={"script": scene.voice_text, "topic": "New topic",
            "scenes": [graphic.model_dump(mode="json")]}).raise_for_status()
        response = client.post(f"/projects/{project_id}/production-preflight", json={
            "style_tokens": {"name": "content-os-subject-first"},
        })
        assert response.status_code == 200, response.text
        second = response.json()
        assert second["scenes"][0]["planned_source_kind"] == "typography"
        assert second["scenes"][0]["production_need"] == "none"
        assert second["preliminary_edit_plan"] is not None
        assert second["status"] == "blocked" and second["dispatch_performed"] is False
        assert "voice_capability_not_verified" in second["stop_reasons"]
        talking = graphic.model_copy(update={"preferred_sources": [SourceKind.AI_VIDEO, SourceKind.TYPOGRAPHY]})
        client.put(f"/projects/{project_id}/draft", json={"script": scene.voice_text, "topic": "New topic",
            "scenes": [talking.model_dump(mode="json")]}).raise_for_status()
        third = client.post(f"/projects/{project_id}/production-preflight", json={}).json()
        assert third["scenes"][0]["production_need"] == "new_talking"
        assert "talking_source_not_bound:hook" in third["stop_reasons"]
        assert client.get(f"/projects/{project_id}/provider-calls").json() == []


def test_normal_draft_persists_explanatory_requirement_without_clearing_voice_gates(tmp_path):
    with TestClient(create_app(tmp_path / "intent.sqlite")) as client:
        project_id, scene = _create_draft(client)
        assert client.get(f"/projects/{project_id}/draft").json()["scenes"][0]["visual_requirement"] == "unknown"
        payload = scene.model_dump(mode="json")
        payload.update(visual_requirement="explanatory", visual_requirement_reason="A short concept card preserves this explanation.", caption_emphasis=["Creator point"])
        saved = client.put(f"/projects/{project_id}/draft", json={"script": scene.voice_text,
            "topic": "New topic", "scenes": [payload]})
        saved.raise_for_status()
        assert client.get(f"/projects/{project_id}/draft").json()["scenes"][0]["visual_requirement_reason"] == payload["visual_requirement_reason"]
        body = client.post(f"/projects/{project_id}/production-preflight", json={}).json()
        assert body["scenes"][0]["planned_source_kind"] != "typography"
        assert body["scenes"][0]["selected_candidate"] is None
        assert "visual_route_unresolved:hook" in body["stop_reasons"]
        assert body["status"] == "blocked" and "voice_capability_not_verified" in body["stop_reasons"]
        payload.update(visual_requirement="creator_speaking", visual_requirement_reason="The creator must visibly deliver the words.")
        client.put(f"/projects/{project_id}/draft", json={"script": scene.voice_text,
            "topic": "New topic", "scenes": [payload]}).raise_for_status()
        body = client.post(f"/projects/{project_id}/production-preflight", json={}).json()
        assert body["scenes"][0]["selected_candidate"] is None
        assert body["scenes"][0]["production_need"] == "capture"  # no new Talking source was authorized
        assert client.get(f"/projects/{project_id}/provider-calls").json() == []


def test_preflight_names_new_talking_voice_unknown_cost_and_bound_repair_without_dispatch(tmp_path: Path) -> None:
    path = tmp_path / "talking.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, scene = _create_draft(client, talking=True)
        response = client.post(f"/projects/{project_id}/production-preflight", json={})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["evidence_level"] == "deterministic_metadata_only"
        assert body["dispatch_performed"] is False and body["status"] == "blocked"
        decision = body["scenes"][0]
        assert decision["scene_plan_id"] == str(scene.id)
        assert decision["visual_role"] == "talking"
        assert decision["selected_candidate"] is None
        assert decision["production_need"] == "new_talking"
        assert decision["suitability"] == "unknown"
        assert body["preliminary_edit_plan"] is None
        assert {action["kind"] for action in body["actions"]} == {
            "voice", "talking", "render", "repair_allowance",
        }
        assert all(action["local_runtime_ms"] is None for action in body["actions"])
        assert body["cost_estimate"]["unknown_cost_count"] == 3
        assert body["cost_estimate"]["known_amount"] == "0"
        assert "unknown_cost_requires_budget_authorization" in body["stop_reasons"]
        assert body["authorization_needs"] == []  # Existing Profile/asset admission owns consent.
        assert body["estimated_user_active_minutes"] is None
        assert client.get(f"/projects/{project_id}/provider-calls").json() == []
    with Database(path) as db:
        assert ProviderCallRepository(db).list_for_project(project_id) == []


@pytest.mark.parametrize("preferred,fallback,need", [
    ([SourceKind.TALKING_PROFILE], [SourceKind.AI_VIDEO], "new_talking"),
    ([SourceKind.TALKING_PROFILE], [], "new_talking"),
    ([SourceKind.USER_ASSET], [SourceKind.AI_VIDEO], "new_talking"),
    ([SourceKind.USER_ASSET], [SourceKind.TALKING_PROFILE], "new_talking"),
    ([SourceKind.CAPTURE], [], "capture"),
    ([SourceKind.USER_ASSET], [SourceKind.CAPTURE], "capture"),
])
def test_normal_preflight_uses_declared_talking_alternatives_without_waiving_dispatch_gates(tmp_path, preferred, fallback, need):
    with TestClient(create_app(tmp_path / "declared-talking.sqlite")) as client:
        project_id, scene = _create_draft(client)
        payload = scene.model_dump(mode="json")
        payload.update(preferred_sources=[source.value for source in preferred],
            fallback_sources=[source.value for source in fallback], visual_requirement="creator_speaking",
            visual_requirement_reason="本人通过已有授权形象讲解本段新文案")
        saved = client.put(f"/projects/{project_id}/draft", json={"script": scene.voice_text,
            "topic": "New topic", "scenes": [payload]})
        saved.raise_for_status()
        response = client.post(f"/projects/{project_id}/production-preflight", json={})
        response.raise_for_status()
        body = response.json()
        decision = body["scenes"][0]
        assert decision["production_need"] == need and decision["selected_candidate"] is None
        assert body["status"] == "blocked" and body["dispatch_performed"] is False
        assert "voice_capability_not_verified" in body["stop_reasons"]
        assert "unknown_cost_requires_budget_authorization" in body["stop_reasons"]
        if need == "new_talking":
            assert decision["planned_source_kind"] == "ai_video" and decision["visual_role"] == "talking"
            assert "capture_required:hook" not in body["stop_reasons"]
            assert "talking_source_not_bound:hook" in body["stop_reasons"]
            assert "talking_capability_not_verified" in body["stop_reasons"]
            assert any(action["kind"] == "talking" and action["required"] for action in body["actions"])
        else:
            assert "capture_required:hook" in body["stop_reasons"]
            assert all(action["kind"] != "talking" for action in body["actions"])
        assert client.get(f"/projects/{project_id}/provider-calls").json() == []


def test_preflight_exposes_horizontal_subtitle_and_transform_unknowns(tmp_path: Path, monkeypatch) -> None:
    # Inspect the single-candidate geometry here; whole-video diversity has its own router test.
    monkeypatch.setattr("app.routing.AssetRouter.route_all", lambda self, scenes: tuple(self.route(s) for s in scenes))
    path = tmp_path / "horizontal.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, scene = _create_draft(client)
        with Database(path) as db:
            asset = Asset(
                source_kind=SourceKind.USER_ASSET, source_file=str(tmp_path / "horizontal.mp4"),
                content_hash="a" * 64, duration_ms=5_000, width=1920, height=1080,
                fps=RationalFps(numerator=30, denominator=1),
                authorization_reference="source-rights", imported_at=datetime.now(timezone.utc),
                metadata={"burned_in_subtitles": "present", "subtitle_evidence_reference": "inspection:known-bottom"},
            )
            AssetRepository(db).create(asset)
            clip = ClipRepository(db).create(Clip(
                asset_id=asset.id, start_ms=0, end_ms=5_000, asset_duration_ms=5_000,
                transcript=scene.voice_text, visual_description="creator showing a workflow", quality_score=0.9,
            ))
        response = client.post(f"/projects/{project_id}/production-preflight", json={})
        assert response.status_code == 200, response.text
        body = response.json()
        decision = body["scenes"][0]
        assert decision["selected_candidate"]["clip_id"] == str(clip.id)
        assert decision["production_need"] == "none"
        assert decision["burned_in_subtitles"] == "present"
        assert decision["subtitle_treatment"] == "none"
        assert decision["suitability"] == "unknown"
        assert body["preliminary_edit_plan"] is not None
        assert body["preliminary_edit_plan"]["scenes"][0]["subtitle_treatment"] == "none"
        assert body["preliminary_edit_plan"]["scenes"][0]["portrait_presentation"] == "full_canvas"
        assert {action["kind"] for action in body["actions"]} == {
            "voice", "analysis", "transform", "render", "repair_allowance",
        }
        assert f"source_suitability_unknown:{scene.scene_id}" in body["stop_reasons"]
        assert body["cost_estimate"]["unknown_cost_count"] == 4
        assert client.get(f"/projects/{project_id}/provider-calls").json() == []


def test_preflight_rejects_unbound_master_claim_without_generation_evidence(tmp_path: Path) -> None:
    path = tmp_path / "master-reuse.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, scene = _create_draft(client, talking=True)
        now = datetime.now(timezone.utc)
        with Database(path) as db:
            master = AudioAsset(
                source_kind=SourceKind.USER_ASSET, source_file=str(tmp_path / "approved-master.wav"),
                content_hash="f" * 64, duration_ms=3_000, sample_rate=24_000, channels=1,
                authorization_reference="voice-consent", imported_at=now,
                transcript_source="fixture-asr", transcript_segments=[TranscriptSegment(
                    start_ms=0, end_ms=3_000, text=scene.voice_text,
                )],
                metadata={"voice_generation": {
                    "project_id": project_id, "qa_state": "verified", "human_review_state": "approved",
                    "human_review": {
                        "approved": True, "evidence_reference": "fixture:voice-review",
                        "findings": ["Exact fixture approved"],
                        "likeness": "pass", "naturalness": "pass", "emphasis": "pass",
                        "pace": "pass", "pauses": "pass", "rhythm": "pass",
                        "reviewed_at": now.isoformat(),
                    },
                }},
            )
            AudioAssetRepository(db).create(master)
        response = client.post(f"/projects/{project_id}/production-preflight", json={})
        assert response.status_code == 200, response.text
        body = response.json()
        # A metadata-only fixture has no bound generation/QA or VoiceProfile.
        # Actual approved retained-Master reuse is covered in retained-production.
        assert body["reusable_master_audio_id"] is None
        voice = next(action for action in body["actions"] if action["kind"] == "voice")
        assert voice["required"] is True
        assert body["authorization_needs"] == []  # No repeated authorization at Run time.
        assert client.get(f"/projects/{project_id}/provider-calls").json() == []


def test_preflight_keeps_claimed_subtitle_absence_unknown_and_checks_budget_gate(tmp_path: Path) -> None:
    path = tmp_path / "budget-suitability.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, scene = _create_draft(client)
        with Database(path) as db:
            asset = Asset(
                source_kind=SourceKind.USER_ASSET, source_file=str(tmp_path / "claimed-clear.mp4"),
                content_hash="e" * 64, duration_ms=5_000, width=1080, height=1920,
                fps=RationalFps(numerator=30, denominator=1),
                authorization_reference="rights", imported_at=datetime.now(timezone.utc),
                metadata={"burned_in_subtitles": "absent", "subtitle_evidence_reference": "caller-claim"},
            )
            AssetRepository(db).create(asset)
            ClipRepository(db).create(Clip(
                asset_id=asset.id, start_ms=0, end_ms=5_000, asset_duration_ms=5_000,
                transcript=scene.voice_text, visual_description="creator showing a workflow",
            ))
        budget = client.put(f"/projects/{project_id}/budget", json={
            "currency": "USD", "max_calls": 0, "allow_unknown_cost": True,
        })
        assert budget.status_code == 200
        response = client.post(f"/projects/{project_id}/production-preflight", json={})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["scenes"][0]["burned_in_subtitles"] == "unknown"
        assert body["scenes"][0]["suitability"] == "unknown"
        assert "transform" not in {action["kind"] for action in body["actions"]}
        assert "budget_call_limit" in body["stop_reasons"]
        assert "provider_license_scope_unverified" in body["stop_reasons"]
        assert "unknown_cost_requires_budget_authorization" not in body["stop_reasons"]
        assert client.get(f"/projects/{project_id}/provider-calls").json() == []


def test_preflight_rejects_stale_copy_style_source_and_capability_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "stale.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, scene = _create_draft(client)
        first = client.post(f"/projects/{project_id}/production-preflight", json={})
        assert first.status_code == 200
        old = first.json()["fingerprint"]
        same = client.post(f"/projects/{project_id}/production-preflight", json={"expected_fingerprint": old})
        assert same.status_code == 200 and same.json()["fingerprint"] == old
        styled = client.post(f"/projects/{project_id}/production-preflight", json={
            "expected_fingerprint": old, "style_tokens": {"accent_color": "#ff0000"},
        })
        assert styled.status_code == 409 and styled.json()["detail"] == "stale_production_preflight"

        with Database(path) as db:
            ProviderMachineSettingRepository(db).save(ProviderMachineSetting(
                capability="voice", mode="local", provider="fixture", model="test", runtime="local", machine_id="test",
                values={"speed": 1.0}, updated_at=datetime.now(timezone.utc),
            ))
        changed_capability = client.post(f"/projects/{project_id}/production-preflight", json={"expected_fingerprint": old})
        assert changed_capability.status_code == 409
        latest = client.post(f"/projects/{project_id}/production-preflight", json={}).json()["fingerprint"]

        revised = scene.model_copy(update={"voice_text": "A changed creator point."})
        updated = client.put(f"/projects/{project_id}/draft", json={
            "script": "A changed creator point.", "topic": "New topic",
            "scenes": [revised.model_dump(mode="json")],
        })
        assert updated.status_code == 200
        # Script edits clear old scenes; the old preview cannot be reused.
        no_plan = client.post(f"/projects/{project_id}/production-preflight", json={"expected_fingerprint": latest})
        assert no_plan.status_code == 409
        assert no_plan.json()["detail"] == "stale_production_preflight"


def test_preflight_source_change_invalidates_plan_but_not_unrelated_media(tmp_path: Path) -> None:
    path = tmp_path / "source.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, scene = _create_draft(client)
        with Database(path) as db:
            asset = Asset(
                source_kind=SourceKind.USER_ASSET, source_file=str(tmp_path / "source.mp4"),
                content_hash="b" * 64, duration_ms=5_000, width=1080, height=1920,
                fps=RationalFps(numerator=30, denominator=1),
                authorization_reference="rights", imported_at=datetime.now(timezone.utc),
            )
            AssetRepository(db).create(asset)
            ClipRepository(db).create(Clip(
                asset_id=asset.id, start_ms=0, end_ms=5_000, asset_duration_ms=5_000,
                transcript=scene.voice_text, visual_description="creator showing a workflow",
            ))
        first = client.post(f"/projects/{project_id}/production-preflight", json={})
        assert first.status_code == 200
        identity = first.json()["fingerprint"]
        visual_identity = first.json()["scenes"][0]["visual_dependency_fingerprint"]
        with Database(path) as db:
            unrelated = asset.model_copy(update={"id": uuid4(), "content_hash": "c" * 64})
            AssetRepository(db).create(unrelated)
        same = client.post(f"/projects/{project_id}/production-preflight", json={"expected_fingerprint": identity})
        assert same.status_code == 200
        with Database(path) as db:
            AssetRepository(db).update(asset.model_copy(update={"authorization_reference": "new-rights-scope"}))
        stale = client.post(f"/projects/{project_id}/production-preflight", json={"expected_fingerprint": identity})
        assert stale.status_code == 409
        refreshed = client.post(f"/projects/{project_id}/production-preflight", json={})
        assert refreshed.json()["scenes"][0]["visual_dependency_fingerprint"] != visual_identity
