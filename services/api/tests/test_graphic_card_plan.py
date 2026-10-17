from uuid import UUID

import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient

from app.domain.models import GraphicCardPlan, CandidateAsset, SourceKind, UsageCost, CostCategory
from app.assembly.edit_plan import resolve_edit_plan, EditPlanPreflightError
from app.main import create_app
from test_planning_input import project, scene


POINTS = ["结论", "条件", "证据", "限制"]


def card():
    return GraphicCardPlan(kind="text_card", treatment="key_point", points=POINTS)


@pytest.mark.parametrize("values", [
    {"kind": "text_card", "treatment": "key_point", "points": []},
    {"kind": "text_card", "treatment": "key_point", "points": [" "]},
    {"kind": "text_card", "treatment": "key_point", "points": ["a" * 25]},
    {"kind": "text_card", "treatment": "key_point", "points": ["a" * 24] * 4},
    {"kind": "text_card", "treatment": "key_point", "points": ["a"] * 5},
    {"kind": "text_card", "treatment": "headline", "points": POINTS},
    {"kind": "text_card", "treatment": "contrast", "points": ["one"]},
    {"kind": "text_card", "treatment": "key_point", "points": ["a\nb"]},
    {"kind": "unsupported"},
    {"kind": "unsupported", "reason": "diagram", "points": ["point"]},
    {"kind": "none", "reason": " "},
    {"kind": "none", "reason": "Keep footage", "treatment": "headline"},
])
def test_incomplete_or_over_capacity_cards_are_rejected(values):
    with pytest.raises(ValidationError):
        GraphicCardPlan(**values)


def test_all_declared_points_survive_resolution_and_supplied_plan_cannot_drop_them():
    owner = project()
    value = scene(voice_text="每一刀检查主张是否完整。").model_copy(update={
        "project_id": owner.id, "graphic_plan": card(), "caption_emphasis": ["结论"],
    })
    candidate = CandidateAsset(scene_plan_id=value.id, source_kind=SourceKind.TYPOGRAPHY,
        match_score=0, why=["explicit card"], estimated_cost=UsageCost(category=CostCategory.TYPOGRAPHY))
    selected = {value.id: candidate}
    plan = resolve_edit_plan(owner, [value], selected, clip_intervals={}, supplied=None)
    entry = plan.scenes[0]
    assert entry.graphic_text == entry.fallback.text == "\n".join(POINTS)
    assert entry.graphic_treatment.value == "key_point"
    changed = plan.model_copy(update={"scenes": [entry.model_copy(update={"graphic_text": "结论"})]})
    with pytest.raises(EditPlanPreflightError, match="points_or_treatment_changed"):
        resolve_edit_plan(owner, [value], selected, clip_intervals={}, supplied=changed)


@pytest.mark.parametrize("treatment", ["key_point", "diagram_flow", "diagram_loss", "diagram_factors"])
def test_declared_card_survives_videospec_and_normal_renderer_props(tmp_path,treatment):
    from app.db import Database, AssetRepository, ClipRepository
    from app.assembly import VideoSpecAssembler
    from app.renderer import RemotionRenderer
    from test_remotion_renderer import _Runner, _renderer_project
    owner = project()
    points = POINTS[:2] if treatment == "diagram_loss" else POINTS
    graphic = GraphicCardPlan(kind="diagram" if treatment.startswith("diagram_") else "text_card",treatment=treatment,points=points)
    value = scene(voice_text="每一刀检查主张是否完整。").model_copy(update={
        "project_id": owner.id, "graphic_plan": graphic, "caption_emphasis": ["结论"],
    })
    candidate = CandidateAsset(scene_plan_id=value.id, source_kind=SourceKind.TYPOGRAPHY,
        match_score=0, why=["explicit card"], estimated_cost=UsageCost(category=CostCategory.TYPOGRAPHY))
    with Database(tmp_path / "spec.sqlite") as db:
        assets, clips = AssetRepository(db), ClipRepository(db)
        spec = VideoSpecAssembler(assets, clips).assemble(owner, [value], {value.id: candidate.model_copy(update={"recommended": True})})
        assert spec.scenes[0].graphic_text == "\n".join(points)
        runner = _Runner()
        RemotionRenderer(assets, clips, renderer_dir=_renderer_project(tmp_path / "renderer"), runner=runner).render(
            spec, tmp_path / "card.mp4")
        staged = runner.calls[0][3]["sceneSources"][value.scene_id]
        assert staged["text"] == "\n".join(points) and staged["graphicTreatment"] == treatment


