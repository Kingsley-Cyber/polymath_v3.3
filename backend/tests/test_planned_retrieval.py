import asyncio
import hashlib
from time import perf_counter
from types import SimpleNamespace

import pytest

import services.retriever as retriever_module
from models.librarian_query_plan import (
    LibrarianShortlistItemV1,
    LibrarianSubqueryV1,
)
from models.schemas import RetrievalTier, SourceChunk
from services.retriever.librarian_planner import (
    apply_librarian_execution_plan,
    build_query_plan_v1,
)
from services.retriever.planned_fusion import seated_document_refs_by_lane
from services.retriever.query_plan import (
    build_query_plan_v2,
    query_plan_execution_lanes,
    query_plan_vocabulary_lanes,
)


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


def test_refinement_seating_counts_only_explicit_lane_reservations():
    attributed_to_both = _chunk("winner")
    attributed_to_both.doc_id = "story"
    attributed_to_both.metadata = {
        "planned_lanes": ["lane_a", "lane_b"],
        "planned_required_lane_reservations": ["lane_a"],
    }

    seats = seated_document_refs_by_lane(
        [attributed_to_both],
        ["lane_a", "lane_b"],
    )

    assert seats == {
        "lane_a": {("c1", "story")},
        "lane_b": set(),
    }


def test_planned_rerank_limit_is_adaptive_to_query_complexity():
    simple = build_query_plan_v2("What is Purple Ocean strategy?")
    assessment = build_query_plan_v2(
        "create a test to test my understanding ecommerce AI"
    )
    comparative = build_query_plan_v2(
        "Compare Purple Ocean strategy with sticky messaging."
    )
    enumeration = build_query_plan_v2("what books help with dropshipping and why?")

    assert (
        retriever_module._planned_rerank_candidate_limit(
            plan=simple,
            intent=SimpleNamespace(need=retriever_module.QueryNeed.SPECIFIC),
            tier=RetrievalTier.qdrant_mongo,
            configured_limit=64,
            final_top_k=8,
        )
        == 21
    )
    assert (
        retriever_module._planned_rerank_candidate_limit(
            plan=assessment,
            intent=SimpleNamespace(need=retriever_module.QueryNeed.BROAD),
            tier=RetrievalTier.qdrant_mongo,
            configured_limit=64,
            final_top_k=8,
        )
        == 29
    )
    assert (
        retriever_module._planned_rerank_candidate_limit(
            plan=comparative,
            intent=SimpleNamespace(need=retriever_module.QueryNeed.BALANCED),
            tier=RetrievalTier.qdrant_mongo,
            configured_limit=64,
            final_top_k=8,
        )
        == 35
    )
    assert (
        retriever_module._planned_rerank_candidate_limit(
            plan=enumeration,
            intent=SimpleNamespace(need=retriever_module.QueryNeed.BALANCED),
            tier=RetrievalTier.qdrant_mongo_graph,
            configured_limit=96,
            final_top_k=8,
        )
        == 42
    )


def test_tree_routing_scopes_full_descent_to_required_obligations():
    lanes = [
        SimpleNamespace(lane_id="original", role="original", required=True),
        SimpleNamespace(lane_id="required_a", role="core", required=True),
        SimpleNamespace(lane_id="required_b", role="core", required=True),
        SimpleNamespace(lane_id="planner_optional", role="core", required=False),
        SimpleNamespace(lane_id="translation_optional", role="core", required=False),
    ]

    assert retriever_module._tree_routing_lane_ids(lanes) == [
        "required_a",
        "required_b",
    ]


