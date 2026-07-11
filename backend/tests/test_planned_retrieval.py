import asyncio
from time import perf_counter
from types import SimpleNamespace

import pytest

import services.retriever as retriever_module
from models.schemas import RetrievalTier, SourceChunk
from services.retriever.query_plan import build_query_plan_v2


def _chunk(chunk_id: str, corpus_id: str = "c1") -> SourceChunk:
    return SourceChunk(
        chunk_id=chunk_id,
        parent_id=f"p-{chunk_id}",
        doc_id=f"d-{chunk_id}",
        corpus_id=corpus_id,
        text=f"evidence {chunk_id}",
        score=0.8,
        source_tier="vector",
    )


def test_planned_rerank_limit_is_adaptive_to_query_complexity():
    simple = build_query_plan_v2("What is Purple Ocean strategy?")
    assessment = build_query_plan_v2(
        "create a test to test my understanding ecommerce AI"
    )
    comparative = build_query_plan_v2(
        "Compare Purple Ocean strategy with sticky messaging."
    )

    assert (
        retriever_module._planned_rerank_candidate_limit(
            plan=simple,
            intent=SimpleNamespace(need=retriever_module.QueryNeed.SPECIFIC),
            tier=RetrievalTier.qdrant_mongo,
            configured_limit=16,
            final_top_k=5,
        )
        == 8
    )
    assert (
        retriever_module._planned_rerank_candidate_limit(
            plan=assessment,
            intent=SimpleNamespace(need=retriever_module.QueryNeed.BROAD),
            tier=RetrievalTier.qdrant_mongo,
            configured_limit=16,
            final_top_k=5,
        )
        == 12
    )
    assert (
        retriever_module._planned_rerank_candidate_limit(
            plan=comparative,
            intent=SimpleNamespace(need=retriever_module.QueryNeed.BALANCED),
            tier=RetrievalTier.qdrant_mongo,
            configured_limit=16,
            final_top_k=5,
        )
        == 16
    )


@pytest.mark.asyncio
async def test_planned_hybrid_batches_embeddings_and_reranks_once(monkeypatch):
    orchestrator = retriever_module.RetrieverOrchestrator()
    plan = build_query_plan_v2(
        "Compare Purple Ocean strategy with sticky messaging.",
        corpus_ids=["c1", "c2"],
    )
    embedded: list[list[str]] = []
    reranks: list[tuple[str, int]] = []
    sequence = 0

    async def fake_filter(corpus_ids):
        return corpus_ids, []

    async def fake_intersection(tier, corpus_ids):
        return tier, None

    async def fake_config(corpus_ids):
        return None

    async def fake_embed(texts, config):
        embedded.append(list(texts))
        return [[float(index)] for index, _ in enumerate(texts)]

    async def fake_search(*args, **kwargs):
        nonlocal sequence
        sequence += 1
        corpus_id = "c2" if sequence % 2 == 0 else "c1"
        return [_chunk(f"chunk-{sequence}", corpus_id)]

    async def identity(chunks, corpus_ids):
        return chunks

    async def rerank(query, chunks):
        reranks.append((query, len(chunks)))
        return chunks

    monkeypatch.setattr(orchestrator, "_filter_existing_corpora", fake_filter)
    monkeypatch.setattr(
        orchestrator, "_enforce_strategy_intersection", fake_intersection
    )
    monkeypatch.setattr(orchestrator, "_embedding_config_for_query", fake_config)
    monkeypatch.setattr(retriever_module, "embed_queries", fake_embed)
    monkeypatch.setattr(retriever_module.funnel_a, "search", fake_search)
    monkeypatch.setattr(retriever_module.funnel_b, "search", fake_search)
    monkeypatch.setattr(retriever_module.lexical_retriever, "search", fake_search)
    monkeypatch.setattr(retriever_module, "attach_document_identities", identity)
    monkeypatch.setattr(retriever_module, "hydrate_rerank_texts", identity)
    monkeypatch.setattr(
        retriever_module,
        "hydrate_chunks",
        lambda chunks, corpus_ids, query=None: identity(chunks, corpus_ids),
    )
    monkeypatch.setattr(retriever_module.reranker_service, "rerank", rerank)
    monkeypatch.setattr(
        retriever_module,
        "select_with_diversity",
        lambda ranked, **kwargs: SimpleNamespace(
            candidates=ranked[: kwargs["final_top_k"]],
            diagnostics={"required_coverage": 1.0},
        ),
    )

    result = await orchestrator.retrieve_planned(
        plan=plan,
        corpus_ids=["c1", "c2"],
        retrieval_tier=RetrievalTier.qdrant_mongo,
        rerank_enabled=True,
        rerank_top_n=32,
        final_top_k=6,
    )

    assert len(embedded) == 1
    assert embedded[0] == [
        lane.dense_text for lane in plan.lanes if lane.role in {"original", "core"}
    ]
    assert reranks == [(plan.original_query, min(16, sequence))]
    assert result.diagnostics["query_plan_version"] == "query_plan.v2"
    assert result.diagnostics["lane_failures"] == []
    # Synthetic candidates carry no phrase/title grounding. Lane provenance
    # alone must not be reported as semantic concept coverage.
    assert result.diagnostics["required_concept_coverage"]["coverage"] == 0.0
    assert result.diagnostics["repair"]["attempted_rounds"] == 1
    assert result.diagnostics["repair"]["missing_lane_ids_before"]
    assert set(result.diagnostics["fusion"]["repair"]["retriever_counts"]) == {
        "dense",
        "summary",
        "lexical",
    }
    assert result.diagnostics["cache"]["key_version"] == "retrieval_v2"
    assert result.effective_tier == RetrievalTier.qdrant_mongo


