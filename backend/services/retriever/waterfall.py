"""Deterministic hydration waterfall — POLYMATH_ARCHITECTURE §5.2/§5.4 (owner-designed).

One ranked parent list + a fixed token budget → a byte-identical context packet.
Assembly is RULES ONLY (no LLM judgment):

  1. Fixed budget; walk ranks top-down, hydrating each parent at the richest
     form that fits the remaining budget: FULL TEXT → SUMMARY → SKIP.
  2. Two-lane anchoring (optional): anchor-lane candidates get a guaranteed
     budget quota and fill slots only while they clear a fixed rerank-score
     threshold; shortfall budget SPILLS to the expansion lane. No anchors →
     single-lane.
  3. Leftover budget fills in fixed order: orphan children (deduped against
     included parents) → shared entities last.
  4. Overflow rule: a full text that doesn't fit swaps to its summary — never
     truncate mid-text.
  5. Surplus rule: budget remaining after all slots promotes the next summary
     (rank order) up to full text.
  6. Dedupe rules: drop any child whose parent is included; a parent appears in
     exactly one form (ladder is per-parent, so summary-vs-full dedupe is
     structural).

Pure module: no I/O, no globals mutated, no randomness. Same inputs ⇒ identical
`packet_hash`. Token counting is injectable; the default uses cl100k_base
(matching the chunker) and falls back to a whitespace approximation only if
tiktoken is unavailable (the fallback is still deterministic).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Callable, Sequence

__all__ = [
    "ParentCandidate",
    "OrphanChild",
    "SharedEntity",
    "DocNote",
    "PacketItem",
    "Packet",
    "allocate",
]

_TOKENIZER = None


def _default_count_tokens(text: str) -> int:
    global _TOKENIZER
    if _TOKENIZER is None:
        try:
            import tiktoken

            _TOKENIZER = tiktoken.get_encoding("cl100k_base")
        except Exception:  # deterministic fallback
            _TOKENIZER = False
    if _TOKENIZER:
        return len(_TOKENIZER.encode(text or "", disallowed_special=()))
    return max(1, len((text or "").split()))


@dataclass(frozen=True)
class ParentCandidate:
    """One reranked parent. `lane` is 'anchor' or 'expansion' ('' = expansion).
    Rank = position in the input sequence (caller sorts by rerank score)."""

    parent_id: str
    doc_id: str
    score: float
    full_text: str
    summary: str = ""
    lane: str = ""


@dataclass(frozen=True)
class OrphanChild:
    """Cross-domain child fragment whose parent did not place."""

    chunk_id: str
    parent_id: str
    doc_id: str
    score: float
    text: str


@dataclass(frozen=True)
class SharedEntity:
    """Graph-layer entity line (fed last, cheapest)."""

    entity_id: str
    text: str


@dataclass(frozen=True)
class DocNote:
    """Passive source-role note. Lowest-priority synthesis context."""

    doc_id: str
    text: str


@dataclass(frozen=True)
class PacketItem:
    kind: str  # "full" | "summary" | "child" | "entity" | "doc_note"
    ref_id: str  # parent_id / chunk_id / entity_id
    doc_id: str
    lane: str
    tokens: int
    text: str


@dataclass
class Packet:
    items: list[PacketItem] = field(default_factory=list)
    packet_hash: str = ""
    budget_tokens: int = 0
    used_tokens: int = 0
    diagnostics: dict = field(default_factory=dict)


def _hash(items: Sequence[PacketItem]) -> str:
    h = hashlib.sha256()
    for it in items:
        h.update(f"{it.kind}\x1f{it.ref_id}\x1f{it.lane}\x1f".encode())
        h.update(it.text.encode())
        h.update(b"\x1e")
    return h.hexdigest()


def _ladder(
    parents: Sequence[ParentCandidate],
    budget: int,
    count: Callable[[str], int],
    lane: str,
) -> tuple[list[PacketItem], int]:
    """Rule 1+4: rank walk, full → summary → skip; never truncate."""
    items: list[PacketItem] = []
    used = 0
    for p in parents:
        full_t = count(p.full_text) if p.full_text else 0
        if p.full_text and used + full_t <= budget:
            items.append(PacketItem("full", p.parent_id, p.doc_id, lane, full_t, p.full_text))
            used += full_t
            continue
        sum_t = count(p.summary) if p.summary else 0
        if p.summary and used + sum_t <= budget:
            items.append(PacketItem("summary", p.parent_id, p.doc_id, lane, sum_t, p.summary))
            used += sum_t
        # else: skip — degrade gracefully, keep walking ranks
    return items, used


def allocate(
    ranked_parents: Sequence[ParentCandidate],
    *,
    budget_tokens: int,
    orphans: Sequence[OrphanChild] = (),
    entities: Sequence[SharedEntity] = (),
    doc_notes: Sequence[DocNote] = (),
    anchor_quota: float = 0.6,
    spillover_threshold: float | None = None,
    count_tokens: Callable[[str], int] | None = None,
) -> Packet:
    """Assemble the deterministic context packet.

    ranked_parents MUST be pre-sorted by rerank score (desc) — rank is
    positional. When any candidate carries lane='anchor', two-lane mode
    activates: the anchor lane gets `anchor_quota` of the budget (guaranteed
    slots) and, with `spillover_threshold` set, anchor candidates below the
    threshold do not consume anchor slots — that budget spills to expansion.
    """
    count = count_tokens or _default_count_tokens
    budget = max(0, int(budget_tokens))
    diag: dict = {"mode": "single_lane", "spilled_tokens": 0}

    anchors = [p for p in ranked_parents if p.lane == "anchor"]
    expansion = [p for p in ranked_parents if p.lane != "anchor"]

    items: list[PacketItem] = []
    used = 0

    if anchors:
        diag["mode"] = "two_lane"
        # Rule 2 — spillover: anchor slots fill only while candidates clear
        # the fixed rerank threshold; the rest of the anchor budget spills.
        eligible = (
            [p for p in anchors if spillover_threshold is None or p.score >= spillover_threshold]
        )
        diag["anchor_candidates"] = len(anchors)
        diag["anchor_eligible"] = len(eligible)
        anchor_budget = int(budget * anchor_quota)
        a_items, a_used = _ladder(eligible, anchor_budget, count, "anchor")
        spilled = anchor_budget - a_used
        diag["spilled_tokens"] = spilled
        e_budget = budget - anchor_budget + spilled
        e_items, e_used = _ladder(expansion, e_budget, count, "expansion")
        items = a_items + e_items
        used = a_used + e_used
    else:
        items, used = _ladder(expansion, budget, count, "expansion")

    # Rule 6 — dedupe sets from the ladder result.
    included_parents = {it.ref_id for it in items}
    full_parents = {it.ref_id for it in items if it.kind == "full"}

    # Rule 3a — orphan children, deduped against included parents.
    dropped_orphans = 0
    for o in orphans:
        if o.parent_id in included_parents:
            dropped_orphans += 1
            continue
        t = count(o.text)
        if o.text and used + t <= budget:
            items.append(PacketItem("child", o.chunk_id, o.doc_id, "expansion", t, o.text))
            used += t
            included_parents.add(o.parent_id)  # its parent is now represented
    diag["orphans_dropped_parent_included"] = dropped_orphans

    # Rule 3b — shared entities last among graph/evidence signals.
    for e in entities:
        t = count(e.text)
        if e.text and used + t <= budget:
            items.append(PacketItem("entity", e.entity_id, "", "graph", t, e.text))
            used += t

    # Rule 5 — surplus promotes summaries → full, in packet (rank) order.
    promoted = 0
    by_id = {p.parent_id: p for p in ranked_parents}
    for idx, it in enumerate(items):
        if it.kind != "summary":
            continue
        p = by_id.get(it.ref_id)
        if p is None or not p.full_text:
            continue
        full_t = count(p.full_text)
        if used - it.tokens + full_t <= budget:
            items[idx] = PacketItem("full", p.parent_id, p.doc_id, it.lane, full_t, p.full_text)
            used = used - it.tokens + full_t
            full_parents.add(p.parent_id)
            promoted += 1
    diag["summaries_promoted"] = promoted

    # Rule 7 — passive source-role notes consume only true leftover budget after
    # evidence selection and summary promotion. They are synthesis hints, not
    # evidence, so token pressure drops them before any evidence-bearing item.
    for note in doc_notes:
        t = count(note.text)
        if note.text and used + t <= budget:
            items.append(PacketItem("doc_note", note.doc_id, note.doc_id, "note", t, note.text))
            used += t

    pkt = Packet(
        items=items,
        packet_hash=_hash(items),
        budget_tokens=budget,
        used_tokens=used,
        diagnostics=diag,
    )
    diag["counts"] = {
        k: sum(1 for it in items if it.kind == k)
        for k in ("full", "summary", "child", "entity", "doc_note")
    }
    return pkt
