"""Deterministic complex-query decomposition templates (Phase 2).

No LLM required. Produces a bounded SubQueryPlanV1 DAG from RootQueryIR +
TypedObligationV1. Caps enforced; DAG must be acyclic.
"""

from __future__ import annotations

import re
from typing import Iterable

from models.complex_query import (
    COMPLEX_QUERY_PLAN_RELEASE,
    RootQueryIR,
    SubQueryPlanV1,
    TypedObligationV1,
    compile_graph_level,
    dag_is_acyclic,
    graph_subquery_ceiling,
    hop_ceiling,
)

_INTENT_HINTS: list[tuple[str, str]] = [
    ("contradict|disagree|conflict|versus vs ", "contradiction_analysis"),
    ("compare|comparison|differ|difference|vs\\.?|versus", "comparison"),
    ("translat|map .+ to|port to|luau|roblox", "cross_domain_translation"),
    ("depend|requires|prerequisite|relies on", "dependency_analysis"),
    ("cause|causal|leads to|because|therefore", "causal_explanation"),
    ("latest|current|as of|now|superseded|formerly", "temporal_latest_state"),
    ("implement|design|how should|architecture", "implementation_design"),
    ("multi-?hop|relationship between|connected to|path from", "multi_hop_relationship"),
    ("artifact|html report|generate a", "artifact_generation"),
    ("across (documents|papers|sources)|synthesize", "cross_document_synthesis"),
]


def detect_intent_class(text: str) -> str:
    lowered = (text or "").lower()
    for pattern, intent in _INTENT_HINTS:
        if re.search(pattern, lowered):
            return intent
    return "direct_lookup"


def default_obligations_for_intent(
    intent: str, *, query_text: str
) -> list[TypedObligationV1]:
    """Compile a small obligation set for the intent (deterministic)."""

    q = (query_text or "").strip()
    if intent == "cross_domain_translation":
        return [
            TypedObligationV1(
                obligation_id="O1",
                type="source_behavior",
                domain="source",
                requirement=f"explain source-domain behavior for: {q}",
            ),
            TypedObligationV1(
                obligation_id="O2",
                type="mechanic_preservation",
                domain="mechanics",
                requirement="identify mechanics/states that must be preserved",
            ),
            TypedObligationV1(
                obligation_id="O3",
                type="target_capability",
                domain="target",
                requirement="identify valid target-runtime mechanisms",
            ),
            TypedObligationV1(
                obligation_id="O4",
                type="cross_domain_bridge",
                domains=["source", "target"],
                requirement="map source semantics to target constructs with two-sided evidence",
            ),
            TypedObligationV1(
                obligation_id="O5",
                type="limitation",
                requirement="identify timing, networking, or lifecycle losses",
            ),
        ]
    if intent == "comparison":
        return [
            TypedObligationV1(
                obligation_id="O1",
                type="side_a",
                requirement=f"evidence for first compared subject in: {q}",
            ),
            TypedObligationV1(
                obligation_id="O2",
                type="side_b",
                requirement=f"evidence for second compared subject in: {q}",
            ),
            TypedObligationV1(
                obligation_id="O3",
                type="contrast",
                requirement="evidence that supports a grounded contrast",
            ),
        ]
    if intent == "contradiction_analysis":
        return [
            TypedObligationV1(
                obligation_id="O1",
                type="claim_support",
                requirement=f"supporting assertions for: {q}",
            ),
            TypedObligationV1(
                obligation_id="O2",
                type="claim_oppose",
                requirement=f"opposing assertions for: {q}",
            ),
            TypedObligationV1(
                obligation_id="O3",
                type="resolution_context",
                requirement="temporal/authority context for the contradiction",
            ),
        ]
    if intent in {"dependency_analysis", "multi_hop_relationship", "causal_explanation"}:
        return [
            TypedObligationV1(
                obligation_id="O1",
                type="seed_entities",
                requirement=f"resolve entities for: {q}",
            ),
            TypedObligationV1(
                obligation_id="O2",
                type="graph_path",
                requirement="retrieve evidence-backed relationship paths",
            ),
            TypedObligationV1(
                obligation_id="O3",
                type="direct_evidence",
                requirement="direct child evidence for the relationship claim",
            ),
        ]
    if intent == "temporal_latest_state":
        return [
            TypedObligationV1(
                obligation_id="O1",
                type="latest_state",
                requirement=f"latest authoritative state for: {q}",
            ),
            TypedObligationV1(
                obligation_id="O2",
                type="supersession",
                requirement="identify superseded/retracted claims to exclude",
            ),
        ]
    # direct / implementation / synthesis fallback
    return [
        TypedObligationV1(
            obligation_id="O1",
            type="direct_evidence",
            requirement=f"direct evidence for: {q}",
        ),
        TypedObligationV1(
            obligation_id="O2",
            type="vocabulary_resolution",
            requirement="resolve corpus-native vocabulary for the query",
        ),
    ]


