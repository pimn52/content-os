"""自动画面调整的共用 Worker 行为；替身通过不代表真实成片通过。"""
from datetime import datetime,timezone
from types import SimpleNamespace
from uuid import UUID,uuid4,uuid5

import pytest

from app.db import Database,IPProfileRepository,ProjectRepository,ProjectDraftRepository,BudgetPolicyRepository
from app.domain.models import IPProfile,Project,ProjectDraft,RationalFps,ScenePlan,VisualIntent,BudgetPolicy
from app.media.planning import MaterialResolutionError
from app.production_runs import ProductionRunNotReady
from app.video_production import VideoProductionService


def pipeline(tmp_path,monkeypatch,topic):
    db=Database(tmp_path/'product.sqlite');now=datetime.now(timezone.utc)
    ip=IPProfileRepository(db).create(IPProfile(creator_name='本地测试创作者'))
    project=ProjectRepository(db).create(Project(ip_profile_id=ip.id,title=topic,topic=topic,
        fps=RationalFps(numerator=30,denominator=1),created_at=now))
    BudgetPolicyRepository(db).save(BudgetPolicy(project_id=project.id,max_calls=30,allow_unknown_cost=True,updated_at=now))
    service=VideoProductionService(db,tmp_path)
    job=service.enqueue(project.id,topic,'one-input')
    scene=ScenePlan(project_id=project.id,scene_id='explain',order=0,purpose='说明原因',voice_text=topic,
        duration_target_ms=3000,visual_intent=VisualIntent(subject='原画面方案'),viewer_takeaway='理解主题中的原因',
        preferred_sources=['user_asset'],visual_requirement='action_evidence',visual_requirement_reason='须展示对应操作')
    draft=ProjectDraftRepository(db).save(ProjectDraft(project_id=project.id,version=1,script_revision=1,
        topic=topic,script=scene.voice_text,scenes=[scene],evidence_refs=[f'content_plan_job:{job.id}'],updated_at=now))
    clip_id=uuid4();events=[]
    gap=MaterialResolutionError('material_scene_unresolved:explain',
        candidate_rejections={'explain':{str(clip_id):['visual_purpose_mismatch']}})
    class Resolver:
        def check_configuration(self,*args):pass
        def resolve(self,project,scenes,scope,**kwargs):
            events.append(('resolve',scope))
            if scope==job.id:
                assert kwargs['revision_reserve_calls']==4  # 未声明批数的替身沿用真实三批上限。
                raise gap
            assert scope==uuid5(job.id,'visual-revision')
            return tuple(scenes)
        def revision_candidates(self,project,failure):
            assert failure is gap
            return (clip_id,)
    service.material_resolver=Resolver()
    def generate(db,project,payload,planner,cost,key,**kwargs):
        events.append(('revise',key))
        kwargs['reservation_guard']()
        assert payload.script==draft.script and not payload.persist
        assert kwargs['material_clip_ids']==(clip_id,) and kwargs['automatic']
        assert kwargs['visual_revision']['max_revisions']==1
        replacement=scene.model_copy(update={'preferred_clip_id':clip_id,
            'visual_intent':VisualIntent(subject='按真实候选调整的对应操作')})
        return SimpleNamespace(scenes=(replacement,))
    monkeypatch.setattr('app.video_production.generate_scene_plan',generate)
    monkeypatch.setattr('app.video_production.build_production_preflight',lambda *args,**kwargs:SimpleNamespace(fingerprint='fixture',scenes=[]))
    run=SimpleNamespace(project_id=project.id,id=uuid4())
    service.runs.start=lambda *args,**kwargs:run
    service.advance=lambda *args:run
    return SimpleNamespace(db=db,project=project,job=job,draft=draft,service=service,events=events,run=run,generate=generate,gap=gap)


