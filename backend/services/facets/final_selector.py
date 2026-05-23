"""Facet-aware final context selector.

This module sits after retrieval/reranking. It does not retrieve anything and
does not know about chat, graph, Qdrant, Mongo, or Neo4j. Its job is narrow:
given scored candidates with facet/lane tags, keep strong global evidence while
reserving room for lanes the coverage detector says are missing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FacetCandidate:
    """A scored chunk plus the facet lanes it can support."""

    item: Any
    score: float
    lanes: set[str] = field(default_factory=set)
    key: str = ""
    doc_id: str = ""
    junk: bool = False
    order: int = 0


def _candidate_key(candidate: FacetCandidate) -> str:
    if candidate.key:
        return candidate.key
    item = candidate.item
    chunk_id = str(getattr(item, "chunk_id", "") or "")
    if chunk_id:
        return f"chunk:{chunk_id}"
    doc_id = str(candidate.doc_id or getattr(item, "doc_id", "") or "")
    text = " ".join(str(getattr(item, "text", "") or "").split())[:240]
    return f"text:{doc_id}:{text}" if text else f"item:{id(item)}"


def select_facet_final(
    candidates: list[FacetCandidate],
    *,
    missing_lanes: list[str] | set[str],
    priority_lanes: list[str] | set[str] | None = None,
    max_items: int,
    lane_budget: int = 1,
    source_cap: int | None = None,
) -> tuple[list[Any], dict[str, Any]]:
    """Select final context candidates using lane reservations plus score order.

    Algorithm:
    1. Drop junk candidates when enough non-junk evidence exists.
    2. Reserve the best candidate for query-priority lanes, if available.
    3. Reserve the best candidate for each missing lane, if available.
    4. Fill the remaining budget by score while respecting a soft lane quota.
    5. Relax quotas if there are still empty slots.
    6. Return items in selector order: reserved lane evidence first, then global
       evidence. The prompt already carries source scores, and this order makes
       facet coverage visible to the model.
    """

    max_items = max(1, int(max_items or 1))
    lane_budget = max(1, int(lane_budget or 1))

    def ordered_lanes(values: list[str] | set[str] | None) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()
        for value in values or []:
            lane = str(value or "").strip()
            if not lane or lane in seen:
                continue
            ordered.append(lane)
            seen.add(lane)
        return ordered

    priority = ordered_lanes(priority_lanes)
    missing = ordered_lanes(missing_lanes)
    tracked = ordered_lanes([*priority, *missing])
    tracked_set = set(tracked)
    clean = [candidate for candidate in candidates if not candidate.junk]
    junk = [candidate for candidate in candidates if candidate.junk]
    if len(clean) < min(max_items, 2):
        needed_junk = max(0, min(max_items, 2) - len(clean))
        clean.extend(junk[:needed_junk])
        junk = junk[needed_junk:]

    ranked = sorted(clean, key=lambda c: (-float(c.score or 0.0), c.order))
    selected: list[FacetCandidate] = []
    seen_keys: set[str] = set()
    seen_docs: set[str] = set()
    lane_counts: dict[str, int] = {lane: 0 for lane in tracked}

    def can_add(candidate: FacetCandidate, *, enforce_source_cap: bool = True) -> bool:
        key = _candidate_key(candidate)
        if key in seen_keys or len(selected) >= max_items:
            return False
        doc_id = str(candidate.doc_id or "")
        if (
            enforce_source_cap
            and source_cap is not None
            and doc_id
            and doc_id not in seen_docs
            and len(seen_docs) >= int(source_cap)
        ):
            return False
        return True

    def add(candidate: FacetCandidate) -> bool:
        if not can_add(candidate):
            return False
        selected.append(candidate)
        seen_keys.add(_candidate_key(candidate))
        if candidate.doc_id:
            seen_docs.add(str(candidate.doc_id))
        for lane in candidate.lanes & tracked_set:
            lane_counts[lane] = lane_counts.get(lane, 0) + 1
        return True

    def reserve_lanes(lanes: list[str]) -> None:
        for lane in lanes:
            if lane_counts.get(lane, 0) >= lane_budget:
                continue
            lane_candidates = [
                candidate
                for candidate in ranked
                if lane in candidate.lanes and can_add(candidate)
            ]
            if not lane_candidates:
                continue
            add(lane_candidates[0])

    # Pass 1: reserve query-stated facets before dynamic/global score fill.
    reserve_lanes(priority)

    # Pass 2: reserve coverage for missing lanes not already covered.
    reserve_lanes(missing)

    # Pass 3: fill by global score, but do not over-stuff already-covered lanes.
    for candidate in ranked:
        if len(selected) >= max_items:
            break
        key_lanes = candidate.lanes & tracked_set
        if key_lanes and all(lane_counts.get(lane, 0) >= lane_budget for lane in key_lanes):
            continue
        add(candidate)

    # Pass 4: relax lane quota. Keep dedupe/source cap.
    for candidate in ranked:
        if len(selected) >= max_items:
            break
        add(candidate)

    # Pass 5: final relaxation for source cap only, useful in tiny corpora.
    for candidate in ranked:
        if len(selected) >= max_items:
            break
        key = _candidate_key(candidate)
        if key in seen_keys:
            continue
        selected.append(candidate)
        seen_keys.add(key)
        for lane in candidate.lanes & tracked_set:
            lane_counts[lane] = lane_counts.get(lane, 0) + 1

    covered = [lane for lane in missing if lane_counts.get(lane, 0) > 0]
    priority_covered = [lane for lane in priority if lane_counts.get(lane, 0) > 0]
    return [candidate.item for candidate in selected[:max_items]], {
        "candidates": len(candidates),
        "clean_candidates": len(clean),
        "filtered_junk": len([candidate for candidate in candidates if candidate.junk]),
        "priority_lanes": priority,
        "covered_priority_lanes": priority_covered,
        "uncovered_priority_lanes": [
            lane for lane in priority if lane not in priority_covered
        ],
        "missing_lanes": missing,
        "covered_lanes": covered,
        "uncovered_lanes": [lane for lane in missing if lane not in covered],
        "lane_counts": lane_counts,
    }
