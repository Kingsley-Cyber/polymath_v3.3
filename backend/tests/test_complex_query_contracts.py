"""Phase 1 complex-query contracts — pure, no live stack."""

from __future__ import annotations

from models.complex_query import (
    AnswerVerificationV1,
    ContextPacketV1,
    CrossDomainBridgeV1,
    GraphPathResultV1,
    GraphTraversalPlanV1,
    RootQueryIR,
    SubQueryPlanV1,
    TypedObligationV1,
    compile_graph_level,
    dag_is_acyclic,
    graph_subquery_ceiling,
    hop_ceiling,
)


def test_root_query_preserves_original_and_hashes():
    root = RootQueryIR(
        query_id="q1",
        original_query="How should a C++ combat update loop map to Luau?",
        standalone_query="How should a C++ combat update loop map to Luau?",
        intent_class="cross_domain_translation",
        selected_corpus_ids=["gsem-e2e-20260804a"],
        answer_obligations=["O1", "O2", "O3", "O4", "O5"],
    ).with_hash()
    again = root.model_copy(update={"plan_hash": ""}).with_hash()
    assert root.plan_hash == again.plan_hash
    assert root.plan_hash.startswith("sha256:")


def test_dag_acyclic_and_rejects_cycle():
    a = SubQueryPlanV1(
        subquery_id="sq1",
        query_id="q1",
        obligation_id="O1",
        parent_subquery_ids=[],
        query_type="direct_evidence",
        query_text="update loop",
        wave=1,
    ).with_hash()
    b = SubQueryPlanV1(
        subquery_id="sq2",
        query_id="q1",
        obligation_id="O2",
        parent_subquery_ids=["sq1"],
        query_type="graph_path",
        query_text="depends on",
        wave=2,
        maximum_hops=2,
    ).with_hash()
    assert dag_is_acyclic([a, b])
    cycle = b.model_copy(update={"parent_subquery_ids": ["sq1"]})
    a_cycle = a.model_copy(update={"parent_subquery_ids": ["sq2"]})
    assert not dag_is_acyclic([a_cycle, cycle])


def test_graph_level_ceilings():
    assert compile_graph_level("direct_lookup") == "graph_lite"
    assert hop_ceiling("graph_lite") == 1
    assert hop_ceiling("graph_deep") == 3
    assert graph_subquery_ceiling("graph_standard") == 2
    assert compile_graph_level("cross_domain_translation") == "graph_deep"


def test_path_rejects_missing_child_support_field_default():
    plan = GraphTraversalPlanV1(
        traversal_id="t1",
        query_id="q1",
        subquery_id="sq2",
        obligation_id="O2",
        start_entity_ids=["e1"],
        require_supporting_child=True,
        maximum_hops=2,
    ).with_hash()
    assert plan.require_supporting_child is True
    path = GraphPathResultV1(
        path_id="p1",
        node_ids=["e1", "e2"],
        assertion_ids=["a1"],
        predicates=["uses"],
        supporting_child_ids=["c1"],
        result_class="explicit_path",
    ).with_hash()
    assert path.supporting_child_ids
    assert path.result_hash.startswith("sha256:")


def test_bridge_identity_merge_forbidden():
    bridge = CrossDomainBridgeV1(
        bridge_id="b1",
        source_domain="cpp",
        target_domain="luau",
        source_child_ids=["c1"],
        target_child_ids=["c2"],
        identity_merge_allowed=False,
    ).with_hash()
    assert bridge.identity_merge_allowed is False


def test_context_packet_and_verification_defaults():
    packet = ContextPacketV1(
        conversation_context={"original_query": "x"},
        schema_records_as_citations=0,
        summaries_as_detailed_claim_citations=0,
    ).with_hash()
    assert packet.schema_records_as_citations == 0
    ver = AnswerVerificationV1(verification_status="not_run").with_hash()
    assert ver.verification_status == "not_run"


def test_typed_obligation_status_values():
    ob = TypedObligationV1(
        obligation_id="O1",
        type="source_behavior",
        domain="cpp",
        requirement="explain update-loop behavior",
        status="partial",
    )
    assert ob.status == "partial"
