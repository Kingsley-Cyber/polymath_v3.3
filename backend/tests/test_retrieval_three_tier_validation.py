from services.retriever.three_tier_eval import (
    anchor_coverage,
    evaluate_route_result,
    extract_trace_summary,
    route_latency_budget,
    sources_joined_text,
    summarize_report,
    summarize_sources,
)


def test_source_anchor_text_includes_normalized_citation_title():
    joined = sources_joined_text(
        [
            {
                "doc_name": "chip-heath-dan-heath-made-to-stick-2007.md",
                "text": "A framework for simple, unexpected, concrete ideas.",
            }
        ]
    )
    coverage = anchor_coverage(
        {
            "anchor_groups": [
                {"name": "book", "terms": ["made to stick"], "required": True}
            ]
        },
        joined,
    )

    assert coverage["required_coverage"] == 1.0


def test_summarize_sources_keeps_counts_without_text():
    sources = [
        {
            "chunk_id": "c1",
            "doc_id": "doc-a",
            "parent_id": "p1",
            "source_tier": "mongo",
            "text": "Python is a programming language.",
        },
        {
            "chunk_id": "c2",
            "doc_id": "doc-a",
            "parent_id": "p1",
            "source_tier": "graph",
            "text": "Python is used for AI development.",
            "provenance": [{"kind": "RELATES_TO"}],
        },
    ]

    summary = summarize_sources(sources)

    assert summary["source_count"] == 2
    assert summary["unique_doc_count"] == 1
    assert summary["parent_duplicate_count"] == 2
    assert summary["source_tier_counts"]["mongo"] == 1
    assert summary["source_tier_counts"]["graph"] == 1
    assert summary["sources_with_provenance"] == 1
    assert summary["graph_signal_sources"] == 1
    assert "Python is" not in str(summary)


def test_anchor_coverage_supports_required_groups():
    case = {
        "anchor_groups": [
            {"name": "python", "terms": ["Python"], "required": True},
            {
                "name": "ai",
                "terms": ["AI", "artificial intelligence"],
                "required": True,
            },
            {"name": "ml", "terms": ["ML", "machine learning"], "required": False},
        ]
    }

    coverage = anchor_coverage(case, "Python is used in artificial intelligence work.")

    assert coverage["required_coverage"] == 1.0
    assert coverage["coverage"] == 0.667
    assert coverage["required_missing"] == []
    assert coverage["missing"] == ["ml"]


def test_extract_trace_summary_reads_local_and_graph_advantage():
    trace = [
        {
            "title": "Local RAG retrieval",
            "metadata": {
                "duration_s": 1.5,
                "effective_tier": "qdrant_mongo_graph",
                "retrieval_diagnostics": {"counts": {"graph_expanded": 4}},
            },
        },
        {
            "title": "Graph Advantage",
            "metadata": {"facts_used": 3, "relations_used": 2},
        },
    ]

    summary = extract_trace_summary(trace)

    assert summary["has_local_rag_trace"] is True
    assert summary["has_graph_advantage"] is True
    assert summary["has_graph_trace"] is True
    assert summary["effective_tier"] == "qdrant_mongo_graph"
    assert summary["graph_advantage"]["facts_used"] == 3


def test_extract_trace_summary_prefers_final_populated_retrieval_event():
    trace = [
        {
            "title": "Local RAG retrieval",
            "metadata": {"duration_s": 0.1, "effective_tier": "qdrant_mongo"},
        },
        {
            "title": "Local RAG retrieval",
            "metadata": {
                "duration_s": 1.5,
                "effective_tier": "qdrant_mongo_graph",
                "retrieval_diagnostics": {
                    "query_plan_version": "query_plan.v2",
                    "timings_s": {"rerank": 0.8},
                },
            },
        },
    ]

    summary = extract_trace_summary(trace)

    assert summary["effective_tier"] == "qdrant_mongo_graph"
    assert summary["local_rag_duration_s"] == 1.5
    assert summary["retrieval_diagnostics"]["query_plan_version"] == "query_plan.v2"


def test_graph_route_requires_graph_advantage_trace():
    case = {"anchor_groups": [{"name": "ontology", "terms": ["ontology"]}]}
    result = {
        "answer": "Ontologies are powerful because they encode relationships.",
        "sources": [{"chunk_id": "c1", "doc_id": "d1", "text": "Ontology relations."}],
        "trace_events": [
            {
                "title": "Local RAG retrieval",
                "metadata": {"duration_s": 1.0, "effective_tier": "qdrant_mongo_graph"},
            }
        ],
        "timings_s": {"total": 2.0, "retrieval_done_sources": 1.0},
    }

    validation = evaluate_route_result(
        query_case=case,
        route_name="Graph Augmentation",
        result=result,
    )

    assert validation["status"] == "fail"
    assert any(issue["code"] == "missing_graph_trace" for issue in validation["issues"])


