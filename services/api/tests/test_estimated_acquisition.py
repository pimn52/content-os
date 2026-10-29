"""估算备选沿同一外取台账验证，真实旁白可重新选择人物段。"""
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.db import AssetRepository
from app.domain.models import GraphicCardPlan, SourceKind, VisualIntent
from app.external_acquisition import ExternalAcquisitionError
from app.talking.scheduling import supporting_footage_scene
from test_external_acquisition import fixture
from test_external_acquisition_gaps import gap_request, measured_fixture, state_file


def _people(make_scene):
    return tuple(make_scene('unused',index).model_copy(update={
        'preferred_sources':[SourceKind.AI_VIDEO],'fallback_sources':[SourceKind.USER_ASSET,SourceKind.STOCK],
        'preferred_clip_id':None,'material_request':None,'graphic_plan':None,
        'visual_requirement':'creator_speaking','visual_requirement_reason':'本人解释观点',
        'supporting_visual_intent':VisualIntent(subject='camera menu',action='set bitrate')})
        for index in range(2))


@pytest.mark.parametrize('invalid',['copy','duration','identity','order','source','graphic','intent','unchanged','all-cutaways','extra'])
def test_estimated_projection_rejects_unplanned_changes_before_acquisition(tmp_path,invalid):
    db,project,_,make_scene,calls,service,_=fixture(tmp_path)
    try:
        originals=_people(make_scene)
        estimated=[supporting_footage_scene(originals[0]),originals[1]]
        if invalid=='copy':estimated[0]=estimated[0].model_copy(update={'voice_text':'改写了观点。'})
        elif invalid=='duration':estimated[0]=estimated[0].model_copy(update={'duration_target_ms':1000})
        elif invalid=='identity':estimated[0]=estimated[0].model_copy(update={'id':uuid4()})
        elif invalid=='order':estimated.reverse()
        elif invalid=='source':estimated[0]=estimated[0].model_copy(update={'preferred_sources':[SourceKind.AI_BROLL]})
        elif invalid=='graphic':estimated[0]=estimated[0].model_copy(update={'graphic_plan':GraphicCardPlan(
            kind='text_card',treatment='headline',points=['替代人物'])})
        elif invalid=='intent':estimated[0]=estimated[0].model_copy(update={'visual_intent':VisualIntent(subject='另一主题')})
        elif invalid=='unchanged':estimated=list(originals)
        elif invalid=='all-cutaways':estimated=[supporting_footage_scene(scene) for scene in originals]
        elif invalid=='extra':estimated.append(originals[1])
        job=uuid4()
        with pytest.raises(ExternalAcquisitionError,match='估算取材'):
            service.acquire(project,originals,job,estimated_scenes=estimated)
        assert calls==[] and not state_file(tmp_path,job).exists()
    finally:db.close()


def test_estimated_projection_restores_without_master_or_new_requests(tmp_path):
    db,project,_,make_scene,calls,service,_=fixture(tmp_path)
    try:
        originals=_people(make_scene)
        estimated=(supporting_footage_scene(originals[0]),originals[1])
        job=uuid4()
        assert service.acquire(project,originals,job,estimated_scenes=estimated)=={}
        assert service.original_scenes(job)==originals
        assert service.resolution_scenes(originals,job)==estimated
        state=json.loads(state_file(tmp_path,job).read_text(encoding='utf-8'))
        assert state['estimated_scenes']==[scene.model_dump(mode='json') for scene in estimated]
        assert 'master_audio_id' not in state and not state['searches'] and not state['imports']
        assert service.acquire(project,originals,job,estimated_scenes=estimated)=={}
        assert calls==[]
    finally:db.close()


def test_measured_selection_takes_over_estimate_without_resetting_acquisition_or_qa(tmp_path,monkeypatch):
    db,project,asset,original,calls,service,master,_=measured_fixture(tmp_path,monkeypatch)
    clock=[0.0]
    monkeypatch.setattr('app.external_acquisition.time',SimpleNamespace(monotonic=lambda:clock[0]))
    factory=service.service_factory
    class TimedService:
        def search(self,*args):
            clock[0]+=5
            return factory('stock-a',project).search(*args)
        def import_video(self,*args,**kwargs):
            clock[0]+=5
            return factory('stock-a',project).import_video(*args,**kwargs)
    service.service_factory=lambda *args:TimedService()
    def analyze(identity):
        clock[0]+=1
        calls.append(('analyze',str(identity)))
    service.analyze=analyze
    try:
        first=original.model_copy(update={'scene_id':'first','voice_text':'先说观点。','duration_target_ms':3000})
        second=original.model_copy(update={'id':uuid4(),'scene_id':'second','order':1,
            'voice_text':'再看画面。','duration_target_ms':3000})
        originals=(first,second)
        estimated=(supporting_footage_scene(first),second)
        job=uuid4()
        service.acquire(project,originals,job,estimated_scenes=estimated)
        gap=gap_request(estimated[0],'camera menu set bitrate')
        result=service.acquire(project,originals,job,estimated_scenes=estimated,
            supplement_for=[gap.material_request.query],gap_scenes=[gap])
        assert result=={gap.material_request.query:[asset.id]}
        # 用真实resolver保存本地夹具QA；后面的配时交接不能清掉这些旧用途事实。
        from app.media.planning import VisualMaterialResolver
        from test_material_resolution import FixtureVideoVision,FixtureVideoExtractor,FixtureVideoProbe
        from app.costs import UnknownProviderCostEstimator
        resolver=VisualMaterialResolver(db,tmp_path,FixtureVideoVision(accept=lambda _:True),
            FixtureVideoExtractor(tmp_path/'estimated-proxies'),FixtureVideoProbe(),UnknownProviderCostEstimator())
        resolver.resolve(project,[gap,second],service.resolution_scope(job),acquired_assets=result)
        qa_before=AssetRepository(db).get(asset.id).metadata['visual_analysis']
        before=json.loads(state_file(tmp_path,job).read_text(encoding='utf-8'))
        assert before['acquisition_elapsed_seconds']>0
        measured=(first.model_copy(update={'duration_target_ms':1500}),
            supporting_footage_scene(second.model_copy(update={'duration_target_ms':1500})))
        clock[0]+=600
        calls.clear()
        assert service.acquire(project,originals,job,measured_scenes=measured,master_audio_id=master.id)==result
        after=json.loads(state_file(tmp_path,job).read_text(encoding='utf-8'))
        assert service.resolution_scenes(originals,job)==measured
        assert service.original_scenes(job)==originals
        assert after['fingerprint']==before['fingerprint'] and after['scene_fingerprint']==before['scene_fingerprint']
        assert after['started_at']==before['started_at'] and after['supplement']==before['supplement']
        assert after['searches']==before['searches'] and after['imports']==before['imports']
        assert after['acquisition_limit_seconds']==before['acquisition_limit_seconds']
        assert before['acquisition_elapsed_seconds']<=after['acquisition_elapsed_seconds']<120
        assert after['estimated_gap_scenes']==before['gap_scenes'] and not after.get('gap_scenes')
        assert after['master_audio_id']==str(master.id) and after['master_content_hash']==master.content_hash
        assert AssetRepository(db).get(asset.id).metadata['visual_analysis']==qa_before
        assert all(call[0]=='analyze' for call in calls)
        calls.clear()
        assert service.acquire(project,originals,job)==result
        assert service.resolution_scenes(originals,job)==measured
        assert all(call[0]=='analyze' for call in calls)
    finally:db.close()
