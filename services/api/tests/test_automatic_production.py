"""一次 HTTP 输入，现有 Worker 自动推进；提供商替身不代表真实成片质量。"""
from datetime import datetime,timedelta,timezone
import hashlib,json,wave
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from uuid import UUID,uuid4

import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from app.db import (Database,JobRepository,ProjectRepository,ProjectDraftRepository,AudioAssetRepository,AssetRepository,
    ClipRepository,ProviderCallRepository)
from app.domain.models import (AudioAsset,Asset,Clip,JobStatus,JobType,ScenePlan,VisualIntent,GraphicCardPlan,TranscriptSegment,RationalFps,ProjectFormat)
from app.jobs import JobRunner,JobStore
from app.jobs.handlers import TalkingQaJobHandler
from app.production_runs import ProductionRunService
from app.video_production import VideoProductionService,ContentPlanJobHandler
from app.source_usage import SourceUsageService,SourceUsageConflict
from app.worker_cli import _run_product_queue
from app.costs import UnknownProviderCostEstimator
from app.media.ffprobe import ProbeMetadata
from test_production_runs import _talking_source_fixture,_voice_dispatch_evidence


@pytest.mark.parametrize("policy,initial,expected",[("auto","vertical","vertical"),("auto","horizontal","vertical"),("fixed","horizontal","horizontal")])
def test_short_video_defaults_to_portrait_before_planning_and_never_follows_source_aspect(tmp_path,monkeypatch,policy,initial,expected):
    from app.db import IPProfileRepository
    from app.domain.models import IPProfile,Project,ProjectFormat,EditVisualRole
    with Database(tmp_path/'aspect.sqlite') as db:
        now=datetime.now(timezone.utc)
        ip=IPProfileRepository(db).create(IPProfile(creator_name='创作者'))
        project=ProjectRepository(db).create(Project(ip_profile_id=ip.id,title='对比讲解',topic='画质',
            format_policy=policy,format=initial,resolution_width=1920 if initial=='horizontal' else 1080,
            resolution_height=1080 if initial=='horizontal' else 1920,
            fps=RationalFps(numerator=30,denominator=1),created_at=now))
        asset=AssetRepository(db).create(Asset(source_file='wide.mp4',content_hash='a'*64,duration_ms=10000,
            width=1920,height=1080,fps=project.fps,authorization_reference='fixture',imported_at=now))
        comparison=ScenePlan(project_id=project.id,scene_id='comparison',order=0,purpose='并排对比',voice_text='对比画质',duration_target_ms=1000,
            visual_intent=VisualIntent(subject='同原片左右对比'),preferred_sources=['user_asset'],visual_requirement='action_evidence',visual_requirement_reason='比较原片')
        explanation=comparison.model_copy(update={'id':uuid4(),'scene_id':'explain','order':1,'duration_target_ms':9000})
        draft=SimpleNamespace(scenes=[comparison,explanation],script='对比画质对比画质')
        plan=SimpleNamespace(fingerprint='fixture',scenes=[SimpleNamespace(scene_plan_id=comparison.id,visual_role=EditVisualRole.FOOTAGE,
            selected_candidate=SimpleNamespace(asset_id=asset.id),production_need='none')],stop_reasons=())
        def generate(db,chosen,*args,**kwargs):
            assert chosen.format.value==expected
            return SimpleNamespace(draft=draft)
        def preflight(db,chosen,*args,**kwargs):
            assert chosen.format.value==expected
            return plan
        monkeypatch.setattr('app.video_production.generate_scene_plan',generate)
        monkeypatch.setattr('app.video_production.build_production_preflight',preflight)
        service=VideoProductionService(db,tmp_path)
        service.runs.start=lambda *args,**kwargs:SimpleNamespace(project_id=project.id,id=uuid4())
        service.advance=lambda *args:None
        job=SimpleNamespace(project_id=project.id,id=uuid4(),payload=SimpleNamespace(topic=project.topic,candidate_of_run_id=None))
        for _ in range(2):
            service.plan(job,object(),object())
            saved=ProjectRepository(db).get(project.id)
            assert saved.format.value==expected
            assert (saved.resolution_width,saved.resolution_height)==((1920,1080) if expected=='horizontal' else (1080,1920))


