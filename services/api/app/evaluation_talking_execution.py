"""Owned one-child Talking accounting/result seam; generic dispatch stays shut.

Input observation is outside locks. Only the durable ledger reservation owner
may invoke the exact retained prepared object. Output rows and all origin
restrictions commit in the ledger's terminal transaction.
"""
from types import SimpleNamespace
from pathlib import Path
from app.db import AssetRepository, ProviderCallRepository, JobRepository
from app.domain.models import CostCategory, SourceKind, execution_canonical_sha256 as digest
from app.domain.evaluation_talking import EvaluationTalkingGenerationResult, EvaluationTalkingQaResult
from app.domain.talking_child import TalkingChildRequest
from app.evaluation_talking import EvaluationTalkingService
from app.execution_admission import UseAdmissionError
from app.frozen_worker import freeze_worker_identity
from app.media.importer import MediaImporter
from app.media.ffprobe import FFProbeAdapter
from app.provider_execution import ProviderExecutionService, RuntimeProviderIdentity
from app.providers.maintained_talking_execution import PreparedMaintainedTalkingChild
from app.providers.maintained_talking_dual import PreparedDualTalkingChild
from app.talking.planned_preview import _source_path
from app.budget import provider_call_input_digest

CALL_SOURCE = "evaluation-talking-owned-generation"


def _output_importer(db, root, tools):
    """Pass the existing probe adapter its declared command-string contract."""
    return MediaImporter(db, root, FFProbeAdapter(str(Path(tools) / "ffprobe.exe")))


def retained_talking_report(service, receipt):
    """Authenticate the saved carrier/terms without comparing today's bundle."""
    import hashlib
    from app.domain.models import ProviderUseLicenseReviewV2, ProviderUseLicenseReviewV5, ExecutionSpecificationV9, ExecutionSpecificationV10
    from pydantic import TypeAdapter
    from app.domain.maintained_runtime import MaintainedTalkingOrigin
    from app.providers.maintained_runtime import _require_origin
    from app.providers.maintained_voice_bundle import BUNDLE_SLOT, BUNDLE_MAX_BYTES
    from app.runtime_inventory import RuntimeInventoryStore
    raw = service._file(receipt.review_reference,maximum=100000)
    if hashlib.sha256(raw).hexdigest() != receipt.review_sha256:
        raise UseAdmissionError("provider_use_review_hash_changed")
    report = TypeAdapter(ProviderUseLicenseReviewV2 | ProviderUseLicenseReviewV5).validate_json(raw)
    if report != receipt.review or type(report.execution_specification) not in (ExecutionSpecificationV9, ExecutionSpecificationV10):
        raise UseAdmissionError("provider_use_binding_changed")
    spec = report.execution_specification
    inventory = RuntimeInventoryStore(service.root).read_v2(spec.runtime_inventory)
    if type(spec) is ExecutionSpecificationV10:
        from app.providers.maintained_talking_dual import require_dual_origin
        require_dual_origin(service.root,spec.runtime_origin,inventory,RuntimeInventoryStore(service.root).read_v2(spec.engine_inventory),current=False)
    else:
        _require_origin(service.root,spec.runtime_origin,inventory,MaintainedTalkingOrigin)
    entries = [item for item in inventory.files if item.role == "entrypoint"]
    if len(entries) != 1 or entries[0].name != BUNDLE_SLOT or not 0 < entries[0].size_bytes <= BUNDLE_MAX_BYTES:
        raise UseAdmissionError("maintained_talking_child_evidence_invalid")
    terms = service._file(report.license_terms_reference)
    if not terms or hashlib.sha256(terms).hexdigest() != report.license_terms_sha256:
        raise UseAdmissionError("provider_use_terms_hash_changed")
    return report


def call_key(operation):
    return f"evaluation-talking:{operation.project_id}:{operation.inference_sha256}:generation"


