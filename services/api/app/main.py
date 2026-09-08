"""Minimal local FastAPI entrypoint for the Content OS scaffold."""
from __future__ import annotations

import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.db import AssetRepository, Database, JobRepository, ProjectRepository
from app.domain.models import Clip, Job, JobStatus, JobType, ProjectFormat
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


def create_app(data_path: str | Path | None = None, *, embedding_provider: EmbeddingProvider | None = None) -> FastAPI:
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

    @application.get("/health", tags=["system"])
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "content-os-api"}

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


app = create_app()