def test_final_context_budget_expands_for_multiple_answer_obligations():
    simple = build_query_plan_v2("What is Purple Ocean strategy?")
    compound = build_query_plan_v2(
        "How can this product find a target audience and how would that "
        "audience respond to the ad. How should I prompt the opening video?"
    )

    assert (
        retriever_module._planned_final_result_limit(
            plan=simple,
            intent=SimpleNamespace(need=retriever_module.QueryNeed.SPECIFIC),
            tier=RetrievalTier.qdrant_mongo,
            requested_limit=5,
            routed_document_count=1,
        )
        == 8
    )
    assert (
        retriever_module._planned_final_result_limit(
            plan=compound,
            intent=SimpleNamespace(need=retriever_module.QueryNeed.BALANCED),
            tier=RetrievalTier.qdrant_mongo,
            requested_limit=8,
            routed_document_count=6,
        )
        == 14
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("librarian_enabled", [False, True])
async def test_planned_hybrid_batches_embeddings_and_reranks_once(
    monkeypatch,
    librarian_enabled,
):
    orchestrator = retriever_module.RetrieverOrchestrator()
    plan = build_query_plan_v2(
        "Compare Purple Ocean strategy with sticky messaging.",
        corpus_ids=["c1", "c2"],
    )
    librarian_plan = (
        build_query_plan_v1(
            plan.original_query,
            corpus_id="c1,c2",
            corpus_doc_version="sha256:" + hashlib.sha256(b"l4-state").hexdigest(),
            shortlist=(),
        )
        if librarian_enabled
        else None
    )
    execution_plan, _policy = (
        apply_librarian_execution_plan(plan, librarian_plan)
        if librarian_plan is not None
        else (plan, None)
    )
    librarian_core_queries = {
        lane.query
        for lane in query_plan_execution_lanes(execution_plan)
        if lane.role == "core"
    }
    if librarian_enabled:
        assert len(librarian_core_queries) >= 2
    embedded: list[list[str]] = []
    reranks: list[tuple[str, int]] = []
    sequence = 0
    active_core_queries: dict[str, int] = {}
    cross_subquery_overlap = False
    cross_subquery_barrier = asyncio.Event()

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
        nonlocal cross_subquery_overlap, sequence
        query_text = str(
            kwargs.get("query_text")
            or (args[0] if args and isinstance(args[0], str) else "")
        )
        is_librarian_core = librarian_enabled and query_text in librarian_core_queries
        if is_librarian_core:
            active_core_queries[query_text] = active_core_queries.get(query_text, 0) + 1
            if len(active_core_queries) >= 2:
                cross_subquery_overlap = True
                cross_subquery_barrier.set()
        try:
            if is_librarian_core:
                await asyncio.wait_for(cross_subquery_barrier.wait(), timeout=0.25)
            else:
                await asyncio.sleep(0.001)
            sequence += 1
            corpus_id = "c2" if sequence % 2 == 0 else "c1"
            return [_chunk(f"chunk-{sequence}", corpus_id)]
        finally:
            if is_librarian_core:
                remaining = active_core_queries[query_text] - 1
                if remaining:
                    active_core_queries[query_text] = remaining
                else:
                    del active_core_queries[query_text]

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

    async def forbidden_refinement(**_kwargs):
        raise AssertionError("default-OFF retrieval must never refine")

    monkeypatch.setattr(
        retriever_module.librarian_refiner,
        "refine",
        forbidden_refinement,
    )
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
        librarian_plan=librarian_plan,
    )

    assert len(embedded) == 1
    assert embedded[0] == [
        *[lane.dense_text for lane in query_plan_execution_lanes(execution_plan)],
        *[lane.dense_text for lane in query_plan_vocabulary_lanes(execution_plan)],
    ]
    assert len(reranks) == 1
    if librarian_enabled:
        assert cross_subquery_overlap is True
    assert reranks[0][0] == plan.original_query
    assert 1 <= reranks[0][1] <= 64
    assert result.diagnostics["query_plan_version"] == "query_plan.v2"
    assert result.diagnostics["lane_failures"] == []
    # Synthetic candidates carry no phrase/title grounding. Lane provenance
    # alone must not be reported as semantic concept coverage.
    assert result.diagnostics["required_concept_coverage"]["coverage"] == 0.0
    assert result.diagnostics["selection"]["sufficiency"]["answerable"] is False
    assert result.diagnostics["selection"]["sufficiency"]["source"] == (
        "query_plan_required_lanes"
    )
    assert result.diagnostics["repair"]["attempted_rounds"] == 1
    assert result.diagnostics["repair"]["missing_lane_ids_before"]
    assert set(result.diagnostics["fusion"]["repair"]["retriever_counts"]) == {
        "dense",
        "summary",
        "lexical",
    }
    assert result.diagnostics["cache"]["key_version"] == "retrieval_v2"
    librarian_execution = result.diagnostics["librarian_execution"]
    assert librarian_execution["active"] is librarian_enabled
    if librarian_enabled:
        execution = librarian_execution["execution"]
        assert execution["v1_subquery_embed_batches"] == 1
        assert execution["v1_subquery_embed_scope"] == (
            "initial_query_plan_execution_lanes"
        )
        assert execution["candidate_generation_parallel"] is True
        assert execution["logical_rerank_batches"] == 1
        cap_diagnostics = execution["rerank_caps"]
        assert cap_diagnostics["active"] is True
        assert cap_diagnostics["effective_sum"] <= (
            cap_diagnostics["global_rerank_cap"]
        )
        assert reranks[0][1] <= cap_diagnostics["effective_sum"]
        assert reranks[0][1] == cap_diagnostics["output_candidates"]
    assert result.effective_tier == RetrievalTier.qdrant_mongo