def test_planning_failure_reports_the_blocking_visual_gap_instead_of_a_normal_unbound_talking_stage(tmp_path):
    from app.domain.models import Job,ContentPlanJobPayload
    from app.jobs.runner import JobExecutionError
    from app.production_runs import ProductionRunNotReady
    class Failed:
        root=tmp_path
        def plan(self,*args):raise ProductionRunNotReady(('talking_source_not_bound:creator','capture_required:evidence'))
    now=datetime.now(timezone.utc);pid=uuid4()
    job=Job(project_id=pid,type=JobType.PLAN_CONTENT,idempotency_key='visual-gap',created_at=now,updated_at=now,attempt=1,
        payload=ContentPlanJobPayload(project_id=pid,topic='解释'))
    with pytest.raises(JobExecutionError) as error:ContentPlanJobHandler(Failed(),object(),object())(job)
    assert error.value.code=='capture_required:evidence'


def test_invalid_graphic_failure_is_specific_retained_and_never_continues(tmp_path):
    from app.domain.models import Job,ContentPlanJobPayload
    from app.jobs.runner import JobExecutionError
    from app.providers.scene_planner import ScenePlannerProviderResponseError
    class Failed:
        root=tmp_path
        def plan(self,*args):
            error=ScenePlannerProviderResponseError('invalid graphic',validation_code='scene_values',domain_field='graphic_plan')
            error.graphic_diagnostic={'scene_index':0,'kind':'diagram','point_count':1,'point_lengths':[5],'reason_present':False}
            error.response_document={'choices':[{'message':{'content':'{"scenes":[]}'}}]}
            raise error
    now=datetime.now(timezone.utc);pid=uuid4()
    job=Job(project_id=pid,type=JobType.PLAN_CONTENT,idempotency_key='failed',created_at=now,updated_at=now,attempt=1,
        payload=ContentPlanJobPayload(project_id=pid,topic='科普'))
    with pytest.raises(JobExecutionError) as failed:
        ContentPlanJobHandler(Failed(),object(),object())(job)
    assert failed.value.code=='scene_planner_response_invalid:graphic_plan'
    assert not failed.value.retryable
    report=json.loads((tmp_path/'render-diagnostics'/str(job.id)/'attempt-1.json').read_text(encoding='utf-8'))
    assert report['validation']['graphic']['point_count']==1
    assert report['validation']['field']=='graphic_plan'
    assert report['planner_response']['choices'][0]['message']['content']=='{"scenes":[]}'


@pytest.mark.parametrize('exception_name,code', [('VisionTimeout','material_video_analysis_timeout'),
    ('VisionProviderResponseError','material_video_analysis_failed')])
def test_material_service_failure_is_specific_and_never_retries(tmp_path,exception_name,code):
    from app.domain.models import Job,ContentPlanJobPayload
    from app.jobs.runner import JobExecutionError
    from app.providers import vision
    class Failed:
        root=tmp_path
        def plan(self,*args):raise getattr(vision,exception_name)('video analysis service failure')
    now=datetime.now(timezone.utc);pid=uuid4()
    job=Job(project_id=pid,type=JobType.PLAN_CONTENT,idempotency_key='video-analysis-failure',created_at=now,updated_at=now,
        attempt=1,payload=ContentPlanJobPayload(project_id=pid,topic='科普'))
    with pytest.raises(JobExecutionError) as failed:
        ContentPlanJobHandler(Failed(),object(),object())(job)
    assert failed.value.code==code and not failed.value.retryable
    report=json.loads((tmp_path/'render-diagnostics'/str(job.id)/'attempt-1.json').read_text(encoding='utf-8'))
    assert report['exception_type']==exception_name


