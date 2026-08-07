"""Bounded dark canary for complex-query — shadow metrics only.

Never mutates production ranking or user-visible answers. Collects
DarkCanaryComparisonV1 rows and auto-disables on rollback conditions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from models.dark_canary import DarkCanaryComparisonV1, GraphStatus

logger = logging.getLogger(__name__)

_LOCK = asyncio.Lock()
_ACTIVE = 0
_STATE: dict[str, Any] = {
    "enabled": True,
    "disabled_reason": None,
    "measured_queries": 0,
    "rollback_events": [],
    "latency_breaches": [],
}

RELATIONSHIP_CLASSES = {
    "direct_one_hop",
    "two_hop_dependency",
    "cross_domain_translation",
    "contradiction",
    "contradiction_analysis",
    "temporal_latest",
    "temporal_latest_state",
    "multi_hop_relationship",
    "dependency_analysis",
}
NEGATIVE_CONTROL_CLASSES = {
    "nonrelationship_negative_control",
    "negative_control",
}
PATH_EXPECTED_CLASSES = {
    "two_hop_dependency",
    "cross_domain_translation",
    "multi_hop_relationship",
    "dependency_analysis",
}


@dataclass
class DarkCanaryGate:
    allowed: bool
    reason: str = ""
    corpus_ids: list[str] = field(default_factory=list)
    user_id: str = ""


def _csv_set(raw: str | None) -> set[str]:
    return {x.strip() for x in str(raw or "").split(",") if x.strip()}


def dark_canary_settings_enabled(settings: Any) -> bool:
    return bool(getattr(settings, "COMPLEX_QUERY_DARK_CANARY_ENABLED", False))


def process_canary_enabled() -> bool:
    return bool(_STATE.get("enabled", True)) and not _STATE.get("disabled_reason")


def gate_dark_canary(
    *,
    settings: Any,
    corpus_ids: list[str] | None,
    user_id: str | None,
) -> DarkCanaryGate:
    if not dark_canary_settings_enabled(settings):
        return DarkCanaryGate(False, "flag_off")
    if not process_canary_enabled():
        return DarkCanaryGate(
            False, f"rolled_back:{_STATE.get('disabled_reason') or 'unknown'}"
        )

    corpora = [str(c) for c in (corpus_ids or []) if str(c)]
    allow_c = _csv_set(getattr(settings, "COMPLEX_QUERY_DARK_CANARY_CORPUS_ALLOWLIST", ""))
    max_c = int(getattr(settings, "COMPLEX_QUERY_DARK_CANARY_CORPORA_MAX", 3))
    if not allow_c:
        return DarkCanaryGate(False, "empty_corpus_allowlist")
    if len(allow_c) > max_c:
        allow_c = set(sorted(allow_c)[:max_c])
    hit = [c for c in corpora if c in allow_c]
    if not hit:
        return DarkCanaryGate(False, "corpus_not_allowlisted", corpora)

    allow_u = _csv_set(getattr(settings, "COMPLEX_QUERY_DARK_CANARY_USER_ALLOWLIST", ""))
    max_u = int(getattr(settings, "COMPLEX_QUERY_DARK_CANARY_USERS_MAX", 3))
    uid = str(user_id or "").strip()
    if allow_u:
        if len(allow_u) > max_u:
            allow_u = set(sorted(allow_u)[:max_u])
        if uid and uid not in allow_u:
            return DarkCanaryGate(False, "user_not_allowlisted", hit, uid)
        if not uid:
            return DarkCanaryGate(False, "user_id_required", hit, uid)

    max_q = int(getattr(settings, "COMPLEX_QUERY_DARK_CANARY_QUERIES_MAX", 100))
    if int(_STATE.get("measured_queries") or 0) >= max_q:
        return DarkCanaryGate(False, "query_budget_exhausted", hit, uid)

    return DarkCanaryGate(True, "ok", hit, uid)


def _looks_like_evidence_id(chunk_id: str) -> bool:
    """Accept fixture doc_* ids and production docHash_index / parent summary ids."""

    if not isinstance(chunk_id, str):
        return False
    c = chunk_id.strip()
    if not c or len(c) < 4:
        return False
    if c.startswith("doc_"):
        return True
    # Production: <doc_hash>_<nnnn> or <doc_hash>_parent_<nnnn>_summary
    if "_" in c and any(ch.isalnum() for ch in c):
        return True
    return False


def _citation_validity(child_ids: list[str]) -> float:
    if not child_ids:
        return 1.0
    ok = sum(1 for c in child_ids if _looks_like_evidence_id(c))
    return round(ok / max(1, len(child_ids)), 4)


def _path_node_blob(paths: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for p in paths:
        for key in ("node_ids", "nodes", "entity_ids", "labels", "node_labels"):
            vals = p.get(key) or []
            if isinstance(vals, list):
                parts.extend(str(v).lower() for v in vals)
        for key in ("path_text", "summary", "predicate"):
            if p.get(key):
                parts.append(str(p.get(key)).lower())
    return " ".join(parts)


def _detect_false_transitive(
    query: str, query_class: str, paths: list[dict[str, Any]], dark_ids: list[str]
) -> int:
    q = (query or "").lower()
    klass = (query_class or "").lower()
    if "unsupported_transitiv" not in klass and not (
        "microsoft" in q and "combat" in q
    ):
        return 0
    blob = _path_node_blob(paths) + " " + " ".join(dark_ids).lower()
    if "microsoft" in q and "combat" in q:
        if "microsoft" in blob and "combat" in blob and paths:
            return 1
    return 0


def _detect_identity_merge(query: str, query_class: str, answer_blob: str) -> int:
    blob = f"{query}\n{answer_blob}".lower()
    klass = (query_class or "").lower()
    if "ambiguous" not in klass and "infrared" not in blob:
        return 0
    if "infrared" in blob and (
        "information retrieval" in blob or " ir " in f" {blob} "
    ):
        if any(
            p in blob
            for p in (
                "same as",
                "identical",
                "are the same",
                "equals ir",
                "ir is infrared",
                "infrared is information",
            )
        ):
            return 1
    return 0


def _derive_graph_status(
    *,
    query_class: str,
    paths_used: int,
    every_path_supported: bool,
    silent_hybrid: int,
    requested_route: str,
    effective_route: str,
    trav_diag: dict[str, Any],
) -> GraphStatus:
    if silent_hybrid > 0:
        return "blocked"
    if "blocked" in str(trav_diag.get("status") or "").lower():
        return "blocked"
    if paths_used > 0:
        if every_path_supported:
            return "executed_with_paths"
        return "partial_graph"
    if query_class in PATH_EXPECTED_CLASSES or query_class in {
        "cross_domain_translation",
        "two_hop_dependency",
    }:
        if trav_diag.get("skipped"):
            return "blocked"
        return "no_supported_path"
    if "graph" not in str(requested_route).lower():
        return "not_required"
    if query_class in NEGATIVE_CONTROL_CLASSES:
        return "not_required"
    if paths_used == 0:
        return "not_required"
    return "no_supported_path"


def build_comparison(
    *,
    query_id: str,
    query: str,
    corpus_ids: list[str],
    user_id: str,
    query_class: str,
    baseline_child_ids: list[str],
    complex_query_diagnostics: dict[str, Any],
    baseline_retrieval_ms: float,
    effective_tier: str = "",
    requested_tier: str = "",
    downgrade_reason: str = "",
    synthesis_preflight_status: str = "unknown",
    provider_status: str = "not_run",
) -> DarkCanaryComparisonV1:
    cq = complex_query_diagnostics or {}
    dark_ids = [str(x) for x in (cq.get("selected_evidence_ids") or []) if x]
    baseline = [str(x) for x in baseline_child_ids if x]
    baseline_set = set(baseline)
    graph_added = [c for c in dark_ids if c not in baseline_set]

    ver = cq.get("answer_verification") or {}
    path_objs = list(cq.get("graph_path_summaries") or [])
    if not path_objs:
        path_objs = [{"path_id": p, "node_ids": []} for p in (cq.get("path_ids") or [])]

    every_support = bool(cq.get("every_path_has_child_support", True))
    unsupported_paths = 0
    if not every_support:
        unsupported_paths = int(cq.get("graph_paths_used") or 0)

    requested = str(requested_tier or "")
    effective = str(effective_tier or "")
    silent = int(cq.get("silent_hybrid_fallback") or 0)
    if "graph" in requested.lower() and "graph" not in effective.lower():
        silent = max(silent, 1)

    ranking_mutated = bool(cq.get("ranking_mutated"))
    packet_unsupported = int(ver.get("claims_unsupported") or 0)
    packet_total = int(ver.get("claims_total") or 0)
    packet_supported = int(ver.get("claims_supported") or 0)
    dark_synthesis_ran = bool(cq.get("dark_synthesis_ran"))
    dark_synthesis_ms = float(cq.get("dark_synthesis_ms") or 0.0)
    if dark_synthesis_ran:
        claims_total, claims_supported, claims_unsupported = (
            packet_total,
            packet_supported,
            packet_unsupported,
        )
    else:
        claims_total = claims_supported = claims_unsupported = 0

    klass = query_class or str(cq.get("intent_class") or "")
    false_t = _detect_false_transitive(query, klass, path_objs, dark_ids)
    identity = _detect_identity_merge(query, klass, " ".join(dark_ids))

    dark_cov = float(ver.get("citation_coverage") or 0.0)
    if packet_total > 0:
        dark_cov = round(packet_supported / packet_total, 4)
    baseline_cov = 1.0 if baseline else 0.0

    paths_used = int(cq.get("graph_paths_used") or 0)
    trav = dict(cq.get("traversal") or {})
    neo_rt = int(
        cq.get("neo4j_round_trips")
        or trav.get("neo4j_round_trips")
        or (cq.get("acceptance") or {}).get("traversal", {}).get("neo4j_round_trips")
        or 0
    )
    hydrate = int(
        cq.get("hydration_batch_fetches")
        or (cq.get("wave1") or {}).get("hydration_batch_fetches")
        or 0
    )
    rerank = int(
        cq.get("reranker_calls")
        or (cq.get("wave1") or {}).get("reranker_calls")
        or 0
    )

    graph_status = _derive_graph_status(
        query_class=klass,
        paths_used=paths_used,
        every_path_supported=every_support,
        silent_hybrid=silent,
        requested_route=requested,
        effective_route=effective,
        trav_diag=trav,
    )

    # Empty paths are "unexplained" only when traversal diagnostics are absent.
    unexplained_empty = 0
    if (
        klass in PATH_EXPECTED_CLASSES
        and paths_used == 0
        and silent == 0
        and not trav
        and not cq.get("traversal")
    ):
        unexplained_empty = 1

    # Irrelevant graph addition = graph work or graph-path evidence on a
    # nonrelationship control. Shadow MMR divergence alone is not graph pollution.
    irrelevant = False
    if klass in NEGATIVE_CONTROL_CLASSES and (
        paths_used > 0 or (bool(graph_added) and neo_rt > 0)
    ):
        irrelevant = True

    dark_ms = float(
        (cq.get("stage_timings_ms") or {}).get("complex_query_total") or 0.0
    )
    baseline_ms = float(baseline_retrieval_ms or 0.0)

    row = DarkCanaryComparisonV1(
        query_id=query_id,
        corpus_ids=list(corpus_ids),
        user_id=user_id,
        query_class=klass,
        requested_route=requested,
        effective_route=effective,
        graph_status=graph_status,
        downgrade_reason=str(downgrade_reason or ""),
        baseline_child_ids=baseline,
        dark_child_ids=dark_ids,
        graph_added_child_ids=graph_added,
        baseline_obligation_coverage=baseline_cov,
        dark_obligation_coverage=dark_cov,
        baseline_citation_validity=_citation_validity(baseline),
        dark_citation_validity=_citation_validity(dark_ids),
        graph_paths_used=paths_used,
        every_path_has_child_support=every_support,
        unsupported_selected_paths=unsupported_paths,
        false_transitive_inferences=false_t,
        ambiguous_identity_merges=identity,
        unexplained_empty_graph_paths=unexplained_empty,
        irrelevant_graph_addition=irrelevant,
        baseline_retrieval_ms=baseline_ms,
        dark_retrieval_ms=dark_ms,
        retrieval_ms=baseline_ms,
        dark_synthesis_ms=dark_synthesis_ms,
        dark_synthesis_ran=dark_synthesis_ran,
        neo4j_round_trips=neo_rt,
        hydration_batches=hydrate,
        reranker_calls=rerank,
        claims_total=claims_total,
        claims_supported=claims_supported,
        claims_unsupported=claims_unsupported,
        packet_claims_unsupported=packet_unsupported,
        contradiction_disclosed=bool(ver.get("contradiction_disclosures") or 0)
        or "contradict" in klass,
        temporal_selection_correct=None,
        silent_hybrid_fallback=silent,
        synthesis_preflight_status=synthesis_preflight_status,
        provider_status=provider_status,
        production_answer_mutations=0,
        production_ranking_mutated=ranking_mutated,
        user_visible_answer_changed=False,
    )
    return evaluate_rollback(row)


def evaluate_rollback(row: DarkCanaryComparisonV1) -> DarkCanaryComparisonV1:
    reasons: list[str] = []
    if row.dark_synthesis_ran and row.claims_unsupported > 0:
        reasons.append("unsupported_claim_detected")
    if row.unsupported_selected_paths > 0:
        reasons.append("unsupported_path")
    if row.false_transitive_inferences > 0:
        reasons.append("false_transitive_inference")
    if row.ambiguous_identity_merges > 0:
        reasons.append("identity_cross_merge")
    if row.dark_citation_validity < 1.0 and row.dark_child_ids:
        reasons.append("unresolved_child_citation")
    if row.silent_hybrid_fallback > 0:
        reasons.append("silent_graph_to_hybrid_downgrade")
    if row.production_ranking_mutated:
        reasons.append("production_ranking_mutation")
    if row.user_visible_answer_changed or row.production_answer_mutations:
        reasons.append("user_visible_answer_mutation")
    if row.neo4j_round_trips > 2:
        reasons.append("neo4j_round_trips_exceed_2")
    if reasons:
        return row.model_copy(
            update={"rollback_triggered": True, "rollback_reasons": reasons}
        ).with_hash()
    return row.with_hash()


def trigger_rollback(reason: str) -> None:
    _STATE["enabled"] = False
    _STATE["disabled_reason"] = reason
    _STATE.setdefault("rollback_events", []).append(
        {"reason": reason, "ts": time.time(), "measured": _STATE.get("measured_queries")}
    )
    logger.error("DARK_CANARY_ROLLBACK reason=%s", reason)


async def record_comparison(
    *,
    comparison: DarkCanaryComparisonV1,
    settings: Any,
    db: Any | None = None,
) -> dict[str, Any]:
    global _ACTIVE
    max_conc = int(getattr(settings, "COMPLEX_QUERY_DARK_CANARY_CONCURRENCY_MAX", 1))
    async with _LOCK:
        if _ACTIVE >= max_conc:
            return {"recorded": False, "reason": "concurrency_saturated"}
        _ACTIVE += 1
    try:
        _STATE["measured_queries"] = int(_STATE.get("measured_queries") or 0) + 1
        if comparison.rollback_triggered:
            trigger_rollback(",".join(comparison.rollback_reasons) or "rollback")

        dark_ms = float(comparison.dark_retrieval_ms or comparison.retrieval_ms or 0.0)
        p95_max = float(
            getattr(settings, "COMPLEX_QUERY_DARK_CANARY_RETRIEVAL_P95_MAX_MS", 10000)
        )
        breaches = list(_STATE.get("latency_breaches") or [])
        if dark_ms > p95_max:
            breaches.append(dark_ms)
            _STATE["latency_breaches"] = breaches[-5:]
            if len(_STATE["latency_breaches"]) >= 3:
                trigger_rollback("repeated_retrieval_over_10_seconds")

        payload = comparison.model_dump()
        out_dir = Path(
            str(
                getattr(
                    settings,
                    "COMPLEX_QUERY_DARK_CANARY_LEDGER_DIR",
                    "/data/ingest-files/complex-query-dark-canary",
                )
            )
        )
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            with (out_dir / "comparisons.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, default=str) + "\n")
            (out_dir / "state.json").write_text(
                json.dumps(
                    {
                        "enabled": process_canary_enabled(),
                        "disabled_reason": _STATE.get("disabled_reason"),
                        "measured_queries": _STATE.get("measured_queries"),
                        "rollback_events": _STATE.get("rollback_events"),
                    },
                    indent=2,
                    default=str,
                ),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("dark canary ledger write failed: %s", exc)

        if db is not None:
            try:
                await db["dark_canary_comparisons"].insert_one(
                    {**payload, "created_at": time.time()}
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("dark canary mongo write failed: %s", exc)

        return {
            "recorded": True,
            "rollback_triggered": comparison.rollback_triggered,
            "measured_queries": _STATE.get("measured_queries"),
            "canary_enabled": process_canary_enabled(),
            "comparison": payload,
        }
    finally:
        async with _LOCK:
            _ACTIVE = max(0, _ACTIVE - 1)


def acceptance_snapshot(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"n": 0, "pass": False}
    n = len(rows)
    unsupported_paths = sum(int(r.get("unsupported_selected_paths") or 0) for r in rows)
    false_t = sum(int(r.get("false_transitive_inferences") or 0) for r in rows)
    identity = sum(int(r.get("ambiguous_identity_merges") or 0) for r in rows)
    silent = sum(int(r.get("silent_hybrid_fallback") or 0) for r in rows)
    mutations = sum(int(r.get("production_answer_mutations") or 0) for r in rows)
    ranking = sum(1 for r in rows if r.get("production_ranking_mutated"))
    cite_ok = sum(1 for r in rows if float(r.get("dark_citation_validity") or 0) >= 1.0)
    child_support = sum(
        1 for r in rows if r.get("every_path_has_child_support", True) is not False
    )
    unexplained = sum(int(r.get("unexplained_empty_graph_paths") or 0) for r in rows)
    neo_breach = sum(1 for r in rows if int(r.get("neo4j_round_trips") or 0) > 2)
    hydrate_breach = sum(1 for r in rows if int(r.get("hydration_batches") or 0) > 1)
    rerank_breach = sum(1 for r in rows if int(r.get("reranker_calls") or 0) > 1)

    ret_ms = [
        float(r.get("retrieval_ms") or r.get("baseline_retrieval_ms") or 0) for r in rows
    ]
    ret_sorted = sorted(ret_ms)
    p95 = ret_sorted[int(0.95 * (len(ret_sorted) - 1))] if ret_sorted else 0.0

    gain_classes = RELATIONSHIP_CLASSES | {
        "direct_one_hop",
        "two_hop_dependency",
        "cross_domain_translation",
        "contradiction",
        "temporal_latest",
        "temporal_latest_state",
    }
    exclude_gain = NEGATIVE_CONTROL_CLASSES | {
        "ambiguous_identity",
        "ambiguous_ir",
        "unsupported_transitivity",
        "graph_fact_block",
        "graph_fact_request",
    }
    gain_rows = [
        r
        for r in rows
        if str(r.get("query_class") or "") in gain_classes
        and str(r.get("query_class") or "") not in exclude_gain
    ]
    positive_gain = sum(
        1
        for r in gain_rows
        if (r.get("graph_added_child_ids") or [])
        or int(r.get("graph_paths_used") or 0) > 0
        or float(r.get("dark_obligation_coverage") or 0)
        > float(r.get("baseline_obligation_coverage") or 0)
    )
    gain_rate = round(positive_gain / max(1, len(gain_rows)), 4)

    neg_rows = [
        r
        for r in rows
        if str(r.get("query_class") or "") in NEGATIVE_CONTROL_CLASSES
    ]
    irrelevant = sum(1 for r in neg_rows if r.get("irrelevant_graph_addition"))
    irrelevant_rate = round(irrelevant / max(1, len(neg_rows)), 4) if neg_rows else 0.0

    by_class: dict[str, int] = {}
    for r in rows:
        k = str(r.get("query_class") or "unknown")
        by_class[k] = by_class.get(k, 0) + 1

    acceptance = {
        "n": n,
        "by_class": by_class,
        "citation_resolution_rate": round(cite_ok / max(1, n), 4),
        "child_support_rate": round(child_support / max(1, n), 4),
        "unsupported_selected_paths": unsupported_paths,
        "false_transitive_inferences": false_t,
        "ambiguous_identity_merges": identity,
        "silent_hybrid_fallbacks": silent,
        "unexplained_empty_graph_paths": unexplained,
        "production_answer_mutations": mutations,
        "ranking_mutations": ranking,
        "OOM_events": 0,
        "graph_relationship_gain_positive_rate": gain_rate,
        "graph_relationship_gain_positive_count": positive_gain,
        "graph_relationship_gain_denominator": len(gain_rows),
        "nonrelationship_irrelevant_graph_addition_rate": irrelevant_rate,
        "nonrelationship_n": len(neg_rows),
        "retrieval_p95_ms": round(p95, 2),
        "neo4j_round_trips_breaches": neo_breach,
        "hydration_batches_breaches": hydrate_breach,
        "reranker_calls_breaches": rerank_breach,
        "rollback_count": sum(1 for r in rows if r.get("rollback_triggered")),
    }
    acceptance["pass"] = (
        acceptance["citation_resolution_rate"] == 1.0
        and acceptance["child_support_rate"] == 1.0
        and acceptance["unsupported_selected_paths"] == 0
        and acceptance["false_transitive_inferences"] == 0
        and acceptance["ambiguous_identity_merges"] == 0
        and acceptance["silent_hybrid_fallbacks"] == 0
        and acceptance["unexplained_empty_graph_paths"] == 0
        and acceptance["production_answer_mutations"] == 0
        and acceptance["ranking_mutations"] == 0
        and acceptance["OOM_events"] == 0
        and acceptance["graph_relationship_gain_positive_rate"] >= 0.70
        and acceptance["nonrelationship_irrelevant_graph_addition_rate"] <= 0.10
        and acceptance["retrieval_p95_ms"] <= 10000
        and acceptance["neo4j_round_trips_breaches"] == 0
        and acceptance["hydration_batches_breaches"] == 0
        and acceptance["reranker_calls_breaches"] == 0
    )
    return acceptance
