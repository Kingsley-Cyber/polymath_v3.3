"""Deterministic context-window packing for research artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PackedResearchContext:
    evidence: list[dict[str, Any]]
    graph_traces: list[dict[str, Any]]
    receipt: dict[str, Any]


def estimate_tokens(value: Any) -> int:
    """Cheap deterministic token estimate for budgeting packed artifacts."""
    text = str(value or "")
    return max(1, (len(text) + 3) // 4)


def context_token_budget(output_token_budget: int) -> int:
    """Map answer budget to a bounded evidence/context budget."""
    return max(1024, min(24_000, int(output_token_budget or 0) * 3))


def _copy_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items()}


def _evidence_cost(row: dict[str, Any]) -> int:
    parts = [
        row.get("citation_id"),
        row.get("subquestion_id"),
        row.get("corpus_id"),
        row.get("doc_id"),
        row.get("chunk_id"),
        row.get("quote"),
    ]
    return estimate_tokens("\n".join(str(part or "") for part in parts))


def _compact_graph_trace(trace: dict[str, Any], *, severe: bool = False) -> dict[str, Any]:
    row = _copy_row(trace)
    row["seeds"] = list(row.get("seeds") or [])[: (3 if severe else 8)]
    graph = row.get("graph") or {}
    if isinstance(graph, dict):
        row["graph"] = {
            "nodes": list(graph.get("nodes") or [])[: (6 if severe else 24)],
            "links": list(graph.get("links") or [])[: (10 if severe else 48)],
        }
    return row


def _graph_cost(row: dict[str, Any]) -> int:
    parts = [
        row.get("subquestion_id"),
        row.get("corpus_id"),
        row.get("status"),
        row.get("seed_count"),
        row.get("node_count"),
        row.get("edge_count"),
        row.get("seeds"),
        row.get("graph"),
    ]
    return estimate_tokens("\n".join(str(part or "") for part in parts))


def pack_research_context(
    *,
    evidence: list[dict[str, Any]],
    graph_traces: list[dict[str, Any]],
    token_budget: int,
) -> PackedResearchContext:
    """Pack evidence and graph traces into a deterministic context budget.

    The packer prefers evidence over graph packets, preserves input order, and
    never drops silently: all truncation/drop decisions are returned in receipt.
    """
    budget = max(256, int(token_budget or 0))
    evidence_budget = max(192, int(budget * 0.75))
    graph_budget = max(64, budget - evidence_budget)

    packed_evidence: list[dict[str, Any]] = []
    evidence_tokens = 0
    dropped_evidence = 0
    truncated_quotes = 0
    for original in evidence:
        row = _copy_row(original)
        cost = _evidence_cost(row)
        if cost > evidence_budget and not packed_evidence:
            quote = str(row.get("quote") or "")
            row["quote"] = quote[: max(320, evidence_budget * 4)]
            row["context_truncated"] = True
            truncated_quotes += 1
            cost = _evidence_cost(row)
        if evidence_tokens + cost <= evidence_budget:
            packed_evidence.append(row)
            evidence_tokens += cost
        else:
            dropped_evidence += 1

    packed_graph: list[dict[str, Any]] = []
    graph_tokens = 0
    dropped_graph_traces = 0
    compacted_graph_traces = 0
    for original in graph_traces:
        row = _compact_graph_trace(original)
        compacted_graph_traces += 1
        cost = _graph_cost(row)
        if cost > graph_budget and not packed_graph:
            row = _compact_graph_trace(original, severe=True)
            cost = _graph_cost(row)
        if graph_tokens + cost <= graph_budget:
            packed_graph.append(row)
            graph_tokens += cost
        else:
            dropped_graph_traces += 1

    receipt = {
        "token_budget": budget,
        "estimated_tokens": evidence_tokens + graph_tokens,
        "evidence_budget": evidence_budget,
        "graph_budget": graph_budget,
        "input_evidence": len(evidence),
        "included_evidence": len(packed_evidence),
        "dropped_evidence": dropped_evidence,
        "truncated_quotes": truncated_quotes,
        "input_graph_traces": len(graph_traces),
        "included_graph_traces": len(packed_graph),
        "dropped_graph_traces": dropped_graph_traces,
        "compacted_graph_traces": compacted_graph_traces,
    }
    return PackedResearchContext(
        evidence=packed_evidence,
        graph_traces=packed_graph,
        receipt=receipt,
    )
