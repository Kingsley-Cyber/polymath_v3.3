import pytest

import services.retriever as retriever_module
from models.schemas import RetrievalTier, SourceChunk, SourceFact
from services.retriever import RetrieverOrchestrator


def _chunk(
    chunk_id: str,
    *,
    score: float,
    source_tier: str = "qdrant_child",
    doc_id: str | None = None,
    text: str | None = None,
) -> SourceChunk:
    return SourceChunk(
        chunk_id=chunk_id,
        parent_id=f"parent-{chunk_id}",
        doc_id=doc_id or f"doc-{chunk_id}",
        corpus_id="c1",
        text=text or f"Evidence for {chunk_id}",
        score=score,
        source_tier=source_tier,
        provenance=[{"retriever": source_tier}],
    )


async def _fake_embed_query(_query, _config):
    return [0.1, 0.2, 0.3]


async def _score_sort_rerank(_query, chunks):
    return sorted(chunks, key=lambda chunk: chunk.score, reverse=True)


async def _identity_hydrate(chunks, _corpus_ids):
    return list(chunks)


def _install_common_mocks(monkeypatch):
    monkeypatch.setattr(retriever_module, "embed_query", _fake_embed_query)
    monkeypatch.setattr(retriever_module.reranker_service, "rerank", _score_sort_rerank)
    monkeypatch.setattr(retriever_module, "hydrate_chunks", _identity_hydrate)


@pytest.mark.asyncio
async def test_chat_query_e2e_named_sources_survive_hybrid_pipeline(monkeypatch):
    _install_common_mocks(monkeypatch)
    monkeypatch.setattr(retriever_module.settings, "NEO4J_ENABLED", False)

    async def fake_a(*_args, **_kwargs):
        return [_chunk("summary-architecture", score=0.70, source_tier="summary")]

    async def fake_b(*_args, **_kwargs):
        return [_chunk("child-layering", score=0.74)]

    async def fake_lexical(*_args, **_kwargs):
        return [_chunk("lexical-gateway", score=0.76, source_tier="mongo+lexical")]

    async def fake_document_anchor(*_args, **_kwargs):
        return [
            _chunk(
                "fowler-layering",
                score=0.79,
                source_tier="document_anchor+lexical",
                doc_id="fowler",
                text="Layering separates domain logic from gateways and mappers.",
            ),
            _chunk(
                "gifts-preference",
                score=0.78,
                source_tier="document_anchor+lexical",
                doc_id="gifts",
                text="Personality type expresses preferences in perception and judgment.",
            ),
        ]

    monkeypatch.setattr(retriever_module.funnel_a, "search", fake_a)
    monkeypatch.setattr(retriever_module.funnel_b, "search", fake_b)
    monkeypatch.setattr(retriever_module.lexical_retriever, "search", fake_lexical)
    monkeypatch.setattr(
        retriever_module.document_anchor_retriever,
        "search",
        fake_document_anchor,
    )

    result = await RetrieverOrchestrator().retrieve(
        query=(
            "Based on Fowler's Patterns of Enterprise Application Architecture "
            "and Myers/Briggs' Gifts Differing, compare layering and preference."
        ),
        corpus_ids=["c1"],
        retrieval_tier=RetrievalTier.qdrant_mongo,
        collections=None,
        retrieval_k=40,
        rerank_enabled=True,
        final_top_k=5,
    )

    chunk_ids = {chunk.chunk_id for chunk in result.chunks}
    assert result.effective_tier == RetrievalTier.qdrant_mongo
    assert {"fowler-layering", "gifts-preference"} <= chunk_ids
    assert "child-layering" in chunk_ids


@pytest.mark.asyncio
async def test_chat_query_e2e_hybrid_does_not_enter_graph_lanes(monkeypatch):
    _install_common_mocks(monkeypatch)
    monkeypatch.setattr(retriever_module.settings, "NEO4J_ENABLED", True)
    events: list[str] = []

    async def forbidden_fact_seed(self, query, corpus_ids):
        del self, query, corpus_ids
        events.append("fact_seed")
        return []

    async def fake_a(*_args, **_kwargs):
        return [_chunk("summary", score=0.70, source_tier="summary")]

    async def fake_b(*_args, **_kwargs):
        return [_chunk("child", score=0.80)]

    async def fake_lexical(*_args, **_kwargs):
        return [_chunk("lexical", score=0.78, source_tier="mongo+lexical")]

    async def fake_document_anchor(*_args, **_kwargs):
        return []

    async def forbidden_expand(*_args, **_kwargs):
        events.append("graph_expand")
        return []

    monkeypatch.setattr(
        RetrieverOrchestrator,
        "_retrieve_graph_seed_facts",
        forbidden_fact_seed,
    )
    monkeypatch.setattr(retriever_module.funnel_a, "search", fake_a)
    monkeypatch.setattr(retriever_module.funnel_b, "search", fake_b)
    monkeypatch.setattr(retriever_module.lexical_retriever, "search", fake_lexical)
    monkeypatch.setattr(
        retriever_module.document_anchor_retriever,
        "search",
        fake_document_anchor,
    )
    monkeypatch.setattr(retriever_module.mode_a_expansion, "expand", forbidden_expand)

    result = await RetrieverOrchestrator().retrieve(
        query="How should layering inform navigation depth?",
        corpus_ids=["c1"],
        retrieval_tier=RetrievalTier.qdrant_mongo,
        collections=None,
        retrieval_k=40,
        rerank_enabled=True,
        final_top_k=3,
    )

    assert result.effective_tier == RetrievalTier.qdrant_mongo
    assert "fact_seed" not in events
    assert "graph_expand" not in events
    assert {chunk.chunk_id for chunk in result.chunks} >= {"child", "lexical"}


