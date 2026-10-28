from datetime import datetime, timezone
import json
import pytest
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from app.db import Database, IPProfileRepository, ProjectRepository
from app.domain.models import IPProfile, Project, RationalFps, ScenePlan, SourceKind, VisualIntent
from app.main import _scene_planner_from_env, create_app
from app.providers.scene_planner import OpenAICompatibleScenePlanner, ScenePlanResult, ScenePlannerHTTPResponse


def test_scene_planner_authentication_error_has_distinct_code():
    from app.main import _scene_planner_error_code
    from app.providers.scene_planner import ScenePlannerAuthenticationError, ScenePlannerConfigurationError

    assert _scene_planner_error_code(ScenePlannerAuthenticationError("rejected")) == "scene_planner_authentication_rejected"
    assert _scene_planner_error_code(ScenePlannerConfigurationError("missing")) == "scene_planner_not_configured"


def test_visual_revision_preserves_original_when_creator_preference_changes(tmp_path, monkeypatch):
    from app.feedback_learning import FeedbackLearningService
    from app.scene_planning import generate_scene_plan, ScenePlanRequest

    path = tmp_path / 'revision-preference.sqlite'
    project = _project(path)
    scene = ScenePlan(project_id=project.id, scene_id='opening', order=0, purpose='解释画质',
        voice_text='码率越高不一定越清晰。', duration_target_ms=3000,
        visual_intent=VisualIntent(subject='码率'), preferred_sources=['user_asset'],
        caption_emphasis=['码率越高不一定越清晰。', '不一定越清晰'])
    preferences = []
    monkeypatch.setattr(FeedbackLearningService, 'active', lambda self, project_id: list(preferences))
    class Planner:
        def plan(self, *args, **kwargs):
            revision = kwargs.get('context', {}).get('visual_revision')
            if revision:
                return ScenePlanResult(project.id, tuple(ScenePlan.model_validate(s) for s in revision['scenes']))
            return ScenePlanResult(project.id, (scene,))

    with Database(path) as db:
        original = generate_scene_plan(db, project, ScenePlanRequest(topic='码率'), Planner(), None, 'first')
        preferences.append({'id': str(uuid4()), 'version': 1, 'rule': FeedbackLearningService.rule,
                            'authority_source': 'creator_selection'})
        revised = generate_scene_plan(db, project, ScenePlanRequest(topic='码率'), Planner(), None,
            'revision', visual_revision={'scenes': [s.model_dump(mode='json') for s in original.scenes]})
        fresh = generate_scene_plan(db, project, ScenePlanRequest(topic='码率'), Planner(), None, 'fresh')
    assert revised.scenes == original.scenes
    assert fresh.scenes[0].caption_emphasis == ['不一定越清晰']
    assert any(ref.startswith('planning_preference:') for ref in fresh.scenes[0].evidence_refs)


@pytest.mark.parametrize('cached',[False,True])
def test_automatic_material_capability_guard_also_checks_provider_replay(tmp_path,monkeypatch,cached):
    from app.domain.models import MaterialRequest
    from app.scene_planning import generate_scene_plan,ScenePlanRequest,ScenePlanResponse
    from app.providers.scene_planner import ScenePlannerProviderResponseError
    path=tmp_path/'material-capability.sqlite'; project=_project(path)
    scene=ScenePlan(project_id=project.id,scene_id='opening',order=0,purpose='解释画质',
        voice_text='码率越高不一定越清晰。',duration_target_ms=3000,
        visual_intent=VisualIntent(subject='编码画面'),preferred_sources=['ai_broll'],
        material_request=MaterialRequest(source_kind='ai_broll',query='编码画面',reason='展示编码'))
    raw=ScenePlanResponse(project_id=project.id,scenes=(scene,),script=scene.voice_text,generated_script=True)
    class Planner:
        def plan(self,*args,**kwargs):
            assert not cached
            return ScenePlanResult(project.id,(scene,))
    if cached:
        monkeypatch.setattr('app.scene_planning.runtime_provider_identity',lambda p:object())
        monkeypatch.setattr('app.scene_planning.ProviderExecutionService.execute',lambda *args,**kwargs:raw)
    with Database(path) as db:
        with pytest.raises(ScenePlannerProviderResponseError) as error:
            generate_scene_plan(db,project,ScenePlanRequest(topic='码率',persist=True),Planner(),None,
                'material-capability',automatic=True)
        assert error.value.domain_field=='material_request'


