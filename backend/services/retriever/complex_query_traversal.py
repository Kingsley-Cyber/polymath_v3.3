"""Batched graph traversal compiler (Phase 5) + beam paths (Phase 6).

Parameterized Cypher only — never LLM-generated. Requires child support on
every returned assertion/path edge. Preferred 1 Neo4j round-trip (max 2).
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

from models.complex_query import (
    GraphPathResultV1,
    GraphTraversalBatchV1,
    GraphTraversalPlanV1,
)

# Fixed templates — never string-built from LLM text.
_BATCH_EDGE_FETCH_CYPHER = """
UNWIND $seed_ids AS seed_id
MATCH (s:Entity {entity_id: seed_id})-[r:RELATES_TO]-(o:Entity)
WHERE any(cid IN coalesce(r.corpus_ids, []) WHERE cid IN $corpus_ids)
  AND size(coalesce(r.evidence_chunk_ids, [])) > 0
  AND (
    size($predicates) = 0
    OR coalesce(r.predicate, 'related_to') IN $predicates
  )
  AND coalesce(r.confidence, 0.0) >= $min_confidence
RETURN
  s.entity_id AS src,
  o.entity_id AS dst,
  coalesce(r.predicate, 'related_to') AS predicate,
  coalesce(r.confidence, 0.0) AS confidence,
  coalesce(r.evidence_chunk_ids, []) AS evidence_chunk_ids,
  coalesce(r.assertion_id, '') AS assertion_id
ORDER BY confidence DESC
LIMIT $edge_cap
"""

_BATCH_EDGE_FETCH_HOP2_CYPHER = """
UNWIND $frontier_ids AS seed_id
MATCH (s:Entity {entity_id: seed_id})-[r:RELATES_TO]-(o:Entity)
WHERE any(cid IN coalesce(r.corpus_ids, []) WHERE cid IN $corpus_ids)
  AND size(coalesce(r.evidence_chunk_ids, [])) > 0
  AND NOT o.entity_id IN $seen_ids
  AND (
    size($predicates) = 0
    OR coalesce(r.predicate, 'related_to') IN $predicates
  )
  AND coalesce(r.confidence, 0.0) >= $hop2_min_confidence
RETURN
  s.entity_id AS src,
  o.entity_id AS dst,
  coalesce(r.predicate, 'related_to') AS predicate,
  coalesce(r.confidence, 0.0) AS confidence,
  coalesce(r.evidence_chunk_ids, []) AS evidence_chunk_ids,
  coalesce(r.assertion_id, '') AS assertion_id
