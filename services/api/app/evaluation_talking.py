"""Normal exact-use authority and immutable ownership for evaluation Talking.

Input rights and execution rights are different roles. Original Master Jobs,
scope, QA and human judgment are never modified by this application service.
"""
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from uuid import UUID, uuid4

from app.db import (AssetRepository, AudioAssetRepository, ClipRepository, JobRepository, ProjectDraftRepository,
    TalkingProfileRepository, ProviderMachineCapabilityProfileRepository)
from app.db.evaluation_talking import EvaluationTalkingRepository
from app.domain.evaluation_talking import (TalkingSelection, EvaluationTalkingUseReview, EvaluationTalkingUseAdmission, EvaluationTalkingOperation,
    CALIBRATION_POLICY, TalkingCalibrationSelection, TalkingCalibrationCandidate, EvaluationTalkingCalibrationOperation, TalkingUseReviewRecord, TalkingSelectionRecord, parse_selection)
from pydantic import TypeAdapter
from app.domain.models import (execution_canonical_sha256 as digest, CostCategory, ExecutionSpecificationV9, ExecutionSpecificationV10, ProviderUseLicenseReviewV2,
    ProviderUseLicenseReviewV3, ProviderUseLicenseReviewV4, ProviderUseLicenseReviewV5,
    Job, JobStatus, JobType, TalkingGenerationJobPayload, TalkingQaJobPayload, PlannedTalkingContext, SourceKind)
from app.evaluation_master import EvaluationMasterService
from app.execution_admission import ProviderUseAdmissionService, UseAdmissionError, configured_admission_actor
from app.execution_scope import ExecutionScopeService, _make_scope, job_use_identity, read_job_scope
from app.production_runs import _unique_scene_speech_span
from app.talking import plan_talking_audio_slice, select_talking_reference
from app.talking.source_admission import TalkingSourceAdmissionRepository
from app.talking.planned_preview import _source_path


