"""一次主题输入，沿现有分镜、ProductionRun、Job/Worker 推进到待审视频。"""
from datetime import datetime,timezone
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Literal, TypedDict
from uuid import UUID,uuid5

from app.db import (ProjectRepository,ProjectDraftRepository,IPProfileRepository,JobRepository,
    AudioAssetRepository,TalkingProfileRepository,ClipRepository,
    ProviderMachineCapabilityProfileRepository,BudgetPolicyRepository,ProviderCallRepository)
from app.domain.models import ContentPlanJobPayload,Job,JobType,JobStatus,VisualStyleTokens,SourceKind,MaterialRequest
from app.production_runs import ProductionRunService,ProductionRunNotReady,TalkingPlanDependency
from app.production_preflight import build_production_preflight
from app.providers.local_generation import local_generation_ready,local_generation_licensed
from app.scene_planning import ScenePlanRequest,generate_scene_plan
from app.project_drafts import save_project_draft,_split_talking_master_clauses
from app.budget import snapshot
from app.planning_input import short_video_project,normalize_automatic_graphics,automatic_graphic_gaps
from app.media.planning import VisualMaterialResolver
from app.voice_qa import voice_human_review_status

IN_PROGRESS_REASONS=("talking_source_not_bound:","talking_qa_run_not_implemented:","talking_exact_preview_review_required:","talking_u_review_run_not_implemented:")
DIAGNOSTIC_ACTIONS=frozenset({'diagnose_qa','diagnose_quality','diagnose_semantic','verify_material'})


class ProductionDecision(TypedDict):
    """当前执行决策；Job/Run 状态只描述已落盘的工序事实。"""
    status: Literal['diagnosing_qa','failed'] | None
    next_action: Literal['continue','review','stopped','diagnose_qa','diagnose_quality','diagnose_semantic','verify_material']
    responsible_stage: str
    stop_reasons: list[str]
    quality_diagnosis: dict | None
    material_recheck: dict | None
    material_repair_id: str | None
    next_job_id: str | None
    next_job_type: str | None


@dataclass(frozen=True)
class VisualFeasibility:
    """一次估算或实测的可行性事实，不另存生产状态。"""
    pool: object
    scenes: tuple
    scheduled: tuple
    allocations: tuple
    basis: str
    reason: str | None = None
    requires_measured_partition: bool = False

    def summary(self, talking_provider=None):
        return dict(basis=self.basis, **self.pool.summary(talking_provider=talking_provider),
            schedulable=None if self.requires_measured_partition else self.reason is None,
            requires_measured_partition=self.requires_measured_partition,
            material_verification='separate_interval_qa_required',
            stop_reasons=[] if self.reason is None else [self.reason],
            requested_talking_ms=sum(scene.duration_target_ms for scene in self.scenes if scene.visual_requirement=='creator_speaking'),
            allocated_reference_ms=sum(value.needed_reference_ms for value in self.allocations),
            talking_scene_ids=[scene.scene_id for scene in self.scheduled if scene.visual_requirement=='creator_speaking'],
            supporting_footage_scene_ids=[scene.scene_id for before,scene in zip(self.scenes,self.scheduled)
                if before.visual_requirement=='creator_speaking' and scene.visual_requirement!='creator_speaking'])


