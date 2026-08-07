"""Per-obligation weighted RRF + bridges/contradictions (Phases 7–8)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

from models.complex_query import (
    ContradictionBundleV1,
    CrossDomainBridgeV1,
    GraphPathResultV1,
    SubQueryResultV1,
    TypedObligationV1,
)


@dataclass
class ObligationFusionResult:
    obligation_id: str
    ranked_child_ids: list[str] = field(default_factory=list)
    lane_contributions: dict[str, list[str]] = field(default_factory=dict)
    score_by_child: dict[str, float] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)


def _rrf(
    ranked_lists: dict[str, list[str]],
    *,
    weights: dict[str, float],
    k: float = 60.0,
) -> dict[str, float]:
    scores: dict[str, float] = defaultdict(float)
    for lane, ids in ranked_lists.items():
        w = float(weights.get(lane, 0.5))
        for rank, cid in enumerate(ids):
            if not cid:
                continue
            scores[cid] += w * (1.0 / (k + rank + 1))
    return dict(scores)


def fuse_obligation(
    *,
    obligation: TypedObligationV1,
    subquery_results: Iterable[SubQueryResultV1],
    settings: Any | None = None,
) -> ObligationFusionResult:
    if settings is None:
        from config import get_settings

        settings = get_settings()
    from services.retriever.cross_domain_rrf import planned_retriever_rrf_weights, rrf_k

    weights_cfg = planned_retriever_rrf_weights(settings)
    weights = {
        "direct": weights_cfg.get("dense", 1.0),
        "vocabulary": weights_cfg.get("trusted_canonical", 0.8),
        "lexical": weights_cfg.get("lexical", 0.8),
        "summary": weights_cfg.get("summary", 0.65),
        "graph": weights_cfg.get("graph", 0.85),
    }
    lists: dict[str, list[str]] = {
        "direct": [],
        "vocabulary": [],
        "lexical": [],
        "summary": [],
        "graph": [],
    }
    for res in subquery_results:
        if res.obligation_id != obligation.obligation_id:
            continue
        lists["direct"].extend(res.direct_child_ids)
        lists["vocabulary"].extend(res.vocabulary_child_ids)
        lists["lexical"].extend(res.lexical_child_ids)
        lists["summary"].extend(res.summary_guided_child_ids)
        lists["graph"].extend(res.graph_child_ids)
        for path in res.graph_paths:
            lists["graph"].extend(path.supporting_child_ids)

    # Dedupe preserving order per lane
    for lane, ids in list(lists.items()):
        seen: set[str] = set()
        uniq: list[str] = []
        for cid in ids:
            if cid and cid not in seen:
                seen.add(cid)
                uniq.append(cid)
        lists[lane] = uniq

    scores = _rrf(lists, weights=weights, k=rrf_k(settings))
    ranked = sorted(scores.keys(), key=lambda c: scores[c], reverse=True)
    return ObligationFusionResult(
        obligation_id=obligation.obligation_id,
        ranked_child_ids=ranked,
        lane_contributions=lists,
        score_by_child=scores,
        diagnostics={
            "weighted_rrf": True,
            "direct_strongest": weights["direct"]
            >= max(v for k, v in weights.items() if k != "direct"),
            "lane_sizes": {k: len(v) for k, v in lists.items()},
        },
    )


def build_cross_domain_bridge(
    *,
    obligation: TypedObligationV1,
    source_child_ids: list[str],
    target_child_ids: list[str],
    source_entity_ids: list[str],
    target_entity_ids: list[str],
    source_domain: str = "source",
    target_domain: str = "target",
) -> CrossDomainBridgeV1:
    both = bool(source_child_ids) and bool(target_child_ids)
    status = "satisfied" if both else ("partial" if source_child_ids or target_child_ids else "unsupported")
    return CrossDomainBridgeV1(
        bridge_id=f"bridge:{obligation.obligation_id}",
        source_domain=source_domain or (obligation.domains[0] if obligation.domains else "source"),
        target_domain=target_domain
        or (obligation.domains[1] if len(obligation.domains) > 1 else "target"),
        source_concept=obligation.requirement[:120],
        target_concept=obligation.requirement[:120],
        source_entity_ids=list(source_entity_ids),
        target_entity_ids=list(target_entity_ids),
        source_child_ids=list(source_child_ids)[:8],
        target_child_ids=list(target_child_ids)[:8],
        bridge_concept="implementation_mapping",
        mapping_type="implementation_bridge",
        support_status=status,  # type: ignore[arg-type]
        confidence=0.7 if both else 0.3,
        identity_merge_allowed=False,
    ).with_hash()


def build_contradiction_bundle(
    *,
    obligation_id: str,
    supporting_child_ids: list[str],
    opposing_child_ids: list[str],
    subject_entity_id: str = "",
) -> ContradictionBundleV1:
    return ContradictionBundleV1(
        contradiction_id=f"contra:{obligation_id}",
        subject_entity_id=subject_entity_id,
        supporting_child_ids=list(supporting_child_ids)[:8],
        opposing_child_ids=list(opposing_child_ids)[:8],
        resolution_status=(
            "unresolved"
            if supporting_child_ids and opposing_child_ids
            else "insufficient_sides"
        ),
    ).with_hash()


def global_pool_from_obligations(
    fusions: list[ObligationFusionResult],
    *,
    per_obligation_cap: int = 6,
) -> list[str]:
    """Combine obligation winners without letting one domain dominate."""

    pool: list[str] = []
    seen: set[str] = set()
    # Round-robin across obligations
    pointers = {f.obligation_id: 0 for f in fusions}
    progress = True
    while progress and len(pool) < per_obligation_cap * max(1, len(fusions)):
        progress = False
        for fus in fusions:
            idx = pointers[fus.obligation_id]
            taken = sum(1 for c in pool if c in set(fus.ranked_child_ids[:per_obligation_cap]))
            if taken >= per_obligation_cap:
                continue
            while idx < len(fus.ranked_child_ids):
                cid = fus.ranked_child_ids[idx]
                idx += 1
                pointers[fus.obligation_id] = idx
                if cid and cid not in seen:
                    seen.add(cid)
                    pool.append(cid)
                    progress = True
                    break
    return pool


def attach_paths_to_results(
    results: list[SubQueryResultV1],
    paths: list[GraphPathResultV1],
) -> list[SubQueryResultV1]:
    by_sq: dict[str, list[GraphPathResultV1]] = defaultdict(list)
    for p in paths:
        by_sq[p.subquery_id].append(p)
    out: list[SubQueryResultV1] = []
    for res in results:
        matched = by_sq.get(res.subquery_id) or []
        if not matched:
            out.append(res)
            continue
        graph_children: list[str] = []
        for p in matched:
            graph_children.extend(p.supporting_child_ids)
        status = res.status
        if matched and status in {"unsupported", "blocked"}:
            status = "partial"
        out.append(
            res.model_copy(
                update={
                    "graph_paths": matched,
                    "graph_child_ids": list(dict.fromkeys(graph_children)),
                    "status": status,
                }
            ).with_hash()
        )
    return out
