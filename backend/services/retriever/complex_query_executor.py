"""Bounded subquery DAG executor (Phase 3 skeleton — dark by default).

Scheduling only in this slice: plans Wave 1–4 order, enforces caps, and does
NOT run sequential per-subquery embed/rerank pipelines. Live retrieval still
goes through retrieve_planned until Phase 4+ lane wiring lands.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from models.complex_query import (
    RootQueryIR,
    SubQueryPlanV1,
    SubQueryResultV1,
    dag_is_acyclic,
)
from services.retriever.complex_query_templates import (
    build_root_query_ir,
    compile_subquery_dag,
    default_obligations_for_intent,
)


@dataclass
class ComplexQueryPlanBundle:
    root: RootQueryIR
    obligations: list[Any]
    subqueries: list[SubQueryPlanV1]
    waves: dict[int, list[str]] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)


def corpus_allowlisted(corpus_ids: list[str], settings: Any) -> bool:
    raw = str(getattr(settings, "COMPLEX_QUERY_CORPUS_ALLOWLIST", "") or "")
    allowed = {x.strip() for x in raw.split(",") if x.strip()}
    if not allowed:
        return False
    return any(cid in allowed for cid in corpus_ids)


def planner_enabled(settings: Any, corpus_ids: list[str]) -> bool:
    """Global planner master switch — production stays off until owner GO."""

    if not bool(getattr(settings, "COMPLEX_QUERY_SUBQUERY_PLANNER_ENABLED", False)):
        return False
    return corpus_allowlisted(corpus_ids, settings)


def fixture_runtime_enabled(settings: Any, corpus_ids: list[str]) -> bool:
    """Allowlist-scoped full executor — independent of the global planner flag.

    COMPLEX_QUERY_RUNTIME_ENABLED is the candidate-adoption companion switch;
    FIXTURE_RUNTIME remains supported for continuity.
    """

    runtime = bool(getattr(settings, "COMPLEX_QUERY_FIXTURE_RUNTIME_ENABLED", False)) or bool(
        getattr(settings, "COMPLEX_QUERY_RUNTIME_ENABLED", False)
    )
    if not runtime:
        return False
    # Prefer candidate allowlist when present; else legacy corpus allowlist.
    cand = str(getattr(settings, "COMPLEX_QUERY_CANDIDATE_CORPUS_ALLOWLIST", "") or "").strip()
    if cand:
        allowed = {x.strip() for x in cand.split(",") if x.strip()}
        return any(str(c) in allowed for c in (corpus_ids or []) if str(c))
    return corpus_allowlisted(corpus_ids, settings)


def plan_complex_query(
    *,
    original_query: str,
    standalone_query: str | None = None,
    corpus_ids: list[str] | None = None,
    conversation_constraints: list[str] | None = None,
    requested_mode: str = "qdrant_mongo_graph",
    settings: Any | None = None,
    query_ir_hash: str = "",
) -> ComplexQueryPlanBundle:
    if settings is None:
        from config import get_settings

        settings = get_settings()
    ids = [str(c) for c in (corpus_ids or []) if str(c)]
    root = build_root_query_ir(
        original_query=original_query,
        standalone_query=standalone_query,
        corpus_ids=ids,
        conversation_constraints=conversation_constraints,
        requested_mode=requested_mode,
        query_ir_hash=query_ir_hash,
    )
    obligations = default_obligations_for_intent(
        root.intent_class, query_text=root.standalone_query
    )
    subqueries = compile_subquery_dag(root, obligations, settings=settings)
    waves: dict[int, list[str]] = defaultdict(list)
    for sq in subqueries:
        waves[int(sq.wave)].append(sq.subquery_id)
    assert dag_is_acyclic(subqueries)
    graph_sq = sum(1 for s in subqueries if s.query_type == "graph_path")
    abs_graph = int(getattr(settings, "COMPLEX_QUERY_GRAPH_SUBQUERY_ABSOLUTE_MAX", 3))
    if graph_sq > abs_graph:
        raise RuntimeError(f"graph subquery count {graph_sq} exceeds absolute max {abs_graph}")
    return ComplexQueryPlanBundle(
        root=root,
        obligations=obligations,
        subqueries=subqueries,
        waves={k: v for k, v in sorted(waves.items())},
        diagnostics={
            "planner_enabled": planner_enabled(settings, ids),
            "allowlisted": corpus_allowlisted(ids, settings),
            "subquery_count": len(subqueries),
            "graph_subquery_count": graph_sq,
            "intent_class": root.intent_class,
            "graph_level": root.graph_level,
            "direct_lane_present": any(
                s.query_type == "direct_evidence" for s in subqueries
            ),
            "rerank_calls_budget": int(
                getattr(settings, "COMPLEX_QUERY_RERANK_CALLS_PER_ROOT", 1)
            ),
            "neo4j_round_trips_max": int(
                getattr(settings, "COMPLEX_QUERY_NEO4J_ROUND_TRIPS_MAX", 2)
            ),
            "execution_mode": "plan_only_until_phase_4",
        },
    )


def schedule_waves(
    subqueries: list[SubQueryPlanV1],
) -> list[list[SubQueryPlanV1]]:
    """Return executable waves; within a wave, plans are concurrent-safe."""

    by_wave: dict[int, list[SubQueryPlanV1]] = defaultdict(list)
    for sq in subqueries:
        by_wave[int(sq.wave)].append(sq)
    return [by_wave[w] for w in sorted(by_wave)]


def placeholder_results(bundle: ComplexQueryPlanBundle) -> list[SubQueryResultV1]:
    """Plan-only results — status unsupported until lanes execute."""

    return [
        SubQueryResultV1(
            subquery_id=sq.subquery_id,
            obligation_id=sq.obligation_id,
            status="blocked" if sq.query_type == "verification" else "unsupported",
            unresolved_items=["phase_4_lane_wiring_pending"],
            coverage={"wave": sq.wave, "query_type": sq.query_type},
        ).with_hash()
        for sq in bundle.subqueries
    ]
