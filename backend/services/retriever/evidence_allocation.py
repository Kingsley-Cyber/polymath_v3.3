"""Pure source-side evidence allocation for chat retrieval.

The retriever and the final selector decide which chunks are *best*. This
module decides how those chunks should be *distributed across the sides of a
multi-document question* — so a title-matching book cannot answer a two-sided
query by itself, and each required side gets real depth from its own documents.

It is intentionally dependency-light. Every function operates on a duck-typed
chunk (any object exposing ``chunk_id`` / ``doc_id`` / ``score`` / ``text`` /
``metadata``) and takes the field extractors and the lane-scoring function as
callbacks. That keeps the allocation logic unit-testable with plain ``python3``
— no chat orchestrator, no retriever, no database client, no pydantic — while
production passes its richer (metadata-aware) scorer in.

Vocabulary:
* "lane" / "side" — one required concept of an :class:`EvidencePlan`
  (e.g. the *seduction* side and the *personality framework* side).
* "support" chunk — a chunk reserved for a side because that side was
  under-covered by the base retrieval.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, List, Optional, Sequence, Set

from services.retriever.evidence_plan import (
    EvidenceLane,
    EvidencePlan,
    evidence_lane_matches_text,
)

# How many DISTINCT-document chunks each required side should aim for in the
# final context. 2 is the floor that makes a side an actual lane of evidence
# rather than a single quotable line.
DEFAULT_PER_SIDE_SOURCES = 2

# A base source only counts toward a side's *coverage* if it matches the side
# this strongly. Matches this strong correspond to a real alias hit
# (``evidence_lane_matches_text``); a stray term co-occurrence (the word
# "personality" inside a seduction passage) scores below this and must NOT be
# allowed to mark the side "already covered".
STRONG_LANE_SCORE = 8

# Support-SELECTION floor — softer than STRONG_LANE_SCORE. A chunk scoring in
# [WEAK_LANE_SUPPORT_FLOOR, STRONG_LANE_SCORE) is a weak-but-present lane match:
# eligible to COMPETE for a support slot, but stronger (>=8) candidates still
# outrank it (lane_score * _LANE_SCORE_WEIGHT dominates), and it never marks the
# side "covered" — coverage still requires STRONG_LANE_SCORE upstream. This lets
# a side backed only by adjacent-vocabulary evidence get real breadth instead of
# an empty lane, without letting a stray cross-domain mention satisfy coverage.
WEAK_LANE_SUPPORT_FLOOR = 4

# Scoring weights for ranking lane-support candidates. Mirrors the production
# heuristic so the pure path and the wired path agree.
_LANE_SCORE_WEIGHT = 10.0
_BASE_SCORE_WEIGHT = 2.0
_NEW_DOC_BONUS = 5.0
_SEMANTIC_DOC_BONUS = 7.0
_SEMANTIC_FLOOR_SCORE = 3  # lane score given to a doc the ingest layer says belongs to the side


def lane_alias_score(text: str, lane: EvidenceLane) -> int:
    """Lightweight, metadata-free lane score from alias/term overlap.

    Used by tests and by any caller that has only chunk text. Production passes
    its own richer scorer (which also rewards support tags and query grounding)
    via the ``score_fn`` parameter of the functions below.
    """

    if evidence_lane_matches_text(lane, text):
        return STRONG_LANE_SCORE
    haystack = f" {' '.join((text or '').lower().split())} "
    score = 0
    for term in lane.search_terms[:10]:
        norm = " ".join(str(term or "").lower().split())
        if norm and f" {norm} " in haystack:
            score += 2
    return score


def select_lane_support(
    candidates: Sequence[Any],
    *,
    lane: EvidenceLane,
    target_k: int,
    existing_chunk_ids: Set[str],
    existing_doc_ids: Set[str],
    semantic_doc_ids: Optional[Set[str]] = None,
    score_fn: Callable[[Any, EvidenceLane], int],
    chunk_id_fn: Callable[[Any], str],
    doc_id_fn: Callable[[Any], str],
    base_score_fn: Callable[[Any], float],
    low_value_fn: Optional[Callable[[Any], bool]] = None,
) -> List[Any]:
    """Pick up to ``target_k`` chunks for one side, each from a DISTINCT document.

    This is the core of per-side allocation: instead of reserving a single
    chunk per lane, reserve ``target_k`` chunks, each from a different document,
    so a side is backed by breadth rather than one passage. New documents (not
    already in ``existing_doc_ids``) and documents the ingestion layer maps to
    this side (``semantic_doc_ids``) are preferred.

    Guarantees:
    * never two chunks from the same document,
    * never a chunk already chosen (``existing_chunk_ids``),
    * never a fully off-lane chunk: support must match the lane at least weakly
      (``WEAK_LANE_SUPPORT_FLOOR``) unless the ingest layer says its document
      belongs to the side. Strong (>=``STRONG_LANE_SCORE``) matches still
      outrank weak ones, and a weak support pick never marks the side "covered".
    """

    semantic_doc_ids = semantic_doc_ids or set()
    target_k = max(1, int(target_k or 1))

    scored: List[tuple] = []
    for chunk in candidates:
        chunk_id = chunk_id_fn(chunk)
        if not chunk_id or chunk_id in existing_chunk_ids:
            continue
        if low_value_fn is not None and low_value_fn(chunk):
            continue
        doc_id = doc_id_fn(chunk)
        semantic_doc_match = bool(doc_id and doc_id in semantic_doc_ids)
        lane_score = score_fn(chunk, lane)
        if lane_score <= 0 and semantic_doc_match:
            lane_score = _SEMANTIC_FLOOR_SCORE
        if lane_score <= 0:
            continue
        if lane_score < WEAK_LANE_SUPPORT_FLOOR and not semantic_doc_match:
            continue
        new_doc = bool(doc_id and doc_id not in existing_doc_ids)
        bounded_base = min(max(float(base_score_fn(chunk) or 0.0), 0.0), 1.0)
        final_score = (
            lane_score * _LANE_SCORE_WEIGHT
            + bounded_base * _BASE_SCORE_WEIGHT
            + (_NEW_DOC_BONUS if new_doc else 0.0)
            + (_SEMANTIC_DOC_BONUS if semantic_doc_match else 0.0)
        )
        scored.append((final_score, lane_score, chunk_id, doc_id, chunk))

    # Highest combined score first; stable on score so callers get deterministic
    # picks for equal scores (insertion order via index is implicit in sort).
    scored.sort(key=lambda row: row[0], reverse=True)

    picks: List[Any] = []
    used_docs: Set[str] = set(existing_doc_ids)
    used_chunks: Set[str] = set(existing_chunk_ids)

    # Pass 1: one chunk per *distinct* document, preferring new/semantic docs
    # (already encoded in final_score), until the side hits target_k.
    for _score, _lane_score, chunk_id, doc_id, chunk in scored:
        if len(picks) >= target_k:
            break
        if chunk_id in used_chunks:
            continue
        if doc_id and doc_id in used_docs:
            continue
        picks.append(chunk)
        used_chunks.add(chunk_id)
        if doc_id:
            used_docs.add(doc_id)

    return picks


def lane_coverage(
    sources: Iterable[Any],
    plan: EvidencePlan,
    *,
    score_fn: Callable[[Any, EvidenceLane], int],
    doc_id_fn: Callable[[Any], str],
    strong_score: int = STRONG_LANE_SCORE,
) -> dict:
    """Compute which required sides are already covered by ``sources``.

    A side is covered only when at least ``max(1, lane.min_sources)`` DISTINCT
    documents each contain a chunk that matches the side *strongly* (score >=
    ``strong_score``). This is the fix for "one stray chunk marks the side
    covered": a single passage — or several passages from the same book — can no
    longer satisfy a two-source side, and a weak term co-occurrence does not
    count at all.
    """

    source_list = list(sources or [])
    lane_docs: dict = {}
    for lane in plan.required_lanes:
        docs: List[str] = []
        seen: Set[str] = set()
        for chunk in source_list:
            if score_fn(chunk, lane) < strong_score:
                continue
            doc_id = doc_id_fn(chunk)
            if doc_id and doc_id not in seen:
                seen.add(doc_id)
                docs.append(doc_id)
        lane_docs[lane.name] = docs

    covered: List[str] = []
    missing: List[str] = []
    for lane in plan.required_lanes:
        need = max(1, int(lane.min_sources or 1))
        if len(lane_docs.get(lane.name) or []) >= need:
            covered.append(lane.name)
        else:
            missing.append(lane.name)

    return {
        "covered_lanes": covered,
        "missing_lanes": missing,
        "lane_doc_ids": lane_docs,
        "distinct_doc_count": len({d for docs in lane_docs.values() for d in docs if d}),
    }


def cap_chunks_per_doc(
    sources: Sequence[Any],
    *,
    cap: int,
    doc_id_fn: Callable[[Any], str],
    protect_fn: Optional[Callable[[Any], bool]] = None,
) -> List[Any]:
    """Keep at most ``cap`` chunks per document, preserving order.

    ``cap <= 0`` disables the ceiling (returns the list unchanged). Chunks for
    which ``protect_fn`` is true (the reserved per-side support chunks) are
    NEVER dropped, but they still count toward their document's tally so a
    dominant book cannot smuggle extra non-reserved passages past the cap.
    """

    if not cap or cap <= 0:
        return list(sources)
    kept: List[Any] = []
    counts: dict = {}
    for chunk in sources:
        doc_id = doc_id_fn(chunk)
        protected = bool(protect_fn and protect_fn(chunk))
        if doc_id and not protected and counts.get(doc_id, 0) >= cap:
            continue
        if doc_id:
            counts[doc_id] = counts.get(doc_id, 0) + 1
        kept.append(chunk)
    return kept


def per_doc_cap_for_plan(
    plan: EvidencePlan,
    *,
    budget: int = 0,
    per_doc: int = DEFAULT_PER_SIDE_SOURCES,
) -> int:
    """Per-document cap for an active multi-side plan, else 0 (disabled).

    For a genuine multi-side question, no single document should contribute more
    than one side's worth of evidence (``per_doc``, default 2). That is what
    turns "4 of 5 chunks from the title-matching book" into a balanced packet:
    the dominant book is capped at ``per_doc`` and the freed slots are taken by
    the reserved per-side support from other documents. Single-side / inactive
    plans return 0 so ordinary queries keep today's behaviour.

    ``budget`` is accepted for signature stability and future budget-aware
    tuning; the cap is intentionally a small constant, not a fraction of the
    budget, because the goal is balance across documents, not maximal fill.
    """

    lanes = list(plan.required_lanes) if plan and plan.active else []
    if len(lanes) < 2:
        return 0
    return max(1, int(per_doc or 1))