@pytest.mark.asyncio
async def test_planned_fast_uses_top_down_qdrant_only_contract(monkeypatch):
    orchestrator = retriever_module.RetrieverOrchestrator()
    plan = build_query_plan_v2("What is Purple Ocean strategy?")
    calls: list[str] = []

    async def forbidden_uncached(**kwargs):
        raise AssertionError("Fast QueryPlanV2 must use the top-down planned route")

    async def fake_filter(corpus_ids):
        return corpus_ids, []

    async def fake_intersection(tier, corpus_ids):
        return tier, None

    async def fake_config(corpus_ids):
        return None

    async def fake_embed(texts, config):
        return [[0.1] for _ in texts]

    async def fake_child_search(*args, **kwargs):
        calls.append("child")
        chunk = _chunk("fast-evidence")
        chunk.text = "Purple Ocean strategy differentiates a brand from competitors."
        return [chunk]

    async def fake_summary_search(*args, **kwargs):
        calls.append("summary")
        chunk = _chunk("fast-parent-summary")
        chunk.parent_id = "p-fast-evidence"
        chunk.text = "Purple Ocean strategy parent context."
        return [chunk]

    async def forbidden_lexical(*args, **kwargs):
        raise AssertionError("Fast must not use the lexical evidence lane")

    async def forbidden_rerank(*args, **kwargs):
        raise AssertionError("Fast must not call the cross-encoder")

    monkeypatch.setattr(orchestrator, "_retrieve_uncached", forbidden_uncached)
    monkeypatch.setattr(orchestrator, "_filter_existing_corpora", fake_filter)
    monkeypatch.setattr(
        orchestrator, "_enforce_strategy_intersection", fake_intersection
    )
    monkeypatch.setattr(orchestrator, "_embedding_config_for_query", fake_config)
    monkeypatch.setattr(retriever_module, "embed_queries", fake_embed)
    monkeypatch.setattr(retriever_module.funnel_b, "search", fake_child_search)
    monkeypatch.setattr(retriever_module.funnel_a, "search", fake_summary_search)
    monkeypatch.setattr(retriever_module.lexical_retriever, "search", forbidden_lexical)
    monkeypatch.setattr(retriever_module.reranker_service, "rerank", forbidden_rerank)

    result = await orchestrator.retrieve_planned(
        plan=plan,
        corpus_ids=["c1"],
        retrieval_tier=RetrievalTier.qdrant_only,
        rerank_enabled=True,
    )

    assert calls
    assert result.effective_tier == RetrievalTier.qdrant_only
    assert "summary" in calls
    assert result.diagnostics["limits"]["summary_top_k"] == 4
    assert result.diagnostics["counts"]["lexical"] == 0
    assert result.diagnostics["reranker"]["status"] == "skipped_by_request"


@pytest.mark.asyncio
async def test_planned_fast_abstains_from_explicit_graph_evidence(monkeypatch):
    orchestrator = retriever_module.RetrieverOrchestrator()
    plan = build_query_plan_v2(
        "What relationship does the graph establish between positioning and messaging?"
    )

    async def must_not_retrieve(**kwargs):
        raise AssertionError("Fast must not silently run a non-graph evidence route")

    monkeypatch.setattr(orchestrator, "_retrieve_uncached", must_not_retrieve)

    result = await orchestrator.retrieve_planned(
        plan=plan,
        corpus_ids=["c1"],
        retrieval_tier=RetrievalTier.qdrant_only,
    )

    assert result.chunks == []
    assert result.diagnostics["status"] == "route_capability_mismatch"
    assert (
        result.diagnostics["reason"] == "explicit_graph_evidence_requires_graph_route"
    )


