"""Minimal local FastAPI entrypoint for the Content OS scaffold."""
from __future__ import annotations

import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from app.db import AssetRepository, ClipRepository, Database, JobRepository, ProjectRepository
from app.domain.models import Asset, Clip, Job, JobStatus, JobType, ProjectFormat, ScenePlan
from app.asset_library import asset_library_page
from app.jobs.targets import AssetJobIdempotencyConflict, AssetJobTargetStore, UnsupportedAssetJobType
from app.providers.embedding import (
    EmbeddingAuthenticationError,
    EmbeddingConfigurationError,
    EmbeddingConnectionError,
    EmbeddingHTTPError,
    EmbeddingInputError,
    EmbeddingProvider,
    EmbeddingProviderResponseError,
    EmbeddingRateLimitError,
    EmbeddingTimeout,
    OpenAICompatibleEmbeddingProvider,
)
from app.providers.scene_planner import (
    OpenAICompatibleScenePlanner,
    ScenePlanner,
    ScenePlannerAuthenticationError,
    ScenePlannerConfigurationError,
    ScenePlannerConnectionError,
    ScenePlannerHTTPError,
    ScenePlannerInputError,
    ScenePlannerProviderResponseError,
    ScenePlannerRateLimitError,
    ScenePlannerTimeout,
)
from app.search import ClipEmbeddingIndexer, EmbeddingIndexError, IndexError


class JobEnqueueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=1, max_length=500)
    project_id: UUID | None = None


class JobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    type: JobType
    status: JobStatus
    attempt: int
    idempotency_key: str
    project_id: UUID | None
    target_asset_id: UUID | None
    created_at: datetime
    updated_at: datetime
    error_code: str | None
    error_message: str | None


class ClipSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=10_000)
    top_k: int = Field(default=10, ge=1, le=100)
    asset_id: UUID | None = None
    orientation: ProjectFormat | None = None
    talking_candidate: bool | None = None


class ClipSearchHitResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    clip: Clip
    score: float = Field(ge=-1, le=1)


class ScenePlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    script: str | None = Field(default=None, min_length=1, max_length=100_000)
    topic: str | None = Field(default=None, min_length=1, max_length=5_000)


class ScenePlanResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: UUID
    scenes: tuple[ScenePlan, ...]


