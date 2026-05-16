"""
Phase E — Unified Model Pool router.

CRUD + test for per-user model pool entries. Plaintext keys encrypted at
rest; only masked views round-trip to the frontend.
"""
import logging

from fastapi import APIRouter, Body, Depends, HTTPException, status
from models.schemas import (
    ModelPoolEntryCreate,
    ModelPoolEntryPublic,
    ModelPoolEntryUpdate,
    ModelPoolListResponse,
    ModelPoolTestResult,
)
from routers.auth import get_current_user
from services.model_pool import model_pool_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/model-pool", tags=["model-pool"])


@router.get("", response_model=ModelPoolListResponse)
async def list_pool(
    current_user: dict = Depends(get_current_user),
) -> ModelPoolListResponse:
    # Best-effort one-shot migration from Phase B profiles the first time the
    # pool is hit for this user.
    try:
        await model_pool_service.migrate_from_legacy(current_user["user_id"])
    except Exception as exc:
        logger.warning("Pool migration skipped: %s", exc)
    entries = await model_pool_service.list_for_user(current_user["user_id"])
    return ModelPoolListResponse(
        entries=[ModelPoolEntryPublic(**e) for e in entries]
    )


@router.post(
    "", response_model=ModelPoolEntryPublic, status_code=status.HTTP_201_CREATED
)
async def create_entry(
    body: ModelPoolEntryCreate = Body(...),
    current_user: dict = Depends(get_current_user),
) -> ModelPoolEntryPublic:
    # Pt10d — validation errors surface as 422 with a precise hint
    # rather than a 500 (and rather than silently 400-ing at synthesis
    # time, which is the failure mode this validator prevents).
    from services.model_pool import InvalidModelNameError
    try:
        entry = await model_pool_service.create(
            user_id=current_user["user_id"],
            label=body.label,
            provider=body.provider,
            base_url=body.base_url,
            model_name=body.model_name,
            api_key=body.api_key,
            use_shared_key=body.use_shared_key,
            extra_params=body.extra_params or {},
            context_length=body.context_length,
            tags=body.tags or ["chat"],
            enabled=body.enabled,
        )
    except InvalidModelNameError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return ModelPoolEntryPublic(**entry)


@router.put("/{entry_id}", response_model=ModelPoolEntryPublic)
async def update_entry(
    entry_id: str,
    body: ModelPoolEntryUpdate = Body(...),
    current_user: dict = Depends(get_current_user),
) -> ModelPoolEntryPublic:
    from services.model_pool import InvalidModelNameError
    patch = body.model_dump(exclude_none=True)
    try:
        entry = await model_pool_service.update(
            current_user["user_id"], entry_id, patch
        )
    except InvalidModelNameError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if not entry:
        raise HTTPException(status_code=404, detail="Pool entry not found")
    return ModelPoolEntryPublic(**entry)


@router.delete("/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_entry(
    entry_id: str,
    current_user: dict = Depends(get_current_user),
) -> None:
    ok = await model_pool_service.delete(current_user["user_id"], entry_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Pool entry not found")


@router.post("/{entry_id}/test", response_model=ModelPoolTestResult)
async def test_entry(
    entry_id: str,
    current_user: dict = Depends(get_current_user),
) -> ModelPoolTestResult:
    result = await model_pool_service.test_connection(
        current_user["user_id"], entry_id
    )
    return ModelPoolTestResult(**result)