@pytest.mark.asyncio
async def test_chat_query_e2e_vector_base_skips_hydrated_recall_lanes(monkeypatch):
    _install_common_mocks(monkeypatch)
    monkeypatch.setattr(retriever_module.settings, "NEO4J_ENABLED", False)

    async def fake_a(*_args, **_kwargs):
        return [_chunk("summary-vector", score=0.70, source_tier="summary")]

    async def fake_b(*_args, **_kwargs):
        return [_chunk("child-vector", score=0.80)]

    async def forbidden_lexical(*_args, **_kwargs):
        raise AssertionError("Fast Search must not execute lexical recall")

    async def forbidden_document_anchor(*_args, **_kwargs):
        raise AssertionError("Fast Search must not execute document-anchor recall")

    async def forbidden_hydrate(*_args, **_kwargs):
        raise AssertionError("Fast Search must not hydrate Mongo parents")

    monkeypatch.setattr(retriever_module.funnel_a, "search", fake_a)
    monkeypatch.setattr(retriever_module.funnel_b, "search", fake_b)
    monkeypatch.setattr(retriever_module.lexical_retriever, "search", forbidden_lexical)
    monkeypatch.setattr(
        retriever_module.document_anchor_retriever,
        "search",
        forbidden_document_anchor,
    )
    monkeypatch.setattr(retriever_module, "hydrate_chunks", forbidden_hydrate)

    result = await RetrieverOrchestrator().retrieve(
        query="Summarize the broad themes in the architecture notes.",
        corpus_ids=["c1"],
        retrieval_tier=RetrievalTier.qdrant_only,
        collections=None,
        retrieval_k=40,
        rerank_enabled=False,
        final_top_k=2,
    )

    assert result.effective_tier == RetrievalTier.qdrant_only
    assert {chunk.chunk_id for chunk in result.chunks} == {
        "child-vector",
        "summary-vector",
    }


@pytest.mark.asyncio
async def test_chat_query_e2e_document_anchor_seeds_graph_expansion(monkeypatch):
    _install_common_mocks(monkeypatch)
    monkeypatch.setattr(retriever_module.settings, "NEO4J_ENABLED", True)
    monkeypatch.setattr(
        retriever_module.settings,
        "RETRIEVAL_GRAPH_RERANK_ENABLED",
        False,
        raising=False,
    )

    async def no_fact_seeds(self, query, corpus_ids, **_kwargs):
        del self, query, corpus_ids
        return []

    async def fake_a(*_args, **_kwargs):
        return [_chunk("summary-architecture", score=0.70, source_tier="summary")]

    async def fake_b(*_args, **_kwargs):
        return [_chunk("child-layering", score=0.74)]

    async def fake_lexical(*_args, **_kwargs):
        return []

    async def fake_document_anchor(*_args, **_kwargs):
        return [
            _chunk(
                "fowler-layering",
                score=0.79,
                source_tier="document_anchor+lexical",
                doc_id="fowler",
            )
        ]

    seen_graph_seed_ids: list[str] = []

    async def fake_expand(chunks, *_args, **_kwargs):
        seen_graph_seed_ids.extend(chunk.chunk_id for chunk in chunks)
        return [_chunk("graph-neighbor", score=0.77, source_tier="graph_expansion")]

    monkeypatch.setattr(
        RetrieverOrchestrator,
        "_retrieve_graph_seed_facts",
        no_fact_seeds,
    )
    monkeypatch.setattr(retriever_module.funnel_a, "search", fake_a)
    monkeypatch.setattr(retriever_module.funnel_b, "search", fake_b)
    monkeypatch.setattr(retriever_module.lexical_retriever, "search", fake_lexical)
    monkeypatch.setattr(
        retriever_module.document_anchor_retriever,
        "search",
        fake_document_anchor,
    )
    monkeypatch.setattr(retriever_module.mode_a_expansion, "expand", fake_expand)

    result = await RetrieverOrchestrator().retrieve(
        query=(
            "Based on Fowler's Patterns of Enterprise Application Architecture, "
            "what does layering imply for UI navigation?"
        ),
        corpus_ids=["c1"],
        retrieval_tier=RetrievalTier.qdrant_mongo_graph,
        collections=None,
        retrieval_k=40,
        rerank_enabled=True,
        final_top_k=5,
    )

    assert "fowler-layering" in seen_graph_seed_ids
    assert {chunk.chunk_id for chunk in result.chunks} >= {
        "fowler-layering",
        "graph-neighbor",
    }


