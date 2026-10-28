"""用途否定后的换源顺序，使用既有公开页/文件夹具，不访问网络。"""
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.db import AssetRepository
from app.domain.models import MaterialRequest,SourceKind
from test_external_media import Provider
from test_web_material_fallback import system


def supplied_system(tmp_path,*,queries=('coffee making',),web='available',backup=False):
    db,project,scene,acquirer,_,events=system(tmp_path,name='other-web-tools')
    original_factory=acquirer.service_factory
    original_analyze=acquirer.analyze
    template=AssetRepository(db).list()[0]
    candidates={};assets={};analysis_jobs=set()
    for source in ('pexels','backup'):
        for query in queries:
            rows=[]
            for index in range(3):
                path=tmp_path/f'{source}-{len(assets)}.mp4'
                path.write_bytes(f'{source}:{query}:{index}'.encode())
                asset=AssetRepository(db).create(template.model_copy(update={
                    'id':uuid4(),'source_file':str(path),'source_kind':SourceKind.STOCK,
                    'content_hash':hashlib.sha256(path.read_bytes()).hexdigest(),'metadata':{}}))
                row=Provider().video.model_copy(update={'id':uuid4(),'provider':source,
                    'width':1080,'height':1920,'duration_ms':6000})
                rows.append(row);assets[row.id]=(asset,source,query,index)
            candidates[(source,query)]=rows
    class Catalogue:
        def __init__(self,source):self.source=source
        def search(self,project_id,query,key):
            events.append(('catalogue_search',self.source,query))
            return SimpleNamespace(call_id=uuid4(),candidates=candidates[(self.source,query)])
        def import_video(self,project_id,call_id,candidate_id,key,**kwargs):
            asset,source,query,index=assets[candidate_id]
            events.append(('catalogue_download',source,query,index))
            analysis=uuid4();analysis_jobs.add(analysis)
            return SimpleNamespace(asset_id=asset.id,analysis_job_id=analysis)
    acquirer.service_factory=lambda source,p:Catalogue(source) if source in {'pexels','backup'} else original_factory(source,p)
    acquirer.sources=('pexels','backup') if backup else ('pexels',)
    acquirer.analyze=lambda job:None if job in analysis_jobs else original_analyze(job)
    if web=='not-configured':
        acquirer.web_sources=()
    elif web=='unavailable':
        factory=acquirer.web_service_factory
        def unavailable(name,p):
            service=factory(name,p)
            def search(*args,**kwargs):
                events.append(('web_search',name))
                return {'results':[{'url':'https://open-films.example/licensing.pdf','title':'License','mime':'application/pdf'}]}
            service.provider.search=search
            return service
        acquirer.web_service_factory=unavailable
    scenes=[scene.model_copy(update={'id':uuid4(),'scene_id':f'need-{index}','order':index,
        'material_request':MaterialRequest(source_kind='stock',query=query,reason='展示明确对象')})
        for index,query in enumerate(queries)]
    return db,project,scenes,acquirer,events,candidates,assets


def test_rejected_catalogue_cache_cannot_fill_download_slots_before_configured_web_fallback(tmp_path):
    db,project,scenes,acquirer,events,_,_=supplied_system(tmp_path)
    try:
        job=uuid4();first=acquirer.acquire(project,scenes,job)
        assert [event[0] for event in events]==['catalogue_search','catalogue_download']
        events.clear()
        replacement=acquirer.acquire(project,scenes,job,supplement_for=['coffee making'])
        names=[event[0] for event in events]
        assert names[:3]==['web_search','page','download']
        assert names.index('download')<names.index('catalogue_download')
        assert any(identity not in first['coffee making'] for identity in replacement['coffee making'])
        state=json.loads((tmp_path/'render-diagnostics'/str(job)/'external-materials.json').read_text(encoding='utf-8'))
        assert len(state['web_searches'])==1 and len(state['imports'])<=state['supplement']['import_limit']==3
        assert len(state['searches'])<=acquirer.max_searches and acquirer.max_imports==5
        before=list(events)
        assert acquirer.acquire(project,scenes,job,supplement_for=['coffee making'])==replacement
        assert events==before
    finally:db.close()