def input_document(operation, execution_sha256):
    return dict(operation_id=str(operation.id), job_id=str(operation.generation_job_id),
        request_sha256=operation.request_sha256, inference_sha256=operation.inference_sha256,
        proof_sha256=digest(operation.proof), execution_sha256=execution_sha256,
        execution_scope_fingerprint=operation.execution_scope_fingerprint,
        output_scope_fingerprint=operation.output_scope_fingerprint)


def generation_metadata(operation, job, context, execution_sha256):
    selection, plan = operation.selection, context["slice_plan"]
    return dict(provider=context["execution"].review.identity.provider, model=context["execution"].review.identity.model,
        provider_version="LatentSync-1.5", talking_profile_id=str(selection.talking_profile_id),
        reference_clip_id=str(selection.reference_clip_id), reference_subtitle_crop_bottom_ratio=0,
        narration_audio_id=str(context["audio"].id), master_narration_audio_id=str(context["audio"].id),
        master_slice_start_ms=plan.start_ms, master_slice_end_ms=plan.end_ms,
        reference_window_start_ms=context["clip"].start_ms, reference_window_end_ms=context["clip"].end_ms,
        job_id=str(job.id), project_id=str(job.project_id), attempt=job.attempt, qa_state="pending",
        planned_context=job.payload.planned_context.model_dump(mode="json"),
        evaluation_talking=dict(policy=operation.policy, operation_id=str(operation.id),
            proof_sha256=digest(operation.proof), execution_sha256=execution_sha256,
            output_scope_fingerprint=operation.output_scope_fingerprint))


def require_generation_result(service, operation, value):
    """Authenticate saved original result; this never launches another child."""
    import json
    result = EvaluationTalkingGenerationResult.model_validate_json(json.dumps(value))
    if (result.operation_id != operation.id or result.job_id != operation.generation_job_id
        or result.provider_call_key != call_key(operation) or result.proof_sha256 != digest(operation.proof)
        or result.output_scope_fingerprint != operation.output_scope_fingerprint):
        raise UseAdmissionError("evaluation_talking_result_identity_changed")
    if service.records.result(operation.id,"generation") != result.model_dump(mode="json"):
        raise UseAdmissionError("evaluation_talking_result_missing")
    scope = service.scopes.get("job",operation.generation_job_id)
    if (scope is None or scope.fingerprint != operation.execution_scope_fingerprint or len(scope.bindings) != 1
        or scope.bindings[0].execution_sha256 != result.execution_sha256):
        raise UseAdmissionError("evaluation_talking_result_execution_changed")
    identity = scope.bindings[0].identity
    call = ProviderCallRepository(service.db).get_by_idempotency_key(call_key(operation))
    expected_digest = provider_call_input_digest(dict(project_id=str(operation.project_id),operation="talking",
        provider=identity.provider,model=identity.model,input_source=CALL_SOURCE,input=input_document(operation,result.execution_sha256)))
    if (call is None or call.status != "completed" or call.project_id != operation.project_id or call.operation != "talking"
        or call.mode != "runtime" or call.provider != identity.provider or call.model != identity.model
        or call.input_source != CALL_SOURCE or call.input_digest != expected_digest
        or call.result_payload != {"result":result.model_dump(mode="json")}):
        raise UseAdmissionError("evaluation_talking_result_ledger_changed")
    asset = AssetRepository(service.db).get(result.asset_id)
    if asset is None or asset.content_hash != result.asset_sha256:
        raise UseAdmissionError("evaluation_talking_result_asset_changed")
    scope = service.scopes.get("asset",asset.id)
    if scope is None or scope.fingerprint != result.output_scope_fingerprint:
        raise UseAdmissionError("evaluation_talking_result_scope_changed")
    # Independent technical QA may add only its named immutable projection.
    metadata = dict(asset.metadata)
    generation = dict(metadata.get("talking_generation",{}))
    qa_value = service.records.result(operation.id,"qa")
    if qa_value is None:
        if "qa" in generation or generation.get("qa_state") != "pending":
            raise UseAdmissionError("evaluation_talking_qa_projection_changed")
    else:
        qa = EvaluationTalkingQaResult.model_validate_json(json.dumps(qa_value))
        saved_job = JobRepository(service.db).get(qa.job_id)
        if saved_job is None:
            raise UseAdmissionError("evaluation_talking_qa_job_missing")
        qa_job = service.require_job_identity(saved_job)
        if (qa.operation_id != operation.id or qa.job_id != operation.qa_job_id or qa_job != operation
            or qa.asset_id != asset.id or qa.asset_sha256 != asset.content_hash
            or qa.generation_result_sha256 != digest(result.model_dump(mode="json"))
            or generation.get("qa") != qa.qa or generation.get("qa_state") != qa.qa_state
            or digest(asset.model_dump(mode="json")) != qa.asset_payload_sha256):
            raise UseAdmissionError("evaluation_talking_qa_projection_changed")
    generation.pop("qa",None)
    generation["qa_state"] = "pending"
    metadata["talking_generation"] = generation
    if digest(asset.model_copy(update={"metadata":metadata}).model_dump(mode="json")) != result.asset_payload_sha256:
        raise UseAdmissionError("evaluation_talking_result_provenance_changed")
    path = _source_path(asset.source_file,service.root)
    MediaImporter._verify_content_hash(path,result.asset_sha256)
    return asset,result


