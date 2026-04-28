import logging

from fastapi import APIRouter, Depends, HTTPException, status
from models.schemas import Tool, ToolCreate, ToolUpdate
from routers.auth import get_current_user
from services.tool_registry import tool_registry

router = APIRouter(prefix="/api/tools", tags=["tools"])
logger = logging.getLogger(__name__)


@router.get("", response_model=list[Tool], response_model_by_alias=False)
async def list_tools(
    current_user: dict = Depends(get_current_user),
) -> list[Tool]:
    return await tool_registry.list_tools()


@router.post(
    "", response_model=Tool, status_code=status.HTTP_201_CREATED,
    response_model_by_alias=False,
)
async def create_tool(
    tool: ToolCreate,
    current_user: dict = Depends(get_current_user),
) -> Tool:
    from services.skills_registry import SlashCommandConflict

    try:
        return await tool_registry.create_tool(tool)
    except SlashCommandConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/{tool_id}", response_model=Tool, response_model_by_alias=False)
async def update_tool(
    tool_id: str,
    tool: ToolUpdate,
    current_user: dict = Depends(get_current_user),
) -> Tool:
    from services.skills_registry import SlashCommandConflict

    try:
        updated = await tool_registry.update_tool(tool_id, tool)
    except SlashCommandConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not updated:
        raise HTTPException(status_code=404, detail="Tool not found")
    return updated


@router.delete("/{tool_id}", status_code=status.HTTP_200_OK)
async def delete_tool(
    tool_id: str,
    current_user: dict = Depends(get_current_user),
) -> dict[str, bool]:
    deleted = await tool_registry.delete_tool(tool_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Tool not found")
    return {"success": True}
