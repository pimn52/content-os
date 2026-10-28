"""Ordinary creator UI entry points for external footage."""
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.concurrency import run_in_threadpool

from app.budget import BudgetLimitError
from app.db import Database, ProjectRepository, ProviderCallRepository
from app.domain.external_media import ExternalVideo
from app.external_media import ExternalImportResult, ExternalMediaInputError, ExternalMediaService, ExternalSearchResult
from app.media.ffprobe import FFProbeAdapter, ProbeError
from app.media.importer import MediaImporter, MediaImportError
from app.provider_execution import ProviderExecutionError
from app.providers.commons_media import ExternalMediaError
from app.external_media_connection import external_provider, external_source_connections
from app.runtime import resolve_local_executable


router = APIRouter(prefix="/projects/{project_id}/external-media", tags=["assets"])


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=2, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=500)
    provider: str = Field(default="wikimedia_commons", pattern="^(wikimedia_commons|pexels|pixabay|coverr)$")
    refresh: bool = False

    @model_validator(mode="after")
    def provider_query_limit(self):
        if self.provider == "pixabay" and len(self.query.strip()) > 100:
            raise ValueError("Pixabay 查询最多 100 字符，请使用具体对象与动作")
        return self


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    search_call_id: UUID
    candidate_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=500)


def _service(request: Request, project_id: UUID, provider_name: str = "wikimedia_commons", *, for_import: bool = False, database: Database | None = None) -> ExternalMediaService:
    state = request.app.state
    db = database or state.database
    project = ProjectRepository(db).get(project_id)
    if project is None:
        raise HTTPException(404, "项目不存在")
    importer = state.media_importer_factory(db, state.data_root) if state.media_importer_factory else MediaImporter(
        db, state.data_root, FFProbeAdapter(resolve_local_executable("ffprobe")))
    provider = getattr(state, "external_media_provider", None)
    if provider is None:
        try:
            provider = external_provider(provider_name, project, for_import=for_import)
        except ExternalMediaError as exc:
            raise HTTPException(409, str(exc)) from exc
    return ExternalMediaService(db, Path(state.data_root), provider, importer)


async def _blocking(request: Request, action):
    db = request.app.state.database
    if db.path == ":memory:":
        return action(db)
    def run():
        # 网络与文件探测不阻塞 UI 请求；SQLite 连接始终在其创建线程使用。
        with Database(db.path, startup_mode="existing") as local:
            return action(local)
    return await run_in_threadpool(run)


@router.get("/providers")
async def providers(project_id: UUID, request: Request):
    if ProjectRepository(request.app.state.database).get(project_id) is None:
        raise HTTPException(404, "项目不存在")
    return external_source_connections()


@router.get("/searches", response_model=list[ExternalSearchResult])
async def searches(project_id: UUID, request: Request):
    service = _service(request, project_id)
    calls = [c for c in ProviderCallRepository(service.db).list_for_project(project_id)
             if c.operation == "stock_search" and c.status == "completed"][-5:]
    return [ExternalSearchResult(call_id=c.id, provider=c.provider, query=(c.result_payload or {}).get("result", {}).get("query", ""), candidates=[ExternalVideo.model_validate(v)
            for v in (c.result_payload or {}).get("result", {}).get("candidates", [])]) for c in reversed(calls)]


@router.post("/search", response_model=ExternalSearchResult)
async def search(project_id: UUID, payload: SearchRequest, request: Request):
    try:
        return await _blocking(request, lambda db: _service(request, project_id, payload.provider, database=db).search(
            project_id, payload.query.strip(), payload.idempotency_key, refresh=payload.refresh))
    except BudgetLimitError as exc:
        raise HTTPException(409, exc.code) from exc
    except ProviderExecutionError as exc:
        raise HTTPException(409, exc.code) from exc
    except ExternalMediaError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.post("/imports", response_model=ExternalImportResult, status_code=201)
async def import_video(project_id: UUID, payload: ImportRequest, request: Request):
    try:
        call = ProviderCallRepository(request.app.state.database).get(payload.search_call_id)
        if call is None or call.project_id != project_id:
            raise HTTPException(422, "请从当前项目的检索结果选择素材")
        return await _blocking(request, lambda db: _service(request, project_id, call.provider, for_import=True, database=db).import_video(
            project_id, payload.search_call_id, payload.candidate_id, payload.idempotency_key))
    except BudgetLimitError as exc:
        raise HTTPException(409, exc.code) from exc
    except ProviderExecutionError as exc:
        raise HTTPException(409, exc.code) from exc
    except ExternalMediaInputError as exc:
        raise HTTPException(422, str(exc)) from exc
    except ExternalMediaError as exc:
        raise HTTPException(502, str(exc)) from exc
    except (ProbeError, MediaImportError) as exc:
        raise HTTPException(422, "素材探测或入库失败；请检查 ffprobe 配置和本地文件") from exc
