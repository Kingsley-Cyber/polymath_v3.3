"""Phase-8 live wiring for alias schema retrieval (shadow / fixture-canary).

Production activation is NOT authorized:
  - enabled_globally default false
  - ranking_enabled default false (fixture allowlist only when explicitly on)
  - production schema writes / backfill hard-locked false
  - schema lane failures never block the original-query lane
"""

from __future__ import annotations

import logging
from time import perf_counter
from typing import Any, Sequence

from config import get_settings
from models.alias_schema_projection import ShadowSchemaRecordV1
from models.schemas import RetrievalTier, SourceChunk
from services.ingestion.alias_schema_retrieval import (
    DualLaneRetrievalResult,
    replay_retrieval_fingerprint,
    run_dual_lane_retrieval,
)

logger = logging.getLogger(__name__)

# In-process registry for isolated fixture shadow records (tests / canary).
_SHADOW_RECORD_REGISTRY: dict[str, list[ShadowSchemaRecordV1]] = {}


def register_shadow_schema_records(
    corpus_id: str, records: Sequence[ShadowSchemaRecordV1]
) -> None:
    """Register shadow schema records for an isolated fixture corpus."""

    _SHADOW_RECORD_REGISTRY[str(corpus_id)] = list(records)


def clear_shadow_schema_registry(corpus_id: str | None = None) -> None:
    if corpus_id is None:
        _SHADOW_RECORD_REGISTRY.clear()
    else:
        _SHADOW_RECORD_REGISTRY.pop(str(corpus_id), None)


def fixture_corpus_allowlist(settings: Any | None = None) -> set[str]:
    settings = settings or get_settings()
    raw = str(getattr(settings, "ALIAS_RETRIEVAL_FIXTURE_CORPUS_ALLOWLIST", "") or "")
    return {part.strip() for part in raw.split(",") if part.strip()}


