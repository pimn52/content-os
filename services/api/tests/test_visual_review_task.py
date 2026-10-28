"""原规划的表达任务传给视频核验；替身结果不代表真实模型已遵守规则。"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest

from app.domain.models import ScenePlan, VisualIntent
from app.media.planning import MaterialResolutionError
from app.media.visual_facts import matches_scene, scene_visual_key, visual_review_task
from app.providers.vision import OpenAICompatibleVisionProvider, VisionHTTPResponse
from test_material_resolution import material_library


def test_product_api_imports_in_a_fresh_process_without_provider_media_cycle():
    api_root=Path(__file__).resolve().parents[1]
    env={**os.environ,'PYTHONPATH':str(api_root)}
    result=subprocess.run([sys.executable,'-c','from app.main import create_app'],cwd=api_root,
        env=env,capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr


def test_video_request_preserves_original_visual_task_without_turning_takeaway_into_extra_proof(tmp_path):
    video=tmp_path/'planned-context.mp4'
    video.write_bytes(b'\x00\x00\x00\x18ftypisom fixture video')
    scenes=[{'scene_key':'context','voice_text':'合理设置参数有助于稳定使用体验。',
        'visual_intent':{'subject':'使用设备的人','action':'观看内容','framing':'近景','description':'承接使用建议的日常场景'},
        'visual_requirement':'explanatory','visual_requirement_reason':'需要日常使用情境，无需展示实测操作。',
        'viewer_takeaway':'按照实际需求选择设置，而非只追求最高参数。'},
        {'scene_key':'actual-result','voice_text':'展示本次参数设置前后的实际差异。',
        'visual_intent':{'subject':'设置界面及同原片对比','action':'切换实际参数并展示结果','framing':'屏幕特写','description':'必须可辨识本次参数及结果'},
        'visual_requirement':'action_evidence','visual_requirement_reason':'确切设置和同原片对比必须真实可见。',
        'viewer_takeaway':'观众看到本次设置确实产生的差异。'}]
    unchanged=deepcopy(scenes)
    requests=[]
    class Transport:
        def post(self,url,headers,body,timeout_seconds):
            requests.append(json.loads(body))
            output={'clips':[{'clip_id':'clip','description':'人在观看设备上的内容','content_consistent':True,
                'comparison_layout':'none','subtitle_regions':[],'protected_regions':[],
                'matches':[{'scene_key':'context','supported':True,'score':.85,'reason':'只承担原计划的使用情境',
                    'fit':'context','quality_adequate':True,'subject_verified':True}], 'reason':'测试替身'}]}
            return VisionHTTPResponse(200,json.dumps({'choices':[{'finish_reason':'stop','message':{'content':json.dumps(output)}}]}).encode(),{})
    provider=OpenAICompatibleVisionProvider('fixture',base_url='https://api.moonshot.cn/v1',model='kimi-k2.6',
        protocol='chat_completions',video_input=True,transport=Transport())
    result=provider.analyze_intervals([{'clip_id':'clip','path':str(video)}],scenes)
    assert len(requests)==1 and scenes==unchanged
    prompt=requests[0]['messages'][0]['content'][0]['text']
    supplied=json.loads(prompt.split('场景用途：',1)[1].split('\n输出 JSON schema：',1)[0])
    for original,request in zip(scenes,supplied):
        assert {key:value for key,value in request.items() if key!='review_task'}==original
        assert request['review_task']['preserve_explicit_visual_requirements'] is True
        assert request['review_task']['viewer_takeaway_is_additional_proof_requirement'] is False
        assert request['review_task']['unrelated_or_misleading_visuals']=='reject'
    assert supplied[0]['review_task']['task']=='verify_planned_explanatory_use'
    assert supplied[0]['review_task']['planned_context_may_support'] is True
    assert supplied[1]['review_task']['task']=='verify_required_factual_evidence'
    assert supplied[1]['review_task']['planned_context_may_support'] is False
    assert result.clips[0].matches[0].fit=='context'


@pytest.mark.parametrize('requirement,supported,quality,accepted',[
    ('explanatory',True,True,True),('explanatory',False,True,False),
    ('explanatory',True,False,False),('action_evidence',True,True,False)])
def test_context_can_serve_planned_explanation_but_never_becomes_factual_evidence_or_passes_unrelated_footage(
    tmp_path,requirement,supported,quality,accepted):
    db,project,_,_,base,provider,resolver=material_library(tmp_path,metered=True)
    try:
        scene=base.model_copy(update={'visual_requirement':requirement,
            'visual_requirement_reason':'原计划的生活使用情境' if requirement=='explanatory' else '必须展示实际操作及结果'})
        original=provider.analyze_intervals
        provider.accept=lambda video:True
        def analyze(videos,requests):
            result=original(videos,requests)
            for clip in result.clips:
                for match in clip.matches:
                    match.supported=supported;match.quality_adequate=quality;match.fit='context';match.subject_verified=True
            return result
        provider.analyze_intervals=analyze
        if accepted:
            assert resolver.resolve(project,[scene],uuid4())[0].preferred_clip_id is not None
        else:
            with pytest.raises(MaterialResolutionError):resolver.resolve(project,[scene],uuid4())
        assert len(provider.calls)==1  # 无人工放行，也不因新规则自动重试。
    finally:db.close()


def test_updated_review_task_does_not_reinterpret_old_cached_qa_or_weaken_explicit_visual_requirements():
    scene=ScenePlan(project_id=uuid4(),scene_id='scene',order=0,purpose='说明建议',voice_text='按实际需求选择参数。',
        duration_target_ms=2000,visual_intent=VisualIntent(subject='日常使用设备'),preferred_sources=['stock'],
        visual_requirement='explanatory',visual_requirement_reason='日常使用场景只用于承接建议。')
    old_request={"voice_text":scene.voice_text,"purpose":scene.purpose,"visual_intent":scene.visual_intent.model_dump(mode='json'),
        "visual_requirement":scene.visual_requirement,"visual_requirement_reason":scene.visual_requirement_reason}
    old_key=hashlib.sha256(json.dumps(old_request,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    facts={'coverage':'video_interval_analysis','scene_matches':{old_key:{'supported':True,'score':.9,
        'quality_adequate':True,'fit':'context'}}}
    preserved=deepcopy(facts)
    assert scene_visual_key(scene)!=old_key and not matches_scene(facts,scene)
    assert facts==preserved  # 旧正负事实保留，不能将新任务解释套在旧结论上。
    measured=scene.model_copy(update={'visual_intent':VisualIntent(subject='同原片实测结果'),
        'visual_requirement_reason':'即使解释性段落也明确要求真实参数与结果。'})
    assert scene_visual_key(measured)!=scene_visual_key(scene)
    assert visual_review_task(measured.visual_requirement)['preserve_explicit_visual_requirements'] is True
