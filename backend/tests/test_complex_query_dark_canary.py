"""Portable dark-canary invariants — no live stack required."""

from __future__ import annotations

from types import SimpleNamespace

from models.dark_canary import DarkCanaryComparisonV1
from services.retriever.complex_query_dark_canary import (
    acceptance_snapshot,
    build_comparison,
    evaluate_rollback,
    gate_dark_canary,
    process_canary_enabled,
    trigger_rollback,
)


def _settings(**kwargs):
    base = dict(
        COMPLEX_QUERY_DARK_CANARY_ENABLED=True,
        COMPLEX_QUERY_DARK_CANARY_CORPUS_ALLOWLIST="gsem-e2e-20260804a",
        COMPLEX_QUERY_DARK_CANARY_USER_ALLOWLIST="user-a",
        COMPLEX_QUERY_DARK_CANARY_CORPORA_MAX=3,
        COMPLEX_QUERY_DARK_CANARY_USERS_MAX=3,
        COMPLEX_QUERY_DARK_CANARY_QUERIES_MAX=100,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_gate_requires_allowlisted_corpus_and_user():
    s = _settings()
    g = gate_dark_canary(
        settings=s, corpus_ids=["gsem-e2e-20260804a"], user_id="user-a"
    )
    assert g.allowed is True
    g2 = gate_dark_canary(settings=s, corpus_ids=["other"], user_id="user-a")
    assert g2.allowed is False
    assert g2.reason == "corpus_not_allowlisted"


def test_gate_off_when_flag_false():
    s = _settings(COMPLEX_QUERY_DARK_CANARY_ENABLED=False)
    g = gate_dark_canary(
        settings=s, corpus_ids=["gsem-e2e-20260804a"], user_id="user-a"
    )
    assert g.allowed is False


def test_comparison_shadow_fields_and_no_answer_mutation():
    row = build_comparison(
        query_id="q1",
        query="How should C++ combat translate to Luau?",
        corpus_ids=["gsem-e2e-20260804a"],
        user_id="user-a",
        query_class="cross_domain_translation",
        baseline_child_ids=["doc_a_c0", "doc_b_c0"],
        complex_query_diagnostics={
            "selected_evidence_ids": ["doc_a_c0", "doc_luau_c0"],
            "path_ids": ["p1"],
            "graph_path_summaries": [
                {"path_id": "p1", "node_ids": ["combat", "luau"], "supporting_child_ids": ["doc_a_c0"]}
            ],
            "graph_paths_used": 1,
            "every_path_has_child_support": True,
            "ranking_mutated": False,
            "silent_hybrid_fallback": 0,
            "neo4j_round_trips": 1,
            "hydration_batch_fetches": 1,
            "reranker_calls": 1,
            "traversal": {"status": "ok", "neo4j_round_trips": 1},
            "answer_verification": {
                "claims_total": 2,
                "claims_supported": 2,
                "claims_unsupported": 0,
                "citation_coverage": 1.0,
            },
            "stage_timings_ms": {"complex_query_total": 120.0},
        },
        baseline_retrieval_ms=800.0,
        requested_tier="qdrant_mongo_graph",
        effective_tier="qdrant_mongo_graph",
        synthesis_preflight_status="ok",
        provider_status="skipped_shadow_retrieval_only",
    )
    assert row.user_visible_answer_changed is False
    assert row.production_ranking_mutated is False
    assert row.graph_status == "executed_with_paths"
    assert row.neo4j_round_trips == 1
    assert row.rollback_triggered is False


def test_silent_hybrid_triggers_rollback():
    row = build_comparison(
        query_id="hyb",
        query="combat path",
        corpus_ids=["gsem-e2e-20260804a"],
        user_id="user-a",
        query_class="two_hop_dependency",
        baseline_child_ids=["doc_a_c0"],
        complex_query_diagnostics={
            "selected_evidence_ids": ["doc_a_c0"],
            "graph_paths_used": 0,
            "every_path_has_child_support": True,
            "ranking_mutated": False,
            "silent_hybrid_fallback": 0,
            "answer_verification": {},
            "stage_timings_ms": {},
            "traversal": {"skipped": True},
        },
        baseline_retrieval_ms=10.0,
        requested_tier="qdrant_mongo_graph",
        effective_tier="qdrant_mongo",
        downgrade_reason="strategy_intersection",
    )
    assert row.silent_hybrid_fallback == 1
    assert row.rollback_triggered is True
    assert "silent_graph_to_hybrid_downgrade" in row.rollback_reasons


def test_packet_unsupported_does_not_rollback_without_synthesis():
    packet_only = DarkCanaryComparisonV1(
        query_id="packet",
        packet_claims_unsupported=1,
        claims_unsupported=0,
        dark_synthesis_ran=False,
        dark_child_ids=["doc_x_c0"],
        dark_citation_validity=1.0,
    )
    assert evaluate_rollback(packet_only).rollback_triggered is False


def test_expanded_acceptance_gain_and_negative_control():
    rows = []
    for i in range(7):
        rows.append(
            build_comparison(
                query_id=f"rel{i}",
                query="dependency path",
                corpus_ids=["gsem-e2e-20260804a"],
                user_id="user-a",
                query_class="two_hop_dependency",
                baseline_child_ids=["doc_a_c0"],
                complex_query_diagnostics={
                    "selected_evidence_ids": ["doc_a_c0", "doc_b_c0"],
                    "graph_paths_used": 1,
                    "every_path_has_child_support": True,
                    "ranking_mutated": False,
                    "path_ids": ["p1"],
                    "graph_path_summaries": [
                        {"path_id": "p1", "node_ids": ["a", "b"], "supporting_child_ids": ["doc_b_c0"]}
                    ],
                    "traversal": {"status": "ok", "neo4j_round_trips": 1},
                    "neo4j_round_trips": 1,
                    "hydration_batch_fetches": 1,
                    "reranker_calls": 0,
                    "answer_verification": {
                        "claims_total": 1,
                        "claims_supported": 1,
                        "claims_unsupported": 0,
                    },
                    "stage_timings_ms": {"complex_query_total": 50.0},
                },
                baseline_retrieval_ms=100.0,
                requested_tier="qdrant_mongo_graph",
                effective_tier="qdrant_mongo_graph",
            ).model_dump()
        )
    rows.append(
        build_comparison(
            query_id="neg",
            query="what is 2+2",
            corpus_ids=["gsem-e2e-20260804a"],
            user_id="user-a",
            query_class="nonrelationship_negative_control",
            baseline_child_ids=["doc_a_c0"],
            complex_query_diagnostics={
                "selected_evidence_ids": ["doc_a_c0"],
                "graph_paths_used": 0,
                "every_path_has_child_support": True,
                "ranking_mutated": False,
                "traversal": {"status": "skipped"},
                "answer_verification": {},
                "stage_timings_ms": {"complex_query_total": 20.0},
                "hydration_batch_fetches": 1,
            },
            baseline_retrieval_ms=80.0,
            requested_tier="qdrant_mongo_graph",
            effective_tier="qdrant_mongo_graph",
        ).model_dump()
    )
    snap = acceptance_snapshot(rows)
    assert snap["graph_relationship_gain_positive_rate"] >= 0.70
    assert snap["nonrelationship_irrelevant_graph_addition_rate"] <= 0.10
    assert snap["pass"] is True


def test_trigger_rollback_disables_process_gate():
    from services.retriever import complex_query_dark_canary as mod

    mod._STATE["enabled"] = True
    mod._STATE["disabled_reason"] = None
    trigger_rollback("test_reason")
    assert process_canary_enabled() is False
    mod._STATE["enabled"] = True
    mod._STATE["disabled_reason"] = None