@pytest.mark.parametrize('diagnostic,expected',[
    ({'phase':'request_upload','elapsed_ms':120000,'request_bytes':5781804,'http_status':429,
        'authorization':'private-token','url':'https://private.invalid/key'},
        {'phase':'request_upload','elapsed_ms':120000,'request_bytes':5781804,'http_status':429}),
    ({'phase':'private-token','elapsed_ms':True,'request_bytes':-1,'http_status':900,'url':'private-token'},None),
])
def test_material_failure_diagnostic_preserves_only_safe_transport_fields(tmp_path,diagnostic,expected):
    from app.domain.models import Job
    from app.jobs.handlers import _write_render_failure_diagnostic
    from app.providers.vision import VisionTimeout
    now=datetime.now(timezone.utc)
    job=Job(type=JobType.PLAN_CONTENT,idempotency_key='transport-failure',created_at=now,updated_at=now,attempt=1)
    error=VisionTimeout('vision request timed out');error.transport_diagnostic=diagnostic
    assert _write_render_failure_diagnostic(job,tmp_path,'content_planning',error)
    text=(tmp_path/'render-diagnostics'/str(job.id)/'attempt-1.json').read_text(encoding='utf-8')
    report=json.loads(text)
    assert report.get('transport')==expected
    assert 'private-token' not in text and 'private.invalid' not in text and 'authorization' not in text


def test_one_input_rejects_insufficient_chain_budget_before_planning(tmp_path):
    path=tmp_path/'budget.sqlite'
    with TestClient(create_app(path)) as client:
        pid,_,_,_=_talking_source_fixture(client,path)
        response=client.post(f'/projects/{pid}/auto-produce',json={'topic':'解释视频压缩','idempotency_key':'insufficient-chain-budget'})
        assert response.status_code==409 and 'budget_chain_call_limit' in response.text
        with Database(path) as db:
            assert db.connection.execute("SELECT COUNT(*) FROM jobs WHERE json_extract(payload,'$.type')='plan_content'").fetchone()[0]==0
            assert ProviderCallRepository(db).list_for_project(UUID(pid))==[]


def _alignment_fixture(db,tmp_path,*,reference_ms,durations):
    from app.db import IPProfileRepository
    from app.domain.models import ContentPlanJobPayload,IPProfile,Job,Project,ProjectDraft
    from app.providers.maintained_talking import MaintainedLatentSyncProvider
    now=datetime.now(timezone.utc)
    ip=IPProfileRepository(db).create(IPProfile(creator_name='创作者'))
    project=ProjectRepository(db).create(Project(ip_profile_id=ip.id,title='码率与画质',topic='解释视频码率',
        fps=RationalFps(numerator=30,denominator=1),created_at=now))
    job=JobRepository(db).create(Job(project_id=project.id,type=JobType.PLAN_CONTENT,
        idempotency_key='alignment-fixture',created_at=now,updated_at=now,
        payload=ContentPlanJobPayload(project_id=project.id,topic=project.topic)))
    copy=['码率越高画面就一定越清晰吗？','码率不能恢复原片已经丢失的细节。']
    scenes=[ScenePlan(project_id=project.id,scene_id=f'person-{index}',order=index,purpose='本人说明',
        voice_text=copy[index],duration_target_ms=duration,visual_intent=VisualIntent(subject='创作者本人'),
        preferred_sources=['ai_video'],visual_requirement='creator_speaking',visual_requirement_reason='本人表达关键判断')
        for index,duration in enumerate(durations)]
    draft=ProjectDraftRepository(db).save(ProjectDraft(project_id=project.id,version=4,script_revision=2,
        topic=project.topic,script='\n\n'.join(scene.voice_text for scene in scenes),scenes=scenes,
        evidence_refs=[f'content_plan_job:{job.id}'],updated_at=now))
    service=VideoProductionService(db,tmp_path,talking_provider=MaintainedLatentSyncProvider)
    reference=SimpleNamespace(start_ms=1200,end_ms=1200+reference_ms)
    service.talking_choice=lambda project_id:(None,reference,None)
    return project,job,draft,service


