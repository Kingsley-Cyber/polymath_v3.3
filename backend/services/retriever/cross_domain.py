"""Q4 — CROSS_DOMAIN_EMPHASIS (off | balanced | strong). §11 H4+G4, owner-steered.

The cross-domain steer, delivered as three rank/budget-shaping levers (no
hard filters; the cross-encoder stays the scoring authority; `balanced`
reproduces pre-Q4 behavior EXACTLY):

  bridge_cap()            — scales Mode A's Phase-5b bridge-lane budget
                            (off: lane dark · balanced: limit//4, today's
                            3:1 throttle · strong: bridges compete for half)
  domain_reserve_swap()   — final-cut guarantee: on breadth-shaped queries,
                            if every selected chunk shares one domain and a
                            different-domain candidate clears a relaxed
                            floor, it takes the LAST slot (never the top)
  mechanisms_overlap_bonus() — rank-only additive for candidates that share
                            a transferable mechanism (compounding,
                            feedback_loop, …) with the pool leaders but come
                            from a DIFFERENT document — the bridge signal

Pure and deterministic: no I/O, fixed tie-breaks, mode strings validated.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

MODES = ("off", "balanced", "strong")


def normalize_mode(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in MODES else "balanced"


def bridge_cap(limit: int, mode: str) -> int:
    """Phase-5b bridge-lane budget for Mode A. balanced == the historical
    max(2, limit//4) throttle; strong lets bridges compete for half."""
    limit = max(0, int(limit))
    mode = normalize_mode(mode)
    if limit == 0 or mode == "off":
        return 0
    if mode == "strong":
        return max(4, limit // 2)
    return max(2, limit // 4)


def _domain_of(chunk: Any) -> str:
    direct = getattr(chunk, "domain", None)
    if direct:
        return str(direct).strip().lower()
    md = getattr(chunk, "metadata", None) or {}
    return str(md.get("domain") or "").strip().lower()


def domain_reserve_swap(
    selected: Sequence[Any],
    ranked: Sequence[Any],
    *,
    mode: str,
    broad: bool,
    balanced_intent: bool = False,
    min_score_ratio: float = 0.25,
) -> tuple[list[Any], Optional[str]]:
    """H4 — distinct-DOMAIN breadth next to distinct-doc breadth.

    Fires only when: mode active for this intent (balanced -> BROAD queries;
    strong -> BROAD or BALANCED), >=2 slots, EVERY selected chunk with a
    known domain shares the top chunk's domain, and some unselected ranked
    candidate has a different non-empty domain with score >= ratio*top.
    The best such candidate replaces the LAST slot (top of the answer is
    never touched). Returns (new_selected, swapped_domain|None).
    """
    mode = normalize_mode(mode)
    out = list(selected)
    if mode == "off" or len(out) < 2 or not ranked:
        return out, None
    if not (broad or (mode == "strong" and balanced_intent)):
        return out, None

    top_domain = _domain_of(out[0])
    if not top_domain:
        return out, None
    known = [_domain_of(c) for c in out]
    if any(d and d != top_domain for d in known):
        return out, None  # already domain-diverse

    top_score = float(getattr(ranked[0], "score", 0.0) or 0.0)
    floor = top_score * float(min_score_ratio)
    selected_ids = {str(getattr(c, "chunk_id", "") or "") for c in out}
    for cand in ranked:
        cid = str(getattr(cand, "chunk_id", "") or "")
        if not cid or cid in selected_ids:
            continue
        d = _domain_of(cand)
        if d and d != top_domain and float(getattr(cand, "score", 0.0) or 0.0) >= floor:
            out[-1] = cand
            return out, d
    return out, None


def _mechanisms_of(chunk: Any) -> frozenset[str]:
    md = getattr(chunk, "metadata", None) or {}
    vals = md.get("mechanisms") or []
    return frozenset(str(m).strip().lower() for m in vals if str(m).strip())


def mechanisms_overlap_bonus(
    ranked: Sequence[Any],
    *,
    mode: str,
    leaders: int = 3,
    bonus: float = 0.02,
) -> int:
    """Rank-only additive: a candidate OUTSIDE the leaders that shares >=1
    mechanism with the leaders' union AND comes from a different document
    gets +bonus (capped at 1.0, applied in place). Returns boosted count.
    Inert until promoted mechanisms[] exist on payloads — by design."""
    mode = normalize_mode(mode)
    if mode == "off" or bonus <= 0 or len(ranked) <= leaders:
        return 0
    lead = list(ranked)[:leaders]
    lead_mechs: set[str] = set()
    for c in lead:
        lead_mechs |= _mechanisms_of(c)
    if not lead_mechs:
        return 0
    lead_docs = {str(getattr(c, "doc_id", "") or "") for c in lead}
    boosted = 0
    for c in list(ranked)[leaders:]:
        if str(getattr(c, "doc_id", "") or "") in lead_docs:
            continue
        if _mechanisms_of(c) & lead_mechs:
            c.score = min(1.0, float(getattr(c, "score", 0.0) or 0.0) + bonus)
            boosted += 1
    return boosted
