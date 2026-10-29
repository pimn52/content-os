"""普通计划任务的有限外部取材；下载结果仍须通过既有区间用途核验。"""
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import time
from uuid import UUID, uuid5

from app.budget import BudgetLimitError, snapshot
from app.db import AssetRepository, AudioAssetRepository, BudgetPolicyRepository, ClipRepository, JobRepository, ProviderCallRepository
from app.domain.external_media import ExternalVideo
from app.domain.models import JobStatus, JobType, ScenePlan, SourceKind
from app.media.local_paths import existing_local_media_file


class ExternalAcquisitionError(ValueError):
    def __init__(self, message, code="external_acquisition_failed"):
        super().__init__(message)
        self.code = code


def reserve_production_tail(db, project, operation="stock_import",*,revision_reserve_calls=0,after_master=False,remaining_talking_calls=1):
    # 沿用普通制作最低调用数，另留一次下载后的区间用途核验。
    tail = (4 if project.presentation_style == "creator_led" else 3) + 1 + revision_reserve_calls
    if after_master:tail=remaining_talking_calls+1+1  # 人物段、渲染余量、一次素材用途核验；声音已合格。
    # 搜索前同时留出至少一次原文件获取，避免花完查询额度才发现不能下载。
    acquisition = 3 if operation in {"web_search", "web_fetch"} else 2 if operation == "stock_search" else 1
    policies = BudgetPolicyRepository(db)
    for policy in (policies.get_by_project(None), policies.get_by_project(project.id)):
        if policy is None:
            continue
        calls = ProviderCallRepository(db).list() if policy.project_id is None else ProviderCallRepository(db).list_for_project(project.id)
        if policy.max_calls is not None and snapshot(calls, policy.currency).calls + acquisition + tail > policy.max_calls:
            raise BudgetLimitError("external_material_tail_budget", "外部获取不能耗尽后续制作额度")


