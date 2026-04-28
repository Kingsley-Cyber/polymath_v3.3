"""
Phase 24 — Skills CRUD router. Mirrors routers/tools.py.

Endpoints:
  GET    /api/skills                — list all skills
  POST   /api/skills                — create
  PATCH  /api/skills/{id}           — update
  DELETE /api/skills/{id}           — delete
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from models.schemas import Skill, SkillCreate, SkillUpdate
from routers.auth import get_current_user
from services.skills_registry import skills_registry, SlashCommandConflict

router = APIRouter(prefix="/api/skills", tags=["skills"])
logger = logging.getLogger(__name__)


@router.get("", response_model=list[Skill], response_model_by_alias=False)
async def list_skills(
    current_user: dict = Depends(get_current_user),
) -> list[Skill]:
    return await skills_registry.list_skills()


@router.post(
    "", response_model=Skill, status_code=status.HTTP_201_CREATED,
    response_model_by_alias=False,
)
async def create_skill(
    skill: SkillCreate,
    current_user: dict = Depends(get_current_user),
) -> Skill:
    try:
        return await skills_registry.create_skill(skill)
    except SlashCommandConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch(
    "/{skill_id}", response_model=Skill, response_model_by_alias=False,
)
async def update_skill(
    skill_id: str,
    skill: SkillUpdate,
    current_user: dict = Depends(get_current_user),
) -> Skill:
    try:
        updated = await skills_registry.update_skill(skill_id, skill)
    except SlashCommandConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not updated:
        raise HTTPException(status_code=404, detail="Skill not found")
    return updated


@router.delete("/{skill_id}", status_code=status.HTTP_200_OK)
async def delete_skill(
    skill_id: str,
    current_user: dict = Depends(get_current_user),
) -> dict[str, bool]:
    deleted = await skills_registry.delete_skill(skill_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Skill not found")
    return {"success": True}
