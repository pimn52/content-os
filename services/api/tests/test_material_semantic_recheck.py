"""Actual recheck boundary over isolated repair/ledger fixtures, without real perception."""
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from app.budget import ProviderCallLedger, provider_call_input_digest
from app.costs import CatalogProviderCostEstimator
from app.db import AssetRepository, BudgetPolicyRepository, ClipRepository, FeedbackRepository, JobRepository, ProviderCallRepository
from app.domain.models import BudgetPolicy, ContentFeedback, CostCategory, JobStatus, JobType, UsageCost, VideoSpec
from app.presentation_repair import PresentationObservationRequest
from app.production_runs import ProductionRunNotReady
from app.presentation_semantic import PresentationSemanticDiagnosis, material_recheck_is_current
from app.source_usage import SourceUsageService
from app.video_production import VideoProductionService
from test_material_repair import material, prepared, add_footage, body, diagnose_material
from test_presentation_semantic import FixtureProvider


@pytest.fixture
def repaired(material):
    return complete_repair(material)


def complete_repair(c):
    c.replacement,c.replacement_clip,c.replacement_path=add_footage(c)
    diagnose_material(c)
    plan=c.service.plan(c.project,c.run,UUID(c.observation['id']))
    assert plan['action']=='render_revision',plan['stop_reasons']
    c.repair=c.service.apply(c.project,c.run,body(c,plan))
    job=JobRepository(c.db).get(UUID(c.repair['replacement_job_id']))
    c.replacement_job=JobRepository(c.db).update(job.model_copy(update={'status':JobStatus.COMPLETED}))
    c.replacement_output=c.root/'renders'/str(c.project)/f'{job.payload.render_id}.mp4'
    c.replacement_output.write_bytes(b'fixture new rendered scene, not a quality verdict')
    technical={'state':'verified','decode':'pass','sha256':hashlib.sha256(c.replacement_output.read_bytes()).hexdigest(),
        'fps':job.payload.video_spec.fps.model_dump(mode='json'),'video_frames':60,'scope':'technical_only_final_u_product_required'}
    c.db.connection.execute('UPDATE presentation_repairs SET technical_qa=? WHERE id=?',(json.dumps(technical),c.repair['id']))
    ledger=ProviderCallLedger(c.db)
    reserved=ledger.reserve_execution(project_id=c.project,idempotency_key=f"presentation-repair:{c.repair['id']}:render:0",
        operation='render',mode='runtime',provider='fixture-render',model='fixture-drawing',input_source=f'render:{job.payload.render_id}',
        input_digest=provider_call_input_digest(job.payload.model_dump(mode='json')),
        estimated_cost=UsageCost(category=CostCategory.RENDER,amount=Decimal('0'),currency='USD'))
    ledger.finish(project_id=c.project,call_id=reserved.record.id,status='completed',usage_observable=False,
        actual_cost=UsageCost(category=CostCategory.RENDER,amount=Decimal('0'),currency='USD'))
    c.initial_observations=c.db.connection.execute('SELECT COUNT(*) FROM presentation_observations').fetchone()[0]
    c.initial_calls=len(ProviderCallRepository(c.db).list())
    c.media.calls.clear()
    return c


def verifier(c,provider=None,estimator=None):
    provider=provider or FixtureProvider(source=True,render=True)
    return PresentationSemanticDiagnosis(c.db,c.root,provider,c.media,c.media,estimator),provider


def check(c,service):
    return service.verify_material_repair(c.project,c.run,c.repair['id'])


def current(c,machine):
    row=c.db.connection.execute('SELECT * FROM production_runs WHERE id=?',(str(c.run),)).fetchone()
    return material_recheck_is_current(c.db,machine,row)


def drop_projection(c):
    raw=json.loads(c.db.connection.execute('SELECT payload FROM presentation_repairs WHERE id=?',(c.repair['id'],)).fetchone()[0])
    raw.pop('semantic_recheck',None)
    c.db.connection.execute('UPDATE presentation_repairs SET payload=? WHERE id=?',(json.dumps(raw),c.repair['id']))