@pytest.mark.parametrize('web',['unavailable','not-configured'])
def test_unavailable_web_keeps_same_source_candidates_as_bounded_last_resort_without_retry(tmp_path,web):
    db,project,scenes,acquirer,events,_,_=supplied_system(tmp_path,web=web)
    try:
        job=uuid4();acquirer.acquire(project,scenes,job);events.clear()
        assert acquirer.acquire(project,scenes,job,supplement_for=['coffee making'])['coffee making']
        names=[event[0] for event in events]
        assert names==(['web_search'] if web=='unavailable' else [])+['catalogue_download','catalogue_download']
        assert all(event[3] in (1,2) for event in events if event[0]=='catalogue_download')
        before=list(events)
        acquirer.acquire(project,scenes,job,supplement_for=['coffee making'])
        assert events==before
        state=json.loads((tmp_path/'render-diagnostics'/str(job)/'external-materials.json').read_text(encoding='utf-8'))
        assert len(state['imports'])==3 and state['supplement']['import_limit']==3
    finally:db.close()


def test_two_queries_keep_one_original_query_slot_for_untried_stock_source_and_share_replacements(tmp_path):
    queries=('coffee making','city street')
    db,project,scenes,acquirer,events,candidates,_=supplied_system(tmp_path,queries=queries,web='not-configured',backup=True)
    try:
        job=uuid4();acquirer.acquire(project,scenes,job)
        assert [event for event in events if event[0]=='catalogue_search']==[
            ('catalogue_search','pexels',query) for query in queries]
        events.clear()
        result=acquirer.acquire(project,scenes,job,supplement_for=queries)
        assert set(result)==set(queries)
        assert [event for event in events if event[0]=='catalogue_search']==[('catalogue_search','backup',queries[0])]
        downloaded=[event for event in events if event[0]=='catalogue_download']
        assert downloaded==[('catalogue_download','backup',queries[0],0),('catalogue_download','pexels',queries[1],1)]
        state=json.loads((tmp_path/'render-diagnostics'/str(job)/'external-materials.json').read_text(encoding='utf-8'))
        assert len(state['searches'])==acquirer.max_searches==3
        assert len(state['imports'])==state['supplement']['import_limit']==4
        assert str(candidates[('pexels',queries[0])][1].id) not in {entry['candidate_id'] for entry in state['imports']}
    finally:db.close()


def test_query_without_cached_alternative_gets_the_limited_web_slot_before_cache_rich_query(tmp_path):
    queries=('coffee making','city street')
    db,project,scenes,acquirer,events,candidates,_=supplied_system(tmp_path,queries=queries)
    try:
        candidates[('pexels',queries[1])]=candidates[('pexels',queries[1])][:1]
        acquirer.max_web_searches=1
        job=uuid4();first=acquirer.acquire(project,scenes,job);events.clear()
        result=acquirer.acquire(project,scenes,job,supplement_for=queries)
        assert [event[0] for event in events]==['web_search','page','download','catalogue_download']
        assert events[-1]==('catalogue_download','pexels',queries[0],1)
        assert all(any(identity not in first[query] for identity in result[query]) for query in queries)
        state=json.loads((tmp_path/'render-diagnostics'/str(job)/'external-materials.json').read_text(encoding='utf-8'))
        assert [search['query'] for search in state['web_searches']]==[queries[1]]
        assert len(state['imports'])==state['supplement']['import_limit']==4
        before=list(events)
        acquirer.acquire(project,scenes,job,supplement_for=queries)
        assert events==before
    finally:db.close()


def test_first_source_cannot_consume_all_three_queries_when_other_catalogue_is_configured(tmp_path):
    queries=('coffee making','city street','laptop screen')
    db,project,scenes,acquirer,events,_,_=supplied_system(tmp_path,queries=queries,web='not-configured',backup=True)
    try:
        assert set(acquirer.acquire(project,scenes,uuid4()))==set(queries)
        assert [event for event in events if event[0]=='catalogue_search']==[
            ('catalogue_search','pexels',queries[0]),('catalogue_search','pexels',queries[1]),
            ('catalogue_search','backup',queries[2])]
        assert len([event for event in events if event[0]=='catalogue_download'])==3
        assert acquirer.max_searches==3 and acquirer.initial_imports==3
    finally:db.close()
