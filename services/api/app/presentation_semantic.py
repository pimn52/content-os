"""Once-only, source-bound visual diagnosis of one existing render observation."""
from __future__ import annotations

import hashlib
import json
import math
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from app.budget import BudgetLimitError, ProviderCallLedger, enforce_reservation, provider_call_input_digest
from app.costs import UnknownProviderCostEstimator
from app.db import (AssetRepository, AudioAssetRepository, BudgetPolicyRepository, ClipRepository, JobRepository,
                    ProjectDraftRepository, ProviderCallRepository)
from app.domain.models import Asset, CostCategory, JobStatus, JobType, SourceKind
from app.presentation_diagnosis import build_semantic_comparison_scope, classify_material_recheck, classify_semantic_comparison
from app.presentation_repair import PresentationRepairService
from app.production_runs import ProductionRunConflict, ProductionRunNotReady, _snapshot_sha256
from app.provider_execution import (ProviderExecutionService, ProviderExecutionIdempotencyConflict,
    ProviderExecutionInProgress, ProviderExecutionRecordedFailure, runtime_provider_identity)
from app.providers.vision import VideoIntervalBatch, VisionTimeout
from app.source_usage import SourceUsageService


def _unknown(reason):
    return dict(state="unknown", assessment_state="unknown", responsible_stage="unknown", stop_reasons=[reason],
                required_capability=["exact_scene_narration_visual_semantics_diagnosis"])


def _run_identity(row):
    return {key: row[key] for key in ("id", "project_id", "stage", "render_job_id", "draft_version",
            "script_revision", "voice_audio_id", "talking_master_audio_id", "talking_source_bindings",
            "automatic", "start_job_id")}


def semantic_diagnosis_is_current(db, machine, row, render=None):
    """Check saved metadata only; status polling must not hash or decode media."""
    try:
        # 停止返工不会改变已观察到的源画面；执行资格在实际调用边界另验。
        if row["stage"] != "render" or json.loads(row["voice_qa_auto_stop_reasons"]):
            return False
        if row["automatic"]:
            parent = JobRepository(db).get(UUID(row["start_job_id"])) if row["start_job_id"] else None
            if (parent is None or parent.type is not JobType.PLAN_CONTENT
                    or str(parent.project_id) != row["project_id"]
                    or parent.status in {JobStatus.FAILED, JobStatus.CANCELLED}):
                return False
        binding = machine["binding"]
        if machine.get("stale") or binding["run"] != _run_identity(row):
            return False
        render = render or JobRepository(db).get(UUID(row["render_job_id"]))
        if (render is None or render.status is not JobStatus.COMPLETED or render.type is not JobType.RENDER
                or str(render.id) != binding["render_job_id"]
                or _snapshot_sha256(render.payload.video_spec.model_dump(mode="json")) != binding["video_spec_sha256"]):
            return False
        draft = ProjectDraftRepository(db).get(render.project_id)
        if draft is None or _snapshot_sha256(draft.model_dump(mode="json")) != binding["draft_sha256"]:
            return False
        for name, repository in (("master", AudioAssetRepository(db)), ("asset", AssetRepository(db)),
                                 ("clip", ClipRepository(db))):
            if binding[name + "_id"] is None and binding[name + "_sha256"] is None:
                continue
            value = repository.get(UUID(binding[name + "_id"]))
            if value is None or _snapshot_sha256(value.model_dump(mode="json")) != binding[name + "_sha256"]:
                return False
        observation = db.connection.execute("SELECT payload FROM presentation_observations WHERE id=?",
                                            (binding["observation_id"],)).fetchone()
        if observation is None:
            return False
        saved = json.loads(observation[0])
        if _snapshot_sha256({key: saved[key] for key in ("id", "source", "request", "render_job_id")}) != binding["observation_sha256"]:
            return False
        latest = db.connection.execute("""SELECT id FROM presentation_observations
            WHERE run_id=? AND render_job_id=? AND json_extract(payload,'$.request.scene_id')=?
            AND json_extract(payload,'$.request.finding')=? ORDER BY rowid DESC LIMIT 1""",
            (row["id"], row["render_job_id"], saved["request"]["scene_id"], "semantic_mismatch")).fetchone()
        usage = SourceUsageService(db)
        return (latest is not None and latest[0] == saved["id"] and usage.review(row,exact_render=True)["state"] != "approved"
                and usage.choice(render.project_id, render.payload.render_id) != "discarded")
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def _recheck_clip_facts(clip):
    facts = clip.model_dump(mode="json")
    for key in ("used_count", "last_used_at"):
        facts.pop(key, None)
    return facts