def test_fresh_actual_pair_passes_only_this_machine_check_without_creating_observations(repaired):
    c=repaired;service,provider=verifier(c)
    result=check(c,service)
    assert result['state']=='pass' and result['assessment_state']=='known' and not result['stale']
    assert current(c,result)
    assert result['scope']['source']['view_source_uses']
    assert result['input']['scope']==result['scope'] and result['raw_result']['batch']['clips']
    assert result['binding']['render_sha256']==hashlib.sha256(c.replacement_output.read_bytes()).hexdigest()
    call=ProviderCallRepository(c.db).get(UUID(result['call_id']))
    assert call.idempotency_key==f"presentation-semantic:{c.run}:{c.replacement_job.id}:repair:{c.repair['id']}"
    assert call.input_source==f"presentation-repair:{c.repair['id']}" and call.status=='completed'
    assert 'human_quality_review_required' in result['limitations'] and 'audio_not_observed' in result['limitations']
    assert 'only_replaced_scene_visual_semantics_rechecked' in result['limitations']
    assert c.db.connection.execute('SELECT COUNT(*) FROM presentation_observations').fetchone()[0]==c.initial_observations
    assert check(c,service)==result and provider.calls==1 and len(c.media.calls)==2
    assert len(ProviderCallRepository(c.db).list())==c.initial_calls+1


def test_normal_usage_counters_do_not_invalidate_current_media_or_receipt(repaired):
    c=repaired
    SourceUsageService(c.db).record_render(c.replacement_job)
    service,provider=verifier(c)
    result=check(c,service)
    assert result['state']=='pass',result['stop_reasons']
    clip=ClipRepository(c.db).get(c.replacement_clip.id)
    ClipRepository(c.db).update(clip.model_copy(update={'used_count':clip.used_count+1,'last_used_at':datetime.now(timezone.utc)}))
    assert current(c,result) and check(c,service)==result and provider.calls==1


@pytest.mark.parametrize('case,expected,owner',[
    ('negative','failed','material'),('assembly_negative','failed','assembly'),('unknown','unknown','unknown'),
    ('invalid_fields','unknown','unknown'),('timeout','failed','unknown')])
def test_failed_or_uncertain_actual_recheck_keeps_evidence_and_does_not_retry(repaired,case,expected,owner):
    c=repaired
    provider=FixtureProvider(source=case!='negative',render=case not in {'negative','assembly_negative'},
        change='quality_adequate' if case=='unknown' else None,fail=case=='timeout')
    if case=='invalid_fields':provider.last_validation_issues=[{'code':'fixture_invalid'}]
    service,_=verifier(c,provider)
    result=check(c,service)
    assert result['state']==expected and result['responsible_stage']==owner,result
    assert result['call_id'] is not None
    before=list(c.media.calls)
    assert check(c,service)==result and provider.calls==1 and c.media.calls==before
    assert len(ProviderCallRepository(c.db).list())==c.initial_calls+1
    assert c.db.connection.execute('SELECT COUNT(*) FROM presentation_observations').fetchone()[0]==c.initial_observations


@pytest.mark.parametrize('cause',['configuration','budget','technical_hash','source_bytes','new_version'])
def test_preflight_refuses_before_proxy_extraction_and_never_sends_a_provider_request(repaired,cause):
    c=repaired;service,provider=verifier(c)
    if cause=='configuration':provider.supports_video_input=False
    if cause=='budget':
        policy=BudgetPolicyRepository(c.db).get_by_project(c.project)
        BudgetPolicyRepository(c.db).save(policy.model_copy(update={'max_calls':c.initial_calls}))
    if cause=='technical_hash':
        c.replacement_output.write_bytes(b'changed output after technical check')
    if cause=='source_bytes':c.replacement_path.write_bytes(b'changed source')
    if cause=='new_version':c.db.connection.execute('UPDATE production_runs SET render_job_id=? WHERE id=?',(str(c.job.id),str(c.run)))
    result=check(c,service)
    assert result['state']=='unknown' and result['call_id'] is None
    assert provider.calls==0 and c.media.calls==[]
    assert len(ProviderCallRepository(c.db).list())==c.initial_calls


def test_inference_input_change_retains_consumed_usage_and_marks_old_repair_stale(repaired):
    c=repaired
    provider=FixtureProvider(source=True,render=True,on_call=lambda:c.replacement_path.write_bytes(b'changed during model call'))
    service,_=verifier(c,provider)
    result=check(c,service)
    assert result['state']=='unknown' and result['stale'] and not current(c,result)
    call=ProviderCallRepository(c.db).get(UUID(result['call_id']))
    assert call.status=='completed' and call.input_tokens==100 and call.output_tokens==20
    assert check(c,service)['stale'] and provider.calls==1


