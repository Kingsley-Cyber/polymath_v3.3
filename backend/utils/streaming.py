# backend/utils/streaming.py
# SSE helpers only. No business logic.

import json
from models.schemas import ChatChunk


def build_sse_chunk(chunk: ChatChunk) -> str:
    """
    Serialize a ChatChunk to SSE wire format.
    Vercel AI SDK useChat expects: data: <json>\n\n
    """
    return f"data: {chunk.model_dump_json()}\n\n"


def build_sse_error(message: str) -> str:
    """Serialize a plain error string to SSE wire format."""
    payload = json.dumps({"type": "error", "content": message})
    return f"data: {payload}\n\n"


def build_sse_done() -> str:
    """Terminal SSE event — signals stream end to client."""
    return "data: [DONE]\n\n"
