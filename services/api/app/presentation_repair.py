"""Small deterministic presentation corrections; observations are not approval."""
from __future__ import annotations
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from app.db import (Database, ProjectRepository, ProjectDraftRepository, JobRepository,
    AssetRepository, AudioAssetRepository, ClipRepository, ImageAssetRepository, ProviderCallRepository)
from app.domain.models import (Job, JobStatus, JobType, VideoSpec, RenderVideoJobPayload,
    CandidateAsset, CostCategory, UsageCost)
from app.production_runs import ProductionRunService, ProductionRunConflict, ProductionRunNotReady, _snapshot_sha256
from app.repair_allowance import used_repairs
from app.renderer.remotion import RemotionRenderer
from app.assembly.edit_plan import resolve_edit_plan, _normalized
from app.budget import ProviderCallLedger
from app.presentation_diagnosis import (_entry_document, diagnosis_for_finding, presentation_correction, summarize_diagnoses,
    DETERMINISTIC_PRESENTATION_FINDINGS, PRESENTATION_CORRECTIONS)


def _dependency_facts(values):
    """Usage bookkeeping is not a change to media, QA or the selected interval."""
    return [{key:value for key,value in item.items() if key not in {'used_count','last_used_at'}}
        if 'asset_duration_ms' in item and 'asset_id' in item else item for item in values]


class PresentationObservationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=1, max_length=500)
    render_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scene_id: str = Field(min_length=1, max_length=100)
    finding: Literal["duplicate_text", "known_subtitle_conflict", "text_overlay", "face_out_of_frame", "source_change", "unknown_subtitles",
                     "voice_naturalness", "talking_naturalness", "visible_sync", "semantic_mismatch", "narrative_rhythm"]
    reason: str = Field(min_length=1, max_length=2000, pattern=r"\S")
    evidence_reference: str = Field(min_length=1, max_length=2000, pattern=r"\S")


class PresentationRepairRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    observation_id: UUID
    expected_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=500)
    confirmed_action: Literal["render_revision"]
    reason: str = Field(min_length=1, max_length=2000, pattern=r"\S")


def semantic_observation_diagnosis(db, observation, row, render):
    """只读当前版本的机器证据；不将定位结果升级为修复或人审许可。"""
    if observation['request']['finding'] != 'semantic_mismatch' or 'machine_diagnosis' not in observation:
        return None
    from app.presentation_semantic import semantic_diagnosis_is_current
    machine=observation['machine_diagnosis']
    current=semantic_diagnosis_is_current(db,machine,row,render)
    return dict(responsible_stage=machine.get('responsible_stage','unknown') if current else 'unknown',
        assessment_state=machine.get('assessment_state','unknown') if current else 'unknown',
        required_capability=machine.get('required_capability',['exact_scene_narration_visual_semantics_diagnosis']) if current else ['current_render_semantic_diagnosis'],
        machine_diagnosis={key:machine.get(key) for key in ('source','method','provider','model','call_id','state','limitations')},
        evidence_current=current)