class VideoProductionService:
    def __init__(self,db,data_root,*,voice_provider=None,talking_provider=None,material_resolver=None,material_acquirer=None,
                 planner=None,planner_cost_estimator=None,semantic_diagnoser=None):
        self.db,self.root=db,data_root
        self.voice_provider,self.talking_provider=voice_provider,talking_provider
        self.material_resolver = material_resolver
        self.material_acquirer = material_acquirer
        self.planner,self.planner_cost_estimator=planner,planner_cost_estimator
        self.semantic_diagnoser=semantic_diagnoser
        self.runs=ProductionRunService(db,data_root,talking_provider=talking_provider)

    def enqueue(self,project_id,topic,key,*,candidate_of_run_id=None,resume_from_job_id=None,expected_draft_version=None):
        previous=None
        origin=None
        if resume_from_job_id is not None:
            previous=JobRepository(self.db).get(resume_from_job_id)
            if previous is None or previous.project_id!=project_id or previous.type is not JobType.PLAN_CONTENT:
                raise ValueError("automatic_resume_source_invalid")
            if candidate_of_run_id is not None or topic.strip()!=previous.payload.topic:
                raise ValueError("automatic_resume_topic_changed")
            if expected_draft_version is None:
                raise ValueError("automatic_resume_draft_required")
            origin=getattr(previous.payload,"resume_from_job_id",None) or previous.id
            candidate_of_run_id=previous.payload.candidate_of_run_id
        elif expected_draft_version is not None:
            raise ValueError("automatic_resume_source_invalid")
        payload=ContentPlanJobPayload(project_id=project_id,topic=topic.strip(),candidate_of_run_id=candidate_of_run_id,
            resume_from_job_id=origin,resume_draft_version=expected_draft_version)
        with self.db.transaction(immediate=True):
            existing=JobRepository(self.db).get_by_idempotency_key(key)
            if existing:
                if existing.type is not JobType.PLAN_CONTENT or existing.payload!=payload:
                    raise ValueError("video_start_idempotency_conflict")
                return existing
            project=ProjectRepository(self.db).get(project_id)
            if project is None:
                raise LookupError("project not found")
            if previous is not None:
                latest=self.db.connection.execute("SELECT id FROM jobs WHERE project_id=? AND json_extract(payload,'$.type')='plan_content' ORDER BY rowid DESC LIMIT 1",(str(project_id),)).fetchone()
                if previous.status is not JobStatus.FAILED or latest is None or latest[0]!=str(previous.id):
                    raise ProductionRunNotReady(("automatic_resume_source_not_latest_failed",))
                if self.db.connection.execute("SELECT 1 FROM production_runs WHERE start_job_id IN (?,?)",(str(previous.id),str(origin))).fetchone():
                    raise ProductionRunNotReady(("automatic_resume_already_has_run",))
                draft=ProjectDraftRepository(self.db).get(project_id)
                if (draft is None or draft.version!=expected_draft_version
                    or f"content_plan_job:{origin}" not in draft.evidence_refs):
                    raise ProductionRunNotReady(("automatic_draft_changed",))
            if candidate_of_run_id is not None and self.db.connection.execute("SELECT 1 FROM production_runs WHERE id=? AND project_id=?",
                (str(candidate_of_run_id),str(project_id))).fetchone() is None:
                raise ValueError("candidate_parent_run_not_found")
            policies=[p for p in (BudgetPolicyRepository(self.db).get_by_project(None),
                BudgetPolicyRepository(self.db).get_by_project(project_id)) if p]
            if not policies:
                raise ProductionRunNotReady(("unknown_cost_requires_budget_authorization",))
            for policy in policies:
                calls=ProviderCallRepository(self.db).list() if policy.project_id is None else ProviderCallRepository(self.db).list_for_project(project_id)
                spent=snapshot(calls,policy.currency)
                if policy.max_calls is not None and spent.calls>=policy.max_calls:
                    raise ProductionRunNotReady(("budget_call_limit",))
                minimum_calls=4 if project.presentation_style=='creator_led' else 3
                from app.media.planning import estimated_material_calls,estimated_talking_calls
                minimum_calls += 1 if previous is not None else (1+estimated_material_calls(self.db, project, payload.topic)
                    +estimated_talking_calls(self.db,project))
                if policy.max_calls is not None and policy.max_calls-spent.calls<minimum_calls:
                    raise ProductionRunNotReady(("budget_chain_call_limit",))
            active=self.db.connection.execute("SELECT id FROM jobs WHERE project_id=? AND json_extract(payload,'$.type')='plan_content' AND status IN ('pending','running')",(str(project_id),)).fetchone()
            if active:
                raise ProductionRunNotReady(("video_production_already_running",))
            for row in self.db.connection.execute("SELECT id FROM production_runs WHERE project_id=? AND automatic=1 AND stage IN ('voice','awaiting_talking','render')",(str(project_id),)).fetchall():
                if self.decision_for_run(UUID(row[0]),set(JobType))['next_action'] not in {'review','stopped'}:
                    raise ProductionRunNotReady(("video_production_already_running",))
            now=datetime.now(timezone.utc)
            return JobRepository(self.db).create(Job(project_id=project_id,type=JobType.PLAN_CONTENT,
                idempotency_key=key,created_at=now,updated_at=now,payload=payload))

    def talking_choice(self,project_id):
        from app.planning_feasibility import configured_talking_choices
        choices=configured_talking_choices(self.db,project_id)
        if choices:
            return choices[0]
        raise ProductionRunNotReady(("creator_talking_reference_not_configured",))

    def _voice_choice(self,project_id):
        from app.voice_plan import resolve_project_voice_profile
        profile=resolve_project_voice_profile(self.db,project_id)
        candidates=[value for value in ProviderMachineCapabilityProfileRepository(self.db).list()
            if value.capability=="voice" and value.provider==profile.provider
            and local_generation_ready(value) and local_generation_licensed(value)]
        if self.voice_provider is not None:
            candidates=[value for value in candidates if value.model==self.voice_provider.model]
        if not candidates:
            raise ProductionRunNotReady(("voice_capability_not_verified",))
        return profile,max(candidates,key=lambda value:value.updated_at)

    def plan(self,job,planner,cost_estimator):
        project=ProjectRepository(self.db).get(job.project_id)
        existing=self.db.connection.execute("SELECT id FROM production_runs WHERE start_job_id=?",(str(job.id),)).fetchone()
        if existing:
            return self.runs.get(job.project_id,UUID(existing[0]))
        vertical = short_video_project(project)
        if vertical != project:
            project = ProjectRepository(self.db).update(vertical)
        if self.material_resolver is not None:
            self.material_resolver.check_configuration(project, job.payload.topic)
        draft = ProjectDraftRepository(self.db).get(project.id)
        origin_id=getattr(job.payload,"resume_from_job_id",None) or job.id
        if origin_id!=job.id and (draft is None or draft.version!=job.payload.resume_draft_version
            or f"content_plan_job:{origin_id}" not in draft.evidence_refs):
            raise ProductionRunNotReady(("automatic_draft_changed",))
        if draft is None or f"content_plan_job:{origin_id}" not in draft.evidence_refs:
            prepare_talking=getattr(self.material_resolver,"prepare_talking_sources",None)
            if callable(prepare_talking):
                prepare_talking(project,origin_id,
                    reservation_guard=lambda:self._reserve_planning_tail(project,needs_analysis=True))
            material_ids=self.material_resolver.prepare(project,job.payload.topic,job.id) if self.material_resolver else None
            response=generate_scene_plan(self.db,project,ScenePlanRequest(topic=job.payload.topic,persist=True),
                planner,cost_estimator,f"video-production:{job.id}:planning",automatic=True,origin_job_id=job.id,
                material_clip_ids=material_ids, external_sources=getattr(self.material_acquirer,"planning_sources",self.material_acquirer.sources) if self.material_acquirer else None,
                talking_provider=self.talking_provider,
                reservation_guard=lambda:self._reserve_planning_tail(project,needs_analysis=bool(material_ids)))
            draft=response.draft
        scenes=normalize_automatic_graphics(draft.scenes)
        gaps=automatic_graphic_gaps(scenes)
        if gaps:
            raise ProductionRunNotReady(gaps)
        if tuple(draft.scenes)!=scenes:
            draft=save_project_draft(self.db,project,script=draft.script,topic=draft.topic,scenes=scenes,
                routes=(),confirmed=(),video_spec=None,evidence_refs=draft.evidence_refs,accept_scene_plan_with_new_script=True)
        parent=self.db.connection.execute("SELECT COALESCE(work_id,id) FROM production_runs WHERE id=?",(str(job.payload.candidate_of_run_id),)).fetchone() if job.payload.candidate_of_run_id else None
        work_id=UUID(parent[0]) if parent else None
        draft=self._prepare_initial_visuals(project,draft,job,planner,cost_estimator,work_id=work_id)
        if self.material_resolver is not None:
            feasible=self._visual_feasibility(project,draft.scenes,script=draft.script,work_id=work_id)
            estimated=(feasible.scheduled if not feasible.requires_measured_partition
                and tuple(draft.scenes)!=feasible.scheduled else None)
            material_scenes=estimated or draft.scenes
            extra = {"reserved_source_uses":self._reserved_talking_uses(feasible)}
            resolution_scope = job.id
            selected_ready=getattr(self.material_resolver,"selected_materials_ready",None)
            materialized=callable(selected_ready) and selected_ready(project,material_scenes)
            if self.material_acquirer is not None:
                self.material_resolver.check_external_configuration(material_scenes)
                remember=getattr(self.material_acquirer,"remember_original_scenes",None)
                if materialized and callable(remember):
                    remember(project,draft.scenes,origin_id)
                extra["acquired_assets"] = ({} if materialized and estimated is None else
                    self.material_acquirer.acquire(project, draft.scenes, origin_id,
                        **({"estimated_scenes":estimated} if estimated is not None else {})))
                resolution_scope = self.material_acquirer.resolution_scope(origin_id)
            else:
                resolution_scope = origin_id
            if origin_id!=job.id:
                extra["attempt_id"] = job.id
            if callable(getattr(self.material_resolver,"revision_candidates",None)):
                extra["revision_reserve_calls"]=1+getattr(self.material_resolver,"max_analysis_batches",VisualMaterialResolver.max_analysis_batches)
            resolution_scenes=material_scenes
            restore_scenes=getattr(self.material_acquirer,"resolution_scenes",None)
            if not materialized and callable(restore_scenes):
                resolution_scenes=restore_scenes(draft.scenes,origin_id)
            try:
                resolved = self._resolve_with_supplement(project,resolution_scenes,job,origin_id,resolution_scope,work_id,extra,
                    acquisition_scenes=draft.scenes,estimated_scenes=estimated)
            except Exception as failure:
                from app.media.planning import MaterialResolutionError
                if not isinstance(failure,MaterialResolutionError):raise
                self._replacement_queries(draft.scenes,failure)  # 仅真实场景缺口可调整，不吞执行错误。
                if estimated is not None:raise  # 必用备选尚未取得，不能先消耗声音或另开调整轮次。
                resolved=self._revise_visuals(project,draft,job,origin_id,work_id,planner,cost_estimator,failure)
            if estimated is not None:
                resolved=self._restore_estimated_talking(draft.scenes,estimated,resolved)
                self._record_visual_feasibility(job.id,feasible,material_verification='estimated_substitutes_verified')
            if tuple(draft.scenes) != resolved:
                draft = save_project_draft(self.db,project,script=draft.script,topic=draft.topic,scenes=resolved,
                    routes=(),confirmed=(),video_spec=None,evidence_refs=draft.evidence_refs,accept_scene_plan_with_new_script=True)
        plan=build_production_preflight(self.db,project,draft,style_tokens=VisualStyleTokens(),repair_allowance=1,source_usage_work_id=work_id)
        version=2 if any(item.production_need=="new_talking" for item in plan.scenes) else 1
        run=self.runs.start(project.id,idempotency_key=f"video-production:{job.id}",expected_fingerprint=plan.fingerprint,
            style_tokens=VisualStyleTokens(),repair_allowance=1,talking_review_policy_version=version,
            automatic=True,start_job_id=job.id,candidate_of_run_id=job.payload.candidate_of_run_id)
        return self.advance(run.project_id,run.id)

    @staticmethod
    def _restore_estimated_talking(originals,estimated,resolved):
        """备选已核验；保留完整人物意图，真实旁白后再确定出镜段。"""
        from app.media.planning import split_footage_clauses
        original_by_id={scene.id:scene for scene in originals}
        projected={part.id:original_by_id[scene.id] for scene in estimated
            if original_by_id[scene.id].visual_requirement=='creator_speaking'
            and scene.visual_requirement!='creator_speaking' for part in split_footage_clauses([scene])}
        restored=[]
        seen=set()
        for scene in resolved:
            original=projected.get(scene.id)
            if original is not None:
                if original.id in seen:continue
                seen.add(original.id)
                scene=original
            restored.append(scene.model_copy(update={'order':len(restored)}))
        return tuple(restored)

    def _reserved_talking_uses(self,feasible):
        from app.source_usage import SourceUsageService
        usage=SourceUsageService(self.db)
        reserved=[]
        for scene,allocation in zip((scene for scene in feasible.scheduled
            if scene.visual_requirement=='creator_speaking'),feasible.allocations):
            reserved.extend({**use,'content_changed':True,'repeatable':False} for use in usage.resolve(
                allocation.entry.asset.id,allocation.start_ms,allocation.end_ms,scene.scene_id))
        return reserved

    @staticmethod
    def _replacement_queries(scenes,failure):
        from app.media.planning import split_footage_clauses
        by_id={scene.scene_id:scene for scene in split_footage_clauses(scenes)}
        missing=[]
        for reason in failure.reasons:
            prefix,_,detail=reason.partition(":")
            if (reason in {"material_analysis_revision_due","material_analysis_batch_limit","material_acquisition_due"}
                and failure.candidate_rejections):continue
            if prefix in {"material_scene_unresolved","material_combination_unresolved"} and detail in by_id:continue
            scene_id,_,source=detail.rpartition(":")
            if (prefix!="material_source_unavailable" or source!="stock" or scene_id not in by_id
                or SourceKind.STOCK not in (*by_id[scene_id].preferred_sources,*by_id[scene_id].fallback_sources)):raise failure
            # 自动补缺不改原稿；补检后的错误也可能用 stock 标记原先无 request 的场景。
            if by_id[scene_id].material_request is not None:
                missing.append(" ".join(by_id[scene_id].material_request.query.split()))
        return missing

    @classmethod
    def _material_gaps(cls,scenes,failure):
        """已选画面失效也是取材缺口；沿用原意图，不改文案、证据或人物身份。"""
        from app.media.planning import split_footage_clauses
        cls._replacement_queries(scenes,failure)  # 执行错误、哈希变更不能触发更多取材。
        missing={reason.partition(":")[2] for reason in failure.reasons
            if reason.partition(":")[0] in {"material_scene_unresolved","material_combination_unresolved"}}
        gaps=[]
        for scene in split_footage_clauses(scenes):
            if (scene.scene_id not in missing or scene.material_request is not None
                or scene.visual_requirement=="creator_speaking"
                or scene.preferred_sources[0] is SourceKind.TYPOGRAPHY
                or SourceKind.STOCK not in (*scene.preferred_sources,*scene.fallback_sources)):
                continue
            query=" ".join(filter(None,(scene.visual_intent.subject,scene.visual_intent.action)))
            # 查询只负责召回；完整表达任务仍交给区间核验，不能由目录命中代判通过。
            query=" ".join(query.split())[:200]
            if not query:continue
            gaps.append(scene.model_copy(update={"preferred_clip_id":None,"material_request":MaterialRequest(
                source_kind="stock",query=query,reason="已有画面未满足当前用途或整片组合，按原画面意图补缺。" )}))
        return tuple(gaps)

    def _resolve_with_supplement(self,project,scenes,job,origin_id,scope,work_id,extra,*,acquisition_scenes=None,visual_scenes=None,
                                estimated_scenes=None,measured_scenes=None,master_audio_id=None):
        from app.media.planning import MaterialResolutionError
        try:
            return self.material_resolver.resolve(project,scenes,scope,work_id=work_id,**extra)
        except MaterialResolutionError as failure:
            if ("material_analysis_revision_due" in failure.reasons
                or "material_analysis_batch_limit" in failure.reasons and not failure.candidate_rejections):raise
            missing=self._replacement_queries(scenes,failure)
            gaps=self._material_gaps(scenes,failure)
            missing=list(dict.fromkeys([*missing,*(scene.material_request.query for scene in gaps)]))
            if not missing or self.material_acquirer is None:raise
            if gaps:
                from app.media.planning import split_footage_clauses
                by_id={scene.scene_id:scene for scene in gaps}
                scenes=tuple(by_id.get(scene.scene_id,scene) for scene in split_footage_clauses(scenes))
                self.material_resolver.check_external_configuration(scenes)
            if measured_scenes is not None and acquisition_scenes is None:
                acquisition_scenes=self.material_acquirer.original_scenes(origin_id)
            replacement=self.material_acquirer.acquire(project,acquisition_scenes if acquisition_scenes is not None else scenes,
                origin_id,supplement_for=missing,**({"gap_scenes":gaps} if gaps else {}),
                **({"visual_scenes":visual_scenes} if visual_scenes is not None else {}),
                **({"estimated_scenes":estimated_scenes} if estimated_scenes is not None else {}),
                **({"measured_scenes":measured_scenes,"master_audio_id":master_audio_id,
                    "remaining_talking_calls":extra.get("remaining_talking_calls",1)} if measured_scenes is not None else {}))
            if replacement is None:raise
            return self.material_resolver.resolve(project,scenes,self.material_acquirer.resolution_scope(origin_id),
                work_id=work_id,acquired_assets=replacement,**{key:value for key,value in extra.items()
                    if key not in {"acquired_assets", "supplement_reserve_calls"}})

    def _reserve_planning_tail(self,project,*,needs_analysis=True,analysis_calls=1,after_master=False,remaining_talking_calls=1):
        tail=4 if project.presentation_style=="creator_led" else 3
        if after_master:tail=remaining_talking_calls+1
        for policy in (BudgetPolicyRepository(self.db).get_by_project(None),BudgetPolicyRepository(self.db).get_by_project(project.id)):
            if policy is None:continue
            calls=ProviderCallRepository(self.db).list() if policy.project_id is None else ProviderCallRepository(self.db).list_for_project(project.id)
            if policy.max_calls is not None and snapshot(calls,policy.currency).calls+1+(analysis_calls if needs_analysis else 0)+tail>policy.max_calls:
                raise ProductionRunNotReady(("planning_tail_budget",))

    def _revise_visuals(self,project,draft,job,origin_id,work_id,planner,cost_estimator,failure):
        candidates=getattr(self.material_resolver,"revision_candidates",None)
        if not callable(candidates):raise failure
        identities=candidates(project,failure)
        if not identities:raise failure
        analysis_calls=getattr(self.material_resolver,"max_analysis_batches",VisualMaterialResolver.max_analysis_batches)
        self._reserve_planning_tail(project,analysis_calls=analysis_calls)
        evidence=Path(self.root)/"render-diagnostics"/str(origin_id)/"visual-revision-attempt.json"
        evidence.parent.mkdir(parents=True,exist_ok=True)
        try:
            with evidence.open("x",encoding="utf-8") as stream:
                json.dump({"job_id":str(job.id),"draft_version":draft.version,"clip_ids":[str(value) for value in identities]},stream)
        except FileExistsError:
            raise failure  # 同一原任务只调整一次，进程重启或显式恢复也不追加抽取机会。
        # 一次成功核验后的画面调整，不是失败请求重试；共用规划/记账/核验与后段预算。
        revision={"scenes":[scene.model_dump(mode="json") for scene in draft.scenes],
            "unresolved":list(failure.reasons),"candidate_rejections":failure.candidate_rejections,
            "max_revisions":1,"new_material_requests":False}
        result=generate_scene_plan(self.db,project,ScenePlanRequest(topic=draft.topic or job.payload.topic,script=draft.script,persist=False),
            planner,cost_estimator,f"video-production:{origin_id}:visual-revision",automatic=True,origin_job_id=origin_id,
            material_clip_ids=identities,talking_provider=self.talking_provider,visual_revision=revision,
            reservation_guard=lambda:self._reserve_planning_tail(project,analysis_calls=analysis_calls))
        if ProjectDraftRepository(self.db).get(project.id)!=draft:
            raise ProductionRunNotReady(("automatic_draft_changed",))
        if any(scene.material_request is not None for scene in result.scenes):raise failure
        immutable=("id","project_id","order","voice_text","duration_target_ms","purpose","viewer_takeaway","visual_requirement",
            "visual_requirement_reason","reuse_intent","graphic_plan","supporting_visual_intent","caption_emphasis","evidence_refs")
        before={scene.scene_id:scene for scene in draft.scenes}
        changed={scene.scene_id:{key:{"before":getattr(before[scene.scene_id],key),"after":getattr(scene,key)}
            for key in immutable if getattr(scene,key,None)!=getattr(before[scene.scene_id],key,None)}
            for scene in result.scenes if scene.scene_id in before}
        changed={key:value for key,value in changed.items() if value}
        (evidence.parent/"visual-revision-check.json").write_text(json.dumps({
            "missing_scene_ids":sorted(set(before)-{scene.scene_id for scene in result.scenes}),
            "unexpected_scene_ids":sorted({scene.scene_id for scene in result.scenes}-set(before)),
            "changed_content_fields":changed},ensure_ascii=False,indent=2,default=str),encoding="utf-8")
        if (len(result.scenes)!=len(before) or {scene.scene_id for scene in result.scenes}!=set(before)
            or changed):
            raise ProductionRunNotReady(("visual_revision_changed_content",))
        from app.media.visual_facts import scene_visual_key
        if all(scene_visual_key(scene)==scene_visual_key(before[scene.scene_id]) for scene in result.scenes):
            raise failure  # 原候选已按相同用途判定；只改素材偏好不能触发再次核验。
        self._require_talking_coverage(project.id,result.scenes,job_id=job.id,script=draft.script,work_id=work_id)
        resolved=self._resolve_with_supplement(project,result.scenes,job,origin_id,uuid5(origin_id,"visual-revision"),work_id,
            {"attempt_id":job.id} if origin_id!=job.id else {},acquisition_scenes=draft.scenes,visual_scenes=result.scenes)
        if ProjectDraftRepository(self.db).get(project.id)!=draft:
            raise ProductionRunNotReady(("automatic_draft_changed",))
        return resolved

    def _visual_feasibility(self,project,scenes,*,script=None,master=None,run_id=None,work_id=None,pool=None):
        """规划估算与合格旁白共用整批分配；只有后者使用真实时间轴。"""
        from app.talking.pool import build_talking_pool,TalkingPoolCapacityError
        from app.talking.scheduling import schedule_talking_scenes
        from app.assembly.video_spec import _master_narration_intervals
        if pool is None:
            pool=build_talking_pool(self.db,project.id,talking_provider=self.talking_provider,
                data_root=self.root,run_id=run_id,work_id=work_id)
        originals=prepared=tuple(scenes)
        if master is not None:
            capacity=pool.summary(talking_provider=self.talking_provider)
            maximum=capacity['max_narration_duration_ms'] or capacity['longest_continuous_ms']
            def measured_scenes(*,merge_clauses=True):
                split=_split_talking_master_clauses(self.db,project,script,list(originals),master=master,
                    max_segment_ms=maximum,merge_clauses=merge_clauses)
                return tuple(scene.model_copy(update={'duration_target_ms':end-start})
                    for scene,start,end in _master_narration_intervals(master,split))
            prepared=measured_scenes()
        basis='measured_master' if master is not None else 'estimated_scene_duration'
        if any(scene.visual_requirement=='creator_speaking' for scene in prepared) and not pool.entries:
            reason='talking_motion_occupancy_unresolved' if pool.occupancy_state!='known' else 'creator_talking_reference_not_configured'
            return VisualFeasibility(pool,prepared,prepared,(),basis,reason)
        try:
            scheduled,allocations=schedule_talking_scenes(pool,prepared,talking_provider=self.talking_provider)
            return VisualFeasibility(pool,prepared,scheduled,allocations,basis)
        except TalkingPoolCapacityError as exc:
            if master is not None and exc.reason=='talking_motion_capacity_insufficient' and capacity.get('alignment_state')=='known':
                finer=measured_scenes(merge_clauses=False)
                if finer!=prepared:
                    try:
                        scheduled,allocations=schedule_talking_scenes(pool,finer,talking_provider=self.talking_provider)
                        return VisualFeasibility(pool,finer,scheduled,allocations,basis)
                    except TalkingPoolCapacityError as fine_error:
                        if fine_error.reason!='talking_motion_capacity_insufficient':
                            return VisualFeasibility(pool,finer,finer,(),basis,fine_error.reason)
            if master is None and exc.reason=='talking_motion_capacity_insufficient':
                capacity=pool.summary(talking_provider=self.talking_provider)
                longest=capacity['max_narration_duration_ms'] or capacity['longest_continuous_ms']
                mandatory=sum(scene.duration_target_ms for scene in prepared
                    if scene.visual_requirement=='creator_speaking' and scene.supporting_visual_intent is None)
                if (longest and mandatory<=pool.usable_total_ms and any(scene.visual_requirement=='creator_speaking'
                    and scene.duration_target_ms>longest for scene in prepared)):
                    # 原整场尚无真实语句边界；总量可能容纳时不能假定
                    # 必须连续出镜。这里不估算切点，待合格Master后严格整批。
                    return VisualFeasibility(pool,prepared,prepared,(),basis,requires_measured_partition=True)
            return VisualFeasibility(pool,prepared,prepared,(),basis,exc.reason)

    def _record_visual_feasibility(self,job_id,result,*,material_verification=None):
        evidence=Path(self.root)/'render-diagnostics'/str(job_id)/'visual-feasibility.json'
        evidence.parent.mkdir(parents=True,exist_ok=True)
        facts=result.summary(self.talking_provider)
        if material_verification is not None:facts['material_verification']=material_verification
        evidence.write_text(json.dumps(facts,ensure_ascii=False,indent=2),encoding='utf-8')

    def _prepare_initial_visuals(self,project,draft,job,planner,cost_estimator,*,work_id=None):
        result=self._visual_feasibility(project,draft.scenes,script=draft.script,work_id=work_id)
        self._record_visual_feasibility(job.id,result)
        if (result.reason=='talking_motion_capacity_insufficient' and self.material_resolver is not None
            and any(scene.visual_requirement=='creator_speaking' and scene.supporting_visual_intent is None for scene in draft.scenes)):
            origin_id=getattr(job.payload,'resume_from_job_id',None) or job.id
            revised=self._motion_revision(project,draft,None,result.scenes,result.pool,job,origin_id,
                planner=planner,cost_estimator=cost_estimator)
            result=self._visual_feasibility(project,revised,script=draft.script,work_id=work_id,pool=result.pool)
            if result.reason is None:
                draft=save_project_draft(self.db,project,script=draft.script,topic=draft.topic,scenes=revised,
                    routes=(),confirmed=(),video_spec=None,evidence_refs=draft.evidence_refs,accept_scene_plan_with_new_script=True)
        self._record_visual_feasibility(job.id,result)
        if result.reason:
            raise ProductionRunNotReady((result.reason,))
        return draft

    def _require_talking_coverage(self,project_id,scenes,*,job_id=None,script=None,work_id=None):
        project=ProjectRepository(self.db).get(project_id)
        result=self._visual_feasibility(project,scenes,script=script,work_id=work_id)
        if job_id is not None:
            self._record_visual_feasibility(job_id,result)
        if result.reason:
            raise ProductionRunNotReady((result.reason,))
        return result

    def _motion_revision(self,project,draft,master,measured,pool,job,origin_id,*,planner=None,cost_estimator=None):
        """同一轮受限实拍补充；估算不足先处理，实测不足仍保留合格旁白。"""
        planner=planner or self.planner
        cost_estimator=cost_estimator or self.planner_cost_estimator
        if planner is None or cost_estimator is None or self.material_resolver is None:
            raise ProductionRunNotReady(("talking_motion_capacity_insufficient",))
        after_master=master is not None
        self._reserve_planning_tail(project,analysis_calls=1,after_master=after_master)
        evidence=Path(self.root)/"render-diagnostics"/str(origin_id)/"visual-revision-attempt.json"
        evidence.parent.mkdir(parents=True,exist_ok=True)
        try:
            with evidence.open("x",encoding="utf-8") as stream:
                json.dump({"kind":"talking_capacity","job_id":str(job.id),"draft_version":draft.version,
                    "timing_basis":"measured_master" if after_master else "estimated_scene_duration",
                    "master_audio_id":str(master.id) if master else None,"master_content_hash":master.content_hash if master else None},stream)
        except FileExistsError:
            raise ProductionRunNotReady(("visual_revision_already_used",)) from None
        revision={"kind":"talking_capacity","scenes":[scene.model_dump(mode="json") for scene in draft.scenes],
            "measured_scenes":[{"scene_id":scene.scene_id,"voice_text":scene.voice_text,
                "duration_ms":scene.duration_target_ms} for scene in measured],
            "talking_motion_pool":pool.summary(talking_provider=self.talking_provider),"max_revisions":1}
        result=generate_scene_plan(self.db,project,ScenePlanRequest(topic=draft.topic or job.payload.topic,
            script=draft.script,persist=False),planner,cost_estimator,
            f"video-production:{origin_id}:visual-revision",automatic=True,origin_job_id=origin_id,
            talking_provider=self.talking_provider,visual_revision=revision,
            reservation_guard=lambda:self._reserve_planning_tail(project,analysis_calls=1,after_master=after_master))
        if ProjectDraftRepository(self.db).get(project.id)!=draft:
            raise ProductionRunNotReady(("automatic_draft_changed",))
        reasons=result.replaceability_reasons
        originals={scene.scene_id:scene for scene in draft.scenes}
        if len(result.scenes)!=len(originals) or {s.scene_id for s in result.scenes}!=set(originals):
            raise ProductionRunNotReady(("visual_revision_changed_content",))
        changes=[]
        for scene in result.scenes:
            original=originals[scene.scene_id]
            if scene.model_dump(exclude={"supporting_visual_intent","fallback_sources"})!=original.model_dump(exclude={"supporting_visual_intent","fallback_sources"}):
                raise ProductionRunNotReady(("visual_revision_changed_content",))
            if scene.supporting_visual_intent!=original.supporting_visual_intent:
                if (original.visual_requirement!="creator_speaking" or original.supporting_visual_intent is not None
                    or scene.supporting_visual_intent is None or not reasons.get(scene.scene_id,"").strip()
                    or not set(original.fallback_sources).issubset(scene.fallback_sources)
                    or any(source not in {SourceKind.USER_ASSET,SourceKind.HISTORICAL_ASSET,SourceKind.STOCK}
                        for source in set(scene.fallback_sources)-set(original.fallback_sources))):
                    raise ProductionRunNotReady(("visual_revision_changed_content",))
                changes.append(scene.scene_id)
            elif scene.fallback_sources!=original.fallback_sources:
                raise ProductionRunNotReady(("visual_revision_changed_content",))
        if not changes:
            raise ProductionRunNotReady(("talking_motion_capacity_insufficient",))
        result_path=evidence.parent/"motion-revision.json"
        result_path.write_text(json.dumps({"master_audio_id":str(master.id) if master else None,"master_content_hash":master.content_hash if master else None,
            "scenes":[scene.model_dump(mode="json") for scene in result.scenes],"reasons":reasons},
            ensure_ascii=False,indent=2),encoding="utf-8")
        return result.scenes

    def _prepare_measured_plan(self,row):
        """保留合格旁白，按真实时长分配本人动作与原稿允许的切题实拍。"""
        project=ProjectRepository(self.db).get(UUID(row["project_id"]))
        draft=ProjectDraftRepository(self.db).get(project.id)
        if draft.version!=row["draft_version"] or draft.script_revision!=row["script_revision"]:
            raise ProductionRunNotReady(("automatic_draft_changed",))
        master=AudioAssetRepository(self.db).get(UUID(row["voice_audio_id"]))
        result=self._visual_feasibility(project,draft.scenes,script=draft.script,master=master,run_id=UUID(row['id']))
        pool,revised=result.pool,result.scenes
        visual_scenes=None
        if result.reason:
            if result.reason!="talking_motion_capacity_insufficient":
                raise ProductionRunNotReady((result.reason,))
            if not any(scene.visual_requirement=="creator_speaking" and scene.supporting_visual_intent is None
                       for scene in draft.scenes):
                raise ProductionRunNotReady((result.reason,))
            if self.planner is None or self.planner_cost_estimator is None or self.material_resolver is None:
                raise ProductionRunNotReady((result.reason,))
            start=JobRepository(self.db).get(UUID(row['start_job_id'])) if row['start_job_id'] else None
            if start is None:raise ProductionRunNotReady((result.reason,))
            origin_id=getattr(start.payload,'resume_from_job_id',None) or start.id
            visual_scenes=self._motion_revision(project,draft,master,revised,pool,start,origin_id)
            result=self._visual_feasibility(project,visual_scenes,script=draft.script,master=master,
                run_id=UUID(row['id']),pool=pool)
            if result.reason:
                raise ProductionRunNotReady((result.reason,))
            revised=result.scenes
        scheduled,allocations=result.scheduled,result.allocations
        changed=[scene.scene_id for before,scene in zip(revised,scheduled)
            if before.visual_requirement!=scene.visual_requirement]
        if changed and self.material_resolver is None:
            raise ProductionRunNotReady(("talking_motion_capacity_insufficient",))
        reserved=self._reserved_talking_uses(result)
        revised=scheduled
        if self.material_resolver is not None:
            # 按合格Master真实时长重排画面；失败保留该Master，不重新生成声音。
            start=JobRepository(self.db).get(UUID(row['start_job_id'])) if row['start_job_id'] else None
            origin_id=(getattr(start.payload,'resume_from_job_id',None) or start.id) if start else UUID(row['id'])
            revised=self._resolve_with_supplement(project,revised,start,origin_id,uuid5(UUID(row['id']),'measured-materials'),
                UUID(row['work_id'] or row['id']),{"reserved_source_uses":reserved,"after_master":True,
                    "remaining_talking_calls":len(allocations),
                    "supplement_reserve_calls":3 if self.material_acquirer is not None and any(
                        scene.visual_requirement != "creator_speaking" and scene.preferred_sources[0] is not SourceKind.TYPOGRAPHY
                        and SourceKind.STOCK in (*scene.preferred_sources, *scene.fallback_sources) for scene in revised) else 0},
                visual_scenes=visual_scenes,measured_scenes=scheduled,master_audio_id=master.id)
        evidence=Path(self.root)/"render-diagnostics"/row['id']/"motion-schedule.json"
        evidence.parent.mkdir(parents=True,exist_ok=True)
        evidence.write_text(json.dumps({"master_audio_id":str(master.id),"master_content_hash":master.content_hash,
            "feasibility":result.summary(self.talking_provider),
            "supporting_footage_scene_ids":changed,"talking_windows":[{
                "scene_id":scene.scene_id,"clip_id":str(allocation.entry.clip.id),
                "reference_start_ms":allocation.start_ms,"reference_end_ms":allocation.end_ms,
                "narration_duration_ms":allocation.narration_duration_ms}
                for scene,allocation in zip((scene for scene in scheduled if scene.visual_requirement=='creator_speaking'),allocations)]},
            ensure_ascii=False,indent=2),encoding="utf-8")
        with self.db.transaction(immediate=True):
            current=self.db.connection.execute('SELECT * FROM production_runs WHERE id=? AND project_id=?',
                (row['id'],row['project_id'])).fetchone()
            if current is None or current['stage']!='voice' or dict(current)!=dict(row):
                raise ProductionRunNotReady(('automatic_production_changed',))
            if ProjectDraftRepository(self.db).get(project.id)!=draft:
                raise ProductionRunNotReady(('automatic_draft_changed',))
            if AudioAssetRepository(self.db).get(master.id)!=master:
                raise ProductionRunNotReady(('automatic_master_changed',))
            self.runs._require_bound_voice_master(current,master.id)
            saved=save_project_draft(self.db,project,script=draft.script,topic=draft.topic,scenes=revised,
                routes=(),confirmed=(),video_spec=None,evidence_refs=draft.evidence_refs,accept_scene_plan_with_new_script=True)
            self.db.connection.execute("UPDATE production_runs SET draft_version=?,script_revision=? WHERE id=?",
                (saved.version,saved.script_revision,row["id"]))
            plan=build_production_preflight(self.db,project,saved,style_tokens=VisualStyleTokens(),repair_allowance=row['repair_allowance'],
                source_usage_run_id=UUID(row["id"]))
            deps=[TalkingPlanDependency(scene_plan_id=value.scene_plan_id,scene_id=value.scene_id,
                visual_dependency_fingerprint=value.visual_dependency_fingerprint).model_dump(mode="json")
                for value in plan.scenes if value.production_need=="new_talking"]
            self.db.connection.execute("UPDATE production_runs SET talking_dependencies=? WHERE id=?",(json.dumps(deps),row["id"]))
            return self.db.connection.execute('SELECT * FROM production_runs WHERE id=?',(row['id'],)).fetchone()

    def advance(self,project_id,run_id):
        row=self.db.connection.execute("SELECT * FROM production_runs WHERE id=? AND project_id=? AND automatic=1",
            (str(run_id),str(project_id))).fetchone()
        if row is None:return None
        run=self.runs.get(project_id,run_id)
        from app.production_runs import production_run_jobs
        _,jobs=production_run_jobs(self.db,run_id)
        if self._queue_decision(row,jobs,set(JobType))['next_action']!='continue':
            return run
        expected=row
        try:
            if run.status=="awaiting_approved_master":
                profile,capability=self._voice_choice(project_id)
                return self.runs.dispatch_voice(project_id,run_id,voice_profile_id=profile.id,capability_profile_id=capability.id)
            if run.status in {"voice_qa_ready_for_draft","approved_master_ready_to_resume"}:
                expected=self._prepare_measured_plan(row)
                self.runs.resume(project_id,run_id)
                expected=self.db.connection.execute('SELECT * FROM production_runs WHERE id=?',(row['id'],)).fetchone()
                run=self.runs.get(project_id,run_id)
            if run.status=="awaiting_talking_dependencies":
                if not any(item.talking_job_id for item in run.talking_source_bindings):
                    return self.runs.dispatch_all_talking(project_id,run_id)
                required={item.scene_plan_id for item in run.talking_dependencies}
                states={item.scene_plan_id:item.state for item in run.talking_qa_dependencies}
                if required and set(states)==required and all(state in {'qa_verified_ready_for_preview','qa_verified_awaiting_u_talking'} for state in states.values()):
                    return self.runs.resume(project_id,run_id)
            return run
        except (ValueError,RuntimeError) as exc:
            self._stop_transition(expected,exc)
            raise

    def _stop_transition(self,expected,error):
        """只停止实际失败的工序快照，迟到回调不覆盖已推进或取消的 Run。"""
        reasons=getattr(error,'reasons',None)
        if not reasons:
            from app.providers.scene_planner import ScenePlannerError
            from app.scene_planning import _scene_planner_error_code
            reasons=((_scene_planner_error_code(error) if isinstance(error,ScenePlannerError)
                else getattr(error,'code',None) or 'automatic_production_stopped'),)
        with self.db.transaction(immediate=True):
            current=self.db.connection.execute('SELECT * FROM production_runs WHERE id=?',(expected['id'],)).fetchone()
            if current is None or current['stage']=='cancelled' or dict(current)!=dict(expected):return
            self.db.connection.execute('UPDATE production_runs SET waiting_stop_reasons=? WHERE id=?',
                (json.dumps(['automatic_stop:'+value for value in reasons]),current['id']))

    def _job_run(self,job):
        if job.type is JobType.PLAN_CONTENT:
            row=self.db.connection.execute("SELECT * FROM production_runs WHERE start_job_id=?",(str(job.id),)).fetchone()
        elif job.type in {JobType.GENERATE_TALKING,JobType.VERIFY_TALKING}:
            context=getattr(job.payload,'planned_context',None)
            run_id=getattr(job.payload,'run_id',None) or (context.run_id if context is not None else None)
            row=self.db.connection.execute('SELECT * FROM production_runs WHERE id=?',(str(run_id),)).fetchone()
        else:
            row=self.db.connection.execute("SELECT * FROM production_runs WHERE voice_job_id=? OR voice_qa_job_id=? OR render_job_id=?",
                (str(job.id),str(job.id),str(job.id))).fetchone()
        return row

    def job_allowed(self,job,handler_types):
        row=self._job_run(job)
        if row is None or not row['automatic']:
            return True
        return self.decision_for_run(UUID(row['id']),handler_types)['next_action']=='continue'

    @staticmethod
    def _completion_types(handler_types):
        supported=set()
        for group in ({JobType.GENERATE_VOICE,JobType.VERIFY_VOICE},
                      {JobType.GENERATE_TALKING,JobType.VERIFY_TALKING},{JobType.RENDER}):
            if group & set(handler_types):supported.update(group)
        return supported

    def recover_completed(self,handler_types):
        from app.production_runs import production_run_jobs
        supported=self._completion_types(handler_types)
        for row in self.db.connection.execute("SELECT id FROM production_runs WHERE stage IN ('voice','awaiting_talking','render')").fetchall():
            current,jobs=production_run_jobs(self.db,UUID(row['id']))
            for job in jobs:
                if job.status is JobStatus.COMPLETED and job.type in supported:
                    if job.type is JobType.RENDER and not self._current_render_replay(current):continue
                    self.completed(job,handler_types)

    def completed(self,job,handler_types=None):
        handler_types = set(JobType) if handler_types is None else set(handler_types)
        row=self._job_run(job)
        if row is not None and row['automatic']:
            from app.production_runs import production_run_jobs
            _,jobs=production_run_jobs(self.db,UUID(row['id']))
            decision=self._queue_decision(row,jobs,handler_types)
            if decision['next_action']=='review' and job.type is JobType.RENDER:
                self._observe_render_spec(job,row)
            if decision['next_action']!='continue':
                return
        if job.type is JobType.GENERATE_VOICE:
            self.runs.reconcile_completed_voice_job(job.id,qa_worker_ready=JobType.VERIFY_VOICE in handler_types)
        elif job.type is JobType.GENERATE_TALKING:
            self.runs.reconcile_completed_talking_job(job.id,qa_worker_ready=JobType.VERIFY_TALKING in handler_types)
        elif job.type is JobType.VERIFY_TALKING:
            self.runs.reconcile_completed_talking_qa_job(job.id)
        if row is not None and row['automatic']:
            try:self.advance(UUID(row["project_id"]),UUID(row["id"]))
            except (ValueError,RuntimeError) as exc:
                from app.jobs.handlers import _write_render_failure_diagnostic
                _write_render_failure_diagnostic(job,self.root,"production_transition",exc)

    def _presentation_diagnosis(self,row,jobs):
        """轮询只读当前版本的观察，不重复解码、哈希或调用诊断模型。"""
        if not row['automatic'] or row['stage']!='render':return None
        render=next((job for job in jobs if job.type is JobType.RENDER and job.status is JobStatus.COMPLETED),None)
        if render is None:return None
        from app.source_usage import SourceUsageService
        usage=SourceUsageService(self.db)
        if usage.review(row,exact_render=True)['state']=='approved' or usage.choice(render.project_id,render.payload.render_id)=='discarded':
            return None  # 已认可或明确弃用的版本不被普通队列重新激活。
        stored=self.db.connection.execute('SELECT payload FROM presentation_observations WHERE run_id=? AND render_job_id=? ORDER BY rowid DESC',
            (str(row['id']),str(render.id))).fetchall()
        if not stored:return None
        from app.presentation_repair import semantic_observation_diagnosis
        from app.presentation_diagnosis import diagnosis_for_finding,presentation_correction,summarize_diagnoses
        findings=[];seen=set()
        fps=render.payload.video_spec.fps
        draft=ProjectDraftRepository(self.db).get(render.project_id)
        for saved in stored:
            observation=json.loads(saved[0]);item=observation['request'];key=(item['scene_id'],item['finding'])
            if key in seen:continue
            seen.add(key)
            scene=next((scene for scene in render.payload.video_spec.scenes if scene.scene_id==item['scene_id']),None)
            assessed=semantic_observation_diagnosis(self.db,observation,row,render) or (
                presentation_correction(render.payload.video_spec,item,draft)['diagnosis'] if scene is not None else diagnosis_for_finding(item['finding']))
            findings.append(dict(observation_id=observation['id'],finding=item['finding'],source=observation['source'],
                needs_semantic_diagnosis=item['finding']=='semantic_mismatch' and 'machine_diagnosis' not in observation,
                scope={'scene_id':item['scene_id'],'start_ms':None if scene is None else round(scene.start_frame*1000*fps.denominator/fps.numerator),
                    'end_ms':None if scene is None else round((scene.start_frame+scene.duration_frames)*1000*fps.denominator/fps.numerator)},
                **assessed))
        if len(findings)==1:return findings[0]
        return dict(observation_id=findings[0]['observation_id'],finding='multiple_findings',source='exact_render_observations',
            scope={'scene_id':'整片','start_ms':None,'end_ms':None},**summarize_diagnoses(findings),findings=findings)

    def _current_render_replay(self,row):
        if not row['automatic'] or row['stage']!='render' or not row['start_job_id']:return False
        latest=self.db.connection.execute("SELECT id FROM jobs WHERE project_id=? AND json_extract(payload,'$.type')='plan_content' ORDER BY rowid DESC LIMIT 1",
            (row['project_id'],)).fetchone()
        return latest is not None and latest[0]==row['start_job_id']

    def _observe_render_spec(self,job,row):
        """记录同版全部确定性装配问题，不冒充语义、口型或自然度检测。"""
        from app.presentation_repair import PresentationRepairService,PresentationObservationRequest
        from app.presentation_diagnosis import DETERMINISTIC_PRESENTATION_FINDINGS,presentation_correction
        from app.source_usage import SourceUsageService
        usage=SourceUsageService(self.db)
        if usage.review(row,exact_render=True)['state']=='approved' or usage.choice(job.project_id,job.payload.render_id)=='discarded':return
        spec=job.payload.video_spec
        draft=ProjectDraftRepository(self.db).get(job.project_id)
        known={(item['request']['scene_id'],item['request']['finding']) for item in (
            json.loads(saved[0]) for saved in self.db.connection.execute(
                'SELECT payload FROM presentation_observations WHERE run_id=? AND render_job_id=?',(row['id'],str(job.id))))}
        service=PresentationRepairService(self.db,Path(self.root),Path(self.root)/'renders')
        digest=None
        for scene in spec.scenes:
            for finding in DETERMINISTIC_PRESENTATION_FINDINGS:
                if (scene.scene_id,finding) in known:continue
                if not presentation_correction(spec,{'scene_id':scene.scene_id,'finding':finding},draft)['observed']:continue
                try:
                    digest=digest or service._hash_render(job)
                    service.observe(job.project_id,UUID(row['id']),PresentationObservationRequest(
                        idempotency_key=f'render-spec-qa:{job.id}:{scene.scene_id}:{finding}',render_sha256=digest,
                        scene_id=scene.scene_id,finding=finding,reason='当前成片规格存在确切的重复文字或已知字幕冲突。',
                        evidence_reference=f'render-spec:{job.id}:{digest}'),source='deterministic_spec_qa')
                    known.add((scene.scene_id,finding))
                except (ValueError,RuntimeError,OSError) as exc:
                    self._stop_quality_repair(row,getattr(exc,'reasons',None) or ['presentation_output_evidence_unavailable'])
                    return

    def _stop_quality_repair(self,row,reasons):
        with self.db.transaction(immediate=True):
            current=self.db.connection.execute('SELECT stage,render_job_id,waiting_stop_reasons FROM production_runs WHERE id=?',(row['id'],)).fetchone()
            if current is None or current['stage']!='render' or current['render_job_id']!=row['render_job_id']:return
            kept=json.loads(current['waiting_stop_reasons'])
            self.db.connection.execute('UPDATE production_runs SET waiting_stop_reasons=? WHERE id=?',
                (json.dumps(list(dict.fromkeys([*kept,*(f'automatic_stop:{reason}' for reason in reasons)]))),row['id']))

    def _repair_presentation(self,row,diagnosis):
        from app.presentation_repair import PresentationRepairService,PresentationRepairRequest
        from app.production_runs import ProductionRunConflict
        service=PresentationRepairService(self.db,Path(self.root),Path(self.root)/'renders',semantic_diagnoser=self.semantic_diagnoser)
        def admit_current():
            current=self.db.connection.execute('SELECT * FROM production_runs WHERE id=?',(row['id'],)).fetchone()
            if current is None or current['render_job_id']!=row['render_job_id']:
                raise ProductionRunNotReady(('automatic_presentation_no_longer_current',))
            decision=self.decision_for_run(UUID(row['id']),set(JobType))
            if (decision['next_action']!='diagnose_quality' or decision['quality_diagnosis']['observation_id']!=diagnosis['observation_id']):
                raise ProductionRunNotReady(('automatic_presentation_no_longer_current',))
        try:
            plan=service.plan(UUID(row['project_id']),UUID(row['id']),UUID(diagnosis['observation_id']))
            if plan['action']=='render_revision':
                service.apply(UUID(row['project_id']),UUID(row['id']),PresentationRepairRequest(
                    observation_id=UUID(diagnosis['observation_id']),expected_fingerprint=plan['fingerprint'],
                    idempotency_key=f"automatic-presentation:{diagnosis['observation_id']}",confirmed_action='render_revision',
                    reason='修正已定位的画面缺陷，复用合格旁白与人物画面，再做整片复检。'),admission_guard=admit_current)
                return
            reasons=plan['stop_reasons'] or ['quality_scoped_fix_required:'+plan['responsible_stage']]
        except (ProductionRunNotReady,ProductionRunConflict,ValueError,LookupError) as exc:
            reasons=getattr(exc,'reasons',None) or ['presentation_diagnosis_not_executable']
            if 'automatic_presentation_no_longer_current' in reasons:return
        self._stop_quality_repair(row,reasons)

    def _material_recheck(self,row,jobs):
        render=next((job for job in jobs if job.type is JobType.RENDER and job.status is JobStatus.COMPLETED),None)
        if render is None:return None
        from app.source_usage import SourceUsageService
        usage=SourceUsageService(self.db)
        if usage.review(row,exact_render=True)['state'] in {'approved','rejected'} or usage.choice(render.project_id,render.payload.render_id)=='discarded':return None
        saved=self.db.connection.execute('SELECT payload FROM presentation_repairs WHERE run_id=? AND replacement_job_id=?',
            (row['id'],str(render.id))).fetchone()
        if saved is None:return None
        record=json.loads(saved[0])
        if not record['plan'].get('material_replacements'):return None
        machine=record.get('semantic_recheck')
        if machine is None:return {'repair_id':record['id'],'result':None}
        from app.presentation_semantic import material_recheck_is_current
        current=material_recheck_is_current(self.db,machine,row,render)
        return {'repair_id':record['id'],'result':{**{key:machine.get(key) for key in (
            'state','assessment_state','responsible_stage','scope','limitations','required_capability','stop_reasons')},
            'evidence_current':current if machine.get('binding') is not None else None}}

    def _queue_decision(self, row, jobs, handler_types) -> ProductionDecision:
        reasons = [v for v in json.loads(row["waiting_stop_reasons"]) if not v.startswith(IN_PROGRESS_REASONS)]
        auto_reasons = json.loads(row["voice_qa_auto_stop_reasons"])
        parent = JobRepository(self.db).get(UUID(row["start_job_id"])) if row["start_job_id"] else None
        failed = [j for j in jobs if j.status in {JobStatus.FAILED, JobStatus.CANCELLED}]
        stage = "talking" if row["stage"] == "awaiting_talking" else row["stage"]
        action = "continue"
        explicit_quality_owner = False
        diagnosis=self._presentation_diagnosis(row,jobs)
        material_recheck=self._material_recheck(row,jobs) if row['stage']=='render' else None
        if (row["stage"] == "cancelled" or (row["start_job_id"] and (parent is None or parent.status in {JobStatus.FAILED, JobStatus.CANCELLED}))
            or auto_reasons or any(v != "planned_talking_qa_failed" for v in reasons)):
            action = "stopped"
        elif row['voice_audio_id'] and (audio:=AudioAssetRepository(self.db).get(UUID(row['voice_audio_id']))) is not None and voice_human_review_status(audio)=='rejected':
            action,stage='stopped','voice';explicit_quality_owner=True
            reasons=['voice_quality_review_rejected']
        elif row['stage']=='render' and any(job.type is JobType.RENDER and job.status is JobStatus.COMPLETED for job in jobs):
            from app.presentation_repair import PRESENTATION_CORRECTIONS
            if diagnosis is not None:stage=diagnosis['responsible_stage']
            # 后加装配观察不能以新 Render 洗掉尚未通过的换材复验。
            if material_recheck is not None and (material_recheck['result'] is None
                    or material_recheck['result']['state']!='pass' or not material_recheck['result']['evidence_current']):
                explicit_quality_owner=True
                if material_recheck['result'] is None:action,stage='verify_material','assembly'
                else:
                    action='stopped';stage=material_recheck['result'].get('responsible_stage') or 'unknown'
                    reasons=material_recheck['result'].get('stop_reasons') or ['material_semantic_recheck_not_passed']
                    if material_recheck['result']['evidence_current'] is False:
                        stage='unknown';reasons=['material_semantic_recheck_stale']
            elif diagnosis is None:action,stage='review','review'
            elif any(finding.get('needs_semantic_diagnosis') for finding in diagnosis.get('findings',[diagnosis])):
                action='diagnose_semantic'
            elif all(finding['assessment_state']=='known' and (finding['finding'] in PRESENTATION_CORRECTIONS or (
                    finding['finding']=='semantic_mismatch'
                    and finding['responsible_stage']=='material' and finding.get('evidence_current'))
                    ) for finding in diagnosis.get('findings',[diagnosis])) and row['repair_allowance']>0:
                action='diagnose_quality'
            else:
                action='stopped';reasons=['quality_scoped_fix_required:'+diagnosis['responsible_stage']]
        elif failed:
            qa = JobType.VERIFY_VOICE if stage == "voice" else JobType.VERIFY_TALKING
            if (row["automatic"] and row["repair_allowance"] > 0
                and all(j.type is qa and j.status is JobStatus.FAILED
                    and j.error_code == ("voice_qa_failed" if stage == "voice" else "talking_qa_failed") for j in failed)):
                action = "diagnose_qa"
            else:
                action = "stopped"
        elif reasons:
            action = "stopped"
        if action == "stopped" and not reasons:
            reasons = auto_reasons or ([failed[0].error_code or "job_failed"] if failed else
                [parent.error_code or 'production_start_'+parent.status.value] if parent is not None
                    and parent.status in {JobStatus.FAILED,JobStatus.CANCELLED} else ["production_stopped"])
        if action=='stopped' and not explicit_quality_owner:
            # Run停在哪一阶段不等于缺陷归哪一道工序；只映射已明确的失败原因。
            if diagnosis and any(reason.removeprefix('automatic_stop:').startswith('presentation_') for reason in reasons):
                stage=diagnosis['responsible_stage']
            for reason in reasons:
                code=reason.removeprefix('automatic_stop:').partition(':')[0]
                if code.startswith(('scene_planner_','visual_revision_')) or code=='planning_tail_budget':
                    stage='planning';break
                if code.startswith(('semantic_','material_semantic_recheck_')):
                    stage='assembly';break
                if code.startswith(('material_','external_acquisition_','landscape_fallback_','source_usage_','source_semantic_','talking_motion_')) or code=='creator_talking_reference_not_configured':
                    stage='material';break
                if code in {'draft_talking_interval_mismatch','draft_talking_speech_would_be_cut','master_narration_interval_mismatch','master_scene_timing_unresolved'}:
                    stage='timing';break
                if code=='talking_qa_narration_requires_voice_repair':
                    stage='voice';break
                if code in {'preliminary_edit_plan_conflict','visual_presentation_unresolved'}:
                    stage='assembly';break
        next_job=next((job for job in jobs if job.status in {JobStatus.PENDING,JobStatus.RUNNING}),None) if action=='continue' else None
        return {"status": "diagnosing_qa" if action in DIAGNOSTIC_ACTIONS else ("failed" if action == "stopped" else None),
            "next_action": action, "responsible_stage": stage, "stop_reasons": reasons,
            "quality_diagnosis":diagnosis,
            'material_recheck':None if material_recheck is None else material_recheck['result'],
            'material_repair_id':None if material_recheck is None else material_recheck['repair_id'],
            "next_job_id":str(next_job.id) if next_job is not None else None,
            "next_job_type":next_job.type.value if next_job is not None else None}

    def decision_for_run(self,run_id,handler_types) -> ProductionDecision:
        from app.production_runs import production_run_jobs
        row,jobs=production_run_jobs(self.db,run_id)
        return self._queue_decision(row,jobs,handler_types)

    def queue_jobs(self, handler_types,*,run_id=None):
        from app.production_runs import production_run_jobs
        supported=self._completion_types(handler_types)
        candidates=[]
        if run_id is None:
            for row in self.db.connection.execute("SELECT id FROM jobs WHERE json_extract(payload,'$.type')='plan_content' AND status IN ('pending','running') ORDER BY created_at"):
                candidates.append(JobRepository(self.db).get(UUID(row[0])))
        query="SELECT * FROM production_runs WHERE stage IN ('voice','awaiting_talking','render')"
        for row in self.db.connection.execute(query+(' AND id=?' if run_id is not None else ''),
            (str(run_id),) if run_id is not None else ()).fetchall():
            if row['render_job_id'] and self._current_render_replay(row):
                render=JobRepository(self.db).get(UUID(row['render_job_id']))
                if render and render.status is JobStatus.COMPLETED and self.decision_for_run(UUID(row['id']),handler_types)['next_action']=='review':
                    self._observe_render_spec(render,row)
                    row=self.db.connection.execute('SELECT * FROM production_runs WHERE id=?',(row['id'],)).fetchone()
            pending=IN_PROGRESS_REASONS
            reasons=json.loads(row["waiting_stop_reasons"])
            _,jobs=production_run_jobs(self.db,UUID(row["id"]))
            decision=self._queue_decision(row,jobs,handler_types)
            if decision["next_action"] == "stopped":continue
            if decision['next_action']=='verify_material':
                if JobType.RENDER not in handler_types:continue
                if self.semantic_diagnoser is None:
                    from app.presentation_semantic import PresentationSemanticDiagnosis
                    diagnoser=PresentationSemanticDiagnosis(self.db,self.root,None,None,None)
                else:diagnoser=self.semantic_diagnoser
                diagnoser.verify_material_repair(UUID(row['project_id']),UUID(row['id']),UUID(decision['material_repair_id']))
                continue
            if decision['next_action']=='diagnose_semantic':
                if JobType.RENDER not in handler_types:continue
                finding=next(value for value in decision['quality_diagnosis'].get('findings',[decision['quality_diagnosis']])
                    if value.get('needs_semantic_diagnosis'))
                if self.semantic_diagnoser is None:
                    from app.presentation_semantic import PresentationSemanticDiagnosis
                    diagnoser=PresentationSemanticDiagnosis(self.db,self.root,None,None,None)
                else:diagnoser=self.semantic_diagnoser
                diagnoser.diagnose(UUID(row['project_id']),UUID(row['id']),UUID(finding['observation_id']))
                continue  # 只保存诊断；语义修正尚无受支持执行器，不由此重生媒体。
            if decision['next_action']=='diagnose_quality':
                self._repair_presentation(row,decision['quality_diagnosis'])
                current,jobs=production_run_jobs(self.db,UUID(row['id']))
                if self._queue_decision(current,jobs,handler_types)['next_action']=='continue':candidates.extend(jobs)
                continue
            if decision["next_action"] == "diagnose_qa":
                from app.production_runs import ProductionRunNotReady,ProductionRunConflict
                if row['stage']=='voice' and {JobType.GENERATE_VOICE,JobType.VERIFY_VOICE}<=handler_types:
                    from app.voice_repair import VoiceRepairService
                    repair=VoiceRepairService(self.db,self.root)
                elif {JobType.GENERATE_TALKING,JobType.VERIFY_TALKING}<=handler_types:
                    from app.talking.repair import TalkingRepairService
                    repair=TalkingRepairService(self.db,self.root)
                else:continue
                try:
                    repaired=repair.reconcile_automatic_qa_failure(UUID(row['project_id']),UUID(row['id']))
                except (ProductionRunNotReady,ProductionRunConflict,LookupError):continue
                if repaired is None:continue
                if 'planned_talking_qa_failed' in reasons:
                    with self.db.transaction(immediate=True):
                        current=self.db.connection.execute('SELECT waiting_stop_reasons FROM production_runs WHERE id=?',(row['id'],)).fetchone()
                        kept=[value for value in json.loads(current[0]) if value != 'planned_talking_qa_failed']
                        self.db.connection.execute('UPDATE production_runs SET waiting_stop_reasons=? WHERE id=?',(json.dumps(kept),row['id']))
                    if any(not value.startswith(pending) for value in kept):continue
                _,jobs=production_run_jobs(self.db,UUID(row['id']))
            for job in jobs:
                if job.status is JobStatus.COMPLETED and job.type in supported:
                    self.completed(job, handler_types)
            current,jobs=production_run_jobs(self.db,UUID(row["id"]))
            if self._queue_decision(current,jobs,handler_types)["next_action"] == "continue":
                candidates.extend(jobs)
        return [job for job in candidates if job.type in handler_types and job.status in {JobStatus.PENDING,JobStatus.RUNNING}]

    def status(self,project_id):
        from app.production_runs import production_run_jobs
        row=self.db.connection.execute("SELECT id FROM jobs WHERE project_id=? AND json_extract(payload,'$.type')='plan_content' ORDER BY rowid DESC LIMIT 1",(str(project_id),)).fetchone()
        if row is None:return None
        start=JobRepository(self.db).get(UUID(row[0]))
        bound=self.db.connection.execute("SELECT id FROM production_runs WHERE start_job_id=?",(str(start.id),)).fetchone()
        run=self.runs.get(project_id,UUID(bound[0])) if bound else None
        jobs=[start]+(production_run_jobs(self.db,run.id)[1] if run else [])
        finished=next((job for job in jobs if job.type is JobType.RENDER and job.status is JobStatus.COMPLETED),None)
        end=finished.updated_at if finished else datetime.now(timezone.utc)
        state=start.status if run is None or start.status in {JobStatus.FAILED,JobStatus.CANCELLED} else run.status
        decision = self._queue_decision(production_run_jobs(self.db, run.id)[0] if run else
            dict(stage='planning',waiting_stop_reasons='[]',voice_qa_auto_stop_reasons='[]',start_job_id=None,
                automatic=1,repair_allowance=0,voice_audio_id=None),jobs[1:] if run else [start],set(JobType))
        failed_job=next((job for job in reversed(jobs) if job.status is JobStatus.FAILED),None)
        error=start.error_code or (failed_job.error_code if failed_job else None)
        if decision["next_action"] in DIAGNOSTIC_ACTIONS:
            state=decision["status"];error=None
        elif decision["next_action"] == "stopped":
            state=run.status if run and (run.status=="cancelled" or run.status.endswith("_failed")) else (start.status if not run else "failed")
            error=error or decision["stop_reasons"][0].removeprefix("automatic_stop:")
        if state=="failed" or str(state).endswith("_failed"):
            end=max(job.updated_at for job in jobs)
        project=ProjectRepository(self.db).get(project_id)
        render=next((job for job in reversed(jobs) if job.type is JobType.RENDER),None)
        spec=render.payload.video_spec if render else None
        draft=ProjectDraftRepository(self.db).get(project_id)
        origin_id=getattr(start.payload,"resume_from_job_id",None) or start.id
        resumable=(start.status is JobStatus.FAILED and run is None and draft is not None
            and self.db.connection.execute("SELECT 1 FROM production_runs WHERE start_job_id=?",(str(origin_id),)).fetchone() is None
            and f"content_plan_job:{origin_id}" in draft.evidence_refs)
        material_requests=([dict(scene_id=s.scene_id,viewer_takeaway=s.viewer_takeaway,
            **s.material_request.model_dump(mode="json")) for s in draft.scenes if s.material_request]
            if draft and f"content_plan_job:{origin_id}" in draft.evidence_refs else [])
        return dict(start_job_id=str(start.id),topic=start.payload.topic,status=state,
            next_action=decision["next_action"],material_recheck=decision['material_recheck'],
            responsible_stage=decision["responsible_stage"],next_job_id=decision["next_job_id"],
            next_job_type=decision["next_job_type"],stop_reasons=decision["stop_reasons"],
            quality_diagnosis=decision['quality_diagnosis'],
            material_requests=material_requests,
            can_resume=resumable,resume_draft_version=draft.version if resumable else None,
            origin_job_id=str(origin_id),
            error_code=error,run=None if run is None else run.model_dump(mode="json"),
            output_format=spec.format if spec else project.format,
            output_width=spec.width if spec else project.resolution_width,
            output_height=spec.height if spec else project.resolution_height,
            elapsed_ms=max(0,int((end-start.created_at).total_seconds()*1000)),
            phases=[dict(type=job.type,status=job.status,started_at=job.started_at,finished_at=job.updated_at if job.status in {JobStatus.COMPLETED,JobStatus.FAILED} else None,
                duration_ms=max(0,int((job.updated_at-job.started_at).total_seconds()*1000)) if job.started_at and job.status in {JobStatus.COMPLETED,JobStatus.FAILED} else None) for job in jobs])


