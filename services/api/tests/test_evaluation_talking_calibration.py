"""Normal Talking bootstrap with real rights resolvers, synthetic children only."""
import json
import hashlib
from uuid import uuid4
import pytest
from app.db import JobRepository, ProviderMachineCapabilityProfileRepository
from app.domain.models import ProviderUseLicenseReviewV5
from app.domain.evaluation_talking import (CALIBRATION_POLICY, TalkingCalibrationCandidate, TalkingCalibrationIntent,
    TalkingCalibrationSelection, TalkingCalibrationStart, TalkingSelectionRecord, TalkingOperationRecord)
from app.execution_admission import configuration_digest, UseAdmissionError
from pydantic import TypeAdapter
from test_execution_admission import OPERATOR_KEY
from test_evaluation_talking_normal_inputs import normal_inputs, qa_case, bound_job, child_case, setup
from test_evaluation_talking_dual_normal import talking_case, original_talking_case, candidate
from test_evaluation_talking_normal_flow import save, http


def test_normal_calibration_intent_http_accepts_json_spec_but_keeps_strict_values(normal_inputs):
    c=normal_inputs
    selected=TalkingCalibrationCandidate(**c.selection.model_dump(exclude={'policy','execution_admission_id','use_admission_id'}),
        policy=CALIBRATION_POLICY,trial_duration_limit_ms=10000,trial_processing_limit_ms=12000)
    payload=TalkingCalibrationIntent(selection=selected,execution_specification=c.spec).model_dump(mode='json')
    response=http(c,'POST',f'/projects/{c.project}/evaluation-talking/calibration-intent',json=payload)
    assert response.status_code==200,response.text
    assert response.json()['trial_input_sha256']==c.service.calibration_intent(c.project,
        TalkingCalibrationIntent.model_validate_json(json.dumps(payload)))['trial_input_sha256']
    payload['execution_specification']['timeout_seconds']=str(payload['execution_specification']['timeout_seconds'])
    assert http(c,'POST',f'/projects/{c.project}/evaluation-talking/calibration-intent',json=payload).status_code==422


def test_current_adopted_source_supplies_unknown_retrieval_fields_without_writing_scores(normal_inputs):
    from app.db import ClipRepository
    c=normal_inputs;clips=ClipRepository(c.db)
    unknown=c.clip.model_copy(update={'talking_candidate':False,'face_visibility':None,'mouth_visibility':None})
    clips.update(unknown)
    assert c.service._inputs(c.project,c.selection)['assessment'].id==c.sources.require_current(c.receipt).id
    assert clips.get(unknown.id)==unknown
    clips.update(unknown.model_copy(update={'mouth_visibility':0.2}))
    with pytest.raises(UseAdmissionError,match='talking_reference_fit_not_verified'):
        c.service._inputs(c.project,c.selection)
    clips.update(unknown)
    c.sources.revoke(c.receipt.id,actor_id='fixture-operator',reason='Synthetic revocation')
    with pytest.raises(ValueError,match='revoked'):
        c.service._inputs(c.project,c.selection)


@pytest.mark.parametrize('normal_inputs',['calibration'],indirect=True)
def test_composed_size_use_report_keeps_hash_and_one_megabyte_bound(calibration_flow):
    c=calibration_flow
    row=c.service.records.admission(c.selection.use_admission_id)
    raw=(c.root/row.review_reference).read_bytes()
    padded=b' '*310000+raw
    assert 300000<len(padded)<1000000
    path=c.root/'larger-real-contract.json';path.write_bytes(padded)
    assert c.service._report(path.name,hashlib.sha256(padded).hexdigest())==row.review
    with pytest.raises(UseAdmissionError,match='changed'):
        c.service._report(path.name,'0'*64)
    path.write_bytes(b' '*1000001)
    with pytest.raises(UseAdmissionError,match='too_large'):
        c.service._report(path.name,hashlib.sha256(path.read_bytes()).hexdigest())


