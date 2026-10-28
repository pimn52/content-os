from datetime import datetime,timezone
import json

from fastapi.testclient import TestClient

from app.db import Database,BudgetPolicyRepository,ProviderCallRepository
from app.domain.models import BudgetPolicy
from app.main import create_app
from app.providers.deepseek_web import DeepSeekWebError,DeepSeekWebProvider
from test_external_media import _project


class Provider:
    provider_name="deepseek"
    _api_key="fixture-web-secret"
    cost=staticmethod(DeepSeekWebProvider.cost)
    def __init__(self):
        self.search_count=self.fetch_count=0
        self.fail=False
    def search(self,query,**kwargs):
        self.search_count+=1
        if self.fail:raise DeepSeekWebError("fixture_search_failed","safe error")
        return {"results":[{"url":"https://www.pexels.com/search/videos/coffee/","title":"Coffee videos"},
            {"url":"https://esahubble.org/videos/heic1507a/","title":"Flight through the cosmic pillars"}]}
    def fetch(self,url):
        self.fetch_count+=1
        return {"url":url,"title":"Source page","content":"仅正文，不表示视频已取得"}


def setup(tmp_path,monkeypatch,max_calls=5):
    monkeypatch.delenv('CONTENT_OS_PEXELS_API_KEY_FILE',raising=False)
    path=tmp_path/'web.sqlite'
    project=_project(path)
    with Database(path) as db:
        BudgetPolicyRepository(db).save(BudgetPolicy(project_id=project.id,currency="CNY",max_calls=max_calls,
            allow_unknown_cost=True,updated_at=datetime.now(timezone.utc)))
    key=tmp_path/'key.txt';key.write_text('fixture-web-secret',encoding='utf-8')
    monkeypatch.setenv('CONTENT_OS_DEEPSEEK_WEB_API_KEY_FILE',str(key))
    monkeypatch.setenv('CONTENT_OS_DATA_ROOT',str(tmp_path/'data'))
    provider=Provider()
    app=create_app(path);app.state.web_discovery_provider=provider
    return path,project,provider,app


def test_ordinary_discovery_entry_saves_and_restores_pages_and_reuses_paid_query(tmp_path,monkeypatch):
    path,p,provider,app=setup(tmp_path,monkeypatch,max_calls=1)
    base=f'/projects/{p.id}/external-media'
    with TestClient(app) as client:
        first=client.post(base+'/web-search',json={'provider':'deepseek','query':'coffee hands','idempotency_key':'one'})
        assert first.status_code==200,first.text
        assert first.json()['pages'][0]['kind']=='video_detail'
        assert first.json()['pages'][0]['can_resolve'] is True
        assert first.json()['pages'][0]['resolution_code']=='resolver_available'
        assert '编码尺寸' in first.json()['pages'][0]['resolution_reason']
        assert first.json()['pages'][1]['kind']=='collection'
        assert first.json()['pages'][1]['can_resolve'] is False
        second=client.post(base+'/web-search',json={'provider':'deepseek','query':'coffee hands','idempotency_key':'two'})
        assert second.status_code==200 and second.json()['cached']
        assert second.json()['call_id']==first.json()['call_id']
        assert client.post(base+'/web-search',json={'provider':'deepseek','query':'different','idempotency_key':'one'}).status_code==409
        assert client.get(base+'/web-searches').json()[0]['call_id']==first.json()['call_id']
    with TestClient(app) as client:
        assert client.post(base+'/web-search',json={'provider':'deepseek','query':'coffee hands','idempotency_key':'two'}).json()==second.json()
    assert provider.search_count==1
    with Database(path) as db:
        rows=ProviderCallRepository(db).list_for_project(p.id)
        assert len(rows)==1 and rows[0].estimated_cost.amount is not None
        assert 'fixture-web-secret' not in json.dumps(rows[0].model_dump(mode='json'))