def test_completed_receipt_restores_missing_projection_even_after_budget_is_exhausted(repaired):
    c=repaired;service,provider=verifier(c)
    first=check(c,service);assert first['state']=='pass'
    drop_projection(c)
    policy=BudgetPolicyRepository(c.db).get_by_project(c.project)
    BudgetPolicyRepository(c.db).save(policy.model_copy(update={'max_calls':0,'allow_unknown_cost':False}))
    restored=check(c,service)
    assert restored['state']=='pass' and restored['call_id']==first['call_id'] and restored['raw_result']==first['raw_result']
    assert current(c,restored) and provider.calls==1


@pytest.mark.parametrize('status',['failed','running'])
def test_missing_projection_for_running_or_failed_receipt_does_not_extract_or_resend(repaired,status):
    c=repaired
    def interrupt():raise KeyboardInterrupt('fixture crash before receipt')
    provider=FixtureProvider(source=True,render=True,fail=status=='failed',on_call=interrupt if status=='running' else None)
    service,_=verifier(c,provider)
    if status=='running':
        with pytest.raises(KeyboardInterrupt):check(c,service)
    else:check(c,service)
    drop_projection(c);before=list(c.media.calls);provider.on_call=None;provider.fail=False
    result=check(c,service)
    assert result['call_status']==status and result['state']!='pass'
    assert provider.calls==1 and c.media.calls==before


def test_lightweight_current_checks_receipt_and_ignores_no_media_or_qa_fields(repaired,monkeypatch):
    c=repaired;service,_=verifier(c);result=check(c,service)
    monkeypatch.setattr(service.presentation,'_hash_render',lambda *args:pytest.fail('metadata current hashed media'))
    assert current(c,result)
    altered=json.loads(json.dumps(result));altered['state']='pass';altered['raw_result']['batch']['clips'][0]['matches'][0]['supported']=False
    assert not current(c,altered)
    clip=ClipRepository(c.db).get(c.replacement_clip.id)
    ClipRepository(c.db).update(clip.model_copy(update={'quality_score':.1}))
    assert not current(c,result)


def test_admission_preserves_unknown_price_and_checks_both_global_and_project_tail(repaired):
    c=repaired;service,_=verifier(c)
    before=c.db.connection.total_changes
    admitted=service.material_recheck_admission(c.project,remaining_render_calls=1)
    assert admitted['state']=='ready' and admitted['required_calls']==2 and admitted['estimated_cost']['amount'] is None
    assert c.db.connection.total_changes==before
    policy=BudgetPolicyRepository(c.db).get_by_project(c.project)
    BudgetPolicyRepository(c.db).save(policy.model_copy(update={'max_calls':c.initial_calls+1}))
    assert service.material_recheck_admission(c.project,remaining_render_calls=1)['stop_reasons']==['semantic_budget_call_limit']
    assert service.material_recheck_admission(c.project,remaining_render_calls=0)['state']=='ready'
    BudgetPolicyRepository(c.db).save(policy.model_copy(update={'max_calls':20,'allow_unknown_cost':False}))
    assert service.material_recheck_admission(c.project)['stop_reasons']==['semantic_budget_unknown_cost']
    BudgetPolicyRepository(c.db).save(policy)
    BudgetPolicyRepository(c.db).save(BudgetPolicy(max_calls=c.initial_calls+1,allow_unknown_cost=True,updated_at=datetime.now(timezone.utc)))
    assert service.material_recheck_admission(c.project)['stop_reasons']==['semantic_budget_call_limit']


def test_admission_uses_the_same_visual_cost_estimator_instead_of_claiming_zero(repaired):
    c=repaired;provider=FixtureProvider(source=True,render=True)
    estimator=CatalogProviderCostEstimator({('vision',provider.provider_name,provider.model):
        UsageCost(category=CostCategory.LLM,amount=Decimal('2.00'),currency='USD')})
    policy=BudgetPolicyRepository(c.db).get_by_project(c.project)
    BudgetPolicyRepository(c.db).save(policy.model_copy(update={'max_amount':Decimal('1.00')}))
    service,_=verifier(c,provider,estimator)
    result=service.material_recheck_admission(c.project)
    assert result['state']=='unknown' and result['stop_reasons']==['semantic_budget_amount_limit']
    assert Decimal(result['estimated_cost']['amount'])==Decimal('2.00')


