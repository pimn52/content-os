"""同稿恢复契约：保留失败证据与已取得素材，显式创建新尝试；不外调。"""
from datetime import datetime,timezone
from types import SimpleNamespace
from uuid import UUID,uuid4

from fastapi.testclient import TestClient
import pytest

from app.db import (BudgetPolicyRepository,Database,IPProfileRepository,JobRepository,
    ProjectDraftRepository,ProjectRepository,ProviderCallRepository)
from app.domain.models import (BudgetPolicy,ContentPlanJobPayload,IPProfile,Job,JobStatus,
    JobType,Project,ProjectDraft,RationalFps,ScenePlan,VisualIntent,SourceKind)
from app.main import create_app
from app.production_runs import ProductionRunNotReady
from app.video_production import VideoProductionService


TOPIC="视频码率为什么不是越高越清晰？"


def _run(db,project_id,*,start_job_id=None):
    run_id=uuid4()
    db.connection.execute("""INSERT INTO production_runs
        (id,project_id,idempotency_key,preflight_fingerprint,draft_version,script_revision,
         style_tokens,repair_allowance,stage,created_at,start_job_id)
        VALUES (?,?,?,?,1,1,'{}',0,'render',?,?)""",
        (str(run_id),str(project_id),str(run_id),str(run_id),datetime.now(timezone.utc).isoformat(),
            None if start_job_id is None else str(start_job_id)))
    return run_id


def _job(db,project_id,*,status=JobStatus.FAILED,origin=None,candidate=None):
    now=datetime.now(timezone.utc)
    return JobRepository(db).create(Job(project_id=project_id,type=JobType.PLAN_CONTENT,
        status=status,attempt=1,idempotency_key=str(uuid4()),created_at=now,updated_at=now,
        error_code="material_video_analysis_timeout" if status is JobStatus.FAILED else None,
        error_message="保留原核验失败" if status is JobStatus.FAILED else None,
        payload=ContentPlanJobPayload(project_id=project_id,topic=TOPIC,candidate_of_run_id=candidate,
            resume_from_job_id=origin,resume_draft_version=7 if origin else None)))


@pytest.fixture
def resume_case(tmp_path,monkeypatch):
    path=tmp_path/'resume.sqlite'
    monkeypatch.setenv('CONTENT_OS_DATA_ROOT',str(tmp_path/'data'))
    with Database(path) as db:
        now=datetime.now(timezone.utc)
        ip=IPProfileRepository(db).create(IPProfile(creator_name='创作者'))
        project=ProjectRepository(db).create(Project(ip_profile_id=ip.id,title='画质说明',topic=TOPIC,
            fps=RationalFps(numerator=30,denominator=1),created_at=now))
        other=ProjectRepository(db).create(project.model_copy(update={'id':uuid4(),'title':'另一项目'}))
        parent=_run(db,project.id)
        failed=_job(db,project.id,candidate=parent)
        scene=ScenePlan(project_id=project.id,scene_id='detail',order=0,purpose='说明压缩影响',
            voice_text='码率增加不能恢复原片丢失的细节。',duration_target_ms=3000,
            visual_intent=VisualIntent(subject='相同原片的画质对比'),preferred_sources=['stock'],
            visual_requirement='action_evidence',visual_requirement_reason='展示同源比较')
        draft=ProjectDraftRepository(db).save(ProjectDraft(project_id=project.id,version=7,script_revision=3,
            topic=TOPIC,script=scene.voice_text,scenes=[scene],evidence_refs=[f'content_plan_job:{failed.id}'],updated_at=now))
        BudgetPolicyRepository(db).save(BudgetPolicy(project_id=project.id,max_calls=30,
            allow_unknown_cost=True,updated_at=now))
    return SimpleNamespace(path=path,root=tmp_path,project=project,other=other,parent=parent,failed=failed,draft=draft)


def _request(case,**changes):
    return dict(topic=TOPIC,idempotency_key='explicit-resume',resume_from_job_id=str(case.failed.id),
        expected_draft_version=case.draft.version)|changes


def test_api_resume_status_and_idempotent_submission_preserve_root_and_original_failure(resume_case):
    c=resume_case
    with TestClient(create_app(c.path)) as client:
        url=f'/projects/{c.project.id}/auto-produce'
        before=client.get(url)
        assert before.status_code==200,before.text
        status=before.json()
        assert status['status']=='failed' and status['can_resume']
        assert status['resume_draft_version']==7 and status['topic']==TOPIC
        assert status['origin_job_id']==str(c.failed.id)
        started=client.post(url,json=_request(c))
        assert started.status_code==201,started.text
        replay=client.post(url,json=_request(c))
        assert replay.status_code==201 and replay.json()==started.json()
        assert started.json()['start_job_id']!=str(c.failed.id)
        pending=client.get(url).json()
        assert pending['status']=='pending' and pending['can_resume'] is False
        assert pending['resume_draft_version'] is None and pending['origin_job_id']==str(c.failed.id)
        assert pending['topic']==TOPIC
    with Database(c.path) as db:
        new=JobRepository(db).get(UUID(started.json()['start_job_id']))
        assert new.payload.resume_from_job_id==c.failed.id and new.payload.resume_draft_version==7
        assert new.payload.candidate_of_run_id==c.parent and new.payload.topic==TOPIC
        assert JobRepository(db).get(c.failed.id)==c.failed
        assert db.connection.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==2
        assert ProviderCallRepository(db).list_for_project(c.project.id)==[]


