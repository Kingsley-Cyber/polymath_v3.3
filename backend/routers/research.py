"""Research job and artifact routes."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse

from models.research import (
    ResearchArtifactListResponse,
    ResearchJob,
    ResearchJobCreate,
    ResearchJobListResponse,
    ResearchTraceEventListResponse,
)
from routers.auth import get_current_user
from services.conversation import conversation_service
from services.research import research_service, run_research_job

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/research", tags=["research"])

LimitParam = Annotated[int, Query(ge=1, le=100)]


def _db_or_503():
    db = conversation_service._db
    if db is None:
        raise HTTPException(status_code=503, detail="Database not connected")
    return db


@router.post(
    "/jobs",
    response_model=ResearchJob,
    status_code=status.HTTP_201_CREATED,
    response_model_by_alias=False,
)
async def create_research_job(
    body: ResearchJobCreate,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user),
    run: bool = Query(default=True),
) -> ResearchJob:
    """Create a durable background research job record."""
    job = await research_service.create_job(
        _db_or_503(),
        user_id=current_user["user_id"],
        request=body,
    )
    if run:
        background_tasks.add_task(
            run_research_job,
            _db_or_503(),
            user_id=current_user["user_id"],
            job_id=job.job_id,
        )
    return job


@router.get(
    "/jobs",
    response_model=ResearchJobListResponse,
    response_model_by_alias=False,
)
async def list_research_jobs(
    current_user: dict = Depends(get_current_user),
    limit: LimitParam = 50,
    status_filter: str | None = Query(default=None, alias="status"),
) -> ResearchJobListResponse:
    items = await research_service.list_jobs(
        _db_or_503(),
        user_id=current_user["user_id"],
        limit=limit,
        status=status_filter,
    )
    return ResearchJobListResponse(items=items, count=len(items))


@router.get(
    "/jobs/{job_id}",
    response_model=ResearchJob,
    response_model_by_alias=False,
)
async def get_research_job(
    job_id: str,
    current_user: dict = Depends(get_current_user),
) -> ResearchJob:
    job = await research_service.get_job(
        _db_or_503(),
        user_id=current_user["user_id"],
        job_id=job_id,
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Research job not found")
    return job


@router.post(
    "/jobs/{job_id}/cancel",
    response_model=ResearchJob,
    response_model_by_alias=False,
)
async def cancel_research_job(
    job_id: str,
    current_user: dict = Depends(get_current_user),
) -> ResearchJob:
    job = await research_service.update_job_status(
        _db_or_503(),
        user_id=current_user["user_id"],
        job_id=job_id,
        status="cancelled",
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Research job not found")
    return job


@router.post(
    "/jobs/{job_id}/run",
    response_model=ResearchJob,
    response_model_by_alias=False,
)
async def run_research_job_now(
    job_id: str,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user),
    background: bool = Query(default=True),
) -> ResearchJob:
    job = await research_service.get_job(
        _db_or_503(),
        user_id=current_user["user_id"],
        job_id=job_id,
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Research job not found")
    if background:
        background_tasks.add_task(
            run_research_job,
            _db_or_503(),
            user_id=current_user["user_id"],
            job_id=job_id,
        )
        return job
    result = await run_research_job(
        _db_or_503(),
        user_id=current_user["user_id"],
        job_id=job_id,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Research job not found")
    return result


@router.get(
    "/jobs/{job_id}/events",
    response_model=ResearchTraceEventListResponse,
    response_model_by_alias=False,
)
async def list_research_job_events(
    job_id: str,
    current_user: dict = Depends(get_current_user),
    limit: int = Query(default=200, ge=1, le=1000),
) -> ResearchTraceEventListResponse:
    job = await research_service.get_job(
        _db_or_503(),
        user_id=current_user["user_id"],
        job_id=job_id,
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Research job not found")
    items = await research_service.list_events(
        _db_or_503(),
        user_id=current_user["user_id"],
        job_id=job_id,
        limit=limit,
    )
    return ResearchTraceEventListResponse(items=items, count=len(items))


@router.get(
    "/jobs/{job_id}/artifacts",
    response_model=ResearchArtifactListResponse,
    response_model_by_alias=False,
)
async def list_research_artifacts(
    job_id: str,
    current_user: dict = Depends(get_current_user),
) -> ResearchArtifactListResponse:
    job = await research_service.get_job(
        _db_or_503(),
        user_id=current_user["user_id"],
        job_id=job_id,
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Research job not found")
    items = await research_service.list_artifacts(
        _db_or_503(),
        user_id=current_user["user_id"],
        job_id=job_id,
    )
    return ResearchArtifactListResponse(items=items, count=len(items))


@router.get("/artifacts/{artifact_id}/download")
async def download_research_artifact(
    artifact_id: str,
    current_user: dict = Depends(get_current_user),
) -> FileResponse:
    try:
        resolved = await research_service.resolve_download(
            _db_or_503(),
            user_id=current_user["user_id"],
            artifact_id=artifact_id,
        )
    except ValueError:
        logger.warning("Blocked unsafe research artifact path for %s", artifact_id)
        raise HTTPException(status_code=400, detail="Unsafe artifact path")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Research artifact file missing")
    if resolved is None:
        raise HTTPException(status_code=404, detail="Research artifact not found")
    return FileResponse(
        path=resolved.path,
        filename=resolved.filename,
        media_type=resolved.mime_type,
    )