@pytest.mark.asyncio
async def test_planned_rerank_timeout_degrades_to_fused_results(monkeypatch):
    orchestrator = retriever_module.RetrieverOrchestrator()
    plan = build_query_plan_v2("Compare Purple Ocean strategy with sticky messaging.")

    async def fake_filter(corpus_ids):
        return corpus_ids, []

    async def fake_intersection(tier, corpus_ids):
        return tier, None

    async def fake_config(corpus_ids):
        return None

    async def fake_search(*args, **kwargs):
        return [_chunk("evidence")]

    async def identity(chunks, corpus_ids, **kwargs):
        return chunks

    async def timeout_rerank(query, chunks):
        raise TimeoutError("bounded reranker deadline")

    monkeypatch.setattr(orchestrator, "_filter_existing_corpora", fake_filter)
    monkeypatch.setattr(
        orchestrator, "_enforce_strategy_intersection", fake_intersection
    )
    monkeypatch.setattr(orchestrator, "_embedding_config_for_query", fake_config)

    async def fake_embed(texts, config):
        return [[0.1] for _ in texts]

    monkeypatch.setattr(retriever_module, "embed_queries", fake_embed)
    monkeypatch.setattr(retriever_module.funnel_a, "search", fake_search)
    monkeypatch.setattr(retriever_module.funnel_b, "search", fake_search)
    monkeypatch.setattr(retriever_module.lexical_retriever, "search", fake_search)
    monkeypatch.setattr(retriever_module, "attach_document_identities", identity)
    monkeypatch.setattr(retriever_module, "hydrate_rerank_texts", identity)
    monkeypatch.setattr(retriever_module, "hydrate_chunks", identity)
    monkeypatch.setattr(retriever_module.reranker_service, "rerank", timeout_rerank)

    result = await orchestrator.retrieve_planned(
        plan=plan,
        corpus_ids=["c1"],
        retrieval_tier=RetrievalTier.qdrant_mongo,
        rerank_enabled=True,
        final_top_k=4,
    )

    assert result.chunks
    assert result.diagnostics["status"] == "query_plan_v2_degraded"
    assert result.diagnostics["reranker"]["status"] == "deadline_fallback_rank_fusion"
    assert any(
        failure["retriever"] == "reranker"
        for failure in result.diagnostics["lane_failures"]
    )


@pytest.mark.asyncio
async def test_planned_route_deadline_degrades_to_lexical_evidence(monkeypatch):
    orchestrator = retriever_module.RetrieverOrchestrator()
    plan = build_query_plan_v2("Compare Purple Ocean strategy with sticky messaging.")

    async def fake_filter(corpus_ids):
        return corpus_ids, []

    async def fake_intersection(tier, corpus_ids):
        return tier, None

    async def fake_config(corpus_ids):
        return None

    async def slow_embed(texts, config):
        await asyncio.sleep(1)
        return [[0.1] for _ in texts]

    async def lexical_search(*args, **kwargs):
        return [_chunk("lexical-evidence")]

    async def identity(chunks, corpus_ids, **kwargs):
        return chunks

    async def slow_rerank(query, chunks):
        await asyncio.sleep(1)
        return chunks

    monkeypatch.setattr(orchestrator, "_filter_existing_corpora", fake_filter)
    monkeypatch.setattr(
        orchestrator, "_enforce_strategy_intersection", fake_intersection
    )
    monkeypatch.setattr(orchestrator, "_embedding_config_for_query", fake_config)
    monkeypatch.setattr(retriever_module, "embed_queries", slow_embed)
    monkeypatch.setattr(retriever_module.lexical_retriever, "search", lexical_search)
    monkeypatch.setattr(retriever_module, "attach_document_identities", identity)
    monkeypatch.setattr(retriever_module, "hydrate_rerank_texts", identity)
    monkeypatch.setattr(retriever_module, "hydrate_chunks", identity)
    monkeypatch.setattr(retriever_module.reranker_service, "rerank", slow_rerank)
    monkeypatch.setattr(
        retriever_module.settings,
        "QUERY_PLAN_HYBRID_TOTAL_DEADLINE_SECONDS",
        0.2,
    )

    started = perf_counter()
    result = await orchestrator.retrieve_planned(
        plan=plan,
        corpus_ids=["c1"],
        retrieval_tier=RetrievalTier.qdrant_mongo,
        rerank_enabled=True,
        final_top_k=4,
    )
    elapsed = perf_counter() - started

    assert elapsed < 0.4
    assert result.chunks
    assert result.diagnostics["status"] == "query_plan_v2_degraded"
    assert result.diagnostics["total_deadline_s"] == 0.2
    assert result.diagnostics["total_s"] < 0.4
    assert any(
        failure["retriever"] == "embedding"
        for failure in result.diagnostics["lane_failures"]
    )
    assert any(
        failure["retriever"] == "reranker"
        for failure in result.diagnostics["lane_failures"]
    )
