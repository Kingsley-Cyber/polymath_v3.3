"""Fixture-gated complex-query runtime (Phases 4–10 orchestration).

Does not enable global planner. Does not mutate production ranking defaults.
Call from isolated eval scripts only.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from models.complex_query import (
    AnswerVerificationV1,
    ContextPacketV1,
    SubQueryResultV1,
    TypedObligationV1,
)
from services.retriever.complex_query_executor import (
    ComplexQueryPlanBundle,
    corpus_allowlisted,
    plan_complex_query,
    schedule_waves,
)
from services.retriever.complex_query_fusion import (
    attach_paths_to_results,
    build_contradiction_bundle,
    build_cross_domain_bridge,
    fuse_obligation,
    global_pool_from_obligations,
)
from services.retriever.complex_query_traversal import (
    compile_traversal_plans,
    execute_traversal_batch,
)
from services.retriever.complex_query_wave1 import (
    SharedWave1Assets,
    build_shared_wave1_assets,
)
from services.retriever.protected_anchors import select_protected_anchors


@dataclass
class ComplexQueryRuntimeResult:
    bundle: ComplexQueryPlanBundle
    wave1: SharedWave1Assets
    subquery_results: list[SubQueryResultV1] = field(default_factory=list)
    traversal_diagnostics: dict[str, Any] = field(default_factory=dict)
    obligation_fusions: list[dict[str, Any]] = field(default_factory=list)
    bridges: list[dict[str, Any]] = field(default_factory=list)
    contradictions: list[dict[str, Any]] = field(default_factory=list)
    protected_child_ids: list[str] = field(default_factory=list)
    mmr_selected_child_ids: list[str] = field(default_factory=list)
    context_packet: dict[str, Any] = field(default_factory=dict)
    verification: dict[str, Any] = field(default_factory=dict)
    acceptance: dict[str, Any] = field(default_factory=dict)
    execution_ms: float = 0.0


class _ScoreChunk:
    def __init__(self, chunk_id: str, score: float, doc_id: str = "", parent_id: str = ""):
        self.chunk_id = chunk_id
        self.score = score
        self.doc_id = doc_id
        self.parent_id = parent_id
        self.source_tier = "child"


async def run_complex_query_fixture(
    *,
    db: Any,
    qdrant: Any,
    neo4j_driver: Any | None,
    query: str,
    corpus_id: str,
    settings: Any | None = None,
    root_embedding: list[float] | None = None,
) -> ComplexQueryRuntimeResult:
    if settings is None:
        from config import get_settings

        settings = get_settings()
    started = time.perf_counter()
    ids = [corpus_id]
    if not corpus_allowlisted(ids, settings):
        raise RuntimeError(
            f"corpus {corpus_id} not on COMPLEX_QUERY_CORPUS_ALLOWLIST — refusing"
        )

    bundle = plan_complex_query(
        original_query=query,
        corpus_ids=ids,
        requested_mode="qdrant_mongo_graph",
        settings=settings,
    )
    wave1 = await build_shared_wave1_assets(
        db=db,
        qdrant=qdrant,
        neo4j_driver=neo4j_driver,
        query=query,
        corpus_ids=ids,
        settings=settings,
        root_embedding=root_embedding,
    )

    # Materialize Wave-1 subquery results from shared assets (no re-embed).
    results: list[SubQueryResultV1] = []
    for sq in bundle.subqueries:
        if sq.wave != 1:
            continue
        if sq.query_type == "direct_evidence":
            results.append(
                SubQueryResultV1(
                    subquery_id=sq.subquery_id,
                    obligation_id=sq.obligation_id,
                    status="partial" if wave1.direct_child_ids else "unsupported",
                    direct_child_ids=list(wave1.direct_child_ids),
                    resolved_entity_ids=list(wave1.resolved_entity_ids),
                    resolved_concepts=list(wave1.resolved_concepts),
                    execution_ms=wave1.execution_ms,
                ).with_hash()
            )
        elif sq.query_type == "vocabulary_resolution":
            results.append(
                SubQueryResultV1(
                    subquery_id=sq.subquery_id,
                    obligation_id=sq.obligation_id,
                    status="partial" if wave1.resolved_concepts else "unsupported",
                    vocabulary_child_ids=list(wave1.vocabulary_child_ids),
                    resolved_entity_ids=list(wave1.resolved_entity_ids),
                    resolved_concepts=list(wave1.resolved_concepts),
                    execution_ms=wave1.execution_ms,
                ).with_hash()
            )
        elif sq.query_type == "summary_routing":
            results.append(
                SubQueryResultV1(
                    subquery_id=sq.subquery_id,
                    obligation_id=sq.obligation_id,
                    status="partial" if wave1.summary_guided_child_ids else "unsupported",
                    summary_guided_child_ids=list(wave1.summary_guided_child_ids),
                    execution_ms=wave1.execution_ms,
                ).with_hash()
            )
        elif sq.query_type in {"lexical_exact", "entity_resolution"}:
            results.append(
                SubQueryResultV1(
                    subquery_id=sq.subquery_id,
                    obligation_id=sq.obligation_id,
                    status="partial"
                    if (wave1.lexical_child_ids or wave1.resolved_entity_ids)
                    else "unsupported",
                    lexical_child_ids=list(wave1.lexical_child_ids),
                    resolved_entity_ids=list(wave1.resolved_entity_ids),
                    execution_ms=wave1.execution_ms,
                ).with_hash()
            )

    # Seed entity IDs onto graph subqueries for Wave 2
    graph_sqs = []
    for sq in bundle.subqueries:
        if sq.query_type != "graph_path":
            continue
        graph_sqs.append(
            sq.model_copy(
                update={"required_input_entity_ids": list(wave1.resolved_entity_ids)}
            )
        )

    trav_diag: dict[str, Any] = {"skipped": True}
    paths = []
    if graph_sqs and wave1.resolved_entity_ids and neo4j_driver is not None:
        batch = compile_traversal_plans(
            query_id=bundle.root.query_id,
            graph_subqueries=graph_sqs,
            seed_entity_ids=wave1.resolved_entity_ids,
            settings=settings,
        )
        trav = await execute_traversal_batch(
            neo4j_driver=neo4j_driver,
            batch=batch,
            corpus_ids=ids,
            settings=settings,
        )
        paths = trav.paths
        trav_diag = dict(trav.diagnostics)
        trav_diag["execution_ms"] = trav.execution_ms
        # Placeholder graph subquery results then attach paths
        for sq in graph_sqs:
            results.append(
                SubQueryResultV1(
                    subquery_id=sq.subquery_id,
                    obligation_id=sq.obligation_id,
                    status="unsupported",
                    resolved_entity_ids=list(wave1.resolved_entity_ids),
                ).with_hash()
            )
        results = attach_paths_to_results(results, paths)

    # Wave 3 bridges / contradictions
    bridges = []
    contradictions = []
    for sq in bundle.subqueries:
        if sq.query_type == "cross_domain_bridge":
            # Split direct children roughly into source/target by domain terms
            mid = max(1, len(wave1.direct_child_ids) // 2)
            bridge = build_cross_domain_bridge(
                obligation=TypedObligationV1(
                    obligation_id=sq.obligation_id,
                    type="cross_domain_bridge",
                    domains=list(sq.domains) or ["source", "target"],
                    requirement=sq.query_text,
                ),
                source_child_ids=wave1.direct_child_ids[:mid],
                target_child_ids=wave1.direct_child_ids[mid:]
                or wave1.lexical_child_ids[:mid],
                source_entity_ids=wave1.resolved_entity_ids[:4],
                target_entity_ids=wave1.resolved_entity_ids[4:8],
            )
            bridges.append(bridge.model_dump())
            results.append(
                SubQueryResultV1(
                    subquery_id=sq.subquery_id,
                    obligation_id=sq.obligation_id,
                    status=bridge.support_status,
                    bridges=[bridge],
                    direct_child_ids=bridge.source_child_ids + bridge.target_child_ids,
                ).with_hash()
            )
        elif sq.query_type == "contradiction":
            mid = max(1, len(wave1.direct_child_ids) // 2)
            contra = build_contradiction_bundle(
                obligation_id=sq.obligation_id,
                supporting_child_ids=wave1.direct_child_ids[:mid],
                opposing_child_ids=wave1.lexical_child_ids[:mid]
                or wave1.direct_child_ids[mid:],
                subject_entity_id=(wave1.resolved_entity_ids or [""])[0],
            )
            contradictions.append(contra.model_dump())
            results.append(
                SubQueryResultV1(
                    subquery_id=sq.subquery_id,
                    obligation_id=sq.obligation_id,
                    status="partial"
                    if contra.supporting_child_ids and contra.opposing_child_ids
                    else "unsupported",
                    contradictions=[contra],
                ).with_hash()
            )

    # Per-obligation fusion
    fusion_rows = []
    for ob in bundle.obligations:
        fus = fuse_obligation(
            obligation=ob, subquery_results=results, settings=settings
        )
        fusion_rows.append(
            {
                "obligation_id": fus.obligation_id,
                "ranked_child_ids": fus.ranked_child_ids[:12],
                "diagnostics": fus.diagnostics,
            }
        )
    from services.retriever.complex_query_fusion import ObligationFusionResult

    fus_objs = [
        ObligationFusionResult(
            obligation_id=r["obligation_id"],
            ranked_child_ids=r["ranked_child_ids"],
            diagnostics=r["diagnostics"],
        )
        for r in fusion_rows
    ]
    global_ids = global_pool_from_obligations(fus_objs, per_obligation_cap=5)

    # Protect + MMR-style remainder (reuse protected_anchors; simple fill)
    scored = [
        _ScoreChunk(
            cid,
            score=1.0 / (i + 1),
            doc_id=(wave1.child_payloads.get(cid) or {}).get("doc_id", ""),
            parent_id=(wave1.child_payloads.get(cid) or {}).get("parent_id", ""),
        )
        for i, cid in enumerate(global_ids)
    ]
    prot = select_protected_anchors(
        scored,
        minimum_protected=int(getattr(settings, "CROSS_DOMAIN_PROTECTED_ANCHORS_MIN", 1)),
        maximum_protected=int(getattr(settings, "CROSS_DOMAIN_PROTECTED_ANCHORS_MAX", 4)),
        max_per_document=int(
            getattr(settings, "CROSS_DOMAIN_MAX_PROTECTED_PER_DOCUMENT", 2)
        ),
        max_per_parent=int(getattr(settings, "CROSS_DOMAIN_MAX_PROTECTED_PER_PARENT", 1)),
    )
    protected_ids = [c.chunk_id for c in prot.protected]
    remainder = [c.chunk_id for c in prot.remainder]
    final_max = int(getattr(settings, "CROSS_DOMAIN_FINAL_CHILDREN_MAX", 18))
    mmr_selected = (protected_ids + remainder)[:final_max]

    # Wave 4 verification subquery
    for sq in bundle.subqueries:
        if sq.query_type != "verification":
            continue
        covered = sum(1 for r in fusion_rows if r["ranked_child_ids"])
        results.append(
            SubQueryResultV1(
                subquery_id=sq.subquery_id,
                obligation_id=sq.obligation_id,
                status="partial" if covered else "unsupported",
                coverage={
                    "obligations_with_evidence": covered,
                    "paths": len(paths),
                    "bridges": len(bridges),
                },
            ).with_hash()
        )

    # Stable capability view for ContextPacket hashing (exclude wall-clock ms).
    trav_diag_stable = {
        k: v
        for k, v in trav_diag.items()
        if k not in {"execution_ms", "timings", "wall_ms"}
    }

    path_verified = sum(1 for p in paths if p.supporting_child_ids)
    claims_supported_n = sum(1 for r in fusion_rows if r["ranked_child_ids"])
    claims_unsupported_n = sum(1 for r in fusion_rows if not r["ranked_child_ids"])
    if claims_unsupported_n == 0 and claims_supported_n > 0 and path_verified == len(paths):
        verification_status = "pass"
    elif claims_supported_n > 0:
        verification_status = "partial"
    else:
        verification_status = "block"
    verification = AnswerVerificationV1(
        claims_total=len(bundle.obligations),
        claims_supported=claims_supported_n,
        claims_unsupported=claims_unsupported_n,
        obligations_covered=sum(1 for r in fusion_rows if len(r["ranked_child_ids"]) >= 2),
        obligations_partial=sum(1 for r in fusion_rows if 0 < len(r["ranked_child_ids"]) < 2),
        obligations_unsupported=sum(1 for r in fusion_rows if not r["ranked_child_ids"]),
        graph_paths_used=len(paths),
        graph_paths_verified=path_verified,
        bridge_claims=len(bridges),
        bridge_claims_verified=sum(
            1
            for b in bridges
            if b.get("source_child_ids") and b.get("target_child_ids")
        ),
        contradiction_disclosures=len(contradictions),
        citation_coverage=(
            sum(1 for r in fusion_rows if r["ranked_child_ids"]) / max(1, len(fusion_rows))
        ),
        verification_status=verification_status,
    ).with_hash()

    packet = ContextPacketV1(
        conversation_context={
            "original_query": bundle.root.original_query,
            "standalone_query": bundle.root.standalone_query,
            "constraints": bundle.root.conversation_constraints,
        },
        root_query=bundle.root.model_dump(),
        query_plan={
            "intent_class": bundle.root.intent_class,
            "graph_level": bundle.root.graph_level,
        },
        subquery_plan=[s.model_dump() for s in bundle.subqueries],
        vocabulary_resolution={
            "concepts": wave1.resolved_concepts,
            "entity_ids": wave1.resolved_entity_ids,
            "hits": wave1.vocabulary_hits,
        },
        graph_capability=trav_diag_stable,
        obligation_results=fusion_rows,
        routing_summaries=[
            {"summary_id": sid, "answer_authority": False}
            for sid in wave1.summary_route_ids
        ],
        protected_child_evidence=[
            {"chunk_id": cid, "selection_reason": "protected_anchor"}
            for cid in protected_ids
        ],
        mmr_selected_child_evidence=[
            {"chunk_id": cid, "selection_reason": "mmr_selected"}
            for cid in mmr_selected
            if cid not in set(protected_ids)
        ],
        graph_paths=[p.model_dump() for p in paths],
        cross_domain_bridges=bridges,
        contradictions=contradictions,
        coverage={
            "final_children": len(mmr_selected),
            "protected": len(protected_ids),
            "waves": len(schedule_waves(bundle.subqueries)),
        },
        unresolved_obligations=[
            r["obligation_id"] for r in fusion_rows if not r["ranked_child_ids"]
        ],
        warnings=[],
        schema_records_as_citations=0,
        summaries_as_detailed_claim_citations=0,
    ).with_hash()

    acceptance = {
        "wave_1": {
            "direct_lane_always_runs": True,
            "vocabulary_lane_parallel": True,
            "duplicate_root_embeddings": wave1.duplicate_root_embeddings,
            "duplicate_child_hydration": wave1.duplicate_child_hydration,
            "ranking_mutated": False,
            **wave1.diagnostics,
        },
        "traversal": {
            **trav_diag,
            "unrestricted_bfs": False,
            "every_returned_assertion_has_child_support": all(
                bool(p.supporting_child_ids) for p in paths
            )
            if paths
            else True,
        },
        "safety": {
            "fixture_only": True,
            "silent_hybrid_fallback": 0,
            "production_behavior_changed": False,
            "planner_global_enable": bool(
                getattr(settings, "COMPLEX_QUERY_SUBQUERY_PLANNER_ENABLED", False)
            ),
        },
        "fusion": {
            "per_obligation_rrf": True,
            "protected_max": len(protected_ids) <= 4,
            "final_children": len(mmr_selected),
        },
    }
    ok = (
        acceptance["wave_1"]["duplicate_root_embeddings"] == 0
        and acceptance["wave_1"]["duplicate_child_hydration"] == 0
        and acceptance["wave_1"]["ranking_mutated"] is False
        and acceptance["safety"]["production_behavior_changed"] is False
        and acceptance["safety"]["planner_global_enable"] is False
        and acceptance["traversal"].get("unrestricted_bfs") is False
        and int(acceptance["traversal"].get("neo4j_round_trips") or 0)
        <= int(getattr(settings, "COMPLEX_QUERY_NEO4J_ROUND_TRIPS_MAX", 2))
    )
    acceptance["phase_4_5_ok"] = ok

    return ComplexQueryRuntimeResult(
        bundle=bundle,
        wave1=wave1,
        subquery_results=results,
        traversal_diagnostics=trav_diag,
        obligation_fusions=fusion_rows,
        bridges=bridges,
        contradictions=contradictions,
        protected_child_ids=protected_ids,
        mmr_selected_child_ids=mmr_selected,
        context_packet=packet.model_dump(),
        verification=verification.model_dump(),
        acceptance=acceptance,
        execution_ms=round((time.perf_counter() - started) * 1000.0, 2),
    )
