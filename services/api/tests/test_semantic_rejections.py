"""Receipted negative use evidence; isolated ledger is real, perception/media are fixtures."""
from copy import deepcopy
import hashlib
import json
from uuid import UUID, uuid4

import pytest

from app.budget import provider_call_input_digest
from app.costs import UnknownProviderCostEstimator
from app.db import AssetRepository, ClipRepository, JobRepository, ProjectDraftRepository, ProjectRepository, ProviderCallRepository
from app.domain.models import CandidateAsset, CostCategory, UsageCost
from app.media.semantic_rejections import (SemanticUseRejected, candidate_rejections, negative_source_evidence,
    rejected_window, require_spec_semantics, source_view_uses)
from app.media.visual_facts import scene_visual_key
from app.presentation_repair import PresentationObservationRequest, PresentationRepairService
from app.routing.asset_router import AssetRouter, RoutingInputError
from test_presentation_semantic import prepared, FixtureProvider, diagnose


def _saved(c):
    return json.loads(c.db.connection.execute('SELECT payload FROM presentation_observations WHERE id=?',
        (c.observation['id'],)).fetchone()[0])


def _save(c, observed):
    c.db.connection.execute('UPDATE presentation_observations SET payload=? WHERE id=?',
        (json.dumps(observed),observed['id']))


def _receipt(c, **provider_options):
    return diagnose(c, FixtureProvider(source=False,render=False,**provider_options))


def _scene(c):
    return ProjectDraftRepository(c.db).get(c.project).scenes[1]


def _candidate(c, *, scene=None, asset=None, clip=None, start=None, end=None):
    scene = scene or _scene(c)
    asset = asset or c.asset
    clip = clip or ClipRepository(c.db).get(c.job.payload.video_spec.scenes[1].visual.clip_id)
    return CandidateAsset(scene_plan_id=scene.id,source_kind=asset.source_kind,asset_id=asset.id,clip_id=clip.id,
        playback_start_ms=start,playback_end_ms=end,match_score=.9,why=['fixture choice'],
        estimated_cost=UsageCost(category=CostCategory.USER_ASSET))


def _parent(c, name, *, duration=10000, metadata=None):
    path=c.root/(name+'.mp4');path.write_bytes(('fixture view '+name).encode())
    value=c.asset.model_copy(update={'id':uuid4(),'source_file':path.name,
        'content_hash':hashlib.sha256(path.read_bytes()).hexdigest(),'duration_ms':duration,'metadata':metadata or {}})
    return AssetRepository(c.db).create(value)


def _derive(c, child, parent, *, start, end, crop=None):
    metadata=deepcopy(child.metadata)
    metadata['source_derivation']={'source_asset_id':str(parent.id),'source_content_hash':parent.content_hash,
        'source_interval':{'start_ms':start,'end_ms':end},
        'crop_window':crop if crop is not None else {'x':0,'y':0,'width':parent.width,'height':parent.height}}
    return AssetRepository(c.db).update(child.model_copy(update={'metadata':metadata}))


def test_only_completed_paired_source_rejection_exports_without_writes(prepared):
    c=prepared
    assert negative_source_evidence(c.db,scene_visual_key(_scene(c)))==()  # Human symptom alone is not source evidence.
    machine=_receipt(c)
    assert machine['assessment_state']=='known' and machine['responsible_stage']=='material'
    before=c.db.connection.total_changes
    records=negative_source_evidence(c.db,machine['scope']['scene_key'])
    assert len(records)==1 and records[0]['call_id']==machine['call_id']
    assert records[0]['uses']==[{'source_hash':c.asset.content_hash,'start_ms':1000,'end_ms':2000}]
    assert rejected_window(c.db,machine['scope']['scene_key'],c.asset.id,1500,1700)==records
    assert c.db.connection.total_changes==before
    assert len(ProviderCallRepository(c.db).list())==1