@pytest.mark.parametrize('topic',['手冲咖啡时水流为什么要稳定？','夜景拍摄为什么容易模糊？'])
def test_one_worker_plan_revises_visuals_once_and_continues_without_manual_resume(tmp_path,monkeypatch,topic):
    c=pipeline(tmp_path,monkeypatch,topic)
    try:
        assert c.service.plan(c.job,object(),object()) is c.run
        assert c.events==[('resolve',c.job.id),('revise',f'video-production:{c.job.id}:visual-revision'),
            ('resolve',uuid5(c.job.id,'visual-revision'))]
        saved=ProjectDraftRepository(c.db).get(c.project.id)
        assert saved.script==c.draft.script and saved.scenes[0].voice_text==c.draft.scenes[0].voice_text
        assert saved.scenes[0].visual_requirement=='action_evidence'
    finally:c.db.close()


@pytest.mark.parametrize('changed', ['voice_text','viewer_takeaway','visual_requirement','graphic_plan'])
def test_visual_revision_cannot_lower_content_or_replace_evidence_with_graphics(tmp_path,monkeypatch,changed):
    c=pipeline(tmp_path,monkeypatch,'保留事实主张')
    def bad(*args,**kwargs):
        result=c.generate(*args,**kwargs)
        updates={'voice_text':'改写后的旁白','viewer_takeaway':'降低主张','visual_requirement':'explanatory',
            'graphic_plan':SimpleNamespace(kind='text_card')}
        return SimpleNamespace(scenes=(result.scenes[0].model_copy(update={changed:updates[changed]}),))
    monkeypatch.setattr('app.video_production.generate_scene_plan',bad)
    try:
        with pytest.raises(ProductionRunNotReady) as failure:c.service.plan(c.job,object(),object())
        assert failure.value.reasons==('visual_revision_changed_content',)
        import json
        evidence=json.loads((tmp_path/'render-diagnostics'/str(c.job.id)/'visual-revision-check.json').read_text(encoding='utf-8'))
        assert list(evidence['changed_content_fields']['explain'])==[changed]
        assert ProjectDraftRepository(c.db).get(c.project.id)==c.draft
        assert len(c.events)==2
    finally:c.db.close()


def test_revision_that_still_lacks_matching_visuals_stops_after_one_adjustment(tmp_path,monkeypatch):
    c=pipeline(tmp_path,monkeypatch,'已有候选仍不足')
    c.service.material_resolver.resolve=lambda *args,**kwargs:(_ for _ in ()).throw(c.gap)
    try:
        with pytest.raises(MaterialResolutionError):c.service.plan(c.job,object(),object())
        assert len([event for event in c.events if event[0]=='revise'])==1
        assert ProjectDraftRepository(c.db).get(c.project.id)==c.draft
    finally:c.db.close()


def test_visual_revision_does_not_consume_reserved_production_tail(tmp_path,monkeypatch):
    c=pipeline(tmp_path,monkeypatch,'预算不足时停止')
    BudgetPolicyRepository(c.db).save(BudgetPolicy(project_id=c.project.id,max_calls=4,allow_unknown_cost=True,updated_at=datetime.now(timezone.utc)))
    try:
        with pytest.raises(ProductionRunNotReady) as failure:c.service.plan(c.job,object(),object())
        assert failure.value.reasons==('planning_tail_budget',)
        assert ProjectDraftRepository(c.db).get(c.project.id)==c.draft
    finally:c.db.close()


@pytest.mark.parametrize('style,tail',[('auto',3),('creator_led',4)])
def test_revision_requires_all_analysis_batches_before_issuing_planning(tmp_path,monkeypatch,style,tail):
    c=pipeline(tmp_path,monkeypatch,'调整前留足全部有界核验')
    c.project=ProjectRepository(c.db).update(c.project.model_copy(update={'presentation_style':style}))
    BudgetPolicyRepository(c.db).save(BudgetPolicy(project_id=c.project.id,max_calls=tail+3,
        allow_unknown_cost=True,updated_at=datetime.now(timezone.utc)))
    try:
        with pytest.raises(ProductionRunNotReady) as failure:
            c.service._revise_visuals(c.project,c.draft,c.job,c.job.id,None,object(),object(),c.gap)
        assert failure.value.reasons==('planning_tail_budget',)
        assert c.events==[]  # 规划1＋最多三批不足时，连规划请求也不开始。
    finally:c.db.close()