def build_root_query_ir(
    *,
    original_query: str,
    standalone_query: str | None = None,
    corpus_ids: Iterable[str] | None = None,
    conversation_constraints: Iterable[str] | None = None,
    requested_mode: str = "qdrant_mongo_graph",
    query_id: str = "root",
    query_ir_hash: str = "",
) -> RootQueryIR:
    standalone = (standalone_query if standalone_query is not None else original_query).strip()
    intent = detect_intent_class(standalone)
    obligations = default_obligations_for_intent(intent, query_text=standalone)
    level = compile_graph_level(intent)  # type: ignore[arg-type]
    root = RootQueryIR(
        query_id=query_id,
        original_query=original_query,
        standalone_query=standalone,
        current_request=standalone,
        conversation_constraints=list(conversation_constraints or []),
        selected_corpus_ids=[str(c) for c in (corpus_ids or []) if str(c).strip()],
        requested_mode=requested_mode,
        intent_class=intent,  # type: ignore[arg-type]
        answer_obligations=[o.obligation_id for o in obligations],
        required_domains=sorted(
            {
                *(o.domain for o in obligations if o.domain),
                *(d for o in obligations for d in o.domains),
            }
        ),
        graph_level=level,
        query_ir_hash=query_ir_hash,
        plan_release=COMPLEX_QUERY_PLAN_RELEASE,
    )
    return root.with_hash()