def test_fetch_only_specific_page_from_saved_query_and_reuses_result(tmp_path,monkeypatch):
    _,p,provider,app=setup(tmp_path,monkeypatch)
    base=f'/projects/{p.id}/external-media'
    with TestClient(app) as client:
        search=client.post(base+'/web-search',json={'provider':'deepseek','query':'coffee hands','idempotency_key':'search'}).json()
        directory={'search_call_id':search['call_id'],'url':search['pages'][1]['url'],'idempotency_key':'directory'}
        assert client.post(base+'/web-fetch',json=directory).status_code==422
        assert provider.fetch_count==0
        request={'search_call_id':search['call_id'],'url':search['pages'][0]['url'],'idempotency_key':'detail'}
        fetched=client.post(base+'/web-fetch',json=request)
        assert fetched.status_code==200,fetched.text
        assert fetched.json()['content']=='仅正文，不表示视频已取得'
        assert client.post(base+'/web-fetch',json={**request,'idempotency_key':'other'}).json()['cached']
        assert provider.fetch_count==1


def test_recorded_search_failure_is_not_automatically_retried(tmp_path,monkeypatch):
    _,p,provider,app=setup(tmp_path,monkeypatch)
    base=f'/projects/{p.id}/external-media'
    with TestClient(app) as client:
        provider.fail=True
        body={'provider':'deepseek','query':'coffee hands','idempotency_key':'failed'}
        assert client.post(base+'/web-search',json=body).status_code==502
        provider.fail=False
        assert client.post(base+'/web-search',json=body).status_code==409
        assert provider.search_count==1
        refreshed=client.post(base+'/web-search',json={**body,'idempotency_key':'explicit-refresh','refresh':True})
        assert refreshed.status_code==200 and provider.search_count==2


def test_existing_search_body_does_not_pay_for_fetch_again(tmp_path,monkeypatch):
    _,p,provider,app=setup(tmp_path,monkeypatch)
    search=provider.search
    def with_content(*args,**kwargs):
        result=search(*args,**kwargs)
        result['results'][1]['content']='搜索已经返回的完整正文'
        return result
    provider.search=with_content
    base=f'/projects/{p.id}/external-media'
    with TestClient(app) as client:
        result=client.post(base+'/web-search',json={'provider':'deepseek','query':'coffee hands','idempotency_key':'body'}).json()
        fetched=client.post(base+'/web-fetch',json={'search_call_id':result['call_id'],'url':result['pages'][0]['url'],'idempotency_key':'read-body'})
        assert fetched.status_code==200 and fetched.json()['content_source']=='saved_search'
        assert fetched.json()['cached'] and provider.fetch_count==0


def test_saved_web_detail_resolves_through_ordinary_media_import(tmp_path,monkeypatch):
    from app.media import MediaImporter
    from test_external_media import Provider as MediaProvider,Probe
    _,p,_,app=setup(tmp_path,monkeypatch)
    media=MediaProvider();media.provider_name='public_page';media.model='public-page-fixture'
    media.video=media.video.model_copy(update={'provider':'public_page'})
    media.check_source=lambda url: None
    app.state.external_media_provider=media
    app.state.media_importer_factory=lambda db,root:MediaImporter(db,root,Probe())
    base=f'/projects/{p.id}/external-media'
    with TestClient(app) as client:
        result=client.post(base+'/web-search',json={'provider':'deepseek','query':'coffee hands','idempotency_key':'search'}).json()
        detail={'search_call_id':result['call_id'],'url':result['pages'][0]['url'],'idempotency_key':'resolve'}
        found=client.post(base+'/resolve-page',json=detail)
        assert found.status_code==200,found.text
        imported=client.post(base+'/imports',json={'search_call_id':found.json()['call_id'],'candidate_id':found.json()['candidates'][0]['id'],'idempotency_key':'import'})
        assert imported.status_code==201,imported.text
        assert imported.json()['analysis_job_id'] and media.download_count==1
        assert media.search_count==1


