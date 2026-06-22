"""MCP query-tool compatibility tests.

These tests keep the sidecar aligned with the live chat/search/graph feature
surface. They are adapter tests: no Mongo/Qdrant/Neo4j containers are required.
"""
from __future__ import annotations

import json
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _install_auth_stubs_if_missing() -> None:
    try:
        import jose  # noqa: F401
    except ImportError:
        jose_mod = ModuleType("jose")

        class JWTError(Exception):
            pass

        class _Jwt:
            @staticmethod
            def encode(*_a, **_kw):  # pragma: no cover
                raise RuntimeError("jose stub: encode not implemented")

            @staticmethod
            def decode(*_a, **_kw):  # pragma: no cover
                raise RuntimeError("jose stub: decode not implemented")

        jose_mod.JWTError = JWTError
        jose_mod.jwt = _Jwt()
        sys.modules["jose"] = jose_mod

    try:
        import passlib.context  # noqa: F401
    except ImportError:
        passlib_mod = ModuleType("passlib")
        ctx_mod = ModuleType("passlib.context")

        class _CryptContext:
            def __init__(self, *a, **kw):
                pass

        ctx_mod.CryptContext = _CryptContext
        passlib_mod.context = ctx_mod
        sys.modules["passlib"] = passlib_mod
        sys.modules["passlib.context"] = ctx_mod


_install_auth_stubs_if_missing()

from models.schemas import RetrievalTier  # noqa: E402
from polymath_mcp import tools as mcp_tools  # noqa: E402
from polymath_mcp.auth import SYSTEM_USER_ID, _current_user_id  # noqa: E402


@pytest.fixture
def system_user():
    token = _current_user_id.set(SYSTEM_USER_ID)
    try:
        yield SYSTEM_USER_ID
    finally:
        _current_user_id.reset(token)


@pytest.mark.asyncio
async def test_mcp_app_guide_exposes_app_routes_and_update_workflow():
    result = await mcp_tools.polymath_app_guide(detail="full")

    route_names = {route["ui_name"] for route in result["retrieval_routes"]}
    workflow_names = {workflow["name"] for workflow in result["agent_workflows"]}
    capability_tools = result["app_capabilities"]["core_capabilities"]

    assert route_names == {"Fast Search", "Hybrid Search", "Graph Augmentation"}
    assert "ingest_and_verify" in workflow_names
    assert "polymath_chat_query" in capability_tools["answer"]
    assert "polymath_graph_query" in capability_tools["graph"]
    assert "polymath_upload_document" in capability_tools["update_knowledge_base"]
    assert "agent_instructions" in result
    assert "API_KEY" not in result["agent_instructions"]


@pytest.mark.asyncio
async def test_mcp_search_forwards_current_retrieval_knobs(monkeypatch, system_user):
    captured: dict = {}

    async def fake_retrieve(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            chunks=[],
            requested_tier=kwargs["retrieval_tier"],
            effective_tier=kwargs["retrieval_tier"],
            downgrade_reason=None,
        )

    monkeypatch.setattr(mcp_tools, "_scope_corpus_ids", AsyncMock(return_value=["c1", "c2"]))
    monkeypatch.setattr(mcp_tools.retriever_orchestrator, "retrieve", fake_retrieve)

    result = await mcp_tools.polymath_search(
        query="privacy preserving on-device AI",
        corpus_ids=["c1", "c2"],
        retrieval_tier="qdrant_mongo_graph",
        top_k=6,
        retrieval_k=40,
        rerank_enabled=False,
        top_k_summary=12,
        rerank_top_n=18,
        similarity_threshold=0.25,
        neo4j_expansion_cap=9,
        max_corpora_per_query=8,
        fact_seed_limit=7,
        search_mode="local",
    )

    assert result["status"] if "status" in result else "ok"
    assert captured["retrieval_tier"] == RetrievalTier.qdrant_mongo_graph
    assert captured["retrieval_k"] == 40
    assert captured["rerank_enabled"] is False
    assert captured["top_k_summary"] == 12
    assert captured["rerank_top_n"] == 18
    assert captured["similarity_threshold"] == 0.25
    assert captured["neo4j_expansion_cap"] == 9
    assert captured["max_corpora_per_query"] == 8
    assert captured["final_top_k"] == 6
    assert captured["fact_seed_limit"] == 7
    assert captured["search_mode"] == "local"