@pytest.mark.parametrize("supported", [True, False])
def test_normal_draft_preflight_exposes_supported_card_or_stops_unsupported_need(tmp_path, supported):
    with TestClient(create_app(tmp_path / "graphic.sqlite")) as client:
        project_id = client.post("/projects", json={"title": "Graphic", "topic": "Interview editing"}).json()["id"]
        graphic = card() if supported else GraphicCardPlan(kind="unsupported", reason="Required animated node links")
        value = scene(voice_text="每一刀检查主张是否完整。").model_copy(update={
            "project_id": UUID(project_id), "graphic_plan": graphic,
            "visual_requirement": "explanatory", "visual_requirement_reason": "Conceptual explanation",
        })
        root = f"/projects/{project_id}"
        saved = client.put(root + "/draft", json={"script": value.voice_text, "topic": "Interview editing",
            "scenes": [value.model_dump(mode="json")]})
        assert saved.status_code == 200, saved.text
        assert saved.json()["scenes"][0]["graphic_plan"] == graphic.model_dump(mode="json")
        response = client.post(root + "/production-preflight", json={})
        assert response.status_code == 200, response.text
        body = response.json()
        if supported:
            assert body["preliminary_edit_plan"]["scenes"][0]["graphic_text"] == "\n".join(POINTS)
        else:
            assert body["preliminary_edit_plan"] is None
            assert "graphic_realization_unsupported:caption-discipline" in body["stop_reasons"]
            assert body["scenes"][0]["suitability"] == "unknown"
        assert "voice_capability_not_verified" in body["stop_reasons"]
        assert body["dispatch_performed"] is False
        assert client.get(root + "/provider-calls").json() == []


def test_no_graphics_survives_media_resolution_and_cannot_be_overridden():
    from uuid import uuid4
    owner = project()
    value = scene(voice_text="Keep the actual demonstration.").model_copy(update={
        "project_id": owner.id, "graphic_plan": GraphicCardPlan(kind="none", reason="Show the evidence")})
    candidate = CandidateAsset(scene_plan_id=value.id, source_kind=SourceKind.USER_ASSET,
        asset_id=uuid4(), clip_id=uuid4(), match_score=1, why=["actual footage"],
        estimated_cost=UsageCost(category=CostCategory.RENDER))
    selected = {value.id: candidate}
    plan = resolve_edit_plan(owner, [value], selected, clip_intervals={}, supplied=None)
    entry = plan.scenes[0]
    assert entry.graphic_text is None and entry.graphic_treatment.value == "none"
    changed = plan.model_copy(update={"scenes": [entry.model_copy(update={"graphic_text": "Decoration", "graphic_treatment": "headline"})]})
    with pytest.raises(EditPlanPreflightError, match="no_graphics_presentation_changed"):
        resolve_edit_plan(owner, [value], selected, clip_intervals={}, supplied=changed)


@pytest.mark.parametrize("requirement", ["creator_speaking", "action_evidence", "explanatory"])
def test_normal_editorial_save_reload_invalidates_old_choices_and_reports_missing_media(tmp_path, requirement):
    from test_project_draft_revisions import _scene, _route
    with TestClient(create_app(tmp_path / "editorial.sqlite")) as client:
        project_id = client.post("/projects", json={"title": requirement, "topic": "Evidence"}).json()["id"]
        value = _scene(UUID(project_id))
        candidate, route = _route(value)
        body = {"script": value.voice_text, "topic": "Evidence", "scenes": [value.model_dump(mode="json")],
            "routes": [route.model_dump(mode="json")], "confirmed": [candidate.model_dump(mode="json")]}
        root = f"/projects/{project_id}"
        initial = client.put(root + "/draft", json=body).json()
        old_preflight = client.post(root + "/production-preflight", json={}).json()
        value = value.model_copy(update={"visual_requirement": requirement,
            "visual_requirement_reason": "Content must preserve its evidence", "evidence_refs": ["source:declared-evidence"],
            "graphic_plan": GraphicCardPlan(kind="none", reason="Preserve the main visual")})
        body["scenes"] = [value.model_dump(mode="json")]
        changed = client.put(root + "/draft", json=body)
        assert changed.status_code == 200, changed.text
        saved = changed.json()
        assert saved["routes"] == saved["confirmed"] == [] and saved["video_spec"] is None
        assert saved["script_revision"] == initial["script_revision"] + 1
        assert saved["scenes"][0] == value.model_dump(mode="json")
        assert client.get(root + "/draft").json()["scenes"] == saved["scenes"]
        preflight = client.post(root + "/production-preflight", json={}).json()
        assert preflight["fingerprint"] != old_preflight["fingerprint"]
        assert preflight["status"] == "blocked" and not preflight["dispatch_performed"]
        assert preflight["preliminary_edit_plan"] is None
        if requirement == "explanatory":
            assert f"no_graphics_requires_media:{value.scene_id}" in preflight["stop_reasons"]
        assert client.get(root + "/provider-calls").json() == []


def test_explicit_no_graphics_rejects_selected_typography():
    owner = project()
    value = scene(voice_text="Original content").model_copy(update={
        "project_id": owner.id, "graphic_plan": GraphicCardPlan(kind="none", reason="Keep footage")})
    candidate = CandidateAsset(scene_plan_id=value.id, source_kind=SourceKind.TYPOGRAPHY,
        match_score=1, why=["legacy fallback"], estimated_cost=UsageCost(category=CostCategory.TYPOGRAPHY))
    with pytest.raises(EditPlanPreflightError, match="no_graphics_requires_media"):
        resolve_edit_plan(owner, [value], {value.id: candidate}, clip_intervals={}, supplied=None)