def test_source_requirements_and_public_metadata_checks_are_distinct_without_network(tmp_path,monkeypatch):
    path,p,provider,app=setup(tmp_path,monkeypatch)
    rows=[
        {"url":"https://www.pexels.com/video/making-coffee-41/","title":"Making coffee"},
        {"url":"https://pixabay.com/videos/coffee-in-cup-42/","title":"Coffee in cup"},
        {"url":"https://coverr.co/videos/pouring-coffee-123","title":"Pouring coffee"},
    ]
    provider.search=lambda *a,**k:{"results":rows}
    base=f'/projects/{p.id}/external-media'
    with TestClient(app) as client:
        result=client.post(base+'/web-search',json={'provider':'deepseek','query':'coffee making','idempotency_key':'status'}).json()
        assert len(result['pages'])==3
        for page in result['pages']:
            assert page['kind']=='video_detail'
            assert page['resolution_reason']
            if 'pixabay.com' in page['url']:
                assert page['can_resolve'] is True and page['resolver_provider']=='public_page'
                continue  # 可尝试元数据解析不等于许可或文件已取得；本测试不发源站请求。
            assert page['can_resolve'] is False
            response=client.post(base+'/resolve-page',json={'search_call_id':result['call_id'],
                'url':page['url'],'idempotency_key':'unavailable:'+page['url']})
            assert response.status_code==422 and response.json()['detail']==page['resolution_reason']
        assert [page['resolution_code'] for page in result['pages']]==[
            'pexels_not_configured','resolver_available','public_page_source_not_admitted']
        assert client.get(base+'/web-searches').json()[0]['pages']==result['pages']
    with Database(path) as db:
        assert len(ProviderCallRepository(db).list_for_project(p.id))==1


def test_discovered_pexels_page_uses_connected_catalogue_and_existing_import_without_paid_fetch(tmp_path,monkeypatch):
    from app.media import MediaImporter
    from app.providers.pexels_media import PexelsMediaProvider
    from test_pexels_media import row,Response,VIDEO,Probe
    path,p,provider,app=setup(tmp_path,monkeypatch,max_calls=3)
    key=tmp_path/'pexels.txt';key.write_text('fixture-pexels-credential',encoding='utf-8')
    monkeypatch.setenv('CONTENT_OS_PEXELS_API_KEY_FILE',str(key))
    provider.search=lambda *a,**kw:{'results':[{'url':'https://www.pexels.com/video/making-coffee-41/','title':'Making coffee'}]}
    seen=[]
    def opener(request,timeout):
        seen.append(request)
        if 'api.pexels.com' in request.full_url:
            assert request.full_url=='https://api.pexels.com/v1/videos/videos/41'
            return Response(json.dumps(row()).encode())
        return Response(VIDEO,mime='video/mp4')
    monkeypatch.setattr('app.external_media_connection.PexelsMediaProvider',lambda key=None,**kw:PexelsMediaProvider(key,opener=opener,**kw))
    app.state.media_importer_factory=lambda db,root:MediaImporter(db,root,Probe())
    base=f'/projects/{p.id}/external-media'
    with TestClient(app) as client:
        search=client.post(base+'/web-search',json={'provider':'deepseek','query':'coffee making','idempotency_key':'search'}).json()
        page=search['pages'][0]
        assert page['can_resolve'] and page['resolver_provider']=='pexels'
        assert page['resolution_code']=='connected_catalogue'
        request={'search_call_id':search['call_id'],'url':page['url'],'idempotency_key':'resolve'}
        found=client.post(base+'/resolve-page',json=request)
        assert found.status_code==200,found.text
        value=found.json()
        assert value['provider']=='pexels' and value['candidates'][0]['source_id']=='41:43'
        cached=client.post(base+'/resolve-page',json={**request,'idempotency_key':'resolve-again'})
        assert cached.status_code==200 and cached.json()['cached']
        key.unlink()  # 原文件沿已有准入和快照下载，不重复索Key。
        payload={'search_call_id':value['call_id'],'candidate_id':value['candidates'][0]['id'],'idempotency_key':'import'}
        imported=client.post(base+'/imports',json=payload)
        assert imported.status_code==201,imported.text
        assert client.post(base+'/imports',json=payload).json()==imported.json()
        assert imported.json()['analysis_job_id']
    with Database(path) as db:
        calls=ProviderCallRepository(db).list_for_project(p.id)
        assert [c.operation for c in calls]==['web_search','stock_search','stock_import']
        assert len(seen)==2 and provider.fetch_count==0
        assert seen[0].get_header('Authorization')=='fixture-pexels-credential'
        assert seen[1].get_header('Authorization') is None


