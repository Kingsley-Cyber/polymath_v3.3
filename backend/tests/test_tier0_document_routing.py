from types import SimpleNamespace

import pytest

import services.retriever as retriever_module
from models.schemas import RetrievalTier, SourceChunk
from services.retriever.query_plan import build_query_plan_v2
from services.retriever.tier0_router import (
    DocumentRoute,
    Tier0DocumentRouter,
    _is_technical_report_route,
    diversify_document_routes,
    select_adaptive_routes,
    select_title_aligned_routes,
)


def test_route_diversity_reorders_but_preserves_relevant_neighborhood():
    routes = [
        DocumentRoute(
            "creative",
            "c",
            "a",
            0.90,
            "Audience advertising",
            "Target audience response to advertising psychology",
        ),
        DocumentRoute(
            "creative",
            "c",
            "b",
            0.89,
            "Audience advertising copy",
            "Target audience response to advertising psychology",
        ),
        DocumentRoute(
            "creative",
            "c",
            "c",
            0.87,
            "Directing cinematic motion",
            "Opening shots camera movement and visual storytelling",
        ),
    ]

    selected = diversify_document_routes(routes)

    assert [route.doc_id for route in selected] == ["a", "c", "b"]
    assert {route.doc_id for route in selected} == {"a", "b", "c"}


def test_technical_repair_reports_do_not_displace_content_documents():
    routes = [
        DocumentRoute("audience", "c", "content", 0.65, "Breakthrough Advertising"),
        DocumentRoute("audience", "c", "report", 0.64, "EPUB backfill status report"),
    ]

    assert not _is_technical_report_route(routes[0])
    assert _is_technical_report_route(routes[1])


def test_title_aligned_answer_object_routes_gate_generic_semantic_neighbors():
    routes = [
        DocumentRoute("books", "c", "book-a", 0.61, "Books that made me rich"),
        DocumentRoute("books", "c", "book-b", 0.55, "Unpopular book tier list"),
        DocumentRoute("books", "c", "generic", 0.54, "AI tools tier list"),
    ]

    selected = select_title_aligned_routes(routes, ("books",))

    assert [route.doc_id for route in selected] == ["book-a", "book-b"]


def test_title_aligned_routes_fail_open_when_title_match_is_not_competitive():
    routes = [
        DocumentRoute("books", "c", "semantic", 0.80, "Founder interview"),
        DocumentRoute("books", "c", "book", 0.55, "Book notes"),
    ]

    selected = select_title_aligned_routes(routes, ("books",))

    assert [route.doc_id for route in selected] == ["semantic", "book"]


class _RouteClient:
    def __init__(self) -> None:
        self.calls = []

    async def query_points(self, **kwargs):
        self.calls.append(kwargs)
        condition = kwargs["query_filter"].must[0]
        corpus_id = condition.match.value
        return SimpleNamespace(
            points=[
                SimpleNamespace(
                    score=0.72 if corpus_id == "c1" else 0.41,
                    payload={
                        "corpus_id": corpus_id,
                        "doc_id": f"doc-{corpus_id}",
                        "title": f"Document {corpus_id}",
                    },
                ),
                SimpleNamespace(
                    score=0.20,
                    payload={
                        "corpus_id": corpus_id,
                        "doc_id": f"noise-{corpus_id}",
                        "title": "Noise",
                    },
                ),
            ]
        )


@pytest.mark.asyncio
async def test_tier0_router_is_fair_per_lane_and_corpus():
    router = Tier0DocumentRouter.__new__(Tier0DocumentRouter)
    router.client = _RouteClient()

    routes, diagnostics = await router.route_lanes(
        {"books": [0.1], "dropshipping": [0.2]},
        ["c1", "c2"],
    )

    assert len(router.client.calls) == 4
    assert {route.corpus_id for route in routes["books"]} == {"c1", "c2"}
    assert {route.corpus_id for route in routes["dropshipping"]} == {"c1", "c2"}
    assert diagnostics["routed_doc_count"] == 2
    assert all(
        "noise" not in route.doc_id for values in routes.values() for route in values
    )


def test_adaptive_routes_cut_background_tail_at_score_cliff():
    routes = [
        DocumentRoute("books", "c1", "d1", 0.91),
        DocumentRoute("books", "c1", "d2", 0.88),
        DocumentRoute("books", "c1", "d3", 0.86),
        DocumentRoute("books", "c1", "d4", 0.62),
        DocumentRoute("books", "c1", "d5", 0.60),
    ]

    selected = select_adaptive_routes(routes, relative_margin=0.40)

    assert [route.doc_id for route in selected] == ["d1", "d2", "d3"]


