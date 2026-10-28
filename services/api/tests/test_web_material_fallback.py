"""无素材 API 的普通取材契约；网络、媒体探测与视觉结论均为显式夹具。"""
from datetime import datetime, timedelta, timezone
from collections import Counter
from decimal import Decimal
import json
from types import SimpleNamespace
from urllib.error import URLError
from uuid import uuid4

import pytest

from app.db import AssetRepository, BudgetPolicyRepository, ProviderCallRepository
from app.domain.models import BudgetPolicy, CostCategory, MaterialRequest, RationalFps, SourceKind, UsageCost
from app.external_acquisition import BoundedExternalAcquirer, analyze_exact_job, reserve_production_tail
from app.external_media import ExternalMediaService
from app.jobs.handlers import AssetAnalysisJobHandler
from app.jobs.runner import JobRunner
from app.jobs.store import JobStore
from app.jobs.targets import AssetJobTargetStore
from app.media import MediaImporter, ProbeMetadata
from app.media.pipeline import MediaAnalysisPipeline
from app.providers.public_page_media import PublicPageMediaProvider
from app.web_discovery import WebDiscoveryService
from test_material_resolution import material_library
from test_media_pipeline import _StaticSegmenter, _WritingExtractor
from test_public_page_media import PUBLIC, PUBLIC_FILE, Response, public_html


def system(tmp_path, name='deepseek', *, catalogue=False, direct_failure=False, fetch_html=True, saved_html=False, max_calls=30):
    db, project, old, clip, original, vision, resolver = material_library(tmp_path)
    BudgetPolicyRepository(db).save(BudgetPolicy(project_id=project.id, currency='CNY', max_calls=max_calls,
        allow_unknown_cost=True, updated_at=datetime.now(timezone.utc)))
    events=[]
    class Probe:
        def probe(self,path):
            return ProbeMetadata(6000,1080,1920,RationalFps(numerator=30,denominator=1),False,{})
    class Tools:
        provider_name=name
        _api_key='explicit-fixture-secret'
        def cost(self):return UsageCost(category=CostCategory.STOCK,amount=Decimal('0.01'),currency='CNY',provider=name)
        def search(self,query,**kwargs):
            events.append(('web_search',name))
            return {'results':[{'url':'https://open-films.example/search/coffee','title':'Coffee videos'},
                {'url':PUBLIC,'title':'Coffee making','content':public_html() if saved_html else ''}]}
        def fetch(self,url):
            events.append(('web_fetch',name))
            return {'url':url,'title':'Coffee making','content':public_html() if fetch_html else '模型摘要与普通 Markdown 不是源页 JSON-LD'}
    class EmptyCatalogue:
        provider_name='empty-catalogue'
        model='video-api'
        def cost(self,currency):return UsageCost(category=CostCategory.STOCK,amount=Decimal(0),currency=currency,provider=self.provider_name)
        def search(self,query):
            events.append(('catalogue_search',))
            return []
    def opener(request,timeout):
        events.append(('page' if request.full_url==PUBLIC else 'download',request.full_url))
        if request.full_url==PUBLIC:
            if direct_failure:raise URLError('fixture direct network unavailable')
            return Response(public_html().encode())
        assert request.full_url==PUBLIC_FILE
        return Response(b'explicit fixture original video',mime='video/mp4')
    def media_factory(source,selected):
        provider=EmptyCatalogue() if source=='empty-catalogue' else PublicPageMediaProvider(opener=opener)
        return ExternalMediaService(db,tmp_path,provider,MediaImporter(db,tmp_path,Probe()),
            reservation_guard=lambda operation:reserve_production_tail(db,project,operation))
    tools=Tools()
    web=lambda name,selected:WebDiscoveryService(db,tools,
        reservation_guard=lambda operation:reserve_production_tail(db,project,operation))
    pipeline=MediaAnalysisPipeline(db,_StaticSegmenter([(0,6000)]),_WritingExtractor(tmp_path/'frames'))
    runner=JobRunner(JobStore(db),{'analyze_asset':AssetAnalysisJobHandler(AssetJobTargetStore(db),AssetRepository(db),pipeline)},
        worker_id='external-contract',lease_duration=timedelta(minutes=1),max_attempts=1)
    acquirer=BoundedExternalAcquirer(db,tmp_path,['empty-catalogue'] if catalogue else [],media_factory,
        lambda job_id:analyze_exact_job(db,runner,job_id),web_sources=[name],web_service_factory=web)
    scene=original.model_copy(update={'id':uuid4(),'scene_id':'coffee','preferred_clip_id':None,
        'voice_text':'咖啡制作的操作。','preferred_sources':[SourceKind.STOCK],
        'material_request':MaterialRequest(source_kind='stock',query='coffee making',reason='展示实际操作')})
    vision.accept=lambda video:AssetRepository(db).get(video['source_asset_id']).source_kind==SourceKind.STOCK
    return db,project,scene,acquirer,resolver,events


