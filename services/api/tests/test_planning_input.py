from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.domain.models import Project, RationalFps, ScenePlan, SourceKind, VisualIntent
from app.planning_input import audience_control_leak_field, planning_request, SEMANTIC_EMPHASIS_RULE


LEAKED_VOICE = "画面文字只承担强调，不整段复读旁白；优先使用已经存在的语义短句，没有就退回正常预检。"


def project():
    return Project(ip_profile_id=uuid4(), title="采访剪辑", topic="如何保住受访者原意",
                   fps=RationalFps(numerator=30, denominator=1), created_at=datetime.now(timezone.utc))


def context():
    return {"planning_preferences": [{"id": "adopted", "version": 2, "rule": SEMANTIC_EMPHASIS_RULE,
            "instruction": "Missing alternatives still use normal preflight.",
            "authority_source": "creator_selection", "evidence_level": "explicit_preference"}],
            "production_feasibility": {"estimated_cost": "unknown"},
            "source_use_constraints": [{"rule": "exclude exact use"}],
            "materials": [{"transcript": "不是制作命令"}], "ip_profile": {"creator_name": "Creator"}}


def scene(**updates):
    return ScenePlan(project_id=uuid4(), scene_id="caption-discipline", order=0, purpose="explain",
        voice_text=updates.get("voice_text", LEAKED_VOICE), duration_target_ms=3000,
        visual_intent=VisualIntent(subject="text"), preferred_sources=[SourceKind.TYPOGRAPHY],
        caption_emphasis=updates.get("caption_emphasis", ["短语义强调"]))


def test_input_projection_separates_control_and_evidence_without_mutation():
    source = context()
    before = deepcopy(source)
    request = planning_request(project(), topic="采访剪辑", script=None, context=source)
    assert source == before
    assert request["audience_brief"] == {"title": "采访剪辑", "topic": "采访剪辑", "user_script": None}
    assert "planning_preferences" not in request["background_evidence"]
    assert request["background_evidence"]["materials"] == source["materials"]
    assert request["production_controls"]["source_use_constraints"] == source["source_use_constraints"]
    preference = request["production_controls"]["planning_preferences"][0]
    assert "instruction" not in preference
    assert "preflight" not in preference["style"]
    assert preference["id"] == "adopted" and preference["version"] == 2


@pytest.mark.parametrize("updates,field", [
    ({}, "voice_text"),
    ({"voice_text": "语境不能剪断。", "caption_emphasis": ["缺短句→预检"]}, "caption_emphasis"),
])
def test_retained_leak_is_rejected_not_silently_rewritten(updates, field):
    value = scene(**updates)
    before = value.model_dump()
    assert audience_control_leak_field([value], topic=project().topic, script=None, context=context()) == field
    assert value.model_dump() == before


@pytest.mark.parametrize("topic,script,voice", [
    ("Content OS 的正常预检流程", None, LEAKED_VOICE),
    ("Content OS preflight tutorial", None, LEAKED_VOICE),
    ("制作流程说明", LEAKED_VOICE, LEAKED_VOICE),
    ("汽车预检", None, "预检是出车前的重要步骤。"),
    ("采访剪辑", None, "先保留原意，再缩短时长。"),
])
def test_legitimate_content_and_explicit_product_tutorial_are_allowed(topic, script, voice):
    assert audience_control_leak_field([scene(voice_text=voice)], topic=topic, script=script, context=context()) is None


def test_metadata_does_not_authorize_internal_workflow_content():
    source = context()
    source["materials"][0]["transcript"] = LEAKED_VOICE
    source["ip_profile"]["creator_name"] = "Content OS 预检"
    assert audience_control_leak_field([scene()], topic="采访剪辑", script=None, context=source) == "voice_text"
    assert audience_control_leak_field([scene()], topic="采访剪辑", script=None, context={}) is None
