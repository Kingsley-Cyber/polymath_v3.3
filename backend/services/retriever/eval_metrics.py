"""Offline retrieval evaluation metrics.

These helpers are intentionally not imported by the live retrieval path.
MRR/MAP/NDCG need relevance labels, so they belong in golden-set eval runs
where speed/quality tradeoffs can be measured without slowing user queries.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import log2
from statistics import mean
from typing import Any, Iterable, Mapping, Sequence


@dataclass(frozen=True)
class RetrievalEvalCase:
    query: str
    route: str
    ranked_ids: tuple[str, ...]
    relevance: Mapping[str, float]
    latency_ms: float | None = None
    answer_sufficient: bool | None = None


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


def route_metric_profile(route: str) -> dict[str, str]:
    """Document which metrics should steer each offline route eval."""

    normalized = route.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in {"fast", "fast_search", "vector", "vector_retrieval", "qdrant_only"}:
        return {
            "first_hit": "MRR@5",
            "candidate_pool": "MAP@20",
            "primary_goal": "fast broad recall",
        }
    if normalized in {"hybrid", "hybrid_search", "mongo_hybrid", "qdrant_mongo"}:
        return {
            "candidate_pool": "MAP@20",
            "final_context": "NDCG@8",
            "primary_goal": "precise multi-document text evidence",
        }
    if normalized in {
        "graph",
        "graph_augmentation",
        "qdrant_mongo_graph",
        "neo4j_graph",
    }:
        return {
            "candidate_pool": "MAP@20",
            "final_context": "NDCG@8",
            "answer": "answer_sufficiency",
            "primary_goal": "structured graph evidence quality",
        }
    return {
        "first_hit": "MRR@5",
        "candidate_pool": "MAP@20",
        "final_context": "NDCG@8",
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


def evaluate_case(
    case: RetrievalEvalCase,
    *,
    mrr_k: int = 5,
    map_k: int = 20,
    ndcg_k: int = 8,
) -> dict[str, Any]:
    return {
        "query": case.query,
        "route": case.route,
        f"MRR@{mrr_k}": reciprocal_rank_at_k(case.ranked_ids, case.relevance, k=mrr_k),
        f"MAP@{map_k}": average_precision_at_k(case.ranked_ids, case.relevance, k=map_k),
        f"NDCG@{ndcg_k}": ndcg_at_k(case.ranked_ids, case.relevance, k=ndcg_k),
        "latency_ms": case.latency_ms,
        "answer_sufficient": case.answer_sufficient,
        "retrieved": len(_dedupe_ranked_ids(case.ranked_ids)),
        "known_relevant": sum(1 for grade in _coerce_relevance(case.relevance).values() if grade > 0),
    }


def summarize_route_eval(
    cases: Sequence[RetrievalEvalCase],
    *,
    mrr_k: int = 5,
    map_k: int = 20,
    ndcg_k: int = 8,
) -> dict[str, Any]:
    """Aggregate offline retrieval metrics for a route/query set."""

    rows = [
        evaluate_case(case, mrr_k=mrr_k, map_k=map_k, ndcg_k=ndcg_k)
        for case in cases
    ]
    if not rows:
        return {
            "query_count": 0,
            "MRR@5": 0.0,
            "MAP@20": 0.0,
            "NDCG@8": 0.0,
            "answer_sufficiency_rate": None,
            "latency_p50_ms": None,
            "latency_p95_ms": None,
            "cases": [],
        }

    metric_names = [f"MRR@{mrr_k}", f"MAP@{map_k}", f"NDCG@{ndcg_k}"]
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
    return {
        "query_count": len(rows),
        **{name: mean(float(row[name]) for row in rows) for name in metric_names},
        "answer_sufficiency_rate": (
            mean(1.0 if flag else 0.0 for flag in answer_flags)
            if answer_flags
            else None
        ),
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
    if not ranked and isinstance(row.get("candidates"), Sequence):
        ranked = [
            item.get("chunk_id") or item.get("id")
            for item in row["candidates"]
            if isinstance(item, Mapping)
        ]
    return RetrievalEvalCase(
        query=str(row.get("query") or ""),
        route=str(row.get("route") or row.get("retrieval_tier") or ""),
        ranked_ids=tuple(_dedupe_ranked_ids(ranked)),
        relevance=_coerce_relevance(row.get("relevance") or row.get("qrels") or {}),
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
    )
