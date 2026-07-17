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

import math
import re
from dataclasses import dataclass
from pathlib import PurePath
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
_SEMANTIC_FLOOR_SCORE = (
    3  # lane score given to a doc the ingest layer says belongs to the side
)

# Exact, case-normalized metadata anchoring. These values are too generic to
# prove that the user named a source, section, entity, or bibliographic term.
_ANCHOR_TOKEN_RE = re.compile(r"[a-z0-9]+")
_ANCHOR_GENERIC_TERMS = frozenset(
    {
        "a",
        "an",
        "appendix",
        "article",
        "author",
        "book",
        "books",
        "chapter",
        "conclusion",
        "document",
        "introduction",
        "of",
        "paper",
        "references",
        "section",
        "the",
    }
)
_ANCHOR_METADATA_KEYS: dict[str, tuple[str, ...]] = {
    "title": (
        "title",
        "source_book",
        "book_title",
        "document_title",
        "doc_title",
        "source_title",
    ),
    "author": (
        "author",
        "authors",
        "author_or_org",
        "creator",
        "creators",
    ),
    "entity": (
        "entity",
        "entities",
        "matched_entities",
        "entity_names",
        "related_entity",
        "seed_entity",
        "neighbor_entity",
    ),
    "bibliographic": (
        "bibliographic_terms",
        "biblio_terms",
        "citation_terms",
        "cited_authors",
        "cited_titles",
        "references",
    ),
}


@dataclass(frozen=True)
class MetadataAnchorMatch:
    """Deterministic explanation for one candidate's anchor classification."""

    is_anchor: bool
    matched_fields: tuple[str, ...] = ()
    matched_terms: tuple[str, ...] = ()


@dataclass(frozen=True)
class TwoLaneAllocation:
    """Selected candidates plus JSON-safe diagnostics for retrieval traces."""

    candidates: tuple[Any, ...]
    diagnostics: dict[str, Any]


def _normalized_anchor_phrase(value: Any) -> str:
    return " ".join(_ANCHOR_TOKEN_RE.findall(str(value or "").casefold()))


def _iter_metadata_values(value: Any) -> Iterable[str]:
    if value is None:
        return
    if isinstance(value, dict):
        for key in (
            "canonical_name",
            "display_name",
            "name",
            "surface_form",
            "title",
            "author",
            "value",
        ):
            nested = value.get(key)
            if nested:
                yield str(nested)
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _iter_metadata_values(item)
        return
    text = str(value).strip()
    if text:
        yield text