class BoundedExternalAcquirer:
    def __init__(self, db, root, sources, service_factory, analyze, *, max_searches=3,
                 max_imports=5, file_bytes=32*1024*1024, max_seconds=120, initial_imports=3,
                 web_sources=(), web_service_factory=None, max_web_searches=2,
                 max_page_resolutions=2, max_web_fetches=1):
        self.db, self.root = db, Path(root)
        self.sources, self.service_factory, self.analyze = tuple(sources), service_factory, analyze
        self.max_searches, self.max_imports = max_searches, max_imports
        self.initial_imports = initial_imports
        self.file_bytes, self.max_seconds = file_bytes, max_seconds
        self.web_sources, self.web_service_factory = tuple(web_sources), web_service_factory
        self.max_web_searches, self.max_page_resolutions, self.max_web_fetches = max_web_searches, max_page_resolutions, max_web_fetches

    @property
    def planning_sources(self):
        return (*self.sources, *(("public_web",) if self.web_sources else ()))

    @staticmethod
    def _service(factory, source, project, state):
        import inspect
        params=inspect.signature(factory).parameters
        if "after_master" in params:
            return factory(source,project,after_master=bool(state.get("master_audio_id")),
                remaining_talking_calls=state.get("remaining_talking_calls",1))
        return factory(source,project)

    def resolution_scope(self, job_id):
        path = self.root / "render-diagnostics" / str(job_id) / "external-materials.json"
        if not path.exists():
            return job_id
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            supplement = state.get("supplement")
            if supplement is None:
                return job_id
            expected = uuid5(job_id,"external-material-supplement")
            if not isinstance(supplement,dict) or supplement.get("analysis_scope_id") != str(expected):
                raise ValueError("invalid material analysis scope")
            return expected
        except (ValueError,AttributeError,OSError) as exc:
            raise ExternalAcquisitionError("补检恢复记录不完整或不属于当前任务", "external_acquisition_changed") from exc

    @staticmethod
    def _queries(scenes):
        queries = {}
        for scene in sorted(scenes, key=lambda s: s.order):
            request = scene.material_request
            if request is None or request.source_kind != "stock" or scene.visual_requirement == "creator_speaking":
                continue
            query = " ".join(request.query.split())
            queries.setdefault(query, []).append(scene)
        return queries

    @staticmethod
    def _scene_fingerprint(scenes):
        return hashlib.sha256(json.dumps([scene.model_dump(mode="json")
            for scene in sorted(scenes,key=lambda value:value.order)],sort_keys=True,
            ensure_ascii=False).encode()).hexdigest()

    @staticmethod
    def _validated_visuals(scenes, visuals, *, motion=False):
        """只接受一次画面调整；文案、主张、人物要求不变，不增加生成请求。"""
        originals = {scene.scene_id:scene for scene in scenes}
        mutable = {"visual_intent", "preferred_sources", "fallback_sources", "preferred_clip_id", "material_request"}
        if motion:mutable.add("supporting_visual_intent")
        try:
            result = tuple(ScenePlan.model_validate(value.model_dump(mode="json") if isinstance(value,ScenePlan) else value)
                for value in visuals)
            if len(result) != len(originals) or {value.scene_id for value in result} != set(originals):
                raise ValueError("visual revision changed scene identity")
            for scene in result:
                original = originals[scene.scene_id]
                if (scene.material_request is not None
                    or scene.model_dump(exclude=mutable) != original.model_dump(exclude=mutable)):
                    raise ValueError("visual revision changed content or generation request")
                if motion and scene.supporting_visual_intent != original.supporting_visual_intent:
                    if (original.visual_requirement!="creator_speaking" or original.supporting_visual_intent is not None
                        or scene.supporting_visual_intent is None
                        or not set(original.fallback_sources).issubset(scene.fallback_sources)
                        or any(source not in {SourceKind.USER_ASSET,SourceKind.HISTORICAL_ASSET,SourceKind.STOCK}
                            for source in set(scene.fallback_sources)-set(original.fallback_sources))):
                        raise ValueError("motion alternative violates original scene")
        except (ValueError,TypeError,AttributeError) as exc:
            raise ExternalAcquisitionError("画面调整不能改变原稿或增加生成请求", "external_acquisition_changed") from exc
        return tuple(sorted(result,key=lambda value:value.order))

    @staticmethod
    def _validated_estimated(scenes, estimated):
        """估算只展开已允许的切题备选；不改原稿，也不宣称真实配时。"""
        from app.talking.scheduling import supporting_footage_scene
        try:
            originals=tuple(scenes)
            result=tuple(ScenePlan.model_validate(value.model_dump(mode="json") if isinstance(value,ScenePlan) else value)
                for value in estimated)
            if len(result)!=len(originals):raise ValueError("estimated scene count changed")
            changed=False
            for original,scene in zip(originals,result):
                if scene==original:continue
                if scene!=supporting_footage_scene(original):raise ValueError("unplanned estimated visual")
                changed=True
            if (not changed or any(scene.visual_requirement=='creator_speaking' for scene in originals)
                and not any(scene.visual_requirement=='creator_speaking' for scene in result)):
                raise ValueError("estimated projection must preserve creator speech")
            return result
        except (ValueError,TypeError,AttributeError) as exc:
            raise ExternalAcquisitionError("估算取材只能使用原稿明确允许的切题备选并保留本人出镜", "external_acquisition_changed") from exc

    def _motion_revision_authorized(self, job_id, visuals, master_audio_id):
        path=self.root/"render-diagnostics"/str(job_id)
        try:
            attempt=json.loads((path/"visual-revision-attempt.json").read_text(encoding="utf-8"))
            result=json.loads((path/"motion-revision.json").read_text(encoding="utf-8"))
            master=AudioAssetRepository(self.db).get(UUID(str(master_audio_id)))
            values=[ScenePlan.model_validate(value.model_dump(mode="json") if isinstance(value,ScenePlan) else value)
                for value in visuals]
            expected=[ScenePlan.model_validate(value) for value in result["scenes"]]
            if (attempt.get("kind")!="talking_capacity" or master is None or values!=expected
                or attempt.get("master_audio_id")!=str(master.id) or result.get("master_audio_id")!=str(master.id)
                or attempt.get("master_content_hash")!=master.content_hash or result.get("master_content_hash")!=master.content_hash
                or not isinstance(result.get("reasons"),dict) or not result["reasons"]
                or any(not isinstance(reason,str) or not reason.strip() for reason in result["reasons"].values())):
                raise ValueError("motion revision binding changed")
            return True
        except (ValueError,TypeError,KeyError,OSError):
            raise ExternalAcquisitionError("人物实拍备选缺少原稿与合格旁白绑定", "external_acquisition_changed") from None

    def original_scenes(self, job_id):
        path = self.root / "render-diagnostics" / str(job_id) / "external-materials.json"
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            scenes = tuple(ScenePlan.model_validate(value) for value in state["original_scenes"])
            if not scenes or self._scene_fingerprint(scenes) != state["scene_fingerprint"]:
                raise ValueError("original scene binding changed")
            return scenes
        except (KeyError,ValueError,TypeError,OSError) as exc:
            raise ExternalAcquisitionError("原任务缺少确切原稿，不能反推或重建取材台账", "external_acquisition_changed") from exc

    def remember_original_scenes(self, project, scenes, job_id):
        """已选材或纯人物初稿也留原稿，供真实配时后按同一台账补缺。"""
        path = self.root / "render-diagnostics" / str(job_id) / "external-materials.json"
        if path.exists():
            return  # 旧台账不从已变更的草稿反推，实际补缺时核验原始记录。
        if self._queries(scenes):
            raise ExternalAcquisitionError("已有外取需求须走正常获取", "external_acquisition_changed")
        self.acquire(project, scenes, job_id)  # 空需求仅持久原稿，不搜索或下载。

    def _validated_measured(self, project, scenes, measured, master_audio_id):
        from app.assembly.video_spec import _master_narration_intervals, _require_generated_voice_qa
        from app.media.source_audio import admitted_source_audio_binding
        from app.voice_boundary_alignment import voice_asset_project_id
        from app.voice_plan import require_project_voice_audio
        from app.voice_qa import comparison_tokens, verified_generated_copy_tokens
        from app.media.planning import split_footage_clauses
        try:
            master = AudioAssetRepository(self.db).get(UUID(str(master_audio_id)))
            if master is None:
                raise ValueError("master missing")
            if admitted_source_audio_binding(self.db,master,project.id) is None:
                if voice_asset_project_id(master,JobRepository(self.db)) != project.id:
                    raise ValueError("master project changed")
                bound = self.db.connection.execute("SELECT id FROM production_runs WHERE project_id=? AND voice_audio_id=? AND automatic=1",
                    (str(project.id),str(master.id))).fetchone()
                _require_generated_voice_qa(master,self.db,production_run_id=UUID(bound["id"]) if bound else None)
                require_project_voice_audio(self.db,project.id,master)
            source = existing_local_media_file(master.source_file,self.root)
            with source.open("rb") as stream:
                if hashlib.file_digest(stream,"sha256").hexdigest() != master.content_hash:
                    raise ValueError("master file changed")
            result = tuple(sorted((ScenePlan.model_validate(value.model_dump(mode="json") if isinstance(value,ScenePlan) else value)
                for value in measured),key=lambda value:value.order))
            if not result or len({scene.order for scene in result})!=len(result):
                raise ValueError("measured scene order changed")
            target = tuple(token for scene in scenes for token in comparison_tokens(scene.voice_text))
            observed = tuple(token for segment in master.transcript_segments for token in comparison_tokens(segment.text))
            if (not target or not master.transcript_source
                or target != observed and target != verified_generated_copy_tokens(master)
                or tuple(token for scene in result for token in comparison_tokens(scene.voice_text)) != target):
                raise ValueError("master copy changed")
            intervals = _master_narration_intervals(master,result)
            if any(scene.duration_target_ms != end-start for scene,start,end in intervals):
                raise ValueError("measured duration differs from master")
            cursor = 0
            footage = {SourceKind.USER_ASSET,SourceKind.HISTORICAL_ASSET,SourceKind.STOCK,SourceKind.AI_BROLL}
            for original in split_footage_clauses(scenes):
                parts=[];copy=""
                while len(copy)<len(original.voice_text) and cursor<len(intervals):
                    scene,start,end=intervals[cursor];cursor+=1;parts.append((scene,start,end));copy+=scene.voice_text
                if copy != original.voice_text or not parts or len(parts)>1 and original.visual_requirement!="creator_speaking":
                    raise ValueError("measured scene changed original copy")
                for scene,start,end in parts:
                    identity = original.id if len(parts)==1 else uuid5(original.id,f"master-clause:{master.id}:{start}:{end}")
                    scene_id = original.scene_id if len(parts)==1 else f"{original.scene_id[:70]}--speech-{identity.hex[:12]}"
                    expected=original.model_copy(update={"id":identity,"scene_id":scene_id,"order":scene.order,
                        "voice_text":scene.voice_text,"duration_target_ms":end-start})
                    if original.visual_requirement!="creator_speaking" and scene.preferred_clip_id is not None:
                        # 初轮选材会登记确切Clip并清除已满足的请求；选中本身不是用途通过，后段仍须核验。
                        clip=ClipRepository(self.db).get(scene.preferred_clip_id)
                        asset=AssetRepository(self.db).get(clip.asset_id) if clip else None
                        if asset is None:
                            raise ValueError("selected material missing")
                        fallback=list(original.fallback_sources)
                        if asset.source_kind not in (*original.preferred_sources,*fallback):
                            from app.media.visual_facts import full_interval_geometry,matches_scene
                            facts=full_interval_geometry(asset,clip)
                            if (asset.source_kind not in footage
                                or asset.source_kind is SourceKind.AI_BROLL and scene.visual_requirement=="action_evidence"
                                or facts.get("coverage")!="video_interval_analysis" or not matches_scene(facts,scene)):
                                raise ValueError("selected source lacks exact use QA")
                            material=existing_local_media_file(asset.source_file,self.root)
                            with material.open("rb") as stream:
                                if hashlib.file_digest(stream,"sha256").hexdigest()!=asset.content_hash:
                                    raise ValueError("selected material file changed")
                            fallback.append(asset.source_kind)
                        expected=expected.model_copy(update={"preferred_clip_id":scene.preferred_clip_id,"material_request":None,
                            "fallback_sources":fallback})
                    if scene.visual_requirement != original.visual_requirement:
                        if (original.visual_requirement!="creator_speaking" or original.supporting_visual_intent is None
                            or scene.visual_requirement!="explanatory"):
                            raise ValueError("measured scene changed visual requirement")
                        from app.talking.scheduling import supporting_footage_scene
                        expected=supporting_footage_scene(expected)
                    if scene != expected:
                        raise ValueError("measured scene changed content or unplanned visuals")
            if cursor!=len(intervals):
                raise ValueError("measured scene added content")
            return result,master
        except (ValueError,TypeError,KeyError,AttributeError,OSError) as exc:
            raise ExternalAcquisitionError("真实配时取材必须绑定合格旁白、完整原稿及获准替代画面", "external_acquisition_changed") from exc

    @staticmethod
    def _validated_gaps(scenes, gaps):
        from app.media.planning import split_footage_clauses
        originals = {scene.scene_id:scene for scene in split_footage_clauses(scenes)}
        result = []
        try:
            for value in gaps:
                gap = ScenePlan.model_validate(value.model_dump(mode="json") if isinstance(value,ScenePlan) else value)
                original = originals.get(gap.scene_id)
                if (original is None or original.material_request is not None
                        or original.visual_requirement == "creator_speaking"
                        or SourceKind.STOCK not in (*original.preferred_sources,*original.fallback_sources)
                        or gap.material_request is None or gap.material_request.source_kind != "stock"
                        or gap.preferred_clip_id is not None
                        or original.model_dump(exclude={"preferred_clip_id","material_request"})
                            != gap.model_dump(exclude={"preferred_clip_id","material_request"})
                        or any(scene.scene_id == gap.scene_id for scene in result)):
                    raise ValueError("gap does not match an eligible original scene")
                result.append(gap)
        except (ValueError,TypeError,AttributeError) as exc:
            raise ExternalAcquisitionError("补缺只能为原计划允许外取的非人物画面增加需求", "external_acquisition_changed") from exc
        return tuple(sorted(result,key=lambda value:(value.order,value.scene_id)))

    def resolution_scenes(self, scenes, job_id):
        path = self.root / "render-diagnostics" / str(job_id) / "external-materials.json"
        if not path.exists():
            return scenes
        state = json.loads(path.read_text(encoding="utf-8"))
        if not state.get("gap_scenes") and not state.get("visual_scenes") and not state.get("measured_scenes") and not state.get("estimated_scenes"):
            return scenes
        if state.get("scene_fingerprint") != self._scene_fingerprint(scenes):
            raise ExternalAcquisitionError("原计划已改变，不能复用补缺需求", "external_acquisition_changed")
        motion = bool(state.get("motion_revision"))
        if motion:self._motion_revision_authorized(job_id,state["visual_scenes"],state.get("master_audio_id"))
        active = self._validated_visuals(scenes,state["visual_scenes"],motion=motion) if state.get("visual_scenes") else scenes
        if state.get("measured_scenes"):
            from app.db import ProjectRepository
            project=ProjectRepository(self.db).get(active[0].project_id)
            active,master=self._validated_measured(project,active,state["measured_scenes"],state.get("master_audio_id"))
            if master.content_hash!=state.get("master_content_hash"):
                raise ExternalAcquisitionError("已登记旁白发生变化", "external_acquisition_changed")
        elif state.get("estimated_scenes"):
            active=self._validated_estimated(scenes,state["estimated_scenes"])
        gaps = {scene.scene_id:scene for scene in self._validated_gaps(active,state.get("gap_scenes",()))}
        from app.media.planning import split_footage_clauses
        return tuple(gaps.get(scene.scene_id,scene) for scene in split_footage_clauses(active))

    def acquire(self, project, scenes, job_id, *, supplement_for=(), gap_scenes=(), visual_scenes=None, estimated_scenes=None,
                measured_scenes=None, master_audio_id=None, remaining_talking_calls=1):
        scenes = tuple(scenes)
        gap_scenes = tuple(gap_scenes)
        estimated = self._validated_estimated(scenes,estimated_scenes) if estimated_scenes is not None else None
        supplement_for = tuple(" ".join(query.split()) for query in supplement_for)
        queries = self._queries(scenes)
        path = self.root / "render-diagnostics" / str(job_id) / "external-materials.json"
        fingerprint = hashlib.sha256(json.dumps({"project": str(project.id), "format": project.format.value,
            "scenes": [s.model_dump(mode="json") for group in queries.values() for s in group]},
            sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if path.exists():
            state = json.loads(path.read_text(encoding="utf-8"))
            if state["fingerprint"] != fingerprint:
                raise ExternalAcquisitionError("素材需求已改变，不能复用旧任务的获取记录", "external_acquisition_changed")
        else:
            state = {"fingerprint": fingerprint, "started_at": datetime.now(timezone.utc).isoformat(),
                "searches": [], "imports": [], "skipped": [], "complete": False,
                "original_scenes":[scene.model_dump(mode="json") for scene in scenes]}
        scene_fingerprint = self._scene_fingerprint(scenes)
        if state.get("scene_fingerprint",scene_fingerprint) != scene_fingerprint:
            raise ExternalAcquisitionError("原计划已改变，不能复用旧任务的获取记录", "external_acquisition_changed")
        # 保留旧需求指纹；原本没有外取需求的场景也须锁定，补缺不能借机改稿。
        state["scene_fingerprint"] = scene_fingerprint
        if state.get("estimated_scenes"):
            previous=self._validated_estimated(scenes,state["estimated_scenes"])
            if estimated is not None and estimated!=previous:
                raise ExternalAcquisitionError("估算备选已登记，不能改写原取材需求", "external_acquisition_changed")
            estimated=previous
        if estimated is not None:
            state["estimated_scenes"]=[scene.model_dump(mode="json") for scene in estimated]
        motion = bool(state.get("motion_revision"))
        if motion:self._motion_revision_authorized(job_id,state["visual_scenes"],state.get("master_audio_id"))
        active = self._validated_visuals(scenes,state["visual_scenes"],motion=motion) if state.get("visual_scenes") else scenes
        if visual_scenes is not None:
            incoming_motion = measured_scenes is not None and (self.root/"render-diagnostics"/str(job_id)/"motion-revision.json").exists()
            if incoming_motion:self._motion_revision_authorized(job_id,visual_scenes,master_audio_id)
            incoming_visuals = self._validated_visuals(scenes,visual_scenes,motion=incoming_motion)
            if state.get("visual_scenes") and incoming_visuals != active:
                raise ExternalAcquisitionError("画面调整已登记，不能增加取材轮次", "external_acquisition_changed")
            if not state.get("visual_scenes") and state.get("supplement"):
                return None  # 初轮已用唯一补缺；画面调整不能重新取得一批下载或重置计时。
            active = incoming_visuals
            state["visual_scenes"] = [scene.model_dump(mode="json") for scene in active]
            if incoming_motion:state["motion_revision"]=True
        if state.get("measured_scenes") or measured_scenes is not None:
            stored_originals = state.get("original_scenes")
            if not stored_originals or self._scene_fingerprint(tuple(ScenePlan.model_validate(value) for value in stored_originals)) != scene_fingerprint:
                raise ExternalAcquisitionError("原任务缺少确切原稿，不能反推真实配时", "external_acquisition_changed")
            measured,master=self._validated_measured(project,active,
                measured_scenes if measured_scenes is not None else state["measured_scenes"],
                master_audio_id if master_audio_id is not None else state.get("master_audio_id"))
            expected_calls=sum(scene.visual_requirement=="creator_speaking" for scene in measured)
            if (type(remaining_talking_calls) is not int or remaining_talking_calls!=expected_calls
                or state.get("remaining_talking_calls",remaining_talking_calls)!=remaining_talking_calls):
                raise ExternalAcquisitionError("人物派发数量与真实配时不符", "external_acquisition_changed")
            if state.get("measured_scenes"):
                if (measured != tuple(ScenePlan.model_validate(value) for value in state["measured_scenes"])
                    or str(master.id)!=state.get("master_audio_id") or master.content_hash!=state.get("master_content_hash")):
                    raise ExternalAcquisitionError("真实配时已登记，不能重置取材轮次", "external_acquisition_changed")
            elif state.get("supplement") and estimated is None:
                return None
            if not state.get("measured_scenes") and estimated is not None and state.get("gap_scenes"):
                # 估算的需求与已耗额度留档；真实分句可选择其它人物段，不能沿用旧场景ID/时长。
                state["estimated_gap_scenes"]=state.pop("gap_scenes")
            active=measured
            state.update(measured_scenes=[scene.model_dump(mode="json") for scene in measured],
                master_audio_id=str(master.id),master_content_hash=master.content_hash,
                remaining_talking_calls=remaining_talking_calls)
        elif estimated is not None:
            active=estimated
        # 原查询保留供旧调用恢复；当前画面只为原来源准入内的缺口增加查询。
        for query,group in self._queries(active).items():
            queries[query]=group
        if state.get("estimated_gap_scenes"):
            archived=self._validated_gaps(estimated,state["estimated_gap_scenes"])
            for query,group in self._queries(archived).items():
                queries.setdefault(query,[]).extend(group)
        persisted_gaps = self._validated_gaps(active,state.get("gap_scenes",()))
        if gap_scenes:
            incoming = self._validated_gaps(active,gap_scenes)
            if not persisted_gaps and state.get("supplement") and state.get("estimated_gap_scenes"):
                return None  # 已耗唯一补缺；实测可以复用原素材，不能再取得一轮下载。
            if (persisted_gaps and incoming != persisted_gaps
                    or not persisted_gaps and state.get("supplement")
                    or not {" ".join(scene.material_request.query.split()) for scene in incoming}.issubset(supplement_for)):
                raise ExternalAcquisitionError("补缺批次已改变，不能增加获取轮次或额度", "external_acquisition_changed")
            persisted_gaps = incoming
            state["gap_scenes"] = [scene.model_dump(mode="json") for scene in incoming]
        for query,group in self._queries(persisted_gaps).items():
            queries.setdefault(query,[]).extend(group)
        limit = state.get("acquisition_limit_seconds", self.max_seconds)
        if (type(limit) not in (int,float) or not math.isfinite(limit) or limit<0):
            raise ExternalAcquisitionError("获取时间记录无效，不能重置原任务窗口", "external_acquisition_changed")
        limit=min(limit,self.max_seconds)
        elapsed=self._saved_elapsed(state,job_id,limit)
        if state.get("acquisition_in_progress"):
            # 硬中断没有 finally 可结算；未知部分不能在恢复时免费重获。
            elapsed=max(elapsed,limit)
        state["acquisition_limit_seconds"]=limit
        state["acquisition_in_progress"]=True
        active_started=time.monotonic()
        def used():
            return elapsed+max(0,time.monotonic()-active_started)
        def remaining():
            return limit-used()
        def save():
            state["acquisition_elapsed_seconds"]=used()
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)
        try:
            save()
            return self._acquire_active(project,queries,job_id,state,save,remaining,supplement_for)
        finally:
            state["acquisition_in_progress"]=False
            save()

    def _saved_elapsed(self,state,job_id,limit):
        """迁移旧记录只计已留证的实际操作，不把稿件等候时间算入取材。"""
        elapsed=state.get("acquisition_elapsed_seconds")
        if elapsed is not None:
            if type(elapsed) not in (int,float) or not math.isfinite(elapsed) or elapsed<0:
                raise ExternalAcquisitionError("获取累计耗时记录无效", "external_acquisition_changed")
            return elapsed
        calls=ProviderCallRepository(self.db)
        total=0.0
        def duration(start,end):
            try:
                start=datetime.fromisoformat(start) if isinstance(start,str) else start
                end=datetime.fromisoformat(end) if isinstance(end,str) else end
                value=(end-start).total_seconds()
                return value if math.isfinite(value) and value>=0 else None
            except (TypeError,ValueError,AttributeError):
                return None
        for kind,entries in (("search",state.get("searches",[])),("import",state.get("imports",[])),
                ("web",state.get("web_searches",[])),("fetch",state.get("web_fetches",[]))):
            for entry in entries:
                span=duration(entry.get("started_at"),entry.get("completed_at"))
                if span is None:
                    call=None
                    try:
                        if entry.get("call_id"):call=calls.get(UUID(entry["call_id"]))
                        if call is None:
                            key=entry.get("idempotency_key")
                            if not key and kind=="import":key=f"material:{job_id}:import:{entry['candidate_id']}"
                            elif not key and kind=="search":key=f"material:{job_id}:search:"+hashlib.sha256((entry['source']+entry['query']).encode()).hexdigest()
                            elif not key and kind=="web":key=f"material:{job_id}:web:"+hashlib.sha256((entry['source']+entry['query']).encode()).hexdigest()
                            elif not key and kind=="fetch":key=f"material:{job_id}:fetch:"+hashlib.sha256((entry['source']+entry['url']).encode()).hexdigest()
                            if key:call=calls.get_by_idempotency_key(key)
                        if call is not None:span=duration(call.created_at,call.completed_at)
                    except (ValueError,KeyError,TypeError):
                        pass
                if span is None:
                    state["acquisition_time_migration"]="unknown_duration_reserved"
                    return limit
                total+=span+1  # 为旧记录未单列的本地查验和落盘留保守余量。
                if kind=="import" and entry.get("status")=="analyzed":
                    try:
                        job=JobRepository(self.db).get(UUID(entry["analysis_job_id"]))
                        analysis=duration(job.started_at or job.created_at,job.updated_at) if job and job.status is JobStatus.COMPLETED else None
                    except (KeyError,ValueError,TypeError):
                        analysis=None
                    if analysis is None:
                        state["acquisition_time_migration"]="unknown_duration_reserved"
                        return limit
                    total+=analysis+1
        state["acquisition_time_migration"]="recorded_operation_durations"
        return total

    def _acquire_active(self,project,queries,job_id,state,save,remaining,supplement_for):
        supplement_for = sorted(set(supplement_for))
        if set(supplement_for)-set(queries):
            raise ExternalAcquisitionError("补检需求与原计划不符", "external_acquisition_changed")
        if supplement_for and not state.get("supplement"):
            # 只追加一次新候选核验；原区间不支持本次用途，不等于原素材被全局禁用。
            # 一个文件可被多个查询共用，不能按首次下载时的 query 判定其用途归属。
            rejected = {}
            for query in supplement_for:
                candidates = {candidate for search in state["searches"] if search["query"] == query
                    for candidate in search.get("candidates", [])}
                rejected[query] = sorted({entry["asset_id"] for entry in state["imports"]
                    if entry.get("asset_id") and entry["candidate_id"] in candidates})
            state["supplement"] = {"queries": supplement_for, "rejected_assets": rejected,
                "import_limit": min(self.max_imports,len(state["imports"])+2),
                "analysis_scope_id": str(uuid5(job_id, "external-material-supplement"))}
            state["complete"] = False
        elif supplement_for and supplement_for != state["supplement"]["queries"]:
            raise ExternalAcquisitionError("补检需求已改变，不能重置原任务额度", "external_acquisition_changed")
        import_limit = min(self.max_imports,state.get("supplement",{}).get("import_limit",self.initial_imports))
        save()
        # 外调已完成但进度文件未落盘时，过期也应恢复既有成果；此处只读账目和文件。
        calls = ProviderCallRepository(self.db)
        for entry in state["searches"]:
            if entry["status"] != "started":
                continue
            key = entry.get("idempotency_key") or f"material:{job_id}:search:{hashlib.sha256((entry['source']+entry['query']).encode()).hexdigest()}"
            alias = self.db.connection.execute("SELECT call_id FROM external_search_replays WHERE idempotency_key=? AND project_id=?",
                (key,str(project.id))).fetchone()
            call = calls.get(UUID(alias["call_id"])) if alias else calls.get_by_idempotency_key(key)
            if (call is None or call.status != "completed" or call.operation != "stock_search"
                    or call.project_id != project.id or call.provider != entry["source"]):
                continue
            snapshot = (call.result_payload or {}).get("result", {})
            if not isinstance(snapshot,dict) or snapshot.get("query") != entry.get("lookup_query", entry["query"]):
                continue
            try:
                candidates = [ExternalVideo.model_validate(value) for value in snapshot["candidates"]]
                candidates = [value for value in candidates if self._fits(value,project,queries[entry["query"]])]
            except (ValueError,KeyError,TypeError):
                continue
            entry.update(status="completed",call_id=str(call.id),candidates=[str(value.id) for value in candidates])
            save()
        for entry in state["imports"]:
            if entry["status"] != "started":
                continue
            call = calls.get_by_idempotency_key(f"material:{job_id}:import:{entry['candidate_id']}")
            if (call is None or call.status != "completed" or call.operation != "stock_import"
                    or call.project_id != project.id or not any(search["source"]==call.provider
                        and entry["candidate_id"] in search.get("candidates",[]) for search in state["searches"])):
                continue
            try:
                asset_id = UUID((call.result_payload or {})["result"])
                asset = AssetRepository(self.db).get(asset_id)
                job = JobRepository(self.db).get_by_idempotency_key(f"external-analysis:{asset_id}")
                if asset is None or job is None or job.type is not JobType.ANALYZE_ASSET:
                    continue
                target = self.db.connection.execute("SELECT 1 FROM asset_job_targets WHERE job_id=? AND asset_id=?",
                    (str(job.id),str(asset_id))).fetchone()
                records = asset.metadata.get("external_sources",[])
                if (not target or not isinstance(records,list) or not any(isinstance(record,dict)
                        and isinstance(record.get("candidate"),dict)
                        and record["candidate"].get("id")==entry["candidate_id"]
                        and record.get("content_hash")==asset.content_hash for record in records)):
                    continue
                source = existing_local_media_file(asset.source_file,self.root)
                with source.open("rb") as stream:
                    if hashlib.file_digest(stream,"sha256").hexdigest()!=asset.content_hash:
                        continue
            except (ValueError,KeyError,TypeError,OSError):
                continue
            entry.update(status="downloaded",asset_id=str(asset.id),analysis_job_id=str(job.id))
            save()
        rejected = state.get("supplement", {}).get("rejected_assets", {})
        def file_ready(query, minimum=1):
            identities = {candidate for search in state["searches"] if search["query"] == query
                for candidate in search.get("candidates", [])}
            originals = set()
            for entry in state["imports"]:
                if (entry["candidate_id"] not in identities or entry["status"] != "analyzed"
                        or entry.get("asset_id") in rejected.get(query, [])):
                    continue
                asset = AssetRepository(self.db).get(UUID(entry["asset_id"]))
                if asset is not None:
                    originals.add(asset.content_hash)
            # 初轮每需求一个文件；一次补检在原剩余槽内准备至多两个不同原文件。
            return len(originals) >= minimum

        def try_candidate(search, candidate_id):
            previous = next((i for i in state["imports"] if i["candidate_id"] == candidate_id), None)
            if previous and previous["status"] != "started":
                return
            if remaining() <= 0 or previous is None and len(state["imports"]) >= import_limit:
                return
            entry = previous or {"query": search["query"], "candidate_id": candidate_id,
                "status": "started", "max_bytes": self.file_bytes}
            if previous is None:
                state["imports"].append(entry)
            save()
            try:
                service = self._service(self.service_factory,search["source"],project,state)
                result = service.import_video(project.id, UUID(search["call_id"]), UUID(candidate_id),
                    f"material:{job_id}:import:{candidate_id}",
                    download_limits={"max_bytes": self.file_bytes, "deadline": time.monotonic()+remaining()})
                entry.update(status="downloaded", asset_id=str(result.asset_id), analysis_job_id=str(result.analysis_job_id))
                save()
                self.analyze(result.analysis_job_id)
                entry["status"] = "analyzed"
            except Exception as exc:
                state_name = "analysis_failed" if entry["status"] == "downloaded" else "failed"
                entry.update(status=state_name, reason=getattr(exc, "code", "external_import_failed"))
            save()

        if not state["complete"]:
            for entry in state["imports"]:
                if entry["status"] != "downloaded":
                    continue
                try:
                    self.analyze(UUID(entry["analysis_job_id"]))
                    entry["status"] = "analyzed"
                except Exception as exc:
                    entry.update(status="analysis_failed",reason=getattr(exc,"code","external_analysis_failed"))
                save()
            query_order=list(queries)
            if state.get("supplement"):
                attempted = {entry["candidate_id"] for entry in state["imports"]}
                # 没有任何缓存替代的需求先取得换源机会；不先下载已被否定来源的次选。
                query_order.sort(key=lambda query:any(identity not in attempted
                    for search in state["searches"] if search["query"]==query
                    for identity in search.get("candidates",[])))
            # 文件能进入本地分析才算满足获取需求，目录有结果不能锁住备用来源。
            for source_index, source in enumerate(self.sources):
                if any(s["source"] == source and s.get("source_blocked", s["status"] == "failed") for s in state["searches"]):
                    continue
                for query in query_order:
                    group=queries[query]
                    if state.get("supplement") and query not in rejected:
                        continue
                    if file_ready(query,1):
                        continue
                    if len(state["imports"]) >= import_limit:
                        break  # 无原文件槽位时，不再消耗搜索调用。
                    if len(query) < 2 or len(query) > 200:
                        if query not in state["skipped"]:
                            state["skipped"].append(query)
                        continue
                    if source == "pixabay" and len(query)>100:
                        skip={"query":query,"source":source,"reason":"pixabay_query_invalid"}
                        if skip not in state["skipped"]:
                            state["skipped"].append(skip)
                            save()
                        continue
                    previous = next((s for s in state["searches"] if s["query"] == query and s["source"] == source), None)
                    if state.get("supplement") and previous is not None:
                        continue  # 用途已否定时，先尝试尚未查询的来源，旧候选留到网页后备之后。
                    if previous and previous["status"] == "failed":
                        continue
                    if remaining() <= 0 or previous is None and len(state["searches"]) >= self.max_searches:
                        continue
                    backup_available=any(other!=source and not any(s["source"]==other
                        and s.get("source_blocked",s["status"]=="failed") for s in state["searches"])
                        for other in self.sources)
                    if (previous is None and source_index == 0 and backup_available and self.max_searches>1
                        and len(state["searches"]) >= self.max_searches-1):
                        break  # 在原总查询额内留一次换源机会，即使首源文件已完成本地分析。
                    entry = previous or {"query": query, "source": source, "status": "started"}
                    if previous is None:
                        state["searches"].append(entry)
                    save()
                    try:
                        service = self._service(self.service_factory,source,project,state)
                        key = f"material:{job_id}:search:{hashlib.sha256((source+query).encode()).hexdigest()}"
                        if entry["status"] == "started":
                            result = service.search(project.id, query, key)
                            candidates = [v for v in result.candidates if self._fits(v, project, group)]
                            entry.update(status="completed", call_id=str(result.call_id),
                                candidates=[str(v.id) for v in candidates])
                    except Exception as exc:
                        code = getattr(exc, "code", "external_search_failed")
                        blocked = not (code.endswith("query_invalid") or code in {"external_material_tail_budget", "provider_execution_in_progress"})
                        entry.update(status="failed", reason=code, error_type=type(exc).__name__, source_blocked=blocked)
                    save()
                    if entry.get("source_blocked"):
                        break
                    if entry.get("candidates"):
                        try_candidate(entry, entry["candidates"][0])
            if state.get("supplement"):
                self._acquire_web(project, {query:queries[query] for query in query_order}, job_id, state, remaining, file_ready,
                    try_candidate, save, import_limit)
            # 新来源及网页先用原有槽位；仍无替代才回缓存。每需求先一份，再用无其它机会的余槽。
            pools = [s for s in state["searches"] if s.get("candidates")]
            for minimum in (1,2) if state.get("supplement") else (1,):
                for index in range(1, 8):
                    for search in pools:
                        target = minimum if search["query"] in rejected else 1
                        if file_ready(search["query"],target) or index >= len(search["candidates"]):
                            continue
                        try_candidate(search, search["candidates"][index])
            if not state.get("supplement"):
                self._acquire_web(project, queries, job_id, state, remaining, file_ready,
                    try_candidate, save, import_limit)
            state["complete"] = True
            save()
        result = {}
        for entry in state["imports"]:
            if entry["status"] not in {"downloaded", "analyzed", "analysis_failed"}:
                continue
            asset = AssetRepository(self.db).get(UUID(entry["asset_id"]))
            if asset is None:
                entry.update(status="unavailable", reason="acquired_asset_removed")
                save()
                continue
            try:
                self.analyze(UUID(entry["analysis_job_id"]))
            except Exception as exc:
                entry.update(status="analysis_failed", reason=getattr(exc, "code", "external_analysis_failed"))
                save()
                continue
            entry["status"] = "analyzed"
            for search in state["searches"]:
                if entry["candidate_id"] in search.get("candidates", []):
                    result.setdefault(search["query"], []).append(asset.id)
            save()
        # 新候选优先进入有限核验窗口；旧文件仍保留，已合格的其他用途继续可用。
        for query, identities in result.items():
            result[query] = sorted(set(identities), key=lambda identity: (str(identity) in rejected.get(query, []), str(identity)))
        if supplement_for and not any(any(str(identity) not in rejected.get(query, [])
            for identity in result.get(query, [])) for query in supplement_for):
            return None
        return result

    def _fits(self, candidate, project, scenes):
        unknown_geometry = (candidate.provider == "public_page" and not candidate.dimensions_exact
            and candidate.width == candidate.height == 0)
        if not candidate.admissible or not unknown_geometry and min(candidate.width, candidate.height) < 720:
            return False
        # 仅几何粗筛，不凭目录文字宣称画面相关或允许裁切。
        target = project.resolution_width/project.resolution_height
        if not unknown_geometry and abs(candidate.width/candidate.height-target) > .04:
            return False
        if candidate.size_bytes is not None and candidate.size_bytes > self.file_bytes:
            return False
        return candidate.duration_ms is None or candidate.duration_ms >= min(s.duration_target_ms for s in scenes)

    def _acquire_web(self, project, queries, job_id, state, remaining, file_ready, try_candidate, save, import_limit):
        """图库不足时用同一需求找公开原文件；搜索工具不等于素材已取得。"""
        if not self.web_sources or self.web_service_factory is None:
            return
        searches = state.setdefault("web_searches", [])
        fetches = state.setdefault("web_fetches", [])
        rejected = state.get("supplement", {}).get("rejected_assets", {})
        def available(query):
            return (remaining() > 0 and len(state["imports"]) < import_limit
                and not file_ready(query) and (not rejected or query in rejected))
        def resolve(page, query, group, *, document=None, suffix=""):
            source = page["resolver_provider"]
            key = f"material:{job_id}:page:" + hashlib.sha256((query+page["url"]+suffix).encode()).hexdigest()
            entry = next((item for item in state["searches"] if item.get("idempotency_key") == key), None)
            if entry is not None and entry["status"] == "failed":
                return entry
            if entry is None:
                if sum(item.get("kind") == "page_resolution" for item in state["searches"]) >= self.max_page_resolutions:
                    return None
                entry = {"source": source, "query": query, "lookup_query": page["url"],
                    "kind": "page_resolution", "idempotency_key": key, "status": "started",
                    "discovery_call_id": page["discovery_call_id"]}
                state["searches"].append(entry)
                save()
            if entry["status"] == "started":
                try:
                    media = self._service(self.service_factory,source,project,state)
                    if document is not None:
                        media.provider.use_page_document(page["url"], document)
                    result = media.search(project.id, page["url"], key)
                    entry.update(status="completed", call_id=str(result.call_id),
                        candidates=[str(value.id) for value in result.candidates if self._fits(value, project, group)])
                except Exception as exc:
                    entry.update(status="failed", reason=getattr(exc, "code", "external_page_resolution_failed"))
                save()
            for identity in entry.get("candidates", []):
                if not available(query):
                    break
                try_candidate(entry, identity)
                imported = next((item for item in state["imports"] if item["candidate_id"] == identity and item.get("asset_id")), None)
                if imported:
                    with self.db.transaction():
                        assets = AssetRepository(self.db)
                        asset = assets.get(UUID(imported["asset_id"]))
                        records = [dict(record) for record in asset.metadata.get("external_sources", [])]
                        for record in records:
                            if record.get("candidate", {}).get("id") == identity:
                                record["discovery_call_id"] = page["discovery_call_id"]
                        assets.update(asset.model_copy(update={"metadata": {**asset.metadata, "external_sources": records}}))
            return entry
        for query, group in queries.items():
            if not available(query):
                continue
            web_order=sorted(self.web_sources,key=lambda name: bool(rejected) and any(
                item["source"]==name and item["query"]==query for item in searches))
            for name in web_order:
                if not available(query):
                    break
                if any(item["source"] == name and item.get("source_blocked") for item in searches):
                    continue
                entry = next((item for item in searches if item["source"] == name and item["query"] == query), None)
                if entry is not None and entry["status"] == "failed":
                    continue
                if entry is None:
                    if len(searches) >= self.max_web_searches:
                        continue  # 新连接无额度时，后面的已保存网页结果仍可有界复用。
                    entry = {"source": name, "query": query, "status": "started"}
                    searches.append(entry)
                    save()
                try:
                    web = self._service(self.web_service_factory,name,project,state)
                    found = web.search(project.id, (query[:100] + " 视频素材 CC BY CC0 MP4")[:128],
                        f"material:{job_id}:web:" + hashlib.sha256((name+query).encode()).hexdigest())
                    entry.update(status="completed", call_id=found["call_id"], pages=found["pages"])
                except Exception as exc:
                    entry.update(status="failed", reason=getattr(exc, "code", "external_web_search_failed"),
                        source_blocked=not isinstance(exc, BudgetLimitError))
                    save()
                    if isinstance(exc, BudgetLimitError):
                        return
                    continue
                save()
                for page in entry["pages"]:
                    if not available(query):
                        break
                    if not page.get("can_resolve"):
                        continue
                    page = {**page, "discovery_call_id": entry["call_id"]}
                    document = None
                    if page.get("resolver_provider") == "public_page":
                        call = ProviderCallRepository(self.db).get(UUID(entry["call_id"]))
                        raw = (call.result_payload or {}).get("result", {}).get("document", {}) if call else {}
                        rows = raw.get("results", raw.get("search_results", []))
                        original = next((row for row in rows if row.get("url") == page["url"]), {})
                        body = original.get("content") or original.get("text")
                        if isinstance(body, str) and "<script" in body.lower() and "VideoObject" in body:
                            document = body
                    outcome = resolve(page, query, group, document=document,
                        suffix=":search-body" if document is not None else "")
                    # 只为直接网络读取失败尝试另一已声明的抓取工具；缺许可/元数据不反复抓。
                    reason = (outcome or {}).get("reason", "")
                    if (available(query) and page.get("resolver_provider") == "public_page"
                        and reason in {"public_page_connection_failed", "public_page_http_403"}
                        and getattr(web, "supports_fetch", False)):
                        fetched = next((item for item in fetches if item["url"] == page["url"] and item["source"] == name), None)
                        if fetched is None and len(fetches) < self.max_web_fetches:
                            fetched = {"source": name, "url": page["url"], "status": "started"}
                            fetches.append(fetched)
                            save()
                        if fetched is not None and fetched["status"] in {"started", "completed"}:
                            try:
                                value = web.fetch(project.id, UUID(entry["call_id"]), page["url"],
                                    f"material:{job_id}:fetch:" + hashlib.sha256((name+page["url"]).encode()).hexdigest())
                                body = value.get("content", "")
                                fetched.update(status="completed", call_id=value["call_id"],
                                    structured_html=isinstance(body, str) and "<script" in body.lower() and "VideoObject" in body)
                                if fetched["structured_html"]:
                                    resolve(page, query, group, document=body, suffix=":fetched")
                            except Exception as exc:
                                fetched.update(status="failed", reason=getattr(exc, "code", "external_web_fetch_failed"))
                            save()
                if file_ready(query):
                    break


def analyze_exact_job(db, runner, job_id):
    job = JobRepository(db).get(job_id)
    if job is None:
        raise ExternalAcquisitionError("本地分析任务不存在")
    if job.status is JobStatus.PENDING:
        runner.run_once(job_id=job_id)
        job = JobRepository(db).get(job_id)
    if job.status in {JobStatus.FAILED, JobStatus.CANCELLED}:
        # 用户在普通素材界面手动完成新分析后，沿用其结果；原失败任务不重试。
        row = db.connection.execute("""SELECT 1 FROM asset_job_targets original
            JOIN asset_job_targets replacement ON replacement.asset_id=original.asset_id
            JOIN jobs j ON j.id=replacement.job_id
            WHERE original.job_id=? AND json_extract(j.payload,'$.type')='analyze_asset' AND j.status='completed' LIMIT 1""", (str(job_id),)).fetchone()
        if row:
            return
    if job.status is not JobStatus.COMPLETED:
        raise ExternalAcquisitionError("本地分析尚未完成，未自动重试", "external_analysis_not_completed")
