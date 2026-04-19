"""
Phase F — Query Preferences router.

GET  /api/query-prefs   → current prefs doc (always synthesizes default empty)
PUT  /api/query-prefs   → partial update with referential integrity validation
"""

import logging

from fastapi import APIRouter, Body, Depends, HTTPException, status

from models.schemas import QueryPrefsResponse, QueryPrefsUpdate
from routers.auth import get_current_user
from services.query_prefs import query_prefs_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/query-prefs", tags=["query-prefs"])


@router.get("", response_model=QueryPrefsResponse)
async def get_prefs(
    current_user: dict = Depends(get_current_user),
) -> QueryPrefsResponse:
    doc = await query_prefs_service.get(current_user["user_id"])
    return QueryPrefsResponse(**doc)


@router.put("", response_model=QueryPrefsResponse)
async def update_prefs(
    body: QueryPrefsUpdate = Body(...),
    current_user: dict = Depends(get_current_user),
) -> QueryPrefsResponse:
    patch = body.model_dump(exclude_unset=True)
    try:
        doc = await query_prefs_service.upsert(current_user["user_id"], patch)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return QueryPrefsResponse(**doc)
