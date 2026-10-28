from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.domain.models import GraphicCardPlan, Project, RationalFps, ScenePlan, SourceKind, VisualIntent
from app.planning_input import (audience_control_leak_field, automatic_graphic_gaps,
    normalize_automatic_graphics, planning_request, presentation_plan_gaps, SEMANTIC_EMPHASIS_RULE)


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


def test_automatic_main_cards_and_paragraph_labels_cannot_masquerade_as_visual_material():
    from app.domain.models import GraphicCardPlan
    from app.planning_input import automatic_graphic_gaps
    text='这段完整旁白不应该全部复制到画面标签'
    base=scene(voice_text=text)
    assert automatic_graphic_gaps([base])==('automatic_graphic_requires_plan:caption-discipline',)
    empty=base.model_copy(update={'graphic_plan':GraphicCardPlan(kind='none',reason='没有图形内容')})
    assert automatic_graphic_gaps([empty])==('automatic_graphic_requires_plan:caption-discipline',)
    card=base.model_copy(update={'graphic_plan':GraphicCardPlan(kind='text_card',treatment='headline',points=['只看码率？'])})
    assert automatic_graphic_gaps([card])==('automatic_text_card_requires_media:caption-discipline',)
    copied=base.model_copy(update={'graphic_plan':GraphicCardPlan(kind='diagram',treatment='diagram_flow',points=[text,'输出'])})
    assert automatic_graphic_gaps([copied])==('diagram_repeats_narration:caption-discipline',)
    factors=base.model_copy(update={'voice_text':'编码器效率、源素材质量、平台压缩。',
        'graphic_plan':GraphicCardPlan(kind='diagram',treatment='diagram_factors',points=['编码器效率','源素材质量','平台压缩'])})
    assert automatic_graphic_gaps([factors])==()


def factor_scene(voice,points,*,treatment='diagram_factors'):
    return scene(voice_text=voice).model_copy(update={
        'graphic_plan':GraphicCardPlan(kind='diagram',treatment=treatment,points=points)})


def test_automatic_factor_labels_follow_explicit_narrated_enumeration_without_rewriting_copy_or_evidence():
    value=factor_scene('码率、编码效率和片源清晰度，三者互相制约。',
        ['码率','编码效率','片源清晰度','平台二次压缩'])
    before=deepcopy(value.model_dump())
    assert automatic_graphic_gaps([value])==('graphic_narration_count_mismatch:caption-discipline',)
    result=normalize_automatic_graphics([value])
    assert isinstance(result,tuple) and len(result)==1
    assert result[0] is not value and result[0].graphic_plan is not value.graphic_plan
    assert result[0].graphic_plan.points==['码率','编码效率','片源清晰度']
    assert result[0].voice_text==value.voice_text and result[0].id==value.id
    assert value.model_dump()==before
    expected=deepcopy(before);expected['graphic_plan']['points']=result[0].graphic_plan.points
    assert result[0].model_dump()==expected
    assert automatic_graphic_gaps(result)==()
    assert normalize_automatic_graphics(result)[0] is result[0]


@pytest.mark.parametrize('voice,points,kept', [
    ('码率和编码效率，两者协同。',['额外因素','编码效率','码率'],['编码效率','码率']),
    ('码率与编码效率，二者协同。',['码率','编码效率','额外因素'],['码率','编码效率']),
])
def test_factor_normalization_keeps_existing_graphic_order_and_supports_explicit_two(voice,points,kept):
    value=factor_scene(voice,points)
    result=normalize_automatic_graphics([value])[0]
    assert result.graphic_plan.points==kept and result.voice_text==voice
    assert automatic_graphic_gaps([result])==()