class PresentationRepairService:
    def __init__(self, db: Database, data_root: Path, render_root: Path, *, semantic_diagnoser=None):
        self.db, self.data_root, self.render_root = db, Path(data_root).resolve(), Path(render_root).resolve()
        self.production = ProductionRunService(db, self.data_root)
        self.semantic_diagnoser = semantic_diagnoser

    def _current(self, project, run):
        row = self.db.connection.execute("SELECT * FROM production_runs WHERE id=? AND project_id=?", (str(run), str(project))).fetchone()
        if row is None: raise LookupError("production run not found")
        if row["stage"] != "render" or row["render_job_id"] is None:
            raise ProductionRunNotReady(("presentation_requires_render_stage",))
        job = JobRepository(self.db).get(UUID(row["render_job_id"]))
        if job is None or job.status != JobStatus.COMPLETED or not isinstance(job.payload, RenderVideoJobPayload):
            raise ProductionRunNotReady(("presentation_requires_completed_render",))
        return row, job

    def _hash_render(self, job):
        path = self.render_root / str(job.project_id) / f"{job.payload.render_id}.mp4"
        if not path.resolve().is_relative_to(self.render_root) or not path.is_file():
            raise ProductionRunNotReady(("presentation_render_file_missing",))
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()

    def observe(self, project, run, request: PresentationObservationRequest, *, source="human_observation"):
        if source not in {"human_observation", "deterministic_spec_qa"}:
            raise ValueError("presentation_observation_source_invalid")
        if source == "deterministic_spec_qa" and request.finding not in DETERMINISTIC_PRESENTATION_FINDINGS:
            raise ValueError("presentation_deterministic_finding_unsupported")
        with self.db.transaction(immediate=True):
            old = self.db.connection.execute("SELECT payload FROM presentation_observations WHERE project_id=? AND idempotency_key=?", (str(project), request.idempotency_key)).fetchone()
            if old:
                saved = json.loads(old["payload"])
                if (saved["run_id"] != str(run) or saved["request"] != request.model_dump(mode="json")
                    or saved.get("source", "human_observation") != source):
                    raise ProductionRunConflict("presentation_observation_idempotency_conflict")
                return saved
            _, job = self._current(project, run)
            if self._hash_render(job) != request.render_sha256:
                raise ProductionRunConflict("presentation_observation_render_changed")
            if request.scene_id not in {s.scene_id for s in job.payload.video_spec.scenes}:
                raise ProductionRunConflict("presentation_observation_scene_mismatch")
            record = {"id": str(uuid4()), "project_id": str(project), "run_id": str(run),
                      "render_job_id": str(job.id), "source": source, "request": request.model_dump(mode="json"),
                      "created_at": datetime.now(timezone.utc).isoformat()}
            self.db.connection.execute("INSERT INTO presentation_observations VALUES (?,?,?,?,?,?)", (record["id"], str(project), str(run), str(job.id), request.idempotency_key, json.dumps(record)))
            return record

    def _validate(self, row, spec: VideoSpec):
        draft = ProjectDraftRepository(self.db).get(spec.project_id)
        project = ProjectRepository(self.db).get(spec.project_id)
        if draft is None or project is None or draft.version != row["draft_version"] or draft.script_revision != row["script_revision"]:
            raise ProductionRunNotReady(("presentation_draft_changed",))
        if spec.edit_plan is None or spec.master_narration is None:
            raise ProductionRunNotReady(("presentation_complete_plan_and_master_required",))
        self.production._require_bound_voice_master(row, spec.master_narration.audio_asset_id)
        snapshots, selections, intervals = [], {}, {}
        for entry in spec.edit_plan.scenes:
            selections[entry.scene_plan_id] = CandidateAsset(scene_plan_id=entry.scene_plan_id, source_kind=entry.selected_source_kind,
                asset_id=entry.selected_asset_id, clip_id=entry.selected_clip_id, match_score=1,
                why=["presentation unchanged selection"], estimated_cost=UsageCost(category=CostCategory.RENDER))
            if entry.selected_clip_id:
                clip = ClipRepository(self.db).get(entry.selected_clip_id)
                if clip is None: raise ProductionRunNotReady(("presentation_clip_missing",))
                intervals[clip.id] = (clip.start_ms, clip.end_ms)
                snapshots.append(clip.model_dump(mode="json"))
            asset = None
            if entry.selected_asset_id:
                asset = AssetRepository(self.db).get(entry.selected_asset_id) or ImageAssetRepository(self.db).get(entry.selected_asset_id)
                if asset is None: raise ProductionRunNotReady(("presentation_asset_missing",))
                self.production._require_source_bytes(asset.source_file, asset.content_hash)
                snapshots.append(asset.model_dump(mode="json"))
        master = AudioAssetRepository(self.db).get(spec.master_narration.audio_asset_id)
        if master is None:
            raise ProductionRunNotReady(("presentation_master_missing",))
        from app.assembly.video_spec import _require_generated_voice_qa
        _require_generated_voice_qa(master)
        self.production._require_master_bytes(master.id)
        snapshots.append(master.model_dump(mode="json"))
        resolve_edit_plan(project, draft.scenes, selections, clip_intervals=intervals, supplied=spec.edit_plan)
        entries = {entry.scene_id: entry for entry in spec.edit_plan.scenes}
        if set(entries) != {scene.scene_id for scene in spec.scenes}:
            raise ProductionRunNotReady(("presentation_scene_mapping_changed",))
        for scene in spec.scenes:
            entry = entries[scene.scene_id]
            for field in ("visual_role", "framing_policy", "portrait_presentation", "subtitle_treatment", "graphic_treatment", "graphic_text"):
                if getattr(scene, field) != getattr(entry, field):
                    raise ProductionRunNotReady(("presentation_plan_spec_mismatch",))
            if (scene.visual.source_kind != entry.selected_source_kind or scene.visual.asset_id != entry.selected_asset_id
                or scene.visual.clip_id != entry.selected_clip_id):
                raise ProductionRunNotReady(("presentation_selection_mismatch",))
        renderer = RemotionRenderer(AssetRepository(self.db), ClipRepository(self.db), images=ImageAssetRepository(self.db),
            audios=AudioAssetRepository(self.db), renderer_dir=Path(__file__).resolve().parents[3] / "apps" / "renderer")
        renderer.data_root = self.data_root
        renderer._validate_master_narration(spec, spec.fps)
        for scene in spec.scenes:
            renderer._validate_scene(scene, spec.fps, project_id=spec.project_id,
                master_audio_id=master.id, validate_scene_narration=False)
        return snapshots


    def _review_material_spec(self, row, spec, draft):
        """Refresh the whole work with fixed audio/timing and check current source policies."""
        from app.assembly.edit_plan import review_edit_plan, review_video_spec
        from app.source_usage import SourceUsageService
        from app.source_use_constraints import SourceUseConstraintService, presentation
        usage = SourceUsageService(self.db)
        uses = usage.spec_uses(spec)
        usage.require_available(spec.project_id, uses, run_id=row['id'])
        by_scene = {scene.scene_id:scene for scene in spec.scenes}
        selections, durations = {}, {}
        constraints = SourceUseConstraintService(self.db,self.data_root)
        records = constraints.records(spec.project_id)
        for entry in spec.edit_plan.scenes:
            scene = by_scene[entry.scene_id]
            candidate = CandidateAsset(scene_plan_id=entry.scene_plan_id, source_kind=entry.selected_source_kind,
                asset_id=entry.selected_asset_id, clip_id=entry.selected_clip_id, match_score=1,
                why=['保留本版已核验选择'], estimated_cost=UsageCost(category=CostCategory.RENDER),
                playback_start_ms=scene.visual.clip_start_ms, playback_end_ms=scene.visual.clip_end_ms)
            selections[entry.scene_plan_id] = candidate
            durations[entry.scene_plan_id] = ((scene.start_frame+scene.duration_frames)*1000*spec.fps.denominator+spec.fps.numerator-1)//spec.fps.numerator - (scene.start_frame*1000*spec.fps.denominator+spec.fps.numerator-1)//spec.fps.numerator
            treated = presentation(entry,width=spec.width,height=spec.height,style=spec.edit_plan.style_tokens,
                visual=scene.visual,captions=[caption.model_dump(mode='json') for caption in scene.captions],transition=scene.transition)
            if constraints.evaluate(candidate,treated,records):
                raise ProductionRunNotReady(('presentation_material_use_constraint',))
        assets = AssetRepository(self.db)
        refreshed = review_edit_plan(spec.edit_plan,draft.scenes,selections,durations_ms=durations,
            assets=assets,source_uses=uses,portrait_output=spec.width<spec.height,timing_basis='rendered_timeline')
        spec = review_video_spec(spec.model_copy(update={'edit_plan':refreshed}),assets,source_uses=uses)
        if spec.edit_plan.whole_video.stop_reasons:
            raise ProductionRunNotReady(tuple(spec.edit_plan.whole_video.stop_reasons))
        return spec

    def _material_correction(self, row, old, draft, observed, correction):
        from app.presentation_material import replacement_options
        if old.edit_plan is None:
            correction['reasons'] = ['presentation_scene_mapping_required']
            return
        target = next(scene for scene in draft.scenes if scene.scene_id == observed['request']['scene_id'])
        rejections = {}
        correction['material_candidate_rejections'] = rejections
        options = replacement_options(self.db,self.data_root,old,target,
            style=old.edit_plan.style_tokens,run_id=row['id'],candidate_rejections=rejections)
        accepted = []
        for option in options:
            raw = old.model_dump(mode='json')
            raw['scenes'] = [option['scene'].model_dump(mode='json') if scene['scene_id']==target.scene_id else scene for scene in raw['scenes']]
            raw['edit_plan']['scenes'] = [option['entry'].model_dump(mode='json') if entry['scene_id']==target.scene_id else entry for entry in raw['edit_plan']['scenes']]
            try:
                spec = self._review_material_spec(row,VideoSpec.model_validate(raw),draft)
                self._validate(row,spec)
            except (ValueError,RuntimeError,OSError) as error:
                rejections[str(option['candidate'].clip_id)] = list(getattr(error,'reasons',None) or ['presentation_dependencies_invalid'])
                continue
            whole = spec.edit_plan.whole_video
            rank = (whole.unintentional_repeat_ms,whole.same_source_on_information_change_ms,
                option['occupancy_degree'],not option['creator_provided'],-option['quality_score'],
                -option['match_score'],str(option['candidate'].clip_id))
            accepted.append((rank,option))
        if not accepted:
            correction['reasons'] = ['presentation_material_replacement_unavailable']
            return
        _, option = min(accepted,key=lambda value:value[0])
        correction.update(after=option['scene'].model_dump(mode='json'),entry_after=_entry_document(option['entry']),
            reasons=[],material_replacement={'scene_id':target.scene_id,**option['evidence']})
        correction['diagnosis'].update(required_capability=[])

    def _material_recheck_stop_reason(self,row,job,render_sha256):
        seen=set()
        while True:
            if job.id in seen:return 'material_semantic_recheck_stale'
            seen.add(job.id)
            saved = self.db.connection.execute('SELECT payload FROM presentation_repairs WHERE replacement_job_id=?',(str(job.id),)).fetchone()
            if saved is None:return None
            previous = json.loads(saved[0])
            if previous['plan'].get('material_replacements'):break
            facts=previous['plan']['predecessor_job']
            job=JobRepository(self.db).get(UUID(facts['id']))
            if job is None or job.model_dump(mode='json')!=facts:return 'material_semantic_recheck_stale'
            try:render_sha256=self._hash_render(job)
            except ProductionRunNotReady:return 'material_semantic_recheck_stale'
        from app.presentation_semantic import material_recheck_is_current
        recheck = previous.get('semantic_recheck')
        if recheck is None:return 'material_semantic_recheck_required'
        if recheck['state']!='pass':return 'material_semantic_recheck_not_passed'
        # 派发后继版本时，复核其确切前版依赖，不能把新 Render ID 当作旧证据失效。
        predecessor_row={**dict(row),'render_job_id':str(job.id)}
        if (not material_recheck_is_current(self.db,recheck,predecessor_row,job)
                or recheck['binding']['render_sha256']!=render_sha256):
            return 'material_semantic_recheck_stale'
        return None

    def plan(self, project, run, observation_id):
        row, job = self._current(project, run)
        obsrow = self.db.connection.execute("SELECT payload FROM presentation_observations WHERE id=? AND project_id=? AND run_id=?", (str(observation_id), str(project), str(run))).fetchone()
        if obsrow is None: raise LookupError("presentation observation not found")
        observation = json.loads(obsrow["payload"])
        reasons, changes, inputs = [], [], []
        old = job.payload.video_spec
        candidate = old.model_dump(mode="json")
        item = observation["request"]
        if observation["render_job_id"] != str(job.id) or item["render_sha256"] != self._hash_render(job):
            raise ProductionRunConflict("presentation_observation_stale")
        dependency_reason=self._material_recheck_stop_reason(row,job,item['render_sha256'])
        if dependency_reason:reasons.append(dependency_reason)
        target = next((scene for scene in old.scenes if scene.scene_id == item["scene_id"]), None)
        if target is None:
            raise ProductionRunConflict("presentation_observation_scene_mismatch")
        latest = {}
        for saved in self.db.connection.execute(
            "SELECT payload FROM presentation_observations WHERE project_id=? AND run_id=? AND render_job_id=? ORDER BY rowid DESC",
            (str(project), str(run), str(job.id)),
        ):
            observed = json.loads(saved["payload"])
            latest.setdefault((observed["request"]["scene_id"], observed["request"]["finding"]), observed)
        observations = list(latest.values())
        if any(observed["request"]["render_sha256"] != item["render_sha256"] for observed in observations):
            raise ProductionRunConflict("presentation_observation_stale")
        fps = old.fps
        scopes = [{"scene_id": observed["request"]["scene_id"],
                   "start_ms": scene.start_frame * 1000 * fps.denominator // fps.numerator,
                   "end_ms": ((scene.start_frame + scene.duration_frames) * 1000 * fps.denominator + fps.numerator - 1) // fps.numerator}
                  for observed in observations for scene in old.scenes if scene.scene_id == observed["request"]["scene_id"]]
        if len(scopes) != len(observations):
            raise ProductionRunConflict("presentation_observation_scene_mismatch")
        scope = scopes[0] if len({value["scene_id"] for value in scopes}) == 1 else {"scene_id": "多个局部场景", "scene_ids": list(dict.fromkeys(value["scene_id"] for value in scopes)),
            "start_ms": min(value["start_ms"] for value in scopes), "end_ms": max(value["end_ms"] for value in scopes)}
        bindings = json.loads(row["talking_source_bindings"])
        master_id = None if old.master_narration is None else old.master_narration.audio_asset_id
        master = None if master_id is None else AudioAssetRepository(self.db).get(master_id)
        # These are retained exact artifacts, not an admission to reuse a defective
        # dependency after a future Voice/Timing/Material change.
        preserves = {"render_job_id": str(job.id), "render_sha256": item["render_sha256"],
                     "master_audio_id": None if master_id is None else str(master_id),
                     "master_audio_sha256": None if master is None else master.content_hash,
                     "talking_job_ids": [binding["talking_job_id"] for binding in bindings if binding.get("talking_job_id")],
                     "talking_output_asset_ids": [binding["talking_output_asset_id"] for binding in bindings if binding.get("talking_output_asset_id")],
                     "scene_plan_ids": [str(entry.scene_plan_id) for entry in old.edit_plan.scenes] if old.edit_plan else [],
                     "reuse_admission": "unchanged_dependencies_only"}
        remaining = max(0, row["repair_allowance"] - used_repairs(self.db, run))
        if remaining == 0: reasons.append("run_repair_allowance_exhausted")
        draft = ProjectDraftRepository(self.db).get(project)
        corrections = [presentation_correction(old, observed["request"], draft) for observed in observations]
        semantic_inputs_valid, material_basis_inputs = None, None
        for observed,correction in zip(observations,corrections):
            machine=semantic_observation_diagnosis(self.db,observed,row,job)
            if machine is not None:
                # 状态页只读元数据；读取具体修复方案时重新核对真实文件和依赖。
                if machine['assessment_state']=='known':
                    if semantic_inputs_valid is None:
                        try:
                            material_basis_inputs = self._validate(row,old)
                            semantic_inputs_valid = True
                        except (ValueError,RuntimeError,OSError):
                            semantic_inputs_valid = False
                    if not semantic_inputs_valid:
                        machine.update(assessment_state='unknown',responsible_stage='unknown',
                            evidence_current=False,required_capability=['current_render_semantic_diagnosis'])
                correction['diagnosis']=machine
                correction['reasons']=['presentation_semantic_scoped_correction_required' if machine['assessment_state']=='known'
                    else 'presentation_semantic_diagnosis_unknown']
                if machine['assessment_state']=='known' and machine['responsible_stage']=='material' and remaining:
                    if len(observations)==1:
                        self._material_correction(row,old,draft,observed,correction)
                    else:
                        correction['reasons']=['presentation_joint_material_repair_not_supported']
        diagnoses = [{"observation_id": observed["id"], "finding": observed["request"]["finding"],
                      "scope": observed_scope, **correction["diagnosis"]}
                     for observed, observed_scope, correction in zip(observations, scopes, corrections)]
        for correction in corrections:
            reasons.extend(correction["reasons"])
        diagnosis = summarize_diagnoses(diagnoses)
        known = diagnosis['assessment_state']=='known'
        if known and not reasons:
            # 每项从原始规格提取有限字段差异，再合并；不把前一项修改当作后一项诊断依据。
            by_scene = {value["scene_id"]: value for value in candidate["scenes"]}
            by_entry = {value["scene_id"]: value for value in candidate["edit_plan"]["scenes"]}
            assigned = {}
            for correction in corrections:
                for label, before, after, target_values in (("scene", correction["before"], correction["after"], by_scene[correction["scene_id"]]),
                    ("entry", correction["entry_before"], correction["entry_after"], by_entry[correction["scene_id"]])):
                    for field, value in after.items():
                        if before[field] == value: continue
                        key = (correction["scene_id"], label, field)
                        if key in assigned and assigned[key] != value:
                            reasons.append("presentation_joint_correction_conflict")
                        assigned[key] = value
                        target_values[field] = value
            for scene_id in dict.fromkeys(value["scene_id"] for value in corrections):
                before = next(value["before"] for value in corrections if value["scene_id"] == scene_id)
                scene = by_scene[scene_id]
                # 清除重复文字已解决遮挡，不再为已移除的文字改变人物版式。
                if scene["graphic_text"] is None and any(value["scope"]["scene_id"] == scene_id and value["finding"] == "text_overlay" for value in diagnoses):
                    scene["portrait_presentation"] = by_entry[scene_id]["portrait_presentation"] = before["portrait_presentation"]
                if scene != before:
                    changes.append({"scene_id": scene_id, "before": before, "after": dict(scene)})
        if reasons:
            changes = []  # 不提供部分可执行方案，不换版丢失未解决的问题。
        action = "render_revision" if changes and not reasons else "scoped_fix_required"
        if remaining == 0: action = "stop"
        new_spec = None
        if changes:
            try:
                new_spec = VideoSpec.model_validate(candidate)
                if any(correction.get('material_replacement') for correction in corrections):
                    new_spec = self._review_material_spec(row,new_spec,draft)
                inputs = self._validate(row, new_spec)
            except (ValueError, RuntimeError) as exc:
                reasons.extend(getattr(exc, "reasons", ("presentation_input_or_preflight_changed",)))
                action = "stop"
        material_replacements = [correction['material_replacement'] for correction in corrections if correction.get('material_replacement')]
        budgets = []
        if action == "render_revision":
            from app.db import BudgetPolicyRepository
            from app.budget import enforce_reservation, BudgetLimitError, snapshot
            repo = ProviderCallRepository(self.db)
            for budget_scope in (None, project):
                policy = BudgetPolicyRepository(self.db).get_by_project(budget_scope)
                budgets.append(None if policy is None else policy.model_dump(mode="json"))
                if policy:
                    calls = repo.list_all() if budget_scope is None else repo.list_for_project(project)
                    try:
                        enforce_reservation(policy, calls, UsageCost(category=CostCategory.RENDER, amount=Decimal("0"), currency="USD"), ignore_existing_unknown_cost=True)
                        if material_replacements and policy.max_calls is not None and snapshot(calls,policy.currency).calls+2>policy.max_calls:
                            raise BudgetLimitError('material_recheck_tail_budget','换材须留出渲染及实际画面复验调用')
                    except BudgetLimitError: reasons.append("presentation_render_budget_blocked"); action = "stop"
            if material_replacements and self.semantic_diagnoser is not None:
                admission = self.semantic_diagnoser.material_recheck_admission(project)
                if admission['state'] != 'ready':
                    reasons.extend(admission['stop_reasons']);action='stop'
        values = {"project_id": str(project), "run_id": str(run), "observation": observation, "observations": observations, "diagnoses": diagnoses,
                  **diagnosis, "scope": scope, "preserves": preserves,
                  "action": action, "stop_reasons": reasons, "changes": changes, "remaining_run_repairs": remaining,
                  "predecessor_job": job.model_dump(mode="json"), "candidate_spec": None if new_spec is None else new_spec.model_dump(mode="json"),
                  "inputs": inputs, "budgets": budgets, "max_local_render_calls": 1, "voice_calls": 0, "talking_calls": 0,
                  "external_charge_ceiling": "0", "local_compute_cost": None, "user_active_minutes": None,
                  "review_scope": "new_output_technical_qa_and_final_u_product"}
        if material_replacements:
            from app.source_use_constraints import SourceUseConstraintService
            values.update(material_replacements=material_replacements, material_basis_inputs=material_basis_inputs,
                source_use_constraint_context=SourceUseConstraintService(self.db,self.data_root).context(project),
                semantic_recheck_calls=1, external_charge_ceiling='unknown',
                review_scope='repaired_scene_semantic_recheck_then_final_u_product')
        if any('material_candidate_rejections' in correction for correction in corrections):
            values['material_candidate_rejections'] = {correction['scene_id']:correction['material_candidate_rejections']
                for correction in corrections if 'material_candidate_rejections' in correction}
        return {**values, "fingerprint": _snapshot_sha256(values)}

    def apply(self, project, run, request: PresentationRepairRequest, *, admission_guard=None):
        with self.db.transaction(immediate=True):
            digest = _snapshot_sha256(request.model_dump(mode="json"))
            old = self.db.connection.execute("SELECT * FROM presentation_repairs WHERE project_id=? AND idempotency_key=?", (str(project), request.idempotency_key)).fetchone()
            if old:
                if old["run_id"] != str(run) or old["request_fingerprint"] != digest: raise ProductionRunConflict("presentation_repair_idempotency_conflict")
                return json.loads(old["payload"])
            if admission_guard is not None:
                admission_guard()
            plan = self.plan(project, run, request.observation_id)
            if plan["fingerprint"] != request.expected_fingerprint: raise ProductionRunConflict("presentation_repair_plan_stale")
            if plan["action"] != "render_revision": raise ProductionRunNotReady(tuple(plan["stop_reasons"]))
            now, repair_id = datetime.now(timezone.utc), uuid4()
            job = Job(project_id=project, type=JobType.RENDER, idempotency_key=f"presentation-repair:{repair_id}",
                payload=RenderVideoJobPayload(project_id=project, render_id=uuid4(), video_spec=VideoSpec.model_validate(plan["candidate_spec"])), created_at=now, updated_at=now)
            JobRepository(self.db).create(job)
            record = {"id": str(repair_id), "project_id": str(project), "run_id": str(run), "reason": request.reason,
                      "plan": plan, "replacement_job_id": str(job.id), "created_at": now.isoformat()}
            self.db.connection.execute("INSERT INTO presentation_repairs(id,project_id,run_id,idempotency_key,request_fingerprint,replacement_job_id,payload) VALUES (?,?,?,?,?,?,?)", (str(repair_id), str(project), str(run), request.idempotency_key, digest, str(job.id), json.dumps(record)))
            self.db.connection.execute("UPDATE production_runs SET render_job_id=? WHERE id=? AND stage='render'", (str(job.id), str(run)))
            if plan.get('material_replacements'):
                from app.source_usage import SourceUsageService
                from app.source_use_constraints import SourceUseConstraintService
                usage = SourceUsageService(self.db)
                usage.save(project,run,usage.spec_uses(job.payload.video_spec))
                SourceUseConstraintService(self.db,self.data_root).snapshot_render(project,job.id)
            return record

    def history(self, project, run):
        if self.production.get(project, run) is None: raise LookupError("production run not found")
        result = []
        for row in self.db.connection.execute("SELECT * FROM presentation_repairs WHERE project_id=? AND run_id=? ORDER BY rowid", (str(project), str(run))):
            record = json.loads(row["payload"]); job = JobRepository(self.db).get(UUID(row["replacement_job_id"]))
            technical = None if row["technical_qa"] is None else json.loads(row["technical_qa"])
            if technical is not None:
                try:
                    if job is None or technical["sha256"] != self._hash_render(job):
                        technical = {**technical, "state": "stale", "reason": "output_bytes_changed"}
                except ProductionRunNotReady:
                    technical = {**technical, "state": "stale", "reason": "output_file_missing"}
            record.update(status=None if job is None else job.status.value, final_review_state="not_submitted",
                technical_qa=technical,
                provider_calls=[c.model_dump(mode="json") for c in ProviderCallRepository(self.db).list_for_project(project) if c.idempotency_key.startswith(f"presentation-repair:{row['id']}:render")])
            machine = record.get('semantic_recheck')
            if machine is not None:
                from app.presentation_semantic import material_recheck_is_current
                current = self.db.connection.execute('SELECT * FROM production_runs WHERE id=?',(str(run),)).fetchone()
                record['semantic_recheck'] = {**machine,'evidence_current':bool(current and material_recheck_is_current(self.db,machine,current,job))}
            result.append(record)
        return result

    def require_current_repair(self, job):
        row = self.db.connection.execute("SELECT payload FROM presentation_repairs WHERE replacement_job_id=?", (str(job.id),)).fetchone()
        if row is None: return None
        record = json.loads(row["payload"])
        current = self.db.connection.execute("SELECT * FROM production_runs WHERE id=?", (record["run_id"],)).fetchone()
        if (current is None or current["stage"] != "render" or current["render_job_id"] != str(job.id)
            or job.project_id != UUID(record["project_id"]) or job.payload.video_spec.model_dump(mode="json") != record["plan"]["candidate_spec"]):
            raise ProductionRunNotReady(("presentation_repair_no_longer_current",))
        predecessor = JobRepository(self.db).get(UUID(record["plan"]["predecessor_job"]["id"]))
        if predecessor is None or predecessor.model_dump(mode="json") != record["plan"]["predecessor_job"]:
            raise ProductionRunNotReady(("presentation_predecessor_job_changed",))
        predecessor_sha256=self._hash_render(predecessor)
        if predecessor_sha256 != record["plan"]["observation"]["request"]["render_sha256"]:
            raise ProductionRunNotReady(("presentation_predecessor_bytes_changed",))
        dependency_reason=self._material_recheck_stop_reason(current,predecessor,predecessor_sha256)
        if dependency_reason:raise ProductionRunNotReady((dependency_reason,))
        if record['plan'].get('material_replacements'):
            if _dependency_facts(self._validate(current,predecessor.payload.video_spec)) != _dependency_facts(record['plan']['material_basis_inputs']):
                raise ProductionRunNotReady(('presentation_material_basis_changed',))
            latest = {}
            for observed in self.db.connection.execute(
                    'SELECT payload FROM presentation_observations WHERE run_id=? AND render_job_id=? ORDER BY rowid DESC',
                    (record['run_id'],str(predecessor.id))):
                saved = json.loads(observed[0])
                latest.setdefault((saved['request']['scene_id'],saved['request']['finding']),saved)
            if list(latest.values()) != record['plan']['observations']:
                raise ProductionRunNotReady(('presentation_material_observations_changed',))
            from app.source_use_constraints import SourceUseConstraintService
            constraints = SourceUseConstraintService(self.db,self.data_root)
            if constraints.context(job.project_id) != record['plan']['source_use_constraint_context']:
                raise ProductionRunNotReady(('presentation_material_policy_changed',))
        if _dependency_facts(self._validate(current, job.payload.video_spec)) != _dependency_facts(record["plan"]["inputs"]):
            raise ProductionRunNotReady(("presentation_dependencies_changed",))
        return record

    def reserve_render(self, job):
        def require_recheck(record):
            if not record.get('plan',{}).get('material_replacements'):return
            if self.semantic_diagnoser is None:
                raise ProductionRunNotReady(('material_semantic_recheck_not_configured',))
            admission = self.semantic_diagnoser.material_recheck_admission(job.project_id)
            if admission['state'] != 'ready':
                raise ProductionRunNotReady(tuple(admission['stop_reasons']))
        def guard():
            record = self.require_current_repair(job)
            require_recheck(record)
            prefix = f"presentation-repair:{record['id']}:render"
            if any(c.idempotency_key.startswith(prefix) for c in ProviderCallRepository(self.db).list_for_project(job.project_id)):
                raise ProductionRunNotReady(("presentation_render_call_limit",))
        record = self.require_current_repair(job)
        if record is None: return None
        require_recheck(record)
        reservation = ProviderCallLedger(self.db).reserve_execution(project_id=job.project_id,
            idempotency_key=f"presentation-repair:{record['id']}:render:{job.attempt}", operation="render", mode="runtime",
            provider="local-render", model="remotion-ffmpeg", input_source=f"render:{job.payload.render_id}",
            input_digest=_snapshot_sha256(job.payload.model_dump(mode="json")),
            estimated_cost=UsageCost(category=CostCategory.RENDER, amount=Decimal("0"), currency="USD", note="zero external charge; local compute unknown"),
            allow_existing_unknown_cost=True, reservation_guard=guard)
        if not reservation.owner: raise ProductionRunNotReady(("presentation_render_recovery_required",))
        return reservation.record

    def verify_output(self, job):
        """Measured technical evidence only; never creates a U-Product approval."""
        self._hash_render(job)
        output = self.render_root / str(job.project_id) / f"{job.payload.render_id}.mp4"
        evidence = measure_render_output(output,job.payload.video_spec)
        with self.db.transaction(immediate=True):
            self.require_current_repair(job)
            self.db.connection.execute("UPDATE presentation_repairs SET technical_qa=? WHERE replacement_job_id=? AND technical_qa IS NULL",
                (json.dumps(evidence), str(job.id)))
        return evidence


def measure_render_output(output, spec, *, frame_range=None):
    """Shared probe/full-decode measurement; writes no admission or review."""
    from app.media.ffprobe import FFProbeAdapter
    from app.runtime import resolve_local_executable
    probe = FFProbeAdapter(command=resolve_local_executable("ffprobe")).probe(output)
    frames = max(scene.start_frame + scene.duration_frames for scene in spec.scenes)
    if frame_range is not None:
        if not 0 <= frame_range[0] < frame_range[1] <= frames:
            raise ProductionRunNotReady(("presentation_preview_interval_invalid",))
        frames = frame_range[1]-frame_range[0]
    expected_ms = frames * 1000 * spec.fps.denominator / spec.fps.numerator
    frame_ms = 1000 * spec.fps.denominator / spec.fps.numerator
    video = next(stream for stream in probe.metadata["streams"] if stream.get("codec_type") == "video")
    audio = next((stream for stream in probe.metadata["streams"] if stream.get("codec_type") == "audio"),None)
    video_ms = float(video.get("duration","nan"))*1000
    audio_ms = float(audio.get("duration","nan"))*1000 if audio else float("nan")
    audio_padding_ms = max(frame_ms,3*1024*1000/int(audio["sample_rate"])) if audio and audio.get("codec_name") == "aac" else frame_ms
    if (probe.width != spec.width or probe.height != spec.height or probe.fps != spec.fps
        or not probe.has_audio or int(video.get("nb_frames",-1)) != frames
        or not abs(video_ms-expected_ms) <= frame_ms
        or not expected_ms-frame_ms <= audio_ms <= expected_ms+audio_padding_ms
        or not expected_ms-frame_ms <= probe.duration_ms <= expected_ms+audio_padding_ms+1):
        raise ProductionRunNotReady(("presentation_output_technical_qa_failed",))
    decoded = subprocess.run([resolve_local_executable("ffmpeg"),"-v","error","-xerror","-i",str(output),
        "-c:v","rawvideo","-c:a","pcm_s16le","-f","null","-"],shell=False,capture_output=True,timeout=60,check=False)
    if decoded.returncode:
        raise ProductionRunNotReady(("presentation_output_decode_failed",))
    with Path(output).open("rb") as stream:
        sha = hashlib.file_digest(stream,"sha256").hexdigest()
    result = dict(state="verified",sha256=sha,width=probe.width,height=probe.height,duration_ms=probe.duration_ms,
        fps=probe.fps.model_dump(mode="json"),has_audio=True,video_duration_ms=video_ms,video_frames=frames,
        audio_duration_ms=audio_ms,audio_padding_tolerance_ms=audio_padding_ms,decode="pass",scope="technical_only_final_u_product_required")
    if frame_range is not None:
        result["preview_frame_range"] = list(frame_range)
    return result