def test_graph_route_reports_zero_signal_augmentation_without_fake_advantage():
    case = {"anchor_groups": [{"name": "topic", "terms": ["topic"]}]}
    result = {
        "answer": "The sources discuss the topic, but no graph edge was established.",
        "sources": [{"chunk_id": "a", "doc_id": "a", "text": "topic evidence"}],
        "trace_events": [
            {
                "title": "Local RAG retrieval",
                "metadata": {
                    "duration_s": 1.0,
                    "effective_tier": "qdrant_mongo_graph",
                },
            },
            {
                "title": "Graph Augmentation",
                "metadata": {
                    "advantage_established": False,
                    "facts_used": 0,
                    "relations_used": 0,
                    "graph_expanded_chunks": 0,
                },
            },
        ],
        "timings_s": {"total": 2.0, "retrieval_done_sources": 1.0},
    }

    validation = evaluate_route_result(
        query_case=case,
        route_name="Graph Augmentation",
        result=result,
    )

    assert validation["status"] == "pass"
    assert validation["trace_summary"]["has_graph_trace"] is True
    assert validation["trace_summary"]["has_graph_advantage"] is False
    assert any(issue["code"] == "weak_graph_signal" for issue in validation["issues"])


def test_fast_route_fails_if_graph_trace_leaks_in():
    case = {"anchor_groups": [{"name": "python", "terms": ["python"]}]}
    result = {
        "answer": "Python is a programming language.",
        "sources": [{"chunk_id": "c1", "doc_id": "d1", "text": "Python."}],
        "trace_events": [
            {
                "title": "Local RAG retrieval",
                "metadata": {"duration_s": 1.0, "effective_tier": "qdrant_only"},
            },
            {"title": "Graph Advantage", "metadata": {"facts_used": 1}},
        ],
        "timings_s": {"total": 2.0, "retrieval_done_sources": 1.0},
    }

    validation = evaluate_route_result(
        query_case=case,
        route_name="Fast Search",
        result=result,
    )

    assert validation["status"] == "fail"
    assert any(
        issue["code"] == "unexpected_graph_advantage" for issue in validation["issues"]
    )


def test_expected_fast_abstention_does_not_fail_empty_source_check():
    case = {
        "expected_empty_routes": ["Fast Search"],
        "anchor_groups": [{"name": "relation", "terms": ["relationship"]}],
    }
    result = {
        "answer": "",
        "sources": [],
        "trace_events": [
            {
                "title": "Local RAG retrieval",
                "metadata": {"duration_s": 0.2, "effective_tier": "qdrant_only"},
            }
        ],
        "timings_s": {"total": 0.3, "retrieval_done_sources": 0.2},
        "stop_after_sources": True,
    }

    validation = evaluate_route_result(
        query_case=case,
        route_name="Fast Search",
        result=result,
    )

    assert validation["status"] == "pass"
    assert validation["expected_empty"] is True
    assert validation["issues"] == []


def test_supported_answer_fails_when_it_omits_a_required_source_concept():
    case = {
        "anchor_groups": [
            {"name": "purple", "terms": ["purple ocean"], "required": True},
            {"name": "sticky", "terms": ["sticky messaging"], "required": True},
        ]
    }
    result = {
        "answer": (
            "Purple Ocean is a market-positioning strategy with a focused "
            "niche and a validated market."
        ),
        "sources": [
            {
                "chunk_id": "purple",
                "doc_id": "purple-doc",
                "text": "Purple Ocean market-positioning strategy.",
            },
            {
                "chunk_id": "sticky",
                "doc_id": "sticky-doc",
                "text": "Sticky messaging makes an offer memorable.",
            },
        ],
        "trace_events": [
            {
                "title": "Local RAG retrieval",
                "metadata": {"duration_s": 1.0, "effective_tier": "qdrant_mongo"},
            }
        ],
        "timings_s": {"total": 2.0, "retrieval_done_sources": 1.0},
    }

    validation = evaluate_route_result(
        query_case=case,
        route_name="Hybrid Search",
        result=result,
    )

    assert validation["status"] == "fail"
    assert any(
        issue["code"] == "required_answer_anchor_missing"
        for issue in validation["issues"]
    )