@pytest.mark.parametrize('cached',[False,True])
def test_automatic_factor_labels_follow_explicit_narration_without_rewriting_accounted_result(tmp_path,monkeypatch,cached):
    from app.domain.models import GraphicCardPlan
    from app.scene_planning import generate_scene_plan,ScenePlanRequest,ScenePlanResponse
    path=tmp_path/'factor-labels.sqlite';project=_project(path)
    scene=ScenePlan(project_id=project.id,scene_id='factors',order=0,purpose='解释画质',
        voice_text='码率、编码效率和片源清晰度，三者互相制约。',duration_target_ms=5800,
        visual_intent=VisualIntent(subject='画质因素'),preferred_sources=['typography'],
        graphic_plan=GraphicCardPlan(kind='diagram',treatment='diagram_factors',
            points=['码率','编码效率','片源清晰度','平台二次压缩']))
    raw=ScenePlanResponse(project_id=project.id,scenes=(scene,),script=scene.voice_text,generated_script=True)
    class Planner:
        def plan(self,*args,**kwargs):
            assert not cached
            return ScenePlanResult(project.id,(scene,))
    if cached:
        monkeypatch.setattr('app.scene_planning.runtime_provider_identity',lambda p:object())
        monkeypatch.setattr('app.scene_planning.ProviderExecutionService.execute',lambda *args,**kwargs:raw)
    with Database(path) as db:
        result=generate_scene_plan(db,project,ScenePlanRequest(topic='画质因素',persist=True),
            Planner(),None,'factor-consistency',automatic=True)
    assert result.draft.scenes[0].graphic_plan.points==['码率','编码效率','片源清晰度']
    assert result.script==result.draft.script==scene.voice_text
    assert scene.graphic_plan.points==raw.scenes[0].graphic_plan.points==['码率','编码效率','片源清晰度','平台二次压缩']


def test_automatic_planning_receives_pool_capacity_without_narration_character_limit(tmp_path,monkeypatch):
    from app.domain.models import GraphicCardPlan
    from app.scene_planning import generate_scene_plan,ScenePlanRequest
    path=tmp_path/'motion-context.sqlite';project=_project(path)
    provider=object()
    def feasibility(db,project_id,*,talking_provider=None):
        assert talking_provider is provider
        return {'talking_motion_pool':{'usable_total_ms':42000,'longest_continuous_ms':30000,
            'max_narration_duration_ms':10000}}
    monkeypatch.setattr('app.scene_planning.planning_feasibility',feasibility)
    class Planner:
        def plan(self,*args,context,**kwargs):
            controls=context['continuous_production']
            motion=controls['talking_motion_pool']
            assert motion['usable_total_ms']==42000 and motion['longest_continuous_ms']==30000
            assert 'talking_motion_budget' not in controls
            assert '不约束文案长度' in controls['creator_motion']
            return ScenePlanResult(project.id,(ScenePlan(project_id=project.id,scene_id='flow',order=0,purpose='解释',
                voice_text='输入先经过处理，然后输出。',duration_target_ms=3000,
                visual_intent=VisualIntent(subject='处理过程'),preferred_sources=['typography'],
                graphic_plan=GraphicCardPlan(kind='diagram',treatment='diagram_flow',points=['输入','输出'])),))
    with Database(path) as db:
        result=generate_scene_plan(db,project,ScenePlanRequest(topic='处理过程'),Planner(),None,
            'motion-hints',automatic=True,material_clip_ids=(),talking_provider=provider)
    assert result.scenes