@pytest.mark.parametrize('reason',['source_content_hash_mismatch','material_analysis_batch_limit','material_video_analysis_timeout'])
def test_execution_failures_do_not_trigger_visual_revision(tmp_path,monkeypatch,reason):
    c=pipeline(tmp_path,monkeypatch,'执行错误不能变成继续花费的理由')
    failure=MaterialResolutionError(reason)
    c.service.material_resolver.resolve=lambda *args,**kwargs:(_ for _ in ()).throw(failure)
    monkeypatch.setattr('app.video_production.generate_scene_plan',lambda *args,**kwargs:pytest.fail('执行失败不能发起规划'))
    try:
        with pytest.raises(MaterialResolutionError) as stopped:c.service.plan(c.job,object(),object())
        assert stopped.value is failure
        assert ProjectDraftRepository(c.db).get(c.project.id)==c.draft
    finally:c.db.close()


def test_user_edit_during_revision_is_preserved_and_stops_before_more_qa(tmp_path,monkeypatch):
    c=pipeline(tmp_path,monkeypatch,'创作者改稿不能被覆盖')
    edited=c.draft.model_copy(update={'version':2,'script':'用户刚刚编辑的原稿'})
    def editing(*args,**kwargs):
        result=c.generate(*args,**kwargs)
        ProjectDraftRepository(c.db).save(edited)
        return result
    monkeypatch.setattr('app.video_production.generate_scene_plan',editing)
    try:
        with pytest.raises(ProductionRunNotReady) as failure:c.service.plan(c.job,object(),object())
        assert failure.value.reasons==('automatic_draft_changed',)
        assert ProjectDraftRepository(c.db).get(c.project.id)==edited
        assert len(c.events)==2
    finally:c.db.close()


def test_fresh_start_counts_planning_call_in_addition_to_production_tail(tmp_path,monkeypatch):
    c=pipeline(tmp_path,monkeypatch,'不能漏算首次规划')
    try:
        c.project=ProjectRepository(c.db).update(c.project.model_copy(update={'presentation_style':'creator_led'}))
        # 现有待执行任务先退出，验证新启动的调用下限；此处没有任何提供商调用。
        from app.db import JobRepository
        from app.domain.models import JobStatus
        JobRepository(c.db).update(c.job.model_copy(update={'status':JobStatus.FAILED}))
        monkeypatch.setattr('app.media.planning.estimated_material_calls',lambda *args:0)
        BudgetPolicyRepository(c.db).save(BudgetPolicy(project_id=c.project.id,max_calls=4,allow_unknown_cost=True,updated_at=datetime.now(timezone.utc)))
        with pytest.raises(ProductionRunNotReady) as failure:c.service.enqueue(c.project.id,c.project.topic,'fresh-again')
        assert failure.value.reasons==('budget_chain_call_limit',)
        BudgetPolicyRepository(c.db).save(BudgetPolicy(project_id=c.project.id,max_calls=5,allow_unknown_cost=True,updated_at=datetime.now(timezone.utc)))
        assert c.service.enqueue(c.project.id,c.project.topic,'fresh-again').id!=c.job.id
    finally:c.db.close()


@pytest.mark.parametrize('resource',['material_analysis_revision_due','material_analysis_batch_limit'])
def test_completed_qa_resource_limit_enters_one_revision_with_partial_evidence(tmp_path,monkeypatch,resource):
    c=pipeline(tmp_path,monkeypatch,'资源有限仍应利用已核验候选')
    failure=MaterialResolutionError(*c.gap.reasons,resource,candidate_rejections=c.gap.candidate_rejections)
    original=c.service.material_resolver.resolve
    def resolve(project,scenes,scope,**kwargs):
        if scope==c.job.id:raise failure
        return original(project,scenes,scope,**kwargs)
    c.service.material_resolver.resolve=resolve
    c.service.material_resolver.revision_candidates=lambda *args:tuple(UUID(identity) for rows in c.gap.candidate_rejections.values() for identity in rows)
    try:
        assert c.service.plan(c.job,object(),object()) is c.run
        assert len([event for event in c.events if event[0]=='revise'])==1
    finally:c.db.close()