@pytest.mark.parametrize('failure',['mouth','face','subtitle','gaze','expression','duration'])
def test_adopted_source_never_overrides_known_negative_or_requested_performance(failure):
    from app.domain.models import Clip,TalkingReferenceAssessment,TalkingPerformanceBrief
    from app.talking import select_talking_reference
    clip=Clip(asset_id=uuid4(),start_ms=0,end_ms=3200,asset_duration_ms=3200,
        talking_reference_assessment=TalkingReferenceAssessment(burned_in_subtitles=False,evidence_reference='fixture-only'))
    brief=TalkingPerformanceBrief(minimum_reference_duration_ms=2320)
    assert select_talking_reference([clip],brief).selected_clip_id is None
    assert select_talking_reference([clip],brief,adopted_full_interval_clip_ids=frozenset({clip.id})).selected_clip_id==clip.id
    if failure=='mouth':clip=clip.model_copy(update={'mouth_visibility':0.2})
    elif failure=='face':clip=clip.model_copy(update={'face_visibility':0.2})
    elif failure=='subtitle':clip=clip.model_copy(update={'talking_reference_assessment':clip.talking_reference_assessment.model_copy(update={'burned_in_subtitles':True})})
    elif failure=='gaze':brief=TalkingPerformanceBrief(minimum_reference_duration_ms=2320,start_gaze='camera')
    elif failure=='expression':brief=brief.model_copy(update={'expression_labels':['smile']})
    elif failure=='duration':brief=brief.model_copy(update={'minimum_reference_duration_ms':4000})
    assert select_talking_reference([clip],brief,adopted_full_interval_clip_ids=frozenset({clip.id})).selected_clip_id is None


@pytest.fixture
def calibration_flow(normal_inputs):
    c=normal_inputs
    assert c.cap.readiness=='configured' and c.cap.quality_status=='unknown' and c.spec.schema_version==10
    selected=TalkingCalibrationCandidate(**c.selection.model_dump(exclude={'policy','execution_admission_id','use_admission_id'}),
        policy=CALIBRATION_POLICY,trial_duration_limit_ms=10000,trial_processing_limit_ms=12000)
    intent=c.service.calibration_intent(c.project,TalkingCalibrationIntent(selection=selected,execution_specification=c.spec))
    terms=c.root/'calibration-terms.txt';terms.write_bytes(b'Synthetic trial and exact-output permission only')
    sha=hashlib.sha256(terms.read_bytes()).hexdigest()
    report=dict(policy_version=5,admission_policy=CALIBRATION_POLICY,max_provider_calls=1,
        trial_input_sha256=intent['trial_input_sha256'],trial_duration_limit_ms=10000,trial_processing_limit_ms=12000,
        evidence_class='operator_verified',project_id=str(c.project),purpose='internal_evaluation',
        capability_profile_id=str(c.cap.id),configuration_sha256=configuration_digest(c.cap),
        identity=c.spec.identity.model_dump(mode='json'),execution_specification=c.spec.model_dump(mode='json'),
        execution_sha256=c.spec.execution_sha256,intended_activity='Synthetic one bounded calibration with conditional exact-result reuse',
        license_source='synthetic',license_version='test-v1',license_terms_reference=terms.name,license_terms_sha256=sha,
        permits_intended_activity=True,permitted_operations=['generation','review'],findings=['No real rights issued'])
    ProviderUseLicenseReviewV5.model_validate_json(json.dumps(report))
    adoption=save(c.root,'calibration-execution.json',report)
    execution=c.service.admissions.adopt(c.project,**adoption.model_dump(),operator_key=OPERATOR_KEY)
    selected=TalkingCalibrationSelection(**selected.model_dump(exclude={'execution_admission_id'}),execution_admission_id=execution.id)
    context=c.service._context(c.project,selected)
    evidence=dict(source='synthetic',version='test-v1',terms_reference=terms.name,terms_sha256=sha,
        applicability='Trial, local QA/preview/six-pass review and conditional same-output Run admission only',permits_intended_activity=True)
    use=dict(policy=CALIBRATION_POLICY,purpose='internal_evaluation',operation='calibrate_master_to_talking_and_admit_exact_output',
        local_only=True,project_id=str(c.project),selection=selected.model_dump(mode='json'),request_sha256=context['request_sha256'],
        proof=context['proof'],intended_activity='Synthetic calibration',findings=['Exact sample only'],
        license_evidence=[dict(evidence,admission_id=str(b.admission_id)) for b in context['output_scope'].bindings],
        dependency_findings=[dict(evidence,dependency=role) for role in ('narration_input_use','source_person_video','talking_output_processing_review')])
    receipt=c.service.adopt(c.project,save(c.root,'calibration-use.json',use),OPERATOR_KEY)
    c.selection=selected.model_copy(update={'use_admission_id':receipt.id});c.intent=intent;c.execution_report=report;c.use_template=use
    return c