def material_recheck_is_current(db, machine, row, render=None):
    """Read metadata and the fixed receipt only; never hash or decode media."""
    try:
        from app.video_production import IN_PROGRESS_REASONS
        from app.media.semantic_rejections import source_view_uses
        binding = machine["binding"]
        if (machine.get("stale") or row["stage"] != "render" or binding["run"] != _run_identity(row)
                or json.loads(row["voice_qa_auto_stop_reasons"])
                or any(not reason.startswith(IN_PROGRESS_REASONS) for reason in json.loads(row["waiting_stop_reasons"]))):
            return False
        if row["automatic"]:
            parent = JobRepository(db).get(UUID(row["start_job_id"])) if row["start_job_id"] else None
            if (parent is None or parent.type is not JobType.PLAN_CONTENT or str(parent.project_id) != row["project_id"]
                    or parent.status in {JobStatus.FAILED, JobStatus.CANCELLED}):
                return False
        render = render or JobRepository(db).get(UUID(row["render_job_id"]))
        if (render is None or render.type is not JobType.RENDER or render.status is not JobStatus.COMPLETED
                or str(render.id) != binding["render_job_id"] or str(render.payload.render_id) != binding["render_id"]
                or _snapshot_sha256(render.payload.video_spec.model_dump(mode="json")) != binding["video_spec_sha256"]):
            return False
        saved = db.connection.execute("SELECT payload,technical_qa FROM presentation_repairs WHERE id=? AND project_id=? AND run_id=? AND replacement_job_id=?",
            (binding["repair_id"], row["project_id"], row["id"], str(render.id))).fetchone()
        if saved is None or saved["technical_qa"] is None:
            return False
        repair, technical = json.loads(saved["payload"]), json.loads(saved["technical_qa"])
        if (repair["plan"]["fingerprint"] != binding["repair_plan_fingerprint"]
                or _snapshot_sha256(repair["plan"]) != binding["repair_plan_sha256"]
                or _snapshot_sha256(technical) != binding["technical_qa_sha256"]
                or technical.get("state") != "verified" or technical.get("decode") != "pass"
                or technical.get("sha256") != binding["render_sha256"]):
            return False
        draft = ProjectDraftRepository(db).get(render.project_id)
        if (draft is None or draft.version != row["draft_version"] or draft.script_revision != row["script_revision"]
                or _snapshot_sha256(draft.model_dump(mode="json")) != binding["draft_sha256"]):
            return False
        for name, repository in (("master", AudioAssetRepository(db)), ("asset", AssetRepository(db)), ("clip", ClipRepository(db))):
            value = repository.get(UUID(binding[name + "_id"]))
            if value is None:
                return False
            facts = _recheck_clip_facts(value) if name == "clip" else value.model_dump(mode="json")
            if _snapshot_sha256(facts) != binding[name + "_sha256"]:
                return False
        scope = machine["scope"]
        source = scope["source"]
        if source_view_uses(db, source["source_asset_id"], source["start_ms"], source["end_ms"]) != source["view_source_uses"]:
            return False
        if machine.get("call_id") is not None:
            call = ProviderCallRepository(db).get(UUID(machine["call_id"]))
            document = machine["input"]
            if (call is None or call.project_id != render.project_id or call.mode != "runtime" or call.operation != "vision"
                    or call.idempotency_key != f"presentation-semantic:{row['id']}:{render.id}:repair:{repair['id']}"
                    or call.input_source != f"presentation-repair:{repair['id']}"
                    or call.provider != machine["provider"] or call.model != machine["model"]
                    or document["binding"] != binding or document["scope"] != scope
                    or call.input_digest != provider_call_input_digest(dict(project_id=row["project_id"], operation="vision",
                        provider=call.provider, model=call.model, input_source=call.input_source, input=document))):
                return False
            if machine.get("raw_result") is not None:
                if call.status != "completed" or call.result_payload != {"result": machine["raw_result"]}:
                    return False
                verdict = classify_material_recheck(scope, VideoIntervalBatch.model_validate(machine["raw_result"]["batch"]))
                if machine["raw_result"].get("validation_issues"):
                    verdict = _unknown("semantic_provider_fields_invalid")
                if any(machine.get(key) != verdict[key] for key in ("state", "assessment_state", "responsible_stage")):
                    return False
            elif machine.get("state") == "pass" or call.status != machine.get("call_status"):
                return False
        elif machine.get("state") == "pass":
            return False
        return True
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


