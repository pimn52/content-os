"""画面调整只传画面字段；其余成片内容由已保存方案决定。"""
from copy import deepcopy
import json
from uuid import uuid4

import pytest

from app.domain.models import ScenePlan, VisualIntent, MaterialRequest
from app.providers.scene_planner import OpenAICompatibleScenePlanner, ScenePlannerHTTPResponse, ScenePlannerProviderResponseError
from test_scene_planner import _project


def _revision_fixture():
    project = _project().model_copy(update={"presentation_style": "creator_led"})
    originals = [ScenePlan(project_id=project.id, scene_id="creator", order=0, purpose="本人引出问题",
        voice_text="看这组对比。", duration_target_ms=1800,
        visual_intent=VisualIntent(subject="创作者本人", action="讲话"), preferred_sources=["ai_video"], fallback_sources=[],
        visual_requirement="creator_speaking", visual_requirement_reason="本人说明判断", caption_emphasis=["对比"],
        viewer_takeaway="先看真实对比，不能只按数字判断。"),
        ScenePlan(project_id=project.id, scene_id="evidence", order=1, purpose="展示实际差异",
        voice_text="同一原片编码后，细节的保留程度不同。", duration_target_ms=1200,
        visual_intent=VisualIntent(subject="同原片编码对比", description="展示实际编码结果"),
        preferred_sources=["stock"], fallback_sources=[], visual_requirement="action_evidence",
        visual_requirement_reason="必须展示同原片的真实编码结果，不能用模糊拍摄假装压缩损失。",
        viewer_takeaway="同原片真实编码结果呈现不同细节。", reuse_intent="reference",
        material_request=MaterialRequest(source_kind="stock", query="同原片 编码 对比", reason="需要真实对比"))]
    clip_id = str(uuid4())
    context = {"visual_revision": {"scenes": [scene.model_dump(mode="json") for scene in originals]},
        "continuous_production": {"talking_motion_budget": {"planned_visible_ms_max": 2400, "approx_chinese_characters_max": 7}},
        "materials": [{"clip_id": clip_id, "source_kind": "stock", "visual_evidence": {
            "source_hash_bound": True, "coverage": "video_interval_analysis",
            "usable_interval": {"start_ms": 0, "end_ms": 1200, "duration_ms": 1200},
            "output_presentation": {"available": True}}}]}
    updates = [{"scene_id": scene.scene_id, "visual_intent": scene.visual_intent.model_dump(exclude={"schema_version"}),
        "preferred_sources": [kind.value for kind in scene.preferred_sources], "fallback_sources": [],
        "preferred_clip_id": None if scene.scene_id == "creator" else clip_id} for scene in originals]
    updates[1]["visual_intent"]["description"] = "已有素材中的同原片编码对比"
    return project, originals, context, {"scenes": updates}


def _planner(document, protocol="chat_completions", mode="json_object"):
    requests = []
    class Transport:
        def post(self, url, headers, body, timeout_seconds):
            requests.append(json.loads(body))
            text = json.dumps(document, ensure_ascii=False)
            response = ({"output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}
                if protocol == "responses" else {"content": [{"type": "text", "text": text}]}
                if protocol == "anthropic_messages" else {"choices": [{"message": {"content": text}}]})
            return ScenePlannerHTTPResponse(200, json.dumps(response).encode(), {})
    options = {"chat_output_mode": mode} if protocol == "chat_completions" else {}
    return OpenAICompatibleScenePlanner("fixture", protocol=protocol, transport=Transport(), **options), requests


@pytest.mark.parametrize("protocol,mode", [("responses", None), ("chat_completions", "json_schema"),
    ("chat_completions", "json_object"), ("anthropic_messages", None)])
def test_visual_patch_schema_and_protocol_preserve_original_content_without_creator_slot(protocol, mode):
    project, originals, context, document = _revision_fixture()
    untouched = deepcopy(context)
    document["scenes"].reverse()  # 模型返回顺序不影响原场景的真实顺序和身份。
    planner, requests = _planner(document, protocol, mode)
    result = planner.plan(project, script="\n\n".join(scene.voice_text for scene in originals), context=context)
    assert len(requests) == 1 and context == untouched
    for original, revised in zip(originals, result.scenes):
        assert revised.model_dump(exclude={"visual_intent", "preferred_clip_id", "material_request"}) == original.model_dump(
            exclude={"visual_intent", "preferred_clip_id", "material_request"})
        assert revised.material_request is None
    assert str(result.scenes[1].preferred_clip_id) == context["materials"][0]["clip_id"]
    assert result.scenes[1].visual_intent.description == "已有素材中的同原片编码对比"
    body = requests[0]
    if protocol == "responses":
        schema = body["text"]["format"]["schema"]
    elif protocol == "anthropic_messages":
        schema = body["output_config"]["format"]["schema"]
    elif mode == "json_schema":
        schema = body["response_format"]["json_schema"]["schema"]
    else:
        prompt = body["messages"][0]["content"]
        schema = json.JSONDecoder().raw_decode(prompt.split("Required output JSON Schema (return only the JSON instance, not the schema):\n")[1])[0]
    assert set(schema["properties"]) == {"scenes"}
    item = schema["properties"]["scenes"]["items"]
    fields = {"scene_id", "visual_intent", "preferred_sources", "fallback_sources", "preferred_clip_id"}
    assert set(item["required"]) == set(item["properties"]) == fields
    assert item["additionalProperties"] is False
    assert item["properties"]["scene_id"]["enum"] == ["creator", "evidence"]
    assert item["properties"]["preferred_clip_id"]["enum"] == [None, context["materials"][0]["clip_id"]]


@pytest.mark.parametrize("violation", ["duplicate", "missing", "unknown_id", "voice_text", "material_request",
    "intent_key", "missing_field", "unknown_clip", "missing_clip", "blocked_clip", "short_clip", "lost_person", "graphic_fallback"])
def test_visual_patch_rejects_invalid_identity_extra_content_and_unusable_materials_without_retry(violation):
    project, _, context, document = _revision_fixture()
    scenes = document["scenes"]
    if violation == "duplicate": scenes[1]["scene_id"] = scenes[0]["scene_id"]
    elif violation == "missing": scenes.pop()
    elif violation == "unknown_id": scenes[1]["scene_id"] = "invented"
    elif violation == "voice_text": scenes[1]["voice_text"] = "改写主张"
    elif violation == "material_request": scenes[1]["material_request"] = None
    elif violation == "intent_key": scenes[1]["visual_intent"]["new_field"] = "extra"
    elif violation == "missing_field": scenes[1].pop("preferred_clip_id")
    elif violation == "unknown_clip": scenes[1]["preferred_clip_id"] = str(uuid4())
    elif violation == "missing_clip": scenes[1]["preferred_clip_id"] = None
    elif violation == "blocked_clip": context["materials"][0]["visual_evidence"]["output_presentation"]["available"] = False
    elif violation == "short_clip": context["materials"][0]["visual_evidence"]["usable_interval"] = {"start_ms": 0, "end_ms": 600, "duration_ms": 600}
    elif violation == "lost_person":
        scenes[0]["preferred_sources"] = ["stock"]
        scenes[0]["preferred_clip_id"] = scenes[1]["preferred_clip_id"]
        context["materials"][0]["visual_evidence"]["usable_interval"] = {"start_ms": 0, "end_ms": 2400, "duration_ms": 2400}
    elif violation == "graphic_fallback": scenes[1]["fallback_sources"] = ["typography"]
    planner, requests = _planner(document)
    with pytest.raises(ScenePlannerProviderResponseError):
        planner.plan(project, context=context)
    assert len(requests) == 1