@pytest.mark.parametrize('voice,points', [
    ('码率、编码效率和片源清晰度，三者互相制约。',['码率','编码效率']),
    ('三者都会影响结果。',['码率','编码效率','片源清晰度','平台二次压缩']),
    ('码率、编码效率和片源清晰度，三者制约，平台二次压缩也影响结果。',
        ['码率','编码效率','片源清晰度','平台二次压缩']),
    ('码率、编码效率和片源清晰度，三者制约；码率和编码效率，两者协同。',
        ['码率','编码效率','片源清晰度','平台二次压缩']),
    ('高码率和编码效率，两者配合。',['码率','高码率','编码效率']),
    ('高码率、编码效率和片源清晰度，三者制约。',['码率','编码效率','片源清晰度','额外因素']),
    ('码率、编码效率和片源清晰度，三者制约。',['码率','编码效率','片源清晰度','码率']),
])
def test_factor_count_ambiguity_or_unmatched_labels_stay_as_specific_gap(voice,points):
    value=factor_scene(voice,points)
    before=deepcopy(value.model_dump())
    assert normalize_automatic_graphics([value])[0] is value
    assert automatic_graphic_gaps([value])==('graphic_narration_count_mismatch:caption-discipline',)
    assert value.model_dump()==before


@pytest.mark.parametrize('value', [
    factor_scene('码率、编码效率和片源清晰度，三者互相制约。',['码率','编码效率','片源清晰度']),
    factor_scene('第三者只是这个故事中的角色。',['人物','故事','背景','关系']),
    factor_scene('编码效率和片源清晰度影响结果。',['码率','编码效率','片源清晰度']),
    factor_scene('三者依次形成这个流程。',['输入','处理','输出','检查'],treatment='diagram_flow'),
    factor_scene('三者互相影响的损失示意。',['保留信息','丢失信息'],treatment='diagram_loss'),
])
def test_qualified_graphics_other_relations_and_noncount_words_remain_unchanged(value):
    before=deepcopy(value.model_dump())
    assert normalize_automatic_graphics([value])[0] is value
    assert automatic_graphic_gaps([value])==()
    assert value.model_dump()==before


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


def test_automatic_production_instructions_are_controls_not_content_evidence():
    value={"continuous_production":{"target":"一次输入完成短视频"},"materials":[]}
    request=planning_request(project(),topic="视频压缩",script=None,context=value)
    assert request["production_controls"]["continuous_production"]==value["continuous_production"]
    assert "continuous_production" not in request["background_evidence"]


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


def test_saved_presentation_style_is_control_only_and_cannot_be_overridden_by_evidence():
    current = project().model_copy(update={"presentation_style": "creator_led"})
    request = planning_request(current, topic=current.topic, script=None,
                               context={"presentation_style": "auto"})
    assert request["production_controls"]["presentation_style"] == "creator_led"
    assert "presentation_style" not in request["audience_brief"]


def creator_led_plan():
    speaking = scene(voice_text="本人解释新文案").model_copy(update={
        "visual_requirement": "creator_speaking", "preferred_sources": [SourceKind.USER_ASSET, SourceKind.AI_VIDEO],
        "duration_target_ms": 5000})
    evidence = scene(voice_text="观察这段真实动作").model_copy(update={
        "visual_requirement": "action_evidence", "preferred_sources": [SourceKind.HISTORICAL_ASSET],
        "duration_target_ms": 2500})
    graphic = scene(voice_text="记住这个要点").model_copy(update={"duration_target_ms": 2500})
    return [speaking, evidence, graphic]


def test_creator_led_requires_visible_speech_without_forcing_action_capture_or_fixed_card_share():
    current = project().model_copy(update={"presentation_style": "creator_led"})
    scenes = creator_led_plan()
    assert presentation_plan_gaps(current, scenes) == ()
    assert presentation_plan_gaps(current, scenes[1:]) == ("creator_led_missing_speaking",)
    generated = scenes[0].model_copy(update={"preferred_sources": [SourceKind.AI_VIDEO]})
    graphic = scenes[-1].model_copy(update={"duration_target_ms": 15000})
    assert presentation_plan_gaps(current, [generated]) == ()
    assert presentation_plan_gaps(current, [generated, graphic]) == ()
    external = scenes[1].model_copy(update={"preferred_sources": [SourceKind.STOCK]})
    assert presentation_plan_gaps(current, [scenes[0], external]) == ()


def test_auto_keeps_graphic_only_and_non_talking_projects_available():
    assert project().presentation_style == "auto"
    assert presentation_plan_gaps(project(), [scene()]) == ()