def start(c,key='trial'):
    state=c.service.preflight(c.project,c.selection);assert state['eligible'],state
    return TalkingCalibrationStart(**c.selection.model_dump(),idempotency_key=key,expected_fingerprint=state['fingerprint'])


@pytest.mark.parametrize('normal_inputs',['calibration'],indirect=True)
def test_normal_calibration_api_claim_and_non_circular_binding(calibration_flow):
    c=calibration_flow
    base=f'/projects/{c.project}/evaluation-talking'
    payload=start(c)
    response=http(c,'POST',base+'/operations',json=payload.model_dump(mode='json'))
    assert response.status_code==201,response.text
    op=c.service.records.operation(response.json()['id'])
    assert c.service.create(c.project,start(c,'another-key'))==op
    c.service.require_calibration_claim(op)
    job=JobRepository(c.db).get(op.generation_job_id)
    assert job.payload.slice_limit_source==CALIBRATION_POLICY
    assert job.payload.slice_max_duration_ms==10000
    assert c.intent['trial_input_sha256']==op.proof['calibration_intent']['trial_input_sha256']
    assert 'execution_admission_id' not in c.intent['document']['selection']
    assert 'use_admission_id' not in c.intent['document']['selection']
    assert c.db.connection.execute('SELECT COUNT(*) FROM evaluation_talking_calibration_claims').fetchone()[0]==1
    wrong=payload.model_dump(mode='json');wrong.pop('policy')
    assert http(c,'POST',base+'/operations',json=wrong).status_code==422
    for policy in ('evaluation-talking-run-v1',True,'unknown'):
        wrong={**payload.model_dump(mode='json'),'policy':policy}
        assert http(c,'POST',base+'/operations',json=wrong).status_code==422
    c.db.connection.execute('DELETE FROM evaluation_talking_calibration_claims')
    with pytest.raises(UseAdmissionError,match='claim_changed'):c.service.require_job_identity(job)


@pytest.mark.parametrize('normal_inputs',['calibration'],indirect=True)
def test_normal_calibration_same_output_review_admission_and_no_promotion(calibration_flow,monkeypatch):
    import test_evaluation_talking_normal_flow as normal
    from app.jobs.evaluation_talking_handler import MaintainedEvaluationTalkingHandler
    c=calibration_flow
    original=ProviderMachineCapabilityProfileRepository(c.db).get(c.cap.id)
    originals={table:tuple(tuple(r) for r in c.db.connection.execute(f'SELECT * FROM {table} ORDER BY id')) for table in ('production_runs','audio_assets')}
    old_jobs=tuple(tuple(r) for r in c.db.connection.execute('SELECT * FROM jobs ORDER BY id'))
    monkeypatch.setattr(normal,'TalkingStart',TalkingCalibrationStart)
    # The imported constructor is patched only in the test module; the class
    # checked by dispatch stays exact, and the runtime/authority are untouched.
    normal.test_normal_owned_handler_through_preview_review_admission_with_real_authority(c,monkeypatch)
    assert ProviderMachineCapabilityProfileRepository(c.db).get(c.cap.id)==original
    for table,rows in originals.items():
        assert tuple(tuple(r) for r in c.db.connection.execute(f'SELECT * FROM {table} ORDER BY id'))==rows
    current_jobs={r['id']:tuple(r) for r in c.db.connection.execute('SELECT * FROM jobs')}
    assert all(current_jobs[row[0]]==row for row in old_jobs)
    row=c.db.connection.execute("SELECT payload FROM evaluation_talking_results WHERE role='admission'").fetchone()
    result=json.loads(row[0]);assert result['result_version']==2
    observation=result['quality_observation']
    assert observation['scope']=='exact_sample_only' and observation['observed_pass_duration_ms']>0
    assert observation['trial_input_sha256']==c.intent['trial_input_sha256']
    assert 'fresh_voice_talking_duration_ms' not in original.verified_parameters
    op=c.service.records.operation(c.db.connection.execute('SELECT id FROM evaluation_talking_operations').fetchone()[0])
    # Completed consumption authenticates the embedded projection, even when
    # a corrupted row has a recomputed outer JSON hash.
    from app.domain.models import execution_canonical_sha256 as digest
    changed=json.loads(row[0]);changed['quality_observation']['observed_pass_duration_ms']+=1
    c.db.connection.execute("UPDATE evaluation_talking_results SET payload=?,payload_sha256=? WHERE operation_id=? AND role='admission'",
        (json.dumps(changed),digest(changed),str(op.id)))
    from app.talking.evaluation_admission import require_admission_projection
    from app.db import AssetRepository
    with pytest.raises(UseAdmissionError,match='quality_observation_changed'):
        require_admission_projection(c.service,op,AssetRepository(c.db).get(result['asset_id']))