def _evidence(lane: str, doc_id: str) -> SourceChunk:
    text = (
        "Influence and other business books teach persuasion useful for "
        "branded dropshipping."
        if lane == "books"
        else "Dropshipping requires product selection, positioning, and persuasion."
    )
    return SourceChunk(
        chunk_id=f"{doc_id}-{lane}",
        parent_id=f"parent-{doc_id}-{lane}",
        doc_id=doc_id,
        corpus_id="c1",
        text=text,
        score=0.9,
        source_tier="tier_a",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tier",
    [
        RetrievalTier.qdrant_only,
        RetrievalTier.qdrant_mongo,
        RetrievalTier.qdrant_mongo_graph,
    ],
)
async def test_all_three_layers_descend_from_document_routes(monkeypatch, tier):
    orchestrator = retriever_module.RetrieverOrchestrator()
    plan = build_query_plan_v2(
        "what books help with dropshipping and why?",
        corpus_ids=["c1"],
    )
    calls = {"summary": 0, "lexical": 0, "rerank": 0, "graph": 0}
    child_scopes: list[dict] = []

    async def fake_filter(corpus_ids):
        return corpus_ids, []

    async def fake_intersection(requested, corpus_ids):
        return requested, None

    async def fake_config(corpus_ids):
        return None

    async def fake_embed(texts, config):
        return [[float(index + 1)] for index, _ in enumerate(texts)]

    async def fake_route(vectors, corpus_ids, **kwargs):
        return (
            {
                "books": [DocumentRoute("books", "c1", "doc-books", 0.8)],
                "books_justification": [
                    DocumentRoute(
                        "books_justification",
                        "c1",
                        "doc-dropshipping",
                        0.8,
                    )
                ],
            },
            {"enabled": True, "routed_doc_count": 2, "routes": {}},
        )

    async def fake_child(*args, **kwargs):
        child_scopes.append(dict(kwargs))
        doc_ids = kwargs.get("doc_ids") or []
        query_text = str(kwargs.get("query_text") or "")
        if doc_ids:
            lane = "books" if "books" in query_text else "dropshipping"
            return [_evidence(lane, doc_ids[0])]
        return [_evidence("dropshipping", "doc-direct")]

    async def fake_summary(*args, **kwargs):
        calls["summary"] += 1
        doc_ids = kwargs.get("doc_ids") or ["doc-direct"]
        lane = (
            "books"
            if "books" in str(kwargs.get("query_text") or "")
            else "dropshipping"
        )
        return [_evidence(lane, doc_ids[0])]

    async def fake_lexical(*args, **kwargs):
        calls["lexical"] += 1
        return []

    async def identity(chunks, corpus_ids, **kwargs):
        return chunks

    async def fake_rerank(query, chunks):
        calls["rerank"] += 1
        return chunks

    async def fake_graph_facts(*args, **kwargs):
        calls["graph"] += 1
        return []

    async def fake_graph_expand(*args, **kwargs):
        calls["graph"] += 1
        return []

    monkeypatch.setattr(retriever_module.settings, "TIER0_ROUTING", True)
    monkeypatch.setattr(orchestrator, "_filter_existing_corpora", fake_filter)
    monkeypatch.setattr(
        orchestrator, "_enforce_strategy_intersection", fake_intersection
    )
    monkeypatch.setattr(orchestrator, "_embedding_config_for_query", fake_config)
    monkeypatch.setattr(orchestrator, "_retrieve_graph_seed_facts", fake_graph_facts)
    monkeypatch.setattr(retriever_module, "embed_queries", fake_embed)
    monkeypatch.setattr(
        retriever_module.tier0_document_router, "route_lanes", fake_route
    )
    monkeypatch.setattr(retriever_module.funnel_b, "search", fake_child)
    monkeypatch.setattr(retriever_module.funnel_a, "search", fake_summary)
    monkeypatch.setattr(retriever_module.lexical_retriever, "search", fake_lexical)
    monkeypatch.setattr(retriever_module, "attach_document_identities", identity)
    monkeypatch.setattr(retriever_module, "hydrate_rerank_texts", identity)
    monkeypatch.setattr(retriever_module, "hydrate_chunks", identity)
    monkeypatch.setattr(retriever_module.reranker_service, "rerank", fake_rerank)
    monkeypatch.setattr(retriever_module.mode_a_expansion, "expand", fake_graph_expand)

    result = await orchestrator.retrieve_planned(
        plan=plan,
        corpus_ids=["c1"],
        retrieval_tier=tier,
        rerank_enabled=True,
        final_top_k=6,
    )

    routed = result.diagnostics["document_routing"]
    lane_rows = {row["lane_id"]: row for row in result.diagnostics["lanes"]}
    assert routed["enabled"] is True
    assert lane_rows["books"]["routed_doc_ids"] == ["doc-books"]
    assert lane_rows["books_justification"]["routed_doc_ids"] == ["doc-dropshipping"]
    assert result.diagnostics["required_concept_coverage"]["coverage"] == 1.0
    assert {chunk.doc_id for chunk in result.chunks} >= {
        "doc-books",
        "doc-dropshipping",
    }
    core_scopes = [scope for scope in child_scopes if scope.get("doc_ids")]
    assert core_scopes
    assert all(scope.get("parent_ids") for scope in core_scopes)

    if tier == RetrievalTier.qdrant_only:
        assert calls["summary"] > 0
        assert calls["lexical"] == 0
        assert calls["rerank"] == 1
        assert calls["graph"] == 0
    else:
        assert calls["summary"] > 0
        assert calls["lexical"] > 0
        assert calls["rerank"] == 1
        assert (calls["graph"] > 0) is (tier == RetrievalTier.qdrant_mongo_graph)
