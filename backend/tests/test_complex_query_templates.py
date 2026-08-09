"""Deterministic complex-query templates + dark executor."""

from __future__ import annotations

from types import SimpleNamespace

from models.complex_query import dag_is_acyclic
from services.retriever.complex_query_executor import (
    plan_complex_query,
    planner_enabled,
    schedule_waves,
)
from services.retriever.complex_query_templates import (
    compile_subquery_dag,
    detect_intent_class,
)


def test_detect_cross_domain_intent():
    intent = detect_intent_class(
        "How should a C++ combat update loop be translated into Roblox Luau?"
    )
    assert intent == "cross_domain_translation"


def test_compile_dag_bounded_and_has_direct_lane():
    bundle = plan_complex_query(
        original_query="How should a C++ combat update loop be translated into Roblox Luau?",
        corpus_ids=["gsem-e2e-20260804a"],
        settings=SimpleNamespace(
            COMPLEX_QUERY_ABSOLUTE_MAX_SUBQUERIES=12,
            COMPLEX_QUERY_BEAM_WIDTH=8,
            COMPLEX_QUERY_ABSOLUTE_MAX_HOPS=3,
            COMPLEX_QUERY_SEED_ENTITY_CAP=8,
            COMPLEX_QUERY_GRAPH_SUBQUERY_ABSOLUTE_MAX=3,
            COMPLEX_QUERY_SUBQUERY_PLANNER_ENABLED=False,
            COMPLEX_QUERY_CORPUS_ALLOWLIST="gsem-e2e-20260804a",
            COMPLEX_QUERY_RERANK_CALLS_PER_ROOT=1,
            COMPLEX_QUERY_NEO4J_ROUND_TRIPS_MAX=2,
        ),
    )
    assert bundle.root.intent_class == "cross_domain_translation"
    assert bundle.root.graph_level == "graph_deep"
    assert dag_is_acyclic(bundle.subqueries)
    assert any(s.query_type == "direct_evidence" for s in bundle.subqueries)
    assert any(s.query_type == "vocabulary_resolution" for s in bundle.subqueries)
    assert sum(1 for s in bundle.subqueries if s.query_type == "graph_path") <= 3
    assert len(bundle.subqueries) <= 12
    waves = schedule_waves(bundle.subqueries)
    assert waves[0]  # wave 1 nonempty
    assert bundle.diagnostics["execution_mode"] == "plan_only_until_phase_4"


def test_planner_dark_without_flag():
    settings = SimpleNamespace(
        COMPLEX_QUERY_SUBQUERY_PLANNER_ENABLED=False,
        COMPLEX_QUERY_CORPUS_ALLOWLIST="gsem-e2e-20260804a",
    )
    assert planner_enabled(settings, ["gsem-e2e-20260804a"]) is False


def test_planner_requires_allowlist():
    settings = SimpleNamespace(
        COMPLEX_QUERY_SUBQUERY_PLANNER_ENABLED=True,
        COMPLEX_QUERY_CORPUS_ALLOWLIST="gsem-e2e-20260804a",
    )
    assert planner_enabled(settings, ["gsem-e2e-20260804a"]) is True
    assert planner_enabled(settings, ["production-corpus"]) is False


def test_direct_lookup_is_graph_lite():
    bundle = plan_complex_query(
        original_query="What does RAG use?",
        corpus_ids=["gsem-e2e-20260804a"],
        settings=SimpleNamespace(
            COMPLEX_QUERY_ABSOLUTE_MAX_SUBQUERIES=12,
            COMPLEX_QUERY_BEAM_WIDTH=8,
            COMPLEX_QUERY_ABSOLUTE_MAX_HOPS=3,
            COMPLEX_QUERY_SEED_ENTITY_CAP=8,
            COMPLEX_QUERY_GRAPH_SUBQUERY_ABSOLUTE_MAX=3,
            COMPLEX_QUERY_SUBQUERY_PLANNER_ENABLED=False,
            COMPLEX_QUERY_CORPUS_ALLOWLIST="gsem-e2e-20260804a",
            COMPLEX_QUERY_RERANK_CALLS_PER_ROOT=1,
            COMPLEX_QUERY_NEO4J_ROUND_TRIPS_MAX=2,
        ),
    )
    assert bundle.root.graph_level == "graph_lite"
    assert all(s.maximum_hops <= 1 for s in bundle.subqueries if s.query_type == "graph_path")