@pytest.mark.parametrize('invalid', ['other_project','version','topic','missing_evidence','not_latest','running','has_run'])
def test_resume_rejects_unrelated_changed_nonlatest_or_already_started_work(resume_case,invalid):
    c=resume_case
    expected={
        'other_project':'automatic_resume_source_invalid','version':'automatic_draft_changed',
        'topic':'automatic_resume_topic_changed','missing_evidence':'automatic_draft_changed',
        'not_latest':'automatic_resume_source_not_latest_failed','running':'automatic_resume_source_not_latest_failed',
        'has_run':'automatic_resume_already_has_run'}[invalid]
    with Database(c.path) as db:
        if invalid=='missing_evidence':
            changed=c.draft.model_copy(update={'version':8,'evidence_refs':[]})
            ProjectDraftRepository(db).save(changed)
        elif invalid=='not_latest':
            _job(db,c.project.id)
        elif invalid=='running':
            _job(db,c.project.id,status=JobStatus.RUNNING)
        elif invalid=='has_run':
            _run(db,c.project.id,start_job_id=c.failed.id)
        before=db.connection.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
    body=_request(c)
    project=c.other.id if invalid=='other_project' else c.project.id
    if invalid=='version':body['expected_draft_version']=6
    if invalid=='topic':body['topic']='另一个主题'
    if invalid=='missing_evidence':body['expected_draft_version']=8
    with TestClient(create_app(c.path)) as client:
        response=client.post(f'/projects/{project}/auto-produce',json=body)
        assert response.status_code==409,response.text
        assert expected in response.text
    with Database(c.path) as db:
        assert db.connection.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==before
        assert JobRepository(db).get(c.failed.id)==c.failed
        assert ProviderCallRepository(db).list_for_project(c.project.id)==[]


def test_resume_of_failed_resume_keeps_the_root_origin_and_comparison_relationship(resume_case):
    c=resume_case
    with Database(c.path) as db:
        previous=_job(db,c.project.id,origin=c.failed.id,candidate=c.parent)
        service=VideoProductionService(db,c.root)
        status=service.status(c.project.id)
        assert status['can_resume'] and status['origin_job_id']==str(c.failed.id)
        new=service.enqueue(c.project.id,TOPIC,'next-resume',resume_from_job_id=previous.id,expected_draft_version=7)
        assert new.payload.resume_from_job_id==c.failed.id
        assert new.payload.candidate_of_run_id==c.parent
        assert JobRepository(db).get(previous.id)==previous


def test_resume_status_does_not_offer_recovery_when_origin_already_has_production_run(resume_case):
    c=resume_case
    with Database(c.path) as db:
        latest=_job(db,c.project.id,origin=c.failed.id,candidate=c.parent)
        _run(db,c.project.id,start_job_id=c.failed.id)
        status=VideoProductionService(db,c.root).status(c.project.id)
        assert status['start_job_id']==str(latest.id) and status['origin_job_id']==str(c.failed.id)
        assert status['can_resume'] is False and status['resume_draft_version'] is None


def _cached_pipeline(db,c,monkeypatch):
    events=[]
    cached_assets={'detail':(uuid4(),)}
    scope=uuid4()
    class Resolver:
        def check_configuration(self,*args):events.append('configuration')
        def check_external_configuration(self,scenes):events.append('external-configuration')
        def prepare(self,*args):pytest.fail('同稿恢复不能重新准备素材目录')
        def resolve(self,project,scenes,resolution_scope,*,work_id,acquired_assets,attempt_id,reserved_source_uses):
            events.append(('resolve',resolution_scope,work_id,attempt_id))
            assert acquired_assets is cached_assets
            assert reserved_source_uses==[]
            return tuple(scenes)
    class Acquirer:
        def acquire(self,project,scenes,origin_id,**kwargs):
            events.append(('acquire-cache',origin_id))
            assert origin_id==c.failed.id and kwargs=={}
            return cached_assets
        def resolution_scope(self,origin_id):
            assert origin_id==c.failed.id
            return scope
    def forbidden(*args,**kwargs):pytest.fail('同稿恢复不能重新生成或重写脚本')
    monkeypatch.setattr('app.video_production.generate_scene_plan',forbidden)
    monkeypatch.setattr('app.video_production.save_project_draft',forbidden)
    monkeypatch.setattr('app.video_production.build_production_preflight',
        lambda *args,**kwargs:SimpleNamespace(fingerprint='same-draft',scenes=[]))
    service=VideoProductionService(db,c.root,material_resolver=Resolver(),material_acquirer=Acquirer())
    run=SimpleNamespace(project_id=c.project.id,id=uuid4())
    service.runs.start=lambda *args,**kwargs:run
    service.advance=lambda project_id,run_id:run
    return service,events,scope,run


