from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterator
from uuid import uuid4

import pytest

from app.domain.models import Project, RationalFps
from app.providers.scene_planner import (
    OpenAICompatibleScenePlanner,
    ScenePlanResult,
    ScenePlannerAuthenticationError,
    ScenePlannerConfigurationError,
    ScenePlannerConnectionError,
    ScenePlannerHTTPError,
    ScenePlannerInputError,
    ScenePlannerProviderResponseError,
    ScenePlannerHTTPResponse,
    ScenePlannerRateLimitError,
    ScenePlannerTimeout,
)


def test_short_motion_reference_does_not_limit_complete_narration_or_trigger_retry():
    from app.providers.scene_planner import ScenePlannerHTTPResponse
    requests=[]
    class Transport:
        def post(self,url,headers,body,timeout_seconds):
            requests.append(json.loads(body))
            return ScenePlannerHTTPResponse(200,json.dumps(_chat_response([_scene("opening",0)])).encode(),{})
    motion={"source_duration_ms":3200,"planned_visible_ms_max":2400,"approx_chinese_characters_max":7}
    OpenAICompatibleScenePlanner("fixture",protocol="chat_completions",transport=Transport()).plan(
        _project(),context={"continuous_production":{"talking_motion_budget":motion}})
    assert len(requests)==1
    prompt=requests[0]["messages"][0]["content"]
    assert "不限制文案字数" in prompt
    assert "真实时长调度多条人物参考" in prompt
    assert "合计不得超过 planned_visible_ms_max" not in prompt


@pytest.mark.parametrize("violation",[None,"long_text","long_duration","second_speaking","missing_slot"])
def test_motion_capacity_is_a_scheduling_constraint_and_never_rejects_complete_copy(violation):
    from app.providers.scene_planner import ScenePlannerHTTPResponse
    requests=[]
    creator={**_scene("creator",0,preferred=["ai_video"]),"fallback_sources":[],"voice_text":"先看这组对比",
        "duration_target_ms":2000,"visual_requirement":"creator_speaking","visual_requirement_reason":"本人引出对比","graphic_plan":None}
    support={**_scene("evidence",1,preferred=["user_asset"]),"fallback_sources":[],"voice_text":"相同原片在不同编码和码率下，细节的保留程度不同。",
        "visual_requirement":"action_evidence","visual_requirement_reason":"展示真实对比","graphic_plan":None}
    if violation=="long_text":creator["voice_text"]="视频码率越高画面就一定越清晰吗"
    if violation=="long_duration":creator["duration_target_ms"]=2500
    if violation=="second_speaking":
        support["visual_requirement"]="creator_speaking"
        support["preferred_sources"]=["ai_video"]
    document={"creator_scene":creator,"scenes":[support]}
    if violation=="missing_slot":document={"scenes":[creator,support]}
    class Transport:
        def post(self,url,headers,body,timeout_seconds):
            requests.append(json.loads(body))
            return ScenePlannerHTTPResponse(200,json.dumps({"choices":[{"message":{"content":json.dumps(document)}}]}).encode(),{})
    planner=OpenAICompatibleScenePlanner("fixture",protocol="chat_completions",chat_output_mode="json_schema",transport=Transport())
    project=_project().model_copy(update={"presentation_style":"creator_led"})
    context={"continuous_production":{"talking_motion_budget":{"source_duration_ms":3200,"planned_visible_ms_max":2400,"approx_chinese_characters_max":7}}}
    result=planner.plan(project,context=context)
    assert [scene.voice_text for scene in result.scenes]==[creator["voice_text"],support["voice_text"]]
    assert len(requests)==1
    schema=requests[0]["response_format"]["json_schema"]["schema"]
    assert 'creator_scene' not in schema['properties']
    assert schema['properties']['scenes']['items']['properties']['voice_text']['maxLength']>7


def _project() -> Project:
    return Project(
        ip_profile_id=uuid4(), title="Creator's practical coding advice", topic="When Vibe Coding is the wrong choice",
        fps=RationalFps(numerator=30, denominator=1), created_at=datetime.now(timezone.utc),
    )


def test_short_reference_supporting_scene_selects_a_supplied_clip_instead_of_invented_media():
    from app.providers.scene_planner import ScenePlannerHTTPResponse
    clip_id=str(uuid4());requests=[]
    creator={**_scene("creator",0,preferred=["ai_video"]),"fallback_sources":[],"voice_text":"看这组对比",
        "duration_target_ms":1800,"visual_requirement":"creator_speaking","visual_requirement_reason":"本人引出","graphic_plan":None}
    support={**_scene("evidence",1,preferred=["user_asset"]),"fallback_sources":[],"voice_text":"比较同一原片编码后的差异。",
        "visual_requirement":"action_evidence","visual_requirement_reason":"使用已提供的对比原片","graphic_plan":None,"preferred_clip_id":clip_id}
    class Transport:
        def post(self,url,headers,body,timeout_seconds):
            requests.append(json.loads(body))
            return ScenePlannerHTTPResponse(200,json.dumps({"choices":[{"message":{"content":json.dumps({"creator_scene":creator,"scenes":[support]})}}]}).encode(),{})
    context={"continuous_production":{"talking_motion_budget":{"planned_visible_ms_max":2400,"approx_chinese_characters_max":7}},
        "materials":[{"clip_id":clip_id,"source_kind":"user_asset","summary":"同原片高低码率对比"}]}
    result=OpenAICompatibleScenePlanner("fixture",protocol="chat_completions",chat_output_mode="json_schema",transport=Transport()).plan(
        _project().model_copy(update={"presentation_style":"creator_led"}),context=context)
    assert str(result.scenes[1].preferred_clip_id)==clip_id
    schema=requests[0]["response_format"]["json_schema"]["schema"]["properties"]["scenes"]["items"]
    graphics=schema['properties']['graphic_plan']['anyOf']
    assert not any(v.get('properties',{}).get('kind',{}).get('enum')==['text_card'] for v in graphics)
    assert result.scenes[1].graphic_plan is None
    assert 'creator_scene' not in requests[0]['response_format']['json_schema']['schema']['properties']


@pytest.mark.parametrize('coverage,chosen,duration,accepted',[
    ('video_interval_analysis',0,3297,True),('video_interval_analysis',0,3700,False),
    ('video_interval_analysis',1,3700,True),('sampled_keyframes',0,3700,True),
])
def test_observed_source_limits_each_clip_and_preserves_full_narration(coverage,chosen,duration,accepted):
    clips=[str(uuid4()),str(uuid4())];requests=[]
    creator={**_scene('creator',0,preferred=['ai_video']),'fallback_sources':[],'voice_text':'看这组对比',
        'duration_target_ms':1800,'visual_requirement':'creator_speaking','visual_requirement_reason':'本人引出','graphic_plan':None}
    narration='所以别盲目拉高码率，先看内容本身。'
    support={**_scene('closing',1,preferred=['user_asset']),'fallback_sources':[],'voice_text':narration,
        'duration_target_ms':duration,'visual_requirement':'unknown','visual_requirement_reason':'已有画面承载观看建议',
        'graphic_plan':None,'preferred_clip_id':clips[chosen]}
    class Transport:
        def post(self,url,headers,body,timeout_seconds):
            requests.append(json.loads(body))
            return ScenePlannerHTTPResponse(200,json.dumps({'choices':[{'message':{'content':json.dumps({'creator_scene':creator,'scenes':[support]})}}]}).encode(),{})
    context={'continuous_production':{'talking_motion_budget':{'planned_visible_ms_max':2400,'approx_chinese_characters_max':7}},
        'materials':[{'clip_id':identity,'source_kind':'user_asset','visual_evidence':{'source_hash_bound':True,
            'coverage':coverage,'usable_interval':{'start_ms':10000,'end_ms':10000+span,'duration_ms':span}}}
            for identity,span in zip(clips,[3297,6000])]}
    planner=OpenAICompatibleScenePlanner('fixture',protocol='chat_completions',chat_output_mode='json_schema',transport=Transport())
    project=_project().model_copy(update={'presentation_style':'creator_led'})
    if accepted:
        result=planner.plan(project,context=context)
        assert result.scenes[1].voice_text==narration and str(result.scenes[1].preferred_clip_id)==clips[chosen]
    else:
        with pytest.raises(ScenePlannerProviderResponseError) as error:planner.plan(project,context=context)
        assert error.value.domain_field=='duration_target_ms'
    assert len(requests)==1
    assert 'creator_scene' not in requests[0]['response_format']['json_schema']['schema']['properties']