def create_app(
    data_path: str | Path | None = None,
    *,
    embedding_provider: EmbeddingProvider | None = None,
    scene_planner: ScenePlanner | None = None,
) -> FastAPI:
    """Create an app whose SQLite connection belongs to its lifespan thread."""
    selected_path = data_path or os.environ.get("CONTENT_OS_DB_PATH") or Path("content-os-data") / "content-os.sqlite3"

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        selected = Database(selected_path)
        application.state.database = selected
        application.state.database_closed = False
        try:
            yield
        finally:
            selected.close()
            application.state.database_closed = True

    application = FastAPI(title="Content OS API", version="0.1.0", description="Local-first API scaffold for Content OS.", lifespan=lifespan)
    application.state.embedding_provider = embedding_provider
    application.state.scene_planner = scene_planner

    @application.get("/health", tags=["system"])
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "content-os-api"}

    # Read-only local Asset Library. Clip preview reuses the original source;
    # the page seeks the video element to the selected Clip interval.
    @application.get("/asset-library", response_class=HTMLResponse, include_in_schema=False)
    def asset_library() -> HTMLResponse:
        return asset_library_page()

    @application.get("/assets", response_model=list[Asset], tags=["assets"])
    async def list_assets() -> list[Asset]:
        return AssetRepository(application.state.database).list()

    @application.get("/assets/{asset_id}", response_model=Asset, tags=["assets"])
    async def get_asset(asset_id: UUID) -> Asset:
        asset = AssetRepository(application.state.database).get(asset_id)
        if asset is None:
            raise HTTPException(status_code=404, detail="asset not found")
        return asset

    @application.get("/assets/{asset_id}/clips", response_model=list[Clip], tags=["assets"])
    async def list_asset_clips(asset_id: UUID) -> list[Clip]:
        db: Database = application.state.database
        if AssetRepository(db).get(asset_id) is None:
            raise HTTPException(status_code=404, detail="asset not found")
        return ClipRepository(db).list_by_asset(asset_id)

    @application.get("/assets/{asset_id}/media", tags=["assets"])
    async def asset_media(asset_id: UUID, start_ms: int | None = Query(default=None, ge=0), end_ms: int | None = Query(default=None, gt=0)) -> FileResponse:
        db: Database = application.state.database
        asset = AssetRepository(db).get(asset_id)
        if asset is None:
            raise HTTPException(status_code=404, detail="asset not found")
        if (start_ms is None) != (end_ms is None) or (start_ms is not None and end_ms is not None and end_ms <= start_ms):
            raise HTTPException(status_code=422, detail="start_ms and end_ms must be a valid pair")
        if end_ms is not None and end_ms > asset.duration_ms:
            raise HTTPException(status_code=422, detail="media interval exceeds asset duration")
        source = Path(asset.source_file).expanduser().resolve()
        if not source.is_file():
            raise HTTPException(status_code=404, detail="asset source file not found")
        return FileResponse(source)

    @application.post("/assets/{asset_id}/jobs/{job_type}", response_model=JobResponse, status_code=201)
    async def enqueue_asset_job(asset_id: UUID, job_type: JobType, payload: JobEnqueueRequest, request: Request) -> dict[str, Any]:
        db: Database = request.app.state.database
        if AssetRepository(db).get(asset_id) is None:
            raise HTTPException(status_code=404, detail="asset not found")
        if payload.project_id is not None and ProjectRepository(db).get(payload.project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        now = datetime.now(timezone.utc)
        job = Job(id=uuid4(), project_id=payload.project_id, type=job_type, idempotency_key=payload.idempotency_key, created_at=now, updated_at=now)
        try:
            persisted = AssetJobTargetStore(db).enqueue(job, asset_id)
        except AssetJobIdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except UnsupportedAssetJobType as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=404, detail="asset target not found") from exc
        return _job_response(db, persisted)

    @application.get("/jobs/{job_id}", response_model=JobResponse)
    async def get_job(job_id: UUID, request: Request) -> dict[str, Any]:
        db: Database = request.app.state.database
        job = JobRepository(db).get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return _job_response(db, job)

    @application.post("/clips/search", response_model=list[ClipSearchHitResponse])
    async def search_clips(payload: ClipSearchRequest, request: Request) -> list[ClipSearchHitResponse]:
        db: Database = request.app.state.database
        try:
            provider = request.app.state.embedding_provider or _embedding_provider_from_env()
            hits = ClipEmbeddingIndexer(db, provider).search(
                payload.query,
                top_k=payload.top_k,
                asset_id=payload.asset_id,
                orientation=payload.orientation,
                talking_candidate=payload.talking_candidate,
            )
        except (EmbeddingConfigurationError, EmbeddingAuthenticationError) as exc:
            raise HTTPException(status_code=503, detail="embedding provider is not configured") from exc
        except (EmbeddingRateLimitError, EmbeddingTimeout, EmbeddingConnectionError) as exc:
            raise HTTPException(status_code=503, detail="embedding provider is temporarily unavailable") from exc
        except EmbeddingHTTPError as exc:
            status = 503 if exc.status_code >= 500 or exc.status_code in {408, 409, 425, 429} else 502
            raise HTTPException(status_code=status, detail="embedding provider request failed") from exc
        except EmbeddingProviderResponseError as exc:
            raise HTTPException(status_code=502, detail="embedding provider returned an invalid response") from exc
        except (EmbeddingInputError, EmbeddingIndexError, IndexError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return [ClipSearchHitResponse(clip=hit.clip, score=hit.score) for hit in hits]

    @application.post("/projects/{project_id}/scene-plan", response_model=ScenePlanResponse)
    async def create_scene_plan(project_id: UUID, payload: ScenePlanRequest, request: Request) -> ScenePlanResponse:
        project = ProjectRepository(request.app.state.database).get(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        try:
            planner = request.app.state.scene_planner or _scene_planner_from_env()
            result = planner.plan(project, script=payload.script, topic=payload.topic)
        except (ScenePlannerConfigurationError, ScenePlannerAuthenticationError) as exc:
            raise HTTPException(status_code=503, detail="scene planner is not configured") from exc
        except (ScenePlannerRateLimitError, ScenePlannerTimeout, ScenePlannerConnectionError) as exc:
            raise HTTPException(status_code=503, detail="scene planner is temporarily unavailable") from exc
        except ScenePlannerHTTPError as exc:
            status = 503 if exc.status_code >= 500 or exc.status_code in {408, 409, 425, 429} else 502
            raise HTTPException(status_code=status, detail="scene planner request failed") from exc
        except ScenePlannerProviderResponseError as exc:
            raise HTTPException(status_code=502, detail="scene planner returned an invalid response") from exc
        except ScenePlannerInputError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return ScenePlanResponse(project_id=result.project_id, scenes=result.scenes)

    @application.get("/clips/{clip_id}", response_model=Clip, tags=["assets"])
    async def get_clip(clip_id: UUID) -> Clip:
        clip = ClipRepository(application.state.database).get(clip_id)
        if clip is None:
            raise HTTPException(status_code=404, detail="clip not found")
        return clip

    @application.get("/clips/{clip_id}/media", tags=["assets"])
    async def clip_media(clip_id: UUID) -> FileResponse:
        db: Database = application.state.database
        clip = ClipRepository(db).get(clip_id)
        if clip is None:
            raise HTTPException(status_code=404, detail="clip not found")
        asset = AssetRepository(db).get(clip.asset_id)
        if asset is None:
            raise HTTPException(status_code=404, detail="asset not found")
        source = Path(asset.source_file).expanduser().resolve()
        if not source.is_file():
            raise HTTPException(status_code=404, detail="asset source file not found")
        return FileResponse(source)

    return application


def _job_response(db: Database, job: Job) -> dict[str, Any]:
    target = AssetJobTargetStore(db).target_for(job.id)
    values = job.model_dump(mode="json")
    values.pop("schema_version", None)
    return {**values, "target_asset_id": None if target is None else target.asset_id}


def _embedding_provider_from_env() -> OpenAICompatibleEmbeddingProvider:
    api_key = os.environ.get("CONTENT_OS_EMBEDDING_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise EmbeddingConfigurationError("embedding API key must be supplied at runtime")
    raw_dimensions = os.environ.get("CONTENT_OS_EMBEDDING_DIMENSIONS")
    try:
        dimensions = None if raw_dimensions is None else int(raw_dimensions)
    except ValueError as exc:
        raise EmbeddingConfigurationError("embedding dimensions must be an integer") from exc
    return OpenAICompatibleEmbeddingProvider(
        api_key,
        base_url=os.environ.get("CONTENT_OS_EMBEDDING_BASE_URL") or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        model=os.environ.get("CONTENT_OS_EMBEDDING_MODEL", "text-embedding-3-small"),
        dimensions=dimensions,
    )


def _scene_planner_from_env() -> OpenAICompatibleScenePlanner:
    api_key = os.environ.get("CONTENT_OS_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise ScenePlannerConfigurationError("scene planner API key must be supplied at runtime")
    return OpenAICompatibleScenePlanner(
        api_key,
        base_url=os.environ.get("CONTENT_OS_LLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        model=os.environ.get("CONTENT_OS_LLM_MODEL", "gpt-4.1-mini"),
    )


app = create_app()
