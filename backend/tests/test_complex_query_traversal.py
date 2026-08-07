"""Phase 5–6 traversal compiler + beam path tests (no Neo4j)."""

from __future__ import annotations

from types import SimpleNamespace

from models.complex_query import GraphTraversalPlanV1, SubQueryPlanV1
from services.retriever.complex_query_traversal import (
    EdgeRow,
    _beam_paths_from_edges,
    compile_traversal_plans,
)


def test_compile_respects_graph_subquery_cap():
    settings = SimpleNamespace(
        COMPLEX_QUERY_ABSOLUTE_MAX_HOPS=3,
        COMPLEX_QUERY_DEFAULT_MAX_HOPS=2,
        COMPLEX_QUERY_BEAM_WIDTH=8,
        COMPLEX_QUERY_GRAPH_SUBQUERY_ABSOLUTE_MAX=3,
        COMPLEX_QUERY_GRAPH_SUBQUERY_PREFERRED_MAX=2,
        COMPLEX_QUERY_REQUIRE_CHILD_SUPPORT=True,
        COMPLEX_QUERY_GLOBAL_EXPANSION_CAP=100,
        COMPLEX_QUERY_PATH_RESULT_CAP=10,
        COMPLEX_QUERY_SEED_ENTITY_CAP=8,
        COMPLEX_QUERY_NEO4J_ROUND_TRIPS_MAX=2,
    )
    sqs = [
        SubQueryPlanV1(
            subquery_id=f"sq_graph_{i}",
            query_id="q1",
            obligation_id=f"O{i}",
            query_type="graph_path",
            query_text="path",
            maximum_hops=2,
        )
        for i in range(5)
    ]
    batch = compile_traversal_plans(
        query_id="q1",
        graph_subqueries=sqs,
        seed_entity_ids=["e1", "e2"],
        settings=settings,
    )
    assert len(batch.plans) <= 3
    assert all(p.require_supporting_child for p in batch.plans)
    assert all(p.maximum_hops <= 3 for p in batch.plans)


def test_beam_rejects_edges_without_child_support():
    plan = GraphTraversalPlanV1(
        traversal_id="t1",
        query_id="q1",
        subquery_id="sq_graph_O2",
        obligation_id="O2",
        start_entity_ids=["e1"],
        target_entity_ids=["e3"],
        allowed_predicate_ids=["uses", "depends_on"],
        maximum_hops=2,
        beam_width=4,
        require_supporting_child=True,
        path_result_cap=5,
    )
    edges = [
        EdgeRow("e1", "e2", "uses", 0.9, ["c1"], "a1"),
        EdgeRow("e2", "e3", "depends_on", 0.8, ["c2"], "a2"),
        EdgeRow("e1", "e9", "uses", 0.95, [], "a3"),  # no child support
    ]
    paths, rejected = _beam_paths_from_edges(plan=plan, edges=edges)
    assert rejected >= 1
    assert paths
    assert all(p.supporting_child_ids for p in paths)
    assert any(p.node_ids[-1] == "e3" for p in paths)


def test_unrestricted_bfs_flag_false_in_compiler_defaults():
    settings = SimpleNamespace(
        COMPLEX_QUERY_ABSOLUTE_MAX_HOPS=3,
        COMPLEX_QUERY_DEFAULT_MAX_HOPS=2,
        COMPLEX_QUERY_BEAM_WIDTH=8,
        COMPLEX_QUERY_GRAPH_SUBQUERY_ABSOLUTE_MAX=3,
        COMPLEX_QUERY_GRAPH_SUBQUERY_PREFERRED_MAX=2,
        COMPLEX_QUERY_REQUIRE_CHILD_SUPPORT=True,
        COMPLEX_QUERY_GLOBAL_EXPANSION_CAP=100,
        COMPLEX_QUERY_PATH_RESULT_CAP=10,
        COMPLEX_QUERY_SEED_ENTITY_CAP=8,
        COMPLEX_QUERY_NEO4J_ROUND_TRIPS_MAX=2,
    )
    batch = compile_traversal_plans(
        query_id="q1",
        graph_subqueries=[
            SubQueryPlanV1(
                subquery_id="sq_graph_O1",
                query_id="q1",
                obligation_id="O1",
                query_type="graph_path",
                query_text="x",
            )
        ],
        seed_entity_ids=["e1"],
        settings=settings,
    )
    assert batch.maximum_round_trips <= 2
