"""Validation helpers for graph hop count and edge-property pruning.

These functions do not call Neo4j or an LLM. They summarize already-returned
`/api/graph/query` payloads so live smoke scripts and offline tests can answer:

  * Did hop 2 add useful coverage or mostly bloat?
  * Are generic/weak/thin edges dominating the returned graph?
  * Did query-time pruning leave a concise, inspectable context packet?
"""

from __future__ import annotations

from collections import Counter
from statistics import mean
from typing import Any

GENERIC_PREDICATES = {"related_to", "references", "mentions"}
WEAK_STRENGTHS = {"weak", "thin"}


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _edge_key(edge: dict[str, Any]) -> str:
    source = str(edge.get("source") or "")
    target = str(edge.get("target") or "")
    predicate = str(edge.get("predicate") or edge.get("kind") or "related_to")
    ordered = sorted([source, target])
    return f"{ordered[0]}|{ordered[1]}|{predicate}"


def summarize_graph_query_payload(
    payload: dict[str, Any],
    *,
    latency_s: float | None = None,
) -> dict[str, Any]:
    """Return bounded, deterministic quality diagnostics for a graph payload."""

    nodes = [n for n in payload.get("nodes") or [] if isinstance(n, dict)]
    links = [e for e in payload.get("links") or [] if isinstance(e, dict)]
    bridges = [b for b in payload.get("bridges") or [] if isinstance(b, dict)]
    gaps = [g for g in payload.get("gaps") or [] if isinstance(g, dict)]
    seeds = [s for s in payload.get("seed_entities") or [] if isinstance(s, dict)]

    node_ids = {
        str(n.get("id") or n.get("entity_id") or "")
        for n in nodes
        if n.get("id") or n.get("entity_id")
    }
    edge_keys = {_edge_key(edge) for edge in links}
    confidences = [_as_float(edge.get("confidence")) for edge in links]
    predicates = Counter(
        str(edge.get("predicate") or edge.get("kind") or "related_to").lower()
        for edge in links
    )
    strengths = Counter(str(edge.get("edge_strength") or "unknown").lower() for edge in links)

    generic_edges = sum(
        1
        for edge in links
        if str(edge.get("predicate") or edge.get("kind") or "related_to").lower()
        in GENERIC_PREDICATES
    )
    weak_or_thin_edges = sum(
        1
        for edge in links
        if str(edge.get("edge_strength") or "").lower() in WEAK_STRENGTHS
    )
    low_confidence_edges = sum(1 for conf in confidences if conf <= 0.45)
    no_evidence_edges = sum(
        1 for edge in links if int(edge.get("evidence_count") or 0) == 0
    )
    eligible_edges = sum(
        1 for edge in links if bool(edge.get("eligible_for_synthesis"))
    )
    specific_edges = max(0, len(links) - generic_edges)

    link_count = len(links)
    # Cheap proxy for context pressure. It is not a token count; it lets evals
    # compare hop/pruning settings consistently without serializing prompts.
    context_bloat_score = round(
        len(nodes) + (0.35 * link_count) + (2.0 * len(bridges)) + (0.5 * len(gaps)),
        2,
    )

    trace = payload.get("trace") if isinstance(payload.get("trace"), dict) else {}
    graph_edge_policy = trace.get("graph_edge_policy") or {}

    return {
        "latency_s": round(float(latency_s), 3) if latency_s is not None else None,
        "nodes": len(nodes),
        "links": link_count,
        "bridges": len(bridges),
        "gaps": len(gaps),
        "seeds": len(seeds),
        "seed_names": [
            str(seed.get("display_name") or seed.get("label") or seed.get("id") or "")
            for seed in seeds[:12]
        ],
        "node_ids": sorted(node_ids),
        "edge_keys": sorted(edge_keys),
        "edge_quality": {
            "avg_confidence": round(mean(confidences), 4) if confidences else 0.0,
            "generic_edges": generic_edges,
            "generic_ratio": round(generic_edges / link_count, 4) if link_count else 0.0,
            "specific_edges": specific_edges,
            "specific_ratio": round(specific_edges / link_count, 4) if link_count else 0.0,
            "weak_or_thin_edges": weak_or_thin_edges,
            "weak_or_thin_ratio": round(weak_or_thin_edges / link_count, 4)
            if link_count
            else 0.0,
            "low_confidence_edges": low_confidence_edges,
            "low_confidence_ratio": round(low_confidence_edges / link_count, 4)
            if link_count
            else 0.0,
            "no_evidence_edges": no_evidence_edges,
            "no_evidence_ratio": round(no_evidence_edges / link_count, 4)
            if link_count
            else 0.0,
            "eligible_edges": eligible_edges,
            "eligible_ratio": round(eligible_edges / link_count, 4) if link_count else 0.0,
            "top_predicates": predicates.most_common(8),
            "edge_strengths": strengths.most_common(),
        },
        "context_bloat_score": context_bloat_score,
        "graph_edge_policy": graph_edge_policy,
    }


