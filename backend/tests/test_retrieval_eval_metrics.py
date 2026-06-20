from services.retriever.eval_metrics import (
    RetrievalEvalCase,
    average_precision_at_k,
    case_from_mapping,
    ndcg_at_k,
    reciprocal_rank_at_k,
    route_metric_profile,
    summarize_route_eval,
)


def test_average_precision_matches_ranked_relevant_example():
    ranked = ["r1", "n2", "r3", "r4", "n5", "n6", "r7", "n8"]
    relevant = {"r1": 1, "r3": 1, "r4": 1, "r7": 1}

    ap = average_precision_at_k(ranked, relevant, k=8)

    expected = (1 / 1 + 2 / 3 + 3 / 4 + 4 / 7) / 4
    assert ap == expected


def test_mrr_can_be_perfect_while_map_is_weak():
    ranked = ["first_good", "bad2", "bad3", "bad4"]
    relevant = {
        "first_good": 1,
        "missing_a": 1,
        "missing_b": 1,
        "missing_c": 1,
    }

    assert reciprocal_rank_at_k(ranked, relevant, k=5) == 1.0
    assert average_precision_at_k(ranked, relevant, k=20) == 0.25


def test_ndcg_uses_graded_relevance_and_order():
    ideal_ranked = ["direct", "support", "mention"]
    weak_ranked = ["mention", "support", "direct"]
    relevance = {"direct": 3, "support": 2, "mention": 1}

    assert ndcg_at_k(ideal_ranked, relevance, k=3) == 1.0
    assert 0.0 < ndcg_at_k(weak_ranked, relevance, k=3) < 1.0


def test_duplicates_do_not_get_extra_metric_credit():
    ranked = ["good", "good", "bad", "also_good"]
    relevant = {"good": 1, "also_good": 1}

    assert reciprocal_rank_at_k(ranked, relevant, k=5) == 1.0
    assert average_precision_at_k(ranked, relevant, k=5) == (1 / 1 + 2 / 3) / 2


def test_route_profiles_match_tier_goals():
    fast = route_metric_profile("Fast Search")
    assert fast["optimize_for"] == ["MRR@5", "latency_p95_ms"]

    hybrid = route_metric_profile("Hybrid Search")
    assert hybrid["optimize_for"] == [
        "MRR@5",
        "NDCG@8",
        "unique_doc_count",
        "near_duplicate_rate",
    ]

    graph = route_metric_profile("Graph Augmentation")
    assert graph["optimize_for"] == [
        "NDCG@8",
        "answer_sufficiency_rate",
        "graph_advantage",
        "atom_coverage",
        "facts_used",
        "relations_used",
        "multi_doc_evidence_rate",
        "near_duplicate_rate",
        "latency_p95_ms",
    ]
    assert graph["secondary_diagnostics"] == ["MRR@5", "MAP@20"]


def test_summarize_route_eval_aggregates_quality_latency_and_answerability():
    cases = [
        RetrievalEvalCase(
            query="q1",
            route="Graph Augmentation",
            ranked_ids=("a", "b", "c"),
            relevance={"a": 3, "b": 2, "c": 1},
            latency_ms=7000,
            answer_sufficient=True,
            doc_ids=("doc1", "doc2", "doc3"),
            diagnostics={
                "atom_coverage": 1.0,
                "facts_used": 12,
                "relations_used": 7,
                "graph_advantage": 0.9,
                "near_duplicate_rate": 0.0,
            },
        ),
        RetrievalEvalCase(
            query="q2",
            route="Graph Augmentation",
            ranked_ids=("x", "z", "y"),
            relevance={"x": 3, "y": 2},
            latency_ms=9000,
            answer_sufficient=False,
            doc_ids=("doc1",),
            diagnostics={
                "atom_coverage": 0.5,
                "facts_used": 2,
                "relations_used": 1,
                "graph_advantage": 0.3,
                "near_duplicate_rate": 0.2,
            },
        ),
    ]

    summary = summarize_route_eval(cases)

    assert summary["query_count"] == 2
    assert summary["MRR@5"] == 1.0
    assert 0.0 < summary["MAP@20"] <= 1.0
    assert 0.0 < summary["NDCG@8"] <= 1.0
    assert summary["answer_sufficiency_rate"] == 0.5
    assert summary["unique_doc_count_avg"] == 2.0
    assert summary["multi_doc_evidence_rate"] == 0.5
    assert summary["atom_coverage_avg"] == 0.75
    assert summary["facts_used_avg"] == 7
    assert summary["relations_used_avg"] == 4
    assert summary["graph_advantage_avg"] == 0.6
    assert summary["near_duplicate_rate_avg"] == 0.1
    assert summary["latency_p50_ms"] == 8000
    assert summary["latency_p95_ms"] == 8900


def test_case_from_mapping_accepts_common_eval_shapes():
    case = case_from_mapping(
        {
            "query": "what is python",
            "retrieval_tier": "qdrant_mongo",
            "candidates": [
                {"chunk_id": "c1", "doc_id": "doc-a"},
                {"chunk_id": "c2", "doc_id": "doc-b"},
            ],
            "qrels": ["c2"],
            "latency_s": 1.5,
            "answerability_pass": True,
            "answerability": {"atom_coverage": 0.8},
            "evidence_delta": {
                "neo4j_facts": 4,
                "neo4j_relations": 3,
            },
            "graph_advantage": {"score": 0.7},
            "near_duplicate_rate": 0.0,
        }
    )

    assert case.route == "qdrant_mongo"
    assert case.ranked_ids == ("c1", "c2")
    assert case.relevance["c2"] == 1.0
    assert case.latency_ms == 1500
    assert case.answer_sufficient is True
    assert case.doc_ids == ("doc-a", "doc-b")
    assert case.diagnostics["atom_coverage"] == 0.8
    assert case.diagnostics["facts_used"] == 4
    assert case.diagnostics["relations_used"] == 3
    assert case.diagnostics["graph_advantage"] == 0.7
    assert case.diagnostics["near_duplicate_rate"] == 0.0