def compile_subquery_dag(
    root: RootQueryIR,
    obligations: list[TypedObligationV1] | None = None,
    *,
    absolute_max_subqueries: int = 12,
    settings: object | None = None,
) -> list[SubQueryPlanV1]:
    """Compile a bounded DAG. Wave 1 independent; Wave 2 graph/bridge; Wave 4 verify."""

    if settings is not None:
        absolute_max_subqueries = int(
            getattr(settings, "COMPLEX_QUERY_ABSOLUTE_MAX_SUBQUERIES", absolute_max_subqueries)
        )
        beam = int(getattr(settings, "COMPLEX_QUERY_BEAM_WIDTH", 8))
        hop_abs = int(getattr(settings, "COMPLEX_QUERY_ABSOLUTE_MAX_HOPS", 3))
        seed_cap = int(getattr(settings, "COMPLEX_QUERY_SEED_ENTITY_CAP", 8))
    else:
        beam = 8
        hop_abs = 3
        seed_cap = 8

    obs = obligations or default_obligations_for_intent(
        root.intent_class, query_text=root.standalone_query
    )
    level = root.graph_level
    max_hops = min(hop_ceiling(level), hop_abs)
    max_graph_sq = graph_subquery_ceiling(level)
    corpus_ids = list(root.selected_corpus_ids)
    plans: list[SubQueryPlanV1] = []
    graph_sq_count = 0

    # Always: original direct evidence (Wave 1)
    plans.append(
        SubQueryPlanV1(
            subquery_id="sq_direct",
            query_id=root.query_id,
            obligation_id=obs[0].obligation_id if obs else "O1",
            parent_subquery_ids=[],
            query_type="direct_evidence",
            query_text=root.standalone_query,
            corpus_ids=corpus_ids,
            domains=list(root.required_domains),
            wave=1,
            candidate_budget=32,
        ).with_hash()
    )
    plans.append(
        SubQueryPlanV1(
            subquery_id="sq_vocab",
            query_id=root.query_id,
            # Prefer a distinct obligation when present so verification can
            # credit vocabulary coverage independently of direct evidence.
            obligation_id=(
                obs[1].obligation_id
                if len(obs) > 1
                else (obs[0].obligation_id if obs else "O1")
            ),
            parent_subquery_ids=[],
            query_type="vocabulary_resolution",
            query_text=root.standalone_query,
            corpus_ids=corpus_ids,
            wave=1,
            candidate_budget=16,
        ).with_hash()
    )
    plans.append(
        SubQueryPlanV1(
            subquery_id="sq_summary",
            query_id=root.query_id,
            obligation_id=obs[0].obligation_id if obs else "O1",
            parent_subquery_ids=[],
            query_type="summary_routing",
            query_text=root.standalone_query,
            corpus_ids=corpus_ids,
            wave=1,
            candidate_budget=12,
        ).with_hash()
    )

    # Cross-domain translation: explicit source/target graph path obligations
    # (Wave 2), capped by graph_subquery_ceiling — never unbounded.
    if root.intent_class == "cross_domain_translation" and max_graph_sq > 0:
        for ob_id, req in (
            ("O1", "source-domain graph path for runtime/behavior entities"),
            ("O3", "target-domain graph path for runtime/capability entities"),
        )[:max_graph_sq]:
            if len(plans) >= absolute_max_subqueries - 1:
                break
            if graph_sq_count >= max_graph_sq:
                break
            graph_sq_count += 1
            plans.append(
                SubQueryPlanV1(
                    subquery_id=f"sq_graph_{ob_id}",
                    query_id=root.query_id,
                    obligation_id=ob_id,
                    parent_subquery_ids=["sq_vocab", "sq_direct"],
                    query_type="graph_path",
                    query_text=req,
                    corpus_ids=corpus_ids,
                    maximum_hops=max_hops,
                    beam_width=beam,
                    expansion_cap=min(25, seed_cap * 4),
                    wave=2,
                    stop_conditions=[
                        "obligation_satisfied",
                        "no_qualified_candidates",
                        "target_entity_reached",
                    ],
                ).with_hash()
            )

    for ob in obs:
        if len(plans) >= absolute_max_subqueries - 1:
            break
        if ob.type in {"direct_evidence", "vocabulary_resolution"}:
            continue
        if ob.type in {"graph_path", "seed_entities"} or "relationship" in ob.type:
            if graph_sq_count >= max_graph_sq:
                continue
            graph_sq_count += 1
            plans.append(
                SubQueryPlanV1(
                    subquery_id=f"sq_graph_{ob.obligation_id}",
                    query_id=root.query_id,
                    obligation_id=ob.obligation_id,
                    parent_subquery_ids=["sq_vocab", "sq_direct"],
                    query_type="graph_path",
                    query_text=ob.requirement,
                    corpus_ids=corpus_ids,
                    domains=list(ob.domains or ([ob.domain] if ob.domain else [])),
                    maximum_hops=max_hops,
                    beam_width=beam,
                    expansion_cap=min(25, seed_cap * 4),
                    wave=2,
                    stop_conditions=[
                        "obligation_satisfied",
                        "no_qualified_candidates",
                        "target_entity_reached",
                    ],
                ).with_hash()
            )
            continue
        if ob.type == "cross_domain_bridge":
            plans.append(
                SubQueryPlanV1(
                    subquery_id=f"sq_bridge_{ob.obligation_id}",
                    query_id=root.query_id,
                    obligation_id=ob.obligation_id,
                    parent_subquery_ids=["sq_direct", "sq_vocab"],
                    query_type="cross_domain_bridge",
                    query_text=ob.requirement,
                    corpus_ids=corpus_ids,
                    domains=list(ob.domains),
                    wave=3,
                ).with_hash()
            )
            continue
        if ob.type in {"claim_oppose", "claim_support"} or "contradict" in ob.type:
            plans.append(
                SubQueryPlanV1(
                    subquery_id=f"sq_contra_{ob.obligation_id}",
                    query_id=root.query_id,
                    obligation_id=ob.obligation_id,
                    parent_subquery_ids=["sq_direct"],
                    query_type="contradiction",
                    query_text=ob.requirement,
                    corpus_ids=corpus_ids,
                    wave=3,
                ).with_hash()
            )
            continue
        if ob.type in {"latest_state", "supersession"}:
            plans.append(
                SubQueryPlanV1(
                    subquery_id=f"sq_temporal_{ob.obligation_id}",
                    query_id=root.query_id,
                    obligation_id=ob.obligation_id,
                    parent_subquery_ids=["sq_direct"],
                    query_type="temporal",
                    query_text=ob.requirement,
                    corpus_ids=corpus_ids,
                    wave=2,
                ).with_hash()
            )
            continue
        # Generic obligation-specific direct lane (still Wave 1)
        plans.append(
            SubQueryPlanV1(
                subquery_id=f"sq_ob_{ob.obligation_id}",
                query_id=root.query_id,
                obligation_id=ob.obligation_id,
                parent_subquery_ids=[],
                query_type="direct_evidence",
                query_text=ob.requirement,
                corpus_ids=corpus_ids,
                domains=list(ob.domains or ([ob.domain] if ob.domain else [])),
                wave=1,
            ).with_hash()
        )

    # Wave 4 verification always last
    if len(plans) < absolute_max_subqueries:
        plans.append(
            SubQueryPlanV1(
                subquery_id="sq_verify",
                query_id=root.query_id,
                obligation_id=obs[-1].obligation_id if obs else "O1",
                parent_subquery_ids=[p.subquery_id for p in plans if p.wave < 4][:6],
                query_type="verification",
                query_text="verify obligation coverage, path support, citations",
                corpus_ids=corpus_ids,
                wave=4,
            ).with_hash()
        )

    plans = plans[:absolute_max_subqueries]
    if not dag_is_acyclic(plans):
        raise RuntimeError("compiled subquery DAG is cyclic")
    # Invariant: at least one direct_evidence subquery
    if not any(p.query_type == "direct_evidence" for p in plans):
        raise RuntimeError("direct_evidence lane missing from subquery DAG")
    return plans