def test_scene_planner_uses_selected_key_file_without_inherited_key_fallback(tmp_path, monkeypatch):
    from app.providers.scene_planner import ScenePlannerConfigurationError
    key_file = tmp_path / "provider-key.txt"
    key_file.write_text("\ufefffile-only-secret\n", encoding="utf-8")
    monkeypatch.setenv("CONTENT_OS_LLM_API_KEY_FILE", str(key_file))
    monkeypatch.setenv("CONTENT_OS_LLM_API_KEY", "inherited-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "inherited-anthropic-secret")
    monkeypatch.setenv("CONTENT_OS_LLM_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("CONTENT_OS_LLM_MODEL", "deepseek-flash")
    monkeypatch.setenv("CONTENT_OS_LLM_PROTOCOL", "chat_completions")
    planner = _scene_planner_from_env()
    assert planner._api_key == "file-only-secret" and planner.provider_name == "openai-compatible"
    key_file.unlink()
    with pytest.raises(ScenePlannerConfigurationError) as error:
        _scene_planner_from_env()
    assert "secret" not in str(error.value)


def test_planning_can_choose_other_described_shots_from_a_matched_source_without_claiming_relevance(tmp_path):
    from app.db import AssetRepository,ClipRepository
    from app.domain.models import Asset,Clip
    from app.scene_planning import _scene_planning_context
    path=tmp_path/'source-options.sqlite';project=_project(path)
    with Database(path) as db:
        asset=AssetRepository(db).create(Asset(source_file='source.mp4',content_hash='a'*64,duration_ms=30000,
            width=1920,height=1080,fps=project.fps,authorization_reference='fixture',imported_at=project.created_at))
        match=ClipRepository(db).create(Clip(asset_id=asset.id,start_ms=0,end_ms=10000,asset_duration_ms=30000,visual_description='Bitrate quality comparison'))
        supporting=ClipRepository(db).create(Clip(asset_id=asset.id,start_ms=10000,end_ms=20000,asset_duration_ms=30000,visual_description='Hand holding a camera'))
        unknown=ClipRepository(db).create(Clip(asset_id=asset.id,start_ms=20000,end_ms=30000,asset_duration_ms=30000))
        context,_=_scene_planning_context(db,project,topic='Bitrate')
        assert [m['clip_id'] for m in context['materials']]==[str(match.id),str(supporting.id)]
        assert context['materials'][1]['evidence_level']=='same_source_visual_option_not_topic_match'
        assert str(unknown.id) not in str(context['materials'])
        assert _scene_planning_context(db,project,topic='unrelated quasar')[0]['materials']==[]


def test_normal_api_sends_small_topic_brief_and_router_keeps_full_local_clips(tmp_path):
    from app.db import AssetRepository, ClipRepository
    from app.domain.models import Asset, Clip
    from app.routing import AssetRouter
    from app.search import ClipTextSearchService
    from app.main import _scene_planning_context
    path = tmp_path / 'small-context.sqlite'
    project = _project(path)
    private = 'PRIVATE_FULL_TRANSCRIPT ' * 3000
    stored_clips = []
    with Database(path) as db:
        profile = IPProfileRepository(db).get(project.ip_profile_id)
        IPProfileRepository(db).update(profile.model_copy(update={
            'creator_name':'PRIVATE_NAME','knowledge':[private], 'metadata':{'secret':'PRIVATE_KEY'},
            'audience':'builders contact me@example.com', 'style_notes':['call +8613812345678']}))
        for i in range(12):
            metadata = {'r1_usage':'production'}
            if i == 0: metadata['r1_usage'] = 'unknown'
            if i == 10: metadata['project_id'] = str(uuid4())
            if i == 11: metadata['r1_usage'] = 'unknown'
            asset = Asset(source_file=f'local-{i}.mp4',content_hash=f'{i:064x}',duration_ms=10000,
                width=1280,height=720,fps=project.fps,authorization_reference='synthetic approved source',
                imported_at=project.created_at,metadata=metadata)
            AssetRepository(db).create(asset)
            clip = Clip(asset_id=asset.id,start_ms=0,end_ms=10000,asset_duration_ms=10000,
                visual_description='Backpack balance demonstration' if i < 4 or i >= 10 else 'Unrelated kitchen cooking video',
                transcript=private)
            ClipRepository(db).create(clip)
            stored_clips.append(clip)
            gap,_ = _scene_planning_context(db,project,topic='video quasar spectroscopy')
        assert gap['materials'] == [] and gap['material_gap']
    bodies = []
    class Transport:
        def post(self,url,headers,body,timeout_seconds):
            bodies.append(body)
            return _runtime_plan_response()
    planner = OpenAICompatibleScenePlanner('fixture-key',transport=Transport())
    with TestClient(create_app(path,scene_planner=planner)) as client:
        client.put(f'/projects/{project.id}/budget',json={'max_calls':1,'allow_unknown_cost':True}).raise_for_status()
        response = client.post(f'/projects/{project.id}/scene-plan',json={
            'topic':'Backpack balance。Unrelated kitchen cooking is not a substitute.','persist':True})
        assert response.status_code == 200 and response.json()['draft']['scenes']
    text = json.loads(bodies[0])['input'][0]['content'][0]['text']
    # Bound the variable project context separately from the fixed instructions and schema.
    assert len(text.split('Project request:\n', 1)[1].encode('utf-8')) < 6000
    assert len(bodies[0]) < 20000
    assert all(v not in text for v in ['PRIVATE_NAME','PRIVATE_KEY','PRIVATE_FULL_TRANSCRIPT','me@example.com','13812345678'])
    sent_context = json.loads(text.split('Project request:\n',1)[1])['background_evidence']
    assert all('Unrelated kitchen' not in m['summary'] for m in sent_context['materials'])
    assert '[redacted]' in text and 'lexical_hint_not_source_admission' in text
    with Database(path) as db:
        context,refs = _scene_planning_context(db,project,topic='Backpack balance')
        assert len(context['materials']) == 3 and context['material_gap'] is None
        assert all(len(m['summary']) <= 240 and 'transcript' not in m for m in context['materials'])
        assert all(f"clip:{m['clip_id']}" in refs for m in context['materials'])
        assert all(ClipRepository(db).get(c.id) == c for c in stored_clips)
        assert all(AssetRepository(db).get(UUID(m['asset_id'])).metadata.get('project_id') is None
                   for m in context['materials'])
        scene = ScenePlan(project_id=project.id,scene_id='packing',order=0,purpose='demonstrate',voice_text='Backpack balance',
            duration_target_ms=3000,visual_intent=VisualIntent(subject='backpack',action='balance'),preferred_sources=[SourceKind.USER_ASSET])
        route = AssetRouter(ClipTextSearchService(db),AssetRepository(db), max_candidates=20).route(scene)
        assert route.candidates[0].source_kind == SourceKind.USER_ASSET
        assert any(candidate.clip_id == stored_clips[0].id for candidate in route.candidates)


def _project(path: Path) -> Project:
    db = Database(path)
    profile = IPProfile(creator_name="Creator")
    IPProfileRepository(db).create(profile)
    project = Project(
        ip_profile_id=profile.id, title="Practical tutorial", topic="When to avoid vibe coding",
        fps=RationalFps(numerator=30, denominator=1), created_at=datetime.now(timezone.utc),
    )
    ProjectRepository(db).create(project)
    db.close()
    return project


@pytest.mark.parametrize('cached',[False,True])
def test_shared_planning_rejects_subjectless_opening_before_saving_including_cached_result(tmp_path,monkeypatch,cached):
    from app.scene_planning import ScenePlanRequest,ScenePlanResponse,generate_scene_plan
    from app.providers.scene_planner import ScenePlannerProviderResponseError
    from app.db import ProjectDraftRepository
    path=tmp_path/'opening.sqlite';project=_project(path)
    broken=ScenePlan(project_id=project.id,scene_id='opening',order=0,purpose='提出问题',voice_text='越高越清晰吗',
        duration_target_ms=2000,visual_intent=VisualIntent(subject='码率'),preferred_sources=['user_asset'])
    class Planner:
        provider_name='fixture';model='fixture'
        def plan(self,*args,**kwargs):
            assert not cached
            return ScenePlanResult(project_id=project.id,scenes=(broken,))
    if cached:
        monkeypatch.setattr('app.scene_planning.runtime_provider_identity',lambda planner:object())
        monkeypatch.setattr('app.scene_planning.ProviderExecutionService.execute',lambda *args,**kwargs:
            ScenePlanResponse(project_id=project.id,scenes=(broken,),script=broken.voice_text,generated_script=True))
    else:monkeypatch.setattr('app.scene_planning.runtime_provider_identity',lambda planner:None)
    with Database(path) as db:
        with pytest.raises(ScenePlannerProviderResponseError) as failure:
            generate_scene_plan(db,project,ScenePlanRequest(topic='视频码率',persist=True),Planner(),None,'no-truncated-opening')
        assert failure.value.domain_rule=='opening_context'
        assert ProjectDraftRepository(db).get(project.id) is None


@pytest.mark.parametrize('cached',[False,True])
def test_automatic_card_guard_applies_to_generated_and_replayed_plans_without_removing_manual_choice(tmp_path,monkeypatch,cached):
    from app.domain.models import GraphicCardPlan
    from app.db import ProjectDraftRepository
    from app.scene_planning import generate_scene_plan,ScenePlanRequest,ScenePlanResponse
    from app.providers.scene_planner import ScenePlannerProviderResponseError
    path=tmp_path/'automatic-card.sqlite3';project=_project(path)
    card=ScenePlan(project_id=project.id,scene_id='card',order=0,purpose='解释',voice_text='先说明码率和画质的区别。',
        duration_target_ms=3000,visual_intent=VisualIntent(subject='码率'),preferred_sources=['typography'],
        visual_requirement='explanatory',visual_requirement_reason='解释概念',
        graphic_plan=GraphicCardPlan(kind='text_card',treatment='headline',points=['码率不是画质']))
    class Planner:
        def plan(self,*args,**kwargs):return ScenePlanResult(project.id,(card,))
    if cached:
        monkeypatch.setattr('app.scene_planning.runtime_provider_identity',lambda p:object())
        monkeypatch.setattr('app.scene_planning.ProviderExecutionService.execute',lambda *args,**kwargs:
            ScenePlanResponse(project_id=project.id,scenes=(card,),script=card.voice_text,generated_script=True))
    with Database(path) as db:
        with pytest.raises(ScenePlannerProviderResponseError):
            generate_scene_plan(db,project,ScenePlanRequest(topic='码率',persist=True),Planner(),None,'auto-card',automatic=True)
        assert ProjectDraftRepository(db).get(project.id) is None
        manual=generate_scene_plan(db,project,ScenePlanRequest(topic='码率',persist=True),Planner(),None,'manual-card')
        assert manual.draft.scenes[0].graphic_plan.kind=='text_card'


def test_scene_plan_api_binds_project_and_returns_structured_scenes(tmp_path: Path) -> None:
    path = tmp_path / "scene-plan.sqlite"
    project = _project(path)
    seen = []

    class Planner:
        def plan(self, candidate, *, script=None, topic=None):
            seen.append((candidate.id, script, topic))
            scene = ScenePlan(
                project_id=candidate.id, scene_id="hook", order=0, purpose="hook",
                voice_text="Vibe coding is not always the answer.", duration_target_ms=3_000,
                visual_intent=VisualIntent(subject="creator", action="using a computer"),
                preferred_sources=[SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET],
                fallback_sources=[SourceKind.TYPOGRAPHY], caption_emphasis=["not always"],
            )
            return ScenePlanResult(candidate.id, (scene,))

    with TestClient(create_app(path, scene_planner=Planner())) as client:
        response = client.post(f"/projects/{project.id}/scene-plan", json={"script": "Opening line", "topic": "Tradeoffs"})
        assert response.status_code == 200
        assert response.json()["project_id"] == str(project.id)
        assert response.json()["scenes"][0]["preferred_sources"][0] == "user_asset"
        assert response.json()["scenes"][0]["evidence_refs"] == [f"ip_profile:{project.ip_profile_id}:v1"]
        assert seen == [(project.id, "Opening line", "Tradeoffs")]


def test_scene_plan_api_missing_project_config_and_secret_field(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "scene-plan-config.sqlite"
    project = _project(path)
    monkeypatch.delenv("CONTENT_OS_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with TestClient(create_app(path)) as client:
        assert client.post(f"/projects/{uuid4()}/scene-plan", json={}).status_code == 404
        assert client.post(f"/projects/{project.id}/scene-plan", json={}).status_code == 503
        assert client.post(f"/projects/{project.id}/scene-plan", json={"api_key": "must-not-persist"}).status_code == 422


def test_scene_planner_runtime_protocol_is_explicitly_configurable(monkeypatch) -> None:
    monkeypatch.setenv("CONTENT_OS_LLM_API_KEY", "runtime-secret")
    monkeypatch.setenv("CONTENT_OS_LLM_BASE_URL", "https://api.moonshot.cn/v1")
    monkeypatch.setenv("CONTENT_OS_LLM_MODEL", "kimi-k3")
    monkeypatch.setenv("CONTENT_OS_LLM_PROTOCOL", "chat_completions")
    monkeypatch.setenv("CONTENT_OS_LLM_REASONING_EFFORT", "low")
    monkeypatch.setenv("CONTENT_OS_LLM_MAX_OUTPUT_TOKENS", "4096")
    monkeypatch.setenv("CONTENT_OS_LLM_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("CONTENT_OS_LLM_CHAT_OUTPUT_MODE", "json_schema")

    planner = _scene_planner_from_env()

    assert planner.protocol == "chat_completions"
    assert planner.model == "kimi-k3"
    assert planner.reasoning_effort == "low"
    assert planner.max_output_tokens == 4096
    assert planner.timeout_seconds == 120
    assert planner.chat_output_mode == "json_schema"


def test_scene_planner_uses_existing_anthropic_runtime_key(monkeypatch) -> None:
    monkeypatch.delenv("CONTENT_OS_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "runtime-secret")
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)

    planner = _scene_planner_from_env()

    assert planner.protocol == "anthropic_messages"
    assert planner.provider_name == "anthropic"
    assert planner.model == "claude-sonnet-4-6"
    assert planner._api_key == "runtime-secret"


@pytest.mark.parametrize("mode,expected", [(None, "json_schema"), ("prompt", "prompt"), ("json_schema", "json_schema")])
def test_moonshot_k26_runtime_uses_non_thinking_structured_plan(monkeypatch, mode, expected) -> None:
    monkeypatch.setenv("CONTENT_OS_LLM_API_KEY", "runtime-secret")
    monkeypatch.setenv("CONTENT_OS_LLM_BASE_URL", "https://api.moonshot.cn/v1")
    monkeypatch.setenv("CONTENT_OS_LLM_MODEL", "kimi-k2.6")
    monkeypatch.setenv("CONTENT_OS_LLM_PROTOCOL", "chat_completions")
    monkeypatch.delenv("CONTENT_OS_LLM_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("CONTENT_OS_LLM_THINKING_TYPE", raising=False)
    monkeypatch.delenv("CONTENT_OS_LLM_CHAT_OUTPUT_MODE", raising=False)
    if mode is not None:
        monkeypatch.setenv("CONTENT_OS_LLM_CHAT_OUTPUT_MODE", mode)
    planner = _scene_planner_from_env()
    assert planner.thinking_type == "disabled"
    assert planner.chat_output_mode == expected


@pytest.mark.parametrize("field,value", [("MAX_OUTPUT_TOKENS", "bad-secret"), ("MAX_OUTPUT_TOKENS", "0"),
    ("TIMEOUT_SECONDS", "bad-secret"), ("TIMEOUT_SECONDS", "nan"), ("REASONING_EFFORT", "bad-secret"), ("CHAT_OUTPUT_MODE", "bad-secret")])
def test_scene_planner_invalid_environment_fails_safely(monkeypatch, field, value):
    from app.providers.scene_planner import ScenePlannerConfigurationError
    monkeypatch.setenv("CONTENT_OS_LLM_API_KEY", "private-key")
    monkeypatch.setenv("CONTENT_OS_LLM_PROTOCOL", "chat_completions")
    monkeypatch.setenv(f"CONTENT_OS_LLM_{field}", value)
    with pytest.raises(ScenePlannerConfigurationError) as raised:
        _scene_planner_from_env()
    assert "bad-secret" not in str(raised.value) and "private-key" not in str(raised.value)


def _runtime_plan_response(status: int = 200) -> ScenePlannerHTTPResponse:
    if status != 200:
        return ScenePlannerHTTPResponse(status, b"{}", {})
    scene = {
        "scene_id": "runtime-hook",
        "order": 0,
        "purpose": "hook",
        "voice_text": "A runtime planner result.",
        "duration_target_ms": 3_000,
        "visual_intent": {"subject": "creator", "action": "speaking", "framing": "medium", "description": "creator speaking to camera"},
        "preferred_sources": ["user_asset"],
        "fallback_sources": ["typography"],
        "caption_emphasis": ["runtime"],
    }
    body = {"output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps({"scenes": [scene]})}]}]}
    return ScenePlannerHTTPResponse(200, json.dumps(body).encode(), {})


def test_runtime_scene_plan_is_blocked_before_provider_when_cost_is_unknown(tmp_path: Path) -> None:
    project = _project(tmp_path / "scene-plan-budget-block.sqlite")
    calls = []

    class Transport:
        def post(self, *args):
            calls.append(args)
            return _runtime_plan_response()

    planner = OpenAICompatibleScenePlanner("runtime-key", transport=Transport())
    with TestClient(create_app(tmp_path / "scene-plan-budget-block.sqlite", scene_planner=planner)) as client:
        response = client.post(f"/projects/{project.id}/scene-plan", json={"topic": "Budgeted runtime"})
        assert response.status_code == 409
        assert response.json()["detail"] == "unknown_cost"
        assert calls == []
        assert client.get(f"/projects/{project.id}/provider-calls").json() == []


def test_runtime_scene_plan_reconciles_success_and_provider_failure(tmp_path: Path) -> None:
    path = tmp_path / "scene-plan-budget-reconcile.sqlite"
    project = _project(path)

    class Transport:
        status = 200

        def post(self, *args):
            return _runtime_plan_response(self.status)

    transport = Transport()
    planner = OpenAICompatibleScenePlanner("runtime-key", transport=transport)
    with TestClient(create_app(path, scene_planner=planner)) as client:
        assert client.put("/budget", json={"allow_unknown_cost": True}).status_code == 200
        success = client.post(f"/projects/{project.id}/scene-plan", json={"topic": "Budgeted runtime"})
        assert success.status_code == 200
        records = client.get(f"/projects/{project.id}/provider-calls").json()
        assert len(records) == 1
        assert records[0]["operation"] == "scene_planning"
        assert records[0]["status"] == "completed"
        assert records[0]["actual_cost"] is None
        assert records[0]["usage_observable"] is False
        records_before_retry_id = records[0]["id"]

        transport.status = 429
        failed = client.post(f"/projects/{project.id}/scene-plan", json={"topic": "Budgeted runtime retry"})
        assert failed.status_code == 503
        records = client.get(f"/projects/{project.id}/provider-calls").json()
        assert len(records) == 2
        # Stored timestamps can tie; UUID order is not execution order.
        failure = next(record for record in records if record["id"] != records_before_retry_id)
        assert failure["status"] == "failed"
        assert failure["error_code"] == "scene_planner_temporarily_unavailable"



def test_runtime_scene_plan_replays_one_completed_result_and_rejects_changed_retry_input(tmp_path: Path) -> None:
    path = tmp_path / "scene-plan-idempotency.sqlite"
    project = _project(path)
    calls: list[object] = []

    class Transport:
        def post(self, *args):
            calls.append(args)
            return _runtime_plan_response()

    planner = OpenAICompatibleScenePlanner("runtime-key", transport=Transport(), max_output_tokens=4096)
    with TestClient(create_app(path, scene_planner=planner)) as client:
        assert client.put("/budget", json={"allow_unknown_cost": True, "max_calls": 1}).status_code == 200
        headers = {"Idempotency-Key": "same-planning-request"}
        first = client.post(f"/projects/{project.id}/scene-plan", json={"topic": "First topic"}, headers=headers)
        replay = client.post(f"/projects/{project.id}/scene-plan", json={"topic": "First topic"}, headers=headers)
        conflict = client.post(f"/projects/{project.id}/scene-plan", json={"topic": "Changed topic"}, headers=headers)

        assert first.status_code == replay.status_code == 200
        assert replay.json() == first.json()
        assert conflict.status_code == 409
        assert conflict.json()["detail"] == "provider_idempotency_conflict"
        assert len(calls) == 1
        assert json.loads(calls[0][2])["max_output_tokens"] == 4096
        prompt = json.loads(calls[0][2])["input"][0]["content"][0]["text"]
        context = json.loads(prompt.split("Project request:\n", 1)[1])["production_controls"]
        brief = context["production_feasibility"]
        assert brief["authority"] == "planning_hint_not_admission_or_authorization"
        assert brief["live_usage_observation"][0]["calls"] == 0
        assert brief["capabilities"]["talking"]["dispatch_authorized"] is False
        records = client.get(f"/projects/{project.id}/provider-calls").json()
        assert len(records) == 1
        assert records[0]["status"] == "completed"
        assert records[0]["result_payload"]["result"]["project_id"] == str(project.id)
        assert client.put("/budget", json={"allow_unknown_cost": True, "max_calls": 2}).status_code == 200
        changed_policy = client.post(f"/projects/{project.id}/scene-plan", json={"topic": "First topic"}, headers=headers)
        assert changed_policy.status_code == 409
        assert changed_policy.json()["detail"] == "provider_idempotency_conflict"
        assert len(calls) == 1


def test_truncated_scene_plan_preserves_draft_and_records_failed_attempt(tmp_path):
    path = tmp_path / "scene-plan-truncated.sqlite"
    project = _project(path)
    calls = []
    class Transport:
        def post(self, *args):
            calls.append(args)
            response = _runtime_plan_response()
            value = json.loads(response.body)
            value["status"] = "incomplete"
            return ScenePlannerHTTPResponse(200, json.dumps(value).encode(), {})
    planner = OpenAICompatibleScenePlanner("runtime-key", transport=Transport(), max_output_tokens=100)
    with TestClient(create_app(path, scene_planner=planner)) as client:
        before = client.get(f"/projects/{project.id}/draft").json()
        assert client.put("/budget", json={"allow_unknown_cost": True}).status_code == 200
        response = client.post(f"/projects/{project.id}/scene-plan", json={"persist": True})
        assert response.status_code == 502
        after = client.get(f"/projects/{project.id}/draft").json()
        # An unsaved empty draft is synthesized at read time, including now().
        assert {k: v for k, v in after.items() if k != "updated_at"} == {k: v for k, v in before.items() if k != "updated_at"}
        records = client.get(f"/projects/{project.id}/provider-calls").json()
        assert len(records) == len(calls) == 1
        assert records[0]["status"] == "failed"
        assert records[0]["usage_observable"] is False
    from app.db import ProjectDraftRepository
    db = Database(path)
    assert ProjectDraftRepository(db).get(project.id) is None
    db.close()


@pytest.mark.parametrize("valid", [True, False])
def test_chat_schema_mode_uses_normal_persistence_and_failure_gate(tmp_path, valid, caplog):
    path = tmp_path / "chat-schema-api.sqlite"
    project = _project(path)
    requests = []
    class Transport:
        def post(self, url, headers, body, timeout):
            requests.append(json.loads(body))
            content = json.loads(_runtime_plan_response().body)["output"][0]["content"][0]["text"]
            if not valid:
                document = json.loads(content)
                document["scenes"][0]["private-untrusted-field"] = "private-provider-value"
                content = json.dumps(document)
            return ScenePlannerHTTPResponse(200, json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": content}}]}).encode(), {})
    planner = OpenAICompatibleScenePlanner("private-key", protocol="chat_completions", chat_output_mode="json_schema", transport=Transport())
    with TestClient(create_app(path, scene_planner=planner)) as client:
        assert client.put("/budget", json={"allow_unknown_cost": True, "max_calls": 1}).status_code == 200
        response = client.post(f"/projects/{project.id}/scene-plan", json={"persist": True}, headers={"Idempotency-Key": "chat-schema-test"})
        assert response.status_code == (200 if valid else 502)
        draft = client.get(f"/projects/{project.id}/draft").json()
        assert len(draft["scenes"]) == (1 if valid else 0)
        records = client.get(f"/projects/{project.id}/provider-calls").json()
        assert len(records) == len(requests) == 1
        assert records[0]["status"] == ("completed" if valid else "failed")
        assert requests[0]["response_format"]["json_schema"]["strict"] is True
        if not valid:
            assert records[0]["error_code"] == "scene_planner_response_invalid"
            assert "validation_code=scene_shape" in caplog.text
        assert "private-key" not in caplog.text and "private-provider-value" not in caplog.text


def test_effective_chat_output_mode_is_part_of_scene_plan_idempotency(tmp_path):
    path = tmp_path / "chat-mode-identity.sqlite"
    project = _project(path)
    requests = []
    class Transport:
        def post(self, url, headers, body, timeout):
            requests.append(json.loads(body))
            content = json.loads(_runtime_plan_response().body)["output"][0]["content"][0]["text"]
            return ScenePlannerHTTPResponse(200, json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": content}}]}).encode(), {})
    def planner(mode):
        return OpenAICompatibleScenePlanner(
            "fixture-key", base_url="https://api.moonshot.cn/v1", model="kimi-k2.6",
            protocol="chat_completions", chat_output_mode=mode, transport=Transport(),
        )
    app = create_app(path, scene_planner=planner(None))
    with TestClient(app) as client:
        client.put("/budget", json={"allow_unknown_cost": True, "max_calls": 2}).raise_for_status()
        headers = {"Idempotency-Key": "same-effective-mode"}
        first = client.post(f"/projects/{project.id}/scene-plan", json={}, headers=headers)
        assert first.status_code == 200
        app.state.scene_planner = planner("json_schema")
        replay = client.post(f"/projects/{project.id}/scene-plan", json={}, headers=headers)
        assert replay.status_code == 200 and replay.json() == first.json()
        app.state.scene_planner = planner("prompt")
        changed = client.post(f"/projects/{project.id}/scene-plan", json={}, headers=headers)
        assert changed.status_code == 409
        assert changed.json()["detail"] == "provider_idempotency_conflict"
        assert len(requests) == len(client.get(f"/projects/{project.id}/provider-calls").json()) == 1


def test_domain_rule_failure_is_private_durable_and_preserves_saved_draft(tmp_path, caplog):
    path = tmp_path / "domain-rule.sqlite"
    project = _project(path)
    requests = []
    class Transport:
        def post(self, url, headers, body, timeout):
            requests.append(json.loads(body))
            content = json.loads(_runtime_plan_response().body)["output"][0]["content"][0]["text"]
            document = json.loads(content)
            document["scenes"][0]["order"] = 1
            document["scenes"][0]["scene_id"] = "private-provider-source"
            return ScenePlannerHTTPResponse(200, json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(document)}}]}).encode(), {})
    planner = OpenAICompatibleScenePlanner("private-key", protocol="chat_completions", transport=Transport())
    with TestClient(create_app(path, scene_planner=planner)) as client:
        client.put("/budget", json={"allow_unknown_cost": True}).raise_for_status()
        client.put(f"/projects/{project.id}/draft", json={"script": "Retained copy", "topic": project.topic}).raise_for_status()
        before = client.get(f"/projects/{project.id}/draft").json()
        response = client.post(f"/projects/{project.id}/scene-plan", json={"persist": True})
        assert response.status_code == 502
        assert client.get(f"/projects/{project.id}/draft").json() == before
        records = client.get(f"/projects/{project.id}/provider-calls").json()
        assert len(records) == len(requests) == 1
        assert records[0]["status"] == "failed" and records[0]["error_code"] == "scene_planner_response_invalid"
        assert "domain_rule=scene_order" in caplog.text
        assert "private-key" not in caplog.text and "private-provider-source" not in caplog.text


def test_explanatory_first_graphic_persists_and_routes_without_waiving_voice(tmp_path):
    path = tmp_path / "explanatory-exception.sqlite"
    project = _project(path)
    requests = []
    class Transport:
        def post(self, url, headers, body, timeout):
            requests.append(json.loads(body))
            content = json.loads(_runtime_plan_response().body)["output"][0]["content"][0]["text"]
            document = json.loads(content)
            document["scenes"][0].update(preferred_sources=["typography"],
                fallback_sources=["user_asset", "historical_asset"], visual_requirement="explanatory",
                visual_requirement_reason="A concise concept card preserves the editorial point.", caption_emphasis=["Concept"],
                visual_intent={"subject": "concept", "action": None, "framing": None, "description": "A short semantic concept card."})
            return ScenePlannerHTTPResponse(200, json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(document)}}]}).encode(), {})
    planner = OpenAICompatibleScenePlanner("key", protocol="chat_completions", transport=Transport())
    with TestClient(create_app(path, scene_planner=planner)) as client:
        client.put("/budget", json={"allow_unknown_cost": True}).raise_for_status()
        response = client.post(f"/projects/{project.id}/scene-plan", json={"persist": True})
        assert response.status_code == 200, response.text
        draft = client.get(f"/projects/{project.id}/draft").json()
        assert draft["scenes"][0]["preferred_sources"] == ["typography"]
        assert draft["scenes"][0]["fallback_sources"] == ["user_asset", "historical_asset"]
        preflight = client.post(f"/projects/{project.id}/production-preflight", json={})
        assert preflight.status_code == 200, preflight.text
        result = preflight.json()
        assert result["scenes"][0]["planned_source_kind"] == "typography"
        assert result["preliminary_edit_plan"] is not None
        assert result["status"] == "blocked" and result["dispatch_performed"] is False
        assert "voice_capability_not_verified" in result["stop_reasons"]
        records = client.get(f"/projects/{project.id}/provider-calls").json()
        assert len(records) == len(requests) == 1 and records[0]["status"] == "completed"


