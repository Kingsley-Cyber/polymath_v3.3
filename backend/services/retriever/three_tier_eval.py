"""Deterministic validators for the three UI retrieval routes.

The live runner in ``scripts/retrieval_three_tier_eval.py`` calls the real
``/api/chat`` SSE endpoint. This module stays pure so unit tests can exercise
the route contracts without needing Qdrant, MongoDB, Neo4j, or an LLM.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any


ROUTES: tuple[dict[str, str], ...] = (
    {
        "ui_name": "Fast Search",
        "tier": "qdrant_only",
        "purpose": "fast semantic parent-summary recall",
    },
    {
        "ui_name": "Hybrid Search",
        "tier": "qdrant_mongo",
        "purpose": "Mongo lexical/hybrid evidence over hydrated chunks",
    },
    {
        "ui_name": "Graph Augmentation",
        "tier": "qdrant_mongo_graph",
        "purpose": "Hybrid seeds plus Neo4j facts, entities, and relations",
    },
)

ROUTE_BY_NAME = {route["ui_name"]: route for route in ROUTES}
ROUTE_BY_TIER = {route["tier"]: route for route in ROUTES}

ROUTE_LATENCY_BUDGETS: dict[str, dict[str, float]] = {
    # These are quality targets, not emergency ceilings. The live runner reports
    # model-generation slowness separately so retrieval regressions cannot hide
    # behind a broad end-to-end timeout.
    "Fast Search": {
        "retrieval_s": 6.0,
        "total_s": 20.0,
        "generation_after_sources_s": 14.0,
    },
    "Hybrid Search": {
        "retrieval_s": 8.0,
        "total_s": 20.0,
        "generation_after_sources_s": 14.0,
    },
    "Graph Augmentation": {
        "retrieval_s": 8.0,
        "total_s": 25.0,
        "generation_after_sources_s": 16.0,
    },
}


def normalize_route(value: str | None) -> dict[str, str]:
    """Resolve either a UI route name or backend tier value."""
    raw = str(value or "").strip()
    return ROUTE_BY_NAME.get(raw) or ROUTE_BY_TIER.get(raw) or {
        "ui_name": raw or "Unknown",
        "tier": raw or "unknown",
        "purpose": "unknown",
    }


def route_latency_budget(route_name: str | None) -> dict[str, float]:
    route = normalize_route(route_name)
    return dict(ROUTE_LATENCY_BUDGETS.get(route["ui_name"], {}))


def _text(value: Any) -> str:
    return str(value or "")


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold()).strip()


def _source_text(source: dict[str, Any]) -> str:
    return _text(
        source.get("text")
        or source.get("content")
        or source.get("chunk_text")
        or source.get("summary")
    )


def source_identifier(source: dict[str, Any]) -> str:
    """Best-effort stable id for reporting without exposing full text."""
    for key in ("chunk_id", "id", "source_id", "parent_id"):
        value = source.get(key)
        if value:
            return str(value)
    doc = source.get("doc_id") or source.get("doc_name") or "unknown-doc"
    parent = source.get("parent_id") or "unknown-parent"
    return f"{doc}:{parent}"


def summarize_sources(sources: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Summarize retrieved sources without copying private corpus text."""
    sources = sources or []
    docs: Counter[str] = Counter()
    parents: Counter[str] = Counter()
    source_tiers: Counter[str] = Counter()
    source_ids: list[str] = []
    text_lengths: list[int] = []
    provenance_count = 0
    graph_signal_sources = 0

    for source in sources:
        doc = _text(source.get("doc_id") or source.get("doc_name") or "unknown")
        parent = _text(source.get("parent_id") or source.get("parent_chunk_id") or "none")
        tier = _text(source.get("source_tier") or source.get("chunk_kind") or "unknown")
        text = _source_text(source)
        provenance = source.get("provenance") or []

        docs[doc] += 1
        parents[parent] += 1
        source_tiers[tier] += 1
        source_ids.append(source_identifier(source))
        text_lengths.append(len(text))
        if provenance:
            provenance_count += 1
        if "graph" in tier.casefold() or provenance:
            graph_signal_sources += 1

    parent_duplicates = {
        key: value for key, value in parents.items() if key != "none" and value > 1
    }
    doc_distribution = dict(docs.most_common())
    total_text_chars = sum(text_lengths)
    return {
        "source_count": len(sources),
        "source_ids": source_ids,
        "unique_doc_count": len(docs),
        "unique_parent_count": len(parents),
        "doc_distribution": doc_distribution,
        "source_tier_counts": dict(source_tiers.most_common()),
        "parent_duplicate_count": sum(parent_duplicates.values()),
        "parent_duplicates": parent_duplicates,
        "sources_with_text": sum(1 for length in text_lengths if length > 0),
        "total_text_chars": total_text_chars,
        "avg_text_chars": round(total_text_chars / max(1, len(sources)), 1),
        "sources_with_provenance": provenance_count,
        "graph_signal_sources": graph_signal_sources,
    }


