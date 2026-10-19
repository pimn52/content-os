"""A bounded normal Run consumer of completed, unchanged evaluation narration."""
from datetime import datetime, timezone
import hashlib
import json
from uuid import UUID, uuid4

from app.db import AudioAssetRepository, JobRepository, ProjectDraftRepository, ProjectRepository
from app.db.evaluation_master import EvaluationMasterRepository
from app.domain.evaluation_master import (POLICY, OPERATION, EvaluationMasterUseReviewV1,
    EvaluationMasterUseAdmission, EvaluationRunMasterBindingV1, MasterSelection)
from app.domain.models import execution_canonical_sha256 as digest
from app.execution_admission import ProviderUseAdmissionService, UseAdmissionError, configured_admission_actor
from app.execution_scope import ExecutionScopeService, _make_scope, read_job_scope, read_subject_scope, job_use_identity
from app.production_preflight import build_production_preflight
from app.voice_evaluation_review import EvaluationVoiceReviewService

STAGE = "evaluation_master"
DIMENSIONS = ("likeness", "naturalness", "emphasis", "pace", "pauses", "rhythm")
# These checks concern later execution, never current media/plan suitability.
DEFERRED = {"unknown_cost_requires_budget_authorization", "budget_call_limit", "budget_amount_limit",
    "provider_license_scope_unverified", "voice_capability_not_verified", "talking_capability_not_verified"}
DOWNSTREAM = ("evaluation_talking_execution_not_integrated", "evaluation_render_execution_not_integrated")


def _same_composed_copy(saved, target):
    from app.voice_qa import comparison_tokens
    # Ignore paragraph formatting, but keep punctuation and spoken word breaks.
    return ("".join(saved.split()) == "".join(target.split())
            and comparison_tokens(saved) == comparison_tokens(target))


def _master_stage_blockers(context):
    # Binding approved narration does not dispatch or approve future footage.
    future_footage = {f"source_suitability_unknown:{s.scene_id}" for s in context["plan"].scenes
        if s.production_need == "none" and s.selected_candidate in context["draft"].confirmed
        and s.planned_source_kind.value in {"user_asset", "historical_asset"}}
    return [r for r in context["plan"].stop_reasons
        if r not in DEFERRED and r not in future_footage and not r.startswith("talking_source_not_admitted:")]


def is_evaluation_master_run(db, run_id):
    from app.evaluation_composition import is_composition
    if is_composition(db,run_id=run_id):
        return True  # All ordinary downstream routes stay closed to this lane.
    row = db.connection.execute("SELECT stage FROM production_runs WHERE id=?", (str(run_id),)).fetchone()
    return bool(row and row[0] == STAGE or db.connection.execute(
        "SELECT 1 FROM evaluation_run_master_bindings WHERE run_id=?", (str(run_id),)).fetchone()
        or (scope := read_subject_scope(db, "run", run_id)) and scope.purpose == "internal_evaluation")


def require_ordinary_run(db, run_id):
    if is_evaluation_master_run(db, run_id):
        raise UseAdmissionError("evaluation_master_downstream_operation_not_supported")