class EvaluationTalkingService:
    def __init__(self, db, root, *, cost_estimator=None):
        self.db, self.root = db, Path(root).resolve()
        from app.costs import UnknownProviderCostEstimator
        self.cost_estimator = cost_estimator or UnknownProviderCostEstimator()
        self.records = EvaluationTalkingRepository(db)
        self.masters = EvaluationMasterService(db, self.root)
        self.admissions = ProviderUseAdmissionService(db, self.root)
        self.scopes = ExecutionScopeService(db, self.root)

    def _facts(self, project_id, selection):
        """Detached DB-only fence; no runtime or evidence observation in locks."""
        return tuple((table, tuple(tuple(row) for row in self.db.connection.execute(query, values)))
            for table, query, values in (
                ("parent", "SELECT * FROM production_runs WHERE id=?", (str(selection.master_run_id),)),
                ("parent_binding", "SELECT * FROM evaluation_run_master_bindings WHERE run_id=?", (str(selection.master_run_id),)),
                ("draft", "SELECT * FROM project_drafts WHERE project_id=?", (str(project_id),)),
                ("source", "SELECT * FROM clips WHERE id=?", (str(selection.reference_clip_id),)),
                ("profile", "SELECT * FROM talking_profiles WHERE id=?", (str(selection.talking_profile_id),)),
                ("assets", "SELECT * FROM assets ORDER BY id", ()),
                ("audio", "SELECT * FROM audio_assets ORDER BY id", ()),
                ("scopes", "SELECT * FROM execution_use_scopes ORDER BY subject_kind,subject_id", ()),
                ("origins", "SELECT * FROM execution_use_content_origins ORDER BY content_hash,subject_kind,subject_id", ()),
                ("voice_reviews", "SELECT * FROM voice_evaluation_human_reviews WHERE project_id=?", (str(project_id),)),
                ("master_admissions", "SELECT * FROM evaluation_master_use_admissions WHERE project_id=?", (str(project_id),)),
                ("source_admissions", "SELECT * FROM talking_source_admissions ORDER BY id", ()),
                ("capabilities", "SELECT * FROM provider_machine_capability_profiles ORDER BY id", ()),
                ("settings", "SELECT * FROM provider_machine_settings ORDER BY id", ()),
                ("execution_admissions", "SELECT * FROM provider_use_admissions WHERE project_id=?", (str(project_id),)),
                ("use_admissions", "SELECT * FROM evaluation_talking_use_admissions WHERE project_id=?", (str(project_id),)),
                ("calibration_claims", "SELECT * FROM evaluation_talking_calibration_claims WHERE project_id=? ORDER BY operation_id", (str(project_id),)),
                ("recoveries", "SELECT * FROM evaluation_talking_recoveries WHERE project_id=? ORDER BY predecessor_id", (str(project_id),)),
                ("recovery_root_jobs", "SELECT j.* FROM jobs j JOIN evaluation_talking_recoveries r JOIN evaluation_talking_operations o ON o.id=r.predecessor_id AND j.id=o.generation_job_id WHERE r.project_id=? ORDER BY j.id", (str(project_id),)),
                ("recovery_root_results", "SELECT v.* FROM evaluation_talking_results v JOIN evaluation_talking_recoveries r ON v.operation_id=r.predecessor_id WHERE r.project_id=? ORDER BY v.operation_id,v.role", (str(project_id),)),
            ))

    def _inputs(self, project_id, selection):
        before = self._facts(project_id, selection)
        parent = self.masters.get(project_id, selection.master_run_id)
        if parent is None or not parent.evaluation_master["current_eligible"]:
            raise UseAdmissionError("evaluation_talking_master_not_eligible")
        if parent.talking_review_policy_version != 2:
            raise UseAdmissionError("evaluation_talking_parent_policy2_required")
        binding = self.masters.records.binding(parent.id)
        audio = AudioAssetRepository(self.db).get(binding.selection.audio_id)
        draft = ProjectDraftRepository(self.db).get(project_id)
        ordered = sorted(draft.scenes, key=lambda s:s.order)
        scene = next((s for s in ordered if s.id == selection.scene_plan_id),None)
        if scene is None:
            raise UseAdmissionError("evaluation_talking_scene_missing")
        # Normal planning decides whether the scene requires a new Talking
        # visual; source selection may not replace operation/case evidence.
        master_context = self.masters._context(project_id,binding.selection)
        planned = next((s for s in master_context["plan"].scenes if s.scene_plan_id == scene.id),None)
        if planned is None or planned.production_need != "new_talking":
            raise UseAdmissionError("evaluation_talking_not_planned_dependency")
        interval = _unique_scene_speech_span(audio, ordered, scene.id)
        if interval is None:
            raise UseAdmissionError("master_scene_speech_interval_ambiguous_or_unaligned")
        profile = TalkingProfileRepository(self.db).get(selection.talking_profile_id)
        clip = ClipRepository(self.db).get(selection.reference_clip_id)
        source = None if clip is None else AssetRepository(self.db).get(clip.asset_id)
        cap = ProviderMachineCapabilityProfileRepository(self.db).get(selection.capability_profile_id)
        if (profile is None or not profile.consent.confirmed or clip is None or clip.id not in profile.reference_clip_ids):
            raise UseAdmissionError("talking_profile_consent_required")
        if source is None or source.source_kind not in (SourceKind.USER_ASSET,SourceKind.HISTORICAL_ASSET) or not source.authorization_reference.strip():
            raise UseAdmissionError("talking_reference_rights_required")
        calibration = type(selection) in (TalkingCalibrationSelection, TalkingCalibrationCandidate)
        if calibration:
            if (cap is None or cap.capability != "talking" or cap.mode != "local" or cap.provider != "latentsync"
                or cap.provider != profile.provider or cap.readiness not in ("configured", "available", "verified") or not cap.evidence_reference):
                raise UseAdmissionError("talking_calibration_configuration_missing")
            limit = selection.trial_duration_limit_ms
        else:
            if (cap is None or cap.capability != "talking" or cap.mode != "local" or cap.provider != profile.provider
                or cap.readiness != "verified" or cap.quality_status != "verified" or not cap.evidence_reference):
                raise UseAdmissionError("exact_talking_capability_not_verified")
            limit = cap.verified_parameters.get("fresh_voice_talking_duration_ms")
            if type(limit) is not int or limit <= 0:
                raise UseAdmissionError("talking_provider_duration_bound_unverified")
        indices = [i for i,s in enumerate(audio.transcript_segments) if s.start_ms >= interval[0] and s.end_ms <= interval[1]]
        if not indices or indices != list(range(indices[0],indices[-1]+1)):
            raise UseAdmissionError("talking_master_interval_not_contiguous")
        slice_plan = plan_talking_audio_slice(audio,start_segment_index=indices[0],end_segment_index=indices[-1]+1,max_duration_ms=limit)
        if (slice_plan.start_ms,slice_plan.end_ms) != interval:
            raise UseAdmissionError("talking_master_interval_changed")
        from app.providers.latentsync import LatentSyncProvider
        model_duration = LatentSyncProvider._model_duration_ms(slice_plan.duration_ms)
        if calibration and model_duration > selection.trial_processing_limit_ms:
            raise UseAdmissionError("talking_calibration_processing_bound_exceeded")
        if clip.end_ms-clip.start_ms < model_duration:
            raise UseAdmissionError("talking_reference_alignment_not_covered")
        brief = selection.brief.model_copy(update={"minimum_reference_duration_ms":max(selection.brief.minimum_reference_duration_ms,interval[1]-interval[0]),"require_no_burned_subtitles":True})
        sources = TalkingSourceAdmissionRepository(self.db, self.root)
        receipt = sources.get(selection.source_admission_id)
        if receipt is None:
            raise UseAdmissionError("talking_source_admission_required")
        assessment = sources.require_current(receipt)
        if (assessment.source_asset_id != source.id or assessment.source_clip_id != clip.id or assessment.source_content_hash != source.content_hash
            or (assessment.start_ms,assessment.end_ms) != (clip.start_ms,clip.end_ms) or assessment.presentation != "source_native_portrait"
            or clip.talking_reference_assessment is None or clip.talking_reference_assessment.burned_in_subtitles is not False):
            raise UseAdmissionError("talking_source_admission_mismatch")
        # The exact adopted full-interval human review supplies source
        # eligibility when old retrieval scores/flags were never populated.
        # Do not fabricate persisted scores or override known negative scores.
        if select_talking_reference([clip],brief,adopted_full_interval_clip_ids=frozenset({clip.id})).selected_clip_id != clip.id:
            raise UseAdmissionError("talking_reference_fit_not_verified")
        for item in (audio,source):
            path = _source_path(item.source_file,self.root)
            with path.open("rb") as stream:
                if hashlib.file_digest(stream,"sha256").hexdigest() != item.content_hash:
                    raise UseAdmissionError("evaluation_talking_input_changed")
        if before != self._facts(project_id,selection):
            raise UseAdmissionError("evaluation_talking_authority_changed")
        return locals()

    def _context(self, project_id, selection, *, retained=False):
        selection = parse_selection(selection)
        inputs = self._inputs(project_id, selection)
        before, parent, binding, audio, draft, scene, master_context, interval, profile, clip, source, cap, slice_plan, model_duration, receipt, assessment = (
            inputs[k] for k in ("before","parent","binding","audio","draft","scene","master_context","interval","profile","clip","source","cap","slice_plan","model_duration","receipt","assessment"))
        execution = self.admissions.get(project_id,selection.execution_admission_id)
        calibration = type(selection) is TalkingCalibrationSelection
        expected_report = ProviderUseLicenseReviewV5 if calibration else ProviderUseLicenseReviewV2
        if execution is None or type(execution.review) is not expected_report or type(execution.review.execution_specification) not in ((ExecutionSpecificationV10,) if calibration else (ExecutionSpecificationV9, ExecutionSpecificationV10)):
            raise UseAdmissionError("maintained_talking_execution_admission_required")
        if (not calibration and type(execution.review.execution_specification) is ExecutionSpecificationV10
            and cap.verified_parameters.get("talking_execution_sha256") != execution.review.execution_specification.execution_sha256):
            raise UseAdmissionError("exact_talking_configuration_not_verified")
        execution_scope = self.scopes.prepare(project_id,purpose="internal_evaluation",admission_ids=(execution.id,))
        origins = list(master_context["scope"].bindings)
        source_scopes = self.scopes.scopes_for_hash(source.content_hash)
        for scope in source_scopes:
            if scope.project_id != project_id:
                raise UseAdmissionError("evaluation_talking_origin_project_mismatch")
            origins.extend(scope.bindings)
        all_bindings = {}
        for b in (*origins,*execution_scope.bindings):
            if b.admission_id in all_bindings and all_bindings[b.admission_id] != b:
                raise UseAdmissionError("evaluation_talking_origin_changed")
            all_bindings[b.admission_id] = b
        output_scope = _make_scope(project_id,"internal_evaluation",tuple(all_bindings[k] for k in sorted(all_bindings,key=str)))
        # Authenticate every retained input receipt and term without treating
        # spent Voice generation as permission to run another inference.
        retained_inventory_cache = {}
        for item in output_scope.bindings:
            value = self.admissions.get(project_id,item.admission_id)
            if value is None or value.revoked_at or value.review_sha256 != item.review_sha256:
                raise UseAdmissionError("evaluation_talking_input_authority_changed")
            if type(value.review) is ProviderUseLicenseReviewV3:
                from app.voice_calibration import _retained_completed_report
                reviewed = _retained_completed_report(self.admissions,value)
            elif type(value.review) is ProviderUseLicenseReviewV4:
                from app.voice_evaluation_qa import _retained_asr_report
                reviewed = _retained_asr_report(self.admissions,value,
                    parsed_inventory_cache=retained_inventory_cache)
            else:
                reviewed = self.admissions._report(value.review_reference,value.review_sha256)
            if reviewed != value.review:
                raise UseAdmissionError("evaluation_talking_input_authority_changed")
        proof = dict(master_binding_sha256=digest(binding.model_dump(mode="json")), master_proof=binding.proof,
            master_use_admission_id=str(binding.admission_id),master_use_admission_sha256=binding.admission_sha256,
            scene=scene.model_dump(mode="json"),master_start_ms=interval[0],master_end_ms=interval[1],slice_plan=slice_plan.__dict__,
            model_duration_ms=model_duration,source_processing_end_ms=clip.start_ms+model_duration,
            source_clip=clip.model_dump(mode="json"),source_asset=source.model_dump(mode="json"),source_receipt=receipt.model_dump(mode="json"),
            source_assessment=assessment.model_dump(mode="json"),source_scopes=[s.model_dump(mode="json") for s in source_scopes],
            source_membership=[list(r) for r in self.db.connection.execute("SELECT subject_kind,subject_id FROM execution_use_content_origins WHERE content_hash=? ORDER BY subject_kind,subject_id",(source.content_hash,))],
            profile=profile.model_dump(mode="json"),execution_admission_sha256=execution.review_sha256,
            execution_scope=execution_scope.model_dump(mode="json"),output_scope=output_scope.model_dump(mode="json"))
        if calibration:
            intent = self._trial_intent(project_id, selection, execution.review.execution_specification, inputs)
            if (execution.review.trial_input_sha256 != intent["trial_input_sha256"]
                or execution.review.trial_duration_limit_ms != selection.trial_duration_limit_ms
                or execution.review.trial_processing_limit_ms != selection.trial_processing_limit_ms):
                raise UseAdmissionError("talking_calibration_trial_binding_changed")
            proof["calibration_intent"] = intent
        # JSON-normalize dataclass values before retaining a portable proof.
        import json
        proof = json.loads(json.dumps(proof,default=str))
        request = dict(policy=selection.policy,purpose=selection.purpose,project_id=str(project_id),
            selection=selection.model_dump(mode="json",exclude={"use_admission_id"}),draft=draft.model_dump(mode="json"),
            style=binding.selection.style_tokens.model_dump(mode="json"),proof=proof)
        inference = dict(request)
        inference["selection"] = {k:v for k,v in request["selection"].items() if k != "master_run_id"}
        inference["proof"] = {k:v for k,v in proof.items()
            if k not in ("master_binding_sha256","master_use_admission_id","master_use_admission_sha256")}
        if calibration:
            inference = dict(policy=CALIBRATION_POLICY, project_id=str(project_id), semantic_trial_sha256=intent["semantic_trial_sha256"])
        if before != self._facts(project_id,selection):
            raise UseAdmissionError("evaluation_talking_authority_changed")
        return dict(selection=selection,parent=parent,binding=binding,audio=audio,source=source,clip=clip,profile=profile,cap=cap,
            execution=execution,execution_scope=execution_scope,output_scope=output_scope,proof=proof,request=request,
            request_sha256=digest(request),inference_sha256=digest(inference),facts=before,slice_plan=slice_plan)

    def _trial_intent(self, project_id, selection, spec, inputs):
        """No own execution/use receipt IDs enter the pre-adoption digest."""
        from app.execution_admission import _identity_matches
        if type(spec) is not ExecutionSpecificationV10 or not _identity_matches(inputs["cap"], spec.identity):
            raise UseAdmissionError("talking_calibration_execution_identity_changed")
        source = inputs["source"]
        candidate = selection.model_dump(mode="json", exclude={"execution_admission_id", "use_admission_id"})
        document = dict(policy=CALIBRATION_POLICY, project_id=str(project_id), selection=candidate,
            execution_sha256=spec.execution_sha256, configuration_sha256=self.admissions._configuration(inputs["cap"]),
            capability=inputs["cap"].model_dump(mode="json",exclude={"updated_at","last_verified_at"}),
            master_binding=inputs["binding"].model_dump(mode="json"),
            master_audio=inputs["audio"].model_dump(mode="json"),
            draft=inputs["draft"].model_dump(mode="json"),
            style=inputs["binding"].selection.style_tokens.model_dump(mode="json"),
            master_scope=inputs["master_context"]["scope"].model_dump(mode="json"),
            interval=list(inputs["interval"]), model_duration_ms=inputs["model_duration"],
            source_processing_end_ms=inputs["clip"].start_ms+inputs["model_duration"],
            profile=inputs["profile"].model_dump(mode="json"),source_asset=source.model_dump(mode="json"),
            source_clip=inputs["clip"].model_dump(mode="json"),
            source_receipt=inputs["receipt"].model_dump(mode="json"),source_assessment=inputs["assessment"].model_dump(mode="json"),
            source_scopes=[v.model_dump(mode="json") for v in self.scopes.scopes_for_hash(source.content_hash)])
        # Semantic inference identity excludes replaceable authority/parent
        # aliases, while the adopted intent above retains all exact roles.
        semantic = {k:v for k,v in document.items() if k not in ("master_binding","master_scope","source_receipt","source_assessment","source_scopes")}
        semantic["selection"] = {k:v for k,v in candidate.items() if k not in ("master_run_id","source_admission_id","authorization_reference")}
        return dict(document=document,trial_input_sha256=digest(document),semantic_trial_sha256=digest(semantic))

    def calibration_intent(self, project_id, payload):
        from app.domain.evaluation_talking import TalkingCalibrationIntent
        payload = TalkingCalibrationIntent.model_validate_json(payload.model_dump_json())
        inputs = self._inputs(project_id,payload.selection)
        intent = self._trial_intent(project_id,payload.selection,payload.execution_specification,inputs)
        if inputs["before"] != self._facts(project_id,payload.selection):
            raise UseAdmissionError("evaluation_talking_authority_changed")
        return intent

    def candidates(self, project_id):
        """Read-only candidate disclosure, never a trial or capability grant."""
        receipts = [self.records.admission(r[0]) for r in self.db.connection.execute(
            "SELECT id FROM evaluation_talking_use_admissions WHERE project_id=?",(str(project_id),))]
        values = []
        for row in self.db.connection.execute("SELECT id FROM provider_machine_capability_profiles ORDER BY id"):
            cap = ProviderMachineCapabilityProfileRepository(self.db).get(row[0])
            if cap.capability != "talking" or cap.mode != "local" or cap.provider != "latentsync":
                continue
            grants = []
            for receipt in receipts:
                if (receipt.revoked_at or receipt.review.policy != CALIBRATION_POLICY
                    or receipt.review.selection.capability_profile_id != cap.id):
                    continue
                execution = self.admissions.get(project_id,receipt.review.selection.execution_admission_id)
                if (execution is not None and not execution.revoked_at and type(execution.review) is ProviderUseLicenseReviewV5
                    and execution.review.configuration_sha256 == self.admissions._configuration(cap)):
                    grants.append(receipt)
            values.append(dict(capability_profile_id=cap.id,provider=cap.provider,model=cap.model,
                readiness=cap.readiness,quality_status=cap.quality_status,
                verified_duration_ms=cap.verified_parameters.get("fresh_voice_talking_duration_ms"),
                calibration_grant_available=bool(grants)))
        return values

    def require_calibration_claim(self, operation):
        row = self.db.connection.execute("SELECT * FROM evaluation_talking_calibration_claims WHERE operation_id=? OR generation_job_id=?",
            (str(operation.id),str(operation.generation_job_id))).fetchone()
        if type(operation) is not EvaluationTalkingCalibrationOperation:
            if row is not None:
                raise UseAdmissionError("talking_calibration_policy_stripped")
            return
        intent = operation.proof.get("calibration_intent",{})
        expected = (str(operation.selection.execution_admission_id),str(operation.project_id),intent.get("semantic_trial_sha256"),
            intent.get("trial_input_sha256"),str(operation.id),str(operation.generation_job_id))
        if row is None or tuple(row) != expected or digest(intent.get("document")) != intent.get("trial_input_sha256"):
            raise UseAdmissionError("talking_calibration_claim_changed")

    def _report(self, reference, sha):
        # A genuine multi-take Master carries every retained source origin.
        # Keep the existing bounded evidence-file limit, not a single-take size.
        raw = self.admissions._file(reference,maximum=1_000_000)
        if hashlib.sha256(raw).hexdigest() != sha:
            raise UseAdmissionError("evaluation_talking_use_evidence_changed")
        report = TypeAdapter(TalkingUseReviewRecord).validate_json(raw)
        for item in (*report.license_evidence,*report.dependency_findings):
            if hashlib.sha256(self.admissions._file(item.terms_reference)).hexdigest() != item.terms_sha256:
                raise UseAdmissionError("evaluation_talking_license_terms_changed")
        return report

    def _match(self, project_id, report, context):
        if report.project_id != project_id or report.request_sha256 != context["request_sha256"] or report.proof != context["proof"]:
            raise UseAdmissionError("evaluation_talking_use_binding_changed")
        if {v.admission_id for v in report.license_evidence} != {b.admission_id for b in context["output_scope"].bindings}:
            raise UseAdmissionError("evaluation_talking_all_origin_license_required")
        required = {"narration_input_use","source_person_video","talking_output_processing_review"}
        if not required.issubset(v.dependency for v in report.dependency_findings):
            raise UseAdmissionError("evaluation_talking_dependency_findings_required")

    def adopt(self, project_id, payload, operator_key):
        actor = configured_admission_actor(operator_key)
        if actor is None: raise UseAdmissionError("execution_admission_operator_required")
        report = self._report(payload.review_reference,payload.review_sha256)
        context = self._context(project_id,report.selection)
        self._match(project_id,report,context)
        with self.db.transaction(immediate=True):
            if context["facts"] != self._facts(project_id,report.selection):
                raise UseAdmissionError("evaluation_talking_authority_changed")
            old = self.db.connection.execute("SELECT id FROM evaluation_talking_use_admissions WHERE project_id=? AND idempotency_key=?",(str(project_id),payload.idempotency_key)).fetchone()
            if old:
                value = self.records.admission(old[0])
                if value.revoked_at or value.actor_id != actor or value.review != report or value.review_sha256 != payload.review_sha256 or value.review_reference != payload.review_reference:
                    raise UseAdmissionError("evaluation_talking_use_idempotency_conflict")
                return value
            return self.records.save_admission(EvaluationTalkingUseAdmission(project_id=project_id,idempotency_key=payload.idempotency_key,
                review_reference=payload.review_reference,review_sha256=payload.review_sha256,review=report,actor_id=actor,created_at=datetime.now(timezone.utc)))

    def _receipt(self, project_id, selection, context):
        value = self.records.admission(selection.use_admission_id) if selection.use_admission_id else None
        if value is None or value.project_id != project_id:
            raise UseAdmissionError("evaluation_talking_use_admission_required")
        if value.revoked_at: raise UseAdmissionError("evaluation_talking_use_admission_revoked")
        if self._report(value.review_reference,value.review_sha256) != value.review:
            raise UseAdmissionError("evaluation_talking_use_evidence_changed")
        self._match(project_id,value.review,context)
        if value.review.selection != selection.model_copy(update={"use_admission_id":None}):
            raise UseAdmissionError("evaluation_talking_use_selection_changed")
        return value

    def _existing_operation(self, project_id, context, receipt):
        if type(context["selection"]) is TalkingCalibrationSelection:
            claim = self.db.connection.execute("SELECT operation_id FROM evaluation_talking_calibration_claims WHERE execution_admission_id=? OR (project_id=? AND semantic_trial_sha256=?)",
                (str(context["selection"].execution_admission_id),str(project_id),context["proof"]["calibration_intent"]["semantic_trial_sha256"])).fetchone()
            if claim is not None:
                value = self.records.operation(claim[0])
                if value is None or value.inference_sha256 != context["inference_sha256"]:
                    raise UseAdmissionError("talking_calibration_grant_spent")
                self.require_calibration_claim(value)
                return value
        row = self.db.connection.execute("SELECT id FROM evaluation_talking_operations WHERE project_id=? AND (inference_sha256=? OR admission_id=?)",
            (str(project_id),context["inference_sha256"],str(receipt.id))).fetchone()
        if row is None:
            return None
        value = self.records.operation(row[0])
        if value.inference_sha256 != context["inference_sha256"]:
            raise UseAdmissionError("evaluation_talking_operation_conflict")
        return value

    def _budget_estimate(self, context):
        from app.evaluation_talking_execution import CALL_SOURCE
        try:
            cap = context["cap"]
            value = self.cost_estimator.estimate(operation="talking",provider=cap.provider,model=cap.model,
                input_source=CALL_SOURCE,category=CostCategory.TALKING)
            from app.domain.models import UsageCost
            if not isinstance(value,UsageCost) or value.category != CostCategory.TALKING:
                raise ValueError()
            return UsageCost.model_validate_json(value.model_dump_json())
        except Exception:
            raise UseAdmissionError("evaluation_talking_cost_estimate_invalid") from None

    def _require_budget(self, project_id, estimate):
        from app.budget import ProviderCallLedger, BudgetLimitError
        try:
            ProviderCallLedger(self.db).check_reservation(project_id=project_id,estimated_cost=estimate)
        except BudgetLimitError as exc:
            raise UseAdmissionError(f"budget_{exc.code}") from None

    def preflight(self, project_id, selection):
        result = dict(policy=selection.policy,purpose=selection.purpose,eligible=False,reasons=[])
        try:
            context = self._context(project_id,selection)
            result.update(request_sha256=context["request_sha256"],proof=context["proof"])
            receipt = self._receipt(project_id,selection,context)
            existing = self._existing_operation(project_id,context,receipt)
            if existing is None:
                estimate = self._budget_estimate(context)
                self._require_budget(project_id,estimate)
                result.update(budget_state="checked_not_reserved",estimated_cost=estimate.model_dump(mode="json"))
            else:
                result.update(budget_state="existing_operation_no_new_reservation",existing_operation_id=str(existing.id))
            result.update(eligible=True,fingerprint=digest(dict(request=context["request_sha256"],receipt=receipt.model_dump(mode="json"))))
        except (ValueError,KeyError,TypeError,OSError) as exc:
            from app.evaluation_talking_access import public_reason
            result["reasons"] = [public_reason(exc)]
        return result

    def revoke(self, project_id, identity, reason, operator_key):
        actor = configured_admission_actor(operator_key)
        if actor is None: raise UseAdmissionError("execution_admission_operator_required")
        if not reason.strip(): raise UseAdmissionError("provider_use_revocation_reason_required")
        with self.db.transaction(immediate=True):
            value = self.records.admission(identity)
            if value is None or value.project_id != project_id: raise UseAdmissionError("evaluation_talking_use_admission_missing")
            if value.revoked_at: return value
            return self.records.save_admission(value.model_copy(update=dict(revoked_at=datetime.now(timezone.utc),revoked_by=actor,revocation_reason=reason)),revoke=True)

    def create(self, project_id, payload):
        return self._create(project_id,payload)

    def _create(self, project_id, payload, *, recovery=None):
        selection = TypeAdapter(TalkingSelectionRecord).validate_json(payload.model_dump_json(exclude={"idempotency_key","expected_fingerprint"}))
        context = self._context(project_id,selection) if recovery is None else recovery["context"]
        receipt = self._receipt(project_id,selection,context)
        fingerprint = digest(dict(request=context["request_sha256"],receipt=receipt.model_dump(mode="json")))
        if fingerprint != payload.expected_fingerprint:
            raise UseAdmissionError("evaluation_talking_preflight_changed")
        estimate = self._budget_estimate(context)
        with self.db.transaction(immediate=True):
            if context["facts"] != self._facts(project_id,selection) or receipt != self.records.admission(receipt.id):
                raise UseAdmissionError("evaluation_talking_authority_changed")
            old = self.db.connection.execute("SELECT id FROM evaluation_talking_operations WHERE project_id=? AND idempotency_key=?",(str(project_id),payload.idempotency_key)).fetchone()
            duplicate = self._existing_operation(project_id,context,receipt)
            if old or duplicate:
                if recovery is not None:
                    raise UseAdmissionError("talking_recovery_same_identity_unsupported")
                value = self.records.operation(old[0]) if old else duplicate
                if value.inference_sha256 != context["inference_sha256"]:
                    raise UseAdmissionError("evaluation_talking_operation_conflict")
                self.require_calibration_claim(value)
                return value
            self._require_budget(project_id,estimate)
            now = datetime.now(timezone.utc)
            identity,generation,qa,preview,series = (uuid4() for _ in range(5))
            operation_type = EvaluationTalkingCalibrationOperation if type(selection) is TalkingCalibrationSelection else EvaluationTalkingOperation
            value = operation_type(policy=selection.policy,id=identity,project_id=project_id,master_run_id=selection.master_run_id,
                request_sha256=context["request_sha256"],inference_sha256=context["inference_sha256"],request=context["request"],proof=context["proof"],
                selection=selection,admission_id=receipt.id,admission_sha256=receipt.review_sha256,
                execution_scope_fingerprint=context["execution_scope"].fingerprint,output_scope_fingerprint=context["output_scope"].fingerprint,
                generation_job_id=generation,qa_job_id=qa,preview_job_id=preview,series_id=series,created_at=now)
            self.records.insert(value,payload.idempotency_key)
            if recovery is not None:
                from app.evaluation_talking_recovery import TalkingRecoveryService
                TalkingRecoveryService(self).bind(value,recovery)
            from app.evaluation_composition import claim_talking_slot
            claim_talking_slot(self.db,value)
            if type(selection) is TalkingCalibrationSelection:
                self.db.connection.execute("INSERT INTO evaluation_talking_calibration_claims VALUES (?,?,?,?,?,?)", (
                    str(selection.execution_admission_id), str(project_id), context["proof"]["calibration_intent"]["semantic_trial_sha256"],
                    context["proof"]["calibration_intent"]["trial_input_sha256"], str(identity), str(generation)))
            plan = context["slice_plan"]
            cap = context["cap"]
            job = Job(id=generation,project_id=project_id,type=JobType.GENERATE_TALKING,idempotency_key=f"evaluation-talking:{identity}:generation",
                created_at=now,updated_at=now,payload=TalkingGenerationJobPayload(project_id=project_id,talking_profile_id=selection.talking_profile_id,
                    reference_clip_id=selection.reference_clip_id,narration_audio_id=context["audio"].id,authorization_reference=selection.authorization_reference,
                    planned_context=PlannedTalkingContext(run_id=identity,scene_plan_id=selection.scene_plan_id,source_admission_id=selection.source_admission_id,
                        capability_profile_id=cap.id,expected_provider=cap.provider,expected_model=cap.model,expected_runtime=cap.runtime,
                        expected_machine_id=cap.machine_id,binding_sha256=digest(value.proof),consent_sha256=digest(context["profile"].consent.model_dump(mode="json"))),
                    slice_start_segment_index=plan.start_segment_index,slice_end_segment_index=plan.end_segment_index,
                    slice_max_duration_ms=(selection.trial_duration_limit_ms if type(selection) is TalkingCalibrationSelection else cap.verified_parameters["fresh_voice_talking_duration_ms"]),
                    slice_limit_source=(CALIBRATION_POLICY if type(selection) is TalkingCalibrationSelection else "verified_capability"),
                    reference_window_start_ms=context["clip"].start_ms,reference_window_end_ms=context["clip"].end_ms))
            if JobRepository(self.db).create(job) != job:
                raise UseAdmissionError("evaluation_talking_job_conflict")
            scope = context["execution_scope"]
            # Named role writer: source/Voice grants remain separately retained
            # in the operation. Generic parent-scope union is intentionally not
            # used for this new Talking execution Job.
            self.db.connection.execute("INSERT INTO execution_use_scopes(subject_kind,subject_id,project_id,fingerprint,payload,subject_sha256) VALUES ('job',?,?,?,?,?)",
                (str(job.id),str(project_id),scope.fingerprint,scope.model_dump_json(),job_use_identity(job)))
            self.db.connection.execute("INSERT INTO evaluation_talking_owned_jobs(job_id,operation_id,role,job_sha256,scope_fingerprint) VALUES (?,?,?,?,?)",
                (str(job.id),str(identity),"generation",job_use_identity(job),scope.fingerprint))
            self.require_job_identity(job)
            return value

    def require_job_identity(self, job):
        """Structural ownership checks shared by direct and dispatched paths."""
        marker = self.db.connection.execute("SELECT * FROM evaluation_talking_owned_jobs WHERE job_id=?",(str(job.id),)).fetchone()
        if marker is None:
            raise UseAdmissionError("evaluation_talking_job_owner_missing")
        operation = self.records.operation(marker["operation_id"])
        if operation is None or operation.project_id != job.project_id:
            raise UseAdmissionError("evaluation_talking_operation_missing")
        self.require_calibration_claim(operation)
        from app.evaluation_composition import require_talking_slot
        require_talking_slot(self.db,operation)
        expected = dict(generation=operation.generation_job_id,qa=operation.qa_job_id,preview=operation.preview_job_id)
        if expected[marker["role"]] != job.id or marker["job_sha256"] != job_use_identity(job):
            raise UseAdmissionError("evaluation_talking_job_changed")
        scope = self.scopes.get("job",job.id)
        row = self.db.connection.execute("SELECT subject_sha256 FROM execution_use_scopes WHERE subject_kind='job' AND subject_id=?",(str(job.id),)).fetchone()
        if (scope is None or scope.project_id != job.project_id or scope.purpose != "internal_evaluation" or scope.fingerprint != marker["scope_fingerprint"]
            or row[0] != marker["job_sha256"] or scope.fingerprint != operation.execution_scope_fingerprint):
            raise UseAdmissionError("evaluation_talking_job_scope_changed")
        if (marker["role"] == "generation" and (job.type != JobType.GENERATE_TALKING or job.payload is None
            or job.payload.planned_context is None or job.payload.planned_context.run_id != operation.id
            or job.payload.planned_context.binding_sha256 != digest(operation.proof))):
            raise UseAdmissionError("evaluation_talking_job_changed")
        if marker["role"] == "qa" and (job.type != JobType.VERIFY_TALKING
            or job.payload != self._qa_payload(operation)):
            raise UseAdmissionError("evaluation_talking_qa_job_changed")
        if marker["role"] == "preview":
            from app.db import TalkingSliceSeriesRepository
            from app.domain.models import TalkingRunPreviewJobPayload
            series = TalkingSliceSeriesRepository(self.db).get(operation.series_id)
            if (series is None or series != self._preview_series(operation,created_at=series.created_at)
                or job.type != JobType.PREPARE_TALKING_RUN_PREVIEW
                or job.payload != TalkingRunPreviewJobPayload(project_id=operation.project_id,
                    series_id=series.id,origin_sha256=digest(series.planned_origin.model_dump(mode="json")))):
                raise UseAdmissionError("evaluation_talking_preview_job_changed")
        return operation

    def _preview_series(self, operation, *, created_at):
        """DB-only immutable one-child origin; never mutate the Master parent."""
        import json
        from app.domain.evaluation_talking import EvaluationTalkingQaResult, EvaluationTalkingGenerationResult
        from app.domain.models import TalkingSliceSeries, PlannedTalkingRunOriginV2
        qa = EvaluationTalkingQaResult.model_validate_json(json.dumps(self.records.result(operation.id,"qa")))
        result = EvaluationTalkingGenerationResult.model_validate_json(json.dumps(self.records.result(operation.id,"generation")))
        if qa.qa_state != "verified":
            raise UseAdmissionError("evaluation_talking_technical_qa_required")
        payload = self._qa_payload(operation)
        proof = operation.proof
        admission = self.records.admission(operation.admission_id)
        origin = PlannedTalkingRunOriginV2(run_id=operation.id,scene_plan_id=operation.selection.scene_plan_id,
            binding_sha256=digest(proof),plan_fingerprint=digest(operation.request["draft"]),
            generation_job_id=operation.generation_job_id,qa_job_id=operation.qa_job_id,
            qa_report_sha256=digest(qa.qa),output_asset_id=result.asset_id,output_sha256=result.asset_sha256,
            master_audio_id=payload.master_audio_id,master_sha256=payload.master_sha256,
            master_review_sha256=proof["master_proof"]["human_record_sha256"],
            master_start_ms=payload.master_start_ms,master_end_ms=payload.master_end_ms,
            source_clip_id=operation.selection.reference_clip_id,source_sha256=proof["source_asset"]["content_hash"],
            source_start_ms=proof["source_clip"]["start_ms"],source_end_ms=proof["source_clip"]["end_ms"],
            source_admission_id=operation.selection.source_admission_id,
            consent_sha256=digest(proof["profile"]["consent"]),
            authorization_reference=operation.selection.authorization_reference,
            license_evidence_reference=admission.review_reference,execution_sha256=result.execution_sha256)
        return TalkingSliceSeries(id=operation.series_id,project_id=operation.project_id,
            idempotency_key=f"evaluation-talking:{operation.id}:series",request_fingerprint=operation.request_sha256,
            narration_audio_id=payload.master_audio_id,child_job_ids=[operation.generation_job_id],
            planned_origin=origin,created_at=created_at)

    def create_preview(self, operation):
        from app.db import TalkingSliceSeriesRepository
        from app.domain.models import TalkingRunPreviewJobPayload
        from app.evaluation_talking_execution import require_generation_result
        context = self.require_retained_operation(operation)
        require_generation_result(self,operation,self.records.result(operation.id,"generation"))
        qa_job = JobRepository(self.db).get(operation.qa_job_id)
        if qa_job is None or qa_job.status != JobStatus.COMPLETED:
            raise UseAdmissionError("evaluation_talking_qa_not_completed")
        now = datetime.now(timezone.utc)
        old_series = TalkingSliceSeriesRepository(self.db).get(operation.series_id)
        series = self._preview_series(operation,created_at=now if old_series is None else old_series.created_at)
        if old_series is not None and old_series != series:
            raise UseAdmissionError("evaluation_talking_preview_series_changed")
        job = Job(id=operation.preview_job_id,project_id=operation.project_id,type=JobType.PREPARE_TALKING_RUN_PREVIEW,
            idempotency_key=f"evaluation-talking:{operation.id}:preview",created_at=now,updated_at=now,
            payload=TalkingRunPreviewJobPayload(project_id=operation.project_id,series_id=series.id,
                origin_sha256=digest(series.planned_origin.model_dump(mode="json"))))
        with self.db.transaction(immediate=True):
            if (self.records.operation(operation.id) != operation or self.records.result(operation.id,"cancellation") is not None
                or self._facts(operation.project_id,operation.selection) != context["facts"]
                or JobRepository(self.db).get(qa_job.id) != qa_job
                or self._preview_series(operation,created_at=series.created_at) != series):
                raise UseAdmissionError("evaluation_talking_authority_changed")
            old = JobRepository(self.db).get(job.id)
            if old is not None:
                self.require_job_identity(old)
                return old
            if TalkingSliceSeriesRepository(self.db).create(series) != series:
                raise UseAdmissionError("evaluation_talking_preview_series_changed")
            scope = self.scopes.get("job",operation.generation_job_id)
            if JobRepository(self.db).create(job) != job:
                raise UseAdmissionError("evaluation_talking_job_conflict")
            self.db.connection.execute("INSERT INTO execution_use_scopes(subject_kind,subject_id,project_id,fingerprint,payload,subject_sha256) VALUES ('job',?,?,?,?,?)",
                (str(job.id),str(job.project_id),scope.fingerprint,scope.model_dump_json(),job_use_identity(job)))
            self.db.connection.execute("INSERT INTO evaluation_talking_owned_jobs(job_id,operation_id,role,job_sha256,scope_fingerprint) VALUES (?,?,?,?,?)",
                (str(job.id),str(operation.id),"preview",job_use_identity(job),scope.fingerprint))
            self.require_job_identity(job)
            return job

    def _qa_payload(self, operation):
        """DB-only exact graph projection from the immutable generation result."""
        from app.domain.evaluation_talking import EvaluationTalkingGenerationResult
        import json
        value = self.records.result(operation.id,"generation")
        if value is None:
            raise UseAdmissionError("evaluation_talking_generation_result_required")
        result = EvaluationTalkingGenerationResult.model_validate_json(json.dumps(value))
        generation = JobRepository(self.db).get(operation.generation_job_id)
        asset = AssetRepository(self.db).get(result.asset_id)
        if (generation is None or asset is None or result.operation_id != operation.id
            or result.job_id != generation.id or asset.content_hash != result.asset_sha256):
            raise UseAdmissionError("evaluation_talking_generation_result_changed")
        metadata = asset.metadata.get("talking_generation",{})
        return TalkingQaJobPayload(project_id=operation.project_id,run_id=operation.id,
            scene_plan_id=operation.selection.scene_plan_id,generation_job_id=generation.id,
            output_asset_id=result.asset_id,output_sha256=result.asset_sha256,
            master_audio_id=generation.payload.narration_audio_id,
            master_sha256=self._qa_master_hash(operation),
            master_start_ms=metadata["master_slice_start_ms"],master_end_ms=metadata["master_slice_end_ms"])

    def _qa_master_hash(self, operation):
        job = JobRepository(self.db).get(operation.generation_job_id)
        audio = AudioAssetRepository(self.db).get(job.payload.narration_audio_id)
        if audio is None:
            raise UseAdmissionError("evaluation_talking_master_missing")
        return audio.content_hash

    def create_qa(self, operation):
        """Recover the reserved QA job without creating another inference."""
        from app.evaluation_talking_execution import require_generation_result
        context = self.require_retained_operation(operation)
        require_generation_result(self,operation,self.records.result(operation.id,"generation"))
        generation = JobRepository(self.db).get(operation.generation_job_id)
        if generation is None or generation.status != JobStatus.COMPLETED:
            raise UseAdmissionError("evaluation_talking_generation_not_completed")
        payload = self._qa_payload(operation)
        now = datetime.now(timezone.utc)
        job = Job(id=operation.qa_job_id,project_id=operation.project_id,type=JobType.VERIFY_TALKING,
            idempotency_key=f"evaluation-talking:{operation.id}:qa",created_at=now,updated_at=now,payload=payload)
        with self.db.transaction(immediate=True):
            if (self.records.operation(operation.id) != operation or self.records.result(operation.id,"cancellation") is not None
                or self._facts(operation.project_id,operation.selection) != context["facts"]
                or JobRepository(self.db).get(generation.id) != generation):
                raise UseAdmissionError("evaluation_talking_authority_changed")
            old = JobRepository(self.db).get(job.id)
            if old is not None:
                self.require_job_identity(old)
                return old
            scope = self.scopes.get("job",generation.id)
            if JobRepository(self.db).create(job) != job:
                raise UseAdmissionError("evaluation_talking_job_conflict")
            self.db.connection.execute("INSERT INTO execution_use_scopes(subject_kind,subject_id,project_id,fingerprint,payload,subject_sha256) VALUES ('job',?,?,?,?,?)",
                (str(job.id),str(job.project_id),scope.fingerprint,scope.model_dump_json(),job_use_identity(job)))
            self.db.connection.execute("INSERT INTO evaluation_talking_owned_jobs(job_id,operation_id,role,job_sha256,scope_fingerprint) VALUES (?,?,?,?,?)",
                (str(job.id),str(operation.id),"qa",job_use_identity(job),scope.fingerprint))
            self.require_job_identity(job)
            return job

    def require_current_operation(self, operation, *, job=None):
        """Fresh input-use permission, separate from structural ownership."""
        if self.db.connection.in_transaction:
            raise UseAdmissionError("execution_observation_inside_transaction")
        current = self.records.operation(operation.id)
        if current != operation:
            raise UseAdmissionError("evaluation_talking_operation_changed")
        self.require_calibration_claim(operation)
        if self.records.result(operation.id,"cancellation") is not None:
            raise UseAdmissionError("evaluation_talking_operation_cancelled")
        if job is not None and self.require_job_identity(job) != operation:
            raise UseAdmissionError("evaluation_talking_job_changed")
        context = self._context(operation.project_id,operation.selection)
        receipt = self._receipt(operation.project_id,operation.selection,context)
        if (context["request_sha256"] != operation.request_sha256 or context["proof"] != operation.proof
            or context["execution_scope"].fingerprint != operation.execution_scope_fingerprint
            or context["output_scope"].fingerprint != operation.output_scope_fingerprint
            or receipt.id != operation.admission_id or receipt.review_sha256 != operation.admission_sha256):
            raise UseAdmissionError("evaluation_talking_operation_authority_changed")
        return context

    def generation_guard(self, operation, job, context):
        """DB-only fence shared by reservation and terminal persistence."""
        stored = JobRepository(self.db).get(job.id)
        if (stored is None or stored.status != JobStatus.RUNNING or job.status != JobStatus.RUNNING
            or stored.attempt != job.attempt or self.require_job_identity(stored) != operation):
            raise UseAdmissionError("evaluation_talking_job_not_running")
        if self.records.result(operation.id,"cancellation") is not None:
            raise UseAdmissionError("evaluation_talking_operation_cancelled")
        if context["facts"] != self._facts(operation.project_id,operation.selection):
            raise UseAdmissionError("evaluation_talking_authority_changed")

    def require_retained_operation(self, operation):
        """Current input/output rights for completed bytes, never fresh dispatch.

        Authentication of the completed ledger/output is required separately.
        A runtime update or exhausted inference allowance cannot itself grant
        or revoke this exact retained review. Source/plan/consent/receipt
        changes still invalidate it.
        """
        if self.db.connection.in_transaction:
            raise UseAdmissionError("execution_observation_inside_transaction")
        if self.records.operation(operation.id) != operation:
            raise UseAdmissionError("evaluation_talking_operation_changed")
        self.require_calibration_claim(operation)
        if self.records.result(operation.id,"cancellation") is not None:
            raise UseAdmissionError("evaluation_talking_operation_cancelled")
        before = self._facts(operation.project_id,operation.selection)
        selected, proof = operation.selection, operation.proof
        if type(operation) is EvaluationTalkingCalibrationOperation:
            execution = self.admissions.get(operation.project_id,selected.execution_admission_id)
            cap = ProviderMachineCapabilityProfileRepository(self.db).get(selected.capability_profile_id)
            if (execution is None or type(execution.review) is not ProviderUseLicenseReviewV5 or cap is None
                or execution.review.configuration_sha256 != self.admissions._configuration(cap)
                or execution.review.trial_input_sha256 != proof["calibration_intent"]["trial_input_sha256"]):
                raise UseAdmissionError("talking_calibration_configuration_changed")
        parent = self.masters.get(operation.project_id,selected.master_run_id)
        binding = self.masters.records.binding(selected.master_run_id)
        draft = ProjectDraftRepository(self.db).get(operation.project_id)
        if (parent is None or not parent.evaluation_master["current_eligible"] or parent.talking_review_policy_version != 2
            or binding is None or digest(binding.model_dump(mode="json")) != proof["master_binding_sha256"]
            or draft is None or draft.model_dump(mode="json") != operation.request["draft"]
            or binding.selection.style_tokens.model_dump(mode="json") != operation.request["style"]):
            raise UseAdmissionError("evaluation_talking_master_or_plan_changed")
        audio = AudioAssetRepository(self.db).get(binding.selection.audio_id)
        profile = TalkingProfileRepository(self.db).get(selected.talking_profile_id)
        clip = ClipRepository(self.db).get(selected.reference_clip_id)
        source = None if clip is None else AssetRepository(self.db).get(clip.asset_id)
        if (profile is None or not profile.consent.confirmed or profile.model_dump(mode="json") != proof["profile"]
            or clip is None or clip.model_dump(mode="json") != proof["source_clip"]
            or source is None or source.model_dump(mode="json") != proof["source_asset"]
            or audio is None):
            raise UseAdmissionError("evaluation_talking_input_changed")
        sources = TalkingSourceAdmissionRepository(self.db, self.root)
        receipt = sources.get(selected.source_admission_id)
        if receipt is None:
            raise UseAdmissionError("talking_source_admission_required")
        assessment = sources.require_current(receipt)
        # Same source-receipt resolver as fresh admission; no loose ID check.
        if receipt.model_dump(mode="json") != proof["source_receipt"] or assessment.model_dump(mode="json") != proof["source_assessment"]:
            raise UseAdmissionError("evaluation_talking_source_authority_changed")
        membership = [list(r) for r in self.db.connection.execute(
            "SELECT subject_kind,subject_id FROM execution_use_content_origins WHERE content_hash=? ORDER BY subject_kind,subject_id",(source.content_hash,))]
        if membership != proof["source_membership"]:
            raise UseAdmissionError("evaluation_talking_origin_changed")
        if [item.model_dump(mode="json") for item in self.scopes.scopes_for_hash(source.content_hash)] != proof["source_scopes"]:
            raise UseAdmissionError("evaluation_talking_origin_changed")
        from app.execution_scope import _parse_scope
        import json
        output_scope = _parse_scope(json.dumps(proof["output_scope"]))
        execution_scope = self.scopes.get("job",operation.generation_job_id)
        if execution_scope is None or execution_scope.fingerprint != operation.execution_scope_fingerprint:
            raise UseAdmissionError("evaluation_talking_job_scope_changed")
        from app.evaluation_talking_execution import retained_talking_report
        retained_inventory_cache = {}
        for item in output_scope.bindings:
            value = self.admissions.get(operation.project_id,item.admission_id)
            if (value is None or value.revoked_at or value.review_sha256 != item.review_sha256
                or value.review.identity != item.identity or value.review.configuration_sha256 != item.configuration_sha256):
                raise UseAdmissionError("evaluation_talking_input_authority_changed")
            if type(value.review) is ProviderUseLicenseReviewV3:
                from app.voice_calibration import _retained_completed_report
                reviewed = _retained_completed_report(self.admissions,value)
            elif type(value.review) is ProviderUseLicenseReviewV4:
                from app.voice_evaluation_qa import _retained_asr_report
                reviewed = _retained_asr_report(self.admissions,value,
                    parsed_inventory_cache=retained_inventory_cache)
            elif type(value.review) in (ProviderUseLicenseReviewV2, ProviderUseLicenseReviewV5) and type(value.review.execution_specification) in (ExecutionSpecificationV9, ExecutionSpecificationV10):
                reviewed = retained_talking_report(self.admissions,value)
            else:
                reviewed = self.admissions._report(value.review_reference,value.review_sha256)
            if reviewed != value.review:
                raise UseAdmissionError("evaluation_talking_input_authority_changed")
        for item in (audio,source):
            with _source_path(item.source_file,self.root).open("rb") as stream:
                if hashlib.file_digest(stream,"sha256").hexdigest() != item.content_hash:
                    raise UseAdmissionError("evaluation_talking_input_changed")
        context = dict(proof=proof,request_sha256=operation.request_sha256,output_scope=output_scope,
            execution_scope=execution_scope,audio=audio,source=source,clip=clip,profile=profile,facts=before)
        self._receipt(operation.project_id,selected,context)
        if before != self._facts(operation.project_id,selected):
            raise UseAdmissionError("evaluation_talking_authority_changed")
        return context

    def _pin_output(self, operation, job, context, asset):
        """Named role-aware writer: retain input-use AND execution restrictions.

        Unlike generic pin/generation checks, authenticated completed Voice/ASR
        inputs are not treated as fresh inference grants. All file/evidence
        checks precede this lock; generation_guard compares their detached DB
        facts again. No caller-selected permission subset is accepted.
        """
        if not self.db.connection.in_transaction:
            raise UseAdmissionError("evaluation_talking_output_transaction_required")
        self.generation_guard(operation,job,context)
        scope = context["output_scope"]
        if scope.fingerprint != operation.output_scope_fingerprint:
            raise UseAdmissionError("evaluation_talking_output_scope_changed")
        for old in self.scopes.scopes_for_hash(asset.content_hash):
            if old.project_id != scope.project_id or old.purpose != scope.purpose:
                raise UseAdmissionError("evaluation_talking_output_alias_scope_changed")
            if any(binding not in scope.bindings for binding in old.bindings):
                raise UseAdmissionError("execution_use_ancestor_dropped")
        self.db.connection.execute("INSERT INTO execution_use_scopes(subject_kind,subject_id,project_id,fingerprint,payload,subject_sha256) VALUES ('asset',?,?,?,?,NULL)",
            (str(asset.id),str(scope.project_id),scope.fingerprint,scope.model_dump_json()))
        self.db.connection.execute("INSERT INTO execution_use_content_origins(content_hash,subject_kind,subject_id) VALUES (?,'asset',?)",
            (asset.content_hash,str(asset.id)))

    def _pin_quarantine(self, operation, scope, identity, content_hash):
        """Restriction-only tombstone: no Asset, inference or read permission.

        Keep the known producer/input restrictions even after revocation or
        cancellation. Same-byte imports must still discover these origins.
        Existing origins stay in the index; no permission union is rewritten.
        """
        from app.execution_scope import _parse_scope
        from app.domain.models import Sha256
        from pydantic import TypeAdapter
        content_hash = TypeAdapter(Sha256).validate_python(content_hash)
        scope = _parse_scope(scope.model_dump_json())
        if (not self.db.connection.in_transaction or self.records.operation(operation.id) != operation
            or scope.project_id != operation.project_id or scope.purpose != "internal_evaluation"
            or scope.fingerprint != operation.output_scope_fingerprint
            or AssetRepository(self.db).get(identity) is not None):
            raise UseAdmissionError("evaluation_talking_quarantine_identity_changed")
        sha = digest(dict(policy=operation.policy,operation_id=str(operation.id),state="quarantined",content_hash=content_hash))
        self.db.connection.execute("INSERT INTO execution_use_scopes(subject_kind,subject_id,project_id,fingerprint,payload,subject_sha256) VALUES ('asset',?,?,?,?,?)",
            (str(identity),str(operation.project_id),scope.fingerprint,scope.model_dump_json(),sha))
        self.db.connection.execute("INSERT INTO execution_use_content_origins(content_hash,subject_kind,subject_id) VALUES (?,'asset',?)",
            (content_hash,str(identity)))

    def cancel(self, project_id, identity):
        """Cancel future consumption; retain immutable operation/call evidence."""
        with self.db.transaction(immediate=True):
            operation = self.records.operation(identity)
            if operation is None or operation.project_id != project_id:
                raise UseAdmissionError("evaluation_talking_operation_missing")
            if self.records.result(operation.id,"cancellation") is None:
                self.records.save_result(operation.id,"cancellation",dict(operation_id=str(identity),cancelled_at=datetime.now(timezone.utc).isoformat()))
                for job_id in (operation.generation_job_id,operation.qa_job_id,operation.preview_job_id):
                    job = JobRepository(self.db).get(job_id)
                    if job is not None and job.status in (JobStatus.PENDING,JobStatus.RUNNING):
                        JobRepository(self.db).update(job.model_copy(update={"status":JobStatus.CANCELLED,"updated_at":datetime.now(timezone.utc)}))
            return operation


def is_evaluation_talking_job(db, job):
    """Multiple durable markers prevent stripped owner/payload fallback."""
    return bool(db.connection.execute("SELECT 1 FROM evaluation_talking_owned_jobs WHERE job_id=?",(str(job.id),)).fetchone()
        or db.connection.execute("SELECT 1 FROM evaluation_talking_calibration_claims WHERE generation_job_id=?",(str(job.id),)).fetchone()
        or getattr(job.payload,"slice_limit_source",None) == CALIBRATION_POLICY
        or db.connection.execute("SELECT 1 FROM evaluation_talking_operations WHERE generation_job_id=? OR qa_job_id=? OR preview_job_id=?",(str(job.id),)*3).fetchone()
        or getattr(getattr(job.payload,"planned_context",None),"run_id",None) and db.connection.execute(
            "SELECT 1 FROM evaluation_talking_operations WHERE id=?",(str(job.payload.planned_context.run_id),)).fetchone())
