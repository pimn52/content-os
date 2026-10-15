"""Concrete once-only local render handler, sharing renderer and technical QA."""
from decimal import Decimal
from uuid import UUID
from app.db import ProviderCallRepository
from app.budget import ProviderCallLedger
from app.domain.models import JobStatus, CostCategory, UsageCost
from app.evaluation_composition import EvaluationCompositionService
from app.jobs.runner import JobExecutionError


class EvaluationCompositionHandler:
    def __init__(self, db, root, renderer):
        self.db,self.root,self.renderer = db,root,renderer
        self.service = EvaluationCompositionService(db,root)

    def require_dispatch(self, job):
        from app.renderer.remotion import RemotionRenderer
        if type(self.renderer) is not RemotionRenderer or self.renderer.assets.db is not self.db:
            raise ValueError("composition_renderer_ownership_invalid")
        self.service.require_current_job(job)
        if job.status != JobStatus.RUNNING or job.attempt != 1:
            raise ValueError("composition_once_only_job_required")

    def __call__(self, job):
        call = None
        ledger = ProviderCallLedger(self.db)
        try:
            self.require_dispatch(job)
            _,owner,scope = self.service.require_job_identity(job)
            key = f"composition:{owner['batch_id']}:render"
            old = ProviderCallRepository(self.db).get_by_idempotency_key(key)
            if old is not None:
                if old.status == "completed":
                    self.service.require_result(job)
                    return  # Reconcile saved result without another render.
                raise ValueError("composition_render_reconciliation_required")
            cost = UsageCost(category=CostCategory.RENDER,amount=Decimal("0"),currency="USD",note="external charge zero; local compute unknown")
            context = self.service.require_current_job(job)
            reserved = ledger.reserve_execution(project_id=job.project_id,idempotency_key=key,operation="render",mode="runtime",
                provider="local-render",model="remotion-ffmpeg",input_source=f"render:{owner['render_id']}",
                input_digest=owner["request_sha256"],estimated_cost=cost,allow_existing_unknown_cost=True,
                reservation_guard=lambda: self.service._fence(context["token"]))
            if not reserved.owner:
                raise ValueError("composition_render_reconciliation_required")
            call = reserved.record
            output = self.service.output_path(owner)
            staged = output.with_name("staged.mp4")
            self.renderer.render_composition(job.id,staged)
            from app.presentation_repair import measure_render_output
            evidence = measure_render_output(staged,job.payload.video_spec)
            context = self.service.require_current_job(job)
            def persist():
                self.service._fence(context["token"])
                if output.exists():
                    raise ValueError("composition_output_already_exists")
                staged.replace(output)
                self.db.connection.execute("INSERT INTO execution_use_content_origins(content_hash,subject_kind,subject_id) VALUES (?,'render',?)",
                    (evidence["sha256"],owner["render_id"]))
                self.service.records.save("result",owner["run_id"],dict(project_id=str(job.project_id),evidence=evidence))
            ledger.finish(project_id=job.project_id,call_id=call.id,status="completed",actual_cost=cost,
                usage_observable=False,result_payload=evidence,persist_result=persist)
        except Exception as exc:
            if call is not None:
                current = ProviderCallRepository(self.db).get(call.id)
                if current is not None and current.status == "running":
                    ledger.finish(project_id=job.project_id,call_id=call.id,status="failed",usage_observable=False,error_code="composition_render_failed")
            # Private staged/uncommitted files stay quarantined and unserved.
            code = str(exc) if isinstance(exc,ValueError) and str(exc).startswith("composition_") else "composition_render_failed"
            raise JobExecutionError(code,code,retryable=False) from None
