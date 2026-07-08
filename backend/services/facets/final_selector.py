"""Facet-aware final context selector.

This module sits after retrieval/reranking. It does not retrieve anything and
does not know about chat, graph, Qdrant, Mongo, or Neo4j. Its job is narrow:
given scored candidates with facet/lane tags, keep strong global evidence while
reserving room for lanes the coverage detector says are missing and, when the
caller supplies an explicit multi-corpus scope, preserving at least one strong
candidate from each represented corpus within the final budget.
"""

from __future__ import annotations

# VERIFICATION (2026-06-14 e2e wiring audit): select_facet_final consumes two
# wired signals — source_cap (hard distinct-doc cap, honored in Passes 1-5) and
# max_per_domain (domain spread for BROAD queries). FacetCandidate.domain is
# populated from SourceChunk.domain (hydrate_chunks + hydrate_summary_rerank_texts)
# via chat_orchestrator._chat_selector_candidates; max_per_domain is gated on
# search_mode=="global". Full chain + integration test:
# CONTINUITY/RETRIEVAL_WIRING_VERIFICATION.md
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
    corpus_id: str = ""
    domain: str = ""
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


def _candidate_corpus_id(candidate: FacetCandidate) -> str:
    return str(
        candidate.corpus_id
        or getattr(candidate.item, "corpus_id", "")
        or ""
    ).strip()


def _ordered_unique(values: list[str] | set[str] | None) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        ordered.append(item)
        seen.add(item)
    return ordered


def _strong_score_floor(score: float, top_score: float) -> bool:
    if top_score <= 0:
        return score > 0
    if 0.0 <= score <= top_score <= 1.0 and top_score > 0.0:
        return (score / top_score) >= 0.80 and score >= 0.35
    return score >= top_score - 1.25


