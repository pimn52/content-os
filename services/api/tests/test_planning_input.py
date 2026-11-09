from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.domain.models import CreatorFit, GraphicCardPlan, IPProfile, Project, RationalFps, ScenePlan, SourceKind, VisualIntent
from app.planning_input import (audience_control_leak_field, automatic_graphic_gaps,
    automatic_identity_anchor_required, creator_planning_brief, creator_scope_gaps,
    normalize_automatic_graphics, normalize_creator_fit,
    normalize_explicit_visual_constraints, planning_request, presentation_plan_gaps, SEMANTIC_EMPHASIS_RULE)


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


def rich_profile():
    return IPProfile(
        creator_name="Creator",
        positioning="面向独立创作者的视频生产研究，强调可验证的方法和适用边界。",
        content_pillars=["短视频生产方法论", "平台分发机制"],
        domains=["video research", "creator workflow"],
        audience="独立视频创作者",
        topics=["平台压缩与画质", "采访剪辑", "视频生产流程"],
        knowledge=["平台压缩会损失细节，不同片源受损程度不同。", "采访剪辑要先保留受访者原意。", "无关的烹饪知识。"],
        opinions=["画质问题不能只归因于码率。", "剪辑取舍要保留上下文。", "没有证据时不要冒充亲历。"],
        vocabulary=["片源", "码率", "结论先行"],
        style_notes=["先讲结论，再给边界"],
        boundaries=["不虚构亲历"],
        avoided_expressions=["绝对保证"],
        common_hooks=["先别急着怪平台"],
        metadata={"private_contact": "should-not-leak@example.com"},
    )


def test_creator_brief_uses_only_topic_relevant_bounded_profile_fields():
    brief = creator_planning_brief(
        rich_profile(),
        topic="平台压缩如何影响视频画质",
        script="解释片源和压缩的边界。",
    )
    assert brief["profile_state"] == "structured"
    assert brief["selection_policy"] == "topic_relevant_bounded_creator_brief_v1"
    assert "平台压缩会损失细节" in brief["knowledge"][0]
    assert "无关的烹饪知识" not in brief["knowledge"]
    assert "画质问题不能只归因于码率。" in brief["opinions"]
    assert brief["positioning"].startswith("面向独立创作者")
    assert "private_contact" not in brief
    assert "should-not-leak" not in str(brief)
    assert "视频生产流程" not in brief["topics"]
    assert brief["omitted_counts"]["topics"] >= 1


def test_sparse_creator_brief_is_explicit_topical_fallback():
    brief = creator_planning_brief(
        IPProfile(creator_name="Sparse", domains=["video research"], topics=["content workflow"]),
        topic="采访剪辑",
    )
    assert brief["profile_state"] == "topical_only"
    assert brief["positioning"] is None
    assert brief["knowledge"] == [] and brief["opinions"] == []


def test_explicit_no_creator_and_local_graphics_only_are_enforced_without_rewriting_copy():
    value = ScenePlan(
        project_id=uuid4(), scene_id="opening", order=0, purpose="提出问题",
        voice_text="斯多葛为什么既吸引人，也可能变成自我压抑？",
        duration_target_ms=4_000, visual_intent=VisualIntent(subject="本人", action="面对镜头"),
        visual_requirement="creator_speaking", visual_requirement_reason="原计划本人出镜",
        preferred_sources=[SourceKind.AI_VIDEO, SourceKind.TALKING_PROFILE],
        fallback_sources=[SourceKind.HISTORICAL_ASSET],
        caption_emphasis=["吸引力还是压抑？"], viewer_takeaway="先提出双重问题",
    )
    topic = "不要本人出镜，不使用任何外部实拍或 AI 画面，全片只用本地图解和文字要点"
    result = normalize_explicit_visual_constraints([value], topic=topic, script=None)
    assert len(result) == 1
    scene = result[0]
    assert scene.voice_text == value.voice_text
    assert scene.visual_requirement == "explanatory"
    assert scene.preferred_sources == [SourceKind.TYPOGRAPHY]
    assert scene.fallback_sources == []
    assert scene.graphic_plan is not None and scene.graphic_plan.kind == "text_card"
    assert scene.graphic_plan.treatment == "headline"
    assert scene.graphic_plan.points == ["吸引力还是压抑"]
    assert all(len(point) <= 24 for point in scene.graphic_plan.points)
    assert sum(map(len, scene.graphic_plan.points)) <= 72
    assert "不使用本人出镜" in (scene.visual_requirement_reason or "")


def test_explicit_local_only_turns_unsupported_opening_into_a_renderable_text_card():
    from app.domain.models import GraphicCardPlan
    value = ScenePlan(
        project_id=uuid4(), scene_id="opening", order=0, purpose="提出问题",
        voice_text="为什么两千年前的斯多葛主义今天仍能安抚现代人？",
        duration_target_ms=4_000,
        visual_intent=VisualIntent(subject="问题标题", action="本地排版呈现问题"),
        visual_requirement="explanatory",
        visual_requirement_reason="用户明确要求全片只用本地文字卡与图解",
        preferred_sources=[SourceKind.TYPOGRAPHY],
        caption_emphasis=["斯多葛为何让现代人安定？"],
        graphic_plan=GraphicCardPlan(
            kind="unsupported", reason="开场只需要标题，不需要虚构因果图。",
        ),
    )
    topic = "不要本人出镜，不使用任何外部实拍或 AI 画面，全片只用本地流程图解和文字要点"
    result = normalize_explicit_visual_constraints([value], topic=topic, script=None)
    assert len(result) == 1 and result[0].voice_text == value.voice_text
    graphic = result[0].graphic_plan
    assert graphic is not None
    assert graphic.kind == "text_card" and graphic.treatment == "headline"
    assert graphic.points == ["斯多葛为何让现代人安定"]
    assert automatic_graphic_gaps(result) == ("automatic_text_card_requires_media:opening",)
    assert automatic_graphic_gaps(result, allow_typography_text_card=True) == ()


