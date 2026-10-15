"""One normal successor for a cancelled, never-reserved composition carrier."""
import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID
from app.db import JobRepository, ProviderCallRepository
from app.domain.models import execution_canonical_sha256 as digest, JobStatus
from app.domain.talking_recovery import TalkingRecoveryReview, RECOVERY_POLICY
from app.execution_admission import UseAdmissionError, configured_admission_actor


class TalkingRecoveryService:
    def __init__(self, owner):
        self.owner, self.db, self.root = owner, owner.db, owner.root

    def _root(self, project, predecessor):
        from app.evaluation_composition import EvaluationCompositionService
        from app.evaluation_talking_execution import call_key
        old = self.owner.records.operation(predecessor)
        if old is None or old.project_id != project or old.policy != "evaluation-talking-calibration-v1":
            raise UseAdmissionError("talking_recovery_predecessor_missing")
        self.owner.require_calibration_claim(old)
        row = self.db.connection.execute(
            "SELECT * FROM evaluation_composition_slots WHERE operation_id=?", (str(old.id),)).fetchone()
        if row is None or row["slot"] != "talking:"+str(old.selection.scene_plan_id):
            raise UseAdmissionError("talking_recovery_root_slot_required")
        composition = EvaluationCompositionService(self.db,self.root)
        batch = composition.records.get("batch",row["batch_id"])
        index = self.db.connection.execute("SELECT * FROM evaluation_composition_batches WHERE id=?",(row["batch_id"],)).fetchone()
        if (batch is None or index is None or index["project_id"]!=str(project)
            or index["master_run_id"]!=str(old.master_run_id) or index["authorization_sha256"]!=batch["review"]["authorization_sha256"]):
            raise UseAdmissionError("talking_recovery_batch_changed")
        if (batch["project_id"] != str(project) or batch["review"]["master_run_id"] != str(old.master_run_id)
            or str(old.selection.scene_plan_id) not in batch["review"]["scene_ids"]):
            raise UseAdmissionError("talking_recovery_batch_changed")
        if self.db.connection.execute("SELECT 1 FROM evaluation_talking_recoveries WHERE successor_id=?",(str(old.id),)).fetchone():
            raise UseAdmissionError("talking_recovery_chain_forbidden")
        job = JobRepository(self.db).get(old.generation_job_id)
        cancellation = self.owner.records.result(old.id,"cancellation")
        if job is None or job.status != JobStatus.CANCELLED or cancellation is None:
            raise UseAdmissionError("talking_recovery_cancelled_root_required")
        if ProviderCallRepository(self.db).get_by_idempotency_key(call_key(old)) is not None:
            raise UseAdmissionError("talking_recovery_call_already_reserved")
        if self.db.connection.execute(
            "SELECT 1 FROM evaluation_talking_results WHERE operation_id=? AND role<>'cancellation'",(str(old.id),)).fetchone():
            raise UseAdmissionError("talking_recovery_existing_output")
        # An owned output with a missing result cannot become a fresh attempt.
        for row_asset in self.db.connection.execute("SELECT payload FROM assets"):
            metadata = json.loads(row_asset[0]).get("metadata",{})
            generation = metadata.get("talking_generation",{})
            if (generation.get("job_id") == str(old.generation_job_id)
                or generation.get("evaluation_talking",{}).get("operation_id") == str(old.id)):
                raise UseAdmissionError("talking_recovery_existing_output")
        claim = self.db.connection.execute("SELECT * FROM evaluation_talking_calibration_claims WHERE operation_id=?",(str(old.id),)).fetchone()
        job_row = dict(self.db.connection.execute("SELECT * FROM jobs WHERE id=?",(str(job.id),)).fetchone())
        proof = dict(operation=old.model_dump(mode="json"),job=job_row,cancellation=cancellation,
            claim=dict(claim),slot=dict(row),batch_sha256=digest(batch),authorization_sha256=batch["review"]["authorization_sha256"])
        return old,batch,proof

    def _compatible(self, old, context):
        from app.runtime_inventory import RuntimeInventoryStore
        previous = old.proof["calibration_intent"]["document"]
        fresh = context["proof"]["calibration_intent"]["document"]
        if ({k:v for k,v in previous.items() if k != "execution_sha256"}
            != {k:v for k,v in fresh.items() if k != "execution_sha256"}):
            raise UseAdmissionError("talking_recovery_inputs_changed")
        old_spec = self.owner.admissions.get(old.project_id,old.selection.execution_admission_id).review.execution_specification
        spec = context["execution"].review.execution_specification
        excluded = {"runtime_inventory","runtime_origin"}
        if ({k:v for k,v in old_spec.model_dump(mode="json").items() if k not in excluded}
            != {k:v for k,v in spec.model_dump(mode="json").items() if k not in excluded}):
            raise UseAdmissionError("talking_recovery_recipe_changed")
        store = RuntimeInventoryStore(self.root)
        before,after = store.read_v2(old_spec.runtime_inventory),store.read_v2(spec.runtime_inventory)
        before_files = {f.name:f for f in before.files if f.role != "entrypoint"}
        after_files = {f.name:f for f in after.files if f.role != "entrypoint"}
        a,b = [f for f in before.files if f.role == "entrypoint"],[f for f in after.files if f.role == "entrypoint"]
        if (before.directories != after.directories or before_files != after_files or len(a)!=1 or len(b)!=1
            or a[0].name != b[0].name or a[0].sha256 == b[0].sha256):
            raise UseAdmissionError("talking_recovery_owned_identity_correction_required")
        if context["inference_sha256"] == old.inference_sha256:
            raise UseAdmissionError("talking_recovery_same_identity_unsupported")

    def preflight(self, project, predecessor, selection):
        old,batch,root = self._root(project,predecessor)
        from app.evaluation_composition import EvaluationCompositionService
        EvaluationCompositionService(self.db,self.root)._batch(UUID(batch["id"]))
        context = self.owner._context(project,selection)
        self.owner._receipt(project,selection,context)
        self._compatible(old,context)
        if self.owner._existing_operation(project,context,self.owner._receipt(project,selection,context)) is not None:
            raise UseAdmissionError("talking_recovery_same_identity_unsupported")
        fingerprint = digest(dict(root=root,request_sha256=context["request_sha256"],proof=context["proof"],
            selection=selection.model_dump(mode="json")))
        receipt=self.owner._receipt(project,selection,context)
        return dict(eligible=True,policy=RECOVERY_POLICY,predecessor_id=str(old.id),batch_id=batch["id"],
            predecessor_sha256=digest(root),fingerprint=fingerprint,original_scene_allowance=True,
            start_fingerprint=digest(dict(request=context["request_sha256"],receipt=receipt.model_dump(mode="json")))),context,root

    def candidates(self, project):
        values=[]
        for row in self.db.connection.execute("SELECT id FROM evaluation_talking_operations WHERE project_id=? ORDER BY rowid",(str(project),)).fetchall():
            old=self.owner.records.operation(row[0])
            if self.owner.records.result(old.id,"cancellation") is None:continue
            entry=dict(predecessor_id=str(old.id),scene_plan_id=str(old.selection.scene_plan_id),choices=[],reasons=[])
            binding=self.status(project,old.id)
            entry["successor_id"]=None if binding is None else binding["successor_id"]
            if binding is None:
                try:
                    self._root(project,old.id)
                    for admission_row in self.db.connection.execute("SELECT id FROM evaluation_talking_use_admissions WHERE project_id=? ORDER BY rowid DESC",(str(project),)).fetchall():
                        receipt=self.owner.records.admission(admission_row[0])
                        if receipt.revoked_at or receipt.review.selection.scene_plan_id!=old.selection.scene_plan_id:continue
                        selection=receipt.review.selection.model_copy(update={"use_admission_id":receipt.id})
                        try:
                            state,_,_=self.preflight(project,old.id,selection)
                            entry["choices"].append(dict(selection=selection.model_dump(mode="json"),**state))
                        except (ValueError,TypeError,KeyError,OSError):pass
                    if not entry["choices"]:entry["reasons"]=["talking_recovery_current_admission_required"]
                except (ValueError,TypeError,KeyError,OSError) as exc:
                    from app.evaluation_talking_access import public_reason
                    entry["reasons"]=[public_reason(exc)]
            else:
                self.require_successor(self.owner.records.operation(UUID(binding["successor_id"])))
            values.append(entry)
        return values

    def _read(self, row):
        if row is None:return None
        value=json.loads(row["payload"])
        if (digest(value)!=row["payload_sha256"] or any(value[k]!=row[k] for k in
            ("predecessor_id","successor_id","project_id","batch_id","slot")) or value.get("policy")!=RECOVERY_POLICY):
            raise UseAdmissionError("talking_recovery_binding_changed")
        return value

    def status(self, project, predecessor):
        row=self.db.connection.execute("SELECT * FROM evaluation_talking_recoveries WHERE predecessor_id=? AND project_id=?",
            (str(predecessor),str(project))).fetchone()
        return self._read(row)

    def create(self, project, predecessor, payload, operator_key):
        actor=configured_admission_actor(operator_key)
        if actor is None:raise UseAdmissionError("execution_admission_operator_required")
        old_binding=self.status(project,predecessor)
        if old_binding:
            if (old_binding["review_sha256"]!=payload.review_sha256 or old_binding["review_reference"]!=payload.review_reference
                or old_binding["start_sha256"]!=digest(payload.start.model_dump(mode="json")) or old_binding["actor_id"]!=actor):
                raise UseAdmissionError("talking_recovery_idempotency_conflict")
            self.require_successor(self.owner.records.operation(UUID(old_binding["successor_id"])))
            return self.owner.records.operation(UUID(old_binding["successor_id"]))
        raw=self.owner.admissions._file(payload.review_reference,maximum=300000)
        if hashlib.sha256(raw).hexdigest()!=payload.review_sha256:raise UseAdmissionError("talking_recovery_evidence_changed")
        review=TalkingRecoveryReview.model_validate_json(raw)
        # Start-only submission fields are excluded from the normal selection.
        from app.domain.evaluation_talking import TalkingCalibrationSelection
        selection=TalkingCalibrationSelection.model_validate_json(payload.start.model_dump_json(exclude={"idempotency_key","expected_fingerprint"}))
        state,context,root=self.preflight(project,predecessor,selection)
        if (review.project_id!=project or review.predecessor_id!=predecessor or review.batch_id!=UUID(state["batch_id"])
            or review.predecessor_sha256!=state["predecessor_sha256"] or review.fingerprint!=state["fingerprint"]
            or review.selection!=selection or not all(s.strip() for s in review.findings)):
            raise UseAdmissionError("talking_recovery_preflight_changed")
        closure_raw=self.owner.admissions._file(review.closure_evidence.reference,maximum=1000000)
        closure=json.loads(closure_raw)
        if (hashlib.sha256(closure_raw).hexdigest()!=review.closure_evidence.sha256
            or closure.get("predecessor_id")!=str(predecessor) or closure.get("processes_absent") is not True
            or closure.get("accepted_local_history") is not True or not closure.get("method")):
            raise UseAdmissionError("talking_recovery_quiescence_required")
        prepared=dict(state=state,root=root,context=context,review=review,actor=actor,payload=payload)
        return self.owner._create(project,payload.start,recovery=prepared)

    def bind(self, operation, prepared):
        old,batch,root=self._root(operation.project_id,prepared["review"].predecessor_id)
        if digest(root)!=digest(prepared["root"]):raise UseAdmissionError("talking_recovery_predecessor_changed")
        if self.status(operation.project_id,old.id):raise UseAdmissionError("talking_recovery_allowance_spent")
        payload=prepared["payload"]
        value=dict(policy=RECOVERY_POLICY,predecessor_id=str(old.id),successor_id=str(operation.id),
            project_id=str(operation.project_id),batch_id=batch["id"],slot="talking:"+str(old.selection.scene_plan_id),
            root_sha256=digest(root),successor_sha256=digest(operation.model_dump(mode="json")),
            review_reference=payload.review_reference,review_sha256=payload.review_sha256,
            review=prepared["review"].model_dump(mode="json"),actor_id=prepared["actor"],
            start_sha256=digest(payload.start.model_dump(mode="json")),created_at=datetime.now(timezone.utc).isoformat())
        self.db.connection.execute("INSERT INTO evaluation_talking_recoveries VALUES (?,?,?,?,?,?,?)",
            (str(old.id),str(operation.id),str(operation.project_id),batch["id"],value["slot"],digest(value),json.dumps(value)))

    def require_successor(self, operation):
        if operation is None:raise UseAdmissionError("talking_recovery_successor_missing")
        row=self.db.connection.execute("SELECT * FROM evaluation_talking_recoveries WHERE successor_id=?",(str(operation.id),)).fetchone()
        value=self._read(row)
        if value is None:raise UseAdmissionError("composition_talking_slot_changed")
        old,batch,root=self._root(operation.project_id,UUID(value["predecessor_id"]))
        if (digest(root)!=value["root_sha256"] or digest(operation.model_dump(mode="json"))!=value["successor_sha256"]
            or operation.master_run_id!=old.master_run_id or operation.selection.scene_plan_id!=old.selection.scene_plan_id
            or value["batch_id"]!=batch["id"]):
            raise UseAdmissionError("talking_recovery_binding_changed")
        return value