@pytest.mark.parametrize('kind,accepted',[('ai_video',True),('user_asset',False)])
def test_empty_prepared_footage_can_use_speaking_capacity_without_inventing_a_clip(kind,accepted):
    requests=[]
    scene={**_scene('advice',0,preferred=[kind]),'fallback_sources':[],'voice_text':'先确定需求，再用代码验证想法。',
        'duration_target_ms':6000,'visual_requirement':'creator_speaking' if kind=='ai_video' else 'unknown',
        'visual_requirement_reason':'本人讲解已有经验','graphic_plan':None,'preferred_clip_id':None}
    class Transport:
        def post(self,url,headers,body,timeout_seconds):
            requests.append(json.loads(body))
            return ScenePlannerHTTPResponse(200,json.dumps(_chat_response([scene])).encode(),{})
    context={'materials':[],'continuous_production':{'talking_motion_budget':{'planned_visible_ms_max':24000,
        'approx_chinese_characters_max':72}}}
    planner=OpenAICompatibleScenePlanner('fixture',protocol='chat_completions',transport=Transport())
    project=_project().model_copy(update={'presentation_style':'creator_led'})
    if accepted:
        result=planner.plan(project,context=context)
        assert result.scenes[0].voice_text==scene['voice_text'] and result.scenes[0].visual_requirement=='creator_speaking'
    else:
        with pytest.raises(ScenePlannerProviderResponseError) as error:planner.plan(project,context=context)
        assert error.value.domain_field=='preferred_clip_id'
    assert len(requests)==1


def test_short_creator_reference_preserves_complete_opening_before_later_visible_speech():
    requests=[]
    opening={**_scene('opening',0,preferred=['user_asset']),'voice_text':'视频码率越高，画面就越清晰吗？',
        'fallback_sources':[],'graphic_plan':None,'visual_requirement':'unknown','visual_requirement_reason':None}
    creator={**_scene('creator',1,preferred=['ai_video']),'voice_text':'并不一定','duration_target_ms':1800,
        'fallback_sources':[],'graphic_plan':None,'visual_requirement':'creator_speaking','visual_requirement_reason':'本人作出简短判断'}
    class Transport:
        def post(self,url,headers,body,timeout):
            requests.append(json.loads(body))
            return ScenePlannerHTTPResponse(200,json.dumps({'choices':[{'message':{'content':json.dumps(
                {'creator_scene':creator,'scenes':[opening]})}}]}).encode(),{})
    result=OpenAICompatibleScenePlanner('fixture',protocol='chat_completions',transport=Transport()).plan(
        _project().model_copy(update={'presentation_style':'creator_led'}),topic='视频码率为什么不是越高越清晰？',
        context={'continuous_production':{'talking_motion_budget':{'planned_visible_ms_max':2400,'approx_chinese_characters_max':7}}})
    assert [s.voice_text for s in result.scenes]==[opening['voice_text'],creator['voice_text']]
    prompt=requests[0]['messages'][0]['content']
    assert '开场必须独立交代主体' in prompt and '不限制文案字数' in prompt


@pytest.mark.parametrize('copy,supplied,accepted',[
    ('越高越清晰吗',False,False),('越高就越清晰吗',False,False),
    ('码率越高越清晰吗？',False,True),('先说视频码率。',False,True),
    ('越高越清晰吗',True,True)])
def test_generated_opening_cannot_start_with_a_subjectless_comparison(copy,supplied,accepted):
    calls=[]
    class Transport:
        def post(self,*args):
            calls.append(1)
            return ScenePlannerHTTPResponse(200,json.dumps(_chat_response([{**_scene('opening',0),'voice_text':copy}])).encode(),{})
    planner=OpenAICompatibleScenePlanner('fixture',protocol='chat_completions',transport=Transport())
    if accepted:
        assert planner.plan(_project(),topic='视频码率',script=copy if supplied else None).scenes[0].voice_text==copy
    else:
        with pytest.raises(ScenePlannerProviderResponseError) as error:planner.plan(_project(),topic='视频码率')
        assert error.value.domain_rule=='opening_context' and error.value.domain_field=='voice_text'
    assert calls==[1]


def _scene(scene_id: str, order: int, *, preferred: list[str] | None = None, fallback: list[str] | None = None) -> dict[str, object]:
    return {
        "scene_id": scene_id,
        "order": order,
        "purpose": "hook" if order == 0 else "explain",
        "voice_text": "Most people misunderstand this tradeoff." if order == 0 else "Start with the problem you can verify.",
        "duration_target_ms": 3500,
        "visual_intent": {
            "subject": "creator", "action": "working at a laptop", "framing": "close", "description": "A practical desk setup.",
        },
        "preferred_sources": preferred or ["user_asset", "historical_asset"],
        "fallback_sources": fallback or ["typography", "stock"],
        "caption_emphasis": ["tradeoff"],
    }


