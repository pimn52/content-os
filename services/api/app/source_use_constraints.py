"""Explicit avoidance of a retained source/presentation use (D028).

Retained rejection and adopted future-use policy have different authority.
This service never changes source admission or creates execution/review Jobs.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from app.db import AssetRepository, ClipRepository, IPProfileRepository, ProjectRepository
from app.domain.models import CandidateAsset, EditPlanScene, VideoSpec, VisualStyleTokens


class RetainedRejectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manifest_path: str = Field(min_length=1, max_length=2000)
    expected_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_class: Literal["assisted_test", "fixture"]
    confirmed_source: Literal[True]
    reason: str = Field(min_length=1, max_length=2000, pattern=r"\S")
    idempotency_key: str = Field(min_length=1, max_length=500)


class UseConstraintStateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(gt=0)
    enabled: bool
    use_ids: list[str] = Field(default_factory=list, max_length=1000)
    reason: str = Field(min_length=1, max_length=2000, pattern=r"\S")
    idempotency_key: str = Field(min_length=1, max_length=500)


class RetainedHumanRejection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved: Literal[False]
    reviewed_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_reference: str = Field(min_length=1, max_length=2000, pattern=r"\S")
    reviewed_at: AwareDatetime
    scope: str = Field(min_length=1, max_length=2000, pattern=r"\S")
    findings: list[str] = Field(min_length=1, max_length=100)

    @field_validator("findings")
    @classmethod
    def concrete_findings(cls, values):
        if any(not v.strip() for v in values):
            raise ValueError("retained findings must be nonempty")
        return values


class UseConstraintDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate: CandidateAsset
    state: Literal["excluded", "unresolved"]
    constraint_id: UUID
    version: int
    use_id: str
    evidence_refs: tuple[str, ...]
    reason: str


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def presentation(entry: EditPlanScene, *, width: int, height: int,
                 style: VisualStyleTokens, visual=None, captions=None, transition="cut") -> dict | None:
    """Normalize only reconstructable effective visual fields, not record IDs.

    Preliminary timed captions have no measured tracks: explicitly unresolved.
    Crop geometry is not contained in EditPlan: do not invent it.
    """
    if (entry.burned_in_subtitles == "unknown" or entry.framing_policy.value != "face_safe_contain"
        or transition != "cut"):
        return None
    if visual is not None and (visual.vertical_reframe_mode.value != "contain" or visual.source_bottom_crop_ratio):
        return None
    if entry.subtitle_treatment.value == "timed_captions" and not captions:
        return None
    tokens = style.model_dump(mode="json")
    tokens.pop("schema_version", None)
    tokens.pop("name", None)  # Label does not change pixels.
    for key in ("background_color", "foreground_color", "accent_color"):
        tokens[key] = tokens[key].lower()
    return {"interpretation": "source_presentation_v1", "width": width, "height": height,
            "visual_role": entry.visual_role.value, "framing_policy": entry.framing_policy.value,
            "portrait_presentation": entry.portrait_presentation.value, "reframe": "contain", "bottom_crop": 0,
            "burned_in_subtitles": entry.burned_in_subtitles, "subtitle_treatment": entry.subtitle_treatment.value,
            "captions": [] if entry.subtitle_treatment.value == "none" else captions,
            "graphic_treatment": entry.graphic_treatment.value, "graphic_text": entry.graphic_text,
            "transition": transition, "style": tokens}


class SourceUseConstraintService:
    rule = "avoid_retained_source_presentation_v1"

    def __init__(self, db, data_root=None):
        self.db = db
        self.root = Path(data_root or getattr(db, "data_root", Path(db.path).parent)).resolve()

    def _path(self, value: str) -> Path:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("use_constraint_file_reference_required")
        path = Path(value)
        path = (path if path.is_absolute() else self.root / path).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise ValueError("use_constraint_file_outside_root_or_missing")
        return path

    def _hash(self, value: str) -> str:
        with self._path(value).open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()

    def _project(self, project_id):
        project = ProjectRepository(self.db).get(project_id)
        if project is None:
            raise LookupError("project not found")
        return project

    def _profile_version(self, creator_id):
        revisions = IPProfileRepository(self.db).revisions(creator_id)
        if not revisions:
            raise ValueError("use_constraint_profile_revision_missing")
        return revisions[-1][0]

    def _replay(self, project_id, key, request):
        row = self.db.connection.execute("SELECT request,response FROM source_use_constraint_requests WHERE project_id=? AND idempotency_key=?", (str(project_id), key)).fetchone()
        if row is None:
            return None
        if json.loads(row["request"]) != request:
            raise ValueError("use_constraint_idempotency_conflict")
        response = json.loads(row["response"])
        if response["creator_id"] != str(self._project(project_id).ip_profile_id):
            raise LookupError("source use constraint not found")
        return response

    def _record_request(self, project_id, key, request, response):
        self.db.connection.execute("INSERT INTO source_use_constraint_requests VALUES (?,?,?,?)",
                                   (str(project_id), key, json.dumps(request), json.dumps(response)))

    def _save(self, value):
        payload = json.dumps(value, sort_keys=True, ensure_ascii=False)
        self.db.connection.execute("UPDATE source_use_constraints SET payload=? WHERE id=?", (payload, value["id"]))
        self.db.connection.execute("INSERT INTO source_use_constraint_versions VALUES (?,?,?)", (value["id"], value["version"], payload))

    def retained_choices(self, project_id):
        """Bounded read-only discovery, not confirmation, intake or adoption.

        Only the configured evaluation-evidence directory is searched; malformed,
        positive and other-project documents cannot become selectable rejection.
        Intake still independently verifies render/source identity and hashes.
        """
        project = self._project(project_id)
        directory = (self.root / "evaluation-evidence").resolve()
        if not directory.is_relative_to(self.root) or not directory.is_dir():
            return []
        choices = []
        for path in islice(directory.rglob("manifest.json"), 500):
            try:
                path = self._path(str(path))
                with path.open("rb") as stream:
                    original = stream.read(2_000_001)
                if len(original) > 2_000_000:
                    continue
                raw = json.loads(original)
                if not isinstance(raw, dict) or raw.get("project_id") != str(project.id):
                    continue
                review = RetainedHumanRejection.model_validate(raw.get("human_review"))
                choices.append({"manifest_path": path.relative_to(self.root).as_posix(),
                    "expected_manifest_sha256": hashlib.sha256(original).hexdigest(),
                    "original_review": review.model_dump(mode="json"),
                    "evidence_level": "unconfirmed_retained_document_not_policy_or_qa"})
            except (ValueError, OSError):
                continue
        return sorted(choices, key=lambda value: value["manifest_path"])

    def intake(self, project_id, request: RetainedRejectionRequest):
        with self.db.transaction(immediate=True):
            project = self._project(project_id)
            action = {"action": "intake", **request.model_dump(mode="json")}
            replay = self._replay(project_id, request.idempotency_key, action)
            if replay is not None:
                return replay
            path = self._path(request.manifest_path)
            with path.open("rb") as stream:
                original = stream.read(2_000_001)
            if len(original) > 2_000_000 or hashlib.sha256(original).hexdigest() != request.expected_manifest_sha256:
                raise ValueError("use_constraint_manifest_size_or_hash_changed")
            raw = json.loads(original)
            if not isinstance(raw, dict) or not {"human_review", "video_spec", "project_id", "sha256", "media"}.issubset(raw):
                raise ValueError("use_constraint_manifest_incomplete")
            review = RetainedHumanRejection.model_validate(raw["human_review"])
            spec = VideoSpec.model_validate(raw["video_spec"])
            if (UUID(str(raw["project_id"])) != project.id or spec.project_id != project.id
                or raw["sha256"] != review.reviewed_sha256 or self._hash(raw["media"]) != review.reviewed_sha256):
                raise ValueError("use_constraint_render_identity_mismatch")
            if spec.edit_plan is None or raw.get("edit_plan") != raw["video_spec"].get("edit_plan"):
                raise ValueError("use_constraint_retained_plan_missing_or_mixed")
            entries = {e.scene_id: e for e in spec.edit_plan.scenes}
            if set(entries) != {s.scene_id for s in spec.scenes}:
                raise ValueError("use_constraint_retained_scene_mapping_mismatch")
            uses = []
            for scene in spec.scenes:
                entry = entries[scene.scene_id]
                visual = scene.visual
                if entry.selected_clip_id is None:
                    continue
                asset = AssetRepository(self.db).get(entry.selected_asset_id)
                clip = ClipRepository(self.db).get(entry.selected_clip_id)
                if (asset is None or clip is None or clip.asset_id != asset.id
                    or visual.asset_id != asset.id or visual.clip_id != clip.id
                    or entry.selected_source_kind != asset.source_kind or visual.source_kind != asset.source_kind
                    or visual.clip_start_ms is None or visual.clip_end_ms is None
                    or not clip.start_ms <= visual.clip_start_ms < visual.clip_end_ms <= clip.end_ms
                    or self._hash(asset.source_file) != asset.content_hash):
                    raise ValueError("use_constraint_source_identity_mismatch")
                for field in ("visual_role", "framing_policy", "portrait_presentation", "subtitle_treatment", "graphic_treatment", "graphic_text"):
                    if getattr(scene, field) != getattr(entry, field):
                        raise ValueError("use_constraint_retained_treatment_mismatch")
                treated = presentation(entry, width=spec.width, height=spec.height, style=scene.style_tokens,
                                       visual=visual, captions=[c.model_dump(mode="json") for c in scene.captions], transition=scene.transition)
                use = {"scene_id": scene.scene_id, "source_asset_id": str(asset.id), "source_clip_id": str(clip.id),
                       "source_label": Path(asset.source_file).name,
                       "source_hash": asset.content_hash, "start_ms": visual.clip_start_ms, "end_ms": visual.clip_end_ms,
                       "presentation": treated, "scope_authority": "proposed_future_use_not_independent_scene_rejection"}
                use["use_id"] = digest({"scene": scene.scene_id, "hash": asset.content_hash, "start": use["start_ms"], "end": use["end_ms"], "presentation": treated})
                uses.append(use)
            if not uses:
                raise ValueError("use_constraint_no_continuous_source_uses")
            value = {"id": str(uuid4()), "project_id": str(project.id), "creator_id": str(project.ip_profile_id),
                     "profile_version": self._profile_version(project.ip_profile_id), "version": 1, "state": "candidate",
                     "rule": self.rule, "classification": "configuration_use_limit",
                     "evidence_class": "fixture" if raw.get("evidence_class") == "fixture" else request.evidence_class,
                     "manifest_path": path.relative_to(self.root).as_posix(), "manifest_sha256": request.expected_manifest_sha256,
                     "media_path": self._path(raw["media"]).relative_to(self.root).as_posix(), "render_sha256": review.reviewed_sha256,
                     "original_manifest": original.decode("utf-8"), "original_review": raw["human_review"],
                     "original_spec_sha256": digest(raw["video_spec"]), "compatibility_interpretation": "videospec_0.1_defaults_v1",
                     "uses": uses, "adopted_use_ids": [], "intake_reason": request.reason, "reason": "candidate; explicit scope adoption required",
                     "intake_authority": "explicit_product_confirmation_of_retained_negative_evidence",
                     "created_at": datetime.now(timezone.utc).isoformat(),
                     "evidence_level": "retained_rejection_and_adopted_policy_not_positive_media_qa"}
            self.db.connection.execute("INSERT INTO source_use_constraints VALUES (?,?,?,?)", (value["id"], value["creator_id"], value["project_id"], json.dumps(value)))
            self._save(value)
            self._record_request(project_id, request.idempotency_key, action, value)
            return value

    def _reasons(self, value, use_ids=None):
        reasons = []
        if value["evidence_class"] == "fixture":
            reasons.append("use_constraint_fixture_not_policy_authority")
        if value["profile_version"] != self._profile_version(UUID(value["creator_id"])):
            reasons.append("use_constraint_profile_revision_changed")
        origin = ProjectRepository(self.db).get(UUID(value["project_id"]))
        if origin is None or str(origin.ip_profile_id) != value["creator_id"]:
            reasons.append("use_constraint_origin_owner_changed")
        try:
            if self._hash(value["manifest_path"]) != value["manifest_sha256"] or self._hash(value["media_path"]) != value["render_sha256"]:
                reasons.append("use_constraint_retained_evidence_changed")
            selected_ids = use_ids if use_ids is not None else value["adopted_use_ids"] if value["state"] == "enabled" else None
            for use in value["uses"]:
                if selected_ids is not None and use["use_id"] not in selected_ids:
                    continue
                asset = AssetRepository(self.db).get(UUID(use["source_asset_id"]))
                clip = ClipRepository(self.db).get(UUID(use["source_clip_id"]))
                if (asset is None or clip is None or clip.asset_id != asset.id
                    or not clip.start_ms <= use["start_ms"] < use["end_ms"] <= clip.end_ms
                    or asset.content_hash != use["source_hash"] or self._hash(asset.source_file) != use["source_hash"]):
                    reasons.append("use_constraint_source_evidence_changed")
        except (ValueError, OSError):
            reasons.append("use_constraint_evidence_unavailable")
        return list(dict.fromkeys(reasons))

    def records(self, project_id):
        project = self._project(project_id)
        rows = self.db.connection.execute("SELECT payload FROM source_use_constraints WHERE creator_id=? ORDER BY rowid", (str(project.ip_profile_id),)).fetchall()
        values = [json.loads(row["payload"]) for row in rows]
        return [{**v, "current_stop_reasons": self._reasons(v) if v["state"] != "disabled" else []} for v in values]

    def state(self, project_id, constraint_id, request: UseConstraintStateRequest):
        with self.db.transaction(immediate=True):
            self._project(project_id)
            action = {"action": "state", "constraint_id": str(constraint_id), **request.model_dump(mode="json")}
            replay = self._replay(project_id, request.idempotency_key, action)
            if replay is not None:
                return replay
            value = next((v for v in self.records(project_id) if v["id"] == str(constraint_id)), None)
            if value is None:
                raise LookupError("source use constraint not found")
            if value["version"] != request.expected_version:
                raise ValueError("use_constraint_version_conflict_reload")
            if request.enabled:
                reasons = self._reasons(value, request.use_ids)
                selected = {u["use_id"]: u for u in value["uses"] if u["presentation"] is not None}
                if reasons:
                    raise ValueError(";".join(reasons))
                if not request.use_ids or len(set(request.use_ids)) != len(request.use_ids) or not set(request.use_ids).issubset(selected):
                    raise ValueError("use_constraint_explicit_reconstructable_scope_required")
            elif request.use_ids:
                raise ValueError("use_constraint_disable_has_no_new_scope")
            value.pop("current_stop_reasons")
            value.update(state="enabled" if request.enabled else "disabled", version=value["version"] + 1,
                         reason=request.reason, adopted_use_ids=sorted(request.use_ids) if request.enabled else value["adopted_use_ids"])
            self._save(value)
            self._record_request(project_id, request.idempotency_key, action, value)
            return value

    def history(self, project_id, constraint_id):
        if not any(v["id"] == str(constraint_id) for v in self.records(project_id)):
            raise LookupError("source use constraint not found")
        return [json.loads(row["payload"]) for row in self.db.connection.execute("SELECT payload FROM source_use_constraint_versions WHERE constraint_id=? ORDER BY version", (str(constraint_id),))]

    @staticmethod
    def context_from_records(records):
        # Include tombstones and held evidence so disable/change fences pending plans.
        return [{k: v[k] for k in ("id", "version", "state", "profile_version", "adopted_use_ids", "current_stop_reasons")}
                for v in records]

    def context(self, project_id):
        return self.context_from_records(self.records(project_id))

    def evaluate(self, candidate, treated, records):
        if candidate.asset_id is None or candidate.clip_id is None:
            return ()
        asset, clip = AssetRepository(self.db).get(candidate.asset_id), ClipRepository(self.db).get(candidate.clip_id)
        if asset is None or clip is None or clip.asset_id != asset.id:
            return ()  # Existing selection/admission gates own missing material.
        return self._evaluate_use(candidate, asset.content_hash, clip.start_ms, clip.end_ms, treated, records)

    def _evaluate_use(self, candidate, source_hash, start, end, treated, records):
        results = []
        for record in records:
            if record["state"] != "enabled":
                continue
            for use in record["uses"]:
                if (use["use_id"] not in record["adopted_use_ids"] or use["source_hash"] != source_hash
                    or (use["start_ms"], use["end_ms"]) != (start, end)):
                    continue
                held = record["current_stop_reasons"]
                if not held and treated is not None and use["presentation"] != treated:
                    continue
                state = "unresolved" if held or treated is None else "excluded"
                results.append(UseConstraintDecision(candidate=candidate, state=state, constraint_id=record["id"],
                    version=record["version"], use_id=use["use_id"],
                    evidence_refs=(f"source_use_constraint:{record['id']}:v{record['version']}", record["original_review"]["evidence_reference"]),
                    reason=";".join(held) if held else "use_constraint_treatment_unresolved" if state == "unresolved" else "adopted_source_presentation_use_excluded"))
        return tuple(results)

    def snapshot_render(self, project_id, job_id):
        self.db.connection.execute("INSERT INTO render_use_constraint_snapshots VALUES (?,?)", (str(job_id), digest(self.context(project_id))))

    def require_render_snapshot(self, job):
        row = self.db.connection.execute("SELECT fingerprint FROM render_use_constraint_snapshots WHERE job_id=?", (str(job.id),)).fetchone()
        if row is None:
            return  # This seam does not claim migrated legacy/direct Job coverage.
        if row["fingerprint"] != digest(self.context(job.project_id)):
            raise ValueError("use_constraint_render_plan_changed_replan")
