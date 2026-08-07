"""Complex-query / multi-hop contracts — extend QueryIR, do not replace it.

Phase 1 contracts for the Complex Query and Multi-Hop Graph RAG plan.
Dark by default: no production activation; fixture/eval callers only until
the subquery executor is wired behind an allowlist flag.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

COMPLEX_QUERY_PLAN_RELEASE = "polymath.complex_query.v1"
SUBQUERY_PLAN_VERSION = "subquery_plan.v1"
GRAPH_TRAVERSAL_PLAN_VERSION = "graph_traversal_plan.v1"
CONTEXT_PACKET_VERSION = "context_packet.v1"
ANSWER_VERIFICATION_VERSION = "answer_verification.v1"

IntentClass = Literal[
    "direct_lookup",
    "comparison",
    "cross_document_synthesis",
    "cross_domain_translation",
    "dependency_analysis",
    "causal_explanation",
    "temporal_latest_state",
    "contradiction_analysis",
    "implementation_design",
    "multi_hop_relationship",
    "artifact_generation",
    "unknown",
]

SubQueryType = Literal[
    "direct_evidence",
    "vocabulary_resolution",
    "canonical_expansion",
    "lexical_exact",
    "summary_routing",
    "entity_resolution",
    "graph_path",
    "temporal",
    "contradiction",
    "cross_domain_bridge",
    "verification",
]

ObligationStatus = Literal["satisfied", "partial", "unsupported", "blocked"]
GraphLevel = Literal["graph_lite", "graph_standard", "graph_deep"]
PathResultClass = Literal[
    "explicit_path",
    "deterministic_inference",
    "speculative_connection",
]
MinimumAuthority = Literal["extracted_assertion", "qualified_fact"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())


def _hash_payload(payload: dict[str, Any]) -> str:
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return f"sha256:{digest}"


class RootQueryIR(StrictModel):
    """Conversation-aware root request compiled once per turn."""

    schema_version: Literal["root_query_ir.v1"] = "root_query_ir.v1"
    query_id: str = Field(min_length=1)
    original_query: str = Field(min_length=1)
    standalone_query: str = Field(min_length=1)
    current_request: str = ""
    conversation_constraints: list[str] = Field(default_factory=list)
    selected_corpus_ids: list[str] = Field(default_factory=list)
    requested_mode: str = ""
    intent_class: IntentClass = "unknown"
    required_domains: list[str] = Field(default_factory=list)
    answer_obligations: list[str] = Field(default_factory=list)
    temporal_requirements: dict[str, Any] = Field(default_factory=dict)
    authority_requirements: dict[str, Any] = Field(default_factory=dict)
    output_contract: str = "grounded_answer"
    evidence_token_budget: int = 7000
    graph_level: GraphLevel = "graph_lite"
    plan_release: str = COMPLEX_QUERY_PLAN_RELEASE
    plan_hash: str = ""
    # Link to existing QueryIR identity when compiled from it.
    query_ir_hash: str = ""

    @field_validator("original_query")
    @classmethod
    def _preserve_user_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("original_query must preserve the user's message")
        return value

    def with_hash(self) -> "RootQueryIR":
        payload = self.model_dump(exclude={"plan_hash"})
        return self.model_copy(update={"plan_hash": _hash_payload(payload)})


class TypedObligationV1(StrictModel):
    """Independently testable evidence obligation."""

    obligation_id: str = Field(min_length=1)
    type: str = Field(min_length=1)
    domain: str = ""
    domains: list[str] = Field(default_factory=list)
    requirement: str = Field(min_length=1)
    required: bool = True
    status: ObligationStatus | None = None


class SubQueryPlanV1(StrictModel):
    schema_version: Literal["subquery_plan.v1"] = SUBQUERY_PLAN_VERSION
    subquery_id: str = Field(min_length=1)
    query_id: str = Field(min_length=1)
    obligation_id: str = Field(min_length=1)
    parent_subquery_ids: list[str] = Field(default_factory=list)
    query_type: SubQueryType
    query_text: str = Field(min_length=1)
    domains: list[str] = Field(default_factory=list)
    corpus_ids: list[str] = Field(default_factory=list)
    required_input_entity_ids: list[str] = Field(default_factory=list)
    target_entity_ids: list[str] = Field(default_factory=list)
    predicate_constraints: list[str] = Field(default_factory=list)
    ontology_class_constraints: list[str] = Field(default_factory=list)
    minimum_authority: MinimumAuthority = "extracted_assertion"
    requested_route: str = ""
    candidate_budget: int = 24
    evidence_token_budget: int = 1200
    maximum_hops: int = 2
    beam_width: int = 8
    expansion_cap: int = 25
    path_cap: int = 10
    expected_output: str = ""
    stop_conditions: list[str] = Field(default_factory=list)
    wave: Literal[1, 2, 3, 4] = 1
    plan_release: str = COMPLEX_QUERY_PLAN_RELEASE
    plan_hash: str = ""

    def with_hash(self) -> "SubQueryPlanV1":
        payload = self.model_dump(exclude={"plan_hash"})
        return self.model_copy(update={"plan_hash": _hash_payload(payload)})


class GraphTraversalPlanV1(StrictModel):
    schema_version: Literal["graph_traversal_plan.v1"] = GRAPH_TRAVERSAL_PLAN_VERSION
    traversal_id: str = Field(min_length=1)
    query_id: str = Field(min_length=1)
    subquery_id: str = Field(min_length=1)
    obligation_id: str = Field(min_length=1)
    start_entity_ids: list[str] = Field(default_factory=list)
    target_entity_ids: list[str] = Field(default_factory=list)
    allowed_predicate_ids: list[str] = Field(default_factory=list)
    allowed_predicate_families: list[str] = Field(default_factory=list)
    allowed_ontology_classes: list[str] = Field(default_factory=list)
    minimum_authority: MinimumAuthority = "extracted_assertion"
    allow_open_relations: bool = False
    allow_shadow_authority: bool = False
    maximum_hops: int = 2
    beam_width: int = 8
    expansion_cap_per_seed: int = 25
    global_expansion_cap: int = 100
    path_result_cap: int = 10
    assertion_result_cap: int = 20
    temporal_constraints: dict[str, Any] = Field(default_factory=dict)
    polarity_constraints: dict[str, Any] = Field(default_factory=dict)
    modality_constraints: dict[str, Any] = Field(default_factory=dict)
    require_supporting_child: bool = True
    ontology_release: str = ""
    graph_release: str = ""
    capability_certificate: str = ""
    plan_hash: str = ""

    def with_hash(self) -> "GraphTraversalPlanV1":
        payload = self.model_dump(exclude={"plan_hash"})
        return self.model_copy(update={"plan_hash": _hash_payload(payload)})


class GraphPathResultV1(StrictModel):
    path_id: str = Field(min_length=1)
    subquery_id: str = ""
    obligation_id: str = ""
    node_ids: list[str] = Field(default_factory=list)
    assertion_ids: list[str] = Field(default_factory=list)
    predicates: list[str] = Field(default_factory=list)
    supporting_child_ids: list[str] = Field(default_factory=list)
    supporting_evidence_spans: list[str] = Field(default_factory=list)
    ontology_release: str = ""
    authority_levels: list[str] = Field(default_factory=list)
    temporal_status: str = ""
    polarity_status: str = ""
    modality_status: str = ""
    path_score: float = 0.0
    result_class: PathResultClass = "explicit_path"
    inference_rule_id: str = ""
    result_hash: str = ""

    def with_hash(self) -> "GraphPathResultV1":
        payload = self.model_dump(exclude={"result_hash"})
        return self.model_copy(update={"result_hash": _hash_payload(payload)})


class CrossDomainBridgeV1(StrictModel):
    bridge_id: str = Field(min_length=1)
    source_domain: str = ""
    source_concept: str = ""
    source_entity_ids: list[str] = Field(default_factory=list)
    source_child_ids: list[str] = Field(default_factory=list)
    target_domain: str = ""
    target_concept: str = ""
    target_entity_ids: list[str] = Field(default_factory=list)
    target_child_ids: list[str] = Field(default_factory=list)
    bridge_concept: str = ""
    mapping_type: str = "implementation_bridge"
    support_status: ObligationStatus = "partial"
    confidence: float = 0.0
    identity_merge_allowed: bool = False
    result_hash: str = ""

    def with_hash(self) -> "CrossDomainBridgeV1":
        payload = self.model_dump(exclude={"result_hash"})
        return self.model_copy(update={"result_hash": _hash_payload(payload)})


class ContradictionBundleV1(StrictModel):
    contradiction_id: str = Field(min_length=1)
    subject_entity_id: str = ""
    predicate_id: str = ""
    supporting_assertion_ids: list[str] = Field(default_factory=list)
    opposing_assertion_ids: list[str] = Field(default_factory=list)
    supporting_child_ids: list[str] = Field(default_factory=list)
    opposing_child_ids: list[str] = Field(default_factory=list)
    temporal_relationship: str = ""
    authority_comparison: str = ""
    resolution_status: str = "unresolved"
    result_hash: str = ""

    def with_hash(self) -> "ContradictionBundleV1":
        payload = self.model_dump(exclude={"result_hash"})
        return self.model_copy(update={"result_hash": _hash_payload(payload)})


class SubQueryResultV1(StrictModel):
    subquery_id: str = Field(min_length=1)
    obligation_id: str = Field(min_length=1)
    status: ObligationStatus = "unsupported"
    resolved_concepts: list[str] = Field(default_factory=list)
    resolved_entity_ids: list[str] = Field(default_factory=list)
    direct_child_ids: list[str] = Field(default_factory=list)
    vocabulary_child_ids: list[str] = Field(default_factory=list)
    lexical_child_ids: list[str] = Field(default_factory=list)
    summary_guided_child_ids: list[str] = Field(default_factory=list)
    graph_child_ids: list[str] = Field(default_factory=list)
    graph_paths: list[GraphPathResultV1] = Field(default_factory=list)
    bridges: list[CrossDomainBridgeV1] = Field(default_factory=list)
    contradictions: list[ContradictionBundleV1] = Field(default_factory=list)
    coverage: dict[str, Any] = Field(default_factory=dict)
    authority_level: str = ""
    unresolved_items: list[str] = Field(default_factory=list)
    execution_ms: float = 0.0
    result_hash: str = ""

    def with_hash(self) -> "SubQueryResultV1":
        payload = self.model_dump(exclude={"result_hash"})
        return self.model_copy(update={"result_hash": _hash_payload(payload)})


class ContextPacketV1(StrictModel):
    schema_version: Literal["context_packet.v1"] = CONTEXT_PACKET_VERSION
    conversation_context: dict[str, Any] = Field(default_factory=dict)
    root_query: dict[str, Any] = Field(default_factory=dict)
    query_plan: dict[str, Any] = Field(default_factory=dict)
    subquery_plan: list[dict[str, Any]] = Field(default_factory=list)
    vocabulary_resolution: dict[str, Any] = Field(default_factory=dict)
    ontology_resolution: dict[str, Any] = Field(default_factory=dict)
    graph_capability: dict[str, Any] = Field(default_factory=dict)
    obligation_results: list[dict[str, Any]] = Field(default_factory=list)
    routing_summaries: list[dict[str, Any]] = Field(default_factory=list)
    protected_child_evidence: list[dict[str, Any]] = Field(default_factory=list)
    mmr_selected_child_evidence: list[dict[str, Any]] = Field(default_factory=list)
    graph_paths: list[dict[str, Any]] = Field(default_factory=list)
    deterministic_inferences: list[dict[str, Any]] = Field(default_factory=list)
    cross_domain_bridges: list[dict[str, Any]] = Field(default_factory=list)
    contradictions: list[dict[str, Any]] = Field(default_factory=list)
    coverage: dict[str, Any] = Field(default_factory=dict)
    unresolved_obligations: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    context_hash: str = ""
    # Authority flags (frozen)
    schema_records_as_citations: int = 0
    summaries_as_detailed_claim_citations: int = 0

    def with_hash(self) -> "ContextPacketV1":
        payload = self.model_dump(exclude={"context_hash"})
        # Timing noise must not break restart replay.
        gc = dict(payload.get("graph_capability") or {})
        for volatile in ("execution_ms", "timings", "wall_ms"):
            gc.pop(volatile, None)
        payload["graph_capability"] = gc
        return self.model_copy(update={"context_hash": _hash_payload(payload)})


class AnswerVerificationV1(StrictModel):
    schema_version: Literal["answer_verification.v1"] = ANSWER_VERIFICATION_VERSION
    claims_total: int = 0
    claims_supported: int = 0
    claims_unsupported: int = 0
    obligations_covered: int = 0
    obligations_partial: int = 0
    obligations_unsupported: int = 0
    graph_paths_used: int = 0
    graph_paths_verified: int = 0
    bridge_claims: int = 0
    bridge_claims_verified: int = 0
    contradiction_disclosures: int = 0
    citation_coverage: float = 0.0
    verification_status: Literal[
        "pass", "revise", "block", "partial", "not_run"
    ] = "not_run"
    result_hash: str = ""

    def with_hash(self) -> "AnswerVerificationV1":
        payload = self.model_dump(exclude={"result_hash"})
        return self.model_copy(update={"result_hash": _hash_payload(payload)})


class GraphTraversalBatchV1(StrictModel):
    """Batch Neo4j request — preferred one round-trip for multiple plans."""

    batch_id: str = Field(min_length=1)
    query_id: str = Field(min_length=1)
    plans: list[GraphTraversalPlanV1] = Field(default_factory=list)
    preferred_round_trips: int = 1
    maximum_round_trips: int = 2


def dag_is_acyclic(plans: list[SubQueryPlanV1]) -> bool:
    """Return True when parent_subquery_ids form a DAG."""

    ids = {p.subquery_id for p in plans}
    edges = {
        p.subquery_id: [x for x in p.parent_subquery_ids if x in ids] for p in plans
    }
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(node: str) -> bool:
        if node in done:
            return True
        if node in visiting:
            return False
        visiting.add(node)
        for parent in edges.get(node, []):
            if not visit(parent):
                return False
        visiting.remove(node)
        done.add(node)
        return True

    return all(visit(n) for n in ids)


def compile_graph_level(intent: IntentClass, *, complex_flag: bool = False) -> GraphLevel:
    if intent in {"direct_lookup"} and not complex_flag:
        return "graph_lite"
    if intent in {
        "comparison",
        "dependency_analysis",
        "multi_hop_relationship",
        "temporal_latest_state",
    }:
        return "graph_standard"
    if intent in {
        "cross_domain_translation",
        "contradiction_analysis",
        "causal_explanation",
        "implementation_design",
        "artifact_generation",
        "cross_document_synthesis",
    }:
        return "graph_deep"
    return "graph_standard" if complex_flag else "graph_lite"


def hop_ceiling(level: GraphLevel) -> int:
    return {"graph_lite": 1, "graph_standard": 2, "graph_deep": 3}[level]


def graph_subquery_ceiling(level: GraphLevel) -> int:
    return {"graph_lite": 1, "graph_standard": 2, "graph_deep": 3}[level]
