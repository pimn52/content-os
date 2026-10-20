"""V72 read-only planning seam: no provider, media or subjective QA calls."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

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
        source_content_hash='a'*64,start_ms=0,end_ms=5000,comparison_layout='side_by_side_equal_halves',
        subtitle_regions=[],evidence_reference='synthetic-source-geometry',coverage='full_interval_continuous',
        covered_start_ms=0,covered_end_ms=5000)}})
    selected = presentation_for_style(entry, style, asset, scene_plan=scene,clip=clip)
    assert selected.portrait_presentation.value == "stacked_comparison"
    assert selected.subtitle_treatment.value == "timed_captions"
    assert selected.selected_asset_id == candidate.asset_id

    non_comparison = scene.model_copy(update={"visual_intent": VisualIntent(subject="camera", action="recording a scene")})
    assert presentation_for_style(entry, style, asset, scene_plan=non_comparison,clip=clip).portrait_presentation.value == "full_canvas"
    assert presentation_for_style(entry, VisualStyleTokens(), asset, scene_plan=scene,clip=clip).portrait_presentation.value == "stacked_comparison"
    with pytest.raises(ValueError,match='geometry_unknown'):
        presentation_for_style(entry,style,SimpleNamespace(width=1280,height=720),scene_plan=scene)
    facts=asset.metadata['visual_analysis'][str(clip.id)]
    for changes in ({'coverage':'sampled_keyframe'}, {'covered_start_ms':1}, {'covered_end_ms':4999}):
        original=dict(facts);facts.update(changes)
        with pytest.raises(ValueError,match='full_interval_clearance_required'):
            presentation_for_style(entry,style,asset,scene_plan=scene,clip=clip)
        facts.clear();facts.update(original)
    facts['subtitle_regions']=[[.3,.88,.4,.08]]
    cropped=presentation_for_style(entry,style,asset,scene_plan=scene,clip=clip)
    assert cropped.source_bottom_crop_ratio==pytest.approx((720-(int(.88*720)-2))/720)
    assert cropped.source_subtitle_evidence_reference=='synthetic-source-geometry'
    facts['source_content_hash']='b'*64
    with pytest.raises(ValueError,match='geometry_unknown'):presentation_for_style(entry,style,asset,scene_plan=scene,clip=clip)
    with pytest.raises(ValueError, match="portrait_comparison_requires_16_9_source"):
        facts['source_content_hash']='a'*64
        presentation_for_style(entry, style, SimpleNamespace(width=1280,height=800,content_hash='a'*64,metadata=asset.metadata), scene_plan=scene,clip=clip)


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
        assert _choose_candidate(scene,(raw,portrait.model_copy(update={'match_score':.1})),ClipRepository(db),AssetRepository(db))[0].asset_id==source.id
        assert _choose_candidate(scene,(raw,),ClipRepository(db),AssetRepository(db))[0].asset_id==source.id
    finally:db.close()


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
        graphic = scene.model_copy(update={"preferred_sources": [SourceKind.USER_ASSET, SourceKind.TYPOGRAPHY]})
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
        assert "talking_source_not_admitted:hook" in third["stop_reasons"]
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
        assert body["scenes"][0]["planned_source_kind"] == "typography"
        assert body["preliminary_edit_plan"] is not None
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
        assert "talking_reference_consent" in body["authorization_needs"]
        assert body["estimated_user_active_minutes"] is None
        assert client.get(f"/projects/{project_id}/provider-calls").json() == []
    with Database(path) as db:
        assert ProviderCallRepository(db).list_for_project(project_id) == []


def test_preflight_exposes_horizontal_subtitle_and_transform_unknowns(tmp_path: Path) -> None:
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
                transcript=scene.voice_text, visual_description="creator showing a workflow",
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
        assert "voice_consent" in body["authorization_needs"]
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
                transcript=scene.voice_text,
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
                transcript=scene.voice_text,
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
