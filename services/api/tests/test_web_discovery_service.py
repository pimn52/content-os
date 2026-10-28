from datetime import datetime,timezone

import pytest

from app import web_discovery as discovery
from app import web_discovery_connection as connections
from app.budget import BudgetLimitError
from app.db import Database,BudgetPolicyRepository,ProviderCallRepository
from app.domain.models import BudgetPolicy
from app.media.web_discovery import classify_search_result
from app.provider_execution import ProviderExecutionRecordedFailure
from app.providers.deepseek_web import DeepSeekWebProvider
from test_external_media import _project


class WebTools:
    provider_name='registered-web'
    model='search'
    def __init__(self,key):
        self._api_key=key
        self.search_count=self.fetch_count=0
        self.fail=False
        self.content=''
    def cost(self):
        return DeepSeekWebProvider.cost().model_copy(update={'provider':self.provider_name})
    def search(self,query,**kwargs):
        self.search_count+=1
        if self.fail:
            raise RuntimeError('fixture unavailable')
        return {'results':[{'url':'https://example.org/footage/coffee',
            'title':'Coffee preparation','content':self.content}]}
    def fetch(self,url):
        self.fetch_count+=1
        return {'title':'Coffee preparation','content':'素材页面正文，原文件及用途仍待核实'}


@pytest.fixture
def configured_service(tmp_path,monkeypatch):
    monkeypatch.setattr(connections,'_ADAPTERS',connections._ADAPTERS.copy())
    connections.register_web_provider('registered-web',WebTools,label='网页工具',
        credential_setting='CONTENT_OS_REGISTERED_WEB_API_KEY_FILE')
    key=tmp_path/'key.txt';key.write_text('fixture-secret',encoding='utf-8')
    monkeypatch.setenv('CONTENT_OS_REGISTERED_WEB_API_KEY_FILE',str(key))
    path=tmp_path/'service.sqlite'
    project=_project(path)
    with Database(path) as db:
        BudgetPolicyRepository(db).save(BudgetPolicy(project_id=project.id,currency='CNY',max_calls=10,
            allow_unknown_cost=True,updated_at=datetime.now(timezone.utc)))
        yield db,project,connections.web_provider('registered-web')


def test_new_search_fetch_guard_preserves_tail_and_replays_do_not_reserve_again(configured_service):
    db,project,provider=configured_service
    guarded=[]
    service=discovery.WebDiscoveryService(db,provider,reservation_guard=guarded.append)
    first=service.search(project.id,'coffee preparation','first')
    assert first['pages'][0]['kind']=='unknown'
    for key in ('first','same-query'):
        assert service.search(project.id,'coffee preparation',key)['call_id']==first['call_id']
    fetched=service.fetch(project.id,first['call_id'],'https://example.org/footage/coffee','fetch')
    again=service.fetch(project.id,first['call_id'],'https://example.org/footage/coffee','fetch-again')
    assert fetched['call_id']==again['call_id'] and again['cached']
    assert guarded==['web_search','web_fetch']
    assert provider.search_count==provider.fetch_count==1
    assert len(ProviderCallRepository(db).list_for_project(project.id))==2


def test_guard_blocks_new_request_before_ledger_and_does_not_block_saved_search_body(configured_service):
    db,project,provider=configured_service
    provider.content='已经取得的页面正文'
    initial=discovery.WebDiscoveryService(db,provider).search(project.id,'coffee preparation','initial')
    def no_budget(operation):
        raise BudgetLimitError('production_tail_reserved','为后段制作保留额度')
    service=discovery.WebDiscoveryService(db,provider,reservation_guard=no_budget,supports_fetch=False)
    assert service.search(project.id,'coffee preparation','cached')['call_id']==initial['call_id']
    result=service.fetch(project.id,initial['call_id'],'https://example.org/footage/coffee','saved-body')
    assert result['cached'] and result['content_source']=='saved_search'
    with pytest.raises(BudgetLimitError):
        service.search(project.id,'another need','new-search')
    assert provider.search_count==1 and provider.fetch_count==0
    assert len(ProviderCallRepository(db).list_for_project(project.id))==1


def test_missing_fetch_capability_is_explicit_and_failed_calls_do_not_reserve_or_retry(configured_service):
    db,project,provider=configured_service
    guarded=[]
    service=discovery.WebDiscoveryService(db,provider,reservation_guard=guarded.append,supports_fetch=False)
    search=service.search(project.id,'coffee preparation','initial')
    with pytest.raises(connections.WebConnectionError) as error:
        service.fetch(project.id,search['call_id'],'https://example.org/footage/coffee','unsupported-fetch')
    assert error.value.code=='web_capability_unavailable'
    assert guarded==['web_search'] and provider.fetch_count==0
    provider.fail=True
    with pytest.raises(RuntimeError):
        service.search(project.id,'unavailable query','failed')
    provider.fail=False
    with pytest.raises(ProviderExecutionRecordedFailure):
        service.search(project.id,'unavailable query','failed-other-key')
    assert guarded==['web_search','web_search'] and provider.search_count==2
    assert len(ProviderCallRepository(db).list_for_project(project.id))==2


def test_unknown_page_resolution_is_permission_to_inspect_not_video_or_asset_claim(monkeypatch):
    seen=[]
    class GenericScanner:
        def check_source(self,url):
            seen.append(url)
            return 'generic'
    monkeypatch.setattr(discovery,'PublicPageMediaProvider',GenericScanner)
    unknown=classify_search_result({'url':'https://example.org/footage/coffee','title':'Coffee closeup'})
    resolved=discovery.page_resolution(unknown)
    assert unknown.kind=='unknown' and resolved['can_resolve']
    assert resolved['resolution_code']=='page_metadata_pending'
    assert resolved['resolver_provider']=='public_page' and '不代表已有视频' in resolved['resolution_reason']
    for url,title in [('https://example.org/','Coffee'),('https://example.org/search/coffee','Coffee'),
            ('https://example.org/coffee.pdf','Coffee'),('https://example.org/coffee','安全验证')]:
        assert not discovery.page_resolution(classify_search_result({'url':url,'title':title}))['can_resolve']
    assert seen==[unknown.url]
    monkeypatch.delenv('CONTENT_OS_PEXELS_API_KEY_FILE',raising=False)
    pexels=discovery.page_resolution(classify_search_result({
        'url':'https://www.pexels.com/video/coffee-41/','title':'Coffee closeup'}))
    assert not pexels['can_resolve'] and pexels['resolution_code']=='pexels_not_configured'
    assert seen==[unknown.url]