def test_explicit_no_creator_disables_automatic_identity_anchor():
    executable = {
        "production_feasibility": {
            "creator_options": {"voice": {"default_binding": "matched"}},
            "selected_talking_sources": [{"clip_id": "clip"}],
            "talking_motion_pool": {"state": "available"},
        },
    }
    value = project()
    assert automatic_identity_anchor_required(value, executable, topic="正常主题")
    assert not automatic_identity_anchor_required(
        value, executable, topic="不要本人出镜，全片只使用本地图解",
    )


def test_same_creator_uses_stable_identity_but_different_topic_evidence_for_two_topics():
    profile = rich_profile()
    compression = creator_planning_brief(profile, topic="平台压缩如何影响视频画质")
    interview = creator_planning_brief(profile, topic="采访剪辑如何保留受访者原意")
    assert compression["positioning"] == interview["positioning"]
    assert compression["boundaries"] == interview["boundaries"]
    assert any("平台压缩" in value for value in compression["knowledge"])
    assert not any("采访剪辑" in value for value in compression["knowledge"])
    assert any("采访剪辑" in value for value in interview["knowledge"])
    assert not any("平台压缩" in value for value in interview["knowledge"])


def test_creator_fit_references_are_filtered_and_missing_fit_is_visible():
    brief = creator_planning_brief(rich_profile(), topic="平台压缩如何影响视频画质")
    unknown = scene().model_copy(update={"creator_fit": CreatorFit(
        fit_state="matched",
        content_pillar="不存在的支柱",
        topic_fit_reason="topic",
        audience_value="value",
        knowledge_refs=["不存在的知识"],
        opinion_refs=["画质问题不能只归因于码率。"],
    )})
    missing = scene()
    normalized = normalize_creator_fit([unknown, missing], {"ip_profile": brief})
    assert normalized[0].creator_fit.fit_state == "partial"
    assert normalized[0].creator_fit.content_pillar is None
    assert normalized[0].creator_fit.knowledge_refs == []
    assert normalized[0].creator_fit.opinion_refs == ["画质问题不能只归因于码率。"]
    assert any("未出现在本次 Creator Brief" in value for value in normalized[0].creator_fit.deviation_risks)
    assert normalized[1].creator_fit.fit_state == "generic"
    assert normalized[1].creator_fit.evidence_gaps


def test_sparse_profile_never_claims_full_creator_match():
    brief = creator_planning_brief(
        IPProfile(creator_name="Sparse", domains=["video research"], topics=["content workflow"]),
        topic="采访剪辑",
    )
    claimed = scene().model_copy(update={"creator_fit": CreatorFit(
        fit_state="matched",
        content_pillar="短视频生产方法论",
        topic_fit_reason="看起来适合",
        audience_value="受众",
    )})
    normalized = normalize_creator_fit([claimed], {"ip_profile": brief})
    assert normalized[0].creator_fit.fit_state == "partial"
    assert normalized[0].creator_fit.content_pillar is None
    assert any("缺少明确知识与观点" in value for value in normalized[0].creator_fit.evidence_gaps)


def test_structured_creator_scope_rejects_uncredited_scene_topics():
    brief = creator_planning_brief(rich_profile(), topic="平台压缩如何影响视频画质")
    fit = CreatorFit(
        fit_state="matched",
        content_pillar="短视频生产方法论",
        topic_fit_reason="画质解释属于生产方法。",
        audience_value="帮助创作者判断画质。",
        opinion_refs=["画质问题不能只归因于码率。"],
    )
    credited = scene().model_copy(update={"creator_fit": fit})
    assert creator_scope_gaps([credited], {"ip_profile": brief}) == ()
    assert creator_scope_gaps([scene()], {"ip_profile": brief}) == ("caption-discipline",)
    sparse = creator_planning_brief(IPProfile(creator_name="Sparse", topics=["内容流程"]), topic="采访剪辑")
    assert creator_scope_gaps([scene()], {"ip_profile": sparse}) == ()


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


def test_automatic_ip_bound_production_requires_visible_creator_when_talking_is_executable():
    current = project()
    source = context()
    source["production_feasibility"] = {
        "creator_options": {"voice": {"default_binding": "matched"}},
        "selected_talking_sources": [{"clip_id": "clip", "profile_id": "profile"}],
        "talking_motion_pool": {"state": "available"},
    }
    request = planning_request(current, topic=current.topic, script=None, context=source)
    assert request["production_controls"]["presentation_style"] == "creator_led"
    assert request["production_controls"]["identity_presence_required"] is True

    speaking = creator_led_plan()[0]
    assert presentation_plan_gaps(current, [speaking], identity_anchor_required=True) == ()
    assert presentation_plan_gaps(current, [scene()], identity_anchor_required=True) == (
        "automatic_identity_presence_missing",
    )
    source["production_feasibility"]["talking_motion_pool"] = {"state": "unavailable"}
    unavailable = planning_request(current, topic=current.topic, script=None, context=source)
    assert unavailable["production_controls"]["presentation_style"] == "auto"
    assert unavailable["production_controls"]["identity_presence_required"] is False


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