def select_facet_final(
    candidates: list[FacetCandidate],
    *,
    missing_lanes: list[str] | set[str],
    priority_lanes: list[str] | set[str] | None = None,
    max_items: int,
    lane_budget: int = 1,
    source_cap: int | None = None,
    max_per_domain: int | None = None,
    per_doc_cap: int | None = None,
    selected_corpus_ids: list[str] | set[str] | None = None,
) -> tuple[list[Any], dict[str, Any]]:
    """Select final context candidates using lane reservations plus score order.

    Algorithm:
    1. Drop junk candidates when enough non-junk evidence exists.
    2. Reserve the best candidate for query-priority lanes, if available.
    3. Reserve the best candidate for each missing lane, if available.
    4. Fill the remaining budget by score while respecting a soft lane quota.
    5. Relax quotas if there are still empty slots.
    6. Reserve strong candidates from selected corpora that would otherwise be
       missing from the final packet.
    7. Return items in selector order: reserved lane evidence first, then global
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
    doc_counts: dict[str, int] = {}
    domain_counts: dict[str, int] = {}
    lane_counts: dict[str, int] = {lane: 0 for lane in tracked}

    def can_add(
        candidate: FacetCandidate,
        *,
        enforce_source_cap: bool = True,
        enforce_domain_cap: bool = True,
    ) -> bool:
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
        # Per-document chunk cap — a hard ceiling (like source_cap, NOT relaxed
        # in the fill passes) so one book can't dominate the final context with
        # many near-redundant chunks. Tagged with enforce_source_cap because it's
        # a hard-cap signal, not the soft domain-spread quota.
        if (
            enforce_source_cap
            and per_doc_cap is not None
            and doc_id
            and doc_counts.get(doc_id, 0) >= int(per_doc_cap)
        ):
            return False
        domain = str(getattr(candidate, "domain", "") or "")
        if (
            enforce_domain_cap
            and max_per_domain is not None
            and domain
            and domain_counts.get(domain, 0) >= int(max_per_domain)
        ):
            return False
        return True

    def add(candidate: FacetCandidate, *, enforce_domain_cap: bool = True) -> bool:
        if not can_add(candidate, enforce_domain_cap=enforce_domain_cap):
            return False
        selected.append(candidate)
        seen_keys.add(_candidate_key(candidate))
        if candidate.doc_id:
            seen_docs.add(str(candidate.doc_id))
            doc_counts[str(candidate.doc_id)] = (
                doc_counts.get(str(candidate.doc_id), 0) + 1
            )
        domain = str(getattr(candidate, "domain", "") or "")
        if domain:
            domain_counts[domain] = domain_counts.get(domain, 0) + 1
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

    def reserve_corpora() -> dict[str, Any]:
        corpus_meta: dict[str, Any] = {
            "enabled": False,
            "target_corpora": [],
            "covered_corpora": [],
            "added": 0,
            "replaced": 0,
            "skipped": [],
        }
        requested = _ordered_unique(selected_corpus_ids)
        if len(requested) < 2 or max_items <= 1:
            return corpus_meta
        top_score = float(ranked[0].score or 0.0) if ranked else 0.0
        eligible_by_corpus: dict[str, FacetCandidate] = {}
        requested_set = set(requested)
        for candidate in ranked:
            corpus_id = _candidate_corpus_id(candidate)
            if not corpus_id or corpus_id not in requested_set or corpus_id in eligible_by_corpus:
                continue
            if not _strong_score_floor(float(candidate.score or 0.0), top_score):
                continue
            eligible_by_corpus[corpus_id] = candidate

        target = [
            corpus_id for corpus_id in requested if corpus_id in eligible_by_corpus
        ][:max_items]
        corpus_meta["enabled"] = bool(target)
        corpus_meta["target_corpora"] = target
        if not target:
            return corpus_meta

        def corpus_counts() -> dict[str, int]:
            counts: dict[str, int] = {}
            for candidate in selected:
                corpus_id = _candidate_corpus_id(candidate)
                if not corpus_id:
                    continue
                counts[corpus_id] = counts.get(corpus_id, 0) + 1
            return counts

        for corpus_id in target:
            counts = corpus_counts()
            if counts.get(corpus_id, 0) > 0:
                continue
            candidate = eligible_by_corpus[corpus_id]
            if len(selected) < max_items:
                if add(candidate):
                    corpus_meta["added"] += 1
                    continue
            replace_pos: int | None = None
            replace_score = float("inf")
            for pos, existing in enumerate(selected):
                existing_corpus = _candidate_corpus_id(existing)
                if not existing_corpus or counts.get(existing_corpus, 0) <= 1:
                    continue
                # Do not drop explicit lane reservations unless there is no
                # alternative; lane coverage is the selector's first contract.
                if existing.lanes & tracked_set:
                    continue
                score = float(existing.score or 0.0)
                if score < replace_score:
                    replace_score = score
                    replace_pos = pos
            if replace_pos is None:
                corpus_meta["skipped"].append(corpus_id)
                continue

            old = selected[replace_pos]
            seen_keys.discard(_candidate_key(old))
            if old.doc_id:
                old_doc = str(old.doc_id)
                doc_counts[old_doc] = max(0, doc_counts.get(old_doc, 0) - 1)
                if doc_counts[old_doc] <= 0:
                    doc_counts.pop(old_doc, None)
                    seen_docs.discard(old_doc)
            old_domain = str(getattr(old, "domain", "") or "")
            if old_domain:
                domain_counts[old_domain] = max(0, domain_counts.get(old_domain, 0) - 1)
                if domain_counts[old_domain] <= 0:
                    domain_counts.pop(old_domain, None)
            for lane in old.lanes & tracked_set:
                lane_counts[lane] = max(0, lane_counts.get(lane, 0) - 1)

            selected[replace_pos] = candidate
            seen_keys.add(_candidate_key(candidate))
            if candidate.doc_id:
                new_doc = str(candidate.doc_id)
                seen_docs.add(new_doc)
                doc_counts[new_doc] = doc_counts.get(new_doc, 0) + 1
            new_domain = str(getattr(candidate, "domain", "") or "")
            if new_domain:
                domain_counts[new_domain] = domain_counts.get(new_domain, 0) + 1
            for lane in candidate.lanes & tracked_set:
                lane_counts[lane] = lane_counts.get(lane, 0) + 1
            corpus_meta["replaced"] += 1

        corpus_meta["covered_corpora"] = [
            corpus_id for corpus_id in target if corpus_counts().get(corpus_id, 0) > 0
        ]
        return corpus_meta

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

    # Pass 5: fill remaining budget. Still honor source_cap (hard distinct-doc
    # ceiling) but RELAX the per-domain cap here — passes 1-4 already spread the
    # selection across domains, so this pass tops up the chunk budget with extra
    # chunks (from already-spread domains/docs) rather than starving the answer
    # when only a few domains exist in the pool. source_cap stays a hard cap
    # (was previously a no-op relaxation that ignored it entirely).
    for candidate in ranked:
        if len(selected) >= max_items:
            break
        add(candidate, enforce_domain_cap=False)

    corpus_floor = reserve_corpora()

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
        "corpus_floor": corpus_floor,
    }