def execute_evaluation_talking(*, db, root, job, prepared, cost_estimator):
    from app.jobs.lease_fence import require_owned_lease
    lease = require_owned_lease(db,job)
    service = EvaluationTalkingService(db,root)
    operation = service.require_job_identity(job)
    context = service.require_current_operation(operation,job=job)
    service.generation_guard(operation,job,context)
    if type(prepared) not in (PreparedMaintainedTalkingChild, PreparedDualTalkingChild):
        raise UseAdmissionError("evaluation_talking_owned_child_required")
    from app.domain.models import ExecutionSpecificationV9, ExecutionSpecificationV10
    expected_type = PreparedDualTalkingChild if type(context["execution"].review.execution_specification) is ExecutionSpecificationV10 else PreparedMaintainedTalkingChild
    if type(prepared) is not expected_type:
        raise UseAdmissionError("evaluation_talking_owned_child_required")
    audio = context["audio"].model_copy(update={"source_file":str(_source_path(context["audio"].source_file,service.root))})
    expected = TalkingChildRequest(profile=context["profile"],narration=audio,
        reference_clip_id=context["clip"].id,reference_path=str(_source_path(context["source"].source_file,service.root)),
        reference_sha256=context["source"].content_hash,reference_start_ms=context["clip"].start_ms,
        reference_end_ms=context["clip"].end_ms,slice_plan=context["slice_plan"])
    if prepared.request_snapshot() != expected:
        raise UseAdmissionError("evaluation_talking_child_input_changed")
    receipt = context["execution"]
    provider = SimpleNamespace(provider_name=receipt.review.identity.provider,model=receipt.review.identity.model)
    frozen = freeze_worker_identity(service.scopes,job,provider,prepared,
        capability_profile_id=operation.selection.capability_profile_id,execution_admission_id=receipt.id)
    importer = _output_importer(db, service.root, prepared._options["tools"])
    staged = []

    def guard():
        lease.require()
        service.generation_guard(operation,job,context)

    def current():
        guard()
        if service.require_current_operation(operation,job=job)["facts"] != context["facts"]:
            raise UseAdmissionError("evaluation_talking_authority_changed")

    def reserve():
        frozen.reservation_guard()
        guard()

    def before_action():
        frozen.verify_before_launch()
        current()

    def action():
        output = prepared.execute_owned(lease)
        item = importer.prepare_path(output,operation.selection.authorization_reference,source_kind=SourceKind.AI_VIDEO)
        staged.append(item)
        current()
        item.verify()
        metadata = dict(item.asset.metadata)
        metadata["talking_generation"] = generation_metadata(operation,job,context,frozen.snapshot.execution_sha256)
        asset = item.asset.model_copy(update={"metadata":metadata})
        result = EvaluationTalkingGenerationResult(operation_id=operation.id,job_id=job.id,asset_id=asset.id,
            asset_sha256=asset.content_hash,asset_payload_sha256=digest(asset.model_dump(mode="json")),
            proof_sha256=digest(operation.proof),execution_sha256=frozen.snapshot.execution_sha256,
            output_scope_fingerprint=operation.output_scope_fingerprint,provider_call_key=call_key(operation))
        current()
        return item,asset,result

    def persist(value):
        item,asset,result = value
        guard()
        # Pin first, before our own new row changes the detached input facts.
        # All of these writes roll back with the terminal ledger update.
        item._retain_evaluation_talking(service,operation,job,context,asset.metadata)
        service.records.save_result(operation.id,"generation",result.model_dump(mode="json"))

    def replay(value):
        return require_generation_result(service,operation,value)

    try:
        value = ProviderExecutionService(db,cost_estimator).execute(project_id=job.project_id,
            idempotency_key=call_key(operation),operation="talking",
            provider=RuntimeProviderIdentity(provider.provider_name,provider.model),input_source=CALL_SOURCE,
            category=CostCategory.TALKING,input_document=input_document(operation,frozen.snapshot.execution_sha256),
            reservation_guard=reserve,before_action=before_action,before_replay=current,
            action=action,encode_result=lambda value:value[2].model_dump(mode="json"),decode_result=replay,
            error_code=lambda exc:str(exc) if isinstance(exc,UseAdmissionError) else "evaluation_talking_generation_failed",
            persist_result=persist)
        return (value[1],value[2]) if len(value) == 3 else value
    except BaseException:
        # Failed/unknown calls remain consumed in the ledger. Retain produced
        # bytes privately; never turn them into an unrestricted Asset alias.
        for item in staged:
            if AssetRepository(db).get(item.asset.id) is None:
                path = item.quarantine(service.root/"execution-quarantine"/str(operation.id))
                with db.transaction(immediate=True):
                    service._pin_quarantine(operation,context["output_scope"],item.asset.id,item.asset.content_hash)
                    service.records.save_result(operation.id,"quarantine",dict(operation_id=str(operation.id),
                        job_id=str(job.id),content_hash=item.asset.content_hash,path=path.relative_to(service.root).as_posix(),
                        restriction_subject_id=str(item.asset.id),output_scope_fingerprint=operation.output_scope_fingerprint,provider_call_key=call_key(operation)))
        if not staged:
            # Invalid probe/runtime post-checks can fail before an import is
            # prepared. Preserve any produced bytes without probing/promoting.
            raw = Path(prepared._temporary.name)/"output.mp4"
            call = ProviderCallRepository(db).get_by_idempotency_key(call_key(operation))
            if call is not None and call.status != "completed" and raw.is_file() and 0 < raw.stat().st_size <= 4*1024**3:
                from app.providers.runtime_tree import _digest
                from uuid import uuid4
                sha = _digest(raw,4*1024**3)[1]
                folder = service.root/"execution-quarantine"/str(operation.id)
                folder.mkdir(parents=True,exist_ok=True)
                identity = uuid4()
                path = folder/f"{identity}.quarantine"
                if not MediaImporter._place(raw,path):
                    raise UseAdmissionError("evaluation_talking_quarantine_conflict")
                with db.transaction(immediate=True):
                    service._pin_quarantine(operation,context["output_scope"],identity,sha)
                    service.records.save_result(operation.id,"quarantine",dict(operation_id=str(operation.id),job_id=str(job.id),
                        content_hash=sha,path=path.relative_to(service.root).as_posix(),restriction_subject_id=str(identity),
                        output_scope_fingerprint=operation.output_scope_fingerprint,provider_call_key=call_key(operation)))
        raise
    finally:
        for item in staged:
            item.close()