@pytest.mark.parametrize('normal_inputs',['calibration'],indirect=True)
@pytest.mark.parametrize('field,value',[('trial_duration_limit_ms',True),('trial_duration_limit_ms',1.5),('trial_duration_limit_ms',0),('trial_processing_limit_ms',-1)])
def test_trial_bounds_are_exact_positive_integers(calibration_flow,field,value):
    raw=calibration_flow.selection.model_dump(mode='json');raw[field]=value
    with pytest.raises(ValueError):TypeAdapter(TalkingSelectionRecord).validate_json(json.dumps(raw))


@pytest.mark.parametrize('normal_inputs',['calibration'],indirect=True)
def test_trial_drift_and_ordinary_lane_remain_closed(calibration_flow):
    c=calibration_flow
    candidates=c.service.candidates(c.project)
    assert len(candidates)==1 and candidates[0]['calibration_grant_available']
    assert candidates[0]['quality_status']=='unknown' and candidates[0]['verified_duration_ms'] is None
    for field,value in [('trial_duration_limit_ms',1),('trial_processing_limit_ms',10001),('authorization_reference','changed')]:
        with pytest.raises(ValueError):c.service._context(c.project,c.selection.model_copy(update={field:value}))
    from app.domain.evaluation_talking import TalkingSelection
    normal=TalkingSelection(**c.selection.model_dump(exclude={'policy','trial_duration_limit_ms','trial_processing_limit_ms'}))
    with pytest.raises(UseAdmissionError,match='not_verified'):c.service._context(c.project,normal)


@pytest.mark.parametrize('normal_inputs',['calibration'],indirect=True)
def test_duplicate_reports_wrong_worker_cancel_and_trial_canonical_forms(calibration_flow):
    c=calibration_flow
    op=c.service.create(c.project,start(c))
    # Adopting the same grant again under another receipt/key cannot reset it.
    adopted=save(c.root,'duplicate-calibration-execution.json',c.execution_report)
    execution=c.service.admissions.adopt(c.project,**adopted.model_dump(),operator_key=OPERATOR_KEY)
    selection=c.selection.model_copy(update={'execution_admission_id':execution.id,'use_admission_id':None})
    context=c.service._context(c.project,selection)
    assert context['proof']['calibration_intent']==c.intent
    evidence=c.use_template['license_evidence'][0]
    use={**c.use_template,'selection':selection.model_dump(mode='json'),'proof':context['proof'],'request_sha256':context['request_sha256'],
        'license_evidence':[{**evidence,'admission_id':str(b.admission_id)} for b in context['output_scope'].bindings]}
    receipt=c.service.adopt(c.project,save(c.root,'duplicate-calibration-use.json',use),OPERATOR_KEY)
    c.selection=selection.model_copy(update={'use_admission_id':receipt.id})
    assert c.service.create(c.project,start(c,'duplicate-report-key'))==op
    assert c.db.connection.execute('SELECT COUNT(*) FROM evaluation_talking_calibration_claims').fetchone()[0]==1
    from app.domain.models import JobStatus
    from app.jobs.evaluation_talking_handler import MaintainedEvaluationTalkingHandler
    job=JobRepository(c.db).get(op.generation_job_id)
    job=JobRepository(c.db).update(job.model_copy(update={'status':JobStatus.RUNNING,'attempt':1}))
    with pytest.raises(UseAdmissionError,match='policy_mismatch'):
        MaintainedEvaluationTalkingHandler(c.db,c.root,options={}).require_dispatch(job)
    candidate=TalkingCalibrationCandidate(**c.selection.model_dump(exclude={'execution_admission_id','use_admission_id'}))
    inputs=c.service._inputs(c.project,candidate)
    original=c.service._trial_intent(c.project,candidate,c.spec,inputs)
    alias=candidate.model_copy(update={'master_run_id':uuid4(),'source_admission_id':uuid4()})
    aliased=c.service._trial_intent(c.project,alias,c.spec,inputs)
    assert aliased['trial_input_sha256']!=original['trial_input_sha256']
    assert aliased['semantic_trial_sha256']==original['semantic_trial_sha256']
    for field,value in [('max_provider_calls',True),('max_provider_calls',2),('policy_version',True),('trial_duration_limit_ms',1.5),
        ('trial_processing_limit_ms',0),('permitted_operations',['generation','derivation','review']),('admission_policy','evaluation-talking-run-v1')]:
        with pytest.raises(ValueError):ProviderUseLicenseReviewV5.model_validate_json(json.dumps({**c.execution_report,field:value}))
    c.service.cancel(c.project,op.id)
    assert c.service.create(c.project,start(c,'after-cancel'))==op
    with pytest.raises(UseAdmissionError,match='cancelled'):c.service.require_current_operation(op)
    for identity in (op.selection.execution_admission_id,execution.id):
        c.service.admissions.revoke(c.project,identity,reason='Synthetic grant withdrawal',operator_key=OPERATOR_KEY)
    assert not c.service.candidates(c.project)[0]['calibration_grant_available']