@pytest.mark.parametrize('name',['deepseek','moonshot','other-web-tools'])
def test_without_any_stock_api_same_web_contract_imports_and_supplies_existing_resolver(tmp_path,name):
    db,project,scene,acquirer,resolver,events=system(tmp_path,name)
    try:
        job=uuid4();assert acquirer.planning_sources==('public_web',)
        acquired=acquirer.acquire(project,[scene],job)
        assert len(acquired['coffee making'])==1
        selected=resolver.resolve(project,[scene],job,acquired_assets=acquired)
        assert selected[0].preferred_clip_id is not None
        asset=AssetRepository(db).get(acquired['coffee making'][0])
        record=asset.metadata['external_sources'][0]
        assert asset.source_kind==SourceKind.STOCK and record['candidate']['license_name']=='CC BY 4.0'
        assert record['candidate']['dimensions_exact'] is False
        assert ProviderCallRepository(db).get(record['discovery_call_id']).operation=='web_search'
        assert any(fact.get('coverage')=='video_interval_analysis' for fact in asset.metadata['visual_analysis'].values())
        before=list(events)
        assert acquirer.acquire(project,[scene],job)==acquired
        assert events==before
        calls=ProviderCallRepository(db).list_for_project(project.id)
        assert Counter(call.operation for call in calls)==Counter(['web_search','stock_search','stock_import'])
        assert 'explicit-fixture-secret' not in json.dumps([call.model_dump(mode='json') for call in calls])
    finally:db.close()


def test_empty_catalogue_uses_web_backup_and_keeps_one_shared_import_pipeline(tmp_path):
    db,project,scene,acquirer,_,events=system(tmp_path,catalogue=True)
    try:
        assert acquirer.acquire(project,[scene],uuid4())['coffee making']
        assert [event[0] for event in events]==['catalogue_search','web_search','page','download']
        assert Counter(call.operation for call in ProviderCallRepository(db).list_for_project(project.id))==Counter([
            'stock_search','web_search','stock_search','stock_import'])
    finally:db.close()


def test_saved_search_original_html_avoids_duplicate_page_read_and_fetch(tmp_path):
    db,project,scene,acquirer,_,events=system(tmp_path,saved_html=True)
    try:
        assert acquirer.acquire(project,[scene],uuid4())['coffee making']
        assert [event[0] for event in events]==['web_search','download']
    finally:db.close()


@pytest.mark.parametrize('fetch_html',[True,False])
def test_direct_network_failure_can_use_fetch_html_but_never_model_summary_as_original_metadata(tmp_path,fetch_html):
    db,project,scene,acquirer,_,events=system(tmp_path,direct_failure=True,fetch_html=fetch_html)
    try:
        job=uuid4();result=acquirer.acquire(project,[scene],job)
        assert bool(result)==fetch_html
        assert [event[0] for event in events].count('page')==1
        assert [event[0] for event in events].count('web_fetch')==1
        calls=ProviderCallRepository(db).list_for_project(project.id)
        direct=next(call for call in calls if call.operation=='stock_search' and call.status=='failed')
        assert len([call for call in calls if call.operation=='web_fetch'])==1
        if fetch_html:
            parsed=next(call for call in calls if call.operation=='stock_search' and call.status=='completed')
            assert direct.input_digest!=parsed.input_digest
            assert len([call for call in calls if call.operation=='stock_import'])==1
        before=list(events)
        assert acquirer.acquire(project,[scene],job)==result and events==before
    finally:db.close()


def test_insufficient_tail_budget_stops_before_web_search_without_spending(tmp_path):
    db,project,scene,acquirer,_,events=system(tmp_path,max_calls=7)
    try:
        # 人物制作4次、用途核验1次、网页发现/解析/下载至少3次，共需8次。
        project.presentation_style='creator_led'
        assert acquirer.acquire(project,[scene],uuid4())=={}
        assert events==[] and ProviderCallRepository(db).list_for_project(project.id)==[]
    finally:db.close()


