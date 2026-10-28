"""已有画面落选后的产品补缺；所有网络与媒体结论均为显式替身。"""
from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.db import AssetRepository, ClipRepository, ProjectDraftRepository
from app.domain.models import Clip, ProjectDraft, SourceKind
from app.external_acquisition import BoundedExternalAcquirer
from app.media.planning import MaterialResolutionError, split_footage_clauses
from app.video_production import VideoProductionService
from test_external_media import Provider
from test_material_resolution import material_library


@pytest.mark.parametrize('reason',['material_scene_unresolved','material_combination_unresolved'])
def test_exact_unresolved_clause_becomes_stock_request_without_changing_expression(tmp_path,reason):
    db,_,_,_,scene,_,_=material_library(tmp_path)
    try:
        scene=scene.model_copy(update={'fallback_sources':[SourceKind.STOCK],
            'duration_target_ms':11000,'voice_text':'第一段说明原理。第二段给出建议。'})
        parts=split_footage_clauses([scene])
        assert len(parts)==2
        failure=MaterialResolutionError(f'{reason}:{parts[1].scene_id}')
        gaps=VideoProductionService._material_gaps([scene],failure)
        assert len(gaps)==1 and gaps[0].scene_id==parts[1].scene_id
        assert gaps[0].material_request.source_kind=='stock' and gaps[0].preferred_clip_id is None
        for key,value in parts[1].model_dump().items():
            if key not in {'material_request','preferred_clip_id'}:
                assert gaps[0].model_dump()[key]==value
    finally:db.close()


@pytest.mark.parametrize('change',[
    {'fallback_sources':[]},
    {'fallback_sources':[SourceKind.STOCK],'visual_requirement':'creator_speaking'},
    {'preferred_sources':[SourceKind.TYPOGRAPHY],'fallback_sources':[SourceKind.STOCK]},
])
def test_stock_gap_cannot_replace_creator_or_expand_disallowed_source(tmp_path,change):
    db,_,_,_,scene,_,_=material_library(tmp_path)
    try:
        scene=scene.model_copy(update=change)
        assert VideoProductionService._material_gaps([scene],
            MaterialResolutionError(f'material_scene_unresolved:{scene.scene_id}'))==()
    finally:db.close()


@pytest.mark.parametrize('acceptable',[True,False])
@pytest.mark.parametrize('local_present',[True,False])
@pytest.mark.parametrize('resume_before_qa',[True,False])
@pytest.mark.parametrize('initial_batch_limit',[True,False])
def test_ordinary_plan_without_initial_requests_gets_and_checks_external_gap_once(
    tmp_path,monkeypatch,acceptable,local_present,resume_before_qa,initial_batch_limit):
    db,project,old,_,original,vision,resolver=material_library(tmp_path)
    scene=original.model_copy(update={'fallback_sources':[SourceKind.STOCK],
        'visual_requirement':'explanatory','visual_requirement_reason':'与主题相符的辅助说明'})
    job=SimpleNamespace(project_id=project.id,id=uuid4(),
        payload=SimpleNamespace(topic=project.topic,candidate_of_run_id=None))
    draft=ProjectDraftRepository(db).save(ProjectDraft(project_id=project.id,version=1,script_revision=1,
        script=scene.voice_text,topic=project.topic,scenes=[scene],
        evidence_refs=[f'content_plan_job:{job.id}'],updated_at=datetime.now(timezone.utc)))
    if not local_present:db.connection.execute('DELETE FROM clips')
    events=[];new_assets=[]
    candidate=Provider().video.model_copy(update={'width':1080,'height':1920,'duration_ms':10000})
    class Catalogue:
        def search(self,project_id,query,key):
            events.append('search')
            return SimpleNamespace(call_id=uuid4(),candidates=[candidate])
        def import_video(self,*args,**kwargs):
            events.append('import')
            source=tmp_path/'new-external.mp4';source.write_bytes(b'new external fixture')
            asset=AssetRepository(db).create(old.model_copy(update={'id':uuid4(),'source_file':str(source),
                'source_kind':SourceKind.STOCK,'content_hash':hashlib.sha256(source.read_bytes()).hexdigest(),'metadata':{}}))
            ClipRepository(db).create(Clip(asset_id=asset.id,start_ms=0,end_ms=10000,asset_duration_ms=asset.duration_ms,
                visual_description=scene.visual_intent.subject))
            new_assets.append(asset.id)
            return SimpleNamespace(asset_id=asset.id,analysis_job_id=uuid4())
    acquirer=BoundedExternalAcquirer(db,tmp_path,['fixture-catalogue'],lambda *args:Catalogue(),lambda *args:None)
    vision.accept=lambda item:acceptable and item['source_asset_id'] in {str(value) for value in new_assets}
    resolver.revision_candidates=lambda *args:()  # 本测试只检验补缺，不另造可成功的画面调整。
    actual_resolve=resolver.resolve
    interrupted=[]
    class InterruptedBeforeQA(RuntimeError):pass
    def resolve(project,scenes,scope,**kwargs):
        if resume_before_qa and scope!=job.id and not interrupted:
            interrupted.append(True)
            raise InterruptedBeforeQA()
        try:return actual_resolve(project,scenes,scope,**kwargs)
        except MaterialResolutionError as failure:
            if initial_batch_limit and scope==job.id:
                raise MaterialResolutionError(*failure.reasons,'material_analysis_batch_limit',
                    candidate_rejections=failure.candidate_rejections or {'demo':{str(original.preferred_clip_id):['interval_analysis_unavailable']}})
            raise
    resolver.resolve=resolve
    service=VideoProductionService(db,tmp_path,material_resolver=resolver,material_acquirer=acquirer)
    class ReachedPreflight(Exception):pass
    selected=[]
    def preflight(db,project,chosen,**kwargs):
        selected.extend(chosen.scenes)
        raise ReachedPreflight()
    monkeypatch.setattr('app.video_production.build_production_preflight',preflight)
    monkeypatch.setattr('app.video_production.generate_scene_plan',lambda *args,**kwargs:pytest.fail('不能重写脚本救片'))
    try:
        assert resolver.selected_materials_ready(project,[scene]) is False
        if resume_before_qa:
            with pytest.raises(InterruptedBeforeQA):service.plan(job,object(),object())
            assert events==['search','import']
        with pytest.raises(ReachedPreflight if acceptable else MaterialResolutionError):
            service.plan(job,object(),object())
        assert events==['search','import'] and len(new_assets)==1
        assert len(vision.calls)==(2 if local_present else 1)
        if local_present:
            assert {item['source_asset_id'] for item in vision.calls[0][0]}=={str(old.id)}
        assert str(new_assets[0]) in {item['source_asset_id'] for item in vision.calls[-1][0]}
        state=json.loads((tmp_path/'render-diagnostics'/str(job.id)/'external-materials.json').read_text(encoding='utf-8'))
        assert len(state['gap_scenes'])==len(state['searches'])==len(state['imports'])==1
        saved=ProjectDraftRepository(db).get(project.id)
        assert saved.script==draft.script and saved.scenes[0].voice_text==scene.voice_text
        if acceptable:
            assert ClipRepository(db).get(selected[0].preferred_clip_id).asset_id==new_assets[0]
            assert selected[0].material_request is None
        else:
            assert saved==draft
            before=len(vision.calls)
            with pytest.raises(MaterialResolutionError):service.plan(job,object(),object())
            assert events==['search','import'] and len(vision.calls)==before
    finally:db.close()