def _response(scenes: list[dict[str, object]]) -> dict[str, object]:
    return {"output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps({"scenes": scenes})}]}]}


def _chat_response(scenes: list[dict[str, object]]) -> dict[str, object]:
    return {"choices": [{"message": {"content": json.dumps({"scenes": scenes})}}]}


@pytest.mark.parametrize('acquisition', ['bounded_search_import_then_interval_qa', 'manual_search_import'])
def test_automatic_planner_requests_only_available_generation_and_reports_stock_connection(acquisition):
    requests=[]
    scene={**_scene('opening',0,preferred=['stock'],fallback=['stock']), 'graphic_plan':None,
        'visual_requirement':'unknown','visual_requirement_reason':None,
        'material_request':{'source_kind':'stock','query':'camera settings','reason':'展示设置'}}
    class Transport:
        def post(self,url,headers,body,timeout):
            requests.append(json.loads(body))
            return ScenePlannerHTTPResponse(200,json.dumps(_chat_response([scene])).encode(),{})
    context={'continuous_production':{'target':'短视频'},'material_supply':{
        'ai_broll_generation':'not_configured','external_acquisition':acquisition}}
    result=OpenAICompatibleScenePlanner('fixture',protocol='chat_completions',chat_output_mode='json_schema',
        transport=Transport()).plan(_project(),context=context)
    assert result.scenes[0].material_request.source_kind=='stock'
    body=requests[0]; prompt=body['messages'][0]['content']
    schema=body['response_format']['json_schema']['schema']['properties']['scenes']['items']['properties']
    assert schema['material_request']['anyOf'][1]['properties']['source_kind']['enum']==['stock']
    assert 'ai_broll' in schema['preferred_sources']['items']['enum']
    assert '未配置 AI 补充画面生成' in prompt
    assert ('已配置外部素材获取' in prompt)==(acquisition=='bounded_search_import_then_interval_qa')
    assert ('当前未配置自动外部素材获取' in prompt)==(acquisition=='manual_search_import')


@pytest.mark.parametrize('request_kind,clip_kind,accepted', [('ai_broll',None,False),('stock',None,True),(None,'ai_broll',True)])
def test_automatic_material_contract_rejects_only_new_unavailable_generation(request_kind,clip_kind,accepted):
    clip_id=str(uuid4()) if clip_kind else None
    scene={**_scene('opening',0,preferred=[clip_kind or request_kind],fallback=['stock']), 'graphic_plan':None,
        'visual_requirement':'unknown','visual_requirement_reason':None,
        'preferred_clip_id':clip_id,'material_request':({'source_kind':request_kind,'query':'camera settings',
        'reason':'展示设置'} if request_kind else None)}
    class Transport:
        def post(self,*args):
            return ScenePlannerHTTPResponse(200,json.dumps(_chat_response([scene])).encode(),{})
    planner=OpenAICompatibleScenePlanner('fixture',protocol='chat_completions',transport=Transport())
    context={'continuous_production':{'target':'短视频'},'material_supply':{'ai_broll_generation':'not_configured'}}
    if clip_id:
        context['materials']=[{'clip_id':clip_id,'source_kind':'ai_broll','summary':'已入库的 AI 画面'}]
    if accepted:
        result=planner.plan(_project(),context=context)
        assert str(result.scenes[0].preferred_clip_id)==clip_id if clip_id else result.scenes[0].material_request.source_kind=='stock'
    else:
        with pytest.raises(ScenePlannerProviderResponseError) as error:
            planner.plan(_project(),context=context)
        assert error.value.domain_field=='material_request'
        assert 'no AI B-roll generation provider configured' in str(error.value)
        assert error.value.response_document is not None
    # 手工规划仍可将生成需求保留为待解决的缺口。
    if request_kind=='ai_broll':
        assert planner.plan(_project()).scenes[0].material_request.source_kind=='ai_broll'


@pytest.mark.parametrize("treatment,points", [
    ("diagram_flow", ["原始画面", "编码", "输出画面"]),
    ("diagram_loss", ["已有细节", "丢失细节"]),
    ("diagram_factors", ["编码方式", "分辨率", "画面内容"]),
])
def test_normal_planner_schema_and_output_can_reach_supported_information_diagrams(treatment, points):
    from app.providers.scene_planner import ScenePlannerHTTPResponse
    from app.assembly.edit_plan import resolve_edit_plan
    from app.domain.models import CandidateAsset, CostCategory, SourceKind, UsageCost
    requests=[]
    scene={**_scene("explain",0,preferred=["typography"]), "fallback_sources":[],
        "visual_requirement":"explanatory", "visual_requirement_reason":"解释信息之间的关系",
        "graphic_plan":{"kind":"diagram","treatment":treatment,"points":points,"reason":None}}
    class Transport:
        def post(self, url, headers, body, timeout_seconds):
            requests.append(json.loads(body))
            return ScenePlannerHTTPResponse(200,json.dumps(_chat_response([scene])).encode(),{})
    project=_project()
    result=OpenAICompatibleScenePlanner("fixture-key",protocol="chat_completions",chat_output_mode="json_schema",transport=Transport()).plan(project)
    variants=requests[0]["response_format"]["json_schema"]["schema"]["properties"]["scenes"]["items"]["properties"]["graphic_plan"]["anyOf"]
    schema=next(v for v in variants if treatment in v.get("properties",{}).get("treatment",{}).get("enum",[]))
    assert "diagram" in schema["properties"]["kind"]["enum"]
    assert treatment in schema["properties"]["treatment"]["enum"]
    assert schema["properties"]["reason"]=={"type":"null"}
    assert schema["properties"]["points"]["minItems"]==2
    assert schema["properties"]["points"]["maxItems"]==(2 if treatment=="diagram_loss" else 4)
    planned=result.scenes[0]
    assert planned.graphic_plan.kind == "diagram"
    candidate=CandidateAsset(scene_plan_id=planned.id,source_kind=SourceKind.TYPOGRAPHY,
        match_score=1,why=["diagram"],recommended=True,estimated_cost=UsageCost(category=CostCategory.TYPOGRAPHY))
    edit=resolve_edit_plan(project,[planned],{planned.id:candidate},clip_intervals={},supplied=None)
    assert edit.scenes[0].graphic_treatment.value == treatment
    assert edit.scenes[0].graphic_text == "\n".join(points)


@pytest.mark.parametrize("points,rule", [
    (["长" * 24, "长" * 24, "长" * 23, "短"], None),
    (["长" * 25, "短"], "graphic_label_length"),
    (["长" * 24] * 4, "graphic_total_length"),
    (["只有一个节点"], "graphic_point_count"),
])
def test_graphic_label_contract_preserves_uneven_valid_labels_and_reports_exact_failure(points, rule):
    from app.providers.scene_planner import ScenePlannerHTTPResponse
    requests = []
    value = {**_scene("graphic", 0, preferred=["typography"]), "fallback_sources": [],
        "visual_requirement": "explanatory", "visual_requirement_reason": "表达节点之间的关系",
        "graphic_plan": {"kind": "diagram", "treatment": "diagram_flow", "points": points, "reason": None}}
    class Transport:
        def post(self, url, headers, body, timeout_seconds):
            requests.append(json.loads(body))
            return ScenePlannerHTTPResponse(200, json.dumps(_chat_response([value])).encode(), {})
    planner = OpenAICompatibleScenePlanner("fixture-key", protocol="chat_completions", chat_output_mode="json_schema", transport=Transport())
    if rule is None:
        assert planner.plan(_project()).scenes[0].graphic_plan.points == points
        variants = requests[0]["response_format"]["json_schema"]["schema"]["properties"]["scenes"]["items"]["properties"]["graphic_plan"]
        branch = next(branch for branch in variants["anyOf"] if "diagram_flow" in branch.get("properties", {}).get("treatment", {}).get("enum", []))
        assert all(len(point) <= branch["properties"]["points"]["items"]["maxLength"] for point in points)
    else:
        with pytest.raises(ScenePlannerProviderResponseError) as raised:
            planner.plan(_project())
        assert raised.value.domain_field == "graphic_plan" and raised.value.domain_rule == rule
        assert raised.value.graphic_diagnostic["point_total_chars"] == sum(map(len, points))
    assert len(requests) == 1


@pytest.mark.parametrize("changed", [False, True])
def test_supplied_copy_must_survive_scene_planning_without_retry(changed):
    from app.providers.scene_planner import ScenePlannerHTTPResponse
    calls = []
    scene = {**_scene("speech", 0), "voice_text": "Rewritten speech." if changed else "Exact supplied speech."}

    class Transport:
        def post(self, *args):
            calls.append(args)
            return ScenePlannerHTTPResponse(200, json.dumps(_chat_response([scene])).encode(), {})

    planner = OpenAICompatibleScenePlanner("fixture-key", protocol="chat_completions", transport=Transport())
    if changed:
        with pytest.raises(ScenePlannerProviderResponseError) as failure:
            planner.plan(_project(), script="Exact supplied speech.")
        assert failure.value.domain_rule == "script_copy_mismatch"
    else:
        assert planner.plan(_project(), script="Exact supplied speech.").scenes[0].voice_text == "Exact supplied speech."
    assert len(calls) == 1


@pytest.mark.parametrize("variant,accepted,retained", [
    ("missing_source", True, False),
    ("permitted_stock", True, True),
    ("non_creator", False, False),
    ("invalid_alternative", False, False),
    ("empty_alternative", False, False),
    ("invalid_fallback", False, False),
    ("unsupported_fallback", False, False),
])
def test_incomplete_creator_cutaway_does_not_invent_permission(variant, accepted, retained):
    alternative = {"subject": "手机画面", "action": "展示清晰度", "framing": "特写", "description": "切题实拍"}
    scene = {**_scene("creator", 0, preferred=["talking_profile", "ai_video"]),
        "fallback_sources": [], "visual_requirement": "creator_speaking",
        "visual_requirement_reason": "本人必须面对镜头说明观点",
        "supporting_visual_intent": alternative, "graphic_plan": None}
    if variant == "permitted_stock":
        scene["fallback_sources"] = ["stock"]
    elif variant == "non_creator":
        scene["visual_requirement"] = "action_evidence"
    elif variant == "invalid_alternative":
        scene["supporting_visual_intent"] = {**alternative, "subject": 17}
    elif variant == "empty_alternative":
        scene["supporting_visual_intent"] = dict.fromkeys(alternative)
    elif variant == "invalid_fallback":
        scene["fallback_sources"] = ["unknown_source"]
    elif variant == "unsupported_fallback":
        scene["fallback_sources"] = ["talking_profile"]

    class Transport:
        def post(self, *args):
            return ScenePlannerHTTPResponse(200, json.dumps(_chat_response([scene])).encode(), {})

    planner = OpenAICompatibleScenePlanner("fixture-key", protocol="chat_completions", transport=Transport())
    if not accepted:
        with pytest.raises(ScenePlannerProviderResponseError):
            planner.plan(_project())
        return
    result = planner.plan(_project()).scenes[0]
    assert result.voice_text == scene["voice_text"]
    assert result.visual_requirement == "creator_speaking"
    assert result.visual_requirement_reason == scene["visual_requirement_reason"]
    assert [source.value for source in result.preferred_sources] == scene["preferred_sources"]
    assert [source.value for source in result.fallback_sources] == scene["fallback_sources"]
    assert (result.supporting_visual_intent is not None) is retained


@pytest.mark.parametrize("primary_graphic", [False, True])
def test_missing_graphic_discards_only_optional_fallback(primary_graphic):
    from app.providers.scene_planner import ScenePlannerHTTPResponse
    scene = {**_scene("visual", 0, preferred=["typography"] if primary_graphic else ["user_asset"]),
        "graphic_plan": None, "visual_requirement": "unknown", "visual_requirement_reason": None}

    class Transport:
        def post(self, *args):
            return ScenePlannerHTTPResponse(200, json.dumps(_chat_response([scene])).encode(), {})

    planner = OpenAICompatibleScenePlanner("fixture-key", protocol="chat_completions", transport=Transport())
    if primary_graphic:
        with pytest.raises(ScenePlannerProviderResponseError) as failure:
            planner.plan(_project())
        assert failure.value.domain_rule == "graphic_plan_required"
    else:
        result = planner.plan(_project()).scenes[0]
        assert [kind.value for kind in result.preferred_sources] == ["user_asset"]
        assert "typography" not in [kind.value for kind in result.fallback_sources]
        assert result.graphic_plan is None


@pytest.mark.parametrize("style,accepted", [("auto", True), ("creator_led", False)])
def test_saved_creator_led_requirement_rejects_card_only_plan_without_another_paid_request(style, accepted):
    from app.providers.scene_planner import ScenePlannerHTTPResponse
    requests = []
    graphic = {**_scene("graphic", 0, preferred=["typography"]), "fallback_sources": [],
        "visual_requirement": "explanatory", "visual_requirement_reason": "解释概念",
        "graphic_plan": {"kind": "text_card", "treatment": "headline", "points": ["码率是数据量"], "reason": None}}
    class Transport:
        def post(self, url, headers, body, timeout_seconds):
            requests.append(json.loads(body))
            return ScenePlannerHTTPResponse(200, json.dumps(_chat_response([graphic])).encode(), {})
    planner = OpenAICompatibleScenePlanner("fixture-key", protocol="chat_completions", transport=Transport())
    current = _project().model_copy(update={"presentation_style": style})
    if accepted:
        assert planner.plan(current).scenes[0].preferred_sources[0] == "typography"
    else:
        with pytest.raises(ScenePlannerProviderResponseError) as raised:
            planner.plan(current)
        assert raised.value.domain_rule == "presentation_requirement"
    assert len(requests) == 1
    prompt = requests[0]["messages"][0]["content"]
    sent=json.loads(prompt.split('Project request:\n',1)[1].splitlines()[0])
    assert sent['production_controls']['presentation_style']==style


def test_creator_led_missing_media_preserves_new_talking_and_real_evidence_plan():
    from app.providers.scene_planner import ScenePlannerHTTPResponse
    scenes = [
        {**_scene("speaker", 0, preferred=["user_asset", "ai_video"]), "fallback_sources": ["capture"],
         "duration_target_ms": 7500, "visual_requirement": "creator_speaking",
         "visual_requirement_reason": "用户选择本人讲解，需要授权人物说出本段新文案", "graphic_plan": None},
        {**_scene("evidence", 1), "fallback_sources": ["capture"], "duration_target_ms": 2500,
         "visual_requirement": "action_evidence", "visual_requirement_reason": "展示同一动作的真实差异", "graphic_plan": None},
    ]
    class Transport:
        def post(self, *args):
            return ScenePlannerHTTPResponse(200, json.dumps(_chat_response(scenes)).encode(), {})
    current = _project().model_copy(update={"presentation_style": "creator_led"})
    result = OpenAICompatibleScenePlanner("fixture-key", protocol="chat_completions", transport=Transport()).plan(
        current, context={"materials": [], "material_gap": "no relevant local media"})
    assert [scene.visual_requirement for scene in result.scenes] == ["creator_speaking", "action_evidence"]
    assert "ai_video" in result.scenes[0].preferred_sources
    assert result.scenes[1].fallback_sources == ["capture"]


@pytest.mark.parametrize("supporting_graphic", [False, True])
def test_creator_led_new_talking_does_not_require_capture_or_action_evidence(supporting_graphic):
    from app.providers.scene_planner import ScenePlannerHTTPResponse
    calls = []
    speaker = {**_scene("speaker", 0, preferred=["ai_video"]), "fallback_sources": [],
        "duration_target_ms": 2500, "visual_requirement": "creator_speaking",
        "visual_requirement_reason": "本人通过已授权 Talking 讲解新文案", "graphic_plan": None}
    scenes = [speaker]
    if supporting_graphic:
        scenes.append({**_scene("concept", 1, preferred=["typography"]), "fallback_sources": [],
            "duration_target_ms": 7500, "visual_requirement": "explanatory",
            "visual_requirement_reason": "用静态对照说明概念，不声称实测结果",
            "graphic_plan": {"kind": "text_card", "treatment": "contrast", "points": ["数据预算", "实际画质"], "reason": None}})

    class Transport:
        def post(self, url, headers, body, timeout_seconds):
            calls.append(json.loads(body))
            return ScenePlannerHTTPResponse(200, json.dumps(_chat_response(scenes)).encode(), {})

    current = _project().model_copy(update={"presentation_style": "creator_led"})
    result = OpenAICompatibleScenePlanner("fixture-key", protocol="chat_completions", transport=Transport()).plan(
        current, context={"materials": []})
    assert [s.visual_requirement for s in result.scenes] == (["creator_speaking", "explanatory"] if supporting_graphic else ["creator_speaking"])
    assert all("capture" not in (*s.preferred_sources, *s.fallback_sources) for s in result.scenes)
    assert len(calls) == 1


@pytest.mark.parametrize("purpose,duration,error_field", [
    ("提出问题", 3500, None), ("解释码率原理", 3500, None), ("给出实用建议", 3500, None),
    ("目" * 100, 3500, None),  # The generation advice does not tighten the existing contract.
    ("", 3500, "purpose"), ("目" * 101, 3500, "purpose"),
    (None, 3500, "purpose"), (42, 3500, "purpose"), ({"label": "问题"}, 3500, "purpose"),
    ("提出问题", 0, "duration_target_ms"),
])
def test_purpose_guidance_preserves_scene_validation(purpose, duration, error_field):
    from app.providers.scene_planner import ScenePlannerHTTPResponse

    requests = []
    class Transport:
        def post(self, url, headers, body, timeout_seconds):
            requests.append(json.loads(body))
            scene = {**_scene("purpose_check", 0), "purpose": purpose, "duration_target_ms": duration}
            return ScenePlannerHTTPResponse(200, json.dumps(_chat_response([scene])).encode(), {})

    planner = OpenAICompatibleScenePlanner("fixture-key", protocol="chat_completions", transport=Transport())
    if error_field is None:
        assert planner.plan(_project()).scenes[0].purpose == purpose
    else:
        with pytest.raises(ScenePlannerProviderResponseError) as raised:
            planner.plan(_project())
        assert raised.value.validation_code == "scene_values" and raised.value.domain_field == error_field
    assert len(requests) == 1
    prompt = requests[0]["messages"][0]["content"]
    assert "short editorial label string" in prompt and "30 Chinese characters" in prompt
    assert "提出问题" in prompt and "purpose is not full narration" in prompt


@pytest.mark.parametrize("requirement", ["unknown", "creator_speaking", "action_evidence", "explanatory"])
def test_planner_accepts_explicit_visual_requirement_and_requests_it(requirement):
    from app.providers.scene_planner import _PLAN_SCHEMA
    scene = {**_scene("intent", 0), "visual_requirement": requirement,
             "visual_requirement_reason": None if requirement == "unknown" else "Retain the editorial evidence this scene needs."}
    with _fake_server(200, _chat_response([scene])) as (url, requests):
        result = OpenAICompatibleScenePlanner("key", base_url=url, protocol="chat_completions").plan(_project())
    assert result.scenes[0].visual_requirement == requirement
    schema = _PLAN_SCHEMA["properties"]["scenes"]["items"]
    assert "visual_requirement" in schema["required"]
    assert "Never classify as explanatory merely" in json.loads(requests[0][2])["messages"][0]["content"]


@pytest.mark.parametrize("fields", [
    {"visual_requirement": "explanatory"},
    {"visual_requirement": "explanatory", "visual_requirement_reason": None},
    {"visual_requirement": "action_evidence", "visual_requirement_reason": "   "},
    {"visual_requirement": "invented", "visual_requirement_reason": "Invalid classification"},
])
def test_planner_rejects_partial_or_unreasoned_visual_requirement(fields):
    with _fake_server(200, _chat_response([{**_scene("intent", 0), **fields}])) as (url, _):
        with pytest.raises(ScenePlannerProviderResponseError):
            OpenAICompatibleScenePlanner("key", base_url=url, protocol="chat_completions").plan(_project())


@contextmanager
def _fake_server(
    status: int,
    payload: object,
    response_headers: dict[str, str] | None = None,
) -> Iterator[tuple[str, list[tuple[str, dict[str, str], bytes]]]]:
    requests: list[tuple[str, dict[str, str], bytes]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append((self.path, dict(self.headers.items()), body))
            response = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            for name, value in (response_headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_scene_planner_posts_strict_schema_and_binds_project() -> None:
    project = _project()
    first = {**_scene("scene_01", 0), "voice_text": "Open with the misconception."}
    response = _response([first, _scene("scene_02", 1)])
    with _fake_server(200, response) as (base_url, requests):
        result = OpenAICompatibleScenePlanner("  runtime-secret  ", base_url=base_url).plan(
            project, script=first["voice_text"] + " " + _scene("scene_02", 1)["voice_text"], topic="When to avoid Vibe Coding",
            context={"ip_profile_version": 2, "evidence_refs": ["ip_profile:example:v2"], "ip_profile": {"audience": "builders"}},
        )

    assert isinstance(result, ScenePlanResult)
    assert result.project_id == project.id
    assert [scene.scene_id for scene in result.scenes] == ["scene_01", "scene_02"]
    assert [scene.order for scene in result.scenes] == [0, 1]
    assert result.scenes[0].project_id == project.id
    path, headers, body = requests[0]
    request = json.loads(body)
    assert path == "/v1/responses"
    assert headers["Authorization"] == "Bearer runtime-secret"
    assert request["model"] == "gpt-4.1-mini"
    assert request["store"] is False
    schema = request["text"]["format"]
    assert schema["type"] == "json_schema" and schema["strict"] is True
    assert schema["schema"]["additionalProperties"] is False
    assert schema["schema"]["properties"]["scenes"]["items"]["additionalProperties"] is False
    prompt = request["input"][0]["content"][0]["text"]
    assert "Prefer suitable creator footage" in prompt
    assert "Ownership does not make unrelated footage suitable" in prompt
    assert "Open with the misconception." in prompt
    assert "When to avoid Vibe Coding" in prompt
    sent=json.loads(prompt.split('Project request:\n',1)[1].splitlines()[0])
    assert sent['background_evidence']['ip_profile_version']==2


def test_scene_planner_supports_explicit_chat_completions_protocol() -> None:
    project = _project()
    with _fake_server(200, _chat_response([_scene("chat_scene", 0)])) as (base_url, requests):
        result = OpenAICompatibleScenePlanner(
            "runtime-secret", base_url=base_url, model="kimi-k3", protocol="chat_completions"
        ).plan(project, topic="H.265 码率与平台转码")

    assert result.project_id == project.id
    assert [scene.scene_id for scene in result.scenes] == ["chat_scene"]
    path, headers, body = requests[0]
    request = json.loads(body)
    assert path == "/v1/chat/completions"
    assert headers["Authorization"] == "Bearer runtime-secret"
    assert request["model"] == "kimi-k3"
    assert list(request) == ["model", "messages"]
    assert request["messages"][0]["role"] == "user"
    assert "H.265 码率与平台转码" in request["messages"][0]["content"]
    prompt = request["messages"][0]["content"]
    from app.providers.scene_planner import _PLAN_SCHEMA
    schema_text = prompt.split("Required output JSON Schema (return only the JSON instance, not the schema):\n", 1)[1].split("\n", 1)[0]
    assert json.loads(schema_text) == _PLAN_SCHEMA
    assert "milliseconds" in prompt
    assert "response_format" not in request


def test_scene_planner_supports_anthropic_messages_structured_output() -> None:
    with _fake_server(200, {
        "content": [{"type": "text", "text": json.dumps({"scenes": [_scene("anthropic", 0)]})}],
        "stop_reason": "end_turn",
    }) as (base_url, requests):
        result = OpenAICompatibleScenePlanner(
            "runtime-secret", base_url=base_url, model="claude-sonnet-4-6", protocol="anthropic_messages",
        ).plan(_project())

    assert result.scenes[0].scene_id == "anthropic"
    path, headers, body = requests[0]
    request = json.loads(body)
    assert path == "/v1/messages"
    headers = {key.lower(): value for key, value in headers.items()}
    assert headers["x-api-key"] == "runtime-secret"
    assert headers["anthropic-version"] == "2023-06-01"
    assert request["max_tokens"] == 4096
    assert request["output_config"]["format"]["type"] == "json_schema"
    schema = request["output_config"]["format"]["schema"]
    assert "minimum" not in json.dumps(schema)
    assert "maxLength" not in json.dumps(schema)
    assert "maxItems" not in json.dumps(schema)


def test_chat_schema_opt_in_uses_the_same_contract_and_ignores_reasoning():
    from app.providers.scene_planner import _PLAN_SCHEMA
    payload = _chat_response([_scene("schema", 0)])
    payload["choices"][0]["message"]["reasoning_content"] = "private reasoning, not JSON"
    with _fake_server(200, payload) as (url, requests):
        result = OpenAICompatibleScenePlanner("key", base_url=url, protocol="chat_completions", chat_output_mode="json_schema").plan(_project())
    assert result.scenes[0].scene_id == "schema"
    assert json.loads(requests[0][2])["response_format"] == {
        "type": "json_schema", "json_schema": {"name": "scene_plan", "strict": True, "schema": _PLAN_SCHEMA}}
    assert len(requests) == 1


@pytest.mark.parametrize("base_url,model,mode,expected", [
    ("https://api.moonshot.cn/v1", "kimi-k2.6", None, "json_schema"),
    ("https://api.moonshot.ai/v1/", "kimi-k2.6", None, "json_schema"),
    ("https://api.moonshot.cn/v1", "kimi-k2.6", "prompt", "prompt"),
    ("https://api.moonshot.cn/v1", "kimi-k2.6", "json_schema", "json_schema"),
    ("https://api.moonshot.cn/v1", "kimi-k3", None, "prompt"),
    ("https://api.deepseek.com/v1", "kimi-k2.6", None, "prompt"),
    ("https://proxy.example/v1", "kimi-k2.6", None, "prompt"),
])
def test_chat_output_default_uses_only_known_official_model_support(base_url, model, mode, expected):
    from app.providers.scene_planner import ScenePlannerHTTPResponse
    requests = []
    class Transport:
        def post(self, url, headers, body, timeout):
            requests.append(json.loads(body))
            return ScenePlannerHTTPResponse(200, json.dumps(_chat_response([_scene("default", 0)])).encode(), {})
    planner = OpenAICompatibleScenePlanner(
        "fixture-key", base_url=base_url, model=model, protocol="chat_completions",
        chat_output_mode=mode, transport=Transport(),
    )
    planner.plan(_project())
    assert planner.chat_output_mode == expected
    assert ("response_format" in requests[0]) == (expected == "json_schema")
    assert len(requests) == 1


def test_official_k26_responses_protocol_keeps_existing_default():
    planner = OpenAICompatibleScenePlanner("fixture-key", base_url="https://api.moonshot.cn/v1", model="kimi-k2.6")
    assert planner.chat_output_mode == "prompt"


@pytest.mark.parametrize("mode,protocol", [("auto", "chat_completions"), ("", "chat_completions"), ("json_schema", "responses")])
def test_chat_output_mode_is_explicitly_validated(mode, protocol):
    with pytest.raises(ScenePlannerConfigurationError):
        OpenAICompatibleScenePlanner("key", protocol=protocol, chat_output_mode=mode)


def test_schema_rejection_does_not_trigger_prompt_fallback():
    with _fake_server(400, {"error": "unsupported schema private-key"}) as (url, requests):
        with pytest.raises(ScenePlannerHTTPError):
            OpenAICompatibleScenePlanner("private-key", base_url=url, protocol="chat_completions", chat_output_mode="json_schema").plan(_project())
    assert len(requests) == 1


@pytest.mark.parametrize("failure,code", [
    ("response_json", "response_json"), ("response_envelope", "response_envelope"),
    ("output_text", "output_text"), ("output_json", "output_json"), ("fence", "output_json"),
    ("plan_shape", "plan_shape"), ("scene_shape", "scene_shape"), ("scene_values", "scene_values"),
    ("incomplete_answer", "incomplete_answer"),
])
def test_safe_validation_codes_without_provider_values(failure, code, caplog):
    import traceback
    secret = "private-provider-content"
    payload = _chat_response([_scene("safe", 0)])
    if failure == "response_json": payload = ("not json " + secret).encode()
    elif failure == "response_envelope": payload = [secret]
    elif failure == "output_text": payload = {"choices": [{"message": {"reasoning_content": secret}}]}
    elif failure == "output_json": payload["choices"][0]["message"]["content"] = secret
    elif failure == "fence": payload["choices"][0]["message"]["content"] = "```" + secret
    elif failure == "plan_shape": payload["choices"][0]["message"]["content"] = json.dumps({"wrong": secret})
    elif failure == "scene_shape":
        scene = _scene("safe", 0)
        scene[secret] = secret
        payload = _chat_response([scene])
    elif failure == "scene_values":
        scene = _scene("safe", 0)
        scene["duration_target_ms"] = secret
        payload = _chat_response([scene])
    elif failure == "incomplete_answer": payload["choices"][0]["finish_reason"] = "length"
    with _fake_server(200, payload) as (url, requests):
        with pytest.raises(ScenePlannerProviderResponseError) as raised:
            OpenAICompatibleScenePlanner(secret, base_url=url, protocol="chat_completions").plan(_project())
    assert raised.value.validation_code == code
    assert f"validation_code={code}" in caplog.text
    assert secret not in caplog.text
    assert secret not in "".join(traceback.format_exception(raised.value))
    assert len(requests) == 1


@pytest.mark.parametrize("failure,rule,field", [
    ("duration", "field_value", "duration_target_ms"),
    ("intent", "field_value", "visual_intent"),
    ("reason", "visual_requirement_reason_required", "visual_requirement_reason"),
    ("order", "scene_order", "unknown"),
    ("duplicate", "scene_id_duplicate", "unknown"),
    ("empty", "empty_plan", "unknown"),
])
def test_domain_diagnostics_are_specific_fixed_and_private(failure, rule, field, caplog):
    import traceback
    secret = "private-source-response-content"
    scenes = [_scene(secret, 0)]
    if failure == "duration": scenes[0]["duration_target_ms"] = secret
    elif failure == "intent": scenes[0]["visual_intent"]["action"] = [secret]
    elif failure == "reason": scenes[0].update(visual_requirement="explanatory", visual_requirement_reason="   ")
    elif failure == "order": scenes[0]["order"] = 1
    elif failure == "duplicate": scenes.append({**_scene(secret, 1)})
    elif failure == "priority": scenes[0]["preferred_sources"] = ["typography", "user_asset"]
    elif failure == "fallback": scenes[0].update(preferred_sources=["typography"], fallback_sources=["user_asset"])
    elif failure == "empty": scenes = []
    with _fake_server(200, _chat_response(scenes)) as (url, requests):
        with pytest.raises(ScenePlannerProviderResponseError) as raised:
            OpenAICompatibleScenePlanner(secret, base_url=url, protocol="chat_completions").plan(_project())
    assert raised.value.validation_code == "scene_values"
    assert raised.value.domain_rule == rule and raised.value.domain_field == field
    assert f"domain_rule={rule} domain_field={field}" in caplog.text
    assert secret not in caplog.text + "".join(traceback.format_exception(raised.value))
    assert len(requests) == 1


def test_domain_diagnostic_rejects_untrusted_identifiers():
    error = ScenePlannerProviderResponseError("safe", domain_rule="private-rule", domain_field="private-field")
    assert error.domain_rule == error.domain_field == "unknown"


@pytest.mark.parametrize("case,path,error_type", [
    ("nested", "visual_intent.action", "string_type"),
    ("motion_fallback", "supporting_visual_intent", "supporting_visual_intent_invalid"),
])
def test_validation_diagnostic_identifies_safe_field_and_type_without_values(case, path, error_type, caplog):
    import traceback
    secret = "private-response-value-should-never-appear"
    scene = _scene("safe", 0)
    if case == "nested":
        scene["visual_intent"]["action"] = [secret]
    else:
        scene.update(visual_requirement="creator_speaking", visual_requirement_reason=secret,
                     supporting_visual_intent={"subject": secret, "action": None, "framing": None, "description": None},
                     fallback_sources=["talking_profile"])
    class Transport:
        def post(self, url, headers, body, timeout_seconds):
            return ScenePlannerHTTPResponse(200, json.dumps(_chat_response([scene])).encode(), {})
    with pytest.raises(ScenePlannerProviderResponseError) as raised:
        OpenAICompatibleScenePlanner(secret, protocol="chat_completions", transport=Transport()).plan(_project())
    assert raised.value.validation_path == path
    assert raised.value.validation_type == error_type
    assert f"validation_path={path} validation_type={error_type}" in caplog.text
    assert secret not in caplog.text + "".join(traceback.format_exception(raised.value))


def test_validation_path_never_renders_provider_controlled_keys():
    from app.providers.scene_planner import _safe_validation_diagnostic
    secret = "private_extra_key"
    assert _safe_validation_diagnostic({"loc": ("visual_intent", secret), "type": "extra_forbidden"}) == (
        "field_value", "visual_intent", "visual_intent", "extra_forbidden")
    assert _safe_validation_diagnostic({"loc": (secret,), "type": secret}) == (
        "field_value", "unknown", "unknown", "unknown")


@pytest.mark.parametrize("protocol", ["responses", "chat_completions"])
@pytest.mark.parametrize("graphic,accepted", [
    ({"kind": "text_card", "treatment": "key_point", "points": ["结论", "条件", "证据", "限制"], "reason": None}, True),
    ({"kind": "unsupported", "treatment": None, "points": [], "reason": "Requires node links"}, True),
    (None, False),
    ({"kind": "text_card", "treatment": "headline", "points": ["结论", "条件"], "reason": None}, False),
])
def test_explicit_graphic_provider_shape_preserves_realization_or_rejects_invalid(protocol, graphic, accepted):
    value = {**_scene("graphic", 0), "visual_requirement": "explanatory",
             "visual_requirement_reason": "A concept explanation", "graphic_plan": graphic}
    if graphic is None:
        value["preferred_sources"] = ["typography"]
    response = _response([value]) if protocol == "responses" else _chat_response([value])
    with _fake_server(200, response) as (url, requests):
        planner = OpenAICompatibleScenePlanner("key", base_url=url, protocol=protocol)
        if accepted:
            assert planner.plan(_project()).scenes[0].graphic_plan.model_dump(mode="json", exclude={"schema_version"}) == graphic
        else:
            with pytest.raises(ScenePlannerProviderResponseError) as failure:
                planner.plan(_project())
            assert failure.value.validation_code == "scene_values"
        assert len(requests) == 1


@pytest.mark.parametrize("protocol", ["responses", "chat_completions"])
def test_prompt_unifies_generation_and_editorial_source_preferences(protocol):
    from app.providers.scene_planner import ScenePlannerHTTPResponse
    response = _response([_scene("valid", 0)]) if protocol == "responses" else _chat_response([_scene("valid", 0)])
    requests = []
    class Transport:
        def post(self, url, headers, body, timeout_seconds):
            requests.append(json.loads(body))
            return ScenePlannerHTTPResponse(200, json.dumps(response).encode(), {})

    result = OpenAICompatibleScenePlanner("key", protocol=protocol, transport=Transport()).plan(_project())
    body = requests[0]
    text = body["messages"][0]["content"] if protocol == "chat_completions" else body["input"][0]["content"][0]["text"]
    assert "Prefer suitable creator footage" in text
    assert "External footage or graphics may lead" in text
    assert "creator sources may be fallback-only" in text
    assert "Plan cloned narration, new Talking and supporting visuals together" in text
    assert "Default to a complete plan needing no new capture" in text
    assert "Do not turn generated illustrations into evidence of real events" in text
    assert "must precede every" not in text and "real sources cannot be fallback-only" not in text
    assert "25 percent" not in text
    assert "creative preference from an irreplaceable visual requirement" in text
    assert "not proof that the creator must visibly speak" in text
    assert "not merely because the topic mentions an action" in text
    assert "Read production_feasibility before choosing sources" in text
    assert "Unknown price is not zero" in text
    assert len(requests) == 1
    assert result.scenes[0].visual_requirement == "unknown"  # unchanged legacy behavior


@pytest.mark.parametrize("protocol", ["responses", "chat_completions"])
@pytest.mark.parametrize("topic,accepted", [("保住采访原意", False), ("Content OS 的预检流程", True)])
def test_retained_audience_control_leak_across_protocols(protocol, topic, accepted, caplog):
    from test_planning_input import context, LEAKED_VOICE
    value = {**_scene("caption-discipline", 0), "voice_text": LEAKED_VOICE}
    response = _response([value]) if protocol == "responses" else _chat_response([value])
    with _fake_server(200, response) as (url, requests):
        planner = OpenAICompatibleScenePlanner("private-key", base_url=url, protocol=protocol)
        if accepted:
            assert planner.plan(_project(), topic=topic, context=context()).scenes[0].voice_text == LEAKED_VOICE
        else:
            with pytest.raises(ScenePlannerProviderResponseError) as failure:
                planner.plan(_project(), topic=topic, context=context())
            assert failure.value.validation_code == "scene_values"
            assert failure.value.domain_rule == "audience_control_leak"
            assert failure.value.domain_field == "voice_text"
            assert "domain_rule=audience_control_leak" in caplog.text
        assert len(requests) == 1
        body = json.loads(requests[0][2])
        prompt = body["messages"][0]["content"] if protocol == "chat_completions" else body["input"][0]["content"][0]["text"]
        serialized = prompt.split("Project request:\n", 1)[1].split("\n\nRequired output JSON Schema", 1)[0]
        request = json.loads(serialized)
        assert request["audience_brief"]["topic"] == topic
        assert "planning_preferences" not in request["background_evidence"]
        assert "instruction" not in request["production_controls"]["planning_preferences"][0]
    assert LEAKED_VOICE not in caplog.text and "private-key" not in caplog.text


@pytest.mark.parametrize("preferred,fallback", [
    (["stock", "user_asset"], ["capture"]),
    (["stock"], ["user_asset", "historical_asset"]),
    (["typography", "user_asset"], ["historical_asset"]),
])
def test_source_order_follows_editorial_intent_not_ownership(preferred, fallback):
    scene = _scene("explain", 0, preferred=preferred, fallback=fallback)
    with _fake_server(200, _chat_response([scene])) as (url, requests):
        result = OpenAICompatibleScenePlanner("key", base_url=url, protocol="chat_completions").plan(_project())
        assert [v.value for v in result.scenes[0].preferred_sources] == preferred
    assert len(requests) == 1


@pytest.mark.parametrize("protocol", ["", "chat", "responses/v1", 1, [], None])
def test_scene_planner_rejects_unknown_protocol(protocol: object) -> None:
    with pytest.raises(ScenePlannerConfigurationError, match="protocol"):
        OpenAICompatibleScenePlanner("key", protocol=protocol)  # type: ignore[arg-type]


@pytest.mark.parametrize("protocol,field", [("responses", "max_output_tokens"), ("chat_completions", "max_completion_tokens")])
def test_explicit_output_limit_and_effort(protocol, field, caplog):
    scene = {**_scene("bounded", 0), "voice_text": "private-script"}
    payload = _response([scene]) if protocol == "responses" else _chat_response([scene])
    with _fake_server(200, payload) as (url, requests):
        with caplog.at_level("INFO", logger="app.providers.scene_planner"):
            OpenAICompatibleScenePlanner("private-key", base_url=url, protocol=protocol,
                max_output_tokens=4096, reasoning_effort="low" if protocol == "chat_completions" else None).plan(_project(), script="private-script")
    body = json.loads(requests[0][2])
    assert body[field] == 4096
    assert body.get("reasoning_effort") == ("low" if protocol == "chat_completions" else None)
    assert len(requests) == 1
    assert "outcome=success stage=validation http=200" in caplog.text
    assert "private-key" not in caplog.text and "private-script" not in caplog.text


@pytest.mark.parametrize("base_url", ["https://api.deepseek.com", "https://api.deepseek.com/v1/"])
def test_official_deepseek_chat_controls_and_parsing(base_url):
    from app.providers.scene_planner import ScenePlannerHTTPResponse

    calls = []
    class Transport:
        def post(self, url, headers, body, timeout_seconds):
            calls.append((url, json.loads(body), timeout_seconds))
            return ScenePlannerHTTPResponse(200, json.dumps(_chat_response([_scene("planned", 0)])).encode(), {})

    result = OpenAICompatibleScenePlanner("runtime-key", base_url=base_url, model="deepseek-flash",
        protocol="chat_completions", reasoning_effort="none", max_output_tokens=4096,
        timeout_seconds=90, chat_output_mode="json_object", transport=Transport()).plan(_project())
    assert len(calls) == 1 and calls[0][0] == base_url.rstrip("/") + "/chat/completions"
    body = calls[0][1]
    assert body["max_tokens"] == 4096 and body["reasoning_effort"] == "none"
    assert "max_completion_tokens" not in body and "max_output_tokens" not in body
    assert body["model"] == "deepseek-flash" and calls[0][2] == 90
    assert body["response_format"]=={"type":"json_object"}
    assert "Required output JSON Schema" in body["messages"][0]["content"]
    assert result.scenes[0].scene_id == "planned"


@pytest.mark.parametrize("base_url", ["https://api.moonshot.cn/v1", "https://api.moonshot.ai/v1"])
def test_moonshot_k26_structured_plan_disables_default_thinking(base_url):
    from app.providers.scene_planner import ScenePlannerHTTPResponse

    calls = []
    class Transport:
        def post(self, url, headers, body, timeout_seconds):
            calls.append(json.loads(body))
            return ScenePlannerHTTPResponse(200, json.dumps(_chat_response([_scene("planned", 0)])).encode(), {})

    planner = OpenAICompatibleScenePlanner("runtime-key", base_url=base_url, model="kimi-k2.6",
        protocol="chat_completions", max_output_tokens=6144, transport=Transport())
    assert planner.plan(_project()).scenes[0].scene_id == "planned"
    assert calls[0]["thinking"] == {"type": "disabled"}
    assert calls[0]["max_tokens"] == 6144
    assert "max_completion_tokens" not in calls[0]
    assert len(calls) == 1


def test_moonshot_k26_thinking_control_is_provider_scoped():
    with pytest.raises(ScenePlannerConfigurationError, match="thinking type"):
        OpenAICompatibleScenePlanner("key", model="gpt-4.1-mini", thinking_type="disabled")
    with pytest.raises(ScenePlannerConfigurationError, match="thinking type"):
        OpenAICompatibleScenePlanner("key", base_url="https://api.moonshot.cn/v1", model="kimi-k2.6",
            protocol="chat_completions", thinking_type="invalid")


@pytest.mark.parametrize("base_url,protocol", [
    ("https://api.openai.com/v1", "chat_completions"),
    ("https://api.moonshot.cn/v1", "chat_completions"),
    ("https://api.deepseek.com.example/v1", "chat_completions"),
    ("https://api.deepseek.com", "responses"),
])
def test_reasoning_none_does_not_change_other_endpoints(base_url, protocol):
    with pytest.raises(ScenePlannerConfigurationError):
        OpenAICompatibleScenePlanner("key", base_url=base_url, protocol=protocol, reasoning_effort="none")


@pytest.mark.parametrize("options", [
    {"max_output_tokens": True}, {"max_output_tokens": 0}, {"max_output_tokens": -1},
    {"max_output_tokens": "4096"}, {"max_output_tokens": 1.5},
    {"reasoning_effort": "medium", "protocol": "chat_completions"},
    {"reasoning_effort": "low"}, {"reasoning_effort": ""},
])
def test_invalid_planner_controls_fail_before_dispatch(options):
    with pytest.raises(ScenePlannerConfigurationError):
        OpenAICompatibleScenePlanner("key", **options)


@pytest.mark.parametrize("finish_reason", ["length", "tool_calls", "content_filter"])
def test_incomplete_chat_response_is_rejected_even_if_json_valid(finish_reason, caplog):
    payload = _chat_response([_scene("partial", 0)])
    payload["choices"][0]["finish_reason"] = finish_reason
    with _fake_server(200, payload) as (url, requests):
        with pytest.raises(ScenePlannerProviderResponseError, match="complete answer"):
            OpenAICompatibleScenePlanner("key", base_url=url, protocol="chat_completions").plan(_project())
    assert len(requests) == 1
    assert "outcome=failure stage=validation http=200" in caplog.text


@pytest.mark.parametrize("stage", ["awaiting_headers", "reading_body", "private-key"])
def test_diagnostic_failure_stage_is_allowlisted(stage, caplog):
    class FailureTransport:
        calls = 0
        def post(self, *args):
            self.calls += 1
            error = ScenePlannerTimeout("safe timeout")
            error.stage = stage
            raise error
    transport = FailureTransport()
    with pytest.raises(ScenePlannerTimeout):
        OpenAICompatibleScenePlanner("private-key", transport=transport).plan(_project(), script="private-script")
    expected = stage if stage != "private-key" else "transport"
    assert f"stage={expected} http=None" in caplog.text
    assert transport.calls == 1
    assert "private-key" not in caplog.text and "private-script" not in caplog.text


@pytest.mark.parametrize("stage", ["awaiting_headers", "reading_body"])
def test_default_transport_reports_timeout_stage(monkeypatch, stage):
    from app.providers.scene_planner import UrllibScenePlannerTransport
    class Response:
        status = 200
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): raise TimeoutError("private upstream detail")
    class Opener:
        def open(self, *args, **kwargs):
            if stage == "awaiting_headers": raise TimeoutError("private upstream detail")
            return Response()
    monkeypatch.setattr("app.providers.scene_planner.build_opener", lambda *args: Opener())
    with pytest.raises(ScenePlannerTimeout) as raised:
        UrllibScenePlannerTransport().post("https://example.com/v1", {}, b"{}", 1)
    assert raised.value.stage == stage
    assert "private" not in str(raised.value)


def test_incomplete_responses_output_cannot_be_admitted():
    payload = _response([_scene("partial", 0)])
    payload["status"] = "incomplete"
    with _fake_server(200, payload) as (url, requests):
        with pytest.raises(ScenePlannerProviderResponseError, match="complete answer"):
            OpenAICompatibleScenePlanner("key", base_url=url).plan(_project())
    assert len(requests) == 1


def test_wrapped_socket_timeout_is_not_misclassified(monkeypatch):
    from urllib.error import URLError
    from app.providers.scene_planner import UrllibScenePlannerTransport
    class Opener:
        def open(self, *args, **kwargs):
            raise URLError(TimeoutError("private detail"))
    monkeypatch.setattr("app.providers.scene_planner.build_opener", lambda *args: Opener())
    with pytest.raises(ScenePlannerTimeout) as raised:
        UrllibScenePlannerTransport().post("https://example.com/v1", {}, b"{}", 1)
    assert raised.value.stage == "awaiting_headers"


@pytest.mark.parametrize(
    ("status", "error_type"),
    [(401, ScenePlannerAuthenticationError), (429, ScenePlannerRateLimitError), (500, ScenePlannerHTTPError)],
)
def test_scene_planner_http_errors_are_safe(status: int, error_type: type[Exception]) -> None:
    key = "scene-planner-secret"
    with _fake_server(status, {"error": {"message": key}}) as (base_url, _):
        with pytest.raises(error_type) as raised:
            OpenAICompatibleScenePlanner(key, base_url=base_url).plan(_project())
    assert key not in str(raised.value)
    if status == 500:
        assert isinstance(raised.value, ScenePlannerHTTPError)
        assert raised.value.status_code == 500


def test_scene_planner_does_not_follow_redirect_with_byok() -> None:
    with _fake_server(200, _response([_scene("winner", 0)])) as (target_url, target_requests):
        with _fake_server(302, {"redirect": "blocked"}, {"Location": f"{target_url}/responses"}) as (source_url, source_requests):
            with pytest.raises(ScenePlannerHTTPError, match="302"):
                OpenAICompatibleScenePlanner("redirect-secret", base_url=source_url).plan(_project())
    assert len(source_requests) == 1
    assert target_requests == []


def test_scene_planner_rejects_bad_provider_order_and_extra_fields() -> None:
    unordered = _response([_scene("later", 1), _scene("first", 0)])
    with _fake_server(200, unordered) as (base_url, _):
        with pytest.raises(ScenePlannerProviderResponseError, match="invalid scene plan"):
            OpenAICompatibleScenePlanner("key", base_url=base_url).plan(_project())

    extra_field = _scene("extra", 0)
    extra_field["provider_only_id"] = "must-not-pass-through"
    with _fake_server(200, _response([extra_field])) as (base_url, _):
        with pytest.raises(ScenePlannerProviderResponseError, match="invalid scene plan"):
            OpenAICompatibleScenePlanner("key", base_url=base_url).plan(_project())



def test_scene_planner_input_configuration_and_transport_errors_are_safe() -> None:
    with pytest.raises(ScenePlannerConfigurationError, match="localhost"):
        OpenAICompatibleScenePlanner("key", base_url="http://remote.example/v1")
    with pytest.raises(ScenePlannerConfigurationError, match="timeout"):
        OpenAICompatibleScenePlanner("key", timeout_seconds=float("nan"))
    with pytest.raises(ScenePlannerInputError, match="script"):
        OpenAICompatibleScenePlanner("key").plan(_project(), script=" ")

    class TimeoutTransport:
        def post(self, url: str, headers: dict[str, str], body: bytes, timeout_seconds: float) -> object:
            raise TimeoutError("secret transport detail")

    key = "timeout-secret"
    with pytest.raises(ScenePlannerTimeout) as raised:
        OpenAICompatibleScenePlanner(key, transport=TimeoutTransport()).plan(_project())  # type: ignore[arg-type]
    assert key not in str(raised.value)

    class BrokenTransport:
        def post(self, url: str, headers: dict[str, str], body: bytes, timeout_seconds: float) -> object:
            raise RuntimeError(key)

    with pytest.raises(ScenePlannerConnectionError) as raised:
        OpenAICompatibleScenePlanner(key, transport=BrokenTransport()).plan(_project())  # type: ignore[arg-type]
    assert key not in str(raised.value)


def test_scene_plan_result_is_immutable_and_rejects_invalid_status() -> None:
    project = _project()
    with pytest.raises(ValueError, match="immutable"):
        ScenePlanResult(project_id=project.id, scenes=[_scene("not-a-contract", 0)])  # type: ignore[arg-type]

    class InvalidStatusTransport:
        def post(self, url: str, headers: dict[str, str], body: bytes, timeout_seconds: float) -> object:
            return type("Response", (), {"status_code": True, "body": b"{}", "headers": {}})()

    with pytest.raises(ScenePlannerProviderResponseError, match="invalid HTTP status"):
        OpenAICompatibleScenePlanner("key", transport=InvalidStatusTransport()).plan(project)  # type: ignore[arg-type]