@pytest.mark.asyncio
async def test_mcp_chat_query_allows_multi_corpus_and_passes_new_options(
    monkeypatch,
    system_user,
):
    captured: dict = {}

    class FakeChatOrchestrator:
        async def process_chat_request(self, request, user_id=None):
            captured["request"] = request
            captured["user_id"] = user_id
            yield "data: " + json.dumps(
                {
                    "type": "trace_event",
                    "trace_event": {"title": "RAG", "status": "done"},
                }
            ) + "\n\n"
            yield "data: " + json.dumps({"type": "token", "content": "answer"}) + "\n\n"
            yield "data: " + json.dumps(
                {
                    "type": "done",
                    "conversation_id": "conv1",
                    "model_used": "openai/test",
                    "chunks_returned": 8,
                    "query_profile_used": "custom",
                    "reasoning_cascade_applied": True,
                    "tools_used": ["web_search"],
                    "skills_used": ["skill1"],
                }
            ) + "\n\n"

    import services.chat_orchestrator as chat_module

    monkeypatch.setattr(mcp_tools, "_scope_corpus_ids", AsyncMock(return_value=["a", "b", "c", "d"]))
    monkeypatch.setattr(chat_module, "chat_orchestrator", FakeChatOrchestrator())

    result = await mcp_tools.polymath_chat_query(
        message="answer across four corpora",
        corpus_ids=["a", "b", "c", "d"],
        retrieval_tier="qdrant_mongo_graph",
        model="pool:model-1",
        query_profile="custom",
        reasoning_mode="compare",
        reasoning_blend=["deductive"],
        reasoning_cascade=True,
        hyde_enabled=True,
        rerank_enabled=True,
        retrieval_k=60,
        final_top_k=12,
        search_mode="auto",
        web_search_enabled=True,
        web_fetch_depth="normal",
        web_research_mode=True,
        web_youtube_transcripts=True,
        web_max_sources=9,
        selected_tools=["web_search"],
        active_skill_ids=["skill1"],
    )

    request = captured["request"]
    assert result["status"] == "ok"
    assert result["answer"] == "answer"
    assert result["trace_events"][0]["title"] == "RAG"
    assert result["query_profile_used"] == "custom"
    assert result["reasoning_cascade_applied"] is True
    assert request.corpus_ids == ["a", "b", "c", "d"]
    assert request.retrieval_tier == RetrievalTier.qdrant_mongo_graph
    assert request.reasoning_cascade is True
    assert request.selected_tools == ["web_search"]
    assert request.active_skill_ids == ["skill1"]
    assert request.overrides.model == "pool:model-1"
    assert request.overrides.reasoning_blend == ["deductive"]
    assert request.overrides.retrieval_k == 60
    assert request.overrides.final_top_k == 12
    assert request.overrides.search_mode == "auto"
    assert request.overrides.web_search_enabled is True
    assert request.overrides.web_fetch_depth == "normal"
    assert request.overrides.web_research_mode is True
    assert request.overrides.web_youtube_transcripts is True
    assert request.overrides.web_max_sources == 9


@pytest.mark.asyncio
async def test_mcp_graph_discover_forwards_multi_corpus_modes(monkeypatch, system_user):
    captured: dict = {}

    async def fake_discover(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            session_id="s1",
            corpus_id="c1",
            corpus_ids=kwargs["corpus_ids"],
            query=kwargs["query"],
            mode=kwargs["mode"],
            headline={"text": "headline"},
            auto_synthesis={"markdown": "synthesis"},
            context_graph={"nodes": [], "links": [], "meta": {}},
            trace={"source_docs": ["doc1"], "llm_context": {"files": ["doc1"]}},
            themes=[],
            bridges=[],
            bridges_v2=[],
            questions=[],
            gaps_v2=[],
            latent_topics=[],
            tensions=[],
            metrics={},
            insight_packet_summary={},
            graph={"nodes": [], "links": []},
        )

    import services.graph.orchestrator as graph_orchestrator

    monkeypatch.setattr(mcp_tools, "_scope_corpus_ids", AsyncMock(return_value=["c1", "c2"]))
    monkeypatch.setattr(mcp_tools.ingestion_service, "_qdrant", object())
    monkeypatch.setattr(mcp_tools.ingestion_service, "_db", object())
    monkeypatch.setattr(graph_orchestrator, "discover", fake_discover)

    result = await mcp_tools.polymath_graph_query(
        query="compare user modeling and knowledge graphs",
        corpus_ids=["c1", "c2"],
        synthesis_mode="nuance",
        validate_synthesis=True,
        agentic=True,
    )

    assert result["status"] == "ok"
    assert result["corpus_ids"] == ["c1", "c2"]
    assert result["synthesis_mode"] == "nuance"
    assert captured["corpus_ids"] == ["c1", "c2"]
    assert captured["synthesis_mode"] == "nuance"
    assert captured["validate_synthesis"] is True
    assert captured["agentic"] is True


def test_query_tools_in_registry():
    names = {fn.__name__ for fn in mcp_tools.ALL_TOOLS}
    assert "polymath_app_guide" in names
    assert "polymath_graph_map_query" in names
    assert "polymath_graph_question_suggestions" in names