def _candidate_anchor_terms(candidate: Any) -> list[tuple[str, str]]:
    """Return source metadata terms only; candidate body text is never authority."""

    metadata = getattr(candidate, "metadata", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    terms: list[tuple[str, str]] = []

    # Hydrated SourceChunk fields and their pre-hydration payload mirrors.
    doc_name = str(getattr(candidate, "doc_name", "") or "").strip()
    if doc_name:
        stem = PurePath(doc_name).stem.replace("_", " ").replace("-", " ")
        terms.append(("title", stem))
    for heading in getattr(candidate, "heading_path", None) or []:
        terms.append(("heading_path", str(heading)))

    for field, keys in _ANCHOR_METADATA_KEYS.items():
        for key in keys:
            for value in _iter_metadata_values(metadata.get(key)):
                terms.append((field, value))

    # Graph provenance already carries resolved entity/bibliographic names.
    for item in getattr(candidate, "provenance", None) or []:
        if not isinstance(item, dict):
            continue
        for key in (
            "entity",
            "subject",
            "surface_form",
            "seed_entity",
            "neighbor_entity",
            "related_entity",
        ):
            for value in _iter_metadata_values(item.get(key)):
                terms.append(("entity", value))

    deduped: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for field, raw in terms:
        normalized = _normalized_anchor_phrase(raw)
        if not normalized:
            continue
        key = (field, normalized)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(key)
    return deduped


def classify_metadata_anchor(query: str, candidate: Any) -> MetadataAnchorMatch:
    """Classify ANCHOR by exact case-normalized candidate metadata matches.

    The classifier deliberately ignores embeddings, candidate text, and scores.
    It accepts an exact token-bounded metadata phrase. A leading English article
    is ignored for titles (``The Art of Seduction`` -> ``art of seduction``),
    while generic one-word headings such as ``Introduction`` never anchor.
    """

    normalized_query = _normalized_anchor_phrase(query)
    if not normalized_query:
        return MetadataAnchorMatch(False)
    query_haystack = f" {normalized_query} "

    matches: list[tuple[str, str]] = []
    for field, normalized in _candidate_anchor_terms(candidate):
        variants = [normalized]
        if field == "title":
            title_tokens = normalized.split()
            if title_tokens and title_tokens[0] in {"a", "an", "the"}:
                variants.append(" ".join(title_tokens[1:]))
        for phrase in variants:
            tokens = phrase.split()
            distinctive = [
                token
                for token in tokens
                if token not in _ANCHOR_GENERIC_TERMS and len(token) >= 3
            ]
            if not distinctive:
                continue
            # Single-token authority is deliberately strict. It is useful for
            # distinctive surnames/entities but rejects generic short labels.
            if len(tokens) == 1 and len(tokens[0]) < 5:
                continue
            if f" {phrase} " in query_haystack:
                matches.append((field, phrase))
                break

    if not matches:
        return MetadataAnchorMatch(False)
    fields = tuple(dict.fromkeys(field for field, _term in matches))
    terms = tuple(dict.fromkeys(term for _field, term in matches))
    return MetadataAnchorMatch(True, fields, terms)


def allocate_two_lane_seats(
    selected: Sequence[Any],
    candidate_pool: Sequence[Any],
    *,
    query: str,
    budget: int,
    anchor_ratio: float = 0.60,
    anchor_threshold: float = 0.10,
    expansion_threshold: float = 0.10,
    candidate_id_fn: Callable[[Any], str],
    score_fn: Callable[[Any], float],
    side_fn: Optional[Callable[[Any], str]] = None,
    protected_ids: Optional[Set[str]] = None,
) -> TwoLaneAllocation:
    """Reserve ANCHOR/EXPANSION seats with deterministic bidirectional spillover.

    Relationship composition is expressed through ``side_fn``. The allocator
    first freezes the already-selected seat count for every side, then applies
    the anchor/expansion quota *inside* that side. It can therefore replace an
    expansion with a metadata anchor from the ranked pool without stealing a
    seat from another relationship side.

    Existing protected seats (sufficiency, corpus floors, graph reserves) are
    retained. Unselected EXPANSION candidates never replace MMR-selected
    expansion evidence; only a newly discovered ANCHOR can enter from the pool.
    """

    budget = max(0, int(budget or 0))
    ratio = min(max(float(anchor_ratio), 0.0), 1.0)
    protected_ids = {str(value) for value in (protected_ids or set()) if str(value)}
    selected_list = list(selected or [])[:budget]
    if budget <= 0 or not selected_list:
        return TwoLaneAllocation(
            tuple(),
            {
                "enabled": True,
                "budget": budget,
                "anchor_ratio": ratio,
                "groups": [],
                "selected": [],
            },
        )

    def _cid(item: Any) -> str:
        return str(candidate_id_fn(item) or "")

    def _side(item: Any) -> str:
        value = str(side_fn(item) if side_fn is not None else "__all__").strip()
        return value or "__unassigned__"

    # Existing final seats define the senior per-side allocation. Two-lane
    # selection is forbidden from changing these counts.
    group_order: list[str] = []
    selected_by_group: dict[str, list[Any]] = {}
    for item in selected_list:
        group = _side(item)
        if group not in selected_by_group:
            group_order.append(group)
            selected_by_group[group] = []
        selected_by_group[group].append(item)

    # The pool is rank ordered. De-duplicate by candidate identity while
    # retaining the first occurrence and classify exactly once.
    pool: list[Any] = []
    seen_ids: set[str] = set()
    matches: dict[str, MetadataAnchorMatch] = {}
    for item in [*candidate_pool, *selected_list]:
        key = _cid(item)
        if not key or key in seen_ids:
            continue
        seen_ids.add(key)
        pool.append(item)
        matches[key] = classify_metadata_anchor(query, item)

    final_by_group: dict[str, list[Any]] = {}
    group_reports: list[dict[str, Any]] = []
    for group in group_order:
        baseline = selected_by_group[group]
        group_budget = len(baseline)
        anchor_quota = min(group_budget, int(math.ceil(ratio * group_budget)))
        expansion_quota = group_budget - anchor_quota

        baseline_ids = {_cid(item) for item in baseline}
        protected = [item for item in baseline if _cid(item) in protected_ids]
        protected_id_set = {_cid(item) for item in protected}
        baseline_anchor = [
            item
            for item in baseline
            if matches[_cid(item)].is_anchor and _cid(item) not in protected_id_set
        ]
        baseline_expansion = [
            item
            for item in baseline
            if not matches[_cid(item)].is_anchor and _cid(item) not in protected_id_set
        ]
        extra_anchor = [
            item
            for item in pool
            if _cid(item) not in baseline_ids
            and _side(item) == group
            and matches[_cid(item)].is_anchor
        ]

        # No metadata anchor in this side means single-lane collapse: preserve
        # the prior selection byte-for-byte and avoid gratuitous reshuffling.
        if not baseline_anchor and not extra_anchor:
            final_by_group[group] = list(baseline)
            group_reports.append(
                {
                    "side": group,
                    "budget": group_budget,
                    "anchor_quota": anchor_quota,
                    "expansion_quota": expansion_quota,
                    "anchors_available": 0,
                    "anchor_candidate_ids": [],
                    "anchors_selected": 0,
                    "expansions_selected": group_budget,
                    "spill_anchor_to_expansion": anchor_quota,
                    "spill_expansion_to_anchor": 0,
                    "collapsed_single_lane": True,
                }
            )
            continue

        chosen: list[Any] = list(protected)
        chosen_ids = {_cid(item) for item in chosen}
        protected_anchor_count = sum(
            1 for item in protected if matches[_cid(item)].is_anchor
        )
        protected_expansion_count = len(protected) - protected_anchor_count

        def _admitted(items: Sequence[Any], threshold: float) -> list[Any]:
            return [
                item
                for item in items
                if _cid(item) not in chosen_ids
                and float(score_fn(item) or 0.0) >= float(threshold)
            ]

        anchors = _admitted(
            [*baseline_anchor, *extra_anchor],
            anchor_threshold,
        )
        expansions = _admitted(baseline_expansion, expansion_threshold)

        def _take(items: Sequence[Any], count: int) -> int:
            taken = 0
            for item in items:
                if taken >= max(0, count) or len(chosen) >= group_budget:
                    break
                key = _cid(item)
                if key in chosen_ids:
                    continue
                chosen.append(item)
                chosen_ids.add(key)
                taken += 1
            return taken

        anchor_need = max(0, anchor_quota - protected_anchor_count)
        expansion_need = max(0, expansion_quota - protected_expansion_count)
        anchors_primary = _take(anchors, anchor_need)
        expansions_primary = _take(expansions, expansion_need)

        # Spill unused expansion seats to anchors first, then unused anchor
        # seats to expansions. Both passes use only above-threshold candidates.
        before_anchor_spill = len(chosen)
        _take(anchors, group_budget - len(chosen))
        spill_expansion_to_anchor = len(chosen) - before_anchor_spill
        before_expansion_spill = len(chosen)
        _take(expansions, group_budget - len(chosen))
        spill_anchor_to_expansion = len(chosen) - before_expansion_spill

        # Preserve cross-encoder authority in presentation order. Membership is
        # quota-driven; ordering remains the original ranked-pool order, with
        # protected baseline-only candidates retaining their baseline order.
        rank = {_cid(item): index for index, item in enumerate(pool)}
        baseline_rank = {
            _cid(item): len(pool) + index for index, item in enumerate(baseline)
        }
        chosen.sort(
            key=lambda item: (
                rank.get(_cid(item), baseline_rank.get(_cid(item), len(pool))),
                _cid(item),
            )
        )
        final_by_group[group] = chosen
        group_reports.append(
            {
                "side": group,
                "budget": group_budget,
                "anchor_quota": anchor_quota,
                "expansion_quota": expansion_quota,
                "anchors_available": len(baseline_anchor) + len(extra_anchor),
                "anchor_candidate_ids": [
                    _cid(item) for item in [*baseline_anchor, *extra_anchor]
                ],
                "anchors_selected": sum(
                    1 for item in chosen if matches[_cid(item)].is_anchor
                ),
                "expansions_selected": sum(
                    1 for item in chosen if not matches[_cid(item)].is_anchor
                ),
                "protected": len(protected),
                "anchor_primary_filled": anchors_primary,
                "expansion_primary_filled": expansions_primary,
                "spill_anchor_to_expansion": spill_anchor_to_expansion,
                "spill_expansion_to_anchor": spill_expansion_to_anchor,
                "collapsed_single_lane": False,
            }
        )

    # Preserve the selected side interleave: replace each prior side occurrence
    # with that side's next allocated candidate. This is deterministic and
    # prevents one side from being visually grouped ahead of another.
    cursors = {group: 0 for group in group_order}
    allocated: list[Any] = []
    for prior in selected_list:
        group = _side(prior)
        index = cursors[group]
        group_items = final_by_group.get(group, [])
        if index >= len(group_items):
            continue
        allocated.append(group_items[index])
        cursors[group] = index + 1

    selected_trace = []
    for seat, item in enumerate(allocated, 1):
        key = _cid(item)
        match = matches[key]
        selected_trace.append(
            {
                "seat": seat,
                "candidate_id": key,
                "side": _side(item),
                "lane": "anchor" if match.is_anchor else "expansion",
                "matched_fields": list(match.matched_fields),
                "matched_terms": list(match.matched_terms),
                "score": round(float(score_fn(item) or 0.0), 6),
                "protected": key in protected_ids,
            }
        )
    return TwoLaneAllocation(
        tuple(allocated),
        {
            "enabled": True,
            "budget": budget,
            "anchor_ratio": ratio,
            "anchor_threshold": float(anchor_threshold),
            "expansion_threshold": float(expansion_threshold),
            "relationship_precedence": side_fn is not None,
            "groups": group_reports,
            "selected": selected_trace,
            "anchor_seats": sum(1 for row in selected_trace if row["lane"] == "anchor"),
            "expansion_seats": sum(
                1 for row in selected_trace if row["lane"] == "expansion"
            ),
        },
    )


def relationship_allocation_eligible(plan: EvidencePlan | None) -> bool:
    """Whether the shared query plan classifies this as a multi-side relation.

    Eligibility deliberately reuses :mod:`query_semantics` through the
    operators already captured on ``EvidencePlan``. It does not inspect raw
    query wording or maintain a second detector. Both explicit relationships
    and comparisons need independently represented sides; ordinary multi-token
    direct/lay queries do not.
    """

    if not plan or not plan.active or len(plan.required_lanes) < 2:
        return False
    operators = {str(operator or "").strip().casefold() for operator in plan.operators}
    return bool(operators & {"relationship", "comparison"})


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
        "distinct_doc_count": len(
            {d for docs in lane_docs.values() for d in docs if d}
        ),
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