def _anchor_groups(case: dict[str, Any]) -> list[dict[str, Any]]:
    groups = case.get("anchor_groups") or []
    normalized: list[dict[str, Any]] = []
    for index, group in enumerate(groups):
        if isinstance(group, dict):
            terms = [str(term) for term in group.get("terms") or [] if str(term).strip()]
            if not terms and group.get("name"):
                terms = [str(group["name"])]
            normalized.append(
                {
                    "name": str(group.get("name") or f"anchor_{index + 1}"),
                    "terms": terms,
                    "required": bool(group.get("required", True)),
                }
            )
        elif isinstance(group, (list, tuple)):
            terms = [str(term) for term in group if str(term).strip()]
            normalized.append(
                {
                    "name": terms[0] if terms else f"anchor_{index + 1}",
                    "terms": terms,
                    "required": True,
                }
            )
        elif str(group).strip():
            normalized.append(
                {"name": str(group), "terms": [str(group)], "required": True}
            )
    return normalized


def anchor_coverage(case: dict[str, Any], haystack: str) -> dict[str, Any]:
    """Measure query anchor coverage in answer text or retrieved source text."""
    groups = _anchor_groups(case)
    hay = _norm(haystack)
    covered: list[str] = []
    missing: list[str] = []
    required = [group for group in groups if group.get("required", True)]
    required_covered: list[str] = []
    required_missing: list[str] = []

    for group in groups:
        terms = [_norm(term) for term in group.get("terms") or []]
        hit = any(term and term in hay for term in terms)
        name = str(group.get("name") or ",".join(terms))
        if hit:
            covered.append(name)
            if group.get("required", True):
                required_covered.append(name)
        else:
            missing.append(name)
            if group.get("required", True):
                required_missing.append(name)

    return {
        "coverage": round(len(covered) / max(1, len(groups)), 3),
        "required_coverage": round(len(required_covered) / max(1, len(required)), 3),
        "covered": covered,
        "missing": missing,
        "required_missing": required_missing,
        "anchor_count": len(groups),
        "required_anchor_count": len(required),
    }


def sources_joined_text(sources: list[dict[str, Any]] | None) -> str:
    return "\n".join(_source_text(source) for source in (sources or []))


def extract_trace_summary(trace_events: list[dict[str, Any]] | None) -> dict[str, Any]:
    trace_events = trace_events or []
    titles = [_text(event.get("title")) for event in trace_events]
    local_rag = next(
        (event for event in trace_events if event.get("title") == "Local RAG retrieval"),
        {},
    )
    graph_advantage = next(
        (event for event in trace_events if event.get("title") == "Graph Advantage"),
        {},
    )
    local_meta = local_rag.get("metadata") or {}
    graph_meta = graph_advantage.get("metadata") or {}
    diagnostics = local_meta.get("retrieval_diagnostics") or {}
    return {
        "trace_titles": titles,
        "has_local_rag_trace": bool(local_rag),
        "has_graph_advantage": bool(graph_advantage),
        "effective_tier": _text(local_meta.get("effective_tier")),
        "local_rag_duration_s": local_meta.get("duration_s"),
        "retrieval_diagnostics": diagnostics,
        "graph_advantage": graph_meta,
    }