def compare_hop_summaries(
    hop1: dict[str, Any],
    hop2: dict[str, Any],
) -> dict[str, Any]:
    """Compare hop 1 and hop 2 summaries for coverage gain vs drift risk."""

    hop1_nodes = set(hop1.get("node_ids") or [])
    hop2_nodes = set(hop2.get("node_ids") or [])
    hop1_edges = set(hop1.get("edge_keys") or [])
    hop2_edges = set(hop2.get("edge_keys") or [])

    new_nodes = hop2_nodes - hop1_nodes
    new_edges = hop2_edges - hop1_edges
    node_gain = len(new_nodes)
    edge_gain = len(new_edges)
    bloat_delta = _as_float(hop2.get("context_bloat_score")) - _as_float(
        hop1.get("context_bloat_score")
    )
    hop2_links = max(1, int(hop2.get("links") or 0))

    return {
        "new_nodes_from_hop2": node_gain,
        "new_edges_from_hop2": edge_gain,
        "node_gain_ratio": round(node_gain / max(1, len(hop1_nodes)), 4),
        "edge_gain_ratio": round(edge_gain / max(1, len(hop1_edges)), 4),
        "context_bloat_delta": round(bloat_delta, 2),
        "hop2_new_edge_share": round(edge_gain / hop2_links, 4),
        "hop2_added_seed_names": [],
        "interpretation": (
            "hop2_added_context"
            if node_gain or edge_gain
            else "hop2_no_extra_context"
        ),
    }


def validate_graph_hop_report(
    *,
    hop1: dict[str, Any],
    hop2: dict[str, Any],
    comparison: dict[str, Any],
    max_latency_s: float = 35.0,
    max_hop2_links: int = 360,
    max_context_bloat_delta: float = 180.0,
    max_generic_ratio: float = 0.45,
    max_weak_or_thin_ratio: float = 0.30,
) -> list[str]:
    """Return human-readable issues. Empty means the validation passed."""

    issues: list[str] = []
    for label, summary in (("hop1", hop1), ("hop2", hop2)):
        latency = summary.get("latency_s")
        if latency is not None and _as_float(latency) > max_latency_s:
            issues.append(f"{label} latency {latency}s exceeds {max_latency_s}s")
        if int(summary.get("seeds") or 0) <= 0:
            issues.append(f"{label} resolved no seed entities")
        if int(summary.get("nodes") or 0) <= 0:
            issues.append(f"{label} returned no graph nodes")

    hop2_quality = hop2.get("edge_quality") or {}
    if int(hop2.get("links") or 0) > max_hop2_links:
        issues.append(f"hop2 links {hop2.get('links')} exceeds cap {max_hop2_links}")
    if _as_float(comparison.get("context_bloat_delta")) > max_context_bloat_delta:
        issues.append(
            "hop2 context bloat delta "
            f"{comparison.get('context_bloat_delta')} exceeds {max_context_bloat_delta}"
        )
    if _as_float(hop2_quality.get("generic_ratio")) > max_generic_ratio:
        issues.append(
            "hop2 generic edge ratio "
            f"{hop2_quality.get('generic_ratio')} exceeds {max_generic_ratio}"
        )
    if _as_float(hop2_quality.get("weak_or_thin_ratio")) > max_weak_or_thin_ratio:
        issues.append(
            "hop2 weak/thin edge ratio "
            f"{hop2_quality.get('weak_or_thin_ratio')} exceeds {max_weak_or_thin_ratio}"
        )
    if int(hop2.get("nodes") or 0) < int(hop1.get("nodes") or 0):
        issues.append("hop2 returned fewer nodes than hop1")
    return issues