@pytest.mark.parametrize('normal_inputs',['calibration'],indirect=True)
def test_concurrent_creates_claim_one_trial_and_source_revocation_remains_current(calibration_flow):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from app.db import Database, ProviderCallRepository
    from app.evaluation_talking import EvaluationTalkingService
    c=calibration_flow
    payload=start(c);barrier=Barrier(2)
    calls=ProviderCallRepository(c.db).list_all()
    def create(key):
        with Database(c.db.path) as db:
            service=EvaluationTalkingService(db,c.root)
            estimate=service._budget_estimate
            def synchronized(context):
                value=estimate(context);barrier.wait(timeout=30);return value
            service._budget_estimate=synchronized
            try:
                return service.create(c.project,payload.model_copy(update={'idempotency_key':key}))
            except UseAdmissionError as exc:
                assert str(exc)=='evaluation_talking_authority_changed'
                return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(create,['race-one','race-two']))
    assert sum(v is not None for v in results)==1
    assert c.db.connection.execute('SELECT COUNT(*) FROM evaluation_talking_calibration_claims').fetchone()[0]==1
    assert ProviderCallRepository(c.db).list_all()==calls
    op=next(v for v in results if v is not None)
    assert c.service.create(c.project,start(c,'recover-race'))==op
    c.sources.revoke(c.receipt.id,actor_id='fixture-operator',reason='Synthetic current withdrawal')
    with pytest.raises(ValueError):c.service.require_current_operation(op)


@pytest.mark.parametrize('normal_inputs',['calibration'],indirect=True)
def test_second_connection_revokes_before_enqueue_without_consuming_claim(calibration_flow,monkeypatch):
    import test_evaluation_talking_normal_flow as normal
    monkeypatch.setattr(normal,'TalkingStart',TalkingCalibrationStart)
    normal.test_normal_second_connection_source_revocation_rolls_back_enqueue(calibration_flow,monkeypatch)
    assert calibration_flow.db.connection.execute('SELECT COUNT(*) FROM evaluation_talking_calibration_claims').fetchone()[0]==0


@pytest.mark.parametrize('normal_inputs',['calibration'],indirect=True)
def test_late_revocation_quarantines_and_never_retries(calibration_flow,monkeypatch):
    import app.domain.evaluation_talking as domain
    import test_evaluation_talking_dual_normal as normal
    monkeypatch.setattr(domain,'TalkingStart',TalkingCalibrationStart)
    normal.test_dual_late_source_revocation_keeps_call_and_denies_output(calibration_flow,monkeypatch)
