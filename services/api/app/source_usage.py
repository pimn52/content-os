"""Visible source intervals shared by planning, production and rendering.

Voice identity/enrolment references are not visible footage. Claims live on the
existing ProductionRun; job state and publication records determine occupancy.
"""
from __future__ import annotations

import json
from uuid import UUID

from app.db import AssetRepository, ClipRepository, JobRepository, ProjectRepository
from app.domain.models import JobStatus, RenderVideoJobPayload


class SourceUsageConflict(ValueError):
    def __init__(self, reason, uses=()):
        self.reason, self.uses = reason, tuple(uses)
        super().__init__(reason)


def overlap(left, right):
    return (left["source_hash"] == right["source_hash"]
            and max(left["start_ms"], right["start_ms"]) < min(left["end_ms"], right["end_ms"]))


def repeatable(asset):
    """原样画面允许引用；新语音/口型在派生链处标记内容改动。"""
    return not any(asset.metadata.get(key) for key in ("talking_generation", "talking_run", "talking_run_preview"))


def conflicts(left, right):
    return overlap(left, right) and not left.get("repeatable", False)


class SourceUsageService:
    def __init__(self, db):
        self.db = db
        self.assets, self.clips = AssetRepository(db), ClipRepository(db)

    def resolve(self, asset_id, start, end, scene_id, *, _seen=()):
        try:
            return self._resolve(asset_id,start,end,scene_id,_seen=_seen)
        except SourceUsageConflict:
            raise
        except (KeyError,ValueError,TypeError,AttributeError) as exc:
            raise SourceUsageConflict("visual_source_lineage_unresolved") from exc

    def _resolve(self, asset_id, start, end, scene_id, *, _seen=()):
        """Map crop and Talking output coordinates back to original motion."""
        asset = self.assets.get(UUID(str(asset_id)))
        if asset is None or not 0 <= start < end <= asset.duration_ms or asset.id in _seen:
            raise SourceUsageConflict("visual_source_lineage_unresolved")
        seen = (*_seen, asset.id)
        derived = asset.metadata.get("source_derivation")
        generation = asset.metadata.get("talking_generation")
        if isinstance(derived, dict):
            parent = self.assets.get(UUID(derived["source_asset_id"]))
            interval = derived.get("source_interval", {})
            if parent is None or parent.content_hash != derived.get("source_content_hash"):
                raise SourceUsageConflict("visual_source_lineage_changed")
            offset = interval.get("start_ms")
            if type(offset) is not int or offset + end > interval.get("end_ms", -1):
                raise SourceUsageConflict("visual_source_lineage_unresolved")
            return self.resolve(parent.id, offset + start, offset + end, scene_id, _seen=seen)
        if isinstance(generation, dict):
            clip = self.clips.get(UUID(generation["reference_clip_id"]))
            offset = generation.get("reference_window_start_ms", None if clip is None else clip.start_ms)
            if clip is None or type(offset) is not int or offset + end > clip.end_ms:
                raise SourceUsageConflict("visual_source_lineage_unresolved")
            return [{**use, "repeatable": False, "content_changed": True} for use in
                    self.resolve(clip.asset_id, offset + start, offset + end, scene_id, _seen=seen)]
        run = asset.metadata.get("talking_run") or asset.metadata.get("talking_run_preview")
        if isinstance(run, dict):
            # Never count a concatenated/generated hash as independent footage.
            series_id = run.get("series_id")
            row = self.db.connection.execute("SELECT payload FROM talking_slice_series WHERE id=?", (series_id,)).fetchone() if series_id else None
            if row is None:
                raise SourceUsageConflict("visual_source_lineage_unresolved")
            from app.db import TalkingSliceSeriesRepository, TalkingRunRepository
            series = TalkingSliceSeriesRepository(self.db).get(UUID(series_id))
            admitted = TalkingRunRepository(self.db).get(UUID(run["run_id"])) if run.get("run_id") else None
            children = []
            if admitted:
                children = [(item.output_asset_id, item.master_end_ms-item.master_start_ms) for item in admitted.child_evidence]
            elif series and series.planned_origin:
                origin = series.planned_origin
                children = [(origin.output_asset_id, origin.master_end_ms-origin.master_start_ms)]
            else:
                raise SourceUsageConflict("visual_source_lineage_unresolved")
            uses, cursor = [], 0
            for child_id, duration in children:
                child = self.assets.get(child_id)
                left, right = max(start, cursor), min(end, cursor + duration)
                if left < right:
                    if child is None:
                        raise SourceUsageConflict("visual_source_lineage_unresolved")
                    uses.extend(self.resolve(child.id, left-cursor, right-cursor, scene_id, _seen=seen))
                cursor += duration
            if cursor < end:
                raise SourceUsageConflict("visual_source_lineage_unresolved")
            return uses
        return [dict(scene_id=str(scene_id), source_hash=asset.content_hash,
                     start_ms=start, end_ms=end, repeatable=repeatable(asset), content_changed=False)]

    def candidate_uses(self, candidate, scene_id, duration_ms):
        if candidate is None or candidate.asset_id is None or candidate.clip_id is None:
            return []
        clip = self.clips.get(candidate.clip_id)
        if clip is None:
            raise SourceUsageConflict("visual_source_lineage_unresolved")
        start = candidate.playback_start_ms if candidate.playback_start_ms is not None else clip.start_ms
        end = candidate.playback_end_ms if candidate.playback_end_ms is not None else clip.end_ms
        if (candidate.asset_id != clip.asset_id or not clip.start_ms <= start < end <= clip.end_ms
            or type(duration_ms) is not int or duration_ms <= 0 or start + duration_ms > end):
            raise SourceUsageConflict("visual_source_playback_interval_invalid")
        return self.resolve(clip.asset_id, start, start + duration_ms, scene_id)

    def allocate_candidate(self, candidate, scene_id, duration_ms, occupied, *, fixed=False, prefer_unused=False):
        """Use another real interval of the same eligible Clip before giving up."""
        uses = self.candidate_uses(candidate, scene_id, duration_ms)
        check = overlap if prefer_unused else conflicts
        hits = [other for other in occupied if any(check(use, other) for use in uses)]
        if not hits:
            return candidate
        if fixed or candidate.playback_start_ms is not None or candidate.source_kind == "ai_video" or len(uses) != 1:
            return None
        clip = self.clips.get(candidate.clip_id)
        start = candidate.playback_start_ms if candidate.playback_start_ms is not None else clip.start_ms
        end = candidate.playback_end_ms if candidate.playback_end_ms is not None else clip.end_ms
        while start + duration_ms <= end:
            use = self.resolve(clip.asset_id, start, start+duration_ms, scene_id)[0]
            hits = [other for other in occupied if check(use, other)]
            if not hits:
                return candidate.model_copy(update={"playback_start_ms":start, "playback_end_ms":end,
                    "why":[*candidate.why, "已自动避开本片及其他视频占用的来源区间"]})
            start += max(other["end_ms"] for other in hits) - use["start_ms"]
        return None

    def preflight_uses(self, preflight, scenes):
        durations = {scene.id:scene.duration_target_ms for scene in scenes}
        if preflight.reusable_master_audio_id is not None:
            from app.db import AudioAssetRepository
            from app.assembly.video_spec import _master_narration_intervals
            master = AudioAssetRepository(self.db).get(preflight.reusable_master_audio_id)
            intervals = _master_narration_intervals(master, sorted(scenes,key=lambda value:value.order))
            durations = {scene.id:end-start for scene,start,end in intervals}
        return [use for item in preflight.scenes for use in self.candidate_uses(
            item.selected_candidate, item.scene_id, durations[item.scene_plan_id])]

    def spec_uses(self, spec):
        result = []
        for scene in sorted(spec.scenes, key=lambda value: value.start_frame):
            visual = scene.visual
            if visual.asset_id is None or visual.clip_start_ms is None:
                continue
            if self.assets.get(visual.asset_id) is None:
                continue  # Still images have no motion interval.
            duration = (scene.duration_frames * 1000 * spec.fps.denominator + spec.fps.numerator-1) // spec.fps.numerator
            # At most one partial frame at scene end; renderer enforces coverage.
            result.extend(self.resolve(visual.asset_id, visual.clip_start_ms,
                min(visual.clip_end_ms, visual.clip_start_ms + duration), scene.scene_id))
        return result

    def output_versions(self, row):
        versions = {f"run:{row['id']}", f"draft-{row['draft_version']}"}
        job = JobRepository(self.db).get(UUID(row["render_job_id"])) if row["render_job_id"] else None
        if job is not None and isinstance(job.payload, RenderVideoJobPayload):
            versions.update((f"render:{job.payload.render_id}", str(job.payload.render_id), str(job.id)))
        return versions

    def review(self, row):
        versions = self.output_versions(row)
        # Exact render feedback takes precedence over older draft-level notes.
        values = [json.loads(value[0]) for value in self.db.connection.execute(
            "SELECT payload FROM content_feedback WHERE project_id=? ORDER BY rowid", (row["project_id"],))]
        matched = [value for value in values if value["output_version"] in versions]
        exact = [value for value in matched if not value["output_version"].startswith("draft-")]
        value = (exact or matched or [None])[-1]
        return dict(state="pending" if value is None else "approved" if value["accepted"] else "rejected",
                    notes=None if value is None else value.get("rejection_reason") or value.get("notes"))

    def _held(self, row):
        versions = self.output_versions(row)
        published = any(value[0] in versions for value in self.db.connection.execute(
            "SELECT output_version FROM publication_records WHERE project_id=?", (row["project_id"],)))
        if published:
            return "published"
        if row["delivery_intent"] == "draft":
            return None
        if row["stage"] not in {"awaiting_master", "awaiting_talking", "voice", "render", "cancelled"}:
            return None
        if row["stage"] == "cancelled" or self.review(row)["state"] == "rejected":
            return None
        if row["render_job_id"]:
            render = JobRepository(self.db).get(UUID(row["render_job_id"]))
            if render and render.status is JobStatus.COMPLETED:
                return None if self.choice(row["project_id"], render.payload.render_id) == "discarded" else "reserved"
        ids = [row[key] for key in ("voice_job_id", "voice_qa_job_id", "render_job_id") if row[key]]
        for binding in json.loads(row["talking_source_bindings"]):
            ids.extend(str(binding[key]) for key in ("talking_job_id", "talking_qa_job_id") if binding.get(key))
        jobs = [JobRepository(self.db).get(UUID(value)) for value in ids]
        if any(job and job.status is JobStatus.RUNNING for job in jobs):
            return "reserved"
        if any(job and job.status in {JobStatus.FAILED, JobStatus.CANCELLED} for job in jobs):
            return None
        return "reserved"

    def _render_run(self, job):
        row = self.db.connection.execute("SELECT * FROM production_runs WHERE render_job_id=?", (str(job.id),)).fetchone()
        if row is None:
            repair = self.db.connection.execute("SELECT run_id FROM presentation_repairs WHERE replacement_job_id=?", (str(job.id),)).fetchone()
            key = repair[0] if repair else job.idempotency_key.split(":")[1] if job.idempotency_key.startswith("production-run:") else None
            if key:
                row = self.db.connection.execute("SELECT * FROM production_runs WHERE id=? AND project_id=?", (key,str(job.project_id))).fetchone()
        return row

    def occupied(self, project_id, *, run_id=None, job_id=None, work_id=None):
        project = ProjectRepository(self.db).get(UUID(str(project_id)))
        if project is None:
            return []
        result = []
        own_versions = set()
        current = self.db.connection.execute("SELECT * FROM production_runs WHERE id=?", (str(run_id),)).fetchone() if run_id else None
        if current:
            own_versions.update(self.output_versions(current))
        own_work = str(work_id) if work_id else (current["work_id"] or current["id"]) if current else None
        if own_work:
            for peer in self.db.connection.execute("SELECT * FROM production_runs WHERE project_id=? AND COALESCE(work_id,id)=?", (str(project_id),own_work)):
                own_versions.update(self.output_versions(peer))
        if (current["delivery_intent"] if current else project.delivery_intent) == "draft":
            return []  # 普通试做/比较不挤占正式作品，片内新表演仍检查重复。
        linked_jobs = {row[0] for row in self.db.connection.execute("SELECT render_job_id FROM production_runs WHERE render_job_id IS NOT NULL")}
        for row in self.db.connection.execute("SELECT r.* FROM production_runs r JOIN projects p ON p.id=r.project_id"):
            owner = ProjectRepository(self.db).get(UUID(row["project_id"]))
            if owner.ip_profile_id != project.ip_profile_id or row["id"] == str(run_id):
                continue
            held = self._held(row)
            if held is None:
                continue
            if own_work and (row["work_id"] or row["id"]) == own_work:
                continue
            uses = json.loads(row["visual_source_uses"])
            if row["render_job_id"]:
                job = JobRepository(self.db).get(UUID(row["render_job_id"]))
                if job and isinstance(job.payload, RenderVideoJobPayload):
                    uses = self.spec_uses(job.payload.video_spec)
            # Legacy Talking runs also occupy their actual input motion, even
            # before this column existed. Inference context is not visible use.
            if not row["render_job_id"]:
                for binding in json.loads(row["talking_source_bindings"]):
                    if not binding.get("talking_job_id"):
                        continue
                    job = JobRepository(self.db).get(UUID(binding["talking_job_id"]))
                    clip = self.clips.get(UUID(binding["reference_clip_id"]))
                    if job and clip:
                        start = job.payload.reference_window_start_ms
                        start = clip.start_ms if start is None else start
                        uses.extend(self.resolve(clip.asset_id, start,
                            start + binding["master_end_ms"]-binding["master_start_ms"], binding["scene_plan_id"]))
            result.extend({**use, "project_id": row["project_id"], "run_id": row["id"], "state": held} for use in uses)
        render_jobs = self.db.connection.execute("""SELECT j.id FROM jobs j JOIN projects p ON p.id=j.project_id
            WHERE json_extract(j.payload,'$.type')='render' AND json_extract(p.payload,'$.ip_profile_id')=?""",
            (str(project.ip_profile_id),)).fetchall()
        for job_row in render_jobs:
            job = JobRepository(self.db).get(UUID(job_row["id"]))
            if (str(job.id) in linked_jobs or str(job.id) == str(job_id)
                or not isinstance(job.payload, RenderVideoJobPayload)):
                continue
            owner = ProjectRepository(self.db).get(job.project_id)
            if owner is None or owner.ip_profile_id != project.ip_profile_id:
                continue
            version = f"render:{job.payload.render_id}"
            render_run = self._render_run(job)
            if render_run is not None and render_run["id"] == str(run_id):
                own_versions.add(version)
                continue  # A revision of this video is not another video's use.
            published = self.db.connection.execute("SELECT 1 FROM publication_records WHERE project_id=? AND output_version=?",
                (str(job.project_id),version)).fetchone()
            if render_run is not None and own_work and (render_run["work_id"] or render_run["id"]) == own_work:
                continue
            if not published and ((render_run is not None and render_run["delivery_intent"] == "draft")
                or render_run is None and owner.delivery_intent == "draft"):
                continue
            if not published and job.status in {JobStatus.FAILED, JobStatus.CANCELLED}:
                continue
            feedback = self.db.connection.execute("SELECT payload FROM content_feedback WHERE project_id=? AND output_version=?",
                (str(job.project_id),version)).fetchone()
            if not published and feedback and not json.loads(feedback[0])["accepted"]:
                continue
            if not published and self.choice(job.project_id,job.payload.render_id)=="discarded":
                continue
            result.extend({**use,"project_id":str(job.project_id),"state":"published" if published else "reserved"}
                for use in self.spec_uses(job.payload.video_spec))
        # Imported/manual publication usage is a historical lower bound. It
        # cannot invent sub-clip coordinates that were never recorded.
        for row in self.db.connection.execute("SELECT payload FROM asset_usage_events"):
            event = json.loads(row[0])
            owner = ProjectRepository(self.db).get(UUID(event["project_id"]))
            if owner.ip_profile_id != project.ip_profile_id or event["media_kind"] != "video" or not event.get("clip_id"):
                continue
            if not self.db.connection.execute("SELECT 1 FROM publication_records WHERE project_id=? AND output_version=?",
                (event["project_id"], event["output_version"])).fetchone():
                continue
            if event["project_id"] == str(project_id) and event["output_version"] in own_versions:
                continue
            clip = self.clips.get(UUID(event["clip_id"]))
            if clip:
                result.extend({**use, "project_id":event["project_id"], "state":"published"} for use in
                    self.resolve(clip.asset_id, event.get("source_start_ms") if event.get("source_start_ms") is not None else clip.start_ms,
                        event.get("source_end_ms") or clip.end_ms, "published"))
        return result

    def require_available(self, project_id, uses, *, run_id=None, job_id=None, work_id=None):
        for index, use in enumerate(uses):
            if any(conflicts(use, other) for other in uses[:index]):
                raise SourceUsageConflict("visual_source_repeated_in_video", uses)
        hits = [other for other in self.occupied(project_id, run_id=run_id, job_id=job_id, work_id=work_id)
                if any(conflicts(use, other) for use in uses)]
        if hits:
            reason = "visual_source_already_published" if any(value["state"] == "published" for value in hits) else "visual_source_reserved_by_other_video"
            raise SourceUsageConflict(reason, hits)

    def save(self, project_id, run_id, uses):
        self.require_available(project_id, uses, run_id=run_id)
        self.db.connection.execute("UPDATE production_runs SET visual_source_uses=? WHERE id=? AND project_id=?",
            (json.dumps(uses), str(run_id), str(project_id)))

    def require_render(self, job):
        row = self.db.connection.execute("SELECT id FROM production_runs WHERE render_job_id=?", (str(job.id),)).fetchone()
        self.require_available(job.project_id, self.spec_uses(job.payload.video_spec),
            run_id=None if row is None else row["id"], job_id=job.id)

    def choice(self, project_id, render_id):
        row=self.db.connection.execute("SELECT disposition FROM render_choices WHERE project_id=? AND render_id=?",
            (str(project_id),str(render_id))).fetchone()
        if row is None:return "candidate"
        if row[0]=="selected":
            rejected=self.db.connection.execute("SELECT 1 FROM content_feedback WHERE project_id=? AND output_version=? AND json_extract(payload,'$.accepted')=0",
                (str(project_id),f"render:{render_id}")).fetchone()
            job_row=self.db.connection.execute("SELECT id FROM jobs WHERE project_id=? AND json_extract(payload,'$.payload.render_id')=?",
                (str(project_id),str(render_id))).fetchone()
            linked=self._render_run(JobRepository(self.db).get(UUID(job_row[0]))) if job_row else None
            if rejected or linked is not None and self.review(linked)["state"]=="rejected":return "discarded"
        return row[0]

    def clip_occupancy_degree(self, clip, occupied):
        """按实际覆盖区间合并占用，不以使用次数、素材年龄或候选版数计分。"""
        uses = self.resolve(clip.asset_id, clip.start_ms, clip.end_ms, "ranking")
        covered, total = 0, 0
        for use in uses:
            total += use["end_ms"] - use["start_ms"]
            intervals = sorted((max(use["start_ms"], other["start_ms"]), min(use["end_ms"], other["end_ms"]))
                for other in occupied if overlap(use, other))
            end = use["start_ms"]
            for left, right in intervals:
                covered += max(0, right - max(left, end))
                end = max(end, right)
        return min(1.0, covered / total) if total else 0.0

    def choose(self, project_id, render_id, disposition):
        from datetime import datetime,timezone
        if disposition not in {"selected","discarded"}:
            raise ValueError("invalid_render_choice")
        with self.db.transaction(immediate=True):
            row=self.db.connection.execute("SELECT id FROM jobs WHERE project_id=? AND json_extract(payload,'$.payload.render_id')=?",
                (str(project_id),str(render_id))).fetchone()
            job=JobRepository(self.db).get(UUID(row[0])) if row else None
            if job is None or job.status not in {JobStatus.COMPLETED,JobStatus.FAILED,JobStatus.CANCELLED}:
                raise SourceUsageConflict("render_choice_requires_finished_output")
            now=datetime.now(timezone.utc).isoformat()
            if disposition=="selected":
                if job.status is not JobStatus.COMPLETED:
                    raise SourceUsageConflict("render_choice_requires_completed_output")
                linked=self._render_run(job)
                if linked is not None and self.review(linked)["state"]=="rejected":
                    raise SourceUsageConflict("rejected_render_cannot_be_selected")
                published=self.db.connection.execute("SELECT 1 FROM publication_records WHERE project_id=? AND output_version=?",
                    (str(project_id),f"render:{render_id}")).fetchone()
                if not published:
                    self.require_available(project_id,self.spec_uses(job.payload.video_spec),
                        run_id=None if linked is None else linked["id"],job_id=job.id)
                for sibling in self._work_renders(job):
                    if sibling.payload.render_id != render_id:
                        self.db.connection.execute("INSERT INTO render_choices VALUES(?,?,'discarded',?) ON CONFLICT(project_id,render_id) DO UPDATE SET disposition='discarded',updated_at=excluded.updated_at",
                            (str(project_id),str(sibling.payload.render_id),now))
            self.db.connection.execute("INSERT INTO render_choices VALUES(?,?,?,?) ON CONFLICT(project_id,render_id) DO UPDATE SET disposition=excluded.disposition,updated_at=excluded.updated_at",
                (str(project_id),str(render_id),disposition,now))
        return {"render_id":str(render_id),"disposition":disposition}

    def _work_renders(self, job):
        linked = self._render_run(job)
        work = None if linked is None else linked["work_id"] or linked["id"]
        result = []
        for row in self.db.connection.execute("SELECT id FROM jobs WHERE project_id=? AND json_extract(payload,'$.type')='render'", (str(job.project_id),)):
            sibling = JobRepository(self.db).get(UUID(row[0]))
            owner = self._render_run(sibling)
            if sibling.id == job.id or work and owner is not None and (owner["work_id"] or owner["id"]) == work:
                result.append(sibling)
        return result

    def choice_impact(self, project_id, render_id):
        row = self.db.connection.execute("SELECT id FROM jobs WHERE project_id=? AND json_extract(payload,'$.payload.render_id')=?", (str(project_id),str(render_id))).fetchone()
        job = JobRepository(self.db).get(UUID(row[0])) if row else None
        if job is None:
            raise SourceUsageConflict("render_choice_requires_finished_output")
        linked = self._render_run(job)
        return dict(render_id=str(render_id), delivery_intent=linked["delivery_intent"] if linked else ProjectRepository(self.db).get(project_id).delivery_intent,
            retained_source_ms=sum(use["end_ms"]-use["start_ms"] for use in self.spec_uses(job.payload.video_spec)),
            discard_render_ids=[str(item.payload.render_id) for item in self._work_renders(job)
                if item.payload.render_id != render_id and self.choice(project_id,item.payload.render_id) != "discarded"],
            published_history_preserved=True)

    def require_run(self, project_id, run_id):
        row = self.db.connection.execute("SELECT visual_source_uses FROM production_runs WHERE id=? AND project_id=?",
            (str(run_id),str(project_id))).fetchone()
        if row:
            self.require_available(project_id, json.loads(row[0]), run_id=run_id)

    def record_render(self, job):
        """Record actual rendered intervals once, not optimistic planned usage."""
        from datetime import datetime, timezone
        from app.db import AssetUsageRepository
        from app.domain.models import AssetUsageEvent
        repository = AssetUsageRepository(self.db)
        spec = job.payload.video_spec
        with self.db.transaction(immediate=True):
            for scene in spec.scenes:
                visual = scene.visual
                if visual.asset_id is None or visual.clip_id is None or self.assets.get(visual.asset_id) is None:
                    continue
                start = visual.clip_start_ms
                duration = (scene.duration_frames*1000*spec.fps.denominator+spec.fps.numerator-1)//spec.fps.numerator
                end = min(visual.clip_end_ms, start+duration)
                version = f"render:{job.payload.render_id}"
                key = f"{job.project_id}:{version}:video:{visual.asset_id}:{visual.clip_id}:{start}:{end}"
                if repository.get_by_event_key(key) is not None:
                    continue
                now = datetime.now(timezone.utc)
                repository.create(AssetUsageEvent(project_id=job.project_id,output_version=version,event_key=key,
                    media_id=visual.asset_id,media_kind="video",clip_id=visual.clip_id,used_at=now,
                    source_start_ms=start,source_end_ms=end))
                clip = self.clips.get(visual.clip_id)
                if clip:
                    self.clips.update(clip.model_copy(update={"used_count":clip.used_count+1,"last_used_at":now}))