@pytest.mark.parametrize("replay", [False, True])
def test_control_leak_preserves_draft_and_cannot_bypass_via_accounted_replay(tmp_path, replay, caplog):
    from test_creator_preference_selection import select
    from test_feedback_learning import change
    from test_planning_input import LEAKED_VOICE
    from app.db import ProviderCallRepository
    path = tmp_path / "control-leak.sqlite"
    project = _project(path)
    calls = []
    class Transport:
        def post(self, *args):
            calls.append(args)
            response = _runtime_plan_response()
            if not replay:
                value = json.loads(response.body)
                document = json.loads(value["output"][0]["content"][0]["text"])
                document["scenes"][0]["voice_text"] = LEAKED_VOICE
                value["output"][0]["content"][0]["text"] = json.dumps(document)
                response = ScenePlannerHTTPResponse(200, json.dumps(value).encode(), {})
            return response
    planner = OpenAICompatibleScenePlanner("private-key", transport=Transport())
    with TestClient(create_app(path, scene_planner=planner)) as client:
        root = f"/projects/{project.id}"
        enabled = change(client, project.id, select(client, project.id))
        assert enabled.status_code == 200
        client.put("/budget", json={"allow_unknown_cost": True}).raise_for_status()
        headers = {"Idempotency-Key": "control-boundary"}
        if replay:
            first = client.post(root + "/scene-plan", json={"persist": True}, headers=headers)
            assert first.status_code == 200
            # Synthetic accounted pre-policy output, not a second provider call.
            with Database(path) as db:
                repo = ProviderCallRepository(db)
                record = repo.list_for_project(project.id)[0]
                payload = json.loads(json.dumps(record.result_payload))
                payload["result"]["scenes"][0]["voice_text"] = LEAKED_VOICE
                payload["result"]["script"] = LEAKED_VOICE
                repo.update(record.model_copy(update={"result_payload": payload}))
        before = client.get(root + "/draft").json()
        response = client.post(root + "/scene-plan", json={"persist": True}, headers=headers)
        assert response.status_code == 502
        after = client.get(root + "/draft").json()
        assert {k:v for k,v in before.items() if k != "updated_at"} == {k:v for k,v in after.items() if k != "updated_at"}
        records = client.get(root + "/provider-calls").json()
        assert len(calls) == len(records) == 1
        assert records[0]["status"] == ("completed" if replay else "failed")
        if not replay:
            assert records[0]["error_code"] == "scene_planner_response_invalid"
            assert "domain_rule=audience_control_leak" in caplog.text
        assert LEAKED_VOICE not in caplog.text and "private-key" not in caplog.text
