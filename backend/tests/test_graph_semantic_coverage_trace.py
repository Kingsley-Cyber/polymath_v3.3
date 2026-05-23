import os

import pytest

os.environ.setdefault("LITELLM_MASTER_KEY", "test-litellm-master-key")
os.environ.setdefault("AUTH_SECRET_KEY", "test-auth-secret-key")
os.environ.setdefault("DEFAULT_ADMIN_PASSWORD", "test-admin-password")

import services.graph.orchestrator as graph_module
import services.retriever as retriever_module
from models.schemas import RetrievalResult


@pytest.mark.asyncio
async def test_graph_semantic_support_records_failed_lanes(monkeypatch):
    facets = [
        {
            "name": "knowledge_graph",
            "label": "knowledge graph",
            "matched": ["knowledge graph"],
            "query_matched": True,
            "support_terms": ["knowledge graph", "ontology"],
        },
        {
            "name": "user_modeling",
            "label": "user modeling",
            "matched": ["user modeling"],
            "query_matched": True,
            "support_terms": ["user modeling", "user profile"],
        },
    ]

    async def fake_facets(*args, **kwargs):
        return facets

    seen_modes: list[str] = []

    async def fake_retrieve(**kwargs):
        seen_modes.append(kwargs["search_mode"])
        return RetrievalResult(
            chunks=[],
            requested_tier=kwargs["retrieval_tier"],
            effective_tier=kwargs["retrieval_tier"],
        )

    monkeypatch.setattr(graph_module, "_semantic_facets_for_query_with_corpus", fake_facets)
    monkeypatch.setattr(retriever_module.retriever_orchestrator, "retrieve", fake_retrieve)

    packet = {
        "evidence": [
            {
                "chunk_id": "base-1",
                "doc_id": "identity.md",
                "doc_name": "identity.md",
                "chunk_text": "Narrative identity and values shape personal meaning.",
            }
        ],
        "edges": [],
    }

    support = await graph_module._semantic_facet_support_evidence(
        None,
        corpus_id="c1",
        query="How can knowledge graphs and user modeling support identity reflection?",
        packet=packet,
        synthesis_mode="research",
        max_support=8,
    )

    assert support == []
    assert set(seen_modes) == {"local"}
    meta = packet["evidence_filter"]["semantic_coverage_support"]
    assert meta["added_candidates"] == 0
    reports = {report["lane"]: report for report in meta["lane_reports"]}
    assert {"knowledge_graph", "user_modeling"} <= set(reports)
    assert reports["knowledge_graph"]["status"] == "uncovered"
    assert reports["knowledge_graph"]["attempts"][0]["status"] == "no_candidates"
