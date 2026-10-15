"""Thin explicit-use endpoints, using the normal project and Run resources."""
from uuid import UUID
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from app.db import AudioAssetRepository
from app.execution_scope import ExecutionScopeService
from app.media.audio_access import AudioAccess
from app.domain.evaluation_master import MasterSelection, MasterStart, MasterAdoption, MasterRevocation
from app.evaluation_master import EvaluationMasterService

router = APIRouter(prefix="/projects/{project_id}/evaluation-master", tags=["projects"])


def service(request):
    return EvaluationMasterService(request.app.state.database, request.app.state.data_root)


def action(fn):
    try:
        return fn()
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise HTTPException(409, detail=str(exc) if isinstance(exc, ValueError) else "evaluation_master_evidence_invalid") from None


@router.get("/candidates")
async def candidates(project_id: UUID, request: Request):
    return action(lambda: service(request).candidates(project_id))


@router.get("/admissions")
async def admissions(project_id: UUID, request: Request):
    return action(lambda: service(request).records.admissions(project_id))


@router.post("/admissions", status_code=201)
async def adopt(project_id: UUID, payload: MasterAdoption, request: Request):
    return action(lambda: service(request).adopt(project_id, payload, request.headers.get("x-content-os-execution-admission-key")))


@router.post("/admissions/{admission_id}/revoke")
async def revoke(project_id: UUID, admission_id: UUID, payload: MasterRevocation, request: Request):
    return action(lambda: service(request).revoke(project_id, admission_id, payload.reason, request.headers.get("x-content-os-execution-admission-key")))


@router.post("/preflight")
async def preflight(project_id: UUID, payload: MasterSelection, request: Request):
    return service(request).preflight(project_id, payload)


@router.post("/runs", status_code=201)
async def create(project_id: UUID, payload: MasterStart, request: Request):
    return action(lambda: service(request).create(project_id, payload))


@router.get("/runs")
async def runs(project_id: UUID, request: Request):
    return action(lambda: service(request).list_runs(project_id))


def audio_access(project_id, run_id, request, purpose):
    consumer = service(request)
    run = action(lambda: consumer.get(project_id, run_id))
    if run is None:
        raise HTTPException(404, "production run not found")
    info = run.evaluation_master
    binding = info["historical_binding"]
    audio_id = None if binding is None else UUID(binding["selection"]["audio_id"])
    audio = None if audio_id is None else AudioAssetRepository(consumer.db).get(audio_id)
    result = dict(audio_id=None if audio_id is None else str(audio_id), state="blocked", restricted=True,
        reasons=info["reasons"] or ["evaluation_master_use_not_current"],
        manifest=ExecutionScopeService(consumer.db, consumer.root).manifest("run", run_id))
    if purpose != "internal_evaluation":
        result["reasons"] = ["evaluation_scope_not_commercial"]
        return result, None
    if not info["current_eligible"] or audio is None:
        return result, None
    # Both the new consumer receipt and original byte-review authority apply.
    return AudioAccess(consumer.db, consumer.root).inspect(audio, project_id=project_id, purpose=purpose)


@router.get("/runs/{run_id}/audio/access")
async def access(project_id: UUID, run_id: UUID, request: Request, purpose: str | None = None):
    return audio_access(project_id, run_id, request, purpose)[0]


@router.get("/runs/{run_id}/audio/media")
async def media(project_id: UUID, run_id: UUID, request: Request, purpose: str | None = None):
    status, source = audio_access(project_id, run_id, request, purpose)
    if source is None:
        raise HTTPException(403, status["reasons"][0])
    return FileResponse(source, headers={"Cache-Control": "no-store"})