class PresentationSemanticDiagnosis:
    def __init__(self, db, root, provider, extractor, probe, cost_estimator=None):
        self.db, self.root = db, Path(root)
        self.provider, self.extractor, self.probe = provider, extractor, probe
        estimator = getattr(provider, "cost_estimator", None)
        self.cost_estimator = cost_estimator or (estimator() if callable(estimator) else UnknownProviderCostEstimator())
        self.presentation = PresentationRepairService(db, self.root, self.root / "renders")

    @staticmethod
    def _require_active(row):
        from app.video_production import IN_PROGRESS_REASONS
        if any(not reason.startswith(IN_PROGRESS_REASONS) for reason in json.loads(row['waiting_stop_reasons'])):
            raise ProductionRunNotReady(('semantic_production_stopped',))

    def material_recheck_admission(self, project_id, *, remaining_render_calls=1):
        """Observe capability, visual price and the Render-plus-recheck budget tail."""
        result = dict(state="unknown", stop_reasons=[], required_capability=["configured_video_comparison_provider"],
                      provider=None, model=None, estimated_cost=None, required_calls=None)
        identity = runtime_provider_identity(self.provider)
        if (identity is None or getattr(self.provider, "supports_video_input", None) is not True
                or getattr(self.provider, "protocol", None) != "chat_completions"
                or not callable(getattr(self.provider, "analyze_intervals", None))):
            return {**result, "stop_reasons": ["semantic_video_provider_unavailable"]}
        result.update(provider=identity.provider, model=identity.model, required_capability=[])
        try:
            project = UUID(str(project_id))
            if type(remaining_render_calls) is not int or remaining_render_calls < 0:
                raise ProductionRunNotReady(("semantic_recheck_tail_invalid",))
            estimate = self.cost_estimator.estimate(operation="vision", provider=identity.provider, model=identity.model,
                input_source=f"presentation-material-recheck:{project}", category=CostCategory.LLM)
            result.update(estimated_cost=estimate.model_dump(mode="json"), required_calls=remaining_render_calls+1)
            calls = ProviderCallRepository(self.db)
            for scope in (None, project):
                policy = BudgetPolicyRepository(self.db).get_by_project(scope)
                if policy is None:
                    continue
                current = enforce_reservation(policy, calls.list_all() if scope is None else calls.list_for_project(project), estimate)
                if policy.max_calls is not None and current.calls+remaining_render_calls+1 > policy.max_calls:
                    raise BudgetLimitError("call_limit", "Render and material recheck exceed the remaining call budget")
            return {**result, "state": "ready"}
        except (ValueError, RuntimeError) as error:
            reasons = getattr(error, "reasons", None)
            code = "semantic_budget_"+error.code if isinstance(error, BudgetLimitError) else reasons[0] if reasons else "semantic_recheck_admission_failed"
            return {**result, "stop_reasons": [code]}

    def _material_repair(self, project, run, repair_id):
        row = self.db.connection.execute("SELECT payload,technical_qa FROM presentation_repairs WHERE id=? AND project_id=? AND run_id=?",
            (str(repair_id), str(project), str(run))).fetchone()
        if row is None:
            raise LookupError("presentation repair not found")
        return json.loads(row["payload"]), None if row["technical_qa"] is None else json.loads(row["technical_qa"])

    def _save_material_recheck(self, repair, machine):
        with nullcontext() if self.db.connection.in_transaction else self.db.transaction(immediate=True):
            saved, _ = self._material_repair(repair["project_id"], repair["run_id"], repair["id"])
            if saved["plan"] != repair["plan"] or saved["replacement_job_id"] != repair["replacement_job_id"]:
                machine = {**machine, **_unknown("semantic_recheck_inputs_changed"), "stale": True}
            saved["semantic_recheck"] = machine
            self.db.connection.execute("UPDATE presentation_repairs SET payload=? WHERE id=?",
                (json.dumps(saved, ensure_ascii=False), repair["id"]))
        return machine

    def _require_material_recheck_current(self, machine, project, run):
        row, render = self.presentation._current(project, run)
        if not material_recheck_is_current(self.db, machine, row, render):
            raise ProductionRunNotReady(("semantic_recheck_inputs_changed",))
        repair = self.presentation.require_current_repair(render)
        if repair is None or repair["id"] != machine["binding"]["repair_id"]:
            raise ProductionRunNotReady(("semantic_recheck_repair_changed",))
        self.presentation._validate(row, render.payload.video_spec)
        if self.presentation._hash_render(render) != machine["binding"]["render_sha256"]:
            raise ProductionRunNotReady(("semantic_recheck_render_bytes_changed",))
        return row, render

    def verify_material_repair(self, project_id, run_id, repair_id):
        """One actual source/render comparison after this material repair's technical QA."""
        project, run, repair_id = UUID(str(project_id)), UUID(str(run_id)), UUID(str(repair_id))
        repair, technical = self._material_repair(project, run, repair_id)
        prior = repair.get("semantic_recheck")
        if prior is not None:
            if prior.get("binding") and prior.get("scope"):
                try:
                    self._require_material_recheck_current(prior, project, run)
                except (ValueError, RuntimeError, OSError):
                    prior = {**prior, **_unknown("semantic_recheck_inputs_changed"), "stale": True}
                    self._save_material_recheck(repair, prior)
            return prior
        machine = dict(source="machine_semantic_comparison", method="same_scene_render_source_video_comparison",
            state="unknown", assessment_state="unknown", responsible_stage="unknown", required_capability=[],
            limitations=["audio_not_observed", "naturalness_and_visible_sync_not_assessed", "human_quality_review_required",
                         "only_replaced_scene_visual_semantics_rechecked", "diagnosis_does_not_authorize_repair"],
            created_at=datetime.now(timezone.utc).isoformat(), scope=None, binding=None, input=None, raw_result=None,
            call_id=None, stale=False, stop_reasons=[])
        key = prefix = None
        try:
            row, render = self.presentation._current(project, run)
            usage=SourceUsageService(self.db)
            if (usage.review(row,exact_render=True)['state'] in {'approved','rejected'}
                    or usage.choice(project,render.payload.render_id)=='discarded'):
                raise ProductionRunNotReady(('semantic_recheck_output_retired',))
            if str(render.id) != repair["replacement_job_id"] or self.presentation.require_current_repair(render) is None:
                raise ProductionRunNotReady(("semantic_recheck_repair_changed",))
            replacements = repair["plan"].get("material_replacements", [])
            if len(replacements) != 1:
                raise ProductionRunNotReady(("semantic_recheck_single_material_scene_required",))
            digest = self.presentation._hash_render(render)
            if (not isinstance(technical, dict) or technical.get("state") != "verified"
                    or technical.get("decode") != "pass" or technical.get("sha256") != digest):
                raise ProductionRunNotReady(("semantic_recheck_technical_qa_required",))
            self.presentation._validate(row, render.payload.video_spec)
            spec = render.payload.video_spec
            draft = ProjectDraftRepository(self.db).get(project)
            plan = next((scene for scene in draft.scenes if scene.scene_id == replacements[0]["scene_id"]), None)
            target = next((scene for scene in spec.scenes if scene.scene_id == replacements[0]["scene_id"]), None)
            if plan is None or target is None or spec.master_narration is None:
                raise ProductionRunNotReady(("semantic_recheck_scene_missing",))
            asset, clip = AssetRepository(self.db).get(target.visual.asset_id), ClipRepository(self.db).get(target.visual.clip_id)
            master = AudioAssetRepository(self.db).get(spec.master_narration.audio_asset_id)
            from app.assembly.video_spec import _master_narration_intervals
            from app.voice_boundary_alignment import scene_narration_segment_spans
            ordered = sorted(draft.scenes, key=lambda scene: scene.order)
            if scene_narration_segment_spans(master, ordered) is None:
                raise ProductionRunNotReady(("semantic_narration_text_interval_unverified",))
            timed = _master_narration_intervals(master, ordered, end_ms=spec.master_narration.end_ms)
            interval = next(((start, end) for scene, start, end in timed if scene.id == plan.id), None)
            scope = build_semantic_comparison_scope(spec, target.scene_id, plan, asset, clip,
                master_verified=True, verified_scene_interval=interval)
            machine["scope"] = scope
            if scope["state"] != "ready":
                return self._save_material_recheck(repair, {**machine, **_unknown(scope["stop_reasons"][0])})
            from app.media.semantic_rejections import source_view_uses
            source = scope["source"]
            source["view_source_uses"] = source_view_uses(self.db, asset.id, source["start_ms"], source["end_ms"])
            machine["binding"] = dict(run=_run_identity(row), repair_id=str(repair_id),
                repair_plan_fingerprint=repair["plan"]["fingerprint"], repair_plan_sha256=_snapshot_sha256(repair["plan"]),
                technical_qa_sha256=_snapshot_sha256(technical), render_job_id=str(render.id), render_id=str(render.payload.render_id),
                render_sha256=digest, video_spec_sha256=_snapshot_sha256(spec.model_dump(mode="json")),
                draft_sha256=_snapshot_sha256(draft.model_dump(mode="json")), master_id=str(master.id),
                master_sha256=_snapshot_sha256(master.model_dump(mode="json")), master_content_hash=master.content_hash,
                asset_id=str(asset.id), asset_content_hash=asset.content_hash, asset_sha256=_snapshot_sha256(asset.model_dump(mode="json")),
                clip_id=str(clip.id), clip_sha256=_snapshot_sha256(_recheck_clip_facts(clip)))
            self._require_material_recheck_current(machine, project, run)
            identity = runtime_provider_identity(self.provider)
            prefix = f"presentation-semantic:{run}:{render.id}:"
            key = prefix+f"repair:{repair_id}"
            existing = ProviderCallRepository(self.db).get_by_idempotency_key(key)
            if existing is not None:
                if (identity is None or existing.project_id != project or existing.operation != "vision" or existing.mode != "runtime"
                        or existing.provider != identity.provider or existing.model != identity.model
                        or existing.input_source != f"presentation-repair:{repair_id}"):
                    raise ProviderExecutionIdempotencyConflict("material recheck provider identity changed")
                machine.update(provider=identity.provider, model=identity.model)
                if existing.status in {"reserved", "running"}:
                    raise ProviderExecutionInProgress("material recheck already claimed")
                if existing.status != "completed":
                    raise ProviderExecutionRecordedFailure("material recheck already failed")
            else:
                if any(call.idempotency_key.startswith(prefix) for call in ProviderCallRepository(self.db).list_for_project(project)):
                    raise ProductionRunNotReady(("semantic_render_call_limit",))
                admitted = self.material_recheck_admission(project, remaining_render_calls=0)
                if admitted["state"] != "ready":
                    return self._save_material_recheck(repair, {**machine, **_unknown(admitted["stop_reasons"][0]),
                        "required_capability": admitted["required_capability"]})
                machine.update(provider=admitted["provider"], model=admitted["model"])
            duration = max(scene.start_frame+scene.duration_frames for scene in spec.scenes)
            rendered = Asset(id=render.payload.render_id, source_kind=SourceKind.USER_ASSET,
                source_file=str(self.root/"renders"/str(project)/f"{render.payload.render_id}.mp4"), content_hash=digest,
                duration_ms=math.ceil(duration*1000*spec.fps.denominator/spec.fps.numerator), width=spec.width, height=spec.height,
                fps=spec.fps, has_audio=True, authorization_reference=f"existing-render:{render.id}", imported_at=render.updated_at)
            videos = [self._video("render", scope, rendered), self._video("source", scope, asset)]
            if sum(Path(video["path"]).stat().st_size for video in videos)>12*1024*1024 or sum(video["proxy_duration_ms"] for video in videos)>48000:
                raise ProductionRunNotReady(("semantic_proxy_scope_too_large",))
            document = {"binding": machine["binding"], "scope": scope,
                "videos": [{key:value for key,value in video.items() if key != "path"} for video in videos],
                "scenes": [scope["scene_request"]], "provider_protocol": self.provider.protocol}
            machine["input"] = document
            def unchanged():
                self._require_material_recheck_current(machine, project, run)
                if source_view_uses(self.db,asset.id,source["start_ms"],source["end_ms"]) != source["view_source_uses"]:
                    raise ProductionRunNotReady(("semantic_source_view_changed",))
                if any(hashlib.sha256(Path(video["path"]).read_bytes()).hexdigest()!=video["video_sha256"] for video in videos):
                    raise ProductionRunNotReady(("semantic_proxy_bytes_changed",))
            def guard():
                unchanged()
                if any(call.idempotency_key.startswith(prefix) for call in ProviderCallRepository(self.db).list_for_project(project)):
                    raise ProductionRunNotReady(("semantic_render_call_limit",))
            def analyze():
                result = self.provider.analyze_intervals(videos, [scope["scene_request"]])
                if not isinstance(result, VideoIntervalBatch):
                    raise ValueError("semantic_provider_result_invalid")
                return {"batch": result.model_dump(mode="json"), "validation_issues": getattr(self.provider,"last_validation_issues",[])}
            def persist(result):
                call = ProviderCallRepository(self.db).get_by_idempotency_key(key)
                verdict = classify_material_recheck(scope, VideoIntervalBatch.model_validate(result["batch"]))
                if result.get("validation_issues"):
                    verdict = _unknown("semantic_provider_fields_invalid")
                saved = {**machine, **verdict, "raw_result": result, "call_id": str(call.id)}
                try:
                    unchanged()
                except (ValueError,RuntimeError,OSError):
                    saved.update(**_unknown("semantic_recheck_inputs_changed"),stale=True)
                self._save_material_recheck(repair,saved)
            result = ProviderExecutionService(self.db,self.cost_estimator).execute(project_id=project,idempotency_key=key,
                operation="vision",provider=identity,input_source=f"presentation-repair:{repair_id}",category=CostCategory.LLM,
                input_document=document,action=analyze,encode_result=lambda value:value,decode_result=lambda value:value,
                error_code=lambda error:"semantic_video_timeout" if isinstance(error,VisionTimeout) else "semantic_video_analysis_failed",
                reservation_guard=guard,before_action=unchanged,before_replay=unchanged,persist_result=persist,
                usage_callback=getattr(self.provider,"usage_accounting",None))
            saved = self._material_repair(project,run,repair_id)[0].get("semantic_recheck")
            if saved is None:
                persist(result)
                saved = self._material_repair(project,run,repair_id)[0]["semantic_recheck"]
            return saved
        except (ValueError,RuntimeError,OSError) as error:
            call = ProviderCallRepository(self.db).get_by_idempotency_key(key) if key else None
            reasons = getattr(error,"reasons",None)
            code = "semantic_budget_"+error.code if isinstance(error,BudgetLimitError) else reasons[0] if reasons else getattr(error,"code",None) or "semantic_recheck_failed"
            return self._save_material_recheck(repair,{**machine,**_unknown(code),"state":"failed" if call else "unknown",
                "call_id":str(call.id) if call else None,"call_status":call.status if call else None})

    def _observation(self, project, run, observation_id):
        row = self.db.connection.execute("SELECT payload FROM presentation_observations WHERE id=? AND project_id=? AND run_id=?",
                                        (str(observation_id), str(project), str(run))).fetchone()
        if row is None:
            raise LookupError("presentation observation not found")
        return json.loads(row[0])

    def _save(self, observation, machine):
        # ProviderCallLedger.finish owns the transaction when saving its result.
        with nullcontext() if self.db.connection.in_transaction else self.db.transaction(immediate=True):
            saved = self._observation(observation["project_id"], observation["run_id"], observation["id"])
            if saved["request"] != observation["request"] or saved["source"] != observation["source"]:
                machine = {**machine, **_unknown("semantic_observation_changed"), "stale": True}
            saved["machine_diagnosis"] = machine
            self.db.connection.execute("UPDATE presentation_observations SET payload=? WHERE id=?",
                                       (json.dumps(saved, ensure_ascii=False), observation["id"]))
        return machine

    def _binding(self, row, render, observation, draft, asset, clip):
        master = AudioAssetRepository(self.db).get(render.payload.video_spec.master_narration.audio_asset_id)
        return dict(run=_run_identity(row), render_job_id=str(render.id), render_id=str(render.payload.render_id),
            render_sha256=observation["request"]["render_sha256"], observation_id=observation["id"],
            observation_sha256=_snapshot_sha256({key: observation[key] for key in ("id", "source", "request", "render_job_id")}),
            video_spec_sha256=_snapshot_sha256(render.payload.video_spec.model_dump(mode="json")),
            draft_sha256=_snapshot_sha256(draft.model_dump(mode="json")),
            master_id=str(master.id), master_sha256=_snapshot_sha256(master.model_dump(mode="json")),
            master_content_hash=master.content_hash, asset_id=None if asset is None else str(asset.id),
            asset_content_hash=None if asset is None else asset.content_hash,
            asset_sha256=None if asset is None else _snapshot_sha256(asset.model_dump(mode="json")),
            clip_id=None if clip is None else str(clip.id),
            clip_sha256=None if clip is None else _snapshot_sha256(clip.model_dump(mode="json")))

    def _require_current(self, machine, project, run):
        row, render = self.presentation._current(project, run)
        if not semantic_diagnosis_is_current(self.db, machine, row, render):
            raise ProductionRunNotReady(("semantic_diagnosis_inputs_changed",))
        self.presentation._validate(row, render.payload.video_spec)
        if self.presentation._hash_render(render) != machine["binding"]["render_sha256"]:
            raise ProductionRunConflict("semantic_render_bytes_changed")
        return row, render

    def _video(self, role, scope, asset):
        details = scope[role]
        path = (self.extractor.extract_video_frames(asset, details["start_frame"], details["end_frame"])
                if role == "render" else self.extractor.extract_video_interval(asset, details["start_ms"], details["end_ms"]))
        path = Path(path).resolve()
        if not path.is_relative_to(self.root.resolve()) or not path.is_file():
            raise ProductionRunNotReady(("semantic_proxy_outside_data_root",))
        info = self.probe.probe(path)
        duration = ((details["end_frame"]-details["start_frame"]) * 1000 * asset.fps.denominator / asset.fps.numerator
                    if role == "render" else details["end_ms"]-details["start_ms"])
        if (abs(info.duration_ms-duration) > math.ceil(1000*asset.fps.denominator/asset.fps.numerator)+2
                or info.width <= 0 or info.height <= 0 or info.has_audio
                or abs(info.width*asset.height-info.height*asset.width) > 2*asset.width):
            raise ProductionRunNotReady(("semantic_proxy_invalid",))
        if role == "render":
            inspector = getattr(self.probe, "probe_video_frames", None)
            if not callable(inspector):
                raise ProductionRunNotReady(("semantic_render_frame_evidence_unavailable",))
            frames = inspector(path)
            rate = frames.get("fps")
            if (frames.get("frame_count") != details["end_frame"]-details["start_frame"]
                    or rate is None or rate.numerator*asset.fps.denominator != asset.fps.numerator*rate.denominator
                    or type(frames.get("duration_ms")) is not int
                    or abs(frames["duration_ms"]-duration) > math.ceil(1000*asset.fps.denominator/asset.fps.numerator)+2):
                raise ProductionRunNotReady(("semantic_render_frame_evidence_invalid",))
        return {**details, "path": str(path), "video_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "proxy_duration_ms": info.duration_ms, "comparison_role": role}

    def diagnose(self, project_id, run_id, observation_id):
        project, run = UUID(str(project_id)), UUID(str(run_id))
        observation = self._observation(project, run, observation_id)
        prior = observation.get("machine_diagnosis")
        if prior is not None:
            # This is an execution entry point, not the lightweight status projection.
            if prior.get("binding"):
                try:
                    self._require_current(prior, project, run)
                except (ValueError, RuntimeError):
                    prior = {**prior, **_unknown("semantic_diagnosis_inputs_changed"), "stale": True}
                    self._save(observation, prior)
            return prior
        base = dict(source="machine_semantic_comparison", method="same_scene_render_source_video_comparison",
                    stale=False, call_id=None, created_at=datetime.now(timezone.utc).isoformat(),
                    limitations=["audio_not_observed", "naturalness_and_visible_sync_not_assessed",
                                 "human_quality_review_required", "diagnosis_does_not_authorize_repair"])
        if observation["request"]["finding"] != "semantic_mismatch":
            return self._save(observation, {**base, **_unknown("semantic_observation_finding_unsupported")})
        identity = runtime_provider_identity(self.provider)
        if identity is not None:
            base.update(provider=identity.provider, model=identity.model)
        key = prefix = None
        machine = dict(base)
        try:
            row, render = self.presentation._current(project, run)
            self._require_active(row)
            if observation["render_job_id"] != str(render.id) or self.presentation._hash_render(render) != observation["request"]["render_sha256"]:
                raise ProductionRunConflict("semantic_observation_stale")
            self.presentation._validate(row, render.payload.video_spec)
            draft = ProjectDraftRepository(self.db).get(project)
            plan = next((scene for scene in draft.scenes if scene.scene_id == observation["request"]["scene_id"]), None)
            target = next((scene for scene in render.payload.video_spec.scenes if scene.scene_id == observation["request"]["scene_id"]), None)
            if plan is None or target is None:
                raise ProductionRunNotReady(("semantic_scene_missing",))
            asset = AssetRepository(self.db).get(target.visual.asset_id) if target.visual.asset_id else None
            clip = ClipRepository(self.db).get(target.visual.clip_id) if target.visual.clip_id else None
            machine["binding"] = self._binding(row, render, observation, draft, asset, clip)
            self._require_current(machine, project, run)
            bindings = json.loads(row["talking_source_bindings"])
            machine["preserves"] = {"render_job_id": str(render.id), "render_sha256": machine["binding"]["render_sha256"],
                "master_audio_id": machine["binding"]["master_id"], "master_audio_sha256": machine["binding"]["master_content_hash"],
                "source_asset_id": None if asset is None else str(asset.id),
                "source_content_hash": None if asset is None else asset.content_hash,
                "talking_job_ids": [item["talking_job_id"] for item in bindings if item.get("talking_job_id")],
                "talking_output_asset_ids": [item["talking_output_asset_id"] for item in bindings if item.get("talking_output_asset_id")],
                "scene_plan_ids": [str(scene.id) for scene in draft.scenes],
                "reuse_admission": "unchanged_dependencies_only"}
            if (identity is None or not getattr(self.provider, "supports_video_input", False)
                    or getattr(self.provider, "protocol", None) != "chat_completions"
                    or not callable(getattr(self.provider, "analyze_intervals", None))):
                return self._save(observation, {**machine, **_unknown("semantic_video_provider_unavailable"),
                    "required_capability": ["configured_video_comparison_provider"]})
            # 全轨 QA 不足以证明该场景文字对应当前音频区间；复用生产配时的持久段映射。
            from app.assembly.video_spec import _master_narration_intervals
            from app.voice_boundary_alignment import scene_narration_segment_spans
            master = AudioAssetRepository(self.db).get(render.payload.video_spec.master_narration.audio_asset_id)
            ordered = sorted(draft.scenes, key=lambda scene: scene.order)
            if scene_narration_segment_spans(master,ordered) is None:
                raise ProductionRunNotReady(("semantic_narration_text_interval_unverified",))
            try:
                timed = _master_narration_intervals(master, ordered,
                    end_ms=render.payload.video_spec.master_narration.end_ms)
            except ValueError:
                raise ProductionRunNotReady(("semantic_narration_text_interval_unverified",)) from None
            interval = next(((start,end) for scene,start,end in timed if scene.id == plan.id),None)
            scope = build_semantic_comparison_scope(render.payload.video_spec, target.scene_id, plan, asset, clip,
                master_verified=True, verified_scene_interval=interval)
            machine["scope"] = scope
            if scope["state"] != "ready":
                return self._save(observation, {**machine, **_unknown(scope["stop_reasons"][0])})
            from app.media.semantic_rejections import source_view_uses
            source_scope = scope['source']
            source_scope['view_source_uses'] = source_view_uses(self.db,asset.id,source_scope['start_ms'],source_scope['end_ms'])
            prefix = f"presentation-semantic:{run}:{render.id}:"
            key = prefix + observation["id"]
            existing = ProviderCallRepository(self.db).get_by_idempotency_key(key)
            if existing is not None:
                if (existing.project_id != project or existing.operation != "vision" or existing.mode != "runtime"
                        or existing.provider != identity.provider or existing.model != identity.model
                        or existing.input_source != f"presentation-observation:{observation['id']}"):
                    raise ProviderExecutionIdempotencyConflict("semantic provider identity changed")
                if existing.status in {"reserved", "running"}:
                    raise ProviderExecutionInProgress("semantic execution already claimed")
                if existing.status != "completed":
                    raise ProviderExecutionRecordedFailure("semantic execution already failed")
                # A completed same-key call may restore its projection even after budget is exhausted.
                # Proxy regeneration below still verifies its complete original input digest.
            else:
                if any(call.idempotency_key.startswith(prefix) for call in ProviderCallRepository(self.db).list_for_project(project)):
                    raise ProductionRunNotReady(("semantic_render_call_limit",))
                estimate = self.cost_estimator.estimate(operation="vision", provider=identity.provider, model=identity.model,
                    input_source=f"presentation-observation:{observation['id']}", category=CostCategory.LLM)
                # Read-only early rejection avoids extracting media for a known denied call.
                # ProviderExecutionService repeats budget and ownership checks atomically at dispatch.
                ProviderCallLedger(self.db).check_reservation(project_id=project, estimated_cost=estimate)
            duration = max(scene.start_frame+scene.duration_frames for scene in render.payload.video_spec.scenes)
            spec = render.payload.video_spec
            rendered = Asset(id=render.payload.render_id, source_kind=SourceKind.USER_ASSET,
                source_file=str(self.root / "renders" / str(project) / f"{render.payload.render_id}.mp4"),
                content_hash=machine["binding"]["render_sha256"], duration_ms=math.ceil(duration*1000*spec.fps.denominator/spec.fps.numerator),
                width=spec.width, height=spec.height, fps=spec.fps, has_audio=True,
                authorization_reference=f"existing-render:{render.id}", imported_at=render.updated_at)
            videos = [self._video("render", scope, rendered), self._video("source", scope, asset)]
            if sum(Path(video["path"]).stat().st_size for video in videos) > 12*1024*1024 or sum(video["proxy_duration_ms"] for video in videos) > 48000:
                raise ProductionRunNotReady(("semantic_proxy_scope_too_large",))
            document = {"binding": machine["binding"], "scope": scope,
                        "videos": [{key:value for key,value in video.items() if key != "path"} for video in videos],
                        "scenes": [scope["scene_request"]], "provider_protocol": self.provider.protocol}
            if existing is not None and existing.status == 'completed':
                legacy = json.loads(json.dumps(document))
                legacy['scope']['source'].pop('view_source_uses',None)
                for video in legacy['videos']:video.pop('view_source_uses',None)
                legacy_digest = provider_call_input_digest(dict(project_id=str(project),operation='vision',
                    provider=identity.provider,model=identity.model,input_source=f"presentation-observation:{observation['id']}",input=legacy))
                if existing.input_digest == legacy_digest:
                    # Recover the old exact-view receipt without upgrading it to a new parent-view claim.
                    document, scope = legacy, legacy['scope']
                    machine['scope'] = scope
            machine["input"] = document
            def unchanged():
                active,_=self._require_current(machine, project, run)
                self._require_active(active)
                bound = scope['source'].get('view_source_uses')
                if bound is not None and source_view_uses(self.db,asset.id,scope['source']['start_ms'],scope['source']['end_ms']) != bound:
                    raise ProductionRunNotReady(('semantic_source_view_changed',))
                if any(hashlib.sha256(Path(video["path"]).read_bytes()).hexdigest() != video["video_sha256"] for video in videos):
                    raise ProductionRunNotReady(("semantic_proxy_bytes_changed",))

            def guard():
                unchanged()
                if any(call.idempotency_key.startswith(prefix) for call in ProviderCallRepository(self.db).list_for_project(project)):
                    raise ProductionRunNotReady(("semantic_render_call_limit",))

            def analyze():
                result = self.provider.analyze_intervals(videos, [scope["scene_request"]])
                if not isinstance(result, VideoIntervalBatch):
                    raise ValueError("semantic_provider_result_invalid")
                return {"batch": result.model_dump(mode="json"),
                        "validation_issues": getattr(self.provider, "last_validation_issues", [])}

            def persist(result):
                call = ProviderCallRepository(self.db).get_by_idempotency_key(key)
                verdict = classify_semantic_comparison(scope, VideoIntervalBatch.model_validate(result["batch"]))
                verdict["required_capability"] = (["scoped_material_replacement"] if verdict["responsible_stage"] == "material"
                    else ["semantic_layout_correction"] if verdict["responsible_stage"] == "assembly"
                    else ["exact_scene_narration_visual_semantics_diagnosis"])
                if result.get("validation_issues"):
                    verdict = _unknown("semantic_provider_fields_invalid")
                saved = {**machine, **verdict, "raw_result": result, "call_id": str(call.id)}
                try:
                    unchanged()
                except (ValueError, RuntimeError, OSError):
                    saved.update(**_unknown("semantic_diagnosis_inputs_changed"), stale=True)
                # Retain consumed usage even when the current version changed during inference.
                self._save(observation, saved)

            result = ProviderExecutionService(self.db, self.cost_estimator).execute(project_id=project,
                idempotency_key=key, operation="vision", provider=identity,
                input_source=f"presentation-observation:{observation['id']}", category=CostCategory.LLM,
                input_document=document, action=analyze, encode_result=lambda value:value,
                decode_result=lambda value:value, error_code=lambda error:"semantic_video_timeout" if isinstance(error, VisionTimeout) else "semantic_video_analysis_failed",
                reservation_guard=guard, before_action=unchanged, before_replay=unchanged,
                persist_result=persist, usage_callback=getattr(self.provider, "usage_accounting", None))
            saved = self._observation(project, run, observation_id).get("machine_diagnosis")
            if saved is None:  # Completed ledger replay also restores a missing observation projection.
                persist(result)
                saved = self._observation(project, run, observation_id)["machine_diagnosis"]
            return saved
        except (ValueError, RuntimeError, OSError) as error:
            call = ProviderCallRepository(self.db).get_by_idempotency_key(key) if key else None
            reasons = getattr(error, "reasons", None)
            code = "semantic_budget_"+error.code if isinstance(error, BudgetLimitError) else (reasons[0] if reasons else getattr(error, "code", None) or "semantic_diagnosis_failed")
            return self._save(observation, {**machine, **_unknown(code), "state": "failed" if call else "unknown",
                "call_id": str(call.id) if call else None, "call_status": call.status if call else None})
