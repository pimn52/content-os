"""Read-only product state and exact-operation preview delivery.

Retained bytes use retained authority, never a new inference grant. Ordinary
media aliases remain closed; this service grants only internal preview review.
"""
import re
from app.db import JobRepository, TalkingSliceSeriesRepository
from app.domain.models import JobStatus
from app.execution_admission import UseAdmissionError
from app.media.importer import MediaImportError
from app.evaluation_talking_execution import require_generation_result
from app.talking.evaluation_admission import EvaluationTalkingAdmissionService
from app.talking.evaluation_admission import require_admission_projection
from app.talking.planned_preview import _source_path


def public_reason(exc):
    if isinstance(exc, UseAdmissionError) and re.fullmatch(r"[a-z][a-z0-9_]{0,150}", str(exc)):
        return str(exc)
    return "evaluation_talking_evidence_invalid"


def _same_status_snapshot(before, after):
    """Read-only status tolerates a running Job's forward heartbeat timestamp.

    Input authority, results, ownership/input/status/attempt and terminal Job
    timestamps remain fenced. Admission/preview snapshots keep exact equality.
    This comparison grants no dispatch or media access.
    """
    if {k:v for k,v in before.items() if k != "jobs"} != {k:v for k,v in after.items() if k != "jobs"}:
        return False
    if len(before["jobs"]) != len(after["jobs"]):
        return False
    for old, new in zip(before["jobs"], after["jobs"]):
        if old == new:
            continue
        if (old is None or new is None or old.status != JobStatus.RUNNING or new.status != JobStatus.RUNNING
            or new.updated_at < old.updated_at
            or old.model_dump(exclude={"updated_at"}) != new.model_dump(exclude={"updated_at"})):
            return False
    return True


class EvaluationTalkingAccess:
    def __init__(self, owner):
        self.owner = owner
        self.admission = EvaluationTalkingAdmissionService(owner.db, owner.root)
        self.admission.owner = owner

    def operation(self, project_id, identity):
        value = self.owner.records.operation(identity)
        if value is None or value.project_id != project_id:
            raise UseAdmissionError("evaluation_talking_operation_missing")
        return value

    def _preview(self, operation):
        series = TalkingSliceSeriesRepository(self.owner.db).get(operation.series_id)
        if series is None:
            raise UseAdmissionError("evaluation_talking_preview_not_completed")
        observed = self.admission._observe(series)
        asset = observed["preview"]
        if observed["snapshot"]["results"]["admission"] is not None:
            require_admission_projection(self.owner, operation, asset)
        scope = self.owner.scopes.get("asset", asset.id)
        origins = self.owner.db.connection.execute(
            "SELECT content_hash FROM execution_use_content_origins WHERE subject_kind='asset' AND subject_id=?",
            (str(asset.id),)).fetchall()
        if [row[0] for row in origins] != [asset.content_hash]:
            raise UseAdmissionError("evaluation_talking_preview_origin_changed")
        # Every same-byte alias restricts access. Additional authority cannot
        # silently enter this exact operation's already retained proof.
        for alias in self.owner.scopes.scopes_for_hash(asset.content_hash):
            if alias.project_id != operation.project_id or alias.purpose != scope.purpose:
                raise UseAdmissionError("evaluation_talking_preview_origin_changed")
            if any(binding not in scope.bindings for binding in alias.bindings):
                raise UseAdmissionError("evaluation_talking_preview_origin_changed")
        path = _source_path(asset.source_file, self.owner.root)
        if self.admission._snapshot(operation) != observed["snapshot"]:
            raise UseAdmissionError("evaluation_talking_authority_changed")
        return asset, path

    def preview(self, project_id, identity, *, purpose=None):
        operation = self.operation(project_id, identity)
        result = dict(operation_id=operation.id, state="blocked", restricted=True,
            purpose="internal_evaluation", reasons=[], asset_id=None, content_hash=None,
            allows_download=False, allows_generation=False, allows_publication=False)
        if purpose != "internal_evaluation":
            result["reasons"] = ["evaluation_scope_not_commercial"]
            return result, None
        try:
            asset, path = self._preview(operation)
            result.update(state="review_allowed", asset_id=asset.id, content_hash=asset.content_hash,
                manifest=self.owner.scopes.manifest("asset", asset.id, content_hash=asset.content_hash))
            return result, path
        except (ValueError, KeyError, TypeError, OSError, MediaImportError) as exc:
            result["reasons"] = [public_reason(exc)]
            return result, None

    def status(self, project_id, identity):
        operation = self.operation(project_id, identity)
        before = self.admission._snapshot(operation)
        stages = {role:"not_created" if job is None else job.status.value
            for role, job in zip(("generation","qa","preview"), before["jobs"])}
        reasons = []
        try:
            self.owner.require_retained_operation(operation)
            for role, job_id in (("generation",operation.generation_job_id),
                                 ("qa",operation.qa_job_id),("preview",operation.preview_job_id)):
                job = JobRepository(self.owner.db).get(job_id)
                if job is not None:
                    self.owner.require_job_identity(job)
                stages[role] = "not_created" if job is None else job.status.value
            if self.owner.records.result(operation.id,"generation") is not None:
                require_generation_result(self.owner,operation,self.owner.records.result(operation.id,"generation"))
            if self.owner.records.result(operation.id,"preview") is not None:
                self._preview(operation)
            if not _same_status_snapshot(before, self.admission._snapshot(operation)):
                raise UseAdmissionError("evaluation_talking_authority_changed")
        except (ValueError, KeyError, TypeError, OSError, MediaImportError) as exc:
            reasons.append(public_reason(exc))
        cancelled = before["results"]["cancellation"] is not None
        state = "cancelled" if cancelled else "blocked" if reasons else "in_progress"
        if not reasons and stages.get("preview") == JobStatus.COMPLETED.value:
            state = "admitted" if before["results"]["admission"] else "awaiting_review"
        if not reasons and any(s in (JobStatus.FAILED.value,JobStatus.CANCELLED.value) for s in stages.values()):
            state = "failed"
        series = TalkingSliceSeriesRepository(self.owner.db).get(operation.series_id)
        review = None if series is None else self.admission.reviews.get_by_series_id(series.id)
        preview_result = before["results"]["preview"]
        use_blockers = [] if preview_result is None else self.admission.policy.blockers(project_id, preview_result["asset_sha256"])
        if state == "admitted" and use_blockers:
            state = "blocked"
        return dict(id=operation.id, project_id=operation.project_id, master_run_id=operation.master_run_id,
            policy=operation.policy,
            quality_observation=(None if reasons or before["results"]["admission"] is None
                else before["results"]["admission"].get("quality_observation")),
            scene_plan_id=operation.selection.scene_plan_id, created_at=operation.created_at,
            state=state, current_eligible=not reasons and not cancelled and not use_blockers, reasons=reasons,
            use_blockers=use_blockers, stages=stages,
            review_policy_version=2, review=None if review is None else review.model_dump(mode="json"),
            talking_run_id=None if reasons or before["results"]["admission"] is None else before["results"]["admission"]["run_id"],
            automatic_retry=False)

    def list_status(self, project_id):
        identities = [row[0] for row in self.owner.db.connection.execute(
            "SELECT id FROM evaluation_talking_operations WHERE project_id=? ORDER BY rowid DESC", (str(project_id),))]
        return [self.status(project_id, identity) for identity in identities]
