"""
Phase 19.3 — Model Profiles router.

Per-user custom model profile CRUD + test-connection endpoint. Keys are
Fernet-encrypted at rest; only masked views (`sk-****abc4`) round-trip to
the frontend.
"""
import logging

from fastapi import APIRouter, Body, Depends, HTTPException, status
from models.schemas import (
    ModelProfileCreate,
    ModelProfilePublic,
    ModelProfileTestResult,
    ModelProfileUpdate,
    ModelProfilesListResponse,
)
from routers.auth import get_current_user
from services.model_profiles import model_profiles_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/model-profiles", tags=["model-profiles"])


@router.get("", response_model=ModelProfilesListResponse)
async def list_profiles(
    current_user: dict = Depends(get_current_user),
) -> ModelProfilesListResponse:
    """List all custom model profiles owned by the current user (masked keys)."""
    docs = await model_profiles_service.list_for_user(current_user["user_id"])
    return ModelProfilesListResponse(
        profiles=[ModelProfilePublic(**d) for d in docs]
    )


@router.post(
    "",
    response_model=ModelProfilePublic,
    status_code=status.HTTP_201_CREATED,
)
async def create_profile(
    body: ModelProfileCreate = Body(...),
    current_user: dict = Depends(get_current_user),
) -> ModelProfilePublic:
    """Create a new custom model profile. api_key is encrypted before storage."""
    doc = await model_profiles_service.create(
        user_id=current_user["user_id"],
        label=body.label,
        base_url=body.base_url,
        model_name=body.model_name,
        api_key=body.api_key,
        extra_params=body.extra_params or {},
    )
    return ModelProfilePublic(**doc)


@router.put("/{profile_id}", response_model=ModelProfilePublic)
async def update_profile(
    profile_id: str,
    body: ModelProfileUpdate = Body(...),
    current_user: dict = Depends(get_current_user),
) -> ModelProfilePublic:
    """Partial update. api_key='' or omitted = leave unchanged."""
    patch = body.model_dump(exclude_none=True)
    doc = await model_profiles_service.update(
        current_user["user_id"], profile_id, patch
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Profile not found")
    return ModelProfilePublic(**doc)


@router.delete("/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_profile(
    profile_id: str,
    current_user: dict = Depends(get_current_user),
) -> None:
    ok = await model_profiles_service.delete(
        current_user["user_id"], profile_id
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Profile not found")


@router.post("/{profile_id}/test", response_model=ModelProfileTestResult)
async def test_profile(
    profile_id: str,
    current_user: dict = Depends(get_current_user),
) -> ModelProfileTestResult:
    """Ping the profile's /chat/completions endpoint with a 1-token 'hi'."""
    result = await model_profiles_service.test_connection(
        current_user["user_id"], profile_id
    )
    return ModelProfileTestResult(**result)
