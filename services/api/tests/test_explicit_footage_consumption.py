from datetime import datetime,timezone
from types import SimpleNamespace
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from app.db import Database,AssetRepository,ClipRepository
from app.domain.models import Asset,Clip,CandidateAsset,RationalFps,SourceKind,UsageCost
from app.routing.asset_router import AssetRouter,RoutingInputError
from app.search import ClipTextSearchService
from app.evaluation_master import _master_stage_blockers
from app.main import create_app
from test_production_preflight import _create_draft

def footage(db,scene,tmp_path):
    asset=AssetRepository(db).create(Asset(source_file=str(tmp_path/'fixture.mp4'),content_hash='a'*64,
        duration_ms=5000,width=1280,height=720,fps=RationalFps(numerator=30,denominator=1),
        authorization_reference='fixture-only',imported_at=datetime.now(timezone.utc)))
    clip=ClipRepository(db).create(Clip(asset_id=asset.id,start_ms=0,end_ms=5000,asset_duration_ms=5000))
    choice=CandidateAsset(scene_plan_id=scene.id,source_kind='user_asset',asset_id=asset.id,clip_id=clip.id,
        match_score=0,why=['explicit fixture selection; not semantic QA'],recommended=False,
        estimated_cost=UsageCost(category='user_asset',amount=0,currency='USD'))
    return asset,clip,choice

def test_saved_explicit_footage_beats_missing_index_without_approving_suitability(tmp_path):
    path=tmp_path/'fixture.sqlite3'
    with TestClient(create_app(path)) as c:
        project,scene=_create_draft(c)
        with Database(path) as db:asset,clip,choice=footage(db,scene,tmp_path)
        c.put(f'/projects/{project}/draft',json=dict(script=scene.voice_text,topic='New topic',
            scenes=[scene.model_dump(mode='json')],confirmed=[choice.model_dump(mode='json')])).raise_for_status()
        r=c.post(f'/projects/{project}/production-preflight',json={});assert r.status_code==200,r.text
        state=r.json();decision=state['scenes'][0]
        assert decision['selected_candidate']['clip_id']==str(clip.id)
        assert decision['selected_candidate']['match_score']==0
        assert decision['production_need']=='none' and decision['suitability']=='unknown'
        assert 'capture_required:hook' not in state['stop_reasons']
        assert 'source_suitability_unknown:hook' in state['stop_reasons']
        assert not state['dispatch_performed'] and state['status']=='blocked'
        assert c.get(f'/projects/{project}/provider-calls').json()==[]

@pytest.mark.parametrize('mutation',['foreign_project','wrong_clip','short','creator'])
def test_explicit_selection_does_not_waive_source_checks(tmp_path,mutation):
    from test_production_preflight import _scene
    with Database(tmp_path/'source.sqlite3') as db:
        scene=_scene(str(uuid4()));asset,clip,choice=footage(db,scene,tmp_path)
        if mutation=='foreign_project':asset=asset.model_copy(update={'metadata':{'project_id':str(uuid4())}})
        elif mutation=='wrong_clip':choice=choice.model_copy(update={'clip_id':uuid4()})
        elif mutation=='short':scene=scene.model_copy(update={'duration_target_ms':6000})
        elif mutation=='creator':scene=scene.model_copy(update={'visual_requirement':'creator_speaking'})
        AssetRepository(db).update(asset)
        router=AssetRouter(ClipTextSearchService(db),AssetRepository(db))
        with pytest.raises(RoutingInputError,match='explicit_footage_selection'):
            router.explicit_footage(scene,[choice])


def test_explicit_selection_ignores_legacy_usage_tag(tmp_path):
    from test_production_preflight import _scene
    with Database(tmp_path/'source.sqlite3') as db:
        scene=_scene(str(uuid4()));asset,_,choice=footage(db,scene,tmp_path)
        AssetRepository(db).update(asset.model_copy(update={'metadata':{'r1_usage':'unknown'}}))
        router=AssetRouter(ClipTextSearchService(db),AssetRepository(db))
        assert router.explicit_footage(scene,[choice]) == choice

def test_master_stage_only_defers_explicit_future_footage_and_preserves_other_gates():
    choice=object();scene=SimpleNamespace(scene_id='broll',production_need='none',selected_candidate=choice,planned_source_kind=SourceKind.USER_ASSET)
    reasons=('source_suitability_unknown:broll','source_suitability_unknown:other','capture_required:missing','talking_source_not_admitted:speaker')
    context=dict(draft=SimpleNamespace(confirmed=[choice]),plan=SimpleNamespace(scenes=[scene],stop_reasons=reasons))
    assert _master_stage_blockers(context)==['source_suitability_unknown:other','capture_required:missing']
    assert context['plan'].stop_reasons==reasons
    context['draft'].confirmed=[]
    assert 'source_suitability_unknown:broll' in _master_stage_blockers(context)