def alias_retrieval_controls(settings: Any | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    return {
        "enabled_globally": bool(
            getattr(settings, "ALIAS_RETRIEVAL_ENABLED_GLOBALLY", False)
        ),
        "shadow_enabled": bool(
            getattr(settings, "ALIAS_RETRIEVAL_SHADOW_ENABLED", True)
        ),
        "ranking_enabled": bool(
            getattr(settings, "ALIAS_RETRIEVAL_RANKING_ENABLED", False)
        ),
        "fixture_corpus_allowlist": sorted(fixture_corpus_allowlist(settings)),
        "production_schema_writes": bool(
            getattr(settings, "ALIAS_RETRIEVAL_PRODUCTION_SCHEMA_WRITES", False)
        ),
        "production_backfill": bool(
            getattr(settings, "ALIAS_RETRIEVAL_PRODUCTION_BACKFILL", False)
        ),
        "shadow_deadline_s": float(
            getattr(settings, "ALIAS_RETRIEVAL_SHADOW_DEADLINE_SECONDS", 0.35)
        ),
    }


def _tier_name(tier: RetrievalTier | str) -> str:
    value = getattr(tier, "value", tier)
    mapping = {
        "qdrant_only": "fast",
        "qdrant_mongo": "hybrid",
        "qdrant_mongo_graph": "graph",
        "fast": "fast",
        "hybrid": "hybrid",
        "graph": "graph",
    }
    return mapping.get(str(value), "fast")


def _chunks_to_child_index(chunks: Sequence[SourceChunk]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for chunk in chunks:
        child_id = str(getattr(chunk, "chunk_id", "") or "")
        if not child_id:
            continue
        meta = getattr(chunk, "metadata", None) or {}
        if not isinstance(meta, dict):
            meta = {}
        rows.append(
            {
                "child_id": child_id,
                "text": str(getattr(chunk, "text", "") or ""),
                "parent_id": str(
                    getattr(chunk, "parent_id", None)
                    or meta.get("parent_id")
                    or ""
                )
                or None,
                "section_id": str(meta.get("section_id") or "") or None,
                "document_id": str(getattr(chunk, "doc_id", "") or "") or None,
            }
        )
    return rows


def _active_scopes_from_chunks(
    chunks: Sequence[SourceChunk],
) -> tuple[list[str], list[str]]:
    docs: list[str] = []
    parents: list[str] = []
    for chunk in chunks:
        doc_id = str(getattr(chunk, "doc_id", "") or "")
        if doc_id:
            docs.append(doc_id)
        meta = getattr(chunk, "metadata", None) or {}
        parent = str(
            getattr(chunk, "parent_id", None) or meta.get("parent_id") or ""
        )
        if parent:
            parents.append(parent)
    return sorted(set(docs)), sorted(set(parents))


def _shadow_records_for_corpora(corpus_ids: Sequence[str]) -> list[ShadowSchemaRecordV1]:
    records: list[ShadowSchemaRecordV1] = []
    for corpus_id in corpus_ids:
        records.extend(_SHADOW_RECORD_REGISTRY.get(str(corpus_id), []))
    # Deterministic order
    records.sort(key=lambda r: (r.corpus_id, r.canonical_term.lower(), r.schema_point_id))
    return records


def _capture_query_report(
    *,
    query: str,
    direct_ids: Sequence[str],
    result: DualLaneRetrievalResult,
    latency_s: float,
    mode: str,
) -> dict[str, Any]:
    traces = []
    for trace in result.traces:
        traces.append(
            {
                "schema_point_id": trace.schema_point_id,
                "corpus_entity_id": trace.corpus_entity_id,
                "alias_candidate_id": trace.alias_candidate_id,
                "alias_decision_id": trace.alias_decision_id,
                "matched_surface": trace.matched_surface,
                "trust_class": trace.trust_class,
                "expanded_query": trace.expanded_query,
                "linked_child_ids": list(trace.linked_child_ids),
                "linked_parent_ids": list(trace.linked_parent_ids),
                "linked_section_ids": list(trace.linked_section_ids),
                "ranking_contribution": trace.ranking_contribution,
                "final_hydrated_child_ids": list(trace.final_hydrated_child_ids),
                "schema_used_as_answer_evidence": trace.schema_used_as_answer_evidence,
                "trace_hash": trace.trace_hash,
            }
        )
    added_or_reordered = [
        child_id
        for child_id in result.final_ranked_child_ids
        if child_id not in set(direct_ids)
    ]
    return {
        "original_query": query,
        "direct_result_ids": list(direct_ids),
        "schema_traces": traces,
        "expanded_queries": list(result.expanded_queries),
        "linked_parent_summary_ids": list(result.used_parent_summary_ids),
        "linked_section_summary_ids": list(result.used_section_summary_ids),
        "linked_graph_node_ids": list(result.used_graph_node_ids),
        "results_added_or_reordered": added_or_reordered,
        "final_hydrated_chunk_ids": list(result.final_ranked_child_ids),
        "latency_s": {
            "alias_shadow_lane": round(latency_s, 4),
            "tier": result.tier,
        },
        "mode": mode,
        "replay": replay_retrieval_fingerprint(result),
        "semantic_related_terms_changed_ranking": (
            result.semantic_related_terms_changed_ranking
        ),
        "schema_records_used_as_answer_evidence": (
            result.schema_records_used_as_answer_evidence
        ),
        "global_fast_activation": result.global_fast_activation,
        "production_schema_mutations": result.production_schema_mutations,
    }


def apply_fixture_ranking_effect(
    chunks: list[SourceChunk],
    result: DualLaneRetrievalResult,
    *,
    allow: bool,
) -> tuple[list[SourceChunk], dict[str, Any]]:
    """Optionally reorder fixture finalists using trusted/bounded schema assist.

    Production (allow=False) returns chunks unchanged.
    """

    if not allow:
        return chunks, {
            "applied": False,
            "reason": "ranking_disabled_or_not_fixture",
            "production_queries_unchanged": True,
        }
    if not chunks:
        return chunks, {"applied": False, "reason": "empty_finalists"}

    by_id = {str(c.chunk_id): c for c in chunks if getattr(c, "chunk_id", None)}
    preferred = [
        child_id
        for child_id in result.final_ranked_child_ids
        if child_id in by_id
    ]
    if not preferred:
        return chunks, {"applied": False, "reason": "no_overlapping_schema_hits"}

    # Trusted/bounded contributions only — never related_terms.
    allowed_contrib = {
        "authoritative_expansion",
        "bounded_assistance",
        "scoped_assistance",
    }
    boost_ids = {
        child_id
        for trace in result.traces
        if trace.ranking_contribution in allowed_contrib
        for child_id in trace.final_hydrated_child_ids
        if child_id in by_id
    }
    if not boost_ids:
        return chunks, {"applied": False, "reason": "no_rankable_schema_hits"}

    head = [by_id[cid] for cid in preferred if cid in boost_ids]
    seen = {str(c.chunk_id) for c in head}
    tail = [c for c in chunks if str(c.chunk_id) not in seen]
    reordered = head + tail
    return reordered, {
        "applied": True,
        "boosted_child_ids": sorted(boost_ids),
        "production_queries_unchanged": False,
    }


def run_alias_retrieval_shadow(
    *,
    query: str,
    tier: RetrievalTier | str,
    corpus_ids: Sequence[str],
    finalists: Sequence[SourceChunk],
    settings: Any | None = None,
) -> tuple[list[SourceChunk], dict[str, Any]]:
    """Execute shadow/canary alias lane. Never raises to callers.

    Returns (possibly reordered finalists, diagnostics).
    """

    settings = settings or get_settings()
    controls = alias_retrieval_controls(settings)
    started = perf_counter()
    direct_ids = [
        str(c.chunk_id) for c in finalists if getattr(c, "chunk_id", None)
    ]
    base_diag: dict[str, Any] = {
        "status": "disabled",
        "controls": controls,
        "tier": _tier_name(tier),
        "original_query_lane_always_runs": True,
        "schema_lane_blocked_direct_retrieval": False,
        "production_schema_writes": False,
        "production_backfill": False,
        "global_fast_activation": False,
        "schema_records_as_citations": 0,
    }

    # Hard safety locks — refuse to operate if production writes somehow enabled.
    if controls["production_schema_writes"] or controls["production_backfill"]:
        base_diag["status"] = "refused_production_mutation_lock"
        base_diag["reason"] = "production_writes_or_backfill_not_authorized"
        return list(finalists), base_diag

    if controls["enabled_globally"]:
        # Explicitly not authorized for this phase — refuse global mode.
        base_diag["status"] = "refused_global_activation"
        base_diag["reason"] = "enabled_globally_not_authorized_in_phase8"
        return list(finalists), base_diag

    if not controls["shadow_enabled"]:
        base_diag["status"] = "shadow_disabled"
        return list(finalists), base_diag

    allowlist = set(controls["fixture_corpus_allowlist"])
    selected = [str(cid) for cid in corpus_ids if str(cid)]
    fixture_hit = bool(allowlist and any(cid in allowlist for cid in selected))
    ranking_allowed = bool(
        controls["ranking_enabled"] and fixture_hit
    )

    try:
        records = _shadow_records_for_corpora(selected)
        # Shadow mode may still run with empty records (no schema hit path).
        child_index = _chunks_to_child_index(finalists)
        doc_scope, parent_scope = _active_scopes_from_chunks(finalists)
        result = run_dual_lane_retrieval(
            query,
            tier=_tier_name(tier),
            shadow_records=records,
            corpus_child_index=child_index,
            active_document_ids=doc_scope,
            active_parent_ids=parent_scope,
            activate_global_fast_schema_expansion=False,
        )
        latency = perf_counter() - started
        report = _capture_query_report(
            query=query,
            direct_ids=direct_ids,
            result=result,
            latency_s=latency,
            mode="fixture_ranking" if ranking_allowed else "shadow",
        )
        out_chunks, rank_meta = apply_fixture_ranking_effect(
            list(finalists), result, allow=ranking_allowed
        )
        # Route budget assertions for diagnostics
        route_ok = True
        if result.tier == "fast" and (
            result.used_parent_summary_ids or result.used_section_summary_ids
        ):
            route_ok = False
        base_diag.update(
            {
                "status": "ok",
                "shadow_records_loaded": len(records),
                "fixture_corpus_selected": fixture_hit,
                "ranking_effect": rank_meta,
                "route_budget_ok": route_ok,
                "ambiguous_cross_expansion": _count_ambiguous_cross(result, doc_scope),
                "related_term_ranking_changes": int(
                    result.semantic_related_terms_changed_ranking
                ),
                "query_report": report,
                "latency_s": report["latency_s"],
            }
        )
        return out_chunks, base_diag
    except Exception as exc:
        logger.info(
            "alias retrieval shadow lane failed closed: %s",
            f"{type(exc).__name__}: {exc}"[:240],
        )
        base_diag.update(
            {
                "status": "schema_lane_failure_fallback",
                "error": f"{type(exc).__name__}: {exc}"[:300],
                "latency_s": {"alias_shadow_lane": round(perf_counter() - started, 4)},
                "schema_lane_blocked_direct_retrieval": False,
                "production_queries_unchanged": True,
            }
        )
        return list(finalists), base_diag


def _count_ambiguous_cross(
    result: DualLaneRetrievalResult, active_docs: Sequence[str]
) -> int:
    """Count ambiguous expansions that escaped document/parent scope (should be 0)."""

    if not active_docs:
        # Without scope, ambiguous matches should not hydrate children.
        return sum(
            1
            for trace in result.traces
            if trace.trust_class == "ambiguous_aliases"
            and trace.final_hydrated_child_ids
        )
    return 0


async def run_alias_retrieval_shadow_async(
    *,
    query: str,
    tier: RetrievalTier | str,
    corpus_ids: Sequence[str],
    finalists: Sequence[SourceChunk],
    settings: Any | None = None,
) -> tuple[list[SourceChunk], dict[str, Any]]:
    """Async wrapper with deadline — never blocks the direct lane past deadline."""

    import asyncio

    settings = settings or get_settings()
    deadline = float(
        getattr(settings, "ALIAS_RETRIEVAL_SHADOW_DEADLINE_SECONDS", 0.35)
    )
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                run_alias_retrieval_shadow,
                query=query,
                tier=tier,
                corpus_ids=corpus_ids,
                finalists=finalists,
                settings=settings,
            ),
            timeout=deadline,
        )
    except Exception as exc:
        return list(finalists), {
            "status": "schema_lane_failure_fallback",
            "error": f"{type(exc).__name__}: {exc}"[:300],
            "controls": alias_retrieval_controls(settings),
            "original_query_lane_always_runs": True,
            "schema_lane_blocked_direct_retrieval": False,
            "production_queries_unchanged": True,
            "schema_records_as_citations": 0,
            "global_fast_activation": False,
        }
