# backend/routers/chat.py
# POST /api/chat - Streaming chat endpoint with SSE
# Thin router: validate → call service → return
# Uses text/event-stream for streaming responses

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from models.schemas import ChatRequest
from routers.auth import get_current_user
from services.chat_orchestrator import chat_orchestrator

router = APIRouter(prefix="/api", tags=["chat"])
logger = logging.getLogger(__name__)


@router.post("/chat")
async def chat(
    request: ChatRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Chat endpoint with streaming SSE response.

    Accepts a message and optional conversation_id.
    Creates new conversation if conversation_id is null.
    Streams response tokens via Server-Sent Events.

    Request body:
        conversation_id: Optional existing conversation ID
        message: User message content
        overrides: Optional model parameter overrides

    Returns:
        StreamingResponse with text/event-stream content type

    SSE Events:
        - type="trimming": History was trimmed to fit context
        - type="token": Individual response token
        - type="error": Error occurred
        - type="done": Stream complete
    """
    # Validate request
    if not request.message or not request.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    # Call orchestrator service and return streaming response.
    # Phase 19.3 — user_id forwarded so the orchestrator can resolve
    # `profile:<id>` model strings against the Model Profiles collection.
    return StreamingResponse(
        chat_orchestrator.process_chat_request(
            request, user_id=current_user["user_id"]
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
