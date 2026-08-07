"""Answer-provider failure contract (owner directive 2026-08-03).

A provider failure AFTER successful retrieval must never be presented as
missing evidence. The chat layer must return either the retrieval evidence
with sources or the structured verdict:

    {"status": "answer_provider_unavailable",
     "retrieval_succeeded": true,
     "sources": [...]}

These tests lock the ChatChunk field, the helper contract, and the wiring
of all three terminal post-retrieval failure paths in the orchestrator.
"""

from __future__ import annotations

from pathlib import Path

from models.schemas import ChatChunk
from services.chat_orchestrator import _answer_provider_unavailable_chunk

BACKEND = Path(__file__).resolve().parents[1]


def test_chat_chunk_carries_answer_status_field():
    assert "answer_status" in ChatChunk.model_fields
    chunk = ChatChunk(type="error", content="x")
    assert chunk.answer_status is None


def test_helper_contract_shape_with_sources():
    sources = [
        {"doc_id": "doc:1", "chunk_id": "chunk:1", "text": "evidence text"},
        {"doc_id": "doc:2", "chunk_id": "chunk:2", "text": "more evidence"},
    ]
    chunk = _answer_provider_unavailable_chunk(
        conversation_id="conv-1",
        sources=sources,
        detail="LLM streaming error: 401 invalid api key",
    )
    # Error-compatible for existing frontend rendering.
    assert chunk.type == "error"
    assert chunk.conversation_id == "conv-1"
    status = chunk.answer_status
    assert status is not None
    assert status["status"] == "answer_provider_unavailable"
    assert status["retrieval_succeeded"] is True
    assert len(status["sources"]) == 2
    assert status["sources"][0]["doc_id"] == "doc:1"
    assert "401 invalid api key" in status["detail"]
    # The user-visible text must frame this as a PROVIDER failure, never as
    # missing evidence.
    assert "no evidence" not in chunk.content.lower()
    assert "retrieval succeeded" in chunk.content.lower()


def test_helper_serializes_pydantic_sources():
    class FakeSource:
        def __init__(self, chunk_id: str) -> None:
            self.chunk_id = chunk_id

        def model_dump(self, mode: str = "python") -> dict:
            return {"chunk_id": self.chunk_id}

    chunk = _answer_provider_unavailable_chunk(
        conversation_id=None,
        sources=[FakeSource("chunk:9"), "not-serializable"],
        detail="empty stream",
    )
    # Non-serializable entries drop out rather than crash the SSE frame.
    assert chunk.answer_status["sources"] == [{"chunk_id": "chunk:9"}]
    assert chunk.conversation_id is None


def test_helper_empty_sources_still_reports_retrieval_success():
    chunk = _answer_provider_unavailable_chunk(
        conversation_id="conv-2",
        sources=None,
        detail="provider down",
    )
    assert chunk.answer_status["sources"] == []
    assert chunk.answer_status["retrieval_succeeded"] is True


def test_all_terminal_post_retrieval_paths_emit_structured_status():
    src = (BACKEND / "services/chat_orchestrator.py").read_text(encoding="utf-8")
    # Three terminal surfaces: primary stream failure, final no-tool stream
    # failure, and empty-answer-after-fallback. Each must emit the verdict.
    assert src.count("_answer_provider_unavailable_chunk(") >= 4  # def + 3 calls
    # The legacy bare-error wording that read like an evidence failure is gone.
    assert "The model did not return an answer after retrieval. " not in src
    assert src.count('ChatChunk(type="error", content=f"LLM streaming error') == 0


def test_status_constant_matches_owner_contract():
    chunk = _answer_provider_unavailable_chunk(
        conversation_id="c", sources=[], detail="d"
    )
    payload = chunk.model_dump(mode="json")
    assert payload["answer_status"]["status"] == "answer_provider_unavailable"
    assert set(payload["answer_status"]) == {
        "status",
        "retrieval_succeeded",
        "sources",
        "detail",
    }
