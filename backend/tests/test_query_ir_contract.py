"""Contract tests for models/query_ir.py (Slice 1 scaffolding).

Validates the Query IR contract WITHOUT touching runtime behavior: the IR
extends QueryPlanV2 fields by value, preserves the user's original query,
rejects degenerate plans, and hashes deterministically (replayability).
"""

from dataclasses import dataclass

import pytest

from models.query_ir import (
    QUERY_IR_SCHEMA_VERSION,
    BridgeObligation,
    EvidenceObligation,
    OntologyRef,
    QueryIR,
    QueryIRLane,
    RequiredDomain,
    RoutePolicy,
    TemporalCompilation,
)


@dataclass(frozen=True)
class FakeLane:
    lane_id: str
    role: str = "core"
    query: str = "what is facs"
    required: bool = True


@dataclass(frozen=True)
class FakePlan:
    """Duck-typed QueryPlanV2 stand-in (contract tests must not import services)."""

    version: str = "query_plan.v2"
    original_query: str = "How does FACS score facial movement?"
    standalone_query: str = "How does FACS score facial movement?"
    complexity: str = "simple"
    concepts: tuple = ("facs",)
    operators: tuple = ()
    lanes: tuple = (FakeLane("lane_1"),)
    answer_shape: str = "single_fact"
    corpus_ids: tuple = ("c1",)


def test_from_plan_mirrors_queryplanv2_fields():
    ir = QueryIR.from_plan(FakePlan())
    assert ir.schema_version == QUERY_IR_SCHEMA_VERSION
    assert ir.plan_version == "query_plan.v2"
    assert ir.original_query == "How does FACS score facial movement?"
    assert ir.complexity == "simple"
    assert ir.concepts == ("facs",)
    assert ir.corpus_ids == ("c1",)
    assert [(lane.lane_id, lane.required) for lane in ir.lanes] == [
        ("lane_1", True)
    ]


def test_required_lanes_become_evidence_obligations():
    ir = QueryIR.from_plan(FakePlan())
    assert [obligation.obligation_id for obligation in ir.obligations] == ["lane_1"]
    assert ir.obligations[0].required is True


def test_original_query_must_be_preserved_verbatim():
    with pytest.raises(ValueError):
        QueryIR(
            plan_version="query_plan.v2",
            original_query="   ",
            standalone_query="q",
            complexity="simple",
        )


def test_lane_ids_must_be_unique():
    with pytest.raises(ValueError):
        QueryIR(
            plan_version="query_plan.v2",
            original_query="q",
            standalone_query="q",
            complexity="simple",
            lanes=(
                QueryIRLane(lane_id="dup", role="core", query="a"),
                QueryIRLane(lane_id="dup", role="core", query="b"),
            ),
        )


def test_ir_hash_is_deterministic_and_field_sensitive():
    plan = FakePlan()
    first = QueryIR.from_plan(plan).ir_hash()
    second = QueryIR.from_plan(plan).ir_hash()
    assert first == second
    changed = QueryIR.from_plan(
        FakePlan(original_query="different query", standalone_query="different query")
    )
    assert changed.ir_hash() != first


def test_ontology_refs_are_typed_and_advisory_only_by_contract():
    ref = OntologyRef(
        ref_kind="predicate",
        canonical_key="located_in",
        surface="based in",
        applicability="source_term_overlap",
    )
    ir = QueryIR.from_plan(FakePlan(), ontology_refs=(ref,))
    assert ir.ontology_refs[0].canonical_key == "located_in"
    # Contract shape: advisory refs carry applicability tier, never a score.
    assert "score" not in type(ir.ontology_refs[0]).model_fields


def test_temporal_compilation_defaults_to_explicit_none():
    ir = QueryIR.from_plan(FakePlan())
    assert ir.temporal.active is False
    assert ir.temporal.interpretation == "none"
    active = QueryIR.from_plan(
        FakePlan(),
        temporal=TemporalCompilation(active=True, interpretation="absolute"),
    )
    assert active.temporal.interpretation == "absolute"


def test_route_policy_defaults_curated_and_validates_literal():
    ir = QueryIR.from_plan(FakePlan())
    assert ir.route_policy.route == "CURATED"
    with pytest.raises(ValueError):
        RoutePolicy(route="SLOW")  # type: ignore[arg-type]


def test_round_trip_serialization_is_stable():
    ir = QueryIR.from_plan(
        FakePlan(),
        obligations=(
            EvidenceObligation(obligation_id="o1", lane_ids=("lane_1",)),
        ),
    )
    restored = QueryIR.model_validate_json(ir.model_dump_json())
    assert restored == ir
    assert restored.ir_hash() == ir.ir_hash()


def test_cross_domain_fields_default_empty_and_accept_domains():
    ir = QueryIR.from_plan(FakePlan())
    assert ir.selected_corpus_ids == ("c1",)
    assert ir.required_domains == ()
    assert ir.bridge_obligations == ()
    assert ir.evidence_token_budget == 7000
    filled = QueryIR.from_plan(
        FakePlan(),
        required_domains=(
            RequiredDomain(domain_id="cpp_source", obligation="Explain source behavior."),
        ),
        bridge_obligations=(
            BridgeObligation(
                bridge_id="b1",
                source_domain="cpp_source",
                target_domain="luau_runtime",
                description="Map game loop to Heartbeat",
            ),
        ),
        requested_retrieval_mode="hybrid",
    )
    assert filled.required_domains[0].domain_id == "cpp_source"
    assert filled.bridge_obligations[0].bridge_id == "b1"
    assert filled.requested_retrieval_mode == "hybrid"