class EvaluationMasterService:
    def __init__(self, db, root):
        self.db, self.root = db, root
        self.records = EvaluationMasterRepository(db)
        self.reviews = EvaluationVoiceReviewService(db, root)
        self.scopes = ExecutionScopeService(db, root)
        self.admissions = ProviderUseAdmissionService(db, root)

    @staticmethod
    def _fingerprint(context, receipt):
        # The retained request already binds copy, draft, presentation and all
        # Voice/QA/human/origin proof. Future provider/budget configuration is
        # fenced during reads but cannot invalidate this completed-use stage.
        return digest(dict(fingerprint_policy="evaluation-master-proof-v2",
            request=context["request_sha256"], receipt=receipt.model_dump(mode="json")))

    def _facts(self, project_id, audio_id):
        audio, claim = self.reviews._context(project_id, audio_id)
        project = ProjectRepository(self.db).get(project_id)
        draft = ProjectDraftRepository(self.db).get(project_id)
        return (self.reviews._facts(audio, claim),
            None if project is None else project.model_dump_json(),
            None if draft is None else draft.model_dump_json(),
            # The normal router reads this library/configuration. Fence its DB
            # dependencies too, so an asset/avoidance change cannot race a bind.
            tuple((table, tuple(tuple(row) for row in self.db.connection.execute(
                f"SELECT * FROM {table} ORDER BY rowid"))) for table in (
                    "assets", "clips", "image_assets", "audio_assets", "source_use_constraints",
                    "provider_machine_capability_profiles", "provider_machine_settings",
                    "budget_policies", "provider_call_records", "talking_runs")))

    def _context(self, project_id, selection):
        if (selection.policy != POLICY or selection.purpose != "internal_evaluation"
            or type(selection.talking_review_policy_version) is not int
            or selection.talking_review_policy_version not in (1, 2)):
            raise UseAdmissionError("evaluation_master_use_policy_unsupported")
        before = self._facts(project_id, selection.audio_id)
        state = self.reviews.status(project_id, selection.audio_id)
        if not state["current_eligible"]:
            raise UseAdmissionError(state["reasons"][0])
        record = self.reviews.records.get_by_audio(selection.audio_id)
        if (record is None or not record.review.approved
            or any(getattr(record.review, key).value != "pass" for key in DIMENSIONS)):
            raise UseAdmissionError("evaluation_master_human_approval_required")
        audio, claim = self.reviews._context(project_id, selection.audio_id)
        project, draft = ProjectRepository(self.db).get(project_id), ProjectDraftRepository(self.db).get(project_id)
        plan = build_production_preflight(self.db, project, draft, style_tokens=selection.style_tokens,
            repair_allowance=0)
        copy = draft.script or "\n\n".join(s.voice_text for s in sorted(draft.scenes, key=lambda s: s.order))
        producer_id = audio.metadata["voice_generation"].get("job_id")
        producer = JobRepository(self.db).get(UUID(producer_id)) if producer_id else None
        source_ids = claim.input_binding.composition_source_take_audio_ids
        if producer is None:
            # Completed QA already reopens these exact composed sources and
            # their original permissions. Do not invent a single producer Job.
            if producer_id or not source_ids or audio.metadata["voice_generation"].get("provider") != "composed_voice_takes":
                raise UseAdmissionError("evaluation_master_origin_missing")
            if not _same_composed_copy(copy, state["target_text"]):
                raise UseAdmissionError("evaluation_master_copy_mismatch")
        elif copy != state["target_text"] or copy != producer.payload.text:
            raise UseAdmissionError("evaluation_master_copy_mismatch")
        if producer and draft.narration_performance_plan and getattr(producer.payload, "speech_text", None) not in (None, copy):
            raise UseAdmissionError("evaluation_master_transform_not_supported")
        values = list(self.scopes.scopes_for_hash(audio.content_hash))
        origin_scopes = [self.scopes.get("audio", audio.id),
                         read_job_scope(self.db, JobRepository(self.db).get(claim.job_id))]
        if producer:
            origin_scopes.insert(1, read_job_scope(self.db, producer))
        for scope in origin_scopes:
            if scope is None:
                raise UseAdmissionError("evaluation_master_origin_missing")
            values.append(scope)
        bindings = {}
        for scope in values:
            if scope.project_id != project_id or scope.purpose != "internal_evaluation":
                raise UseAdmissionError("evaluation_master_origin_unsupported")
            for binding in scope.bindings:
                if binding.admission_id in bindings and bindings[binding.admission_id] != binding:
                    raise UseAdmissionError("evaluation_master_origin_changed")
                bindings[binding.admission_id] = binding
        scope = _make_scope(project_id, "internal_evaluation", tuple(bindings[key] for key in sorted(bindings, key=str)))
        proof = dict(audio_id=str(audio.id), audio_sha256=audio.content_hash,
            duration_ms=audio.duration_ms, start_ms=0, end_ms=audio.duration_ms,
            audio_format=dict(sample_rate_hz=audio.sample_rate, channels=audio.channels),
            producer_job_id=None if producer is None else str(producer.id),
            producer_job_sha256=None if producer is None else job_use_identity(producer),
            qa=record.binding(), human_record_id=str(record.id), human_record_sha256=digest(record.model_dump(mode="json")),
            origins=[s.model_dump(mode="json") for s in sorted({s.fingerprint: s for s in values}.values(), key=lambda s: s.fingerprint)],
            origin_membership=[list(row) for row in self.db.connection.execute(
                "SELECT subject_kind,subject_id FROM execution_use_content_origins WHERE content_hash=? ORDER BY subject_kind,subject_id", (audio.content_hash,))],
            current_source_facts_sha256=digest(before[0]))
        if producer is None:
            proof["composition_source_take_audio_ids"] = [str(identity) for identity in source_ids]
        request = dict(policy=POLICY, purpose="internal_evaluation", project=project.model_dump(mode="json"),
            draft=draft.model_dump(mode="json"), copy=copy, copy_sha256=hashlib.sha256(copy.encode()).hexdigest(),
            style_tokens=selection.style_tokens.model_dump(mode="json"),
            talking_review_policy_version=selection.talking_review_policy_version, proof=proof,
            preliminary_edit_plan=None if plan.preliminary_edit_plan is None else plan.preliminary_edit_plan.model_dump(mode="json"))
        if before != self._facts(project_id, audio.id):
            raise UseAdmissionError("evaluation_master_authority_changed")
        return dict(audio=audio, draft=draft, plan=plan, proof=proof, request=request,
            request_sha256=digest(request), scope=scope, facts=before)

    def _report(self, reference, sha):
        raw = self.admissions._file(reference, maximum=150_000)
        if hashlib.sha256(raw).hexdigest() != sha:
            raise UseAdmissionError("evaluation_master_use_evidence_changed")
        try:
            report = EvaluationMasterUseReviewV1.model_validate_json(raw)
        except ValueError:
            raise UseAdmissionError("evaluation_master_use_review_invalid") from None
        for item in report.license_evidence:
            if hashlib.sha256(self.admissions._file(item.terms_reference)).hexdigest() != item.terms_sha256:
                raise UseAdmissionError("evaluation_master_license_terms_changed")
        return report

    @staticmethod
    def _match_report(report, context, project_id):
        if report.project_id != project_id or report.master_use_request_sha256 != context["request_sha256"] or report.proof != context["proof"]:
            raise UseAdmissionError("evaluation_master_use_binding_changed")
        needed = {b.admission_id for b in context["scope"].bindings}
        if {item.admission_id for item in report.license_evidence} != needed:
            raise UseAdmissionError("evaluation_master_all_origin_license_required")

    def adopt(self, project_id, payload, operator_key):
        actor = configured_admission_actor(operator_key)
        if actor is None:
            raise UseAdmissionError("execution_admission_operator_required")
        report = self._report(payload.review_reference, payload.review_sha256)
        context = self._context(project_id, report.selection)
        self._match_report(report, context, project_id)
        with self.db.transaction(immediate=True):
            if context["facts"] != self._facts(project_id, report.selection.audio_id):
                raise UseAdmissionError("evaluation_master_authority_changed")
            old = self.db.connection.execute("SELECT id FROM evaluation_master_use_admissions WHERE project_id=? AND idempotency_key=?",
                (str(project_id), payload.idempotency_key)).fetchone()
            if old:
                value = self.records.admission(old[0])
                if value.revoked_at or value.actor_id != actor or value.review != report or value.review_sha256 != payload.review_sha256 or value.review_reference != payload.review_reference:
                    raise UseAdmissionError("evaluation_master_use_idempotency_conflict")
                return value
            return self.records.save_admission(EvaluationMasterUseAdmission(project_id=project_id,
                idempotency_key=payload.idempotency_key, review_reference=payload.review_reference,
                review_sha256=payload.review_sha256, review=report, actor_id=actor, created_at=datetime.now(timezone.utc)))

    def revoke(self, project_id, admission_id, reason, operator_key):
        actor = configured_admission_actor(operator_key)
        if actor is None:
            raise UseAdmissionError("execution_admission_operator_required")
        if not reason.strip():
            raise UseAdmissionError("provider_use_revocation_reason_required")
        with self.db.transaction(immediate=True):
            value = self.records.admission(admission_id)
            if value is None or value.project_id != project_id:
                raise UseAdmissionError("evaluation_master_use_admission_missing")
            if value.revoked_at:
                return value
            return self.records.save_admission(value.model_copy(update=dict(revoked_at=datetime.now(timezone.utc),
                revoked_by=actor, revocation_reason=reason)), revoke=True)

    def _require_receipt(self, project_id, selection, context):
        value = self.records.admission(selection.admission_id) if selection.admission_id else None
        if value is None or value.project_id != project_id:
            raise UseAdmissionError("evaluation_master_use_admission_required")
        if value.revoked_at:
            raise UseAdmissionError("evaluation_master_use_admission_revoked")
        if value.review != self._report(value.review_reference, value.review_sha256):
            raise UseAdmissionError("evaluation_master_use_evidence_changed")
        self._match_report(value.review, context, project_id)
        if value.review.selection != selection.model_copy(update={"admission_id": None}):
            raise UseAdmissionError("evaluation_master_use_selection_changed")
        return value

    def preflight(self, project_id, selection):
        result = dict(policy=POLICY, purpose="internal_evaluation", project_id=str(project_id),
            master_stage_eligible=False, whole_run_ready=False, dispatch_authorized=False, reasons=[],
            downstream_reasons=list(DOWNSTREAM))
        try:
            c = self._context(project_id, selection)
            result.update(audio_id=str(c["audio"].id), duration_ms=c["audio"].duration_ms,
                master_use_request_sha256=c["request_sha256"], proof=c["proof"],
                draft_version=c["draft"].version, script_revision=c["draft"].script_revision)
            blocked = _master_stage_blockers(c)
            result["downstream_reasons"] = list(dict.fromkeys((*c["plan"].stop_reasons, *DOWNSTREAM)))
            result["reasons"] = blocked
            receipt = self._require_receipt(project_id, selection, c)
            result["fingerprint"] = self._fingerprint(c, receipt)
            result["fingerprint_policy"] = "evaluation-master-proof-v2"
            result["master_stage_eligible"] = not blocked
        except (ValueError, KeyError, TypeError, OSError) as exc:
            result["reasons"].append(str(exc) if isinstance(exc, ValueError) else "evaluation_master_proof_invalid")
        return result

    def create(self, project_id, payload):
        c = self._context(project_id, payload)
        receipt = self._require_receipt(project_id, MasterSelection.model_validate(payload.model_dump(exclude={"idempotency_key", "expected_fingerprint"})), c)
        blocked = _master_stage_blockers(c)
        if blocked:
            raise UseAdmissionError(blocked[0])
        fingerprint = self._fingerprint(c, receipt)
        if payload.expected_fingerprint != fingerprint:
            raise UseAdmissionError("evaluation_master_preflight_changed")
        selection = MasterSelection.model_validate(payload.model_dump(exclude={"idempotency_key", "expected_fingerprint"}))
        with self.db.transaction(immediate=True):
            if c["facts"] != self._facts(project_id, payload.audio_id) or receipt != self.records.admission(receipt.id):
                raise UseAdmissionError("evaluation_master_authority_changed")
            old = self.db.connection.execute("SELECT id FROM production_runs WHERE idempotency_key=?", (payload.idempotency_key,)).fetchone()
            duplicate = self.db.connection.execute("SELECT run_id FROM evaluation_run_master_bindings WHERE project_id=? AND request_sha256=?",
                (str(project_id), c["request_sha256"])).fetchone()
            if old or duplicate:
                identity = (old or duplicate)[0]
                value = self.records.binding(identity)
                if (not value or value.project_id != project_id or value.request_sha256 != c["request_sha256"]
                    or value.admission_id != receipt.id or value.selection != selection):
                    raise UseAdmissionError("evaluation_master_run_conflict")
                run_id = UUID(identity)
            else:
                run_id, now = uuid4(), datetime.now(timezone.utc)
                self.db.connection.execute("""INSERT INTO production_runs(id,project_id,idempotency_key,preflight_fingerprint,
                    draft_version,script_revision,style_tokens,repair_allowance,stage,render_job_id,created_at,talking_review_policy_version)
                    VALUES (?,?,?,?,?,?,?,0,?,NULL,?,?)""",
                    (str(run_id), str(project_id), payload.idempotency_key, fingerprint, c["draft"].version,
                     c["draft"].script_revision, payload.style_tokens.model_dump_json(), STAGE, now.isoformat(), payload.talking_review_policy_version))
                # Named completed-use pin: never call generation prepare/pin or create a new media origin.
                scope = c["scope"]
                self.db.connection.execute("INSERT INTO execution_use_scopes(subject_kind,subject_id,project_id,fingerprint,payload,subject_sha256) VALUES ('run',?,?,?,?,?)",
                    (str(run_id), str(project_id), scope.fingerprint, scope.model_dump_json(), c["request_sha256"]))
                self.records.bind(EvaluationRunMasterBindingV1(run_id=run_id, project_id=project_id,
                    request_sha256=c["request_sha256"], request=c["request"], admission_id=receipt.id,
                    admission_sha256=receipt.review_sha256, selection=selection, proof=c["proof"], scope_fingerprint=scope.fingerprint))
        return self.get(project_id, run_id)

    def get(self, project_id, run_id):
        from app.production_runs import ProductionRun
        row = self.db.connection.execute("SELECT * FROM production_runs WHERE id=? AND project_id=?", (str(run_id), str(project_id))).fetchone()
        if row is None:
            return None
        info = dict(policy=POLICY, purpose="internal_evaluation", current_eligible=False, whole_run_ready=False,
            dispatch_authorized=False, reasons=[], historical_binding=None)
        try:
            binding = self.records.binding(run_id)
            if binding is None or binding.project_id != project_id:
                raise UseAdmissionError("evaluation_master_binding_missing")
            info["historical_binding"] = binding.model_dump(mode="json")
            if row["stage"] not in (STAGE, "cancelled"):
                raise UseAdmissionError("evaluation_master_stage_changed")
            scope = read_subject_scope(self.db, "run", run_id)
            scope_row = self.db.connection.execute("SELECT subject_sha256 FROM execution_use_scopes WHERE subject_kind='run' AND subject_id=?", (str(run_id),)).fetchone()
            if scope is None or scope.fingerprint != binding.scope_fingerprint or scope_row[0] != binding.request_sha256:
                raise UseAdmissionError("evaluation_master_scope_changed")
            c = self._context(project_id, binding.selection)
            receipt = self._require_receipt(project_id, binding.selection, c)
            if (c["request_sha256"] != binding.request_sha256 or c["request"] != binding.request or c["proof"] != binding.proof
                or receipt.review_sha256 != binding.admission_sha256 or c["scope"] != scope):
                raise UseAdmissionError("evaluation_master_proof_changed")
            if (row["draft_version"] != c["draft"].version or row["script_revision"] != c["draft"].script_revision
                or json.loads(row["style_tokens"]) != binding.selection.style_tokens.model_dump(mode="json")
                or row["talking_review_policy_version"] != binding.selection.talking_review_policy_version
                or any(row[key] is not None for key in ("voice_job_id", "voice_qa_job_id", "voice_audio_id", "render_job_id", "talking_master_audio_id"))):
                raise UseAdmissionError("evaluation_master_parent_changed")
            expected = self._fingerprint(c, receipt)
            # Retain the original interpretation of already stored v1 parents;
            # no GET, replay or added provider silently upgrades their binding.
            legacy = digest(dict(request=c["request_sha256"], receipt=receipt.model_dump(mode="json"), plan=c["plan"].fingerprint))
            if row["preflight_fingerprint"] not in (expected, legacy):
                raise UseAdmissionError("evaluation_master_parent_changed")
            info["fingerprint_policy"] = "evaluation-master-proof-v2" if row["preflight_fingerprint"] == expected else "legacy-full-preflight-v1"
            if row["stage"] == "cancelled":
                raise UseAdmissionError("evaluation_master_run_cancelled")
            blockers = _master_stage_blockers(c)
            if blockers:
                raise UseAdmissionError(blockers[0])
            info.update(current_eligible=True, audio_id=str(c["audio"].id), audio_sha256=c["audio"].content_hash,
                duration_ms=c["audio"].duration_ms, copy=c["request"]["copy"])
        except (ValueError, KeyError, TypeError, OSError) as exc:
            info["reasons"] = [str(exc) if isinstance(exc, ValueError) else "evaluation_master_proof_invalid"]
        return ProductionRun(id=run_id, project_id=project_id, idempotency_key=row["idempotency_key"],
            preflight_fingerprint=row["preflight_fingerprint"], render_job_id=None, created_at=row["created_at"],
            status="cancelled" if row["stage"] == "cancelled" else "evaluation_master_bound" if info["current_eligible"] else "evaluation_master_blocked",
            talking_review_policy_version=row["talking_review_policy_version"],
            waiting_stop_reasons=tuple((*info["reasons"], *DOWNSTREAM)), evaluation_master=info)

    def cancel(self, project_id, run_id):
        with self.db.transaction(immediate=True):
            self.db.connection.execute("UPDATE production_runs SET stage='cancelled' WHERE id=? AND project_id=? AND stage=?",
                (str(run_id), str(project_id), STAGE))
        return self.get(project_id, run_id)

    def list_runs(self, project_id):
        return [self.get(project_id, UUID(r[0])) for r in self.db.connection.execute(
            "SELECT id FROM production_runs WHERE project_id=? AND (stage=? OR id IN (SELECT run_id FROM evaluation_run_master_bindings)) ORDER BY created_at DESC LIMIT 50",
            (str(project_id), STAGE))]

    def candidates(self, project_id):
        values = []
        for row in self.db.connection.execute("SELECT audio_id FROM voice_evaluation_human_reviews WHERE project_id=? ORDER BY rowid DESC LIMIT 50", (str(project_id),)):
            audio = AudioAssetRepository(self.db).get(UUID(row[0]))
            if audio:
                state = self.reviews.status(project_id, audio.id)
                generation = audio.metadata.get("voice_generation")
                values.append(dict(audio_id=str(audio.id), audio_sha256=audio.content_hash, duration_ms=audio.duration_ms,
                    text=generation.get("target_text", "") if isinstance(generation, dict) else "",
                    reviewed_at=None if state["record"] is None else state["record"]["review"]["reviewed_at"],
                    current_review_eligible=state["current_eligible"], reasons=state["reasons"],
                    human_approved=bool(state["record"] and state["record"]["review"]["approved"])))
        return values