def test_unregistered_person_source_stops_before_external_acquisition(tmp_path):
    from app.production_runs import ProductionRunNotReady
    with Database(tmp_path/'plan-alignment.sqlite') as db:
        project,job,draft,service=_alignment_fixture(db,tmp_path,reference_ms=3200,durations=[1600,1600])
        events=[]
        class Resolver:
            def check_configuration(self,*args):events.append('configuration')
            def check_external_configuration(self,*args):pytest.fail('已知人物覆盖不足不能继续外取')
            def prepare(self,*args):pytest.fail('已保存的当前稿件不能重做规划')
            def resolve(self,*args,**kwargs):pytest.fail('已知人物覆盖不足不能继续素材核验')
        class Acquirer:
            def acquire(self,*args,**kwargs):pytest.fail('人物覆盖不足必须在新增外取调用前停止')
        service.material_resolver=Resolver();service.material_acquirer=Acquirer()
        with pytest.raises(ProductionRunNotReady) as failed:
            service.plan(job,object(),UnknownProviderCostEstimator())
        assert failed.value.reasons==('creator_talking_reference_not_configured',)
        assert events==['configuration']
        assert ProjectDraftRepository(db).get(project.id)==draft
        assert ProviderCallRepository(db).list_for_project(project.id)==[]
        assert db.connection.execute('SELECT COUNT(*) FROM production_runs').fetchone()[0]==0


def test_measured_voice_alignment_failure_keeps_specific_stop_reason_and_original_draft(tmp_path,monkeypatch):
    from app.production_runs import ProductionRunNotReady
    from app.providers.talking import TalkingInputError
    with Database(tmp_path/'measured-alignment.sqlite') as db:
        project,_,draft,service=_alignment_fixture(db,tmp_path,reference_ms=3199,durations=[1200])
        # 3000ms旁白虽短于3199ms参考，真实适配器的帧窗口仍要求3200ms，不能派发。
        with pytest.raises(TalkingInputError):
            service.talking_provider.validate_reference_duration(3000,3199)
        master=AudioAssetRepository(db).create(AudioAsset(source_file='fixture-measured-voice.wav',
            content_hash='b'*64,duration_ms=3000,sample_rate=24000,channels=1,
            authorization_reference='fixture profile consent',imported_at=datetime.now(timezone.utc),
            transcript_source='fixture measured transcript',
            transcript_segments=[TranscriptSegment(start_ms=0,end_ms=3000,text=draft.script)]))
        def forbidden(*args,**kwargs):pytest.fail('覆盖失败不能改写稿件或继续预检')
        monkeypatch.setattr('app.video_production.save_project_draft',forbidden)
        monkeypatch.setattr('app.video_production.build_production_preflight',forbidden)
        row=dict(id=str(uuid4()),project_id=str(project.id),draft_version=draft.version,
            script_revision=draft.script_revision,voice_audio_id=str(master.id))
        with pytest.raises(ProductionRunNotReady) as failed:
            service._prepare_measured_plan(row)
        assert failed.value.reasons==('creator_talking_reference_not_configured',)
        assert ProjectDraftRepository(db).get(project.id)==draft
        assert ProviderCallRepository(db).list_for_project(project.id)==[]


