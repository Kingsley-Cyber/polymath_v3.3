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

from services.librarian.shelf_engine import (
    ALL_ROLES,
    ROLE_BRIDGE,
    ROLE_COUNTERBALANCE,
    assign_shelf_roles,
)

# P1.5 shelf_reserve seats exactly these roles, in this priority order.
_SHELF_RESERVE_ROLES = (ROLE_BRIDGE, ROLE_COUNTERBALANCE)


@dataclass
class ShelfReserveContext:
    """Caller-supplied inputs for the P1.5 shelf_reserve seat pass.

    Pure data — the caller (chat_orchestrator) resolves the query concepts
    and fetches ``librarian_cards`` (one Mongo find); this module performs no
    I/O. ``cards_by_doc`` maps ``(corpus_id, doc_id)`` to the
    ``librarian_card.v0`` dict for documents present in the candidate pool.
    ``None``/absent context = pass fully off (zero behavior change).
    """

    query_concepts: list[str] = field(default_factory=list)
    cards_by_doc: dict[tuple[str, str], dict] = field(default_factory=dict)
    enabled: bool = True


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
    shelf_reserve_context: ShelfReserveContext | None = None,
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
    7. When ``shelf_reserve_context`` is provided (P1.5 shelf_reserve, dark
       behind SHELF_RESERVE_ENABLED), reserve at most one seat each for the
       best bridge and best counterbalance shelf-role candidate already in
       the pool that passes the calibrated corpus-reservation bound.
       ``None`` (the default) keeps this pass fully off — zero behavior
       change.
    8. Return items in selector order: reserved lane evidence first, then global
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

    def corpus_counts() -> dict[str, int]:
        counts: dict[str, int] = {}
        for candidate in selected:
            corpus_id = _candidate_corpus_id(candidate)
            if not corpus_id:
                continue
            counts[corpus_id] = counts.get(corpus_id, 0) + 1
        return counts

    def weakest_unprotected_pos(
        counts: dict[str, int],
        *,
        protected_keys: set[str] | None = None,
    ) -> int | None:
        """Replace-weakest-unprotected discipline, shared by the corpus floor
        (``reserve_corpora``) and the shelf_reserve seat pass: never drop a
        corpus's last selected seat, never drop explicit lane reservations,
        and never drop seats the calling pass has itself just reserved."""

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
            if protected_keys and _candidate_key(existing) in protected_keys:
                continue
            score = float(existing.score or 0.0)
            if score < replace_score:
                replace_score = score
                replace_pos = pos
        return replace_pos

    def swap_selected(replace_pos: int, candidate: FacetCandidate) -> None:
        """Swap a selected seat for ``candidate`` with full bookkeeping.

        Scores are never modified — seat protection is expressed through
        selection membership and diagnostics only."""

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

        for corpus_id in target:
            counts = corpus_counts()
            if counts.get(corpus_id, 0) > 0:
                continue
            candidate = eligible_by_corpus[corpus_id]
            if len(selected) < max_items:
                if add(candidate):
                    corpus_meta["added"] += 1
                    continue
            replace_pos = weakest_unprotected_pos(counts)
            if replace_pos is None:
                corpus_meta["skipped"].append(corpus_id)
                continue
            swap_selected(replace_pos, candidate)
            corpus_meta["replaced"] += 1

        corpus_meta["covered_corpora"] = [
            corpus_id for corpus_id in target if corpus_counts().get(corpus_id, 0) > 0
        ]
        return corpus_meta

    def reserve_shelf_roles() -> dict[str, Any] | None:
        """P1.5 shelf_reserve seat pass — runs AFTER ``reserve_corpora``.

        Reserves AT MOST one seat each for the best bridge and best
        counterbalance shelf-role candidate (in that priority) that (a)
        already exists in the candidate pool, (b) passes the calibrated
        P0.3 corpus-reservation bound
        (``reservation_policy.passes_corpus_reservation``), and (c) is not
        already selected. Roles come from card-field overlap only
        (``shelf_engine.assign_shelf_roles``); pool scores are NEVER
        modified and un-pooled documents are NEVER added. Skip beats weak
        fill: every skipped role records a reason. The returned dict is the
        reading-path seed (``reading_path`` is role-ordered over seated +
        already-present docs).
        """

        ctx = shelf_reserve_context
        if ctx is None:
            return None
        # Deferred import: `services.retriever` package init imports
        # `services.facets` (funnel_a/hydrate/...), so a module-level import
        # here would deadlock the partially-initialized facets package.
        from services.retriever.reservation_policy import passes_corpus_reservation

        shelf_meta: dict[str, Any] = {
            "enabled": False,
            "roles_considered": list(_SHELF_RESERVE_ROLES),
            "seated": [],
            "skipped": {},
            "policy_version": None,
            "reading_path": [],
        }
        query_concepts = [
            str(concept).strip()
            for concept in (getattr(ctx, "query_concepts", None) or [])
            if str(concept or "").strip()
        ]
        cards = [
            card
            for card in (getattr(ctx, "cards_by_doc", None) or {}).values()
            if isinstance(card, dict)
        ]
        if not getattr(ctx, "enabled", False):
            shelf_meta["skipped"] = {role: "disabled" for role in _SHELF_RESERVE_ROLES}
            return shelf_meta
        if not query_concepts:
            shelf_meta["skipped"] = {
                role: "no_query_concepts" for role in _SHELF_RESERVE_ROLES
            }
            return shelf_meta
        if not cards:
            shelf_meta["skipped"] = {
                role: "no_cards_for_pooled_documents" for role in _SHELF_RESERVE_ROLES
            }
            return shelf_meta

        shelf_meta["enabled"] = True
        assignment = assign_shelf_roles(query_concepts, cards)
        shelf_meta["policy_version"] = assignment.get("policy_version")
        engine_skips = assignment.get("skipped_roles") or {}

        # Role rows: (engine role score, doc_id, corpus_id, role dict),
        # ordered by engine score desc then doc_id/corpus_id asc — the
        # engine's own deterministic ordering. Engine scores order role
        # candidates only; they never touch pool scores.
        rows_by_role: dict[str, list[tuple[float, str, str, dict]]] = {
            role: [] for role in ALL_ROLES
        }
        for entry in assignment.get("assignments") or []:
            doc_id = str(entry.get("doc_id") or "")
            corpus_id = str(entry.get("corpus_id") or "")
            for role in entry.get("roles") or []:
                name = str(role.get("role") or "")
                if name in rows_by_role:
                    rows_by_role[name].append(
                        (float(role.get("score") or 0.0), doc_id, corpus_id, role)
                    )
        for rows in rows_by_role.values():
            rows.sort(key=lambda row: (-row[0], row[1], row[2]))

        # Best pooled candidate per doc (ranked = score-desc, junk-filtered):
        # the seat pass may only promote evidence that already exists in the
        # candidate pool — NEVER un-pooled documents.
        best_pool: dict[str, FacetCandidate] = {}
        for candidate in ranked:
            doc_id = str(candidate.doc_id or "")
            if doc_id and doc_id not in best_pool:
                best_pool[doc_id] = candidate

        def pooled_candidate(doc_id: str, corpus_id: str) -> FacetCandidate | None:
            candidate = best_pool.get(doc_id)
            if candidate is None:
                return None
            candidate_corpus = _candidate_corpus_id(candidate)
            if candidate_corpus and corpus_id and candidate_corpus != corpus_id:
                return None
            return candidate

        top_score = float(ranked[0].score or 0.0) if ranked else 0.0
        shelf_seated_keys: set[str] = set()
        for role_name in _SHELF_RESERVE_ROLES:
            rows = rows_by_role.get(role_name) or []
            if not rows:
                shelf_meta["skipped"][role_name] = str(
                    engine_skips.get(role_name) or "no_candidate_met_role_eligibility"
                )
                continue
            selected_docs = {str(c.doc_id or "") for c in selected if c.doc_id}
            already = next((row for row in rows if row[1] in selected_docs), None)
            if already is not None:
                # The role is already represented in the final packet; a
                # second seat would evict stronger evidence for redundant
                # role coverage (skip beats weak fill).
                shelf_meta["skipped"][role_name] = f"already_selected:{already[1]}"
                continue
            chosen: tuple[FacetCandidate, str, dict] | None = None
            any_pooled = False
            for _role_score, doc_id, corpus_id, role in rows:
                candidate = pooled_candidate(doc_id, corpus_id)
                if candidate is None:
                    continue
                any_pooled = True
                if not passes_corpus_reservation(
                    float(candidate.score or 0.0), top_score
                ):
                    continue
                chosen = (candidate, doc_id, role)
                break
            if chosen is None:
                shelf_meta["skipped"][role_name] = (
                    "below_reservation_bound"
                    if any_pooled
                    else "not_in_candidate_pool"
                )
                continue
            candidate, doc_id, role = chosen
            seated = False
            if len(selected) < max_items and add(candidate):
                seated = True
            else:
                replace_pos = weakest_unprotected_pos(
                    corpus_counts(), protected_keys=shelf_seated_keys
                )
                if replace_pos is not None:
                    swap_selected(replace_pos, candidate)
                    seated = True
            if not seated:
                shelf_meta["skipped"][role_name] = "no_replaceable_unprotected_seat"
                continue
            shelf_seated_keys.add(_candidate_key(candidate))
            shelf_meta["seated"].append(
                {
                    "doc_id": doc_id,
                    "role": role_name,
                    "matched_fields": role.get("matched_fields") or {},
                    "evidence_ids": role.get("evidence_ids") or [],
                }
            )

        # Reading-path seed: role-ordered (direct -> foundational -> adjacent
        # -> bridge -> counterbalance) over SEATED + already-present docs,
        # each doc once at its earliest role.
        final_docs = {str(c.doc_id or "") for c in selected if c.doc_id}
        path_seen: set[str] = set()
        for role_name in ALL_ROLES:
            for _role_score, doc_id, corpus_id, _role in rows_by_role.get(
                role_name
            ) or []:
                if doc_id not in final_docs or doc_id in path_seen:
                    continue
                path_seen.add(doc_id)
                shelf_meta["reading_path"].append(
                    {"doc_id": doc_id, "corpus_id": corpus_id, "role": role_name}
                )
        return shelf_meta

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

    # P1.5 shelf_reserve seat pass — after reserve_corpora, fully gated on
    # shelf_reserve_context (None = off, zero behavior change).
    shelf_reserve = reserve_shelf_roles()

    covered = [lane for lane in missing if lane_counts.get(lane, 0) > 0]
    priority_covered = [lane for lane in priority if lane_counts.get(lane, 0) > 0]
    selector_meta: dict[str, Any] = {
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
    if shelf_reserve is not None:
        selector_meta["shelf_reserve"] = shelf_reserve
    return [candidate.item for candidate in selected[:max_items]], selector_meta
