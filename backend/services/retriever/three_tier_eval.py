"""Deterministic validators for the three UI retrieval routes.

The live runner in ``scripts/retrieval_three_tier_eval.py`` calls the real
``/api/chat`` SSE endpoint. This module stays pure so unit tests can exercise
the route contracts without needing Qdrant, MongoDB, Neo4j, or an LLM.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from services.retriever.eval_metrics import (
    average_precision_at_k,
    ndcg_at_k,
    recall_at_k,
    reciprocal_rank_at_k,
)


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
        "retrieval_s": 2.0,
        "total_s": 20.0,
        "generation_after_sources_s": 14.0,
    },
    "Hybrid Search": {
        "retrieval_s": 8.0,
        "total_s": 20.0,
        "generation_after_sources_s": 14.0,
    },
    "Graph Augmentation": {
        "retrieval_s": 10.0,
        "total_s": 25.0,
        "generation_after_sources_s": 16.0,
    },
}


def normalize_route(value: str | None) -> dict[str, str]:
    """Resolve either a UI route name or backend tier value."""
    raw = str(value or "").strip()
    return (
        ROUTE_BY_NAME.get(raw)
        or ROUTE_BY_TIER.get(raw)
        or {
            "ui_name": raw or "Unknown",
            "tier": raw or "unknown",
            "purpose": "unknown",
        }
    )


def route_latency_budget(route_name: str | None) -> dict[str, float]:
    route = normalize_route(route_name)
    return dict(ROUTE_LATENCY_BUDGETS.get(route["ui_name"], {}))


def _text(value: Any) -> str:
    return str(value or "")


def _norm(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", value.casefold())
    return re.sub(r"\s+", " ", normalized).strip()


def _source_text(source: dict[str, Any]) -> str:
    body = _text(
        source.get("text")
        or source.get("content")
        or source.get("chunk_text")
        or source.get("summary")
    )
    attribution = " ".join(
        _text(source.get(key))
        for key in ("doc_name", "document_name", "title", "corpus_name")
        if source.get(key)
    )
    return "\n".join(part for part in (attribution, body) if part)


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
        parent = _text(
            source.get("parent_id") or source.get("parent_chunk_id") or "none"
        )
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
            terms = [
                str(term) for term in group.get("terms") or [] if str(term).strip()
            ]
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
    local_rag_events = [
        event for event in trace_events if event.get("title") == "Local RAG retrieval"
    ]
    local_rag = next(
        (
            event
            for event in reversed(local_rag_events)
            if (event.get("metadata") or {}).get("retrieval_diagnostics")
        ),
        local_rag_events[-1] if local_rag_events else {},
    )
    graph_event = next(
        (
            event
            for event in trace_events
            if event.get("title") in {"Graph Advantage", "Graph Augmentation"}
        ),
        {},
    )
    local_meta = local_rag.get("metadata") or {}
    graph_meta = graph_event.get("metadata") or {}
    graph_signal = bool(
        graph_event.get("title") == "Graph Advantage"
        and graph_meta.get("advantage_established", True)
    )
    diagnostics = local_meta.get("retrieval_diagnostics") or {}
    return {
        "trace_titles": titles,
        "has_local_rag_trace": bool(local_rag),
        "has_graph_trace": bool(graph_event),
        "has_graph_advantage": graph_signal,
        "effective_tier": _text(local_meta.get("effective_tier")),
        "local_rag_duration_s": local_meta.get("duration_s"),
        "retrieval_diagnostics": diagnostics,
        "graph_advantage": graph_meta,
    }


def _issue(level: str, code: str, message: str) -> dict[str, str]:
    return {"level": level, "code": code, "message": message}


def _expectation_groups(values: Any) -> list[list[str]]:
    groups: list[list[str]] = []
    for value in values or []:
        if isinstance(value, (list, tuple, set)):
            group = [_norm(str(item)) for item in value if _norm(str(item))]
        else:
            group = [_norm(str(value))] if _norm(str(value)) else []
        if group:
            groups.append(group)
    return groups


def grounding_quality(
    query_case: dict[str, Any],
    sources: list[dict[str, Any]],
    diagnostics: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate vocabulary, routing, coverage, and context precision contracts."""

    vocabulary = diagnostics.get("vocabulary_resolution") or {}
    matches = [
        row for row in (vocabulary.get("matches") or []) if isinstance(row, dict)
    ]
    matched_surface = _norm(
        " ".join(
            str(value)
            for row in matches
            for value in [
                row.get("term") or row.get("canonical_name"),
                *(row.get("aliases") or []),
                *(row.get("abbreviations") or []),
            ]
            if value
        )
    )
    expected_lexicon = _expectation_groups(
        query_case.get("expected_lexicon_terms")
    )
    optional_lexicon = _expectation_groups(
        query_case.get("optional_lexicon_terms")
    )
    forbidden_lexicon = _expectation_groups(
        query_case.get("forbidden_lexicon_terms")
    )

    def group_hits(groups: list[list[str]], haystack: str) -> list[bool]:
        return [any(term and term in haystack for term in group) for group in groups]

    expected_hits = group_hits(expected_lexicon, matched_surface)
    optional_hits = group_hits(optional_lexicon, matched_surface)
    forbidden_hits = group_hits(forbidden_lexicon, matched_surface)

    routed = (diagnostics.get("document_routing") or {}).get("routes") or {}
    routed_rows = [
        row
        for rows in routed.values()
        for row in (rows or [])
        if isinstance(row, dict)
    ]
    routed_text = _norm(
        " ".join(
            " ".join(
                [
                    str(row.get("doc_id") or ""),
                    str(row.get("title") or ""),
                    " ".join(str(value) for value in (row.get("concepts") or [])),
                ]
            )
            for row in routed_rows
        )
    )
    document_expectations = _expectation_groups(
        query_case.get("document_route_patterns")
    )
    document_hits = group_hits(document_expectations, routed_text)

    expected_corpora = {
        str(value)
        for value in (query_case.get("expected_corpus_ids") or [])
        if str(value)
    }
    represented_corpora = {
        str(source.get("corpus_id") or "")
        for source in sources
        if str(source.get("corpus_id") or "")
    }
    ranked_doc_ids = list(
        dict.fromkeys(
            str(source.get("doc_id") or "")
            for source in sources
            if str(source.get("doc_id") or "")
        )
    )
    document_relevance = {
        str(doc_id): float(grade)
        for doc_id, grade in (query_case.get("document_relevance") or {}).items()
        if str(doc_id)
    }

    required_groups = [
        group
        for group in _anchor_groups(query_case)
        if group.get("required", True)
    ]
    relevant_source_count = 0
    for source in sources:
        if document_relevance:
            relevant = document_relevance.get(str(source.get("doc_id") or ""), 0) > 0
        else:
            source_text = _norm(_source_text(source))
            relevant = not required_groups or any(
                any(_norm(term) in source_text for term in group.get("terms") or [])
                for group in required_groups
            )
        if relevant:
            relevant_source_count += 1

    required_lane_coverage = float(
        (diagnostics.get("required_concept_coverage") or {}).get("coverage") or 0.0
    )
    expansion = vocabulary.get("expansion") or {}
    grounded_planner = vocabulary.get("grounded_planner") or {}
    return {
        "lexicon_recall": round(
            sum(expected_hits) / max(1, len(expected_hits)), 3
        ) if expected_lexicon else None,
        "expected_lexicon_hits": expected_hits,
        "optional_lexicon_hits": optional_hits,
        "forbidden_lexicon_hits": forbidden_hits,
        "matched_lexicon_ids": [
            str(row.get("lexicon_id") or "") for row in matches if row.get("lexicon_id")
        ],
        "matched_lexicon_terms": [
            str(row.get("term") or row.get("canonical_name") or "")
            for row in matches
            if row.get("term") or row.get("canonical_name")
        ],
        "rejected_expansion_count": len(vocabulary.get("rejected_expansions") or []),
        "translation_lane_count": len(expansion.get("translation_lane_ids") or []),
        "step_back_lane_count": len(expansion.get("step_back_lane_ids") or []),
        "exploratory_expansions_required": bool(expansion.get("required", False)),
        "document_route_recall": round(
            sum(document_hits) / max(1, len(document_hits)), 3
        ) if document_expectations else None,
        "document_route_hits": document_hits,
        "routed_document_count": len(
            {
                (str(row.get("corpus_id") or ""), str(row.get("doc_id") or ""))
                for row in routed_rows
            }
        ),
        "required_lane_coverage": round(required_lane_coverage, 3),
        "context_precision": round(
            relevant_source_count / max(1, len(sources)), 3
        ),
        "document_MRR@5": (
            round(reciprocal_rank_at_k(ranked_doc_ids, document_relevance, k=5), 4)
            if document_relevance
            else None
        ),
        "document_Recall@20": (
            round(recall_at_k(ranked_doc_ids, document_relevance, k=20), 4)
            if document_relevance
            else None
        ),
        "document_MAP@20": (
            round(average_precision_at_k(ranked_doc_ids, document_relevance, k=20), 4)
            if document_relevance
            else None
        ),
        "document_NDCG@8": (
            round(ndcg_at_k(ranked_doc_ids, document_relevance, k=8), 4)
            if document_relevance
            else None
        ),
        "expected_corpus_coverage": round(
            len(expected_corpora & represented_corpora) / max(1, len(expected_corpora)),
            3,
        ) if expected_corpora else None,
        "represented_corpus_ids": sorted(represented_corpora),
        "provider_calls": int(grounded_planner.get("provider_calls") or 0),
        "planner_cache_hit": bool(grounded_planner.get("cache_hit")),
    }


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
    retrieval_diagnostics = trace_summary.get("retrieval_diagnostics") or {}
    grounding = grounding_quality(query_case, sources, retrieval_diagnostics)
    sufficiency = (retrieval_diagnostics.get("selection") or {}).get(
        "sufficiency"
    ) or {}
    grounded_abstention = bool(
        answer
        and "cannot answer that as a source-backed result" in answer.casefold()
        and sufficiency.get("answerable") is False
    )
    expected_empty = route["ui_name"] in set(
        query_case.get("expected_empty_routes") or []
    )

    issues: list[dict[str, str]] = []
    if result.get("error_events"):
        issues.append(
            _issue("fail", "error_events", f"error events: {result['error_events']}")
        )
    if source_summary["source_count"] <= 0 and not expected_empty:
        issues.append(_issue("fail", "no_sources", "route returned zero sources"))
    elif source_summary["source_count"] > 0 and expected_empty:
        issues.append(
            _issue(
                "fail",
                "unexpected_sources",
                "route returned evidence where its contract requires abstention",
            )
        )
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
    if (
        not expected_empty
        and source_cov["required_anchor_count"]
        and source_cov["required_coverage"] < 0.5
    ):
        issues.append(
            _issue(
                "warn",
                "weak_source_anchor_coverage",
                "retrieved sources cover less than half of required anchors",
            )
        )

    minimum_lexicon_recall = float(query_case.get("min_lexicon_recall") or 0.0)
    if (
        grounding["lexicon_recall"] is not None
        and grounding["lexicon_recall"] < minimum_lexicon_recall
    ):
        issues.append(
            _issue(
                "fail",
                "lexicon_recall_below_threshold",
                f"lexicon recall {grounding['lexicon_recall']:.3f} < {minimum_lexicon_recall:.3f}",
            )
        )
    if any(grounding["forbidden_lexicon_hits"]):
        issues.append(
            _issue(
                "fail",
                "forbidden_vocabulary_expansion",
                "a negative-control vocabulary concept was introduced",
            )
        )
    if grounding["exploratory_expansions_required"]:
        issues.append(
            _issue(
                "fail",
                "exploratory_expansion_became_required",
                "an exploratory vocabulary expansion became a required claim",
            )
        )
    minimum_route_recall = float(query_case.get("min_document_route_recall") or 0.0)
    if (
        grounding["document_route_recall"] is not None
        and grounding["document_route_recall"] < minimum_route_recall
    ):
        issues.append(
            _issue(
                "fail",
                "document_route_recall_below_threshold",
                f"document route recall {grounding['document_route_recall']:.3f} < {minimum_route_recall:.3f}",
            )
        )
    minimum_lane_coverage = float(
        query_case.get("min_required_lane_coverage") or 0.0
    )
    if grounding["required_lane_coverage"] < minimum_lane_coverage:
        issues.append(
            _issue(
                "fail",
                "required_lane_coverage_below_threshold",
                f"required lane coverage {grounding['required_lane_coverage']:.3f} < {minimum_lane_coverage:.3f}",
            )
        )
    minimum_context_precision = float(query_case.get("min_context_precision") or 0.0)
    if sources and grounding["context_precision"] < minimum_context_precision:
        issues.append(
            _issue(
                "fail",
                "context_precision_below_threshold",
                f"context precision {grounding['context_precision']:.3f} < {minimum_context_precision:.3f}",
            )
        )
    for field, case_key, code in (
        ("document_MRR@5", "min_document_mrr", "document_mrr_below_threshold"),
        (
            "document_Recall@20",
            "min_document_recall",
            "document_recall_below_threshold",
        ),
        ("document_NDCG@8", "min_document_ndcg", "document_ndcg_below_threshold"),
    ):
        observed = grounding.get(field)
        minimum = query_case.get(case_key)
        if observed is None or minimum is None:
            continue
        if float(observed) < float(minimum):
            issues.append(
                _issue(
                    "fail",
                    code,
                    f"{field} {float(observed):.3f} < {float(minimum):.3f}",
                )
            )
    if (
        grounding["expected_corpus_coverage"] is not None
        and grounding["expected_corpus_coverage"] < 1.0
    ):
        issues.append(
            _issue(
                "fail",
                "selected_corpus_representation_missing",
                "one or more required corpora were absent from final evidence",
            )
        )
    if (
        answer
        and not grounded_abstention
        and answer_cov["required_anchor_count"]
        and answer_cov["required_coverage"] < 0.5
    ):
        issues.append(
            _issue(
                "warn",
                "weak_answer_anchor_coverage",
                "answer covers less than half of required anchors",
            )
        )
    # A supported answer must carry the concepts that retrieval established.
    # This catches fluent synthesis drift that source-only metrics cannot see
    # while preserving deliberate abstention for unsupported questions.
    if (
        answer
        and not grounded_abstention
        and answer_cov["required_anchor_count"]
        and source_cov["required_coverage"] >= 0.9
        and answer_cov["required_coverage"] < 0.9
    ):
        issues.append(
            _issue(
                "fail",
                "required_answer_anchor_missing",
                "answer omitted a required concept established by its sources",
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
        if not trace_summary["has_graph_trace"]:
            issues.append(
                _issue(
                    "fail",
                    "missing_graph_trace",
                    "Graph Augmentation did not emit a graph trace",
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
        "expected_empty": expected_empty,
        "grounded_abstention": grounded_abstention,
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
        "grounding_quality": grounding,
        "trace_summary": trace_summary,
    }


def summarize_report(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate live route results into UI-route summaries."""

    def percentile(values: list[float], fraction: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        position = max(0.0, min(1.0, fraction)) * (len(ordered) - 1)
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        weight = position - lower
        return ordered[lower] * (1.0 - weight) + ordered[upper] * weight

    by_route: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        by_route.setdefault(result.get("route") or "Unknown", []).append(result)

    summaries: dict[str, Any] = {}
    for route, route_results in sorted(by_route.items()):
        timings = [
            float(
                (res.get("validation") or {}).get("timings_s", {}).get("total") or 0.0
            )
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
            "p50_total_s": round(percentile(timings, 0.50), 3),
            "p95_total_s": round(percentile(timings, 0.95), 3),
            "max_total_s": round(max(timings or [0.0]), 3),
            "avg_retrieval_or_sources_s": round(
                sum(retrievals) / max(1, len(retrievals)), 3
            ),
            "p50_retrieval_or_sources_s": round(
                percentile(retrievals, 0.50), 3
            ),
            "p95_retrieval_or_sources_s": round(
                percentile(retrievals, 0.95), 3
            ),
            "max_retrieval_or_sources_s": round(max(retrievals or [0.0]), 3),
            "avg_generation_after_sources_s": round(
                sum(generations) / max(1, len(generations)), 3
            ),
            "p50_generation_after_sources_s": round(
                percentile(generations, 0.50), 3
            ),
            "p95_generation_after_sources_s": round(
                percentile(generations, 0.95), 3
            ),
            "max_generation_after_sources_s": round(max(generations or [0.0]), 3),
        }
    return summaries