def _issue(level: str, code: str, message: str) -> dict[str, str]:
    return {"level": level, "code": code, "message": message}


def evaluate_route_result(
    *,
    query_case: dict[str, Any],
    route_name: str,
    result: dict[str, Any],
    max_total_s: float | None = None,
    max_retrieval_s: float | None = None,
    max_generation_s: float | None = None,
    fail_on_total_budget: bool = False,
    fail_on_generation_budget: bool = False,
) -> dict[str, Any]:
    """Return deterministic route-specific checks for a live result."""
    route = normalize_route(route_name)
    budget = route_latency_budget(route["ui_name"])
    retrieval_budget_s = (
        float(max_retrieval_s)
        if max_retrieval_s is not None
        else float(budget.get("retrieval_s", 8.0))
    )
    total_budget_s = (
        float(max_total_s)
        if max_total_s is not None
        else float(budget.get("total_s", 25.0))
    )
    generation_budget_s = (
        float(max_generation_s)
        if max_generation_s is not None
        else float(budget.get("generation_after_sources_s", 16.0))
    )
    sources = result.get("sources") or []
    answer = _text(result.get("answer"))
    trace_summary = extract_trace_summary(result.get("trace_events") or [])
    source_summary = summarize_sources(sources)
    source_cov = anchor_coverage(query_case, sources_joined_text(sources))
    answer_cov = anchor_coverage(query_case, answer)
    timings = result.get("timings_s") or {}
    total_s = float(timings.get("total") or 0.0)
    retrieval_s = timings.get("retrieval_done_sources")
    if retrieval_s is None:
        retrieval_s = trace_summary.get("local_rag_duration_s")
    retrieval_s = float(retrieval_s or 0.0)
    generation_s = timings.get("generation_after_sources")
    generation_s = float(generation_s) if generation_s is not None else None
    graph_advantage = trace_summary.get("graph_advantage") or {}

    issues: list[dict[str, str]] = []
    if result.get("error_events"):
        issues.append(
            _issue("fail", "error_events", f"error events: {result['error_events']}")
        )
    if source_summary["source_count"] <= 0:
        issues.append(_issue("fail", "no_sources", "route returned zero sources"))
    if answer and len(answer) < 80:
        issues.append(
            _issue("warn", "short_answer", f"answer has only {len(answer)} chars")
        )
    elif not answer and not result.get("stop_after_sources"):
        issues.append(_issue("fail", "no_answer", "route produced no answer tokens"))
    if total_s > total_budget_s:
        issues.append(
            _issue(
                "fail" if fail_on_total_budget else "warn",
                "total_over_budget",
                f"total {total_s:.2f}s > {total_budget_s:.2f}s",
            )
        )
    if retrieval_s > retrieval_budget_s:
        issues.append(
            _issue(
                "fail",
                "retrieval_over_budget",
                f"retrieval/source prework {retrieval_s:.2f}s > {retrieval_budget_s:.2f}s",
            )
        )
    if generation_s is not None and generation_s > generation_budget_s:
        issues.append(
            _issue(
                "fail" if fail_on_generation_budget else "warn",
                "generation_over_budget",
                f"generation after sources {generation_s:.2f}s > {generation_budget_s:.2f}s",
            )
        )
    if source_cov["required_anchor_count"] and source_cov["required_coverage"] < 0.5:
        issues.append(
            _issue(
                "warn",
                "weak_source_anchor_coverage",
                "retrieved sources cover less than half of required anchors",
            )
        )
    if answer and answer_cov["required_anchor_count"] and answer_cov["required_coverage"] < 0.5:
        issues.append(
            _issue(
                "warn",
                "weak_answer_anchor_coverage",
                "answer covers less than half of required anchors",
            )
        )

    if route["ui_name"] in {"Fast Search", "Hybrid Search"}:
        if trace_summary["has_graph_advantage"]:
            issues.append(
                _issue(
                    "fail",
                    "unexpected_graph_advantage",
                    f"{route['ui_name']} emitted Graph Advantage",
                )
            )
    if route["ui_name"] == "Graph Augmentation":
        if not trace_summary["has_graph_advantage"]:
            issues.append(
                _issue(
                    "fail",
                    "missing_graph_advantage",
                    "Graph Augmentation did not emit a Graph Advantage trace",
                )
            )
        facts = int(graph_advantage.get("facts_used") or 0)
        relations = int(graph_advantage.get("relations_used") or 0)
        expanded = int(graph_advantage.get("graph_expanded_chunks") or 0)
        if max(facts, relations, expanded) <= 0:
            issues.append(
                _issue(
                    "warn",
                    "weak_graph_signal",
                    "Graph Augmentation had no facts, relations, or expanded chunks",
                )
            )

    fail_count = sum(1 for issue in issues if issue["level"] == "fail")
    warn_count = sum(1 for issue in issues if issue["level"] == "warn")
    return {
        "status": "fail" if fail_count else "pass",
        "route": route["ui_name"],
        "tier": route["tier"],
        "purpose": route["purpose"],
        "issues": issues,
        "fail_count": fail_count,
        "warn_count": warn_count,
        "timings_s": {
            "total": round(total_s, 3),
            "retrieval_or_sources": round(retrieval_s, 3),
            "first_answer_token": timings.get("first_answer_token"),
            "generation_after_sources": (
                round(generation_s, 3) if generation_s is not None else None
            ),
        },
        "latency_budget_s": {
            "retrieval_or_sources": retrieval_budget_s,
            "total": total_budget_s,
            "generation_after_sources": generation_budget_s,
            "total_is_hard_fail": bool(fail_on_total_budget),
            "generation_is_hard_fail": bool(fail_on_generation_budget),
        },
        "source_summary": source_summary,
        "source_anchor_coverage": source_cov,
        "answer_anchor_coverage": answer_cov,
        "trace_summary": trace_summary,
    }


