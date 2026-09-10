from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.domain.models import Project, ProjectFormat, RationalFps
from scripts.real_sceneplan_assisted_test import _input_hash, _json_from_model, _validate_plan


def _project() -> Project:
    return Project(
        ip_profile_id=uuid4(),
        title="Assisted test",
        topic="A real topic",
        format=ProjectFormat.VERTICAL,
        fps=RationalFps(numerator=30, denominator=1),
        created_at=datetime.now(timezone.utc),
    )


def _scene(scene_id: str, order: int) -> dict[str, object]:
    return {
        "scene_id": scene_id,
        "order": order,
        "purpose": "hook" if order == 0 else "close",
        "voice_text": f"Draft {order}",
        "duration_target_ms": 5_000,
        "visual_intent": {"subject": "creator asset", "action": "shows context", "framing": "medium", "description": "real local media"},
        "preferred_sources": ["user_asset"],
        "fallback_sources": ["typography"],
        "caption_emphasis": ["draft"],
    }


def test_assisted_scene_plan_is_stable_and_evidence_bound() -> None:
    project = _project()
    raw = {"scenes": [_scene("scene_01", 0), _scene("scene_02", 1)]}
    input_hash = _input_hash({"project": project.model_dump(mode="json"), "evidence_refs": ["asset:a"]}, "test-model")
    plans = _validate_plan(raw, project, ["asset:a"], input_hash)
    repeated = _validate_plan(raw, project, ["asset:a"], input_hash)
    assert plans[0].id == repeated[0].id
    assert plans[0].project_id == project.id
    assert plans[0].evidence_refs == ["asset:a"]


def test_assisted_scene_plan_rejects_non_real_preferred_source() -> None:
    project = _project()
    raw = {"scenes": [_scene("scene_01", 0), _scene("scene_02", 1)]}
    raw["scenes"][0]["preferred_sources"] = ["typography"]
    with pytest.raises(ValueError, match="real creator media"):
        _validate_plan(raw, project, ["asset:a"], "a" * 64)


def test_assisted_scene_plan_rejects_extra_model_fields() -> None:
    project = _project()
    scene = _scene("scene_01", 0)
    scene["provider_asset_id"] = "must not pass through"
    with pytest.raises(ValueError, match="strict field set"):
        _validate_plan({"scenes": [scene, _scene("scene_02", 1)]}, project, [], "b" * 64)
