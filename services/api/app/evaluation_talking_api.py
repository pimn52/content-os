"""Thin explicit evaluation Talking entry; GET never schedules work."""
from uuid import UUID
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from app.domain.evaluation_master import MasterAdoption, MasterRevocation
from app.domain.evaluation_talking import (TalkingSelectionRecord, TalkingStartRecord, TalkingCalibrationIntent, EvaluationTalkingReviewRequest,
    EvaluationTalkingConcernRequest, EvaluationTalkingConcernAnswersRequest)
from app.evaluation_talking import EvaluationTalkingService
from app.evaluation_talking_access import EvaluationTalkingAccess, public_reason
from app.media.importer import MediaImportError

router = APIRouter(prefix="/projects/{project_id}/evaluation-talking", tags=["projects"])


def service(request):
    return EvaluationTalkingService(request.app.state.database, request.app.state.data_root,
        cost_estimator=request.app.state.cost_estimator)


def action(fn):
    try:
        return fn()
    except (ValueError, KeyError, TypeError, OSError, MediaImportError) as exc:
        raise HTTPException(409, detail=public_reason(exc)) from None


def owned(project_id, identity, request):
    access = EvaluationTalkingAccess(service(request))
    value = action(lambda: access.operation(project_id, identity))
    return access, value


def review_context(project_id, identity, request):
    access, operation = owned(project_id, identity, request)
    series = access.admission.series.get(operation.series_id)
    if series is None or series.project_id != project_id:
        raise HTTPException(409, "evaluation_talking_preview_not_completed")
    return access, series


def fields(payload):
    return {name:getattr(payload,name) for name in type(payload).model_fields}


@router.get("/admissions")
async def admissions(project_id: UUID, request: Request):
    owner = service(request)
    return action(lambda: [owner.records.admission(row[0]) for row in owner.db.connection.execute(
        "SELECT id FROM evaluation_talking_use_admissions WHERE project_id=? ORDER BY rowid DESC", (str(project_id),))])


@router.get("/candidates")
async def candidates(project_id: UUID, request: Request):
    return action(lambda: service(request).candidates(project_id))


@router.post("/admissions", status_code=201)
async def adopt(project_id: UUID, payload: MasterAdoption, request: Request):
    return action(lambda: service(request).adopt(project_id,payload,request.headers.get("x-content-os-execution-admission-key")))


@router.post("/admissions/{identity}/revoke")
async def revoke(project_id: UUID, identity: UUID, payload: MasterRevocation, request: Request):
    return action(lambda: service(request).revoke(project_id,identity,payload.reason,request.headers.get("x-content-os-execution-admission-key")))


@router.post("/preflight")
async def preflight(project_id: UUID, payload: TalkingSelectionRecord, request: Request):
    return action(lambda: service(request).preflight(project_id,payload))


@router.post("/operations", status_code=201)
async def create(project_id: UUID, payload: TalkingStartRecord, request: Request):
    return action(lambda: service(request).create(project_id,payload))


@router.post("/calibration-intent")
async def calibration_intent(project_id: UUID, payload: TalkingCalibrationIntent, request: Request):
    return action(lambda: service(request).calibration_intent(project_id,payload))


@router.get("/operations")
async def operations(project_id: UUID, request: Request):
    return action(lambda: EvaluationTalkingAccess(service(request)).list_status(project_id))


@router.get("/recoveries")
async def recoveries(project_id: UUID, request: Request):
    from app.evaluation_talking_recovery import TalkingRecoveryService
    return action(lambda: TalkingRecoveryService(service(request)).candidates(project_id))


from app.domain.talking_recovery import TalkingRecoveryPreflight, TalkingRecoveryStart


@router.post("/operations/{identity}/recovery/preflight")
async def recovery_preflight(project_id: UUID, identity: UUID, payload: TalkingRecoveryPreflight, request: Request):
    from app.evaluation_talking_recovery import TalkingRecoveryService
    return action(lambda: TalkingRecoveryService(service(request)).preflight(project_id,identity,payload.selection)[0])


@router.post("/operations/{identity}/recovery",status_code=201)
async def recovery_create(project_id: UUID, identity: UUID, payload: TalkingRecoveryStart, request: Request):
    from app.evaluation_talking_recovery import TalkingRecoveryService
    return action(lambda: TalkingRecoveryService(service(request)).create(project_id,identity,payload,
        request.headers.get("x-content-os-execution-admission-key")))


@router.get("/operations/{identity}/recovery")
async def recovery_status(project_id: UUID, identity: UUID, request: Request):
    from app.evaluation_talking_recovery import TalkingRecoveryService
    return action(lambda: TalkingRecoveryService(service(request)).status(project_id,identity))


@router.get("/operations/{identity}")
async def status(project_id: UUID, identity: UUID, request: Request):
    access, _ = owned(project_id,identity,request)
    return action(lambda: access.status(project_id,identity))


@router.post("/operations/{identity}/cancel")
async def cancel(project_id: UUID, identity: UUID, request: Request):
    access, _ = owned(project_id,identity,request)
    return action(lambda: access.owner.cancel(project_id,identity))


@router.post("/operations/{identity}/qa")
async def qa(project_id: UUID, identity: UUID, request: Request):
    access, operation = owned(project_id,identity,request)
    return action(lambda: access.owner.create_qa(operation))


@router.post("/operations/{identity}/preview")
async def preview(project_id: UUID, identity: UUID, request: Request):
    access, operation = owned(project_id,identity,request)
    return action(lambda: access.owner.create_preview(operation))


@router.get("/operations/{identity}/preview/access")
async def preview_access(project_id: UUID, identity: UUID, request: Request, purpose: str | None = None):
    access, _ = owned(project_id,identity,request)
    return action(lambda: access.preview(project_id,identity,purpose=purpose)[0])


@router.get("/operations/{identity}/preview/media")
async def preview_media(project_id: UUID, identity: UUID, request: Request, purpose: str | None = None):
    access, _ = owned(project_id,identity,request)
    info, path = action(lambda: access.preview(project_id,identity,purpose=purpose))
    if path is None:
        raise HTTPException(403, info["reasons"][0])
    return FileResponse(path, media_type="video/mp4", headers={"Cache-Control":"no-store"})


@router.get("/operations/{identity}/review")
async def review_status(project_id: UUID, identity: UUID, request: Request):
    access, series = review_context(project_id,identity,request)
    return action(lambda: access.admission.review_status(series))


@router.post("/operations/{identity}/review")
async def review(project_id: UUID, identity: UUID, payload: EvaluationTalkingReviewRequest, request: Request):
    access, series = review_context(project_id,identity,request)
    return action(lambda: access.admission.record_review(series,**fields(payload)))


@router.post("/operations/{identity}/concerns")
async def concern(project_id: UUID, identity: UUID, payload: EvaluationTalkingConcernRequest, request: Request):
    access, series = review_context(project_id,identity,request)
    return action(lambda: access.admission.register_concern(series,**fields(payload)))


@router.post("/operations/{identity}/concern-answers")
async def concern_answers(project_id: UUID, identity: UUID, payload: EvaluationTalkingConcernAnswersRequest, request: Request):
    access, series = review_context(project_id,identity,request)
    return action(lambda: access.admission.answer_concerns(series,**fields(payload)))


@router.post("/operations/{identity}/admit")
async def admit(project_id: UUID, identity: UUID, request: Request):
    access, series = review_context(project_id,identity,request)
    return action(lambda: access.admission.admit(series))