def summarize_report(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate live route results into UI-route summaries."""
    by_route: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        by_route.setdefault(result.get("route") or "Unknown", []).append(result)

    summaries: dict[str, Any] = {}
    for route, route_results in sorted(by_route.items()):
        timings = [
            float((res.get("validation") or {}).get("timings_s", {}).get("total") or 0.0)
            for res in route_results
        ]
        retrievals = [
            float(
                (res.get("validation") or {})
                .get("timings_s", {})
                .get("retrieval_or_sources")
                or 0.0
            )
            for res in route_results
        ]
        generations = [
            float(
                (res.get("validation") or {})
                .get("timings_s", {})
                .get("generation_after_sources")
                or 0.0
            )
            for res in route_results
        ]
        failures = sum(
            int((res.get("validation") or {}).get("fail_count") or 0)
            for res in route_results
        )
        warnings = sum(
            int((res.get("validation") or {}).get("warn_count") or 0)
            for res in route_results
        )
        summaries[route] = {
            "cases": len(route_results),
            "failures": failures,
            "warnings": warnings,
            "avg_total_s": round(sum(timings) / max(1, len(timings)), 3),
            "max_total_s": round(max(timings or [0.0]), 3),
            "avg_retrieval_or_sources_s": round(
                sum(retrievals) / max(1, len(retrievals)), 3
            ),
            "max_retrieval_or_sources_s": round(max(retrievals or [0.0]), 3),
            "avg_generation_after_sources_s": round(
                sum(generations) / max(1, len(generations)), 3
            ),
            "max_generation_after_sources_s": round(max(generations or [0.0]), 3),
        }
    return summaries