def test_grounded_answerability_refusal_is_not_synthesis_drift():
    case = {
        "anchor_groups": [
            {"name": "positioning", "terms": ["product positioning"]},
            {"name": "messaging", "terms": ["memorable messaging"]},
        ]
    }
    result = {
        "answer": (
            "I cannot answer that as a source-backed result from the selected "
            "corpus. The retrieval did not establish a relationship."
        ),
        "sources": [
            {"chunk_id": "a", "doc_id": "a", "text": "product positioning"},
            {"chunk_id": "b", "doc_id": "b", "text": "memorable messaging"},
        ],
        "trace_events": [
            {
                "title": "Local RAG retrieval",
                "metadata": {
                    "duration_s": 1.0,
                    "effective_tier": "qdrant_mongo",
                    "retrieval_diagnostics": {
                        "selection": {"sufficiency": {"answerable": False}}
                    },
                },
            }
        ],
        "timings_s": {"total": 2.0, "retrieval_done_sources": 1.0},
    }

    validation = evaluate_route_result(
        query_case=case,
        route_name="Hybrid Search",
        result=result,
    )

    assert validation["status"] == "pass"
    assert validation["grounded_abstention"] is True
    assert not any(
        issue["code"] == "required_answer_anchor_missing"
        for issue in validation["issues"]
    )


def test_summarize_report_aggregates_route_budgets():
    results = [
        {
            "route": "Fast Search",
            "validation": {
                "fail_count": 0,
                "warn_count": 1,
                "timings_s": {"total": 1.2, "retrieval_or_sources": 0.8},
            },
        },
        {
            "route": "Fast Search",
            "validation": {
                "fail_count": 1,
                "warn_count": 0,
                "timings_s": {"total": 2.0, "retrieval_or_sources": 1.5},
            },
        },
    ]

    summary = summarize_report(results)

    assert summary["Fast Search"]["cases"] == 2
    assert summary["Fast Search"]["failures"] == 1
    assert summary["Fast Search"]["warnings"] == 1
    assert summary["Fast Search"]["max_total_s"] == 2.0


def test_route_latency_budget_is_not_a_35_second_blanket():
    assert route_latency_budget("Fast Search")["retrieval_s"] == 2.0
    assert route_latency_budget("Hybrid Search")["retrieval_s"] == 8.0
    assert route_latency_budget("Graph Augmentation")["retrieval_s"] == 10.0


def test_retrieval_budget_is_hard_fail_by_default():
    case = {"anchor_groups": [{"name": "python", "terms": ["python"]}]}
    result = {
        "answer": "Python is a programming language.",
        "sources": [{"chunk_id": "c1", "doc_id": "d1", "text": "Python."}],
        "trace_events": [
            {
                "title": "Local RAG retrieval",
                "metadata": {"duration_s": 6.25, "effective_tier": "qdrant_only"},
            }
        ],
        "timings_s": {"total": 7.0, "retrieval_done_sources": 6.25},
    }

    validation = evaluate_route_result(
        query_case=case,
        route_name="Fast Search",
        result=result,
    )

    assert validation["status"] == "fail"
    assert any(
        issue["code"] == "retrieval_over_budget" for issue in validation["issues"]
    )


def test_total_budget_is_warning_unless_promoted():
    case = {"anchor_groups": [{"name": "python", "terms": ["python"]}]}
    result = {
        "answer": "Python is a programming language.",
        "sources": [{"chunk_id": "c1", "doc_id": "d1", "text": "Python."}],
        "trace_events": [
            {
                "title": "Local RAG retrieval",
                "metadata": {"duration_s": 1.0, "effective_tier": "qdrant_only"},
            }
        ],
        "timings_s": {
            "total": 21.0,
            "retrieval_done_sources": 1.0,
            "generation_after_sources": 20.0,
        },
    }

    warning_only = evaluate_route_result(
        query_case=case,
        route_name="Fast Search",
        result=result,
    )
    hard_fail = evaluate_route_result(
        query_case=case,
        route_name="Fast Search",
        result=result,
        fail_on_total_budget=True,
    )

    assert warning_only["status"] == "pass"
    assert any(issue["code"] == "total_over_budget" for issue in warning_only["issues"])
    assert hard_fail["status"] == "fail"
    assert any(
        issue["code"] == "total_over_budget" and issue["level"] == "fail"
        for issue in hard_fail["issues"]
    )