@pytest.mark.parametrize('tamper',['input_digest','raw_result','provider','stale','unknown','assisted','failed_call','reclassified'])
def test_untrusted_or_nonmaterial_receipt_never_becomes_source_veto(prepared,tamper):
    c=prepared;machine=_receipt(c);observed=_saved(c)
    value=observed['machine_diagnosis'];calls=ProviderCallRepository(c.db);call=calls.get(UUID(machine['call_id']))
    if tamper=='input_digest':call=call.model_copy(update={'input_digest':'0'*64})
    elif tamper=='raw_result':value['raw_result']['batch']['clips'][0]['description']='altered response'
    elif tamper=='provider':value['provider']='different provider'
    elif tamper=='stale':value['stale']=True
    elif tamper=='unknown':value.update(state='unknown',assessment_state='unknown')
    elif tamper=='assisted':call=call.model_copy(update={'mode':'assisted_test'})
    elif tamper=='failed_call':call=call.model_copy(update={'status':'failed','result_payload':None})
    else:
        # Even internally consistent stored labels cannot replace reclassification of the paired response.
        for row in value['raw_result']['batch']['clips']:
            row['matches'][0].update(supported=True,subject_verified=True,score=.9)
        call=call.model_copy(update={'result_payload':{'result':deepcopy(value['raw_result'])}})
    calls.update(call);_save(c,observed)
    assert negative_source_evidence(c.db,machine['scope']['scene_key'])==()
    assert rejected_window(c.db,machine['scope']['scene_key'],c.asset.id,1000,2000)==()


@pytest.mark.parametrize('provider_options',[{'source':True,'render':False},{'source':True,'render':True},
    {'source':False,'render':False,'change':'subject_verified'}])
def test_assembly_or_uncertain_paired_observation_does_not_condemn_source(prepared,provider_options):
    c=prepared;machine=diagnose(c,FixtureProvider(**provider_options))
    assert machine['responsible_stage'] in {'assembly','unknown'}
    assert negative_source_evidence(c.db,machine['scope']['scene_key'])==()


def test_task_window_and_usage_bookkeeping_are_independent_of_negative_evidence(prepared):
    c=prepared;machine=_receipt(c);scene=_scene(c);key=machine['scope']['scene_key']
    original=ClipRepository(c.db).get(c.job.payload.video_spec.scenes[1].visual.clip_id)
    alias=ClipRepository(c.db).create(original.model_copy(update={'id':uuid4()}))
    assert candidate_rejections(c.db,scene,_candidate(c,clip=alias),fps=c.job.payload.video_spec.fps)
    assert not rejected_window(c.db,key,c.asset.id,2000,3000)
    assert not rejected_window(c.db,key,c.asset.id,0,1000)
    other=scene.model_copy(update={'voice_text':'另一种画面用途，不能沿用原结论。'})
    assert not rejected_window(c.db,scene_visual_key(other),c.asset.id,1000,2000)
    clip=ClipRepository(c.db).get(c.job.payload.video_spec.scenes[1].visual.clip_id)
    ClipRepository(c.db).update(clip.model_copy(update={'used_count':clip.used_count+1}))
    asset=AssetRepository(c.db).get(c.asset.id)
    AssetRepository(c.db).update(asset.model_copy(update={'metadata':{'bookkeeping':'changed'}}))
    assert negative_source_evidence(c.db,key)
    # A long catalogue Clip containing the bad interval is still usable outside that interval.
    safe=_candidate(c,start=3000,end=5000)
    assert not candidate_rejections(c.db,scene,safe,fps=c.job.payload.video_spec.fps)
    assert candidate_rejections(c.db,scene,_candidate(c),fps=c.job.payload.video_spec.fps)


def test_latest_same_version_observation_supersedes_but_new_run_pointer_does_not_erase_source_fact(prepared):
    c=prepared;machine=_receipt(c);key=machine['scope']['scene_key']
    c.db.connection.execute('UPDATE production_runs SET stage=?,render_job_id=NULL WHERE id=?',('cancelled',str(c.run)))
    assert negative_source_evidence(c.db,key)
    # A new version's unresolved feedback is a separate group, not a revocation of the old source receipt.
    replacement=JobRepository(c.db).create(c.job.model_copy(update={'id':uuid4(),'idempotency_key':'new-version',
        'payload':c.job.payload.model_copy(update={'render_id':uuid4()})}))
    newer={**c.observation,'id':str(uuid4()),'render_job_id':str(replacement.id),
        'request':{**c.observation['request'],'idempotency_key':'new-version'}}
    c.db.connection.execute('INSERT INTO presentation_observations VALUES (?,?,?,?,?,?)',
        (newer['id'],str(c.project),str(c.run),newer['render_job_id'],'new-version',json.dumps(newer)))
    assert negative_source_evidence(c.db,key)
    superseding={**c.observation,'id':str(uuid4()),'request':{**c.observation['request'],'idempotency_key':'new-evidence'}}
    c.db.connection.execute('INSERT INTO presentation_observations VALUES (?,?,?,?,?,?)',
        (superseding['id'],str(c.project),str(c.run),str(c.job.id),'new-evidence',json.dumps(superseding)))
    assert negative_source_evidence(c.db,key)==()


