# backend/routers/chat.py
# POST /api/chat - Streaming chat endpoint with SSE
# Thin router: validate → call service → return
# Uses text/event-stream for streaming responses

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from config import get_settings
from models.schemas import ChatRequest
from routers.auth import get_current_user
from services.chat_cost_meter import meter_chat_sse_stream
from services.chat_orchestrator import chat_orchestrator

router = APIRouter(prefix="/api", tags=["chat"])
logger = logging.getLogger(__name__)
settings = get_settings()


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
    # Phase 29 — message-or-attachments joint constraint is enforced by
    # the model validator on ChatRequest, so we no longer reject empty
    # messages here. An attachment-only turn ("what's in this image?")
    # is now a valid request. Pydantic raises 422 on the joint-empty
    # case before this handler ever runs.

    # Call orchestrator service and return streaming response.
    # Phase 19.3 — user_id forwarded so the orchestrator can resolve
    # `profile:<id>` model strings against the Model Profiles collection.
    source = chat_orchestrator.process_chat_request(
        request,
        user_id=current_user["user_id"],
    )
    return StreamingResponse(
        meter_chat_sse_stream(
            source,
            enabled=bool(settings.CHAT_COST_TELEMETRY_ENABLED),
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
