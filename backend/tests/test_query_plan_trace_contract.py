from models.schemas import RetrievalTier
from services.chat_orchestrator import _format_retrieval_diagnostics_trace


def test_query_plan_v2_trace_reports_real_stores_and_candidate_counts():
    diagnostics = {
        "status": "query_plan_v2_degraded",
        "query_plan_version": "query_plan.v2",
        "effective_tier": "qdrant_mongo",
        "store_contract": {
            "label": "Hybrid Search",
            "description": "Qdrant vectors plus Mongo lexical retrieval.",
            "qdrant_vectors": True,
            "mongo_lexical": True,
            "neo4j_facts": False,
        },
        "search_mode": "local",
        "intent": {"need": "balanced"},
        "counts": {
            "funnel_a": 20,
            "funnel_b": 40,
            "lexical": 12,
            "merged_initial": 110,
            "ranked": 16,
            "ranked_query_grounded": 9,
            "distinct_docs_merged": 31,
            "distinct_docs_in_pool": 9,
        },
        "limits": {
            "child_top_k": 40,
            "summary_top_k": 20,
            "final_top_k": 5,
            "rerank_enabled": True,
        },
        "final_source_tiers": {"child": 4, "summary": 1},
        "unique_docs_final": 4,
        "max_doc_share_final": 0.4,
        "timings_s": {
            "embed": 0.1,
            "candidate_generation": 2.0,
            "rerank": 4.5,
            "hydrate_finalists": 0.2,
        },
        "total_s": 6.8,
    }

    trace = _format_retrieval_diagnostics_trace(
        diagnostics,
        fallback_tier=RetrievalTier.qdrant_mongo,
        raw_chunks=5,
        context_chunks=5,
    )

    assert "tier: Hybrid Search (qdrant_mongo)" in trace
    assert "Qdrant vectors" in trace
    assert "Mongo lexical/hydration" in trace
    assert "training-data only or no corpus stores" not in trace
    assert "merged=110 ranked=16 grounded=9 final=5/5" in trace
    assert "child=4" in trace and "summary=1" in trace
    assert "funnels=2.00s" in trace
    assert "hydrate=0.20s" in trace
