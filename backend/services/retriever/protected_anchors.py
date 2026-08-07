"""Protected-anchor selection for cross-domain curation.

Up to four strongest fused/reranked *child* chunks are protected from
diversity pruning. This is NOT the final evidence count — MMR selects the
remainder of a dynamic 10–18 packet.

Pure + deterministic. No I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence


@dataclass(frozen=True)
class ProtectedAnchorResult:
    protected: list[Any]
    remainder: list[Any]
    diagnostics: dict[str, Any]


def _chunk_id(chunk: Any) -> str:
    return str(getattr(chunk, "chunk_id", "") or "")


def _doc_id(chunk: Any) -> str:
    return str(getattr(chunk, "doc_id", "") or "")


def _parent_id(chunk: Any) -> str:
    return str(getattr(chunk, "parent_id", "") or "")


def _score(chunk: Any) -> float:
    try:
        return float(getattr(chunk, "score", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _is_child(chunk: Any) -> bool:
    tier = str(getattr(chunk, "source_tier", "") or "").lower()
    if tier in {"child", "evidence", "chunk", ""}:
        # Empty tier treated as child when chunk_id looks like a child id.
        return bool(_chunk_id(chunk))
    return "summary" not in tier and "schema" not in tier


def select_protected_anchors(
    ranked_children: Sequence[Any],
    *,
    minimum_protected: int = 1,
    maximum_protected: int = 4,
    max_per_document: int = 2,
    max_per_parent: int = 1,
    minimum_quality: float = 0.0,
    top_score_ratio_floor: float = 0.0,
) -> ProtectedAnchorResult:
    """Select 1–4 strongest qualified children as protected anchors.

    A lane does not automatically earn a protected slot — the candidate must
    clear quality and identity constraints.
    """

    ranked = [c for c in ranked_children if _is_child(c) and _chunk_id(c)]
    if not ranked:
        return ProtectedAnchorResult([], [], {
            "protected_count": 0,
            "reason": "no_child_candidates",
        })

    max_n = max(0, min(int(maximum_protected), 4))
    min_n = max(0, min(int(minimum_protected), max_n))
    top = _score(ranked[0])
    floor = max(float(minimum_quality), float(top_score_ratio_floor) * top)

    protected: list[Any] = []
    protected_ids: set[str] = set()
    docs: dict[str, int] = {}
    parents: dict[str, int] = {}

    for cand in ranked:
        if len(protected) >= max_n:
            break
        cid = _chunk_id(cand)
        if not cid or cid in protected_ids:
            continue
        if _score(cand) < floor and len(protected) >= min_n:
            continue
        if _score(cand) < floor and len(protected) < min_n and floor > 0:
            # Still allow minimum protected if nothing clears floor — take best.
            if protected:
                continue
        did = _doc_id(cand) or "__unknown_doc__"
        pid = _parent_id(cand) or "__unknown_parent__"
        if docs.get(did, 0) >= int(max_per_document):
            continue
        if parents.get(pid, 0) >= int(max_per_parent):
            continue
        protected.append(cand)
        protected_ids.add(cid)
        docs[did] = docs.get(did, 0) + 1
        parents[pid] = parents.get(pid, 0) + 1

    # Guarantee minimum when possible (already constrained by identity rules).
    if len(protected) < min_n:
        for cand in ranked:
            if len(protected) >= min_n:
                break
            cid = _chunk_id(cand)
            if not cid or cid in protected_ids:
                continue
            did = _doc_id(cand) or "__unknown_doc__"
            pid = _parent_id(cand) or "__unknown_parent__"
            if docs.get(did, 0) >= int(max_per_document):
                continue
            if parents.get(pid, 0) >= int(max_per_parent):
                continue
            protected.append(cand)
            protected_ids.add(cid)
            docs[did] = docs.get(did, 0) + 1
            parents[pid] = parents.get(pid, 0) + 1

    remainder = [c for c in ranked if _chunk_id(c) not in protected_ids]
    return ProtectedAnchorResult(
        protected=protected,
        remainder=remainder,
        diagnostics={
            "protected_count": len(protected),
            "remainder_count": len(remainder),
            "maximum_protected": max_n,
            "minimum_protected": min_n,
            "quality_floor": floor,
            "protected_chunk_ids": [_chunk_id(c) for c in protected],
            "protected_is_not_final_total": True,
        },
    )


def dynamic_final_child_target(
    *,
    query_class: str,
    available_qualified: int,
    preferred_cross: int = 12,
    max_children: int = 18,
    min_simple: int = 6,
    max_simple: int = 10,
    min_cross: int = 10,
) -> int:
    """Quality-gated target size — never pad with weak evidence."""

    available = max(0, int(available_qualified))
    qclass = str(query_class or "simple_single_domain").strip().lower()
    if qclass in {"cross_domain", "comparison_or_design", "graph_multi_hop"}:
        lo, hi = int(min_cross), int(max_children)
        preferred = min(int(preferred_cross), hi)
        target = preferred if available >= preferred else min(available, hi)
        if available >= lo:
            target = max(target, min(available, lo)) if available < preferred else target
        return max(0, min(available, max(target, 0), hi))
    if qclass in {"detailed_single_domain"}:
        lo, hi = 10, min(14, int(max_children))
        return max(0, min(available, hi if available >= lo else available))
    # simple_single_domain
    lo, hi = int(min_simple), int(max_simple)
    return max(0, min(available, hi if available >= lo else available))