@pytest.mark.asyncio
async def test_chat_query_e2e_graph_pipeline_seeds_facts_before_expand(monkeypatch):
    events: list[tuple[str, tuple[str, ...]]] = []
    monkeypatch.setattr(retriever_module.settings, "NEO4J_ENABLED", True)
    monkeypatch.setattr(
        retriever_module.settings,
        "RETRIEVAL_GRAPH_RERANK_ENABLED",
        False,
        raising=False,
    )

    async def fake_embed_query(_query, _config):
        events.append(("embed", ()))
        return [0.1, 0.2, 0.3]

    async def fake_fact_seeds(self, query, corpus_ids, **_kwargs):
        del self, query, corpus_ids
        events.append(("facts", ()))
        return [
            SourceFact(
                fact_id="fact-1",
                subject="Layering",
                fact_type="concept",
                property_name="separates",
                value="domain logic and gateways",
                confidence=0.9,
                evidence_phrase="Layering separates responsibilities.",
                chunk_id="fact-seed",
                doc_id="fowler",
                corpus_id="c1",
            )
        ]

    async def fake_a(*_args, **_kwargs):
        return [_chunk("summary", score=0.70, source_tier="summary")]

    async def fake_b(*_args, **_kwargs):
        return [_chunk("child", score=0.74)]

    async def fake_lexical(*_args, **_kwargs):
        return []

    async def fake_document_anchor(*_args, **_kwargs):
        return [
            _chunk(
                "fowler-layering",
                score=0.79,
                source_tier="document_anchor+lexical",
                doc_id="fowler",
            )
        ]

    async def fake_expand(chunks, *_args, **_kwargs):
        events.append(("expand", tuple(chunk.chunk_id for chunk in chunks)))
        return [_chunk("graph-neighbor", score=0.77, source_tier="graph_expansion")]

    async def fake_rerank(_query, chunks):
        events.append(("rerank", tuple(chunk.chunk_id for chunk in chunks)))
        return sorted(chunks, key=lambda chunk: chunk.score, reverse=True)

    async def fake_hydrate(chunks, _corpus_ids, **_kwargs):
        events.append(("hydrate", tuple(chunk.chunk_id for chunk in chunks)))
        return list(chunks)

    monkeypatch.setattr(retriever_module, "embed_query", fake_embed_query)
    monkeypatch.setattr(
        RetrieverOrchestrator,
        "_retrieve_graph_seed_facts",
        fake_fact_seeds,
    )
    monkeypatch.setattr(retriever_module.funnel_a, "search", fake_a)
    monkeypatch.setattr(retriever_module.funnel_b, "search", fake_b)
    monkeypatch.setattr(retriever_module.lexical_retriever, "search", fake_lexical)
    monkeypatch.setattr(
        retriever_module.document_anchor_retriever,
        "search",
        fake_document_anchor,
    )
    monkeypatch.setattr(retriever_module.mode_a_expansion, "expand", fake_expand)
    monkeypatch.setattr(retriever_module.reranker_service, "rerank", fake_rerank)
    monkeypatch.setattr(retriever_module, "hydrate_chunks", fake_hydrate)

    await RetrieverOrchestrator().retrieve(
        query=(
            "Based on Fowler's Patterns of Enterprise Application Architecture, "
            "what does layering imply for UI navigation?"
        ),
        corpus_ids=["c1"],
        retrieval_tier=RetrievalTier.qdrant_mongo_graph,
        collections=None,
        retrieval_k=40,
        rerank_enabled=True,
        final_top_k=5,
    )

    event_names = [name for name, _chunk_ids in events]
    # Fact seeding is now kicked off concurrently and overlaps embed + funnels
    # (facts feed neither embed nor the funnel queries), so it is no longer
    # required to finish "first". The invariant that matters is that the seed
    # facts are resolved before they are consumed by graph expansion — verified
    # both here and by the fact-seed chunk appearing in the expand input below.
    assert event_names.index("facts") < event_names.index("expand")
    assert event_names.index("expand") < event_names.index("rerank")
    assert event_names.index("rerank") < event_names.index("hydrate")

    expand_ids = next(ids for name, ids in events if name == "expand")
    rerank_ids = next(ids for name, ids in events if name == "rerank")
    assert {"fact-seed", "fowler-layering"} <= set(expand_ids)
    assert "graph-neighbor" in rerank_ids