@pytest.mark.parametrize("opening_ms,qa_missing,silence_tail,with_footage",[(1200,False,0,False),(6000,False,0,False),(1200,True,0,False),(1200,False,160,False),(1200,False,0,True),(1200,False,0,'revise'),(1200,False,0,'motion-reflow'),(1200,False,0,'motion-revision')])
def test_one_input_preserves_person_and_never_downgrades_uncovered_speech_to_graphics(tmp_path,opening_ms,qa_missing,silence_tail,with_footage):
    reflow=with_footage in {'motion-reflow','motion-revision'}
    motion_revision=with_footage=='motion-revision'
    second_ms=6000 if reflow else 1200
    path=tmp_path/"product.sqlite3"
    with TestClient(create_app(path)) as client:
        pid,old,binding,_=_talking_source_fixture(client,path)
        with Database(path) as db:
            clip=ClipRepository(db).get(UUID(binding["reference_clip_id"]))
            asset=AssetRepository(db).get(clip.asset_id)
            AssetRepository(db).update(asset.model_copy(update={"has_audio":False}))
            if with_footage:
                project=ProjectRepository(db).get(UUID(pid))
                footage_file=tmp_path/'existing-workflow-footage.mp4'
                footage_file.write_bytes(b'\x00\x00\x00\x18ftypisom fixture workflow source')
                footage=AssetRepository(db).create(Asset(source_file=str(footage_file),content_hash=hashlib.sha256(footage_file.read_bytes()).hexdigest(),
                    duration_ms=10000,width=1080,height=1920,fps=project.fps,has_audio=False,
                    authorization_reference='fixture intake rights',imported_at=datetime.now(timezone.utc)))
                footage_clip=ClipRepository(db).create(Clip(asset_id=footage.id,
                    start_ms=0,end_ms=10000,asset_duration_ms=10000,visual_description='视频压缩流程图的已有录像'))
        binding.pop("authorization_reference",None)
        assert client.post(f"/projects/{pid}/production-runs/{old['id']}/talking-source-bind",json=binding).status_code==200
        _voice_dispatch_evidence(path)
        # 含实拍6；需要调整时另留规划1＋最多3批核验；无实拍4。
        calls=10 if with_footage=='revise' else 6 if with_footage else 4
        assert client.put(f'/projects/{pid}/budget',json={'currency':'USD','max_calls':calls,'allow_unknown_cost':True}).status_code==200
        started=client.post(f"/projects/{pid}/auto-produce",json={"topic":"解释视频压缩","idempotency_key":"one-topic-input"})
        assert started.status_code==201,started.text
        replay=client.post(f"/projects/{pid}/auto-produce",json={"topic":"解释视频压缩","idempotency_key":"one-topic-input"})
        assert replay.json()["start_job_id"]==started.json()["start_job_id"]
        duplicate=client.post(f"/projects/{pid}/auto-produce",json={"topic":"另一个主题","idempotency_key":"another-start"})
        assert duplicate.status_code==409 and "already_running" in duplicate.text
        project_id=UUID(pid)
        with Database(path) as db:
            db.data_root=tmp_path
            service=VideoProductionService(db,tmp_path,voice_provider=SimpleNamespace(model="fixture-model"))
            stop=Event();steps=[];revised=[False]
            if with_footage:
                from test_material_resolution import FixtureVideoVision,FixtureVideoExtractor,FixtureVideoProbe
                from app.media.planning import VisualMaterialResolver
                service.material_resolver=VisualMaterialResolver(db,tmp_path,FixtureVideoVision(accept=lambda v:True,steps=steps,
                    description='原片中的视频压缩工作流程图'),FixtureVideoExtractor(tmp_path/'analysis-fixtures'),
                    FixtureVideoProbe(),UnknownProviderCostEstimator())
                if with_footage=='revise':
                    original_analysis=service.material_resolver.provider.analyze_intervals
                    def analyze(videos,requests):
                        result=original_analysis(videos,requests)
                        if requests and not revised[0]:
                            for row in result.clips:
                                for match in row.matches:match.supported=False;match.score=.1
                        return result
                    service.material_resolver.provider.analyze_intervals=analyze
            class Planner:
                def plan(self,project,**kwargs):
                    revision=kwargs.get('context',{}).get('visual_revision')
                    if revision:
                        steps.append('revision');revised[0]=True
                        original=[ScenePlan.model_validate_json(json.dumps(row)) for row in revision['scenes']]
                        if revision.get('kind')=='talking_capacity':
                            return SimpleNamespace(project_id=project.id,scenes=[scene.model_copy(update={
                                'supporting_visual_intent':VisualIntent(subject='视频压缩流程图')})
                                if scene.scene_id=='explanation' else scene for scene in original],
                                replaceability_reasons=(('explanation','讲解可由切题录像覆盖'),))
                        return SimpleNamespace(project_id=project.id,scenes=[scene.model_copy(update={
                            'visual_intent':scene.visual_intent.model_copy(update={'subject':'已核验的真实压缩工作流程'})})
                            if scene.scene_id=='explanation' else scene for scene in original])
                    if with_footage:
                        observed=kwargs['context']['materials']
                        assert observed and all(v['visual_evidence'].get('usable_interval') for v in observed)
                        assert steps[0]=='catalogue'
                        steps.append('planning')
                        selected_clip=UUID(observed[0]['clip_id'])
                    return SimpleNamespace(project_id=project.id,scenes=[
                        ScenePlan(project_id=project.id,scene_id="opening",order=0,purpose="hook",voice_text="A new creator claim.",duration_target_ms=opening_ms,
                            visual_intent=VisualIntent(subject="creator",action="speaking"),visual_requirement="creator_speaking",visual_requirement_reason="本人说明观点",
                            preferred_sources=["ai_video"],fallback_sources=["typography"],
                            graphic_plan=GraphicCardPlan(kind="diagram",treatment="diagram_flow",points=["Input","Output"])),
                        ScenePlan(project_id=project.id,scene_id="explanation",order=1,purpose="explanation",voice_text="A useful diagram.",duration_target_ms=second_ms,
                            visual_intent=VisualIntent(subject="creator" if reflow else "workflow"),visual_requirement="creator_speaking" if reflow else "explanatory",visual_requirement_reason="解释因果关系",
                            supporting_visual_intent=VisualIntent(subject="视频压缩流程图") if reflow and not motion_revision else None,
                            preferred_sources=["ai_video"] if reflow else ["user_asset"] if with_footage else ["typography"],fallback_sources=["user_asset"] if reflow else [],
                            preferred_clip_id=selected_clip if with_footage and not reflow else None,
                            graphic_plan=None if with_footage else GraphicCardPlan(kind="diagram",treatment="diagram_flow",points=["Input","Output"]))])
            def voice(job):
                steps.append("voice")
                from app.production_runs import ProductionRunNotReady
                with pytest.raises(ProductionRunNotReady,match="already_running"):
                    service.enqueue(project_id,"同时启动另一个主题","new-while-voice-running")
                target=tmp_path/"new-voice.wav"
                with wave.open(str(target),"wb") as audio:
                    audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(24000);audio.writeframes(b"\x01\x00"*(24*(opening_ms+silence_tail+second_ms)))
                AudioAssetRepository(db).create(AudioAsset(source_file=str(target),content_hash=hashlib.sha256(target.read_bytes()).hexdigest(),
                    duration_ms=opening_ms+silence_tail+second_ms,sample_rate=24000,channels=1,authorization_reference=job.payload.authorization_reference,imported_at=datetime.now(timezone.utc),
                    metadata={"voice_generation":{"job_id":str(job.id),"project_id":pid,"target_text":job.payload.text,
                        "voice_profile_id":str(job.payload.voice_profile_id),"provider":job.payload.expected_provider,"model":job.payload.expected_model,"qa_state":"pending"}}))
            def voice_qa(job):
                steps.append("voice_qa")
                repo=AudioAssetRepository(db);audio=repo.get(job.payload.narration_audio_id);meta=dict(audio.metadata)
                meta["voice_generation"]={**meta["voice_generation"],"qa_state":"verified","human_review_state":"pending",
                    "qa":{"missing_token_count":0,"duplicate_token_count":0,"substitution_token_count":0,"copy_coverage":1,
                        "transcription_scope":"generation_parts","source_part_count":2},
                    "generation_segments":[dict(start_char=0,end_char=job.payload.target_text.index('\n\n'),start_sample=0,end_sample=(opening_ms+silence_tail)*24),
                        dict(start_char=job.payload.target_text.index('\n\n')+2,end_char=len(job.payload.target_text),
                            start_sample=(opening_ms+silence_tail)*24,end_sample=audio.duration_ms*24)]}
                repo.update(audio.model_copy(update={"metadata":meta,"transcript_source":"synthetic-asr",
                    "transcript_segments":[TranscriptSegment(start_ms=0,end_ms=opening_ms,text="A new creator claim."),TranscriptSegment(start_ms=opening_ms+silence_tail,end_ms=opening_ms+silence_tail+second_ms,text="A useful diagram.")]}))
                if qa_missing:
                    from app.jobs.runner import JobExecutionError
                    raise JobExecutionError('voice_qa_failed','synthetic missing word',retryable=False)
            def talking(job):
                steps.append("talking")
                payload=job.payload;context=payload.planned_context
                service.runs.require_talking_job_admission(job,provider=context.expected_provider,model=context.expected_model,
                    runtime=context.expected_runtime,machine_id=context.expected_machine_id)
                target=tmp_path/"talking.mp4";target.write_bytes(b"synthetic talking output")
                AssetRepository(db).create(Asset(source_kind="ai_video",source_file=str(target),content_hash=hashlib.sha256(target.read_bytes()).hexdigest(),
                    duration_ms=1200,width=1080,height=1920,fps=RationalFps(numerator=25,denominator=1),has_audio=True,
                    authorization_reference=payload.authorization_reference,imported_at=datetime.now(timezone.utc),metadata={"talking_generation":{
                        "job_id":str(job.id),"project_id":pid,"planned_context":context.model_dump(mode="json"),"talking_profile_id":str(payload.talking_profile_id),"reference_clip_id":str(payload.reference_clip_id),
                        "narration_audio_id":str(payload.narration_audio_id),"provider":context.expected_provider,"model":context.expected_model,
                        "master_slice_start_ms":0,"master_slice_end_ms":1200,"reference_window_start_ms":payload.reference_window_start_ms,
                        "reference_window_end_ms":payload.reference_window_end_ms,"expected_video_fps":{"numerator":25,"denominator":1},"qa_state":"pending"}}))
            class Probe:
                def probe_video_frames(self, path):
                    # Synthetic frame evidence for this local fixture.
                    return {"frame_count": 30, "duration_ms": 1200,
                        "fps": RationalFps(numerator=25, denominator=1)}
                def probe(self,path):
                    return ProbeMetadata(duration_ms=1200,width=1080,height=1920,fps=RationalFps(numerator=25,denominator=1),has_audio=True,
                        metadata={"streams":[{"codec_type":"video"},{"codec_type":"audio"}]})
            def render(job):
                steps.append("render")
                spec=job.payload.video_spec
                from app.renderer import RemotionRenderer
                renderer=RemotionRenderer(AssetRepository(db),ClipRepository(db),audios=AudioAssetRepository(db),renderer_dir=tmp_path)
                for scene in spec.scenes:
                    renderer._validate_scene(scene,spec.fps,project_id=project_id,master_audio_id=spec.master_narration.audio_asset_id,
                        production_run_id=UUID(service.status(project_id)['run']['id']),validate_scene_narration=False)
                assert [scene.visual_role.value for scene in spec.scenes]==["talking","footage" if with_footage else "graphic"]
                assert spec.scenes[1].graphic_treatment.value==("none" if with_footage else "diagram_flow")
                if with_footage:
                    from app.media.visual_facts import full_interval_geometry
                    source_clip=ClipRepository(db).get(spec.scenes[1].visual.clip_id)
                    facts=full_interval_geometry(AssetRepository(db).get(source_clip.asset_id),source_clip)
                    assert facts['coverage']=='video_interval_analysis'
                assert AudioAssetRepository(db).get(spec.master_narration.audio_asset_id).metadata["voice_generation"]["human_review_state"]=="pending"
            planner=Planner()
            service.planner=planner
            service.planner_cost_estimator=UnknownProviderCostEstimator()
            handlers={JobType.PLAN_CONTENT:ContentPlanJobHandler(service,planner,service.planner_cost_estimator),
                JobType.GENERATE_VOICE:voice,JobType.VERIFY_VOICE:voice_qa,JobType.GENERATE_TALKING:talking,
                JobType.VERIFY_TALKING:TalkingQaJobHandler(AssetRepository(db),data_root=tmp_path,probe=Probe()),JobType.RENDER:render}
            def completed(job):
                service.completed(job)
                if job.type is JobType.RENDER:stop.set()
            runner=JobRunner(JobStore(db),handlers,worker_id="product-worker",lease_duration=timedelta(minutes=1),max_attempts=1,on_completed=completed)
            runner.production_service=service
            # Bound the fixture even if a product transition fails.
            from threading import Timer
            timeout=Timer(3,stop.set);timeout.start()
            try:_run_product_queue(SimpleNamespace(once=False,poll_seconds=.01,data_root=tmp_path),db,runner,stop)
            finally:timeout.cancel()
            status=service.status(project_id)
            diagnostics=[value.read_text(encoding="utf-8") for value in (tmp_path/"render-diagnostics").glob("**/*.json")]
            if opening_ms==6000:
                assert status["status"]=="failed" and status["error_code"]=="talking_motion_capacity_insufficient"
                assert steps==[]  # 不可排的既定人物方案在声音投入前停止。
                assert status['run'] is None
                assert ProjectDraftRepository(db).get(project_id).scenes[0].visual_requirement=="creator_speaking"
                return
            if qa_missing:
                assert status['status']=='voice_qa_failed' and status['error_code']=='voice_qa_failed'
                assert steps==['voice','voice_qa']
                return
            assert status["status"]=="render_completed_awaiting_review",(status,diagnostics)
            expected=["catalogue","planning","material"] if with_footage else []
            if with_footage=='revise':expected+=["revision","material"]
            assert steps==(["catalogue","planning"]+(["revision"] if motion_revision else [])+["material","voice","voice_qa","talking","render"] if reflow else expected+["voice","voice_qa","talking","render"])
            if reflow:
                assert steps.count('voice')==1
                assert ProjectDraftRepository(db).get(project_id).script=='A new creator claim.\n\nA useful diagram.'
                schedule=json.loads((tmp_path/'render-diagnostics'/status['run']['id']/'motion-schedule.json').read_text(encoding='utf-8'))
                assert schedule['supporting_footage_scene_ids']==['explanation']
                assert status['run']['voice_audio_id']==schedule['master_audio_id']
            assert status["run"]["review_state"]=="pending"
            assert all(value["duration_ms"] is not None for value in status["phases"])
            # 修改后续画幅设置不能篡改已有成片的实际尺寸。
            saved_project=ProjectRepository(db).get(project_id)
            ProjectRepository(db).update(saved_project.model_copy(update={"format":ProjectFormat.HORIZONTAL,
                "resolution_width":1920,"resolution_height":1080,"format_policy":"fixed"}))
            displayed=service.status(project_id)
            assert (displayed['output_format'],displayed['output_width'],displayed['output_height'])==(
                status['output_format'],status['output_width'],status['output_height'])
            ProjectRepository(db).update(saved_project)
            assert ProviderCallRepository(db).list_for_project(project_id)==[]
            from app.voice_plan import is_current_run_voice
            audio=AudioAssetRepository(db).get(UUID(status["run"]["voice_audio_id"]))
            assert is_current_run_voice(db,status["run"]["id"],audio)
            assert not is_current_run_voice(db,uuid4(),audio)
            changed=audio.model_copy(update={"metadata":{"voice_generation":{**audio.metadata["voice_generation"],"human_review_state":"rejected"}}})
            assert not is_current_run_voice(db,status["run"]["id"],changed)
            changed=audio.model_copy(update={"metadata":{"voice_generation":{**audio.metadata["voice_generation"],"voice_profile_id":str(uuid4())}}})
            assert not is_current_run_voice(db,status["run"]["id"],changed)
            candidate=service.enqueue(project_id,"解释视频压缩","explicit-comparison",candidate_of_run_id=UUID(status["run"]["id"]))
            comparison=service.plan(candidate,Planner(),UnknownProviderCostEstimator())
            assert str(comparison.id)!=status["run"]["id"]
            assert str(comparison.work_id)==status["run"]["work_id"]
            assert comparison.status=="voice_pending"  # 同作品关系不扩大旧 Voice 的质量结论。
            assert db.connection.execute("SELECT COUNT(*) FROM render_choices").fetchone()[0]==0