def test_full_frame_time_offsets_propagate_to_parent_without_banning_other_intervals(prepared):
    c=prepared;parent=_parent(c,'parent');grand=_parent(c,'grand',duration=14000)
    parent=_derive(c,parent,grand,start=1000,end=11000)
    _derive(c,c.asset,parent,start=2000,end=8000)
    machine=_receipt(c);key=machine['scope']['scene_key']
    frozen=machine['scope']['source']['view_source_uses']
    assert frozen==source_view_uses(c.db,c.asset.id,1000,2000)
    assert {'source_hash':parent.content_hash,'start_ms':3000,'end_ms':4000} in frozen
    assert {'source_hash':grand.content_hash,'start_ms':4000,'end_ms':5000} in frozen
    assert rejected_window(c.db,key,parent.id,3000,4000)
    assert rejected_window(c.db,key,grand.id,4000,5000)
    assert not rejected_window(c.db,key,parent.id,4000,5000)


@pytest.mark.parametrize('change',['spatial_crop','missing_crop','extra_transform','duration_mismatch'])
def test_spatial_or_unknown_derivation_retains_exact_view_without_condemning_parent(prepared,change):
    c=prepared;parent=_parent(c,'parent')
    child=_derive(c,c.asset,parent,start=2000,end=8000)
    metadata=deepcopy(child.metadata);derived=metadata['source_derivation']
    if change=='spatial_crop':derived['crop_window'].update(x=100,width=parent.width-100)
    elif change=='missing_crop':derived.pop('crop_window')
    elif change=='extra_transform':derived['crop_window']['rotation']=90
    else:derived['source_interval']['end_ms']=7000
    AssetRepository(c.db).update(child.model_copy(update={'metadata':metadata}))
    machine=_receipt(c);key=machine['scope']['scene_key']
    assert len(machine['scope']['source']['view_source_uses'])==1
    assert rejected_window(c.db,key,c.asset.id,1000,2000)
    assert not rejected_window(c.db,key,parent.id,3000,4000)


def test_legacy_receipt_replay_cannot_upgrade_exact_bytes_into_a_parent_view_claim(prepared):
    c=prepared;parent=_parent(c,'parent');_derive(c,c.asset,parent,start=2000,end=8000)
    machine=_receipt(c);observed=_saved(c);saved=observed['machine_diagnosis']
    saved['scope']['source'].pop('view_source_uses')
    saved['input']['scope']['source'].pop('view_source_uses')
    for video in saved['input']['videos']:video.pop('view_source_uses',None)
    calls=ProviderCallRepository(c.db);call=calls.get(UUID(saved['call_id']))
    digest=provider_call_input_digest(dict(project_id=str(c.project),operation='vision',provider=call.provider,
        model=call.model,input_source=call.input_source,input=saved['input']))
    calls.update(call.model_copy(update={'input_digest':digest}))
    # Simulate a pre-view-field completed call whose display projection must be recovered.
    observed.pop('machine_diagnosis');_save(c,observed)
    provider=FixtureProvider(source=False,render=False)
    restored=diagnose(c,provider)
    assert restored['call_id']==machine['call_id'] and provider.calls==0
    assert 'view_source_uses' not in restored['scope']['source']
    assert rejected_window(c.db,restored['scope']['scene_key'],c.asset.id,1000,2000)
    assert not rejected_window(c.db,restored['scope']['scene_key'],parent.id,3000,4000)
    assert len(calls.list())==1


def test_lineage_change_during_perception_preserves_completed_call_but_exports_no_negative(prepared):
    c=prepared;parent=_parent(c,'parent');grand=_parent(c,'grand',duration=14000)
    parent=_derive(c,parent,grand,start=1000,end=11000)
    _derive(c,c.asset,parent,start=2000,end=8000)
    def changed():
        metadata=deepcopy(parent.metadata);metadata['source_derivation']['crop_window']['x']=100
        AssetRepository(c.db).update(parent.model_copy(update={'metadata':metadata}))
    machine=_receipt(c,on_call=changed)
    assert machine['stale'] and machine['assessment_state']=='unknown'
    assert ProviderCallRepository(c.db).get(UUID(machine['call_id'])).status=='completed'
    assert negative_source_evidence(c.db,machine['scope']['scene_key'])==()