@pytest.mark.parametrize('source,render,expected',[(False,False,True),(True,False,False),(True,True,False)])
def test_recheck_source_failures_are_reused_by_later_selection_without_new_observations(repaired,source,render,expected):
    from app.media.semantic_rejections import negative_source_evidence,rejected_window
    c=repaired;service,provider=verifier(c,FixtureProvider(source=source,render=render))
    result=check(c,service)
    records=negative_source_evidence(c.db,result['scope']['scene_key'])
    assert any(record.get('repair_id')==c.repair['id'] for record in records)==expected
    assert bool(rejected_window(c.db,result['scope']['scene_key'],c.replacement.id,
        result['scope']['source']['start_ms'],result['scope']['source']['end_ms']))==expected
    assert c.db.connection.execute('SELECT COUNT(*) FROM presentation_observations').fetchone()[0]==c.initial_observations
    assert provider.calls==1
    if expected:
        call=ProviderCallRepository(c.db).get(UUID(result['call_id']))
        ProviderCallRepository(c.db).update(call.model_copy(update={'input_digest':'0'*64}))
        assert not any(record.get('repair_id')==c.repair['id'] for record in negative_source_evidence(c.db,result['scope']['scene_key']))


@pytest.mark.parametrize('case,owner',[
    ('material','material'),('assembly','assembly'),('uncertain','unknown'),('timeout','unknown')])
def test_ordinary_center_stops_at_recheck_owner_and_preserves_all_work_without_another_attempt(repaired,case,owner):
    c=repaired
    provider=FixtureProvider(source=case!='material',render=case not in {'material','assembly'},
        change='quality_adequate' if case=='uncertain' else None,fail=case=='timeout')
    service,_=verifier(c,provider)
    center=VideoProductionService(c.db,c.root,semantic_diagnoser=service)
    jobs=JobRepository(c.db).list();old_bytes=c.output.read_bytes();new_bytes=c.replacement_output.read_bytes()
    assert center.status(c.project)['next_action']=='verify_material'
    assert center.queue_jobs({JobType.RENDER},run_id=c.run)==[]
    result=center.status(c.project)
    assert result['next_action']=='stopped' and result['responsible_stage']==owner,result
    assert result['material_recheck']['state']!='pass'
    for _ in range(2):
        center.queue_jobs({JobType.RENDER},run_id=c.run)
        assert center.status(c.project)['next_action']=='stopped'
    assert provider.calls==1 and len(ProviderCallRepository(c.db).list())==c.initial_calls+1
    assert JobRepository(c.db).list()==jobs and c.output.read_bytes()==old_bytes and c.replacement_output.read_bytes()==new_bytes
    assert c.db.connection.execute('SELECT COUNT(*) FROM presentation_observations').fetchone()[0]==c.initial_observations


def test_non_render_worker_leaves_recheck_pending_and_changed_pass_never_enters_review(repaired):
    c=repaired;service,provider=verifier(c)
    center=VideoProductionService(c.db,c.root,semantic_diagnoser=service)
    assert center.queue_jobs({JobType.GENERATE_VOICE},run_id=c.run)==[]
    assert provider.calls==0 and center.status(c.project)['next_action']=='verify_material'
    center.queue_jobs({JobType.RENDER},run_id=c.run)
    assert provider.calls==1 and center.status(c.project)['next_action']=='review'
    clip=ClipRepository(c.db).get(c.replacement_clip.id)
    ClipRepository(c.db).update(clip.model_copy(update={'quality_score':.1}))
    status=center.status(c.project)
    assert status['next_action']=='stopped' and status['responsible_stage']=='unknown'
    assert status['stop_reasons']==['material_semantic_recheck_stale']
    center.queue_jobs({JobType.RENDER},run_id=c.run)
    assert provider.calls==1


