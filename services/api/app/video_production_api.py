"""普通主题制作与未发布版本选择入口。"""
from uuid import UUID
from fastapi import APIRouter,HTTPException,Request
from pydantic import BaseModel,ConfigDict,Field
from app.video_production import VideoProductionService
from app.production_runs import ProductionRunNotReady
from app.source_usage import SourceUsageService,SourceUsageConflict

router=APIRouter(tags=["production"])

class VideoStart(BaseModel):
    model_config=ConfigDict(extra="forbid")
    topic:str=Field(min_length=1,max_length=5000)
    idempotency_key:str=Field(min_length=1,max_length=500)
    candidate_of_run_id:UUID|None=None
    resume_from_job_id:UUID|None=None
    expected_draft_version:int|None=Field(default=None,ge=1)

class RenderChoice(BaseModel):
    model_config=ConfigDict(extra="forbid")
    disposition:str=Field(pattern="^(selected|discarded)$")

@router.post("/projects/{project_id}/auto-produce",status_code=201)
async def start_video(project_id:UUID,payload:VideoStart,request:Request):
    service=VideoProductionService(request.app.state.database,request.app.state.data_root)
    try:
        job=service.enqueue(project_id,payload.topic,payload.idempotency_key,candidate_of_run_id=payload.candidate_of_run_id,
            resume_from_job_id=payload.resume_from_job_id,expected_draft_version=payload.expected_draft_version)
        return {"start_job_id":str(job.id),"status":job.status}
    except ProductionRunNotReady as exc:
        raise HTTPException(409,detail={"reasons":exc.reasons}) from exc
    except LookupError as exc:raise HTTPException(404,detail=str(exc)) from exc
    except ValueError as exc:raise HTTPException(409,detail=str(exc)) from exc

@router.get("/projects/{project_id}/auto-produce")
async def video_status(project_id:UUID,request:Request):
    return VideoProductionService(request.app.state.database,request.app.state.data_root).status(project_id)

@router.get("/projects/{project_id}/talking-pool")
async def talking_pool(project_id:UUID,request:Request):
    from app.db import ProjectRepository
    from app.talking.pool import build_talking_pool
    if ProjectRepository(request.app.state.database).get(project_id) is None:
        raise HTTPException(404,detail="project not found")
    return build_talking_pool(request.app.state.database,project_id,
        data_root=request.app.state.data_root).summary()

@router.get("/projects/{project_id}/renders/{render_id}/choice")
async def render_choice(project_id:UUID,render_id:UUID,request:Request):
    return {"render_id":str(render_id),"disposition":SourceUsageService(request.app.state.database).choice(project_id,render_id)}

@router.put("/projects/{project_id}/renders/{render_id}/choice")
async def choose_render(project_id:UUID,render_id:UUID,payload:RenderChoice,request:Request):
    try:return SourceUsageService(request.app.state.database).choose(project_id,render_id,payload.disposition)
    except SourceUsageConflict as exc:raise HTTPException(409,detail={"reasons":[exc.reason]}) from exc

@router.get("/projects/{project_id}/renders/{render_id}/choice-impact")
async def render_choice_impact(project_id:UUID,render_id:UUID,request:Request):
    try:return SourceUsageService(request.app.state.database).choice_impact(project_id,render_id)
    except SourceUsageConflict as exc:raise HTTPException(409,detail={"reasons":[exc.reason]}) from exc