class ContentPlanJobHandler:
    def __init__(self,service,planner,cost_estimator):
        self.service,self.planner,self.cost_estimator=service,planner,cost_estimator

    def __call__(self,job):
        from app.jobs.runner import JobExecutionError
        try:self.service.plan(job,self.planner,self.cost_estimator)
        except Exception as exc:
            from app.jobs.handlers import _write_render_failure_diagnostic
            _write_render_failure_diagnostic(job,self.service.root,"content_planning",exc)
            from app.providers.scene_planner import ScenePlannerError
            from app.providers.vision import VisionError, VisionTimeout
            from app.scene_planning import _scene_planner_error_code
            reasons=getattr(exc,'reasons',())
            actionable=[reason for reason in reasons if not reason.startswith(IN_PROGRESS_REASONS)]
            field = "opening_context" if getattr(exc,"domain_rule",None)=="opening_context" else getattr(exc,"domain_field","unknown")
            code=("material_video_analysis_timeout" if isinstance(exc,VisionTimeout)
                else "material_video_analysis_failed" if isinstance(exc,VisionError)
                else _scene_planner_error_code(exc)+(":"+field if field!="unknown" else "")
                if isinstance(exc,ScenePlannerError) else getattr(exc,"code",None) or (actionable or reasons or ('video_production_failed',))[0])
            if len(code)>100:code=code.split(':',1)[0]
            raise JobExecutionError(code,"自动制作未完成，失败原因已保留；未自动重试",retryable=False) from exc