def test_failed_web_connection_is_not_reused_for_other_queries_in_same_video(tmp_path):
    db,project,scene,acquirer,_,events=system(tmp_path)
    try:
        attempts=[]
        def broken(*args):
            attempts.append(True)
            raise RuntimeError('fixture connection unavailable')
        acquirer.web_service_factory=broken;acquirer.max_web_searches=4
        other=scene.model_copy(update={'id':uuid4(),'scene_id':'other','order':1,
            'material_request':MaterialRequest(source_kind='stock',query='city street',reason='另一画面')})
        assert acquirer.acquire(project,[scene,other],uuid4())=={}
        assert len(attempts)==1
    finally:db.close()


def test_new_web_reservation_guard_runs_inside_existing_budget_transaction(tmp_path):
    db,project,scene,acquirer,_,events=system(tmp_path)
    try:
        original=acquirer.web_service_factory
        guards=[]
        def factory(name,p):
            service=original(name,p)
            service.reservation_guard=lambda operation:guards.append((operation,db.connection.in_transaction))
            return service
        acquirer.web_service_factory=factory
        assert acquirer.acquire(project,[scene],uuid4())
        assert guards==[('web_search',True)]
    finally:db.close()


def test_completed_fetch_restores_same_page_after_interruption_without_repeating_paid_request(tmp_path):
    db,project,scene,acquirer,_,events=system(tmp_path,direct_failure=True)
    try:
        original=acquirer.service_factory
        interrupted=[]
        def factory(name,p):
            service=original(name,p)
            use=service.provider.use_page_document
            def crash_once(url,content):
                if not interrupted:
                    interrupted.append(True)
                    raise SystemExit('fixture worker interruption after fetch receipt')
                use(url,content)
            service.provider.use_page_document=crash_once
            return service
        acquirer.service_factory=factory
        job=uuid4()
        with pytest.raises(SystemExit):acquirer.acquire(project,[scene],job)
        assert acquirer.acquire(project,[scene],job)['coffee making']
        assert [event[0] for event in events].count('web_search')==1
        assert [event[0] for event in events].count('web_fetch')==1
        assert [event[0] for event in events].count('page')==1
    finally:db.close()


def test_ordinary_auto_produce_job_uses_web_backup_before_existing_production_preflight(tmp_path,monkeypatch):
    from uuid import UUID
    from fastapi.testclient import TestClient
    from app.db import ProjectDraftRepository
    from app.domain.models import JobType
    from app.main import create_app
    from app.project_drafts import save_project_draft
    from app.video_production import ContentPlanJobHandler, VideoProductionService
    db,project,scene,acquirer,resolver,events=system(tmp_path)
    try:
        monkeypatch.setenv('CONTENT_OS_DATA_ROOT',str(tmp_path))
        with TestClient(create_app(db.path)) as client:
            started=client.post(f'/projects/{project.id}/auto-produce',json={
                'topic':'咖啡制作','idempotency_key':'ordinary-one-input'})
            assert started.status_code==201,started.text
        job_id=UUID(started.json()['start_job_id'])
        resolver.prepare=lambda *args:()
        def generate(*args,**kwargs):
            assert kwargs['external_sources']==('public_web',)
            draft=save_project_draft(db,project,topic='咖啡制作',script=scene.voice_text,scenes=[scene],
                routes=(),confirmed=(),video_spec=None,evidence_refs=[f'content_plan_job:{job_id}'],
                accept_scene_plan_with_new_script=True)
            return SimpleNamespace(draft=draft)
        reached=[]
        def preflight(db,p,draft,**kwargs):
            reached.append(draft.scenes[0].preferred_clip_id)
            assert reached[-1] is not None
            return SimpleNamespace(fingerprint='fixture-preflight',scenes=[SimpleNamespace(production_need='none')])
        monkeypatch.setattr('app.video_production.generate_scene_plan',generate)
        monkeypatch.setattr('app.video_production.build_production_preflight',preflight)
        service=VideoProductionService(db,tmp_path,material_resolver=resolver,material_acquirer=acquirer)
        service.runs.start=lambda *a,**kw:SimpleNamespace(project_id=project.id,id=uuid4())
        service.advance=lambda *a:None
        runner=JobRunner(JobStore(db),{JobType.PLAN_CONTENT:ContentPlanJobHandler(service,object(),object())},
            worker_id='ordinary-external-contract',lease_duration=timedelta(minutes=1),max_attempts=1)
        result=runner.run_once(job_id=job_id)
        assert result.status.value=='completed'
        assert reached and ProjectDraftRepository(db).get(project.id).scenes[0].preferred_clip_id==reached[0]
        assert [event[0] for event in events]==['web_search','page','download']
    finally:db.close()
