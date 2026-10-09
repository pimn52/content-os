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
    CandidateAsset, GraphicTreatment, SubtitleTreatment, EditVisualRole, CostCategory, UsageCost)
from app.production_runs import ProductionRunService, ProductionRunConflict, ProductionRunNotReady, _snapshot_sha256
from app.repair_allowance import used_repairs
from app.renderer.remotion import RemotionRenderer
from app.assembly.edit_plan import resolve_edit_plan, _normalized
from app.budget import ProviderCallLedger


class PresentationObservationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=1, max_length=500)
    render_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scene_id: str = Field(min_length=1, max_length=100)
    finding: Literal["duplicate_text", "known_subtitle_conflict", "text_overlay", "face_out_of_frame", "source_change", "unknown_subtitles"]
    reason: str = Field(min_length=1, max_length=2000, pattern=r"\S")
    evidence_reference: str = Field(min_length=1, max_length=2000, pattern=r"\S")


class PresentationRepairRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    observation_id: UUID
    expected_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=500)
    confirmed_action: Literal["render_revision"]
    reason: str = Field(min_length=1, max_length=2000, pattern=r"\S")


class PresentationRepairService:
    def __init__(self, db: Database, data_root: Path, render_root: Path):
        self.db, self.data_root, self.render_root = db, Path(data_root).resolve(), Path(render_root).resolve()
        self.production = ProductionRunService(db, self.data_root)

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

    def observe(self, project, run, request: PresentationObservationRequest):
        with self.db.transaction(immediate=True):
            old = self.db.connection.execute("SELECT payload FROM presentation_observations WHERE project_id=? AND idempotency_key=?", (str(project), request.idempotency_key)).fetchone()
            if old:
                saved = json.loads(old["payload"])
                if saved["run_id"] != str(run) or saved["request"] != request.model_dump(mode="json"):
                    raise ProductionRunConflict("presentation_observation_idempotency_conflict")
                return saved
            _, job = self._current(project, run)
            if self._hash_render(job) != request.render_sha256:
                raise ProductionRunConflict("presentation_observation_render_changed")
            if request.scene_id not in {s.scene_id for s in job.payload.video_spec.scenes}:
                raise ProductionRunConflict("presentation_observation_scene_mismatch")
            record = {"id": str(uuid4()), "project_id": str(project), "run_id": str(run),
                      "render_job_id": str(job.id), "source": "human_observation", "request": request.model_dump(mode="json"),
                      "created_at": datetime.now(timezone.utc).isoformat()}
            self.db.connection.execute("INSERT INTO presentation_observations VALUES (?,?,?,?,?,?)", (record["id"], str(project), str(run), str(job.id), request.idempotency_key, json.dumps(record)))
            return record

    def _validate(self, row, spec: VideoSpec):
        draft = ProjectDraftRepository(self.db).get(spec.project_id)
        project = ProjectRepository(self.db).get(spec.project_id)
        if draft is None or project is None or draft.version != row["draft_version"] or draft.script_revision != row["script_revision"]:
            raise ProductionRunNotReady(("presentation_draft_changed_replan",))
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
        remaining = max(0, row["repair_allowance"] - used_repairs(self.db, run))
        if remaining == 0: reasons.append("run_repair_allowance_exhausted")
        if old.edit_plan is None:
            reasons.append("presentation_complete_edit_plan_required")
        else:
            scene = next(s for s in candidate["scenes"] if s["scene_id"] == item["scene_id"])
            entry = next((e for e in candidate["edit_plan"]["scenes"] if e["scene_id"] == item["scene_id"]), None)
            before = scene.copy()
            if entry is None: reasons.append("presentation_scene_mapping_requires_replan")
            elif item["finding"] == "duplicate_text":
                captions = "".join(c["text"] for c in scene["captions"]) or scene["caption"] or ""
                if scene["subtitle_treatment"] != "timed_captions" or not captions or not scene["graphic_text"] or _normalized(scene["graphic_text"]) != _normalized(captions):
                    reasons.append("presentation_no_supported_duplicate_candidate")
                elif scene["visual_role"] == "graphic":
                    draft = ProjectDraftRepository(self.db).get(project)
                    target = next((s for s in draft.scenes if s.scene_id == item["scene_id"]), None)
                    short = None if target is None or not target.caption_emphasis else target.caption_emphasis[0]
                    if not short or _normalized(short) == _normalized(captions): reasons.append("presentation_semantic_short_text_required")
                    else: scene["graphic_text"] = entry["graphic_text"] = short
                else:
                    scene["graphic_text"] = entry["graphic_text"] = None
                    scene["graphic_treatment"] = entry["graphic_treatment"] = "none"
            elif item["finding"] == "known_subtitle_conflict":
                if (entry["burned_in_subtitles"] != "present" or not entry["selected_clip_id"]
                    or scene["subtitle_treatment"] != "timed_captions"):
                    reasons.append("presentation_known_source_subtitles_required")
                else:
                    scene["subtitle_treatment"] = entry["subtitle_treatment"] = "none"
                    # Keep semantic copy used by admission; the renderer suppresses
                    # new captions through treatment, not by deleting narration.
            elif item["finding"] == "text_overlay":
                if (entry["visual_role"] != "talking" or entry["selected_source_kind"] != "ai_video"
                    or old.width != 1080 or old.height != 1920
                    or entry["framing_policy"] != "face_safe_contain" or entry["burned_in_subtitles"] == "unknown"
                    or scene["portrait_presentation"] != "full_canvas" or not scene["graphic_text"]
                    or scene["visual"]["vertical_reframe_mode"] != "contain" or scene["visual"]["source_bottom_crop_ratio"]):
                    reasons.append("presentation_qualified_source_preserving_panel_required")
                else:
                    scene["portrait_presentation"] = entry["portrait_presentation"] = "portrait_panel"
            else: reasons.append("presentation_source_interval_or_layout_replan_required")
            if scene != before: changes.append({"scene_id": item["scene_id"], "before": before, "after": scene.copy()})
        action = "render_revision" if changes and not reasons else "replan"
        if remaining == 0: action = "stop"
        new_spec = None
        if changes:
            try:
                new_spec = VideoSpec.model_validate(candidate)
                inputs = self._validate(row, new_spec)
            except (ValueError, RuntimeError) as exc:
                reasons.extend(getattr(exc, "reasons", ("presentation_input_or_preflight_requires_replan",)))
                action = "stop"
        budgets = []
        if action == "render_revision":
            from app.db import BudgetPolicyRepository
            from app.budget import enforce_reservation, BudgetLimitError
            repo = ProviderCallRepository(self.db)
            for scope in (None, project):
                policy = BudgetPolicyRepository(self.db).get_by_project(scope)
                budgets.append(None if policy is None else policy.model_dump(mode="json"))
                if policy:
                    try: enforce_reservation(policy, repo.list_all() if scope is None else repo.list_for_project(project), UsageCost(category=CostCategory.RENDER, amount=Decimal("0"), currency="USD"), ignore_existing_unknown_cost=True)
                    except BudgetLimitError: reasons.append("presentation_render_budget_blocked"); action = "stop"
        values = {"project_id": str(project), "run_id": str(run), "observation": observation,
                  "action": action, "stop_reasons": reasons, "changes": changes, "remaining_run_repairs": remaining,
                  "predecessor_job": job.model_dump(mode="json"), "candidate_spec": None if new_spec is None else new_spec.model_dump(mode="json"),
                  "inputs": inputs, "budgets": budgets, "max_local_render_calls": 1, "voice_calls": 0, "talking_calls": 0,
                  "external_charge_ceiling": "0", "local_compute_cost": None, "user_active_minutes": None,
                  "review_scope": "new_output_technical_qa_and_final_u_product"}
        return {**values, "fingerprint": _snapshot_sha256(values)}

    def apply(self, project, run, request: PresentationRepairRequest):
        with self.db.transaction(immediate=True):
            digest = _snapshot_sha256(request.model_dump(mode="json"))
            old = self.db.connection.execute("SELECT * FROM presentation_repairs WHERE project_id=? AND idempotency_key=?", (str(project), request.idempotency_key)).fetchone()
            if old:
                if old["run_id"] != str(run) or old["request_fingerprint"] != digest: raise ProductionRunConflict("presentation_repair_idempotency_conflict")
                return json.loads(old["payload"])
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
        if self._hash_render(predecessor) != record["plan"]["observation"]["request"]["render_sha256"]:
            raise ProductionRunNotReady(("presentation_predecessor_bytes_changed",))
        if self._validate(current, job.payload.video_spec) != record["plan"]["inputs"]:
            raise ProductionRunNotReady(("presentation_dependencies_changed",))
        return record

    def reserve_render(self, job):
        def guard():
            record = self.require_current_repair(job)
            prefix = f"presentation-repair:{record['id']}:render"
            if any(c.idempotency_key.startswith(prefix) for c in ProviderCallRepository(self.db).list_for_project(job.project_id)):
                raise ProductionRunNotReady(("presentation_render_call_limit",))
        record = self.require_current_repair(job)
        if record is None: return None
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
        from app.media.ffprobe import FFProbeAdapter
        from app.runtime import resolve_local_executable
        output = self.render_root / str(job.project_id) / f"{job.payload.render_id}.mp4"
        probe = FFProbeAdapter(command=resolve_local_executable("ffprobe")).probe(output)
        spec = job.payload.video_spec
        frames = max(scene.start_frame + scene.duration_frames for scene in spec.scenes)
        expected_ms = frames * 1000 * spec.fps.denominator / spec.fps.numerator
        frame_ms = 1000 * spec.fps.denominator / spec.fps.numerator
        video = next(stream for stream in probe.metadata["streams"] if stream.get("codec_type") == "video")
        audio = next((stream for stream in probe.metadata["streams"] if stream.get("codec_type") == "audio"), None)
        video_ms = float(video.get("duration", "nan")) * 1000
        audio_ms = float(audio.get("duration", "nan")) * 1000 if audio else float("nan")
        # Remotion AAC adds encoder/container padding. Check exact video frames
        # separately; permit at most three AAC sample frames, not arbitrary drift.
        audio_padding_ms = max(frame_ms, 3 * 1024 * 1000 / int(audio["sample_rate"])) if audio and audio.get("codec_name") == "aac" else frame_ms
        if (probe.width != spec.width or probe.height != spec.height or probe.fps != spec.fps
            or not probe.has_audio or int(video.get("nb_frames", -1)) != frames
            or not abs(video_ms - expected_ms) <= frame_ms
            or not expected_ms - frame_ms <= audio_ms <= expected_ms + audio_padding_ms
            or not expected_ms - frame_ms <= probe.duration_ms <= expected_ms + audio_padding_ms + 1):
            raise ProductionRunNotReady(("presentation_output_technical_qa_failed",))
        decoded = subprocess.run([resolve_local_executable("ffmpeg"), "-v", "error", "-xerror", "-i", str(output),
            "-c:v", "rawvideo", "-c:a", "pcm_s16le", "-f", "null", "-"], shell=False, capture_output=True, timeout=60, check=False)
        if decoded.returncode:
            raise ProductionRunNotReady(("presentation_output_decode_failed",))
        evidence = {"state": "verified", "sha256": self._hash_render(job), "width": probe.width, "height": probe.height,
                    "duration_ms": probe.duration_ms, "fps": probe.fps.model_dump(mode="json"), "has_audio": True,
                    "video_duration_ms": video_ms, "video_frames": frames, "audio_duration_ms": audio_ms,
                    "audio_padding_tolerance_ms": audio_padding_ms,
                    "decode": "pass", "scope": "technical_only_final_u_product_required"}
        with self.db.transaction(immediate=True):
            self.require_current_repair(job)
            self.db.connection.execute("UPDATE presentation_repairs SET technical_qa=? WHERE replacement_job_id=? AND technical_qa IS NULL",
                (json.dumps(evidence), str(job.id)))
        return evidence
