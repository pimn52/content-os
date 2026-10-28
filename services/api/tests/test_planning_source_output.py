"""规划使用装配层事实；本地夹具不代表实际出片质量。"""
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.assembly.edit_plan import source_subtitle_updates
from app.db import AssetRepository,ClipRepository,ProviderCallRepository
from app.media.vision import planning_visual_evidence
from app.providers.scene_planner import validate_observed_materials,ScenePlannerProviderResponseError
from app.scene_planning import _scene_planning_context
from test_material_resolution import material_library


@pytest.mark.parametrize('topic',['手冲咖啡的水温','城市夜景拍摄'])
@pytest.mark.parametrize('wide',[False,True])
def test_prepared_material_uses_same_crop_as_assembly_without_creating_portrait_crop(tmp_path,topic,wide):
    db,project,asset,_,_,provider,resolver=material_library(tmp_path)
    original=provider.analyze_intervals
    def observed(videos,requests):
        result=original(videos,requests)
        for row in result.clips:
            row.subtitle_regions=[[0,.9,1,.08]]
            row.protected_regions=[[.1,.1,.5,.5]]
        return result
    provider.analyze_intervals=observed
    try:
        if wide:
            asset=AssetRepository(db).update(asset.model_copy(update={'width':1920,'height':1080}))
            probe=resolver.probe.probe
            resolver.probe=SimpleNamespace(probe=lambda path:SimpleNamespace(
                **{**vars(probe(path)),'width':1920,'height':1080}))
        prepared=resolver.prepare(project,project.topic,uuid4())
        assert prepared
        project=project.model_copy(update={'topic':topic})
        context,_=_scene_planning_context(db,project,topic=topic,material_clip_ids=prepared)
        for material in context['materials']:
            clip=ClipRepository(db).get(next(value for value in prepared if str(value)==material['clip_id']))
            asset=AssetRepository(db).get(clip.asset_id)
            interval=(material['start_ms'],material['end_ms'])
            output=material['visual_evidence']['output_presentation']
            assert output['available'] and output['output_format']=='vertical'
            assert output['source_bottom_crop_ratio']==source_subtitle_updates(asset,clip,interval)['source_bottom_crop_ratio']
            assert output['landscape_secondary'] is wide
            assert output['layouts']==['full_frame_contain']
            assert output['quality_state']=='unverified'
    finally:db.close()


def test_known_unpresentable_source_is_not_reanalyzed_or_promised_to_next_plan(tmp_path):
    db,project,_,_,_,provider,resolver=material_library(tmp_path,metered=True)
    try:
        prepared=resolver.prepare(project,project.topic,uuid4())
        calls=len(provider.calls)
        for identity in prepared:
            clip=ClipRepository(db).get(identity)
            asset=AssetRepository(db).get(clip.asset_id)
            metadata=json.loads(json.dumps(asset.metadata))
            facts=metadata['visual_analysis'][str(identity)]
            facts['source_subtitle_clearance_verified']=False
            AssetRepository(db).update(asset.model_copy(update={'metadata':metadata}))
            evidence=planning_visual_evidence(db,AssetRepository(db).get(asset.id),clip,project=project)
            assert 'usable_interval' not in evidence
            assert evidence['output_presentation']=={'available':False,'reason':'source_subtitle_clearance_required'}
        assert resolver.prepare(project,project.topic,uuid4())==()
        assert len(provider.calls)==calls
        context,_=_scene_planning_context(db,project,topic=project.topic,material_clip_ids=())
        assert context['materials']==[] and context['production_feasibility']
        assert len(ProviderCallRepository(db).list_for_project(project.id))==calls
    finally:db.close()


def test_planner_cannot_select_known_blocked_material_even_if_old_interval_is_supplied(tmp_path):
    db,_,_,_,scene,_,_=material_library(tmp_path)
    try:
        material={'clip_id':str(scene.preferred_clip_id),'source_kind':'user_asset',
            'visual_evidence':{'output_presentation':{'available':False,'reason':'source_subtitle_clearance_required'}}}
        with pytest.raises(ScenePlannerProviderResponseError) as failure:
            validate_observed_materials([scene],[material],require_clip_selection=True)
        assert failure.value.domain_field=='preferred_clip_id'
    finally:db.close()