def test_resume_reuses_original_draft_and_acquisition_identity_with_new_qa_attempt(resume_case,monkeypatch):
    c=resume_case
    with Database(c.path) as db:
        service,events,scope,run=_cached_pipeline(db,c,monkeypatch)
        resumed=service.enqueue(c.project.id,TOPIC,'resume',resume_from_job_id=c.failed.id,expected_draft_version=7)
        changes=db.connection.total_changes
        result=service.plan(resumed,object(),object())
        assert result is run
        assert events==['configuration','external-configuration',('acquire-cache',c.failed.id),
            ('resolve',scope,c.parent,resumed.id)]
        assert ProjectDraftRepository(db).get(c.project.id)==c.draft
        assert db.connection.total_changes==changes
        assert JobRepository(db).get(c.failed.id)==c.failed
        assert ProviderCallRepository(db).list_for_project(c.project.id)==[]


def test_queued_resume_stops_before_acquisition_if_draft_changed(resume_case,monkeypatch):
    c=resume_case
    with Database(c.path) as db:
        service,events,_,_=_cached_pipeline(db,c,monkeypatch)
        resumed=service.enqueue(c.project.id,TOPIC,'resume-before-edit',resume_from_job_id=c.failed.id,expected_draft_version=7)
        changed=c.draft.model_copy(update={'version':8,'script':'创作者已改稿'})
        ProjectDraftRepository(db).save(changed)
        with pytest.raises(ProductionRunNotReady) as error:
            service.plan(resumed,object(),object())
        assert error.value.reasons==('automatic_draft_changed',)
        assert events==['configuration']
        assert ProjectDraftRepository(db).get(c.project.id)==changed
        assert ProviderCallRepository(db).list_for_project(c.project.id)==[]


@pytest.mark.parametrize('local_failure,should_supplement',[
    ('material_scene_unresolved:local',True),
    ('source_content_hash_mismatch',False),
    ('material_analysis_batch_limit',False),
])
def test_mixed_gaps_supplement_stock_once_but_never_bypass_remaining_gap(resume_case,monkeypatch,local_failure,should_supplement):
    from app.domain.models import MaterialRequest
    from app.media.planning import MaterialResolutionError
    c=resume_case
    with Database(c.path) as db:
        scene=c.draft.scenes[0].model_copy(update={'material_request':MaterialRequest(
            source_kind='stock',query=' video compression ',reason='说明压缩过程')})
        local=scene.model_copy(update={'id':uuid4(),'scene_id':'local','order':1,'material_request':None,
            'preferred_sources':[SourceKind.USER_ASSET],'fallback_sources':[]})
        c.draft=c.draft.model_copy(update={'scenes':[scene,local],'version':8})
        ProjectDraftRepository(db).save(c.draft)
        service,_,scope,_=_cached_pipeline(db,c,monkeypatch)
        supplements=[];resolutions=[]
        def acquire(project,scenes,origin_id,**kwargs):
            if kwargs:supplements.append(kwargs)
            return {'detail':(uuid4(),)}
        def resolve(*args,**kwargs):
            resolutions.append(kwargs)
            if len(resolutions)==1:
                raise MaterialResolutionError(local_failure,'material_source_unavailable:detail:stock')
            raise MaterialResolutionError(local_failure)
        service.material_acquirer.acquire=acquire
        service.material_resolver.resolve=resolve
        service.runs.start=lambda *args,**kwargs:pytest.fail('仍有本地缺口不能进入制作')
        resumed=service.enqueue(c.project.id,TOPIC,'mixed-gaps',resume_from_job_id=c.failed.id,expected_draft_version=8)
        with pytest.raises(MaterialResolutionError) as failed:
            service.plan(resumed,object(),object())
        assert failed.value.reasons==((local_failure,) if should_supplement else
            (local_failure,'material_source_unavailable:detail:stock'))
        assert supplements==([{'supplement_for':['video compression']}] if should_supplement else [])
        assert len(resolutions)==(2 if should_supplement else 1)
        assert ProviderCallRepository(db).list_for_project(c.project.id)==[]