ORDER BY confidence DESC
LIMIT $edge_cap
"""


@dataclass
class EdgeRow:
    src: str
    dst: str
    predicate: str
    confidence: float
    evidence_chunk_ids: list[str]
    assertion_id: str = ""


@dataclass
class TraversalBatchResult:
    batch_id: str
    paths: list[GraphPathResultV1] = field(default_factory=list)
    rejected_missing_child_support: int = 0
    neo4j_round_trips: int = 0
    edges_examined: int = 0
    unrestricted_bfs: bool = False
    execution_ms: float = 0.0
    diagnostics: dict[str, Any] = field(default_factory=dict)


def compile_traversal_plans(
    *,
    query_id: str,
    graph_subqueries: list[Any],
    seed_entity_ids: list[str],
    settings: Any | None = None,
) -> GraphTraversalBatchV1:
    """Compile graph_path subqueries into a batched traversal request."""

    if settings is None:
        from config import get_settings

        settings = get_settings()
    abs_hops = int(getattr(settings, "COMPLEX_QUERY_ABSOLUTE_MAX_HOPS", 3))
    default_hops = int(getattr(settings, "COMPLEX_QUERY_DEFAULT_MAX_HOPS", 2))
    beam = int(getattr(settings, "COMPLEX_QUERY_BEAM_WIDTH", 8))
    abs_graph = int(getattr(settings, "COMPLEX_QUERY_GRAPH_SUBQUERY_ABSOLUTE_MAX", 3))
    preferred = int(getattr(settings, "COMPLEX_QUERY_GRAPH_SUBQUERY_PREFERRED_MAX", 2))
    require_child = bool(getattr(settings, "COMPLEX_QUERY_REQUIRE_CHILD_SUPPORT", True))
    expansion = int(getattr(settings, "COMPLEX_QUERY_GLOBAL_EXPANSION_CAP", 100))
    path_cap = int(getattr(settings, "COMPLEX_QUERY_PATH_RESULT_CAP", 10))

    plans: list[GraphTraversalPlanV1] = []
    seeds = [str(s) for s in seed_entity_ids if str(s)][
        : int(getattr(settings, "COMPLEX_QUERY_SEED_ENTITY_CAP", 8))
    ]
    for sq in list(graph_subqueries)[:abs_graph]:
        hops = min(int(getattr(sq, "maximum_hops", default_hops) or default_hops), abs_hops)
        start = list(getattr(sq, "required_input_entity_ids", None) or []) or list(seeds)
        start = [str(x) for x in start if str(x)][:8]
        if not start:
            continue
        plans.append(
            GraphTraversalPlanV1(
                traversal_id=f"trav:{getattr(sq, 'subquery_id', 'sq')}",
                query_id=query_id,
                subquery_id=str(getattr(sq, "subquery_id", "")),
                obligation_id=str(getattr(sq, "obligation_id", "")),
                start_entity_ids=start,
                target_entity_ids=list(getattr(sq, "target_entity_ids", None) or []),
                allowed_predicate_ids=list(getattr(sq, "predicate_constraints", None) or []),
                allowed_predicate_families=list(
                    getattr(sq, "ontology_class_constraints", None) or []
                ),
                maximum_hops=hops,
                beam_width=int(getattr(sq, "beam_width", beam) or beam),
                expansion_cap_per_seed=int(getattr(sq, "expansion_cap", 25) or 25),
                global_expansion_cap=expansion,
                path_result_cap=min(int(getattr(sq, "path_cap", path_cap) or path_cap), path_cap),
                require_supporting_child=require_child,
                minimum_authority="extracted_assertion",
                allow_open_relations=False,
            ).with_hash()
        )
    # Prefer ≤preferred plans; keep absolute max.
    if len(plans) > preferred:
        plans = plans[:abs_graph]
    batch_id = f"gbatch:{query_id}:{len(plans)}"
    return GraphTraversalBatchV1(
        batch_id=batch_id,
        query_id=query_id,
        plans=plans,
        preferred_round_trips=1,
        maximum_round_trips=int(getattr(settings, "COMPLEX_QUERY_NEO4J_ROUND_TRIPS_MAX", 2)),
    )


def _path_id(nodes: list[str], predicates: list[str]) -> str:
    raw = "|".join(nodes) + "#" + "|".join(predicates)
    return "gpath:" + hashlib.sha256(raw.encode()).hexdigest()[:24]


def _score_path(
    *,
    predicates: list[str],
    confidences: list[float],
    child_count: int,
    hops: int,
) -> float:
    if child_count <= 0:
        return -1.0
    pred_bonus = 0.12 * len([p for p in predicates if p and p != "related_to"])
    conf = sum(confidences) / max(1, len(confidences))
    hop_penalty = 0.08 * max(0, hops - 1)
    return 0.55 * conf + pred_bonus + 0.05 * min(child_count, 4) - hop_penalty


async def _fetch_edges(
    session: Any,
    *,
    seed_ids: list[str],
    corpus_ids: list[str],
    predicates: list[str],
    min_confidence: float,
    edge_cap: int,
    hop2: bool = False,
    seen_ids: list[str] | None = None,
    hop2_min_confidence: float = 0.3,
) -> list[EdgeRow]:
    cypher = _BATCH_EDGE_FETCH_HOP2_CYPHER if hop2 else _BATCH_EDGE_FETCH_CYPHER
    params: dict[str, Any] = {
        "corpus_ids": corpus_ids,
        "predicates": predicates,
        "edge_cap": edge_cap,
    }
    if hop2:
        params.update(
            {
                "frontier_ids": seed_ids,
                "seen_ids": list(seen_ids or []),
                "hop2_min_confidence": hop2_min_confidence,
            }
        )
    else:
        params.update({"seed_ids": seed_ids, "min_confidence": min_confidence})
    result = await session.run(cypher, **params)
    rows: list[EdgeRow] = []
    async for rec in result:
        evid = [str(x) for x in (rec.get("evidence_chunk_ids") or []) if x]
        if not evid:
            continue
        rows.append(
            EdgeRow(
                src=str(rec["src"]),
                dst=str(rec["dst"]),
                predicate=str(rec["predicate"] or "related_to"),
                confidence=float(rec.get("confidence") or 0.0),
                evidence_chunk_ids=evid,
                assertion_id=str(rec.get("assertion_id") or ""),
            )
        )
    return rows


def _beam_paths_from_edges(
    *,
    plan: GraphTraversalPlanV1,
    edges: list[EdgeRow],
) -> tuple[list[GraphPathResultV1], int]:
    """Bounded beam expansion in Python over prefetched edges."""

    rejected = 0
    adj: dict[str, list[EdgeRow]] = {}
    for e in edges:
        if plan.allowed_predicate_ids and e.predicate not in plan.allowed_predicate_ids:
            continue
        adj.setdefault(e.src, []).append(e)
        # Undirected for RELATES_TO exploration
        adj.setdefault(e.dst, []).append(
            EdgeRow(
                src=e.dst,
                dst=e.src,
                predicate=e.predicate,
                confidence=e.confidence,
                evidence_chunk_ids=e.evidence_chunk_ids,
                assertion_id=e.assertion_id,
            )
        )

    # state: (node_ids, predicates, assertion_ids, child_ids, confidences)
    beam: list[tuple[list[str], list[str], list[str], list[str], list[float]]] = [
        ([sid], [], [], [], []) for sid in plan.start_entity_ids
    ]
    completed: list[GraphPathResultV1] = []
    targets = set(plan.target_entity_ids)

    for hop in range(max(1, plan.maximum_hops)):
        nxt: list[tuple[list[str], list[str], list[str], list[str], list[float]]] = []
        for nodes, preds, asserts, children, confs in beam:
            frontier = nodes[-1]
            candidates = sorted(
                adj.get(frontier, []),
                key=lambda e: e.confidence,
                reverse=True,
            )[: plan.expansion_cap_per_seed]
            for edge in candidates:
                if edge.dst in nodes:
                    continue
                if plan.require_supporting_child and not edge.evidence_chunk_ids:
                    rejected += 1
                    continue
                new_nodes = nodes + [edge.dst]
                new_preds = preds + [edge.predicate]
                new_asserts = asserts + ([edge.assertion_id] if edge.assertion_id else [])
                new_children = list(
                    dict.fromkeys([*children, *edge.evidence_chunk_ids])
                )
                new_confs = confs + [edge.confidence]
                state = (new_nodes, new_preds, new_asserts, new_children, new_confs)
                reached_target = not targets or edge.dst in targets
                at_max = hop + 1 >= plan.maximum_hops
                if reached_target or at_max:
                    score = _score_path(
                        predicates=new_preds,
                        confidences=new_confs,
                        child_count=len(new_children),
                        hops=len(new_preds),
                    )
                    if score < 0:
                        rejected += 1
                        continue
                    completed.append(
                        GraphPathResultV1(
                            path_id=_path_id(new_nodes, new_preds),
                            subquery_id=plan.subquery_id,
                            obligation_id=plan.obligation_id,
                            node_ids=new_nodes,
                            assertion_ids=new_asserts,
                            predicates=new_preds,
                            supporting_child_ids=new_children,
                            authority_levels=["extracted_assertion"],
                            path_score=round(score, 4),
                            result_class="explicit_path",
                        ).with_hash()
                    )
                else:
                    nxt.append(state)
        # Beam prune
        nxt.sort(
            key=lambda s: _score_path(
                predicates=s[1],
                confidences=s[4],
                child_count=len(s[3]),
                hops=len(s[1]),
            ),
            reverse=True,
        )
        beam = nxt[: plan.beam_width]
        if not beam:
            break

    completed.sort(key=lambda p: p.path_score, reverse=True)
    return completed[: plan.path_result_cap], rejected


async def execute_traversal_batch(
    *,
    neo4j_driver: Any,
    batch: GraphTraversalBatchV1,
    corpus_ids: list[str],
    settings: Any | None = None,
) -> TraversalBatchResult:
    if settings is None:
        from config import get_settings

        settings = get_settings()
    started = time.perf_counter()
    out = TraversalBatchResult(batch_id=batch.batch_id, unrestricted_bfs=False)
    if not batch.plans:
        out.diagnostics = {"reason": "no_plans"}
        return out
    if neo4j_driver is None:
        out.diagnostics = {"reason": "neo4j_unavailable"}
        return out

    all_seeds: list[str] = []
    preds: list[str] = []
    for p in batch.plans:
        for s in p.start_entity_ids:
            if s not in all_seeds:
                all_seeds.append(s)
        for pred in p.allowed_predicate_ids:
            if pred not in preds:
                preds.append(pred)
    max_hops = max(p.maximum_hops for p in batch.plans)
    edge_cap = int(getattr(settings, "COMPLEX_QUERY_GLOBAL_EXPANSION_CAP", 100))
    min_conf = float(getattr(settings, "GRAPH_REL_MIN_CONFIDENCE", 0.15) or 0.15)
    hop2_min = float(getattr(settings, "GRAPH_REL_HOP2_MIN_CONFIDENCE", 0.3) or 0.3)

    edges: list[EdgeRow] = []
    async with neo4j_driver.session() as session:
        # Round-trip 1: hop-1 edges for all seeds
        edges.extend(
            await _fetch_edges(
                session,
                seed_ids=all_seeds,
                corpus_ids=corpus_ids,
                predicates=preds,
                min_confidence=min_conf,
                edge_cap=edge_cap,
            )
        )
        out.neo4j_round_trips = 1
        # Round-trip 2 only if any plan needs hop>1
        if max_hops >= 2:
            frontier = sorted(
                {
                    e.dst
                    for e in edges
                    if e.dst not in all_seeds
                }
            )[: edge_cap]
            seen = list({*all_seeds, *[e.src for e in edges], *[e.dst for e in edges]})
            if frontier:
                edges.extend(
                    await _fetch_edges(
                        session,
                        seed_ids=frontier,
                        corpus_ids=corpus_ids,
                        predicates=preds,
                        min_confidence=hop2_min,
                        edge_cap=edge_cap,
                        hop2=True,
                        seen_ids=seen,
                        hop2_min_confidence=hop2_min,
                    )
                )
                out.neo4j_round_trips = 2

    out.edges_examined = len(edges)
    # Deduplicate edges
    uniq: dict[tuple[str, str, str], EdgeRow] = {}
    for e in edges:
        key = (e.src, e.dst, e.predicate)
        prev = uniq.get(key)
        if prev is None or e.confidence > prev.confidence:
            uniq[key] = e
    edges = list(uniq.values())

    paths: list[GraphPathResultV1] = []
    rejected = 0
    for plan in batch.plans:
        plan_paths, plan_rej = _beam_paths_from_edges(plan=plan, edges=edges)
        paths.extend(plan_paths)
        rejected += plan_rej

    # Hard reject any path missing child support
    kept: list[GraphPathResultV1] = []
    for p in paths:
        if not p.supporting_child_ids:
            rejected += 1
            continue
        kept.append(p)
    out.paths = kept
    out.rejected_missing_child_support = rejected
    out.execution_ms = round((time.perf_counter() - started) * 1000.0, 2)
    out.diagnostics = {
        "graph_subqueries": len(batch.plans),
        "graph_subqueries_preferred_max": int(
            getattr(settings, "COMPLEX_QUERY_GRAPH_SUBQUERY_PREFERRED_MAX", 2)
        ),
        "graph_subqueries_absolute_max": int(
            getattr(settings, "COMPLEX_QUERY_GRAPH_SUBQUERY_ABSOLUTE_MAX", 3)
        ),
        "neo4j_round_trips": out.neo4j_round_trips,
        "neo4j_round_trips_max": batch.maximum_round_trips,
        "unrestricted_bfs": False,
        "every_returned_assertion_has_child_support": all(
            bool(p.supporting_child_ids) for p in out.paths
        ),
        "paths_selected": len(out.paths),
        "llm_cypher": False,
        "traversal_plan_hashes": [p.plan_hash for p in batch.plans if p.plan_hash],
    }
    if out.neo4j_round_trips > batch.maximum_round_trips:
        out.diagnostics["round_trip_violation"] = True
    return out
