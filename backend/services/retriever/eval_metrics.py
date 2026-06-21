"""Offline retrieval evaluation metrics.

These helpers are intentionally not imported by the live retrieval path.
MRR/MAP/NDCG need relevance labels, so they belong in golden-set eval runs
where speed/quality tradeoffs can be measured without slowing user queries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import log2
from statistics import mean
from typing import Any, Iterable, Mapping, Sequence


@dataclass(frozen=True)
class RetrievalEvalCase:
    query: str
    route: str
    ranked_ids: tuple[str, ...]
    relevance: Mapping[str, float]
    exact_source_ids: tuple[str, ...] = ()
    latency_ms: float | None = None
    answer_sufficient: bool | None = None
    doc_ids: tuple[str, ...] = ()
    diagnostics: Mapping[str, Any] = field(default_factory=dict)


def _clean_id(value: Any) -> str:
    return str(value or "").strip()


def _dedupe_ranked_ids(ranked_ids: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in ranked_ids:
        item_id = _clean_id(raw)
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        out.append(item_id)
    return out


def _coerce_relevance(relevance: Mapping[str, Any] | Iterable[Any]) -> dict[str, float]:
    if isinstance(relevance, Mapping):
        out: dict[str, float] = {}
        for key, value in relevance.items():
            item_id = _clean_id(key)
            if not item_id:
                continue
            try:
                out[item_id] = float(value)
            except (TypeError, ValueError):
                out[item_id] = 1.0 if value else 0.0
        return out
    return {_clean_id(item): 1.0 for item in relevance if _clean_id(item)}


def reciprocal_rank_at_k(
    ranked_ids: Sequence[Any],
    relevance: Mapping[str, Any] | Iterable[Any],
    *,
    k: int = 5,
) -> float:
    """MRR component for one query: first relevant hit within top-k."""

    rel = _coerce_relevance(relevance)
    relevant_ids = {item_id for item_id, grade in rel.items() if grade > 0}
    if not relevant_ids or k <= 0:
        return 0.0
    for rank, item_id in enumerate(_dedupe_ranked_ids(ranked_ids)[:k], start=1):
        if item_id in relevant_ids:
            return 1.0 / rank
    return 0.0


def average_precision_at_k(
    ranked_ids: Sequence[Any],
    relevance: Mapping[str, Any] | Iterable[Any],
    *,
    k: int = 20,
) -> float:
    """AP@k for one query.

    Relevance is binary for MAP. Graded labels greater than zero count as
    relevant. The denominator is min(number of known relevant ids, k), which
    is the usual bounded AP@k convention and keeps MAP@20 interpretable when a
    topic has more than 20 known relevant chunks.
    """

    rel = _coerce_relevance(relevance)
    relevant_ids = {item_id for item_id, grade in rel.items() if grade > 0}
    if not relevant_ids or k <= 0:
        return 0.0

    hits = 0
    precision_sum = 0.0
    for rank, item_id in enumerate(_dedupe_ranked_ids(ranked_ids)[:k], start=1):
        if item_id not in relevant_ids:
            continue
        hits += 1
        precision_sum += hits / rank

    return precision_sum / min(len(relevant_ids), k)


def recall_at_k(
    ranked_ids: Sequence[Any],
    relevance: Mapping[str, Any] | Iterable[Any],
    *,
    k: int = 20,
) -> float:
    """Recall@k for known relevant chunk ids."""

    rel = _coerce_relevance(relevance)
    relevant_ids = {item_id for item_id, grade in rel.items() if grade > 0}
    if not relevant_ids or k <= 0:
        return 0.0
    retrieved = set(_dedupe_ranked_ids(ranked_ids)[:k])
    return len(retrieved & relevant_ids) / len(relevant_ids)


def exact_source_recall_at_k(
    ranked_ids: Sequence[Any],
    exact_source_ids: Sequence[Any] | Iterable[Any],
    *,
    k: int = 8,
) -> float | None:
    """Exact source-recovery slice.

    This is separate from relevance. Relevance can include broad supporting
    chunks; exact_source_ids are the gold chunks/passages that should be
    recovered for exact-span/source-recovery tests. Returns None when the eval
    row does not provide exact-source labels.
    """

    gold = {_clean_id(item) for item in exact_source_ids if _clean_id(item)}
    if not gold:
        return None
    if k <= 0:
        return 0.0
    retrieved = set(_dedupe_ranked_ids(ranked_ids)[:k])
    return len(retrieved & gold) / len(gold)


def exact_source_hit_at_k(
    ranked_ids: Sequence[Any],
    exact_source_ids: Sequence[Any] | Iterable[Any],
    *,
    k: int = 8,
) -> float | None:
    """1.0 when at least one exact gold source is recovered in top-k."""

    recall = exact_source_recall_at_k(ranked_ids, exact_source_ids, k=k)
    if recall is None:
        return None
    return 1.0 if recall > 0.0 else 0.0


def mean_average_precision_at_k(
    runs: Sequence[tuple[Sequence[Any], Mapping[str, Any] | Iterable[Any]]],
    *,
    k: int = 20,
) -> float:
    if not runs:
        return 0.0
    return mean(average_precision_at_k(ranked, rel, k=k) for ranked, rel in runs)


def dcg_at_k(
    ranked_ids: Sequence[Any],
    relevance: Mapping[str, Any] | Iterable[Any],
    *,
    k: int = 8,
) -> float:
    rel = _coerce_relevance(relevance)
    if k <= 0:
        return 0.0

    total = 0.0
    for rank, item_id in enumerate(_dedupe_ranked_ids(ranked_ids)[:k], start=1):
        grade = max(0.0, float(rel.get(item_id, 0.0)))
        if grade <= 0:
            continue
        gain = (2.0**grade) - 1.0
        total += gain / log2(rank + 1)
    return total


def ndcg_at_k(
    ranked_ids: Sequence[Any],
    relevance: Mapping[str, Any] | Iterable[Any],
    *,
    k: int = 8,
) -> float:
    rel = _coerce_relevance(relevance)
    if k <= 0 or not rel:
        return 0.0

    ideal_grades = sorted((max(0.0, grade) for grade in rel.values()), reverse=True)
    ideal_ranked = [f"ideal:{idx}" for idx, _ in enumerate(ideal_grades)]
    ideal_rel = {item_id: grade for item_id, grade in zip(ideal_ranked, ideal_grades)}
    ideal = dcg_at_k(ideal_ranked, ideal_rel, k=k)
    if ideal <= 0:
        return 0.0
    return dcg_at_k(ranked_ids, rel, k=k) / ideal


def route_metric_profile(route: str) -> dict[str, Any]:
    """Document which metrics should steer each offline route eval."""

    normalized = route.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in {"fast", "fast_search", "vector", "vector_retrieval", "qdrant_only"}:
        return {
            "optimize_for": ["MRR@5", "Recall@20", "latency_p95_ms"],
            "first_hit": "MRR@5",
            "candidate_recall": "Recall@20",
            "latency": "latency_p95_ms",
            "secondary_diagnostics": ["MAP@20", "NDCG@8", "ExactSourceRecall@8"],
            "primary_goal": "fast broad recall",
        }
    if normalized in {"hybrid", "hybrid_search", "mongo_hybrid", "qdrant_mongo"}:
        return {
            "optimize_for": [
                "MRR@5",
                "Recall@20",
                "MAP@20",
                "NDCG@8",
                "ExactSourceRecall@8",
                "unique_doc_count",
                "near_duplicate_rate",
            ],
            "first_hit": "MRR@5",
            "candidate_recall": "Recall@20 + MAP@20",
            "final_context": "NDCG@8",
            "exact_source_recovery": "ExactSourceRecall@8",
            "source_diversity": "unique_doc_count",
            "secondary_diagnostics": ["latency_p95_ms"],
            "primary_goal": "precise multi-document text evidence",
        }
    if normalized in {
        "graph",
        "graph_augmentation",
        "qdrant_mongo_graph",
        "neo4j_graph",
    }:
        return {
            "optimize_for": [
                "NDCG@8",
                "answer_sufficiency_rate",
                "ExactSourceRecall@8",
                "graph_advantage",
                "atom_coverage",
                "facts_used",
                "relations_used",
                "multi_doc_evidence_rate",
                "near_duplicate_rate",
                "latency_p95_ms",
            ],
            "final_context": "NDCG@8",
            "answer": "answer_sufficiency",
            "exact_source_recovery": "ExactSourceRecall@8",
            "graph_advantage": "facts + relations + atoms + multi-doc support",
            "secondary_diagnostics": ["MRR@5", "Recall@20", "MAP@20"],
            "primary_goal": "structured graph evidence quality",
        }
    return {
        "optimize_for": ["MRR@5", "Recall@20", "NDCG@8", "latency_p95_ms"],
        "first_hit": "MRR@5",
        "candidate_recall": "Recall@20",
        "final_context": "NDCG@8",
        "secondary_diagnostics": ["MAP@20", "ExactSourceRecall@8"],
        "primary_goal": "generic retrieval quality",
    }


def _percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return float(values[0])
    ordered = sorted(float(v) for v in values)
    q = min(1.0, max(0.0, q))
    pos = q * (len(ordered) - 1)
    lower = int(pos)
    upper = min(lower + 1, len(ordered) - 1)
    if lower == upper:
        return ordered[lower]
    frac = pos - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * frac


def _optional_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_number(*values: Any) -> float | None:
    for value in values:
        number = _optional_number(value)
        if number is not None:
            return number
    return None


def _unique_doc_count(case: RetrievalEvalCase) -> int | None:
    explicit = _optional_number(case.diagnostics.get("unique_doc_count"))
    if explicit is not None:
        return int(explicit)
    docs = {_clean_id(doc_id) for doc_id in case.doc_ids if _clean_id(doc_id)}
    if docs:
        return len(docs)
    return None


def _multi_doc_evidence(case: RetrievalEvalCase) -> bool | None:
    explicit = case.diagnostics.get("multi_doc_evidence")
    if explicit is not None:
        return bool(explicit)
    count = _unique_doc_count(case)
    if count is None:
        return None
    return count >= 2


def _case_diagnostic(case: RetrievalEvalCase, key: str) -> float | None:
    return _optional_number(case.diagnostics.get(key))


def evaluate_case(
    case: RetrievalEvalCase,
    *,
    mrr_k: int = 5,
    recall_k: int = 20,
    map_k: int = 20,
    ndcg_k: int = 8,
    source_k: int = 8,
) -> dict[str, Any]:
    unique_docs = _unique_doc_count(case)
    multi_doc = _multi_doc_evidence(case)
    exact_source_recall = exact_source_recall_at_k(
        case.ranked_ids,
        case.exact_source_ids,
        k=source_k,
    )
    exact_source_hit = exact_source_hit_at_k(
        case.ranked_ids,
        case.exact_source_ids,
        k=source_k,
    )
    return {
        "query": case.query,
        "route": case.route,
        f"MRR@{mrr_k}": reciprocal_rank_at_k(case.ranked_ids, case.relevance, k=mrr_k),
        f"Recall@{recall_k}": recall_at_k(case.ranked_ids, case.relevance, k=recall_k),
        f"MAP@{map_k}": average_precision_at_k(case.ranked_ids, case.relevance, k=map_k),
        f"NDCG@{ndcg_k}": ndcg_at_k(case.ranked_ids, case.relevance, k=ndcg_k),
        f"ExactSourceRecall@{source_k}": exact_source_recall,
        f"ExactSourceHit@{source_k}": exact_source_hit,
        "latency_ms": case.latency_ms,
        "answer_sufficient": case.answer_sufficient,
        "unique_doc_count": unique_docs,
        "multi_doc_evidence": multi_doc,
        "atom_coverage": _case_diagnostic(case, "atom_coverage"),
        "facts_used": _case_diagnostic(case, "facts_used"),
        "relations_used": _case_diagnostic(case, "relations_used"),
        "graph_advantage": _case_diagnostic(case, "graph_advantage"),
        "near_duplicate_rate": _case_diagnostic(case, "near_duplicate_rate"),
        "retrieved": len(_dedupe_ranked_ids(case.ranked_ids)),
        "known_relevant": sum(1 for grade in _coerce_relevance(case.relevance).values() if grade > 0),
    }


def summarize_route_eval(
    cases: Sequence[RetrievalEvalCase],
    *,
    mrr_k: int = 5,
    recall_k: int = 20,
    map_k: int = 20,
    ndcg_k: int = 8,
    source_k: int = 8,
) -> dict[str, Any]:
    """Aggregate offline retrieval metrics for a route/query set."""

    rows = [
        evaluate_case(
            case,
            mrr_k=mrr_k,
            recall_k=recall_k,
            map_k=map_k,
            ndcg_k=ndcg_k,
            source_k=source_k,
        )
        for case in cases
    ]
    if not rows:
        return {
            "query_count": 0,
            f"MRR@{mrr_k}": 0.0,
            f"Recall@{recall_k}": 0.0,
            f"MAP@{map_k}": 0.0,
            f"NDCG@{ndcg_k}": 0.0,
            f"ExactSourceRecall@{source_k}": None,
            f"ExactSourceHit@{source_k}": None,
            "answer_sufficiency_rate": None,
            "unique_doc_count_avg": None,
            "multi_doc_evidence_rate": None,
            "atom_coverage_avg": None,
            "facts_used_avg": None,
            "relations_used_avg": None,
            "graph_advantage_avg": None,
            "near_duplicate_rate_avg": None,
            "latency_p50_ms": None,
            "latency_p95_ms": None,
            "cases": [],
        }

    metric_names = [
        f"MRR@{mrr_k}",
        f"Recall@{recall_k}",
        f"MAP@{map_k}",
        f"NDCG@{ndcg_k}",
    ]
    latencies = [
        float(row["latency_ms"])
        for row in rows
        if row.get("latency_ms") is not None
    ]
    answer_flags = [
        bool(row["answer_sufficient"])
        for row in rows
        if row.get("answer_sufficient") is not None
    ]
    multi_doc_flags = [
        bool(row["multi_doc_evidence"])
        for row in rows
        if row.get("multi_doc_evidence") is not None
    ]

    def average_present(key: str) -> float | None:
        values = [float(row[key]) for row in rows if row.get(key) is not None]
        return mean(values) if values else None

    return {
        "query_count": len(rows),
        **{name: mean(float(row[name]) for row in rows) for name in metric_names},
        f"ExactSourceRecall@{source_k}": average_present(f"ExactSourceRecall@{source_k}"),
        f"ExactSourceHit@{source_k}": average_present(f"ExactSourceHit@{source_k}"),
        "answer_sufficiency_rate": (
            mean(1.0 if flag else 0.0 for flag in answer_flags)
            if answer_flags
            else None
        ),
        "unique_doc_count_avg": average_present("unique_doc_count"),
        "multi_doc_evidence_rate": (
            mean(1.0 if flag else 0.0 for flag in multi_doc_flags)
            if multi_doc_flags
            else None
        ),
        "atom_coverage_avg": average_present("atom_coverage"),
        "facts_used_avg": average_present("facts_used"),
        "relations_used_avg": average_present("relations_used"),
        "graph_advantage_avg": average_present("graph_advantage"),
        "near_duplicate_rate_avg": average_present("near_duplicate_rate"),
        "latency_p50_ms": _percentile(latencies, 0.50),
        "latency_p95_ms": _percentile(latencies, 0.95),
        "cases": rows,
    }


def case_from_mapping(row: Mapping[str, Any]) -> RetrievalEvalCase:
    ranked = (
        row.get("ranked_chunk_ids")
        or row.get("ranked_ids")
        or row.get("chunk_ids")
        or []
    )
    candidates = row.get("candidates") if isinstance(row.get("candidates"), Sequence) else []
    if not ranked and candidates:
        ranked = [
            item.get("chunk_id") or item.get("id")
            for item in candidates
            if isinstance(item, Mapping)
        ]
    doc_ids = row.get("doc_ids") or row.get("final_doc_ids") or []
    if not doc_ids and candidates:
        doc_ids = [
            item.get("doc_id") or item.get("document_id")
            for item in candidates
            if isinstance(item, Mapping)
        ]
    exact_source_ids = (
        row.get("exact_source_ids")
        or row.get("exact_chunk_ids")
        or row.get("target_chunk_ids")
        or row.get("gold_chunk_ids")
        or []
    )
    evidence_delta = row.get("evidence_delta") if isinstance(row.get("evidence_delta"), Mapping) else {}
    graph_advantage = row.get("graph_advantage") if isinstance(row.get("graph_advantage"), Mapping) else {}
    answerability = row.get("answerability") if isinstance(row.get("answerability"), Mapping) else {}
    diagnostics = {
        "unique_doc_count": _first_number(
            row.get("unique_doc_count"),
            row.get("final_docs"),
            evidence_delta.get("final_docs"),
        ),
        "atom_coverage": _first_number(
            row.get("atom_coverage"),
            row.get("answer_atom_coverage"),
            answerability.get("atom_coverage"),
        ),
        "facts_used": _first_number(
            row.get("facts_used"),
            graph_advantage.get("facts_used"),
            evidence_delta.get("neo4j_facts"),
        ),
        "relations_used": _first_number(
            row.get("relations_used"),
            graph_advantage.get("relations_used"),
            evidence_delta.get("neo4j_relations"),
        ),
        "graph_advantage": _first_number(
            row.get("graph_advantage_score"),
            graph_advantage.get("score"),
            graph_advantage.get("graph_advantage"),
        ),
        "near_duplicate_rate": _first_number(
            row.get("near_duplicate_rate"),
            row.get("near_duplicates"),
            evidence_delta.get("near_duplicate_rate"),
        ),
    }
    if row.get("multi_doc_evidence") is not None:
        diagnostics["multi_doc_evidence"] = bool(row.get("multi_doc_evidence"))
    diagnostics = {key: value for key, value in diagnostics.items() if value is not None}
    return RetrievalEvalCase(
        query=str(row.get("query") or ""),
        route=str(row.get("route") or row.get("retrieval_tier") or ""),
        ranked_ids=tuple(_dedupe_ranked_ids(ranked)),
        relevance=_coerce_relevance(row.get("relevance") or row.get("qrels") or {}),
        exact_source_ids=tuple(_dedupe_ranked_ids(exact_source_ids)),
        latency_ms=(
            float(row["latency_ms"])
            if row.get("latency_ms") is not None
            else (
                float(row["latency_s"]) * 1000.0
                if row.get("latency_s") is not None
                else None
            )
        ),
        answer_sufficient=(
            bool(row["answer_sufficient"])
            if row.get("answer_sufficient") is not None
            else (
                bool(row["answerability_pass"])
                if row.get("answerability_pass") is not None
                else None
            )
        ),
        doc_ids=tuple(_dedupe_ranked_ids(doc_ids)),
        diagnostics=diagnostics,
    )