@pytest.mark.parametrize('case',['pending','uncertain','assembly_failed','pass'])
def test_another_assembly_observation_cannot_drop_the_unresolved_material_recheck(material,case):
    c=material
    c.db.connection.execute('UPDATE production_runs SET repair_allowance=2 WHERE id=?',(str(c.run),))
    raw=c.job.payload.video_spec.model_dump(mode='json')
    intro=raw['scenes'][0]
    text='先明确使用场景。'
    intro['caption']=text
    intro['subtitle_treatment']=raw['edit_plan']['scenes'][0]['subtitle_treatment']='timed_captions'
    intro['graphic_treatment']=raw['edit_plan']['scenes'][0]['graphic_treatment']='key_point'
    intro['graphic_text']=raw['edit_plan']['scenes'][0]['graphic_text']=text
    c.job=JobRepository(c.db).update(c.job.model_copy(update={
        'payload':c.job.payload.model_copy(update={'video_spec':VideoSpec.model_validate(raw)})}))
    c=complete_repair(c)
    service,provider=verifier(c,FixtureProvider(source=True,render=case!='assembly_failed',
        change='quality_adequate' if case in {'pending','uncertain'} else None))
    if case!='pending':check(c,service)
    observation=c.service.observe(c.project,c.run,PresentationObservationRequest(
        idempotency_key='new-assembly-note',render_sha256=hashlib.sha256(c.replacement_output.read_bytes()).hexdigest(),
        scene_id=intro['scene_id'],finding='duplicate_text',reason='fixture exact duplicate',evidence_reference='fixture:new-output'))
    plan=c.service.plan(c.project,c.run,UUID(observation['id']))
    center=VideoProductionService(c.db,c.root,semantic_diagnoser=service)
    if case=='pass':
        assert plan['action']=='render_revision',plan['stop_reasons']
        assert center.status(c.project)['next_action']=='diagnose_quality'
        request=body(c,plan,key='follow-up-assembly').model_copy(update={'observation_id':UUID(observation['id'])})
        next_repair=c.service.apply(c.project,c.run,request)
        next_job=JobRepository(c.db).get(UUID(next_repair['replacement_job_id']))
        assert c.service.require_current_repair(next_job)['id']==next_repair['id']
        next_job=JobRepository(c.db).update(next_job.model_copy(update={'status':JobStatus.COMPLETED}))
        next_output=c.root/'renders'/str(c.project)/f'{next_job.payload.render_id}.mp4'
        next_output.write_bytes(b'fixture completed assembly successor')
        next_observation=c.service.observe(c.project,c.run,PresentationObservationRequest(
            idempotency_key='another-successor-note',render_sha256=hashlib.sha256(next_output.read_bytes()).hexdigest(),
            scene_id=intro['scene_id'],finding='duplicate_text',reason='fixture later observation',evidence_reference='fixture:successor'))
        drop_projection(c)
        with pytest.raises(ProductionRunNotReady,match='material_semantic_recheck_required'):
            c.service.require_current_repair(next_job)
        later_plan=c.service.plan(c.project,c.run,UUID(next_observation['id']))
        assert 'material_semantic_recheck_required' in later_plan['stop_reasons']
    else:
        expected='material_semantic_recheck_required' if case=='pending' else 'material_semantic_recheck_not_passed'
        assert expected in plan['stop_reasons'] and plan['candidate_spec'] is None
        assert center.status(c.project)['next_action']==('verify_material' if case=='pending' else 'stopped')
        jobs=JobRepository(c.db).list()
        center.queue_jobs({JobType.RENDER},run_id=c.run)
        assert center.status(c.project)['next_action']=='stopped'
        assert JobRepository(c.db).list()==jobs and provider.calls==1


@pytest.mark.parametrize('scope',['draft','run','old_render','current_render'])
@pytest.mark.parametrize('accepted',[True,False])
def test_only_exact_current_output_feedback_can_retire_its_pending_recheck(repaired,scope,accepted):
    c=repaired;service,provider=verifier(c)
    version={'draft':'draft-1','run':f'run:{c.run}','old_render':f'render:{c.job.payload.render_id}',
        'current_render':f'render:{c.replacement_job.payload.render_id}'}[scope]
    FeedbackRepository(c.db).create(ContentFeedback(project_id=c.project,output_version=version,accepted=accepted,
        created_at=datetime.now(timezone.utc),notes='fixture scoped feedback'))
    center=VideoProductionService(c.db,c.root,semantic_diagnoser=service)
    assert center.status(c.project)['next_action']==('review' if scope=='current_render' else 'verify_material')
    center.queue_jobs({JobType.RENDER},run_id=c.run)
    assert provider.calls==(0 if scope=='current_render' else 1)
    assert center.status(c.project)['next_action']=='review'


@pytest.mark.parametrize('accepted',[True,False])
def test_exact_human_review_stops_fresh_work_but_does_not_erase_existing_visual_receipt(repaired,accepted):
    c=repaired;service,provider=verifier(c)
    first=check(c,service)
    FeedbackRepository(c.db).create(ContentFeedback(project_id=c.project,
        output_version=f'render:{c.replacement_job.payload.render_id}',accepted=accepted,
        created_at=datetime.now(timezone.utc)))
    assert current(c,first) and check(c,service)==first and provider.calls==1
    drop_projection(c)
    result=check(c,service)
    assert result['state']=='unknown' and result['stop_reasons']==['semantic_recheck_output_retired']
    assert provider.calls==1