def test_registered_web_tools_reuse_ordinary_search_fetch_and_persisted_results(tmp_path,monkeypatch):
    from app import web_discovery_connection as connections
    monkeypatch.setattr(connections,'_ADAPTERS',connections._ADAPTERS.copy())
    path,p,provider,app=setup(tmp_path,monkeypatch)
    provider.provider_name='other-web'
    connections.register_web_provider('other-web',lambda key:provider,label='其他网页工具',
        credential_setting='CONTENT_OS_OTHER_WEB_API_KEY_FILE')
    key=tmp_path/'other.txt';key.write_text('fixture-other-secret',encoding='utf-8')
    monkeypatch.setenv('CONTENT_OS_OTHER_WEB_API_KEY_FILE',str(key))
    base=f'/projects/{p.id}/external-media'
    with TestClient(app) as client:
        row=next(value for value in client.get(base+'/web-providers').json() if value['id']=='other-web')
        assert row['configured'] and row['search'] and row['fetch']
        result=client.post(base+'/web-search',json={
            'provider':'other-web','query':'coffee hands','idempotency_key':'third-party-search'})
        assert result.status_code==200,result.text
        search=result.json()
        assert search['provider']=='other-web'
        fetch=client.post(base+'/web-fetch',json={'search_call_id':search['call_id'],
            'url':search['pages'][0]['url'],'idempotency_key':'third-party-fetch'})
        assert fetch.status_code==200,fetch.text
        assert fetch.json()['provider']=='other-web'
        assert client.get(base+'/web-searches').json()[0]['provider']=='other-web'
        unsupported=client.post(base+'/web-search',json={
            'provider':'chat-key-only','query':'coffee hands','idempotency_key':'unknown'})
        assert unsupported.status_code==422
    with Database(path) as db:
        calls=ProviderCallRepository(db).list_for_project(p.id)
        assert [(call.provider,call.operation) for call in calls]==[
            ('other-web','web_search'),('other-web','web_fetch')]


def test_search_only_connection_rejects_fetch_before_spending(tmp_path,monkeypatch):
    from app import web_discovery_connection as connections
    monkeypatch.setattr(connections,'_ADAPTERS',connections._ADAPTERS.copy())
    path,p,provider,app=setup(tmp_path,monkeypatch)
    provider.provider_name='search-only'
    connections.register_web_provider('search-only',lambda key:provider,label='仅搜索',
        credential_setting='CONTENT_OS_SEARCH_ONLY_API_KEY_FILE',fetch=False)
    base=f'/projects/{p.id}/external-media'
    with TestClient(app) as client:
        search=client.post(base+'/web-search',json={
            'provider':'search-only','query':'coffee hands','idempotency_key':'search'}).json()
        fetch=client.post(base+'/web-fetch',json={'search_call_id':search['call_id'],
            'url':search['pages'][0]['url'],'idempotency_key':'fetch'})
        assert fetch.status_code==422 and provider.fetch_count==0
    with Database(path) as db:
        assert len(ProviderCallRepository(db).list_for_project(p.id))==1
