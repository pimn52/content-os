"""普通素材界面的可选网页发现；不自动付费抓目录或生成媒体。"""
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.budget import BudgetLimitError
from app.db import ProjectRepository, ProviderCallRepository
from app.external_media_api import _blocking, _service as media_service
from app.providers.commons_media import ExternalMediaError
from app.media.web_discovery import classify_search_results, is_page_candidate
from app.provider_execution import ProviderExecutionError
from app.web_discovery import WebDiscoveryService, page_resolution
from app.web_discovery_connection import WebConnectionError, web_provider, web_provider_connections

router=APIRouter(prefix="/projects/{project_id}/external-media",tags=["assets"])


class WebSearchRequest(BaseModel):
    model_config=ConfigDict(extra="forbid")
    provider: str=Field(pattern="^[a-z][a-z0-9_-]{0,63}$")
    query: str=Field(min_length=2,max_length=128)
    idempotency_key: str=Field(min_length=1,max_length=400)
    refresh: bool=False


class WebFetchRequest(BaseModel):
    model_config=ConfigDict(extra="forbid")
    search_call_id: UUID
    url: str=Field(min_length=1,max_length=8192)
    idempotency_key: str=Field(min_length=1,max_length=400)


@router.post("/resolve-page")
async def resolve_page(project_id:UUID,payload:WebFetchRequest,request:Request):
    call=ProviderCallRepository(request.app.state.database).get(payload.search_call_id)
    if not call or call.project_id!=project_id or call.operation!="web_search" or call.status!="completed":
        raise HTTPException(422,"请选择当前项目已保存的网页搜索结果")
    raw=call.result_payload["result"].get("document",call.result_payload["result"])
    page=next((p for p in classify_search_results(raw) if p.url==payload.url and is_page_candidate(p)),None)
    if page is None:
        raise HTTPException(422,"请选择具体视频详情页，目录和验证页不能当作视频文件")
    resolution=page_resolution(page)
    if not resolution["can_resolve"]:
        raise HTTPException(422,resolution["resolution_reason"])
    try:
        def resolve(db):
            provider_name = resolution["resolver_provider"]
            service=media_service(request,project_id,provider_name,database=db)
            if provider_name == "public_page":
                service.provider.check_source(payload.url)
            return service.search(project_id,payload.url,payload.idempotency_key)
        return await _blocking(request,resolve)
    except (BudgetLimitError,ProviderExecutionError) as exc:
        raise HTTPException(409,exc.code) from exc
    except ExternalMediaError as exc:
        raise HTTPException(422,getattr(exc,"code","public_page_unavailable")) from exc


def _service(db,request,project_id,name,*,capability="search"):
    if not ProjectRepository(db).get(project_id):
        raise HTTPException(404,"项目不存在")
    connection=next((row for row in web_provider_connections() if row["id"]==name),None)
    if connection is None or not connection.get(capability):
        raise HTTPException(422,"此连接未提供所需网页工具能力")
    provider=getattr(request.app.state,"web_discovery_provider",None)
    if provider is None or provider.provider_name!=name:
        try:
            provider=web_provider(name,capability=capability)
        except WebConnectionError as exc:
            raise HTTPException(409,str(exc)) from exc
    return WebDiscoveryService(db,provider,supports_fetch=connection["fetch"])


@router.get("/web-providers")
async def providers(project_id:UUID,request:Request):
    if not ProjectRepository(request.app.state.database).get(project_id):
        raise HTTPException(404,"项目不存在")
    return web_provider_connections()


@router.post("/web-search")
async def search(project_id:UUID,payload:WebSearchRequest,request:Request):
    try:
        return await _blocking(request,lambda db:_service(db,request,project_id,payload.provider).search(
            project_id,payload.query,payload.idempotency_key,refresh=payload.refresh))
    except (BudgetLimitError,ProviderExecutionError) as exc:
        raise HTTPException(409,exc.code) from exc
    except RuntimeError as exc:
        raise HTTPException(502,getattr(exc,"code","web_discovery_failed")) from exc
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from exc


@router.get("/web-searches")
async def searches(project_id:UUID,request:Request):
    db=request.app.state.database
    if not ProjectRepository(db).get(project_id):
        raise HTTPException(404,"项目不存在")
    calls=[c for c in ProviderCallRepository(db).list_for_project(project_id)
        if c.operation=="web_search" and c.status=="completed" and c.input_source=="external_web"][-5:]
    return [WebDiscoveryService.search_snapshot(c) for c in reversed(calls)]


@router.post("/web-fetch")
async def fetch(project_id:UUID,payload:WebFetchRequest,request:Request):
    call=ProviderCallRepository(request.app.state.database).get(payload.search_call_id)
    if not call or call.project_id!=project_id or call.operation!="web_search" or call.status!="completed":
        raise HTTPException(422,"请选择当前项目的网页发现结果")
    try:
        return await _blocking(request,lambda db:_service(db,request,project_id,call.provider,capability="fetch").fetch(
            project_id,payload.search_call_id,payload.url,payload.idempotency_key))
    except (BudgetLimitError,ProviderExecutionError) as exc:
        raise HTTPException(409,exc.code) from exc
    except RuntimeError as exc:
        raise HTTPException(502,getattr(exc,"code","web_discovery_failed")) from exc
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from exc
