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


def test_declared_card_survives_videospec_and_normal_renderer_props(tmp_path):
    from app.db import Database, AssetRepository, ClipRepository
    from app.assembly import VideoSpecAssembler
    from app.renderer import RemotionRenderer
    from test_remotion_renderer import _Runner, _renderer_project
    owner = project()
    value = scene(voice_text="每一刀检查主张是否完整。").model_copy(update={
        "project_id": owner.id, "graphic_plan": card(), "caption_emphasis": ["结论"],
    })
    candidate = CandidateAsset(scene_plan_id=value.id, source_kind=SourceKind.TYPOGRAPHY,
        match_score=0, why=["explicit card"], estimated_cost=UsageCost(category=CostCategory.TYPOGRAPHY))
    with Database(tmp_path / "spec.sqlite") as db:
        assets, clips = AssetRepository(db), ClipRepository(db)
        spec = VideoSpecAssembler(assets, clips).assemble(owner, [value], {value.id: candidate.model_copy(update={"recommended": True})})
        assert spec.scenes[0].graphic_text == "\n".join(POINTS)
        runner = _Runner()
        RemotionRenderer(assets, clips, renderer_dir=_renderer_project(tmp_path / "renderer"), runner=runner).render(
            spec, tmp_path / "card.mp4")
        assert runner.calls[0][3]["sceneSources"][value.scene_id]["text"] == "\n".join(POINTS)


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
