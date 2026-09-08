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
from app.domain.models import Job, JobStatus, JobType
from app.jobs.targets import AssetJobIdempotencyConflict, AssetJobTargetStore, UnsupportedAssetJobType


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


def create_app(data_path: str | Path | None = None) -> FastAPI:
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

    return application


def _job_response(db: Database, job: Job) -> dict[str, Any]:
    target = AssetJobTargetStore(db).target_for(job.id)
    values = job.model_dump(mode="json")
    values.pop("schema_version", None)
    return {**values, "target_asset_id": None if target is None else target.asset_id}


app = create_app()
