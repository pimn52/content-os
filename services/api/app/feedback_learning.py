"""Explicit creator-scoped presentation preferences, never automatic admission."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.assembly.edit_plan import _normalized
from app.db import Database, IPProfileRepository, JobRepository, ProjectDraftRepository, ProjectRepository
from app.domain.models import JobStatus, RenderVideoJobPayload, ScenePlan


class PreferenceCandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    observation_id: UUID
    classification: Literal["asset_defect", "configuration_limit", "creator_preference"]


class PreferenceStateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(gt=0)
    enabled: bool
    reason: str = Field(min_length=1, max_length=2000, pattern=r"\S")


class CreatorPreferenceSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_profile_version: int = Field(gt=0)
    rule: Literal["semantic_graphic_not_full_copy_v1"]
    reason: str = Field(min_length=1, max_length=2000, pattern=r"\S")


def concise_emphasis(scene: ScenePlan) -> ScenePlan:
    """Select an existing semantic alternative, never invent or truncate copy."""
    if not scene.caption_emphasis or _normalized(scene.caption_emphasis[0]) != _normalized(scene.voice_text):
        return scene
    alternatives = [text for text in scene.caption_emphasis[1:] if _normalized(text)
                    and len(text) <= 120 and _normalized(text) != _normalized(scene.voice_text)]
    if not alternatives:
        return scene  # Unknown semantic alternative remains subject to normal preflight.
    return scene.model_copy(update={"caption_emphasis": alternatives})


class FeedbackLearningService:
    rule = "semantic_graphic_not_full_copy_v1"

    @staticmethod
    def _spec_hash(job):
        document = json.dumps(job.payload.video_spec.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(document.encode()).hexdigest()

    def __init__(self, db: Database, render_root: Path):
        self.db, self.render_root = db, Path(render_root).resolve()

    def _project(self, project_id):
        project = ProjectRepository(self.db).get(project_id)
        if project is None:
            raise LookupError("project not found")
        return project

    def _profile_version(self, creator_id):
        revisions = IPProfileRepository(self.db).revisions(creator_id)
        if not revisions:
            raise ValueError("preference_profile_revision_missing")
        return revisions[-1][0]

    def observations(self, project_id):
        self._project(project_id)
        rows = self.db.connection.execute("SELECT payload FROM presentation_observations WHERE project_id=? ORDER BY rowid", (str(project_id),)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def selection_scope(self, project_id):
        project = self._project(project_id)
        version = self._profile_version(project.ip_profile_id)
        baseline = self.db.connection.execute("SELECT provenance FROM ip_profile_revision_baselines WHERE profile_id=? AND version=?", (str(project.ip_profile_id), version)).fetchone()
        return {"creator_id": str(project.ip_profile_id), "profile_version": version,
                "rule": self.rule, "profile_revision_origin": baseline["provenance"] if baseline else "recorded_profile_revision"}

    def select(self, project_id, request: CreatorPreferenceSelectionRequest):
        """Creator authority is sufficient for preference, never for media QA."""
        with self.db.transaction(immediate=True):
            scope = self.selection_scope(project_id)
            if request.expected_profile_version != scope["profile_version"]:
                raise ValueError("preference_profile_revision_changed")
            row = self.db.connection.execute("SELECT payload FROM planning_preferences WHERE creator_id=? AND observation_id IS NULL", (scope["creator_id"],)).fetchone()
            if row:
                value = json.loads(row["payload"])
                if value["profile_version"] == scope["profile_version"]:
                    return value  # Replay never silently re-enables a disabled rule.
                value.update(profile_version=scope["profile_version"], state="candidate", version=value["version"] + 1,
                             profile_revision_origin=scope["profile_revision_origin"], reason=request.reason, selection_reason=request.reason)
                self._save(value)
                return value
            value = {"id": str(uuid4()), **scope, "version": 1, "state": "candidate",
                     "authority_source": "creator_selection", "observation_id": None, "observation": None,
                     "classification": "creator_preference", "retained_case": None, "stop_reasons": [],
                     "reason": request.reason, "selection_reason": request.reason,
                     "instruction": "Select an existing semantic short emphasis instead of a first emphasis equal to the full voice copy. Do not invent or truncate speech. Missing alternatives still use normal preflight.",
                     "evidence_level": "explicit_preference_not_observed_quality_evidence"}
            self.db.connection.execute("INSERT INTO planning_preferences VALUES (?,?,?,?,?)", (value["id"], value["creator_id"], None, value["classification"], json.dumps(value)))
            self._save(value)
            return value

    def _evidence(self, observation):
        job = JobRepository(self.db).get(UUID(observation["render_job_id"]))
        if (job is None or job.project_id != UUID(observation["project_id"]) or job.status != JobStatus.COMPLETED
                or not isinstance(job.payload, RenderVideoJobPayload)):
            raise ValueError("preference_render_evidence_missing")
        path = self.render_root / str(job.project_id) / f"{job.payload.render_id}.mp4"
        if not path.resolve().is_relative_to(self.render_root) or not path.is_file():
            raise ValueError("preference_render_evidence_missing")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != observation["request"]["render_sha256"]:
            raise ValueError("preference_render_evidence_changed")
        return job

    def _save(self, value):
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False)
        self.db.connection.execute("UPDATE planning_preferences SET payload=? WHERE id=?", (encoded, value["id"]))
        self.db.connection.execute("INSERT INTO planning_preference_versions VALUES (?,?,?)", (value["id"], value["version"], encoded))

    def candidate(self, project_id, request: PreferenceCandidateRequest):
        with self.db.transaction(immediate=True):
            project = self._project(project_id)
            observation = next((o for o in self.observations(project_id) if o["id"] == str(request.observation_id)), None)
            if observation is None:
                raise LookupError("presentation observation not found")
            existing = self.db.connection.execute("SELECT payload FROM planning_preferences WHERE observation_id=? AND classification=?", (str(request.observation_id), request.classification)).fetchone()
            if existing:
                return json.loads(existing["payload"])
            job = self._evidence(observation)
            retained = next((s for s in job.payload.video_spec.scenes if s.scene_id == observation["request"]["scene_id"]), None)
            reasons = []
            if request.classification != "creator_preference":
                reasons.append("asset_or_configuration_finding_not_creator_default")
            if observation["request"]["finding"] != "duplicate_text":
                reasons.append("finding_requires_separate_scoped_rule")
            caption = "" if retained is None else "".join(c.text for c in retained.captions) or retained.caption or ""
            if (retained is None or retained.subtitle_treatment.value != "timed_captions"
                    or not caption or not retained.graphic_text or _normalized(retained.graphic_text) != _normalized(caption)):
                reasons.append("retained_case_does_not_show_full_copy_duplication")
            draft = ProjectDraftRepository(self.db).get(project_id)
            source = None if draft is None else next((s for s in draft.scenes if s.scene_id == observation["request"]["scene_id"]), None)
            regression = None
            if source is None or _normalized(source.voice_text) != _normalized(caption):
                reasons.append("retained_scene_copy_missing_or_changed")
            else:
                # The observed failed graphic is the retained counterexample.
                before = source.model_copy(update={"caption_emphasis": [retained.graphic_text or caption, *source.caption_emphasis]})
                after = concise_emphasis(before)
                regression = {"before": before.model_dump(mode="json"), "after": after.model_dump(mode="json"),
                              "passed": before.caption_emphasis != after.caption_emphasis and before.voice_text == after.voice_text}
                if not regression["passed"]:
                    reasons.append("retained_case_has_no_safe_semantic_alternative")
            value = {"id": str(uuid4()), "creator_id": str(project.ip_profile_id),
                     "profile_version": self._profile_version(project.ip_profile_id), "observation_id": observation["id"],
                     "classification": request.classification, "authority_source": "render_observation", "rule": self.rule, "version": 1, "state": "candidate",
                     "observation": observation, "render_spec_sha256": self._spec_hash(job),
                     "retained_case": regression, "stop_reasons": reasons,
                     "reason": "candidate only; explicit adoption required",
                     "instruction": "For this creator, supply a semantic short emphasis alternative. Select an existing short alternative instead of a first emphasis equal to the full voice copy; never invent, truncate or change speech. Missing alternatives still fail normal preflight.",
                     "evidence_level": "persisted_observation_not_runtime_quality_proof"}
            self.db.connection.execute("INSERT INTO planning_preferences VALUES (?,?,?,?,?)", (value["id"], value["creator_id"], observation["id"], request.classification, json.dumps(value)))
            self._save(value)
            return value

    def _current_reasons(self, value):
        reasons = list(value["stop_reasons"])
        if value["profile_version"] != self._profile_version(UUID(value["creator_id"])):
            reasons.append("preference_profile_revision_changed")
        if value.get("authority_source") == "creator_selection":
            return reasons
        try:
            job = self._evidence(value["observation"])
            if self._spec_hash(job) != value["render_spec_sha256"]:
                reasons.append("preference_render_spec_changed")
            case = value["retained_case"]
            if case:
                scene = next((s for s in job.payload.video_spec.scenes if s.scene_id == case["before"]["scene_id"]), None)
                if scene is None or _normalized(scene.graphic_text or "") != _normalized(case["before"]["voice_text"]):
                    reasons.append("preference_retained_case_changed")
        except ValueError as exc:
            reasons.append(str(exc))
        return list(dict.fromkeys(reasons))

    def list(self, project_id):
        project = self._project(project_id)
        rows = self.db.connection.execute("SELECT payload FROM planning_preferences WHERE creator_id=? ORDER BY rowid", (str(project.ip_profile_id),)).fetchall()
        return [{**json.loads(r["payload"]), "current_stop_reasons": self._current_reasons(json.loads(r["payload"]))} for r in rows]

    def set_state(self, project_id, preference_id, request: PreferenceStateRequest):
        with self.db.transaction(immediate=True):
            values = self.list(project_id)
            value = next((v for v in values if v["id"] == str(preference_id)), None)
            if value is None:
                raise LookupError("creator preference not found")
            if value["version"] != request.expected_version:
                raise ValueError("preference_version_conflict_reload")
            if request.enabled and value["current_stop_reasons"]:
                raise ValueError(";".join(value["current_stop_reasons"]))
            if request.enabled and value.get("authority_source") != "creator_selection" and any(
                    v["state"] == "enabled" and v["rule"] == value["rule"]
                    and v.get("authority_source") == "creator_selection" and not v["current_stop_reasons"] for v in values):
                raise ValueError("explicit_creator_selection_has_priority_disable_it_first")
            if request.enabled:
                for other in values:
                    if other["id"] != value["id"] and other["rule"] == value["rule"] and other["state"] == "enabled":
                        other.pop("current_stop_reasons")
                        other.update(state="disabled", version=other["version"] + 1, reason=f"superseded by {value['id']}")
                        self._save(other)
            value.pop("current_stop_reasons")
            value.update(state="enabled" if request.enabled else "disabled", version=value["version"] + 1, reason=request.reason)
            self._save(value)
            return value

    def active(self, project_id):
        project = self._project(project_id)
        rows = self.db.connection.execute("SELECT payload FROM planning_preferences WHERE creator_id=? ORDER BY rowid", (str(project.ip_profile_id),)).fetchall()
        values = [json.loads(r["payload"]) for r in rows]
        return [v for v in values if v["state"] == "enabled" and not self._current_reasons(v)]

    def history(self, project_id, preference_id):
        if not any(v["id"] == str(preference_id) for v in self.list(project_id)):
            raise LookupError("creator preference not found")
        rows = self.db.connection.execute("SELECT payload FROM planning_preference_versions WHERE preference_id=? ORDER BY version", (str(preference_id),)).fetchall()
        return [json.loads(r["payload"]) for r in rows]


def apply_preferences(scenes, preferences):
    result = []
    for scene in scenes:
        changed = scene
        for preference in preferences:
            if preference["rule"] == FeedbackLearningService.rule:
                candidate = concise_emphasis(changed)
                if candidate != changed:
                    ref = f"planning_preference:{preference['id']}:v{preference['version']}"
                    changed = candidate.model_copy(update={"evidence_refs": list(dict.fromkeys([*candidate.evidence_refs, ref]))})
        result.append(changed)
    return tuple(result)
