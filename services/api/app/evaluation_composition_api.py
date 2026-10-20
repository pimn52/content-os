"""Operator media-use/allowance adoption; execution belongs to ProductionRun."""
from uuid import UUID
from fastapi import APIRouter, Request
from app.domain.evaluation_composition import CompositionSelection, CompositionAdoption
from app.domain.evaluation_master import MasterRevocation
from app.evaluation_composition import EvaluationCompositionService
from app.evaluation_master_api import action

router = APIRouter(prefix="/projects/{project_id}/evaluation-composition",tags=["projects"])


def service(request):
    return EvaluationCompositionService(request.app.state.database,request.app.state.data_root)


@router.post("/preflight")
async def preflight(project_id: UUID, payload: CompositionSelection, request: Request):
    return service(request).preflight(project_id,payload)


@router.get("/admissions")
async def admissions(project_id: UUID, request: Request):
    return action(lambda: [v["value"] for v in service(request).records.list("admission",project_id)])


@router.post("/admissions",status_code=201)
async def adopt(project_id: UUID, payload: CompositionAdoption, request: Request):
    return action(lambda: service(request).adopt(project_id,payload,request.headers.get("x-content-os-execution-admission-key")))


@router.post("/admissions/{identity}/revoke")
async def revoke(project_id: UUID, identity: UUID, payload: MasterRevocation, request: Request):
    return action(lambda: service(request).revoke(project_id,identity,payload.reason,request.headers.get("x-content-os-execution-admission-key")))


@router.get("/batches")
async def batches(project_id: UUID, request: Request):
    return action(lambda: service(request).records.list("batch",project_id))


@router.post("/batches",status_code=201)
async def batch(project_id: UUID, payload: CompositionAdoption, request: Request):
    return action(lambda: service(request).adopt_batch(project_id,payload,request.headers.get("x-content-os-execution-admission-key")))


@router.post("/previews",status_code=201)
async def preview(project_id: UUID, payload: CompositionAdoption, request: Request):
    s = service(request)
    identity = action(lambda: s.start_preview(project_id,payload,request.headers.get("x-content-os-execution-admission-key")))
    return action(lambda: s.get(project_id,identity))


@router.get("/runs")
async def runs(project_id: UUID, request: Request):
    # Ordinary production has no history-list equivalent yet; retain discovery,
    # without another start, reconcile or media execution entry.
    s = service(request)
    return action(lambda: [s.get(project_id,UUID(r[0])) for r in s.db.connection.execute(
        "SELECT run_id FROM evaluation_composition_owners WHERE project_id=? ORDER BY rowid DESC",(str(project_id),))])
