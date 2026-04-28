# backend/routers/conversations.py
# CRUD operations for conversations
# Thin router: validate → call service → return
# All business logic lives in services/conversation.py

import logging
from typing import Annotated

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, status
from models.schemas import (
    Conversation,
    ConversationCreate,
    ConversationListItem,
    ConversationUpdate,
    ErrorResponse,
)
from routers.auth import get_current_user
from services.conversation import conversation_service

router = APIRouter(prefix="/api/conversations", tags=["conversations"])
logger = logging.getLogger(__name__)

# Type aliases for dependency injection
LimitParam = Annotated[
    int, Query(ge=1, le=100, description="Max conversations to return")
]
OffsetParam = Annotated[int, Query(ge=0, description="Number of conversations to skip")]


@router.get(
    "",
    response_model=list[ConversationListItem],
    status_code=status.HTTP_200_OK,
    response_model_by_alias=False,  # emit `id` (field name), not `_id` (Mongo alias)
    summary="List all conversations",
    description="Returns a paginated list of conversations sorted by most recent.",
)
async def list_conversations(
    current_user: dict = Depends(get_current_user),
    limit: LimitParam = 50,
    offset: OffsetParam = 0,
) -> list[ConversationListItem]:
    """
    List conversations with pagination.

    Args:
        limit: Maximum number of conversations to return (1-100)
        offset: Number of conversations to skip

    Returns:
        List of ConversationListItem objects
    """
    try:
        conversations = await conversation_service.list_conversations(
            limit=limit,
            offset=offset,
        )
        return conversations
    except Exception as e:
        logger.error(f"Failed to list conversations: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve conversations",
        )


@router.get(
    "/{conversation_id}",
    response_model=Conversation,
    status_code=status.HTTP_200_OK,
    summary="Get a conversation",
    description="Returns a single conversation with all its messages.",
    responses={
        404: {"model": ErrorResponse, "description": "Conversation not found"},
        400: {"model": ErrorResponse, "description": "Invalid conversation ID format"},
    },
)
async def get_conversation(
    conversation_id: str,
    current_user: dict = Depends(get_current_user),
) -> Conversation:
    """
    Get a conversation by ID.

    Args:
        conversation_id: MongoDB ObjectId as string

    Returns:
        Conversation object with messages

    Raises:
        HTTPException: 400 if invalid ID format, 404 if not found
    """
    # Validate ObjectId format
    if not ObjectId.is_valid(conversation_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid conversation ID format: {conversation_id}",
        )

    conversation = await conversation_service.get_conversation(conversation_id)

    if conversation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Conversation not found: {conversation_id}",
        )

    return conversation


@router.post(
    "",
    response_model=dict[str, str],
    status_code=status.HTTP_201_CREATED,
    summary="Create a new conversation",
    description="Creates a new conversation and returns its ID.",
    responses={
        400: {"model": ErrorResponse, "description": "Invalid request body"},
    },
)
async def create_conversation(
    payload: ConversationCreate | None = None,
    current_user: dict = Depends(get_current_user),
) -> dict[str, str]:
    """
    Create a new conversation.

    Args:
        payload: Optional conversation settings (title, model_config)

    Returns:
        Dict with the new conversation ID

    Example:
        POST /api/conversations
        Body: {"title": "My Chat", "model_config": {"model": "ollama/llama3.2:3b"}}
        Response: {"id": "507f1f77bcf86cd799439011"}
    """
    try:
        # Use defaults if no payload provided
        title = "New Conversation"
        model_config = None

        if payload:
            title = payload.title or title
            model_config = payload.llm_config

        conversation_id = await conversation_service.create_conversation(
            title=title,
            model_config=model_config,
        )

        return {"id": conversation_id}

    except Exception as e:
        logger.error(f"Failed to create conversation: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create conversation",
        )


@router.patch(
    "/{conversation_id}",
    response_model=dict[str, bool],
    status_code=status.HTTP_200_OK,
    summary="Update a conversation",
    description="Updates conversation title or model configuration.",
    responses={
        404: {"model": ErrorResponse, "description": "Conversation not found"},
        400: {"model": ErrorResponse, "description": "Invalid request body"},
    },
)
async def update_conversation(
    conversation_id: str,
    payload: ConversationUpdate,
    current_user: dict = Depends(get_current_user),
) -> dict[str, bool]:
    """
    Update a conversation's title or model config.

    Args:
        conversation_id: MongoDB ObjectId as string
        payload: Fields to update (title, model_config)

    Returns:
        Dict with success status

    Raises:
        HTTPException: 400 if invalid ID, 404 if not found
    """
    # Validate ObjectId format
    if not ObjectId.is_valid(conversation_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid conversation ID format: {conversation_id}",
        )

    try:
        updated = False

        # Update title if provided
        if payload.title is not None:
            updated = await conversation_service.update_conversation_title(
                conversation_id=conversation_id,
                title=payload.title,
            )

        # Update model config if provided
        if payload.llm_config is not None:
            config_updated = await conversation_service.update_model_config(
                conversation_id=conversation_id,
                model_config=payload.llm_config,
            )
            updated = updated or config_updated

        if not updated:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Conversation not found: {conversation_id}",
            )

        return {"success": True}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update conversation: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update conversation",
        )


@router.delete(
    "/{conversation_id}",
    response_model=dict[str, bool],
    status_code=status.HTTP_200_OK,
    summary="Delete a conversation",
    description="Permanently deletes a conversation and all its messages.",
    responses={
        404: {"model": ErrorResponse, "description": "Conversation not found"},
        400: {"model": ErrorResponse, "description": "Invalid conversation ID format"},
    },
)
async def delete_conversation(
    conversation_id: str,
    current_user: dict = Depends(get_current_user),
) -> dict[str, bool]:
    """
    Delete a conversation.

    Args:
        conversation_id: MongoDB ObjectId as string

    Returns:
        Dict with success status

    Raises:
        HTTPException: 400 if invalid ID, 404 if not found
    """
    # Validate ObjectId format
    if not ObjectId.is_valid(conversation_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid conversation ID format: {conversation_id}",
        )

    deleted = await conversation_service.delete_conversation(conversation_id)

    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Conversation not found: {conversation_id}",
        )

    return {"success": True}