@pytest.mark.asyncio
@pytest.mark.parametrize("refinement_scenario", ["success", "persistent", "fallback"])
async def test_librarian_refinement_runs_only_gapped_lane_and_improves_seating(
    monkeypatch,
    refinement_scenario,
):
    orchestrator = retriever_module.RetrieverOrchestrator()
    query = "Compare Story Craft with Camera Craft."
    base = build_query_plan_v2(query, corpus_ids=["c1"])
    librarian = build_query_plan_v1(
        query,
        corpus_id="c1",
        corpus_doc_version="sha256:" + hashlib.sha256(b"l55-state").hexdigest(),
        shortlist=(
            LibrarianShortlistItemV1(
                corpus_id="c1",
                doc_id="story",
                title="Story Craft",
                summary="Narrative directing and dramatic structure.",
                score=0.92,
            ),
            LibrarianShortlistItemV1(
                corpus_id="c1",
                doc_id="camera",
                title="Camera Craft",
                summary="Camera movement and visual emphasis.",
                score=0.84,
            ),
        ),
    )
    initial_execution, initial_policy = apply_librarian_execution_plan(
        base,
        librarian,
    )
    side_a_lane, side_b_lane = [
        lane for lane in initial_execution.lanes if lane.role == "core"
    ]
    refined_text = "How does camera movement shape visual emphasis?"
    refined_subqueries = list(librarian.subqueries)
    original_side_b = refined_subqueries[1]
    refined_subqueries[1] = LibrarianSubqueryV1(
        role=original_side_b.role,
        text=refined_text,
        target_doc_ids=(),
        seat_quota=original_side_b.seat_quota,
        tier=original_side_b.tier,
        rerank_cap=original_side_b.rerank_cap,
    )
    refined_plan = librarian.model_copy(
        update={"subqueries": tuple(refined_subqueries)}
    )
    embedded: list[list[str]] = []
    searched_queries: list[str] = []
    rerank_calls = 0
    hydration_calls = 0
    refined_doc_scopes: list[list[str] | None] = []
    graph_calls: list[tuple[str, tuple[str, ...]]] = []
    refiner_calls = 0

    async def fake_filter(corpus_ids):
        return corpus_ids, []

    async def fake_intersection(tier, corpus_ids):
        return tier, None

    async def fake_config(corpus_ids):
        return None

    async def fake_embed(texts, _config):
        embedded.append(list(texts))
        return [[float(index + 1)] for index, _text in enumerate(texts)]

    async def fake_search(*args, **kwargs):
        query_text = str(
            kwargs.get("query_text")
            or (args[0] if args and isinstance(args[0], str) else "")
        )
        searched_queries.append(query_text)
        if query_text == side_b_lane.query:
            return []
        if query_text == refined_text:
            refined_doc_scopes.append(kwargs.get("doc_ids"))
            if refinement_scenario == "persistent":
                return []
            chunk = _chunk("camera-refined")
            chunk.doc_id = "camera"
            chunk.text = "Camera movement shapes visual emphasis through framing."
            return [chunk]
        if query_text in {query, side_a_lane.query}:
            chunk = _chunk("story-initial")
            chunk.doc_id = "story"
            chunk.text = "Story craft uses narrative directing and dramatic structure."
            return [chunk]
        return []

    async def identity(chunks, _corpus_ids):
        return chunks

    async def hydrate_once(chunks, _corpus_ids, query=None):
        nonlocal hydration_calls
        hydration_calls += 1
        return chunks

    async def rerank(_query, chunks):
        nonlocal rerank_calls
        rerank_calls += 1
        return chunks

    async def no_graph_facts(*_args, **_kwargs):
        return []

    async def graph_expand(chunks, *_args, query, **_kwargs):
        graph_calls.append(
            (
                query,
                tuple(sorted({str(chunk.doc_id) for chunk in chunks})),
            )
        )
        return []

    async def fake_refine(**kwargs):
        nonlocal refiner_calls
        refiner_calls += 1
        assert kwargs["original_query"] == query
        assert [gap.lane_id for gap in kwargs["gaps"]] == [side_b_lane.lane_id]
        assert {item.doc_id for item in kwargs["seated_documents"]} == {"story"}
        assert kwargs["seated_documents"][0].summary == (
            "Narrative directing and dramatic structure."
        )

        if refinement_scenario == "fallback":

            class _FallbackResult:
                status = "fallback"
                plan = librarian

                @staticmethod
                def diagnostics():
                    return {
                        "status": "fallback",
                        "reason": "planner_refinement_unavailable:test",
                        "fired": True,
                        "gaps": [
                            item.model_dump(mode="json") for item in kwargs["gaps"]
                        ],
                        "refined_subquery_indexes": [],
                        "refined_plan": None,
                        "cache": {
                            "hit": False,
                            "key": "sha256:" + "c" * 64,
                        },
                        "provider_calls": 1,
                        "silent_fallback_count": 1,
                        "planner_refinement_unavailable": True,
                        "round": 0,
                    }

            return _FallbackResult()

        class _Result:
            status = "built"
            plan = refined_plan

            @staticmethod
            def diagnostics():
                return {
                    "status": "built",
                    "reason": "test_refinement",
                    "fired": True,
                    "gaps": [item.model_dump(mode="json") for item in kwargs["gaps"]],
                    "refined_subquery_indexes": [1],
                    "refined_plan": refined_plan.model_dump(mode="json"),
                    "cache": {"hit": False, "key": "sha256:" + "b" * 64},
                    "provider_calls": 1,
                    "silent_fallback_count": 0,
                    "planner_refinement_unavailable": False,
                    "round": 1,
                }

        return _Result()

    monkeypatch.setattr(orchestrator, "_filter_existing_corpora", fake_filter)
    monkeypatch.setattr(
        orchestrator,
        "_enforce_strategy_intersection",
        fake_intersection,
    )
    monkeypatch.setattr(orchestrator, "_embedding_config_for_query", fake_config)
    monkeypatch.setattr(
        orchestrator,
        "_retrieve_graph_seed_facts",
        no_graph_facts,
    )
    monkeypatch.setattr(retriever_module, "embed_queries", fake_embed)
    monkeypatch.setattr(retriever_module.funnel_a, "search", fake_search)
    monkeypatch.setattr(retriever_module.funnel_b, "search", fake_search)
    monkeypatch.setattr(retriever_module.lexical_retriever, "search", fake_search)
    monkeypatch.setattr(retriever_module, "attach_document_identities", identity)
    monkeypatch.setattr(retriever_module, "hydrate_rerank_texts", identity)
    monkeypatch.setattr(retriever_module, "hydrate_chunks", hydrate_once)
    monkeypatch.setattr(retriever_module.reranker_service, "rerank", rerank)
    monkeypatch.setattr(
        retriever_module.mode_a_expansion,
        "expand",
        graph_expand,
    )
    monkeypatch.setattr(retriever_module.librarian_refiner, "refine", fake_refine)
    monkeypatch.setattr(
        retriever_module,
        "select_with_diversity",
        lambda ranked, **kwargs: SimpleNamespace(
            candidates=ranked[: kwargs["final_top_k"]],
            diagnostics={"required_coverage": 1.0},
        ),
    )

    result = await orchestrator.retrieve_planned(
        plan=base,
        corpus_ids=["c1"],
        retrieval_tier=RetrievalTier.qdrant_mongo_graph,
        rerank_enabled=True,
        rerank_top_n=32,
        final_top_k=6,
        librarian_plan=librarian,
        librarian_refinement_enabled=True,
        librarian_refinement_user_id="user",
    )

    refinement = result.diagnostics["librarian_execution"]["refinement"]
    if refinement_scenario == "fallback":
        assert len(embedded) == 1
        assert refined_text not in searched_queries
        assert refinement["status"] == "fallback"
        assert refinement["planner_refinement_unavailable"] is True
        assert refinement["silent_fallback_count"] == 1
        assert refinement["second_pass"]["attempted"] is False
        assert [chunk.chunk_id for chunk in result.chunks] == ["story-initial"]
        assert [chunk.doc_id for chunk in result.chunks] == ["story"]
        assert rerank_calls == 1
        assert hydration_calls == 1
        assert refiner_calls == 1
        return

    refined_candidate = refinement_scenario == "success"
    assert len(embedded) == 2
    assert embedded[1] == [refined_text]
    assert refined_text in searched_queries
    assert refined_doc_scopes
    assert all(not scope for scope in refined_doc_scopes)
    assert (
        side_a_lane.query
        not in searched_queries[searched_queries.index(refined_text) :]
    )
    assert refinement["second_pass"]["lane_ids"] == [side_b_lane.lane_id]
    assert refinement["second_pass"]["embed_batches"] == 1
    assert refinement["second_pass"]["improved_seating"] is refined_candidate
    assert (
        refinement["second_pass"]["before_lane_quota_fulfilled"][side_b_lane.lane_id]
        == 0
    )
    side_b_after = refinement["second_pass"]["after_lane_quota_fulfilled"][
        side_b_lane.lane_id
    ]
    assert (side_b_after >= 1) is refined_candidate
    assert (
        refinement["second_pass"]["after_lane_quota_fulfilled"][side_a_lane.lane_id]
        >= 1
    )
    if refined_candidate:
        assert {chunk.doc_id for chunk in result.chunks} >= {"story", "camera"}
        assert [graph_query for graph_query, _seeds in graph_calls] == [
            base.standalone_query,
            refined_text,
        ]
        assert graph_calls[1][1] == ("camera",)
        assert refinement["second_pass"]["remaining_gaps"] == []
    else:
        assert {chunk.doc_id for chunk in result.chunks} == {"story"}
        assert [graph_query for graph_query, _seeds in graph_calls] == [
            base.standalone_query,
        ]
        assert refinement["second_pass"]["remaining_gaps"]
    assert refiner_calls == 1
    assert refinement["second_pass"]["max_rounds"] == 1
    assert (
        result.diagnostics["librarian_execution"]["execution"]["logical_rerank_batches"]
        == 2
    )
    assert rerank_calls == 2
    assert hydration_calls == 1
    assert (
        initial_policy.lane_seat_quotas
        == result.diagnostics["librarian_execution"]["lane_seat_quotas"]
    )


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

    async def focused_rerank(query, chunks):
        calls.append("rerank")
        return chunks

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
    monkeypatch.setattr(retriever_module.reranker_service, "rerank", focused_rerank)

    result = await orchestrator.retrieve_planned(
        plan=plan,
        corpus_ids=["c1"],
        retrieval_tier=RetrievalTier.qdrant_only,
        rerank_enabled=True,
    )

    assert calls
    assert result.effective_tier == RetrievalTier.qdrant_only
    assert "summary" in calls
    assert result.diagnostics["limits"]["summary_top_k"] >= 12
    assert result.diagnostics["counts"]["lexical"] == 0
    assert "rerank" in calls
    assert result.diagnostics["reranker"]["status"] != "skipped_by_request"


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
    monkeypatch.setattr(retriever_module.settings, "QUERY_PLAN_QUALITY_FIRST", False)

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