def test_router_preferred_and_explicit_choices_cannot_override_receipted_negative(prepared):
    c=prepared;_receipt(c);scene=_scene(c)
    clip=ClipRepository(c.db).get(c.job.payload.video_spec.scenes[1].visual.clip_id)
    scene=scene.model_copy(update={'preferred_clip_id':clip.id})
    class Search:
        def search(self,*args,**kwargs):raise AssertionError('preferred Clip must not search')
    router=AssetRouter(Search(),AssetRepository(c.db))
    result=router.route(scene)
    assert all(candidate.clip_id!=clip.id for candidate in result.candidates)
    with pytest.raises(RoutingInputError,match='source_semantic_use_rejected'):
        router.explicit_footage(scene,[_candidate(c,scene=scene)])
    other=scene.model_copy(update={'voice_text':'另一个允许的用途。'})
    assert router.explicit_footage(other,[_candidate(c,scene=other)]).clip_id==clip.id


def test_qualified_material_selection_and_local_replacement_consume_the_same_negative(prepared):
    from app.assembly.edit_plan import _default_edit_plan
    from app.media.planning import VisualMaterialResolver
    from app.presentation_material import replacement_options
    from test_material_repair import add_footage
    c=prepared;c.scene=_scene(c)
    safe,safe_clip,_=add_footage(c,name='safe-usage')
    rejected=c.asset;old_clip=ClipRepository(c.db).get(c.job.payload.video_spec.scenes[1].visual.clip_id)
    facts=deepcopy(safe.metadata['visual_analysis'][str(safe_clip.id)])
    facts.update(source_content_hash=rejected.content_hash,clip_interval={'start_ms':1000,'end_ms':5500},
        covered_start_ms=1000,covered_end_ms=5500,analysis_duration_ms=4500)
    AssetRepository(c.db).update(rejected.model_copy(update={'metadata':{'visual_analysis':{str(old_clip.id):facts}}}))
    project=ProjectRepository(c.db).get(c.project)
    resolver=VisualMaterialResolver(c.db,c.root,None,c.media,c.media,UnknownProviderCostEstimator())
    # The current failed scene can now use different bytes; the historical source veto still applies.
    spec=c.job.payload.video_spec
    new_visual=spec.scenes[1].visual.model_copy(update={'asset_id':safe.id,'clip_id':safe_clip.id,
        'clip_start_ms':safe_clip.start_ms,'clip_end_ms':safe_clip.end_ms,'authorization_reference':safe.authorization_reference})
    draft=ProjectDraftRepository(c.db).get(c.project)
    choices={scene.id:_candidate(c,scene=scene,asset=safe,clip=safe_clip) for scene in draft.scenes}
    plan=_default_edit_plan(project,draft.scenes,choices)
    spec=spec.model_copy(update={'scenes':[spec.scenes[0].model_copy(update={'visual':new_visual}),
        spec.scenes[1].model_copy(update={'visual':new_visual})],'edit_plan':plan})
    assert any(option['candidate'].asset_id==rejected.id for option in replacement_options(
        c.db,c.root,spec,c.scene,style=plan.style_tokens,run_id=c.run))
    _receipt(c)
    selected=resolver._select(project,[c.scene],{str(old_clip.id):(rejected,old_clip),str(safe_clip.id):(safe,safe_clip)},None)
    assert selected[0].preferred_clip_id==safe_clip.id
    options=replacement_options(c.db,c.root,spec,c.scene,style=plan.style_tokens,run_id=c.run)
    assert not any(option['candidate'].asset_id==rejected.id for option in options)
    assert len(ProviderCallRepository(c.db).list())==1


def test_assembled_task_key_rechecks_late_negative_before_render(prepared):
    from app.assembly.edit_plan import _default_edit_plan, review_edit_plan
    c=prepared;draft=ProjectDraftRepository(c.db).get(c.project);project=ProjectRepository(c.db).get(c.project)
    selections={scene.id:_candidate(c,scene=scene) for scene in draft.scenes}
    plan=review_edit_plan(_default_edit_plan(project,draft.scenes,selections),draft.scenes,selections,
        assets=AssetRepository(c.db))
    spec=c.job.payload.video_spec.model_copy(update={'edit_plan':plan})
    require_spec_semantics(c.db,spec)
    _receipt(c)
    with pytest.raises(SemanticUseRejected) as failure:
        require_spec_semantics(c.db,spec)
    assert {hit['scene_id'] for hit in failure.value.hits}=={'detail'}
    assert len(ProviderCallRepository(c.db).list())==1
